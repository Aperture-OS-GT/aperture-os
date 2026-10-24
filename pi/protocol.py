"""Strict command validation shared by both transports."""
import json
import math
import re

from pi.flash import BAUDS, MAX_CHUNK_CHARS, MAX_IMAGE_BYTES
from pi.header import GPIO_PINS

MAX_LINE = 65536


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def parse_command(line):
    if len(line) > MAX_LINE:
        raise ValueError("command too large")
    command = json.loads(line)
    if not isinstance(command, dict):
        raise ValueError("command must be an object")  # noqa: TRY004
    cmd = command.get("cmd")
    fields = {
        "ping": set(), "read_state": set(),
        "set_pin": {"pin", "val"}, "set_pwm": {"pin", "duty"},
        "inject_fault": {"val"}, "run_vector": {"trig", "pwm", "fault"},
        "assert_output": {"pin", "expected", "timeout_ms"},
        "list_pins": set(), "configure_pin": {"pin", "direction"},
        "write": {"pin", "val"}, "pwm": {"pin", "duty", "freq"},
        "read": {"pins"}, "sample": {"pins", "duration_ms", "rate_hz"},
        "release": {"pin"}, "select_profile": {"profile"},
        "flash_begin": {"images"}, "flash_chunk": {"index", "data"},
        "flash_run": {"baud", "boot_pin", "en_pin"},
    }
    if not isinstance(cmd, str) or cmd not in fields:
        raise ValueError("unknown command")
    optional = {"pull"} if cmd == "configure_pin" else set()
    if set(command) - optional != fields[cmd] | {"cmd"}:
        raise ValueError("missing or unexpected fields")
    if "pin" in command and type(command["pin"]) is not int:
        raise ValueError("pin must be an integer")
    if cmd in ("configure_pin", "write", "pwm", "release") and command["pin"] not in GPIO_PINS:
        raise ValueError("invalid or reserved BCM GPIO")
    if cmd == "configure_pin" and (command["direction"] not in ("tx", "rx") or
                                    command.get("pull", "none") not in ("none", "up", "down")):
        raise ValueError("direction must be tx/rx and pull none/up/down")
    if cmd in ("read", "sample"):
        pins = command["pins"]
        if (not isinstance(pins, list) or not 1 <= len(pins) <= 26 or
                any(type(pin) is not int or pin not in GPIO_PINS for pin in pins) or
                len(set(pins)) != len(pins)):
            raise ValueError("pins must be a nonempty unique list of available BCM GPIOs")
    if cmd == "pwm" and not number(command["freq"], 1, 10000):
        raise ValueError("freq must be 1..10000 Hz")
    if cmd == "sample" and (not number(command["duration_ms"], 1, 2000) or
                            not number(command["rate_hz"], 1, 1000) or
                            command["duration_ms"] * command["rate_hz"] / 1000 * len(command["pins"]) > 200):
        raise ValueError("sample bounds: 1..2000 ms, 1..1000 Hz, maximum 200 pin samples")
    if cmd == "select_profile" and command["profile"] not in (
        "esp32_safety", "mpu6050_gyro", "display"
    ):
        raise ValueError("unknown profile")
    if cmd == "flash_begin":
        images = command["images"]
        if (not isinstance(images, list) or not 1 <= len(images) <= 8 or any(
                not isinstance(image, dict) or set(image) != {"offset", "size", "sha256"} or
                type(image["offset"]) is not int or not 0 <= image["offset"] < MAX_IMAGE_BYTES or
                type(image["size"]) is not int or not 1 <= image["size"] <= MAX_IMAGE_BYTES or
                not isinstance(image["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", image["sha256"])
                for image in images) or sum(image["size"] for image in images) > MAX_IMAGE_BYTES):
            raise ValueError("images must be 1..8 {offset,size,sha256} entries within 4 MiB")
    if cmd == "flash_chunk" and (type(command["index"]) is not int or
                                 not isinstance(command["data"], str) or
                                 not 0 < len(command["data"]) <= MAX_CHUNK_CHARS):
        raise ValueError("flash_chunk needs an integer index and base64 data up to 48 KiB")
    if cmd == "flash_run":
        boot_pin, en_pin = command["boot_pin"], command["en_pin"]
        if type(command["baud"]) is not int or command["baud"] not in BAUDS:
            raise ValueError(f"baud must be one of {BAUDS}")
        if (type(en_pin) is not int or en_pin not in GPIO_PINS or boot_pin == en_pin or
                (boot_pin is not None and (type(boot_pin) is not int or boot_pin not in GPIO_PINS))):
            raise ValueError("en_pin must be a BCM GPIO; boot_pin a different BCM GPIO or null")
    for key in ("val", "trig", "fault", "expected"):
        if key in command and (type(command[key]) is not int or command[key] not in (0, 1)):
            raise ValueError(f"{key} must be 0 or 1")
    if cmd == "set_pin" and command["pin"] not in (17, 27):
        raise ValueError("digital output pin must be 17 or 27")
    if cmd == "set_pwm" and command["pin"] != 18:
        raise ValueError("PWM pin must be 18")
    if cmd == "assert_output":
        if command["pin"] not in (22, 23):
            raise ValueError("readback pin must be 22 or 23")
        if not number(command["timeout_ms"], 0, 5000):
            raise ValueError("timeout_ms must be 0..5000")
    for key in ("duty", "pwm"):
        if key in command and not number(command[key], 0, 1):
            raise ValueError(f"{key} must be 0..1")
    return command
