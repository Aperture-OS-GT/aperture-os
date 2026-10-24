"""Build ESP32 firmware with PlatformIO and collect the images ``program_esp32`` stages.

Projects live in ``firmware/<project>`` (repo root = parent of the ``aperture`` package) and
build into ``.pio/build/<env>``. ``images()`` returns ``(offset, bytes)`` in the layout
pi/bridge/esp32_flash.py writes; ``boot_app0.bin`` comes from the Arduino-ESP32 framework
package. ``placeholder_images()`` lets the Virtual Rig rehearse a flash without a build.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = REPO_ROOT / "firmware"
DEFAULT_PROJECT, DEFAULT_ENV = "esp32_dut", "esp32dev"
APP_IMAGE, BOOT_APP0 = "firmware.bin", "boot_app0.bin"
LAYOUT = (("bootloader.bin", 0x1000), ("partitions.bin", 0x8000), (BOOT_APP0, 0xE000), (APP_IMAGE, 0x10000))
FRAMEWORK_BOOT_APP0 = f"framework-arduinoespressif32*/tools/partitions/{BOOT_APP0}"
BUILD_TIMEOUT_S = 600
OUTPUT_TAIL_CHARS = 1500
IMAGE_MAGIC = b"\xe9"  # first byte of every ESP32 app/bootloader image
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class FirmwareError(Exception):
    pass


def platformio_home() -> Path:
    return Path(os.environ.get("PLATFORMIO_CORE_DIR") or Path.home() / ".platformio")


def find_pio() -> str | None:
    """The PlatformIO CLI: ``pio``/``platformio`` on PATH, else the installer's penv."""
    for name in ("pio", "platformio"):
        found = shutil.which(name)
        if found:
            return found
    fallback = platformio_home() / "penv" / "bin" / "pio"
    return str(fallback) if fallback.is_file() else None


def _name(value: str, what: str) -> str:
    """Project and env names become paths and CLI arguments, so only plain names pass."""
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise FirmwareError(f"invalid {what} {value!r}")
    return value


def project_dir(project: str = DEFAULT_PROJECT) -> Path:
    return FIRMWARE_DIR / _name(project, "firmware project")


def build_dir(project: str = DEFAULT_PROJECT, env: str = DEFAULT_ENV) -> Path:
    return project_dir(project) / ".pio" / "build" / _name(env, "PlatformIO env")


def has_build(project: str = DEFAULT_PROJECT, env: str = DEFAULT_ENV) -> bool:
    return (build_dir(project, env) / APP_IMAGE).is_file()


def _tail(*parts) -> str:
    text = "".join(p.decode("utf-8", "replace") if isinstance(p, bytes) else p or "" for p in parts)
    return text[-OUTPUT_TAIL_CHARS:]


def build(project: str = DEFAULT_PROJECT, env: str = DEFAULT_ENV, timeout_s: float = BUILD_TIMEOUT_S,
          run=subprocess.run) -> Path:
    """``pio run`` one env of a project and return its build folder; FirmwareError carries the output tail."""
    out = build_dir(project, env)
    directory = project_dir(project)
    if not (directory / "platformio.ini").is_file():
        raise FirmwareError(f"no PlatformIO project at {directory}")
    pio = find_pio()
    if pio is None:
        raise FirmwareError("PlatformIO not found: install it (pio on PATH or ~/.platformio/penv)")
    try:
        proc = run([pio, "run", "-d", str(directory), "-e", env], capture_output=True, text=True,
                   timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        tail = _tail(exc.stdout, exc.stderr)
        raise FirmwareError(f"PlatformIO build timed out after {timeout_s} s: {tail}") from None
    except OSError as exc:
        raise FirmwareError(f"cannot run PlatformIO: {exc}") from None
    if proc.returncode:
        raise FirmwareError(f"PlatformIO build failed ({proc.returncode}): {_tail(proc.stdout, proc.stderr)}")
    return out


def boot_app0(directory: Path) -> Path | None:
    """``boot_app0.bin`` from the build folder, else from an installed Arduino-ESP32 framework."""
    candidates = [directory / BOOT_APP0, *sorted((platformio_home() / "packages").glob(FRAMEWORK_BOOT_APP0))]
    return next((path for path in candidates if path.is_file()), None)


def images(project: str = DEFAULT_PROJECT, env: str = DEFAULT_ENV) -> list[tuple[int, bytes]]:
    """``(offset, bytes)`` for each artifact of a build, in flash order; firmware.bin is required."""
    directory = build_dir(project, env)
    if not (directory / APP_IMAGE).is_file():
        raise FirmwareError(f"no {APP_IMAGE} in {directory}; build {project} first")
    found = []
    for name, offset in LAYOUT:
        path = boot_app0(directory) if name == BOOT_APP0 else directory / name
        if path is not None and path.is_file():
            found.append((offset, path.read_bytes()))
    return found


def placeholder_images() -> list[tuple[int, bytes]]:
    """Tiny stand-ins at the real offsets so the Virtual Rig can rehearse a flash with nothing built."""
    return [(offset, IMAGE_MAGIC + f"aperture placeholder {name}".encode()) for name, offset in LAYOUT]
