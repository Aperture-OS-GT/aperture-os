"""Virtual Rig: an in-memory Raspberry Pi bridge speaking protocol v1.

The rig owns a 40-pin header model, a deterministic clock, a scheduler, and a
list of behaviors (loopbacks, fake devices) that drive pins. It executes the
same ops the Pi bridge executes and emits the same events, so the UI, agent
tools, sessions and tests behave identically with no hardware present.
"""
from __future__ import annotations

import heapq
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from .. import pins as P
from ..protocol import DRIVING_OPS, ProtocolError, event, validate_request

SCENARIO_DIR = Path(__file__).with_name("scenarios")
FLASH_APP_OFFSET = 0x10000           # esptool layout: the application image
FLASH_STAGE_LIMIT = 4 * 1024 * 1024  # ESP32 DevKit V1 flash size
FLASH_BAUD = 115200
UART_BAUD_MIN, UART_BAUD_MAX = 300, 4_000_000


class VirtualClock:
    """Microsecond clock. Manual mode advances only via ``advance``; realtime follows the wall clock."""

    def __init__(self, realtime: bool = False):
        self.realtime = realtime
        self._t = 0
        self._wall0 = time.monotonic_ns()

    def now_us(self) -> int:
        if self.realtime:
            return (time.monotonic_ns() - self._wall0) // 1000
        return self._t

    def advance(self, us: int) -> None:
        if self.realtime:
            raise RuntimeError("cannot advance a realtime clock")
        self._t += int(us)


@dataclass
class PinState:
    bcm: int
    mode: str = "disabled"
    level: int = 0
    drive: int | None = None          # level the rig drives when mode == output/pwm
    external: int | None = None       # level a behavior (the DUT) drives onto the pin
    pwm: tuple[float, float] | None = None
    floating: bool = True
    contention: bool = False

    def snapshot(self) -> dict:
        data = {"pin": self.bcm, "mode": self.mode, "level": self.level, "floating": self.floating}
        if self.pwm:
            data["pwm"] = {"freq_hz": self.pwm[0], "duty": self.pwm[1]}
        if self.contention:
            data["contention"] = True
        return data


@dataclass
class UartPort:
    port: int
    tx: int
    rx: int
    baud: int
    rxbuf: bytearray = field(default_factory=bytearray)


@dataclass
class Capture:
    capture_id: int
    pins: list[int]
    rate_hz: int
    start_us: int
    end_us: int
    initial: dict
    edges: list = field(default_factory=list)
    done: bool = False


