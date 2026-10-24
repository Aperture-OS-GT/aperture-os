"""Flash a DUT over the header UART with esptool; EN and BOOT are driven from Pi GPIO."""
import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_CHUNK_CHARS = 49152
BAUDS = (115200, 230400, 460800, 921600)
BUILD_TAG = re.compile(rb"APERTURE_BUILD [ -~]{1,80}")


class Flasher:
    def __init__(self, is_real, serial_port=None):
        self.is_real = is_real
        self.serial_port = serial_port
        self.stage = None
        self.staged = None

    def begin(self, images):
        self.abort()
        self.stage = Path(tempfile.mkdtemp(prefix="aperture-flash-"))
        self.staged = []
        for index, image in enumerate(images):
            path = self.stage / f"image{index}.bin"
            path.write_bytes(b"")
            self.staged.append({**image, "path": path, "received": 0, "hash": hashlib.sha256()})
        return {"staged": len(self.staged)}

    def chunk(self, index, data):
        if self.staged is None or not 0 <= index < len(self.staged):
            raise ValueError("call flash_begin first and use a valid image index")
        image = self.staged[index]
        raw = base64.b64decode(data, validate=True)
        if image["received"] + len(raw) > image["size"]:
            raise ValueError("chunk exceeds the declared image size")
        with image["path"].open("ab") as handle:
            handle.write(raw)
        image["received"] += len(raw)
        image["hash"].update(raw)
        return {"index": index, "received": image["received"], "size": image["size"]}

    def ready(self):
        if not self.staged:
            raise ValueError("no staged images: call flash_begin and flash_chunk first")
        for image in self.staged:
            if image["received"] != image["size"] or image["hash"].hexdigest() != image["sha256"]:
                raise ValueError(f"image at {image['offset']:#x} is incomplete or corrupt")
        return self.staged

    def abort(self):
        if self.stage:
            shutil.rmtree(self.stage, ignore_errors=True)
        self.stage = self.staged = None

    def run(self, baud, boot_pin, en_pin):
        try:
            images = self.ready()
        except ValueError:
            self.abort()  # a failed run always requires a fresh flash_begin
            raise
        start = time.monotonic()
        try:
            tag = None
            for image in images:
                match = BUILD_TAG.search(image["path"].read_bytes())
                if match:
                    tag = match.group(0).decode()
            log = self.esptool(images, baud, boot_pin, en_pin) if self.is_real else (
                f"simulated write_flash of {len(images)} images at {baud} baud "
                f"(boot_pin={boot_pin}, en_pin={en_pin})")
        finally:
            manifest = [{"offset": image["offset"], "size": image["size"]} for image in images]
            self.abort()
        return {"flashed": True, "seconds": round(time.monotonic() - start, 3), "baud": baud,
                "images": manifest, "build_tag": tag, "manual_boot": boot_pin is None,
                "log": log[-4000:]}

    def esptool(self, images, baud, boot_pin, en_pin):
        from gpiozero import OutputDevice

        # Idle HIGH: pulling EN low holds the ESP32 in reset, GPIO0 low selects the bootloader.
        en = OutputDevice(en_pin, initial_value=True)
        boot = OutputDevice(boot_pin, initial_value=True) if boot_pin is not None else None
        try:
            if boot:
                boot.off()
                time.sleep(0.05)
            en.off()
            time.sleep(0.1)
            en.on()
            time.sleep(0.3)
            args = [sys.executable, "-m", "esptool", "--chip", "esp32", "--port", self.serial_port,
                    "--baud", str(baud), "--before", "no_reset", "--after", "no_reset",
                    "write_flash", "-z"]
            for image in images:
                args += [f"{image['offset']:#x}", str(image["path"])]
            try:
                proc = subprocess.run(args, capture_output=True, text=True, timeout=300,
                                      check=False)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("esptool timed out after 300 s") from exc
            log = proc.stdout + proc.stderr
            if proc.returncode:
                raise ValueError(f"esptool failed ({proc.returncode}): {log[-1500:]}")
            if boot:
                boot.on()
                time.sleep(0.05)
            en.off()
            time.sleep(0.1)
            en.on()
            return log
        finally:
            if boot:
                boot.close()
            en.close()
