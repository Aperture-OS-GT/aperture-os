"""Real Raspberry Pi header: lgpio for GPIO and software PWM, pyserial for UART, spidev for
SPI, smbus2 for I2C (each imported only when used).

Checked against python3-lgpio on Raspberry Pi OS Bookworm (Pi 4, header on gpiochip 0):
pulls are ``SET_PULL_UP/DOWN/NONE``; ``tx_pwm`` takes duty in percent and rejects
``tx_pwm(gpio, 0, 0)`` ("bad PWM micros"), so a running PWM is stopped with duty 0 at its own
frequency; edge callbacks get ``(chip, gpio, level, timestamp)`` where level 2 is a watchdog
tick and the timestamp origin varies by kernel, so edges are stamped with the bridge clock on
arrival.
"""
from __future__ import annotations

import errno
import subprocess
import threading
import time
from pathlib import Path

from .base import EdgeCallback, HardwareDriver, RxCallback, UartDevice

WATCHDOG_LEVEL = 2
SPIN_US = 2000          # pulses shorter than this are timed with a busy-wait
UART_READ_CHUNK = 256
UART_POLL_S = 0.02
I2C_SCAN_RANGE = range(0x03, 0x78)
THROTTLED_UNDERVOLTAGE = 0x1


def _board_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00\n ") or "Raspberry Pi"
    except OSError:
        return "Raspberry Pi"


class SerialUart(UartDevice):
    def __init__(self, device: str, baud: int, on_rx: RxCallback, clock):
        import serial  # pyserial

        self._serial = serial.Serial(device, baud, timeout=UART_POLL_S)
        self._on_rx, self._clock = on_rx, clock
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name=f"uart-{device}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._serial.read(UART_READ_CHUNK)
            except OSError:
                return
            if data:
                self._on_rx(bytes(data), self._clock())

    def write(self, data: bytes) -> int:
        return self._serial.write(data)

    def close(self) -> None:
        self._stop.set()
        self._reader.join(timeout=1)
        self._serial.close()