class VirtualRig:
    BRIDGE = "virtual-rig"

    def __init__(self, scenario: dict | None = None, realtime: bool = False, limits: dict | None = None):
        self.clock = VirtualClock(realtime)
        self.lock = threading.RLock()
        self.limits = {**P.DEFAULT_LIMITS, **(limits or {})}
        self.pins = {bcm: PinState(bcm) for bcm in P.GPIO_PINS}
        self.behaviors: list = []
        self.i2c_devices: dict[int, object] = {}
        self.spi_devices: dict[int, object] = {}
        self.ports: dict[int, UartPort] = {}
        self.captures: dict[int, Capture] = {}
        self.streams: set[int] = set()
        self.controller: dict | None = None
        self.scenario = scenario or {"name": "empty", "behaviors": []}
        self._listeners: list[Callable[[dict], None]] = []
        self._queue: list = []
        self._seq = 0
        self._next_port = 0
        self._next_capture = 1
        self._pwm_gen: dict[int, int] = {}
        self._uart_busy_until: dict[int, int] = {}
        self._flash: dict[int, int] = {}  # staged image sizes by offset; the bytes are never needed
        from .behaviors import make_behavior
        for spec in self.scenario.get("behaviors", []):
            self.add_behavior(make_behavior(spec))

    # --- plumbing -----------------------------------------------------------------
    def now_us(self) -> int:
        return self.clock.now_us()

    def on_event(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def _emit(self, name: str, **payload) -> None:
        message = event(name, self.now_us(), **payload)
        for callback in list(self._listeners):
            callback(message)

    def at(self, t_us: int, fn: Callable[[], None]) -> None:
        self._seq += 1
        heapq.heappush(self._queue, (int(t_us), self._seq, fn))

    def after(self, us: int, fn: Callable[[], None]) -> None:
        self.at(self.now_us() + us, fn)

    def pump(self) -> int:
        """Run every scheduled callback that is due. Returns how many ran."""
        ran = 0
        with self.lock:
            while self._queue and self._queue[0][0] <= self.now_us():
                _, _, fn = heapq.heappop(self._queue)
                fn()
                ran += 1
            self._check_watchdog()
        return ran

    def advance(self, us: int) -> None:
        """Manual clock only: step time forward, firing callbacks in order at their exact times."""
        with self.lock:
            target = self.now_us() + int(us)
            while self._queue and self._queue[0][0] <= target:
                t, _, fn = heapq.heappop(self._queue)
                self.clock._t = max(self.clock._t, t)
                fn()
                self._check_watchdog()
            self.clock._t = target
            self._check_watchdog()

    def add_behavior(self, behavior) -> None:
        self.behaviors.append(behavior)
        behavior.attach(self)

    def behavior(self, name: str):
        for b in self.behaviors:
            if getattr(b, "name", None) == name:
                return b
        raise KeyError(name)

    def manifest(self) -> dict:
        return P.build_manifest(self.BRIDGE, f"virtual-{__version__}", "Virtual Rig (Pi 4 header)",
                                self.limits, {"vbus_ok": True},
                                denied=set(self.scenario.get("denied_pins", [])))

    # --- electrical model ---------------------------------------------------------
    def drive_external(self, bcm: int, level: int | None) -> None:
        """A behavior (the DUT side) drives ``level`` onto the pin, or None to let go."""
        pin = self.pins[bcm]
        pin.external = level
        self._resolve(pin)

    def _resolve(self, pin: PinState) -> None:
        old = pin.level
        contention = False
        if pin.mode in ("output", "pwm") and pin.drive is not None:
            level, floating = pin.drive, False
            contention = pin.external is not None and pin.external != pin.drive
        elif pin.external is not None:
            level, floating = pin.external, False
        elif pin.mode == "input_pullup":
            level, floating = 1, False
        elif pin.mode == "input_pulldown":
            level, floating = 0, False
        else:
            level, floating = pin.level, True
        if contention and not pin.contention:
            self._emit("rig.warning", pin=pin.bcm, warning="contention",
                       detail=f"GPIO{pin.bcm} driven to {pin.drive} by rig and {pin.external} by DUT")
        pin.contention, pin.floating, pin.level = contention, floating, level
        if level != old:
            self._level_changed(pin, level)

    def _level_changed(self, pin: PinState, level: int) -> None:
        t = self.now_us()
        for capture in self.captures.values():
            if not capture.done and pin.bcm in capture.pins:
                quantum = 1_000_000 / capture.rate_hz
                capture.edges.append([int(round(t / quantum) * quantum), pin.bcm, level])
        if pin.bcm in self.streams:
            self._emit("pin.change", pin=pin.bcm, level=level)
        for behavior in self.behaviors:
            behavior.on_pin(pin.bcm, level, t)

    def _set_drive(self, bcm: int, level: int | None) -> None:
        pin = self.pins[bcm]
        pin.drive = level
        self._resolve(pin)

    # --- UART plumbing used by behaviors --------------------------------------------
    def uart_deliver(self, rx_pin: int, data: bytes) -> None:
        """A behavior sends bytes towards the host on ``rx_pin``; one byte per 10 bit-times, queued
        behind anything the same device is still transmitting so streams never interleave."""
        for port in self.ports.values():
            if port.rx == rx_pin:
                byte_us = 10_000_000 / port.baud
                start = max(self.now_us(), self._uart_busy_until.get(rx_pin, 0))
                for i, b in enumerate(data):
                    self.at(round(start + (i + 1) * byte_us),
                            lambda b=b, port=port: self._uart_rx(port, bytes([b])))
                self._uart_busy_until[rx_pin] = round(start + len(data) * byte_us)
                return

    def uart_open_for(self, rx_pin: int) -> bool:
        return any(port.rx == rx_pin for port in self.ports.values())

    def _uart_rx(self, port: UartPort, data: bytes) -> None:
        if port.port not in self.ports:
            return
        port.rxbuf += data
        self._emit("uart.rx", port=port.port, data=data)

    # --- control -------------------------------------------------------------------
    def _check_watchdog(self) -> None:
        if self.controller and self.now_us() - self.controller["seen"] > self.controller["watchdog_s"] * 1e6:
            self._release_all()
            self.controller = None
            self._emit("control.lost", reason="watchdog")

    def client_disconnected(self, token: str | None) -> None:
        with self.lock:
            if self.controller and (token is None or self.controller["token"] == token):
                self._release_all()
                self.controller = None
                self._emit("control.lost", reason="disconnect")

    # --- execution -----------------------------------------------------------------
    def execute(self, message: dict) -> dict:
        """Execute one raw request map. Returns the result map; raises ProtocolError."""
        request = validate_request(message)
        with self.lock:
            self.pump()
            op = request["op"]
            if op in DRIVING_OPS:
                if not self.controller:
                    raise ProtocolError("not_controller", "take control first")
                self.controller["seen"] = self.now_us()
            elif self.controller and op == "ping":
                self.controller["seen"] = self.now_us()
            handler = getattr(self, "_op_" + op.replace(".", "_"))
            return handler(request)

    def _pin(self, bcm: int) -> PinState:
        if bcm in self.scenario.get("denied_pins", []):
            raise ProtocolError("unsupported", f"GPIO{bcm} is denied by the rig configuration")
        return self.pins[bcm]

    def _notify_pin(self, bcm: int) -> None:
        pin = self.pins[bcm]
        for behavior in self.behaviors:
            behavior.on_pin(bcm, pin.level, self.now_us())

    def _op_hello(self, r):
        self._client = r["client"]
        return {"manifest": self.manifest()}

    def _op_ping(self, r):
        return {}

    def _op_control_take(self, r):
        now = self.now_us()
        token = r.get("token") or secrets.token_hex(8)
        if self.controller and self.controller["token"] != token:
            idle = (now - self.controller["seen"]) / 1e6
            if not (r.get("force") and idle >= self.limits["idle_s"]):
                raise ProtocolError("busy", "rig is controlled by another client",
                                    controller=self.controller["client"], idle_s=round(idle, 1))
            previous, reason = self.controller["client"], "force"
            self._release_all()
        else:
            previous, reason = None, "take"
        self.controller = {"token": token, "client": self._client, "seen": now,
                           "watchdog_s": float(r.get("watchdog_s") or self.limits["watchdog_s"])}
        self._emit("control.changed", controller=self._client, previous=previous, reason=reason)
        return {"controller": True, "token": token, "watchdog_s": self.controller["watchdog_s"]}

    _client = "host"

    def _op_control_release(self, r):
        if not self.controller or self.controller["token"] != r["token"]:
            raise ProtocolError("not_controller", "token does not hold control")
        self._release_all()
        self.controller = None
        self._emit("control.changed", controller=None, reason="release")
        return {}

    def _op_list_pins(self, r):
        return {"pins": [self.pins[b].snapshot() for b in sorted(self.pins)],
                "controller": self.controller["client"] if self.controller else None}

    def _op_pin_mode(self, r):
        pin = self._pin(r["pin"])
        mode = r["mode"]
        if any(r["pin"] in (p.tx, p.rx) for p in self.ports.values()):
            raise ProtocolError("pin_mode", f"GPIO{r['pin']} is in use by a UART port")
        if pin.mode == "pwm" and mode != "pwm":
            self._stop_pwm(pin)
        pin.mode = mode
        if mode == "output":
            pin.drive = r.get("initial", pin.drive if pin.drive is not None else 0)
        elif mode == "pwm":
            pin.drive = pin.drive if pin.drive is not None else 0
        else:
            pin.drive = None
        self._resolve(pin)
        self._notify_pin(pin.bcm)
        return {"pin": pin.bcm, "mode": pin.mode, "level": pin.level}

    def _op_pin_write(self, r):
        pin = self._pin(r["pin"])
        if pin.mode != "output":
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is {pin.mode}, not output")
        self._set_drive(pin.bcm, r["level"])
        return {"pin": pin.bcm, "level": pin.level}

    def _op_pin_read(self, r):
        pin = self._pin(r["pin"])
        if pin.mode == "disabled":
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is disabled")
        return {"pin": pin.bcm, "level": pin.level, "floating": pin.floating}

    def _op_pin_pulse(self, r):
        pin = self._pin(r["pin"])
        if pin.mode != "output":
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is {pin.mode}, not output")
        if r["us"] > self.limits["pulse_us"]:
            raise ProtocolError("range", f"pulse longer than {self.limits['pulse_us']} us")
        previous = pin.drive
        t0 = self.now_us()
        self._set_drive(pin.bcm, r["level"])
        if self.clock.realtime:
            time.sleep(r["us"] / 1e6)
        else:
            self.advance(r["us"])
        self._set_drive(pin.bcm, previous)
        return {"pin": pin.bcm, "edges": [[t0, r["level"]], [self.now_us(), previous]], "jitter_us": 0}

    def _op_pin_pwm(self, r):
        pin = self._pin(r["pin"])
        if pin.mode != "pwm":
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is {pin.mode}, not pwm")
        if r["freq_hz"] > self.limits["pwm_hz"]:
            raise ProtocolError("range", f"freq_hz above {self.limits['pwm_hz']}")
        self._stop_pwm(pin)
        if r["duty"] <= 0:
            self._set_drive(pin.bcm, 0)
        elif r["duty"] >= 1:
            self._set_drive(pin.bcm, 1)
        else:
            pin.pwm = (r["freq_hz"], r["duty"])
            self._pwm_gen[pin.bcm] = self._pwm_gen.get(pin.bcm, 0) + 1
            self._pwm_edge(pin.bcm, self._pwm_gen[pin.bcm], 1)
        self._notify_pin(pin.bcm)
        return {"pin": pin.bcm, "freq_hz": r["freq_hz"], "duty": r["duty"]}

    def _pwm_edge(self, bcm: int, generation: int, level: int) -> None:
        pin = self.pins[bcm]
        if self._pwm_gen.get(bcm) != generation or pin.pwm is None:
            return
        freq, duty = pin.pwm
        self._set_drive(bcm, level)
        period = 1e6 / freq
        self.after(round(period * (duty if level else 1 - duty)),
                   lambda: self._pwm_edge(bcm, generation, 1 - level))

    def _stop_pwm(self, pin: PinState) -> None:
        pin.pwm = None
        self._pwm_gen[pin.bcm] = self._pwm_gen.get(pin.bcm, 0) + 1

    def _op_uart_open(self, r):
        for pin in (self.pins[r["tx"]], self.pins[r["rx"]]):
            if pin.mode != "disabled" or any(pin.bcm in (p.tx, p.rx) for p in self.ports.values()):
                raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is already in use")
        _check_baud(r["baud"])
        port = UartPort(self._next_port, r["tx"], r["rx"], r["baud"])
        self._next_port += 1
        self.ports[port.port] = port
        for behavior in self.behaviors:
            behavior.on_uart_open(port.tx, port.rx, port.baud)
        return {"port": port.port, "tx": port.tx, "rx": port.rx, "baud": port.baud}

    def _port(self, number: int) -> UartPort:
        if number not in self.ports:
            raise ProtocolError("bad_request", f"port {number} is not open")
        return self.ports[number]

    def _op_uart_close(self, r):
        self._port(r["port"])
        del self.ports[r["port"]]
        return {}

    def _op_uart_write(self, r):
        port = self._port(r["port"])
        byte_us = 10_000_000 / port.baud
        self.after(round(len(r["data"]) * byte_us),
                   lambda: [b.on_uart(port.tx, r["data"], self.now_us()) for b in self.behaviors])
        return {"n": len(r["data"])}

    def _op_uart_read(self, r):
        port = self._port(r["port"])
        timeout = r.get("timeout_ms", 0)
        if not port.rxbuf and timeout:
            if self.clock.realtime:
                deadline = time.monotonic() + timeout / 1000
                while not port.rxbuf and time.monotonic() < deadline:
                    self.lock.release()
                    try:
                        time.sleep(0.002)
                    finally:
                        self.lock.acquire()
                    self.pump()
            else:
                self.advance(timeout * 1000)
        n = r.get("max", len(port.rxbuf)) or len(port.rxbuf)
        data = bytes(port.rxbuf[:n])
        del port.rxbuf[:n]
        return {"data": data}

    def _op_spi_xfer(self, r):
        if r["bus"] != 0:
            raise ProtocolError("unsupported", "only SPI bus 0 exists")
        device = self.spi_devices.get(r["cs"])
        if device is None:
            return {"data": bytes(len(r["data"]))}
        return {"data": bytes(device.spi_xfer(r["data"]))}

    def _i2c(self, r):
        if r["bus"] != 1:
            raise ProtocolError("unsupported", "only I2C bus 1 exists")
        device = self.i2c_devices.get(r["addr"])
        if device is None:
            raise ProtocolError("io", f"no device acknowledged at 0x{r['addr']:02x}")
        return device

    def _op_i2c_write(self, r):
        self._i2c(r).i2c_write(r["data"])
        return {"n": len(r["data"])}

    def _op_i2c_read(self, r):
        return {"data": bytes(self._i2c(r).i2c_read(r["n"], r.get("reg")))}

    def _op_i2c_scan(self, r):
        if r["bus"] != 1:
            raise ProtocolError("unsupported", "only I2C bus 1 exists")
        return {"addrs": sorted(self.i2c_devices)}

    def _op_capture_start(self, r):
        if r["rate_hz"] > self.limits["capture_hz"]:
            raise ProtocolError("range", f"rate_hz above {self.limits['capture_hz']}")
        if any(not c.done for c in self.captures.values()):
            raise ProtocolError("busy", "a capture is already running")
        capture = Capture(self._next_capture, r["pins"], r["rate_hz"], self.now_us(),
                          self.now_us() + r["duration_ms"] * 1000, {p: self.pins[p].level for p in r["pins"]})
        self._next_capture += 1
        self.captures[capture.capture_id] = capture
        self.at(capture.end_us, lambda: self._finish_capture(capture, False))
        return {"capture_id": capture.capture_id, "rate_hz_actual": capture.rate_hz}

    def _finish_capture(self, capture: Capture, cancelled: bool) -> None:
        if capture.done:
            return
        capture.done = True
        self._emit("capture.done", capture_id=capture.capture_id, initial={str(k): v for k, v in capture.initial.items()},
                   edges=capture.edges, rate_hz_actual=capture.rate_hz, cancelled=cancelled,
                   start_us=capture.start_us, pins=capture.pins)

    def _op_capture_cancel(self, r):
        capture = self.captures.get(r["capture_id"])
        if capture is None:
            raise ProtocolError("bad_request", "unknown capture_id")
        self._finish_capture(capture, True)
        return {}

    def _op_stream_set(self, r):
        if r["on"]:
            self.streams.update(r["pins"])
        else:
            self.streams.difference_update(r["pins"])
        return {"pins": sorted(self.streams)}

    # --- flashing (simulated: images are staged and reported, never interpreted) -------
    def _op_flash_begin(self, r):
        self._flash = {}
        return {}

    def _op_flash_write(self, r):
        if sum(self._flash.values()) + len(r["data"]) > FLASH_STAGE_LIMIT:
            raise ProtocolError("range", f"staged images exceed {FLASH_STAGE_LIMIT // (1024 * 1024)} MiB")
        size = self._flash.get(r["offset"], 0) + len(r["data"])
        self._flash = {**self._flash, r["offset"]: size}
        return {"offset": r["offset"], "size": size}

    def _op_flash_run(self, r):
        staged, self._flash = self._flash, {}  # every run consumes the staging, whatever the outcome
        if FLASH_APP_OFFSET not in staged:
            raise ProtocolError("bad_request", f"no app image staged at 0x{FLASH_APP_OFFSET:x}")
        baud = r.get("baud", FLASH_BAUD)
        _check_baud(baud)
        closed = self._close_uarts()  # the real flasher needs the serial device
        for behavior in self.behaviors:
            behavior.on_flash()
        images = [{"offset": offset, "size": size} for offset, size in sorted(staged.items())]
        return {"flashed": True, "simulated": True, "seconds": 0.0, "images": images, "uart_closed": closed,
                "log": _flash_log(images, baud, r.get("manual_boot", False))}

    def _close_uarts(self) -> list[int]:
        closed = sorted(self.ports)
        self.ports.clear()
        self._uart_busy_until.clear()
        return closed

    def _op_release_all(self, r):
        self._release_all()
        return {}

    def _release_all(self) -> None:
        released = [bcm for bcm, pin in self.pins.items() if pin.mode != "disabled"]
        for pin in self.pins.values():
            self._stop_pwm(pin)
            pin.mode = "disabled"
            pin.drive = None
            self._resolve(pin)
        self._close_uarts()
        for capture in self.captures.values():
            self._finish_capture(capture, True)
        self.streams.clear()
        for bcm in released:  # as pin.mode does: e.g. a released reset line lets the DUT boot
            self._notify_pin(bcm)

    def reset_dut(self) -> None:
        """Reset every behavior (the DUT side), e.g. after a scenario change."""
        with self.lock:
            for behavior in self.behaviors:
                behavior.reset()


def _check_baud(baud: int) -> None:
    if not UART_BAUD_MIN <= baud <= UART_BAUD_MAX:
        raise ProtocolError("range", f"baud must be {UART_BAUD_MIN}..{UART_BAUD_MAX}")


def _flash_log(images: list[dict], baud: int, manual_boot: bool) -> str:
    parts = ", ".join(f"0x{image['offset']:x} ({image['size']} bytes)" for image in images)
    boot = "manual BOOT" if manual_boot else "GPIO24 BOOT"
    return f"simulated write-flash at {baud} baud ({boot}, GPIO25 EN): {parts}"


def load_scenario(name_or_path: str) -> dict:
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIO_DIR / f"{name_or_path}.json"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in SCENARIO_DIR.glob("*.json")))
        raise FileNotFoundError(f"unknown scenario {name_or_path!r}; available: {available}")
    return json.loads(path.read_text())


def list_scenarios() -> list[dict]:
    out = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        out.append({"name": path.stem, "title": data.get("title", path.stem), "description": data.get("description", "")})
    return out
