"""flash.begin / flash.write / flash.run for the hardware executor: program the ESP32 DUT.

Images are staged in memory per flash offset; ``flash.run`` writes them to a temporary
directory and runs ``esp32_flash.flash`` (esptool on the header UART, EN and BOOT on GPIO).
Same contract as the Virtual Rig:

* flashing needs the header UART and the EN/BOOT pins (``BusConfig.flashing``), else every
  flash op is ``unsupported``;
* ``flash.write`` appends to the image at ``offset``; all staged images together hold at
  most 4 MiB (``range``); ``flash.run`` needs the app image at 0x10000 (``bad_request``);
* open UART ports are closed first, because esptool needs the serial device, and reported
  in ``uart_closed``; the caller reopens them;
* every ``flash.run`` consumes the staged images, whatever the outcome; an esptool failure
  is ``io``, an esptool hang is ``timeout``, and the bridge keeps serving either way.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from aperture.protocol import ProtocolError

from . import esp32_flash
from .hardware_buses import UART_BAUD_MAX, UART_BAUD_MIN

APP_OFFSET = 0x10000
MAX_STAGED_BYTES = 4 * 1024 * 1024
TAIL_CHARS = 400          # esptool output kept in a reply (error message or success log)
ESPTOOL_TIMEOUT_S = 220   # inside the host's 240 s flash.run timeout: a hang is reported


def _one_line(text: str, limit: int = TAIL_CHARS) -> str:
    """Keep what failed (the start) and esptool's verdict (the end) of a long message."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = limit // 4
    return f"{text[:head]} ... {text[head - limit:]}"


def _write_images(folder: Path, staged: dict[int, bytearray]) -> list[tuple[int, Path]]:
    images = []
    for offset, data in sorted(staged.items()):
        path = folder / f"image-{offset:#x}.bin"
        path.write_bytes(data)
        images.append((offset, path))
    return images


class FlashOps:
    """Mixin for ``HardwareExecutor`` (uses ``driver``, ``buses``, ``ports``, ``flash_images``,
    ``_rx`` and ``_close_ports``). Handlers run with the executor lock held."""

    def _require_flashing(self) -> None:
        if not self.buses.flashing:
            raise ProtocolError("unsupported", "flashing needs the header UART and EN/BOOT pins, "
                                               "which this bridge does not have")

    def _op_flash_begin(self, r):
        self._require_flashing()
        self.flash_images = {}
        return {}

    def _op_flash_write(self, r):
        self._require_flashing()
        staged = sum(len(image) for image in self.flash_images.values())
        if staged + len(r["data"]) > MAX_STAGED_BYTES:
            raise ProtocolError("range", f"staged images would exceed {MAX_STAGED_BYTES} bytes")
        image = self.flash_images.setdefault(r["offset"], bytearray())
        image += r["data"]
        return {"offset": r["offset"], "size": len(image)}

    def _op_flash_run(self, r):
        self._require_flashing()
        staged, self.flash_images = self.flash_images, {}
        if APP_OFFSET not in staged:
            raise ProtocolError("bad_request", f"no app image staged at {APP_OFFSET:#x}")
        baud = r.get("baud", esp32_flash.DEFAULT_BAUD)
        if not UART_BAUD_MIN <= baud <= UART_BAUD_MAX:
            raise ProtocolError("range", f"baud must be {UART_BAUD_MIN}..{UART_BAUD_MAX}")
        closed = sorted(self.ports)
        self._close_ports()
        self._rx.notify_all()
        with tempfile.TemporaryDirectory(prefix="aperture-flash-") as folder:
            result = self._esptool(_write_images(Path(folder), staged), baud,
                                   r.get("manual_boot", False))
        return {"flashed": True, "seconds": result["seconds"],
                "images": [{"offset": offset, "size": len(data)}
                           for offset, data in sorted(staged.items())],
                "uart_closed": closed, "log": result["log"][-TAIL_CHARS:]}

    def _esptool(self, images: list[tuple[int, Path]], baud: int, manual_boot: bool) -> dict:
        boot, en = self.buses.flash
        try:
            return esp32_flash.flash(self.driver, images, port=self.buses.uart, baud=baud,
                                     boot_pin=None if manual_boot else boot, en_pin=en,
                                     timeout_s=ESPTOOL_TIMEOUT_S)
        except RuntimeError as exc:
            raise ProtocolError("io", _one_line(str(exc))) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProtocolError("timeout", f"esptool did not finish within {exc.timeout:g} s; "
                                           "try a higher baud") from exc