class LgpioDriver(HardwareDriver):
    def __init__(self, chip: int = 0, lgpio_module=None):
        if lgpio_module is None:
            import lgpio as lgpio_module
        self.lg = lgpio_module
        self.board = _board_model()
        self._t0 = time.monotonic_ns()
        self._lock = threading.RLock()
        self._callbacks: dict[int, object] = {}
        self._claimed: set[int] = set()
        self._pwm: dict[int, float] = {}  # pin -> frequency of a running software PWM
        self.handle = self._call(self.lg.gpiochip_open, chip)

    def now_us(self) -> int:
        return (time.monotonic_ns() - self._t0) // 1000

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except self.lg.error as exc:
            raise OSError(errno.EIO, f"lgpio: {exc}") from exc

    # --- pins -------------------------------------------------------------------------
    def _release_line(self, pin: int) -> None:
        callback = self._callbacks.pop(pin, None)
        if callback is not None:
            callback.cancel()
        self._stop_tx(pin)
        if pin in self._claimed:
            self._call(self.lg.gpio_free, self.handle, pin)
            self._claimed.discard(pin)

    def _stop_tx(self, pin: int) -> None:
        # This lgpio rejects tx_pwm(freq=0) ("bad PWM micros"); duty 0 at the running frequency stops it.
        freq = self._pwm.pop(pin, None)
        if freq is not None:
            self._call(self.lg.tx_pwm, self.handle, pin, freq, 0)

    def set_mode(self, pin: int, mode: str, level: int = 0) -> None:
        lg = self.lg
        with self._lock:
            self._release_line(pin)
            if mode in ("output", "pwm"):
                self._call(lg.gpio_claim_output, self.handle, pin, level, 0)
            elif mode == "disabled":
                self._call(lg.gpio_claim_input, self.handle, pin, lg.SET_PULL_NONE)
            else:
                flags = {"input": lg.SET_PULL_NONE, "input_pullup": lg.SET_PULL_UP,
                         "input_pulldown": lg.SET_PULL_DOWN}[mode]
                self._call(lg.gpio_claim_alert, self.handle, pin, lg.BOTH_EDGES, flags)
            self._claimed.add(pin)

    def read(self, pin: int) -> int:
        return int(self._call(self.lg.gpio_read, self.handle, pin))

    def write(self, pin: int, level: int) -> None:
        self._call(self.lg.gpio_write, self.handle, pin, level)

    def start_pwm(self, pin: int, freq_hz: float, duty: float) -> None:
        self._call(self.lg.tx_pwm, self.handle, pin, freq_hz, duty * 100)
        self._pwm[pin] = freq_hz

    def stop_pwm(self, pin: int, level: int) -> None:
        self._stop_tx(pin)
        self.write(pin, level)

    def pulse(self, pin: int, level: int, us: int) -> tuple[int, int]:
        previous = self.read(pin)
        self.write(pin, level)
        start = self.now_us()
        due = start + us
        if us > SPIN_US:
            time.sleep((us - SPIN_US) / 1e6)
        while self.now_us() < due:
            pass
        self.write(pin, previous)
        return start, self.now_us()

    def watch(self, pin: int, callback: EdgeCallback | None) -> None:
        with self._lock:
            existing = self._callbacks.pop(pin, None)
            if existing is not None:
                existing.cancel()
            if callback is None:
                return

            def on_edge(_chip, gpio, level, _timestamp):
                if level != WATCHDOG_LEVEL:
                    callback(gpio, level, self.now_us())

            self._callbacks[pin] = self._call(self.lg.callback, self.handle, pin,
                                              self.lg.BOTH_EDGES, on_edge)

    # --- buses ------------------------------------------------------------------------
    def open_uart(self, device: str, baud: int, on_rx: RxCallback) -> UartDevice:
        return SerialUart(device, baud, on_rx, self.now_us)

    def spi_xfer(self, bus: int, cs: int, data: bytes, hz: int, mode: int) -> bytes:
        try:
            import spidev
        except ImportError as exc:
            raise OSError(errno.ENOSYS, "spidev is not installed (pip install spidev)") from exc
        spi = spidev.SpiDev()
        spi.open(bus, cs)
        try:
            spi.max_speed_hz, spi.mode = hz, mode
            return bytes(spi.xfer2(list(data)))
        finally:
            spi.close()

    def _smbus(self, bus: int):
        from smbus2 import SMBus

        return SMBus(bus)

    def i2c_write(self, bus: int, addr: int, data: bytes) -> None:
        from smbus2 import i2c_msg

        with self._smbus(bus) as smbus:
            smbus.i2c_rdwr(i2c_msg.write(addr, list(data)))

    def i2c_read(self, bus: int, addr: int, n: int, reg: int | None) -> bytes:
        from smbus2 import i2c_msg

        read = i2c_msg.read(addr, n)
        with self._smbus(bus) as smbus:
            if reg is None:
                smbus.i2c_rdwr(read)
            else:
                smbus.i2c_rdwr(i2c_msg.write(addr, [reg]), read)
        return bytes(list(read))

    def i2c_scan(self, bus: int) -> list[int]:
        found = []
        with self._smbus(bus) as smbus:
            for addr in I2C_SCAN_RANGE:
                try:
                    smbus.read_byte(addr)
                    found.append(addr)
                except OSError:
                    continue
        return found

    # --- board ------------------------------------------------------------------------
    def power(self) -> dict:
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                                 timeout=2, check=True).stdout
            flags = int(out.strip().split("=")[1], 16)
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return {}
        return {"vbus_ok": not flags & THROTTLED_UNDERVOLTAGE, "throttled": hex(flags)}

    def close(self) -> None:
        with self._lock:
            for pin in list(self._claimed):
                try:
                    self._release_line(pin)
                except OSError:
                    continue
            self._call(self.lg.gpiochip_close, self.handle)
