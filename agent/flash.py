"""Stream PlatformIO build artifacts to the harness, which flashes over the header UART."""
import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from agent.profiles import checked

PROJECT = Path(__file__).resolve().parent.parent / "firmware" / "esp32"
ENVS = ("esp32dev", "fixed")
CHUNK_BYTES = 36 * 1024  # base64 of 36 KiB is exactly the 48 KiB protocol limit
LAYOUT = (("bootloader.bin", 0x1000), ("partitions.bin", 0x8000), ("firmware.bin", 0x10000))
FLASH_PINS = {"tx": 14, "rx": 15, "en": 25, "boot": 24}


def build(env, project=PROJECT):
    if env not in ENVS:
        raise ValueError(f"env must be one of {ENVS}")
    pio = shutil.which("pio") or shutil.which("platformio")
    if not pio:
        raise ValueError("PlatformIO not found on this machine: pip install platformio")
    proc = subprocess.run([pio, "run", "-d", str(project), "-e", env],
                          capture_output=True, text=True, timeout=1800, check=False)
    if proc.returncode:
        raise ValueError(f"pio run failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-1500:]}")
    return proc.stdout[-2000:]


def boot_app0(project=PROJECT):
    core = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    found = sorted(core.glob("packages/framework-arduinoespressif32*/tools/partitions/boot_app0.bin"))
    return found[-1] if found else None


def images(env, project=PROJECT):
    if env not in ENVS:
        raise ValueError(f"env must be one of {ENVS}")
    build_dir = Path(project) / ".pio" / "build" / env
    result = []
    for name, offset in LAYOUT:
        path = build_dir / name
        if not path.is_file():
            raise ValueError(f"{path} is missing: run `pio run -d {project} -e {env}` "
                             "or tick 'build first'")
        result.append({"offset": offset, "name": name, "data": path.read_bytes()})
    ota = boot_app0(project)
    if ota:
        result.append({"offset": 0xE000, "name": "boot_app0.bin", "data": ota.read_bytes()})
    return sorted(result, key=lambda image: image["offset"])


def simulated_images(env):
    """Placeholder images for the simulated harness when no PlatformIO build exists."""
    if env not in ENVS:
        raise ValueError(f"env must be one of {ENVS}")
    tag = f"APERTURE_BUILD esp32_safety DEMO_BUG={0 if env == 'fixed' else 1}".encode()
    return [{"offset": 0x1000, "name": "bootloader.bin", "data": b"\xe9" + b"\xff" * 63},
            {"offset": 0x10000, "name": "firmware.bin", "data": b"\xe9" + tag + b"\x00" * 31}]


def manifest(images):
    return [{"offset": image["offset"], "size": len(image["data"]),
             "sha256": hashlib.sha256(image["data"]).hexdigest()} for image in images]


def flash(client, images, baud=460800, boot_pin=FLASH_PINS["boot"], en_pin=FLASH_PINS["en"],
          progress=None):
    checked(client, cmd="flash_begin", images=manifest(images))
    for index, image in enumerate(images):
        data = image["data"]
        for position in range(0, len(data), CHUNK_BYTES):
            chunk = base64.b64encode(data[position:position + CHUNK_BYTES]).decode()
            result = checked(client, cmd="flash_chunk", index=index, data=chunk)
            if progress:
                progress({"stage": "upload", "name": image.get("name"), "offset": image["offset"],
                          "received": result["received"], "size": result["size"]})
    return checked(client, cmd="flash_run", baud=baud, boot_pin=boot_pin, en_pin=en_pin)


def required_pins(manual_boot=False):
    pins = [FLASH_PINS["tx"], FLASH_PINS["rx"], FLASH_PINS["en"]]
    return pins if manual_boot else pins + [FLASH_PINS["boot"]]
