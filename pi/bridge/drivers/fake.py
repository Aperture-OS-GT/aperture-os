"""In-memory driver for tests and dry runs: jumpers between pins, pulls, an external
(DUT-side) drive per pin, an echo UART, a register-mapped I2C bus and SPI loopback.

It models only what the hardware executor relies on; electrical behaviour (contention,
floating inputs) is the Virtual Rig's job.
"""
from __future__ import annotations

import errno
import threading
import time

from .base import EdgeCallback, HardwareDriver, RxCallback, UartDevice


class FakeUart(UartDevice):
    def __init__(self, driver: FakeDriver, on_rx: RxCallback):
        self.driver, self.on_rx, self.closed, self.sent = driver, on_rx, False, b""

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError(errno.EBADF, "port closed")
        self.sent += data
        if self.driver.uart_echo:
            self.on_rx(bytes(data), self.driver.now_us())
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeDriver(HardwareDriver):
    board = "Fake Pi (tests)"

    def __init__(self, jumpers: tuple[tuple[int, int], ...] = (), uart_echo: bool = True,
                 i2c_devices: dict[int, dict[int, int]] | None = None, clock=None):
        self.jumpers = tuple(jumpers)
        self.uart_echo = uart_echo
        self.i2c_devices = {addr: dict(regs) for addr, regs in (i2c_devices or {}).items()}
        self._clock = clock or (lambda: time.monotonic_ns() // 1000)
        self.modes: dict[int, str] = {}
        self.drive: dict[int, int] = {}
        self.external: dict[int, int] = {}
        self.pwm: dict[int, tuple[float, float]] = {}
        self.watchers: dict[int, EdgeCallback] = {}
        self.uarts: list[FakeUart] = []
        self.calls: list[tuple] = []
        self.fail_release: set[int] = set()
        self.closed = False
        self._lock = threading.RLock()

    def now_us(self) -> int:
        return self._clock()

    # --- pins ----------------------------------------------------------------------
    def _level(self, pin: int) -> int:
        mode = self.modes.get(pin, "disabled")
        if mode in ("output", "pwm"):
            return self.drive.get(pin, 0)
        for a, b in self.jumpers:
            other = b if pin == a else a if pin == b else None
            if other is not None and self.modes.get(other) in ("output", "pwm"):
                return self.drive.get(other, 0)
        if pin in self.external:
            return self.external[pin]
        return {"input_pullup": 1, "input_pulldown": 0}.get(mode, 0)

    def _changed(self, before: dict[int, int]) -> None:
        for pin, old in before.items():
            new = self._level(pin)
            callback = self.watchers.get(pin)
            if new != old and callback:
                callback(pin, new, self.now_us())

    def _snapshot(self) -> dict[int, int]:
        return {pin: self._level(pin) for pin in self.watchers}

    def set_mode(self, pin: int, mode: str, level: int = 0) -> None:
        with self._lock:
            if mode == "disabled" and pin in self.fail_release:
                raise OSError(errno.EIO, f"cannot release GPIO{pin}")
            before = self._snapshot()
            self.calls.append(("set_mode", pin, mode, level))
            self.modes[pin] = mode
            self.pwm.pop(pin, None)
            if mode in ("output", "pwm"):
                self.drive[pin] = level
            else:
                self.drive.pop(pin, None)
            self._changed(before)

    def read(self, pin: int) -> int:
        with self._lock:
            return self._level(pin)

    def write(self, pin: int, level: int) -> None:
        with self._lock:
            before = self._snapshot()
            self.calls.append(("write", pin, level))
            self.drive[pin] = level
            self._changed(before)

    def start_pwm(self, pin: int, freq_hz: float, duty: float) -> None:
        with self._lock:
            self.calls.append(("start_pwm", pin, freq_hz, duty))
            self.pwm[pin] = (freq_hz, duty)

    def stop_pwm(self, pin: int, level: int) -> None:
        with self._lock:
            self.calls.append(("stop_pwm", pin, level))
            self.pwm.pop(pin, None)
            self.write(pin, level)

    def pulse(self, pin: int, level: int, us: int) -> tuple[int, int]:
        previous = self.drive.get(pin, 0)
        self.write(pin, level)
        start = self.now_us()
        time.sleep(us / 1e6)
        end = self.now_us()
        self.write(pin, previous)
        return start, end

    def watch(self, pin: int, callback: EdgeCallback | None) -> None:
        with self._lock:
            if callback is None:
                self.watchers.pop(pin, None)
            else:
                self.watchers[pin] = callback

    def set_external(self, pin: int, level: int | None) -> None:
        """Test hook: the DUT drives ``pin`` (None lets go)."""
        with self._lock:
            before = self._snapshot()
            if level is None:
                self.external.pop(pin, None)
            else:
                self.external[pin] = level
            self._changed(before)

    # --- buses ---------------------------------------------------------------------
    def open_uart(self, device: str, baud: int, on_rx: RxCallback) -> UartDevice:
        if device == "/dev/missing":
            raise OSError(errno.ENOENT, f"No such file or directory: {device!r}")
        uart = FakeUart(self, on_rx)
        self.uarts.append(uart)
        return uart

    def spi_xfer(self, bus: int, cs: int, data: bytes, hz: int, mode: int) -> bytes:
        self.calls.append(("spi_xfer", bus, cs, hz, mode))
        return bytes(data)

    def _device(self, addr: int) -> dict[int, int]:
        if addr not in self.i2c_devices:
            raise OSError(getattr(errno, "EREMOTEIO", 121), "Remote I/O error")
        return self.i2c_devices[addr]

    def i2c_write(self, bus: int, addr: int, data: bytes) -> None:
        registers = self._device(addr)
        if len(data) > 1:
            for offset, value in enumerate(data[1:]):
                registers[data[0] + offset] = value

    def i2c_read(self, bus: int, addr: int, n: int, reg: int | None) -> bytes:
        registers = self._device(addr)
        start = 0 if reg is None else reg
        return bytes(registers.get(start + i, 0) & 0xFF for i in range(n))

    def i2c_scan(self, bus: int) -> list[int]:
        return sorted(self.i2c_devices)

    def power(self) -> dict:
        return {"vbus_ok": True}

    def close(self) -> None:
        self.closed = True
