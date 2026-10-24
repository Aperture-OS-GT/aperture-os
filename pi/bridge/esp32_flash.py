"""Program an ESP32 over the Pi header: esptool on UART0, EN and BOOT (GPIO0) driven from GPIO.

Wiring (README): Pi GPIO14 TXD → ESP32 RX0, ESP32 TX0 → Pi GPIO15 RXD, GPIO25 → EN,
GPIO24 → BOOT/GPIO0. BOOT low through an EN pulse enters the ROM bootloader; BOOT high
through a second pulse boots the new image. Both pins are released afterwards so the
DevKit's own pull-ups keep it running. Uses the esptool 5 command spelling.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

LAYOUT = (("bootloader.bin", 0x1000), ("partitions.bin", 0x8000), ("boot_app0.bin", 0xE000),
          ("firmware.bin", 0x10000))
BOOT_PIN, EN_PIN = 24, 25
DEFAULT_BAUD = 115200
RESET_LOW_S, BOOT_SETTLE_S, STRAP_SETTLE_S = 0.1, 0.3, 0.05
ESPTOOL_TIMEOUT_S = 300
MANUAL_BOOT_WINDOW_S = 30.0   # how long the tester has to hold BOOT once flashing starts
APP_BOOT_WINDOW_S = 30.0      # how long to wait for BOOT to be released after writing
DOWNLOAD_LISTEN_S, APP_LISTEN_S = 0.9, 2.0
DOWNLOAD_MARKERS = (b"DOWNLOAD_BOOT", b"waiting for download")
APP_MARKER = b"SPI_FAST_FLASH_BOOT"
BOOT_LOG_CHARS = 1500
log = logging.getLogger(__name__)


def images_in(directory: Path) -> list[tuple[int, Path]]:
    found = [(offset, directory / name) for name, offset in LAYOUT if (directory / name).is_file()]
    if not any(path.name == "firmware.bin" for _, path in found):
        raise FileNotFoundError(f"no firmware.bin in {directory}")
    return found


def _pulse_reset(driver, en_pin: int) -> None:
    driver.write(en_pin, 0)
    time.sleep(RESET_LOW_S)
    driver.write(en_pin, 1)


def _reset_and_listen(driver, en_pin: int, serial_port, seconds: float) -> bytes:
    """Pulse EN and return what the chip prints in the next ``seconds`` (ROM banner, then app)."""
    serial_port.reset_input_buffer()
    _pulse_reset(driver, en_pin)
    data, end = b"", time.monotonic() + seconds
    while time.monotonic() < end:
        data += serial_port.read(512)
    return data


def wait_for_download_mode(driver, en_pin: int, open_serial, window_s: float = MANUAL_BOOT_WINDOW_S) -> int:
    """Manual boot without a BOOT wire: reset once a second until the ROM reports download mode,
    i.e. until the tester is holding BOOT. Returns the number of resets it took."""
    with open_serial() as serial_port:
        deadline, attempts = time.monotonic() + window_s, 0
        while time.monotonic() < deadline:
            attempts += 1
            boot_log = _reset_and_listen(driver, en_pin, serial_port, DOWNLOAD_LISTEN_S)
            if any(marker in boot_log for marker in DOWNLOAD_MARKERS):
                return attempts
    raise RuntimeError(f"the ESP32 did not enter download mode within {window_s:.0f} s: hold its BOOT "
                       "button while the Pi resets it, then try again")


def boot_application(driver, en_pin: int, open_serial, window_s: float = APP_BOOT_WINDOW_S) -> bytes | None:
    """After a manual-boot write: reset until the chip boots from flash (BOOT released). Returns
    what it printed in the first seconds of that boot, the new app's banner included, else None."""
    with open_serial() as serial_port:
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline:
            boot_log = _reset_and_listen(driver, en_pin, serial_port, APP_LISTEN_S)
            if APP_MARKER in boot_log:
                return boot_log
    return None


def esptool_command(port: str, baud: int, images: list[tuple[int, Path]]) -> list[str]:
    command = [sys.executable, "-m", "esptool", "--chip", "esp32", "--port", port, "--baud",
               str(baud), "--before", "no-reset", "--after", "no-reset", "write-flash", "-z"]
    for offset, path in images:
        command += [hex(offset), str(path)]
    return command


def _release(driver, pins) -> None:
    for pin in pins:
        try:
            driver.set_mode(pin, "disabled")
        except OSError:
            log.exception("could not release GPIO%s after flashing", pin)


def flash(driver, images: list[tuple[int, Path]], port: str, baud: int = DEFAULT_BAUD,
          boot_pin: int | None = BOOT_PIN, en_pin: int = EN_PIN, run=subprocess.run,
          timeout_s: float = ESPTOOL_TIMEOUT_S, open_serial=None) -> dict:
    """Write ``images`` and boot them; raises RuntimeError with esptool's output on failure
    and ``subprocess.TimeoutExpired`` if esptool runs past ``timeout_s``.

    ``boot_pin=None`` is manual boot: the tester holds the ESP32's BOOT button during the reset.
    With ``open_serial`` (a callable returning a context-managed serial port) manual boot listens
    to the ROM: it resets until the chip is in download mode and, after writing, until it boots
    from flash, so the tester does not have to time the button press.
    """
    patient = boot_pin is None and open_serial is not None
    resets, boot_log = 1, None
    start = time.monotonic()
    pins = [en_pin] if boot_pin is None else [boot_pin, en_pin]
    try:
        for pin in pins:
            driver.set_mode(pin, "output", 1)
        if boot_pin is not None:
            driver.write(boot_pin, 0)
            time.sleep(STRAP_SETTLE_S)
        if patient:
            resets = wait_for_download_mode(driver, en_pin, open_serial, MANUAL_BOOT_WINDOW_S)
        else:
            _pulse_reset(driver, en_pin)
            time.sleep(BOOT_SETTLE_S)
        proc = run(esptool_command(port, baud, images), capture_output=True, text=True,
                   timeout=timeout_s, check=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode:
            raise RuntimeError(f"esptool failed ({proc.returncode}): {output[-1500:]}")
        if boot_pin is not None:
            driver.write(boot_pin, 1)
            time.sleep(STRAP_SETTLE_S)
        if patient:
            boot_log = boot_application(driver, en_pin, open_serial, APP_BOOT_WINDOW_S)
        else:
            _pulse_reset(driver, en_pin)
    finally:
        _release(driver, pins)
    result = {"flashed": True, "seconds": round(time.monotonic() - start, 1), "baud": baud,
              "images": [{"offset": offset, "name": path.name, "size": path.stat().st_size}
                         for offset, path in images], "log": output[-2000:]}
    if patient:
        result.update(boot_resets=resets, app_booted=boot_log is not None,
                      boot_log=(boot_log or b"").decode("ascii", "replace")[-BOOT_LOG_CHARS:])
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Flash an ESP32 from the Pi header")
    parser.add_argument("directory", type=Path, help="folder with bootloader/partitions/firmware.bin")
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--chip", type=int, default=0, help="gpiochip of the 40-pin header")
    parser.add_argument("--manual-boot", action="store_true",
                        help="no BOOT wire: hold the ESP32 BOOT button during the reset")
    args = parser.parse_args(argv)
    from .drivers.lgpio_driver import LgpioDriver

    driver = LgpioDriver(args.chip)
    try:
        result = flash(driver, images_in(args.directory), args.port, args.baud,
                       boot_pin=None if args.manual_boot else BOOT_PIN)
    finally:
        driver.close()
    print(f"flashed {len(result['images'])} images in {result['seconds']} s at {result['baud']} baud")


if __name__ == "__main__":
    main()
