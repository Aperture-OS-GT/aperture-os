"""Protocol v1 executor for a real header, behind a ``HardwareDriver``.

Op semantics, error codes and payloads mirror ``aperture.rig.virtual.VirtualRig`` so the
host cannot tell a bench from the Virtual Rig except by timing. Hardware-only rules:

* pins owned by an enabled bus (UART0 14/15, I2C1 2/3, SPI0 7-11) or by flashing (the
  DUT's BOOT and EN) are reserved: GPIO ops on them fail ``pin_mode`` because claiming them
  would take the pin away from its owner;
* ``flash.*`` programs the ESP32 DUT over UART0 (``hardware_flash``);
* captures sample on a thread at ``rate_hz`` (edges quantised like the Virtual Rig);
* stream events come from driver edge callbacks for inputs and from the executor's own
  writes for outputs; software PWM edges are not streamed;
* ``floating`` is ``None`` (unknown) for pins without a pull or driver;
* every exit path (watchdog, disconnect, release, shutdown) disables every pin.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from aperture import __version__
from aperture import pins as P
from aperture.protocol import DRIVING_OPS, ProtocolError, event, validate_request

from .control import Control
from .drivers import HardwareDriver
from .hardware_buses import NO_BUSES, BusConfig, BusOps
from .hardware_flash import FlashOps

log = logging.getLogger(__name__)
CONTROLLER_OPS = frozenset({"ping", "uart.read", "i2c.read", "i2c.scan"})
SPIN_US = 200  # busy-wait the last stretch before a capture sample for steadier timing


@dataclass
class HwPin:
    bcm: int
    mode: str = "disabled"
    drive: int | None = None
    pwm: tuple[float, float] | None = None
    last: int | None = None   # last level reported in a pin.change


@dataclass
class HwCapture:
    capture_id: int
    pins: list[int]
    rate_hz: int
    start_us: int
    end_us: int
    initial: dict
    edges: list = field(default_factory=list)
    samples: int = 0
    done: bool = False


class HardwareExecutor(BusOps, FlashOps):
    def __init__(self, driver: HardwareDriver, name: str, buses: BusConfig = NO_BUSES,
                 limits: dict | None = None, denied: frozenset[int] = frozenset()):
        self.driver, self.name, self.buses = driver, name, buses
        self.limits = {**P.DEFAULT_LIMITS, **(limits or {})}
        self.denied = frozenset(denied)
        self.lock = threading.RLock()
        self._rx = threading.Condition(self.lock)
        self.reserved = buses.reserved_pins()
        self.pins = {bcm: HwPin(bcm) for bcm in P.GPIO_PINS if bcm not in self.denied}
        self.ports: dict = {}
        self.flash_images: dict[int, bytearray] = {}  # offset -> bytes staged by flash.write
        self.captures: dict[int, HwCapture] = {}
        self.streams: set[int] = set()
        self._next_capture = 1
        self._client = "host"
        self._listeners: list[Callable[[dict], None]] = []
        self.control = Control(self.now_us, self.limits, self._release_all, self._emit)
        for pin in self.pins.values():
            if pin.bcm not in self.reserved:
                self._disable(pin)

    # --- executor protocol ---------------------------------------------------------------
    def now_us(self) -> int:
        return self.driver.now_us()

    def on_event(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def _emit(self, name: str, t_us: int | None = None, **payload) -> None:
        message = event(name, self.now_us() if t_us is None else t_us, **payload)
        for callback in list(self._listeners):
            callback(message)

    def pump(self) -> None:
        with self.lock:
            self.control.check_watchdog()

    def client_disconnected(self, token: str | None) -> None:
        with self.lock:
            self.control.disconnected(token)

    def close(self) -> None:
        with self.lock:
            self._release_all()
        self.driver.close()

    @property
    def controller(self):
        return self.control.controller

    def manifest(self) -> dict:
        manifest = P.build_manifest(self.name, f"aperture-bridge-{__version__}", self.driver.board,
                                    self.limits, self.driver.power(), denied=self.denied)
        return {**manifest, **self.buses.manifest_entries()}

    def execute(self, message: dict) -> dict:
        request = validate_request(message)
        op = request["op"]
        with self.lock:
            self.control.check_watchdog()
            if op in DRIVING_OPS:
                self.control.require()
            tracked = (op in DRIVING_OPS or op in CONTROLLER_OPS) and self.controller is not None
            if tracked:
                self.control.begin(driving=op in DRIVING_OPS)
            try:
                return getattr(self, "_op_" + op.replace(".", "_"))(request)
            except OSError as exc:
                raise ProtocolError("io", f"{op}: {exc.strerror or exc}") from exc
            finally:
                if tracked:
                    self.control.end()

    # --- control -------------------------------------------------------------------------
    def _op_hello(self, r):
        self._client = r["client"]
        return {"manifest": self.manifest()}

    def _op_ping(self, r):
        return {}

    def _op_control_take(self, r):
        return self.control.take(self._client, r.get("token"), r.get("force", False),
                                 r.get("watchdog_s"))

    def _op_control_release(self, r):
        return self.control.release(r["token"])

    # --- pins ----------------------------------------------------------------------------
    def _pin(self, bcm: int) -> HwPin:
        if bcm in self.denied:
            raise ProtocolError("unsupported", f"GPIO{bcm} is denied by the rig configuration")
        if bcm in self.reserved:
            raise ProtocolError("pin_mode", f"GPIO{bcm} is reserved for {self.reserved[bcm]} "
                                            "on this bridge")
        return self.pins[bcm]

    def _snapshot(self, pin: HwPin) -> dict:
        reserved = self.reserved.get(pin.bcm)
        level = None if reserved else self.driver.read(pin.bcm)
        pulled = pin.mode in ("input_pullup", "input_pulldown", "output", "pwm")
        data = {"pin": pin.bcm, "mode": pin.mode, "level": level, "floating": False if pulled else None}
        if pin.pwm:
            data["pwm"] = {"freq_hz": pin.pwm[0], "duty": pin.pwm[1]}
        if reserved:
            data["reserved"] = reserved
        return data

    def _op_list_pins(self, r):
        return {"pins": [self._snapshot(self.pins[b]) for b in sorted(self.pins)],
                "controller": self.control.client}

    def _op_pin_mode(self, r):
        pin = self._pin(r["pin"])
        mode = r["mode"]
        if pin.pwm and mode != "pwm":
            self.driver.stop_pwm(pin.bcm, pin.drive or 0)
            pin.pwm = None
        if mode == "output":
            drive = r.get("initial", pin.drive if pin.drive is not None else 0)
        elif mode == "pwm":
            drive = pin.drive if pin.drive is not None else 0
        else:
            drive = None
        self.driver.set_mode(pin.bcm, mode, drive or 0)
        pin.mode, pin.drive = mode, drive
        self._sync_watch(pin)
        level = self._commanded(pin)
        return {"pin": pin.bcm, "mode": pin.mode, "level": level}

    def _op_pin_write(self, r):
        pin = self._require_mode(r["pin"], "output")
        self.driver.write(pin.bcm, r["level"])
        pin.drive = r["level"]
        return {"pin": pin.bcm, "level": self._commanded(pin)}

    def _op_pin_read(self, r):
        pin = self._pin(r["pin"])
        if pin.mode == "disabled":
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is disabled")
        snapshot = self._snapshot(pin)
        return {"pin": pin.bcm, "level": snapshot["level"], "floating": snapshot["floating"]}

    def _op_pin_pulse(self, r):
        pin = self._require_mode(r["pin"], "output")
        if r["us"] > self.limits["pulse_us"]:
            raise ProtocolError("range", f"pulse longer than {self.limits['pulse_us']} us")
        previous = pin.drive if pin.drive is not None else 0
        start, end = self.driver.pulse(pin.bcm, r["level"], r["us"])
        if pin.bcm in self.streams and r["level"] != previous:
            self._emit("pin.change", start, pin=pin.bcm, level=r["level"])
            self._emit("pin.change", end, pin=pin.bcm, level=previous)
        pin.last = previous
        return {"pin": pin.bcm, "edges": [[start, r["level"]], [end, previous]],
                "jitter_us": abs((end - start) - r["us"])}

    def _op_pin_pwm(self, r):
        pin = self._require_mode(r["pin"], "pwm")
        if r["freq_hz"] > self.limits["pwm_hz"]:
            raise ProtocolError("range", f"freq_hz above {self.limits['pwm_hz']}")
        if r["duty"] <= 0 or r["duty"] >= 1:
            pin.drive = 0 if r["duty"] <= 0 else 1
            self.driver.stop_pwm(pin.bcm, pin.drive)
            pin.pwm = None
            self._commanded(pin)
        else:
            self.driver.start_pwm(pin.bcm, r["freq_hz"], r["duty"])
            pin.pwm = (r["freq_hz"], r["duty"])
        return {"pin": pin.bcm, "freq_hz": r["freq_hz"], "duty": r["duty"]}

    def _require_mode(self, bcm: int, mode: str) -> HwPin:
        pin = self._pin(bcm)
        if pin.mode != mode:
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is {pin.mode}, not {mode}")
        return pin

    def _commanded(self, pin: HwPin) -> int:
        """Read back after a command and stream the change, like the Virtual Rig's resolve."""
        level = self.driver.read(pin.bcm)
        if level != pin.last:
            pin.last = level
            if pin.bcm in self.streams:
                self._emit("pin.change", pin=pin.bcm, level=level)
        return level

    # --- stream ---------------------------------------------------------------------------
    def _op_stream_set(self, r):
        # Bus- and flash-owned pins cannot be watched without taking them from their owner: they
        # are skipped and named, so "stream the whole header" still streams every other GPIO.
        skipped = {bcm: self.reserved[bcm] for bcm in r["pins"] if bcm in self.reserved}
        pins = [self._pin(bcm) for bcm in r["pins"] if bcm not in skipped]
        for pin in pins:
            if r["on"]:
                self.streams.add(pin.bcm)
                pin.last = self.driver.read(pin.bcm)
            else:
                self.streams.discard(pin.bcm)
            self._sync_watch(pin)
        reply = {"pins": sorted(self.streams)}
        if skipped:
            reply["skipped"] = [{"pin": bcm, "reserved": owner} for bcm, owner in sorted(skipped.items())]
        return reply

    def _sync_watch(self, pin: HwPin) -> None:
        wanted = pin.bcm in self.streams and pin.mode in P.INPUT_MODES
        self.driver.watch(pin.bcm, self._on_edge if wanted else None)

    def _on_edge(self, bcm: int, level: int, t_us: int) -> None:
        with self.lock:
            pin = self.pins.get(bcm)
            if pin is None or level == pin.last or bcm not in self.streams:
                return
            pin.last = level
            self._emit("pin.change", t_us, pin=bcm, level=level)

    # --- capture ---------------------------------------------------------------------------
    def _op_capture_start(self, r):
        if r["rate_hz"] > self.limits["capture_hz"]:
            raise ProtocolError("range", f"rate_hz above {self.limits['capture_hz']}")
        if any(not c.done for c in self.captures.values()):
            raise ProtocolError("busy", "a capture is already running")
        pins = [self._pin(bcm).bcm for bcm in r["pins"]]
        now = self.now_us()
        capture = HwCapture(self._next_capture, pins, r["rate_hz"], now, now + r["duration_ms"] * 1000,
                            {bcm: self.driver.read(bcm) for bcm in pins})
        self._next_capture += 1
        self.captures[capture.capture_id] = capture
        threading.Thread(target=self._sample, args=(capture,), name=f"capture-{capture.capture_id}",
                         daemon=True).start()
        return {"capture_id": capture.capture_id, "rate_hz_actual": capture.rate_hz}

    def _sample(self, capture: HwCapture) -> None:
        quantum = 1_000_000 / capture.rate_hz
        last = dict(capture.initial)
        index = 0
        try:
            while not capture.done:
                index += 1
                due = capture.start_us + index * quantum
                if due > capture.end_us:
                    break
                self._sleep_until(due)
                levels = {bcm: self.driver.read(bcm) for bcm in capture.pins}
                t = int(round(self.now_us() / quantum) * quantum)
                capture.samples += 1
                for bcm, level in levels.items():
                    if level != last[bcm]:
                        capture.edges.append([t, bcm, level])
                        last[bcm] = level
        except OSError:
            log.exception("capture %s failed", capture.capture_id)
        with self.lock:
            self._finish_capture(capture, cancelled=False)

    def _sleep_until(self, due_us: float) -> None:
        remaining = due_us - self.now_us()
        if remaining > SPIN_US:
            time.sleep((remaining - SPIN_US) / 1e6)
        while self.now_us() < due_us:
            pass

    def _finish_capture(self, capture: HwCapture, cancelled: bool) -> None:
        if capture.done:
            return
        capture.done = True
        elapsed_s = max(self.now_us() - capture.start_us, 1) / 1e6
        rate = capture.rate_hz if cancelled else round(capture.samples / elapsed_s)
        self._emit("capture.done", capture_id=capture.capture_id,
                   initial={str(k): v for k, v in capture.initial.items()},
                   edges=list(capture.edges),  # the sampler may still append to the live list
                   rate_hz_actual=rate, cancelled=cancelled, start_us=capture.start_us,
                   pins=capture.pins)

    def _op_capture_cancel(self, r):
        capture = self.captures.get(r["capture_id"])
        if capture is None:
            raise ProtocolError("bad_request", "unknown capture_id")
        self._finish_capture(capture, True)
        return {}

    # --- release ----------------------------------------------------------------------------
    def _op_release_all(self, r):
        self._release_all()
        return {}

    def _disable(self, pin: HwPin) -> None:
        self.driver.watch(pin.bcm, None)
        if pin.pwm:
            self.driver.stop_pwm(pin.bcm, 0)
        self.driver.set_mode(pin.bcm, "disabled")
        pin.mode, pin.drive, pin.pwm = "disabled", None, None

    def _release_all(self) -> None:
        """Every GPIO to high-Z, PWM off, ports closed, captures cancelled, streams and staged
        flash images cleared. A failure on one pin is logged and the rest are still released."""
        self.flash_images = {}  # a later controller must never flash someone else's upload
        for pin in self.pins.values():
            if pin.bcm in self.reserved:
                continue
            try:
                self._disable(pin)
            except OSError:
                log.exception("failed to release GPIO%s", pin.bcm)
                self._emit("rig.warning", pin=pin.bcm, warning="release_failed",
                           detail=f"GPIO{pin.bcm} could not be set to high impedance")
        self._close_ports()
        for capture in self.captures.values():
            self._finish_capture(capture, True)
        self.streams.clear()
        self._rx.notify_all()
