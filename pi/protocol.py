"""Strict command validation shared by both transports."""
import json
import math

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
    }
    if not isinstance(cmd, str) or cmd not in fields:
        raise ValueError("unknown command")
    if set(command) != fields[cmd] | {"cmd"}:
        raise ValueError("missing or unexpected fields")
    if "pin" in command and type(command["pin"]) is not int:
        raise ValueError("pin must be an integer")
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
