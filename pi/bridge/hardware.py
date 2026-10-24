"""Real-GPIO engine for the Pi bridge.

``HardwareRig`` reuses the VirtualRig control/watchdog/capture/stream machinery
(realtime clock) and replaces the electrical model with a ``Driver``: levels come
from the header, edges from GPIO alerts, buses from the kernel devices.

``LgpioDriver`` works on Pi 4 and Pi 5 (lgpio, pyserial, spidev, smbus2 imported
lazily). ``FakeDriver`` is the hardware-free stand-in used by the tests.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from aperture import __version__
from aperture import pins as P
from aperture.protocol import ProtocolError
from aperture.rig.virtual import PinState, UartPort, VirtualRig

EdgeCallback = Callable[[int, int], None]  # (bcm, level)


class Driver:
    """Hardware access used by HardwareRig. Every method is synchronous."""

    board = "Raspberry Pi"

    def set_input(self, bcm: int, pull: str | None, on_edge: EdgeCallback) -> int: ...
    def set_output(self, bcm: int, level: int) -> None: ...
    def write(self, bcm: int, level: int) -> None: ...
    def read(self, bcm: int) -> int: ...
    def pwm(self, bcm: int, freq_hz: float, duty: float) -> None: ...
    def release(self, bcm: int) -> None: ...
    def uart_open(self, tx: int, rx: int, baud: int, on_rx: Callable[[bytes], None]) -> None: ...
    def uart_write(self, data: bytes) -> None: ...
    def uart_close(self) -> None: ...
    def spi_xfer(self, bus: int, cs: int, data: bytes, hz: int | None, mode: int | None) -> bytes: ...
    def i2c_write(self, bus: int, addr: int, data: bytes) -> None: ...
    def i2c_read(self, bus: int, addr: int, n: int, reg: int | None) -> bytes: ...
    def i2c_scan(self, bus: int) -> list[int]: ...
    def close(self) -> None: ...


class LgpioDriver(Driver):  # pragma: no cover - needs a Raspberry Pi
    def __init__(self, chip: int | None = None, serial_device: str = "/dev/serial0"):
        import lgpio
        self.lg = lgpio
        self.handle = self._open(chip)
        self.serial_device = serial_device
        self._callbacks: dict[int, object] = {}
        self._serial = None
        self._rx_thread: threading.Thread | None = None
        self._spi: dict[tuple[int, int], object] = {}
        try:
            self.board = Path("/proc/device-tree/model").read_text().strip("\x00\n")
        except OSError:
            self.board = "Raspberry Pi"

    def _open(self, chip):
        for candidate in ([chip] if chip is not None else [0, 4]):
            try:
                handle = self.lg.gpiochip_open(candidate)
                if "pinctrl" in self.lg.gpio_get_chip_info(handle)[2] or chip is not None:
                    return handle
                self.lg.gpiochip_close(handle)
            except self.lg.error:
                continue
        return self.lg.gpiochip_open(0)

    def _cancel(self, bcm):
        cb = self._callbacks.pop(bcm, None)
        if cb is not None:
            cb.cancel()

    def set_input(self, bcm, pull, on_edge):
        self.release(bcm)
        flags = {"up": self.lg.SET_PULL_UP, "down": self.lg.SET_PULL_DOWN}.get(pull, self.lg.SET_PULL_NONE)
        self.lg.gpio_claim_alert(self.handle, bcm, self.lg.BOTH_EDGES, flags)
        self._callbacks[bcm] = self.lg.callback(self.handle, bcm, self.lg.BOTH_EDGES,
                                                lambda chip, gpio, level, tick: level in (0, 1) and on_edge(gpio, level))
        return self.lg.gpio_read(self.handle, bcm)

    def set_output(self, bcm, level):
        self.release(bcm)
        self.lg.gpio_claim_output(self.handle, bcm, level)

    def write(self, bcm, level):
        self.lg.gpio_write(self.handle, bcm, level)

    def read(self, bcm):
        return self.lg.gpio_read(self.handle, bcm)

    def pwm(self, bcm, freq_hz, duty):
        self.lg.tx_pwm(self.handle, bcm, freq_hz, duty * 100.0)

    def release(self, bcm):
        self._cancel(bcm)
        try:
            self.lg.tx_pwm(self.handle, bcm, 0, 0)
        except self.lg.error:
            pass
        try:
            self.lg.gpio_free(self.handle, bcm)  # freed lines return to input = high-impedance
        except self.lg.error:
            pass

    def uart_open(self, tx, rx, baud, on_rx):
        import serial
        self._serial = serial.Serial(self.serial_device, baud, timeout=0.05)

        def reader(port=self._serial):
            while port.is_open:
                try:
                    data = port.read(256)
                except (OSError, TypeError, AttributeError):
                    return
                if data:
                    on_rx(data)
        self._rx_thread = threading.Thread(target=reader, name="uart-rx", daemon=True)
        self._rx_thread.start()

    def uart_write(self, data):
        self._serial.write(data)

    def uart_close(self):
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def spi_xfer(self, bus, cs, data, hz, mode):
        import spidev
        dev = self._spi.get((bus, cs))
        if dev is None:
            dev = spidev.SpiDev()
            dev.open(bus, cs)
            self._spi[(bus, cs)] = dev
        dev.max_speed_hz = hz or 1_000_000
        dev.mode = mode or 0
        return bytes(dev.xfer2(list(data)))

    def _bus(self, bus):
        import smbus2
        return smbus2.SMBus(bus)

    def i2c_write(self, bus, addr, data):
        import smbus2
        with self._bus(bus) as b:
            b.i2c_rdwr(smbus2.i2c_msg.write(addr, list(data)))

    def i2c_read(self, bus, addr, n, reg):
        import smbus2
        with self._bus(bus) as b:
            msgs = ([smbus2.i2c_msg.write(addr, [reg])] if reg is not None else []) + [smbus2.i2c_msg.read(addr, n)]
            b.i2c_rdwr(*msgs)
            return bytes(list(msgs[-1]))

    def i2c_scan(self, bus):
        found = []
        with self._bus(bus) as b:
            for addr in range(0x08, 0x78):
                try:
                    b.read_byte(addr)
                    found.append(addr)
                except OSError:
                    pass
        return found

    def close(self):
        for bcm in list(self._callbacks):
            self._cancel(bcm)
        self.uart_close()
        for dev in self._spi.values():
            dev.close()
        self.lg.gpiochip_close(self.handle)


class FakeDriver(Driver):
    """In-memory driver: ``external`` levels stand in for the DUT; records every call."""

    board = "Fake Pi"

    def __init__(self):
        self.calls: list[tuple] = []
        self.levels: dict[int, int] = {}
        self.external: dict[int, int] = {}
        self.edge_callbacks: dict[int, EdgeCallback] = {}
        self.uart_rx: Callable[[bytes], None] | None = None
        self.uart_tx = bytearray()
        self.i2c: dict[int, bytes] = {}

    def set_input(self, bcm, pull, on_edge):
        self.calls.append(("input", bcm, pull))
        self.edge_callbacks[bcm] = on_edge
        level = self.external.get(bcm, {"up": 1, "down": 0}.get(pull, 0))
        self.levels[bcm] = level
        return level

    def set_output(self, bcm, level):
        self.calls.append(("output", bcm, level))
        self.edge_callbacks.pop(bcm, None)
        self.levels[bcm] = level

    def write(self, bcm, level):
        self.calls.append(("write", bcm, level))
        self.levels[bcm] = level

    def read(self, bcm):
        return self.levels.get(bcm, 0)

    def pwm(self, bcm, freq_hz, duty):
        self.calls.append(("pwm", bcm, freq_hz, duty))

    def release(self, bcm):
        self.calls.append(("release", bcm))
        self.edge_callbacks.pop(bcm, None)

    def dut_drive(self, bcm, level):
        """Simulate the DUT changing an input pin."""
        self.external[bcm] = level
        self.levels[bcm] = level
        if bcm in self.edge_callbacks:
            self.edge_callbacks[bcm](bcm, level)

    def uart_open(self, tx, rx, baud, on_rx):
        self.calls.append(("uart_open", tx, rx, baud))
        self.uart_rx = on_rx

    def uart_write(self, data):
        self.uart_tx += data

    def uart_close(self):
        self.calls.append(("uart_close",))
        self.uart_rx = None

    def spi_xfer(self, bus, cs, data, hz, mode):
        return bytes(data)

    def i2c_write(self, bus, addr, data):
        if addr not in self.i2c:
            raise OSError(f"no ack at 0x{addr:02x}")

    def i2c_read(self, bus, addr, n, reg):
        if addr not in self.i2c:
            raise OSError(f"no ack at 0x{addr:02x}")
        start = reg or 0
        return self.i2c[addr][start:start + n]

    def i2c_scan(self, bus):
        return sorted(self.i2c)

    def close(self):
        self.calls.append(("close",))


class HardwareRig(VirtualRig):
    BRIDGE = "aperture-pi"
    PULLS: ClassVar[dict] = {"input": None, "input_pullup": "up", "input_pulldown": "down"}

    def __init__(self, driver: Driver, limits: dict | None = None, denied_pins: list[int] | None = None):
        self.driver = driver
        self._applied: dict[int, tuple] = {}
        super().__init__({"name": "hardware", "behaviors": [], "denied_pins": denied_pins or []},
                         realtime=True, limits={"capture_hz": 5_000, **(limits or {})})

    def manifest(self) -> dict:
        return P.build_manifest(self.BRIDGE, __version__, getattr(self.driver, "board", "Raspberry Pi"),
                                self.limits, {"vbus_ok": True}, denied=set(self.scenario.get("denied_pins", [])))

    # --- electrical model -> driver -------------------------------------------------
    def _on_edge(self, bcm: int, level: int) -> None:
        with self.lock:
            pin = self.pins[bcm]
            if pin.mode in self.PULLS and level != pin.level:
                pin.level, pin.floating = level, False
                self._level_changed(pin, level)

    def _resolve(self, pin: PinState) -> None:
        old = pin.level
        if pin.mode in ("output", "pwm"):
            level = pin.drive if pin.drive is not None else 0
            want = ("out",)
            if self._applied.get(pin.bcm) != want:
                self.driver.set_output(pin.bcm, level)
                self._applied[pin.bcm] = want
            elif pin.pwm is None:
                self.driver.write(pin.bcm, level)
            pin.level, pin.floating = level, False
        elif pin.mode in self.PULLS:
            want = ("in", self.PULLS[pin.mode])
            if self._applied.get(pin.bcm) != want:
                pin.level = self.driver.set_input(pin.bcm, self.PULLS[pin.mode], self._on_edge)
                self._applied[pin.bcm] = want
            else:
                pin.level = self.driver.read(pin.bcm)
            pin.floating = False
        else:
            if self._applied.pop(pin.bcm, None) is not None:
                self.driver.release(pin.bcm)
            pin.floating = True
        if pin.level != old:
            self._level_changed(pin, pin.level)

    def _op_pin_read(self, r):
        pin = self._pin(r["pin"])
        if pin.mode == "disabled":
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is disabled")
        if pin.mode in self.PULLS:
            level = self.driver.read(pin.bcm)
            if level != pin.level:
                pin.level = level
                self._level_changed(pin, level)
        return {"pin": pin.bcm, "level": pin.level, "floating": pin.floating}

    def _op_pin_pwm(self, r):
        pin = self._pin(r["pin"])
        if pin.mode != "pwm":
            raise ProtocolError("pin_mode", f"GPIO{pin.bcm} is {pin.mode}, not pwm")
        if r["freq_hz"] > self.limits["pwm_hz"]:
            raise ProtocolError("range", f"freq_hz above {self.limits['pwm_hz']}")
        self._resolve(pin)
        if 0 < r["duty"] < 1:
            pin.pwm = (r["freq_hz"], r["duty"])
            self.driver.pwm(pin.bcm, r["freq_hz"], r["duty"])
        else:
            pin.pwm = None
            self.driver.pwm(pin.bcm, 0, 0)
            self._set_drive(pin.bcm, 1 if r["duty"] >= 1 else 0)
        return {"pin": pin.bcm, "freq_hz": r["freq_hz"], "duty": r["duty"]}

    def _stop_pwm(self, pin: PinState) -> None:
        if pin.pwm is not None:
            self.driver.pwm(pin.bcm, 0, 0)
        super()._stop_pwm(pin)

    # --- buses ------------------------------------------------------------------------
    def _op_uart_open(self, r):
        allowed = [(u["tx"], u["rx"]) for u in P.UART_PORTS]
        if (r["tx"], r["rx"]) not in allowed:
            raise ProtocolError("unsupported", f"hardware UART pins are {allowed}")
        if self.ports:
            raise ProtocolError("busy", "the hardware UART is already open")
        for bcm in (r["tx"], r["rx"]):
            if self.pins[bcm].mode != "disabled":
                raise ProtocolError("pin_mode", f"GPIO{bcm} is already in use")
        if r["baud"] < 300 or r["baud"] > 4_000_000:
            raise ProtocolError("range", "baud must be 300..4000000")
        port = UartPort(self._next_port, r["tx"], r["rx"], r["baud"])
        self._next_port += 1
        try:
            self.driver.uart_open(port.tx, port.rx, port.baud, lambda data: self._hw_rx(port, data))
        except OSError as exc:
            raise ProtocolError("io", f"cannot open UART: {exc}") from None
        self.ports[port.port] = port
        return {"port": port.port, "tx": port.tx, "rx": port.rx, "baud": port.baud}

    def _hw_rx(self, port: UartPort, data: bytes) -> None:
        with self.lock:
            self._uart_rx(port, data)

    def _op_uart_close(self, r):
        self._port(r["port"])
        self.driver.uart_close()
        del self.ports[r["port"]]
        return {}

    def _op_uart_write(self, r):
        self._port(r["port"])
        self.driver.uart_write(r["data"])
        return {"n": len(r["data"])}

    def _op_spi_xfer(self, r):
        try:
            return {"data": self.driver.spi_xfer(r["bus"], r["cs"], r["data"], r.get("hz"), r.get("mode"))}
        except OSError as exc:
            raise ProtocolError("io", str(exc)) from None

    def _op_i2c_write(self, r):
        try:
            self.driver.i2c_write(r["bus"], r["addr"], r["data"])
        except OSError as exc:
            raise ProtocolError("io", str(exc)) from None
        return {"n": len(r["data"])}

    def _op_i2c_read(self, r):
        try:
            return {"data": self.driver.i2c_read(r["bus"], r["addr"], r["n"], r.get("reg"))}
        except OSError as exc:
            raise ProtocolError("io", str(exc)) from None

    def _op_i2c_scan(self, r):
        try:
            return {"addrs": self.driver.i2c_scan(r["bus"])}
        except OSError as exc:
            raise ProtocolError("io", str(exc)) from None

    def _release_all(self) -> None:
        if self.ports:
            self.driver.uart_close()
        super()._release_all()

    def shutdown(self) -> None:
        with self.lock:
            self._release_all()
            self.controller = None
        self.driver.close()
