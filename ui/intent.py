import re


def interpret(prompt):
    text = prompt.lower().strip()
    if not text:
        raise ValueError("Enter a hardware instruction.")
    match = re.fullmatch(r"connect\s+gpio(\d+)\s+to\s+(\S+)\s+(.+)", text, re.IGNORECASE)
    if match:
        return {"action": "request_connection", "pi_pin": int(match[1]),
                "component": match[2].upper(), "dut_pin": match[3].upper(),
                "reason": "Requested by operator"}, "Waiting for human wiring confirmation."
    match = re.fullmatch(r"read\s+(?:pin|gpio)\s*(\d+)", text)
    if match:
        return {"cmd": "read", "pins": [int(match[1])]}, "Reading confirmed GPIO input."
    if "flash" in text or "upload firmware" in text or "program the esp32" in text:
        env = "fixed" if re.search(r"\bfix(ed)?\b", text) else "esp32dev"
        return ({"action": "flash", "env": env},
                f"Flashing the {'fixed' if env == 'fixed' else 'DEMO_BUG'} build over the Pi header UART.")
    for word, profile in (("gyroscope", "mpu6050_gyro"), ("gyro", "mpu6050_gyro"),
                          ("display", "display"), ("esp32", "esp32_safety")):
        if "test" in text and word in text:
            return {"action": "test_profile", "profile": profile}, "Preparing profile and required wires."
    if "plan" in text or "test" in text:
        return None, "Running the full safety plan; verifying Serial against physical readback."
    if "reset" in text or "idle" in text:
        return {"cmd": "run_vector", "trig": 0, "pwm": 0, "fault": 0}, "Reset to idle."
    if "pwm" in text:
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
        if not match or not 0 <= float(match[1]) <= 100:
            raise ValueError("Specify PWM as 0–100%, for example: Set PWM 90%.")
        return {"cmd": "set_pwm", "pin": 18, "duty": float(match[1]) / 100}, f"Set PWM {match[1]}%."
    if "fault" in text:
        value = 0 if any(word in text for word in ("clear", "off", "low")) else 1
        return {"cmd": "inject_fault", "val": value}, "Cleared fault." if not value else "Injected fault."
    if "trigger" in text:
        value = 0 if any(word in text for word in ("off", "low", "clear", "0")) else 1
        return {"cmd": "set_pin", "pin": 17, "val": value}, f"Set trigger {'HIGH' if value else 'LOW'}."
    if "read" in text or "state" in text:
        return {"cmd": "read_state"}, "Read fresh telemetry and physical pins."
    raise ValueError("Try a safety plan, trigger high/low, PWM N%, inject/clear fault, read or reset.")
