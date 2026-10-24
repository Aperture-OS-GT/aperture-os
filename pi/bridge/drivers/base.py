"""Hardware access used by the hardware executor.

Levels are 0/1 and times are bridge microseconds (``now_us``). Every method may raise
``OSError`` for an I/O failure; the executor maps it to ``err:io``. Drivers keep no
protocol state: modes, PWM settings, ports and captures are the executor's.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

EdgeCallback = Callable[[int, int, int], None]  # (pin, level, t_us)
RxCallback = Callable[[bytes, int], None]       # (data, t_us)


class UartDevice(ABC):
    @abstractmethod
    def write(self, data: bytes) -> int: ...

    @abstractmethod
    def close(self) -> None: ...


class HardwareDriver(ABC):
    board: str = "unknown"

    @abstractmethod
    def now_us(self) -> int: ...

    @abstractmethod
    def set_mode(self, pin: int, mode: str, level: int = 0) -> None:
        """Apply a protocol mode. ``output``/``pwm`` start driving ``level`` with no glitch;
        ``disabled`` is a high-impedance input with pulls off."""

    @abstractmethod
    def read(self, pin: int) -> int: ...

    @abstractmethod
    def write(self, pin: int, level: int) -> None: ...

    @abstractmethod
    def start_pwm(self, pin: int, freq_hz: float, duty: float) -> None:
        """Square wave with 0 < duty < 1 on a pin already in ``pwm`` mode."""

    @abstractmethod
    def stop_pwm(self, pin: int, level: int) -> None:
        """Stop the square wave and hold ``level``."""

    @abstractmethod
    def pulse(self, pin: int, level: int, us: int) -> tuple[int, int]:
        """Drive ``level`` for ``us`` then restore the previous level; blocks until done.
        Returns the measured (start, end) times."""

    @abstractmethod
    def watch(self, pin: int, callback: EdgeCallback | None) -> None:
        """Deliver edges on an input pin to ``callback`` (any thread), or stop with None."""

    @abstractmethod
    def open_uart(self, device: str, baud: int, on_rx: RxCallback) -> UartDevice: ...

    @abstractmethod
    def spi_xfer(self, bus: int, cs: int, data: bytes, hz: int, mode: int) -> bytes: ...

    @abstractmethod
    def i2c_write(self, bus: int, addr: int, data: bytes) -> None: ...

    @abstractmethod
    def i2c_read(self, bus: int, addr: int, n: int, reg: int | None) -> bytes: ...

    @abstractmethod
    def i2c_scan(self, bus: int) -> list[int]: ...

    def power(self) -> dict:
        return {}

    def close(self) -> None:
        pass
