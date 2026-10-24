"""GPIO-only profile execution. No run proceeds on unconfirmed required wires."""
import math

from agent.planner import verify


def checked(client, **command):
    result = client.command(**command)
    if not result.get("pass"):
        raise ValueError(result.get("error", "harness command failed"))
    return result


def run_profile(client, wiring, display=None):
    missing = wiring.missing()
    if missing:
        return {"profile": wiring.profile["id"], "status": "waiting_for_connections",
                "pass": False, "missing": missing, "steps": []}
    profile = wiring.profile
    for wire in profile["wires"]:
        checked(client, cmd="configure_pin", pin=wire["pi_pin"], direction=wire["direction"])
    rows = []
    try:
        for test in profile["tests"]:
            expected = test["expect"]
            issues = []
            latency = 0
            row = {"step": test["name"], "reasoning": f"Verify {expected}", "injected": {}}
            if profile["id"] == "esp32_safety":
                vector = test["vector"]
                for pin, value in ((17, vector["trig"]), (27, vector["fault"])):
                    latency += checked(client, cmd="write", pin=pin, val=value)["latency_ms"]
                latency += checked(client, cmd="pwm", pin=18, duty=vector["pwm"], freq=100)["latency_ms"]
                result = checked(client, cmd="read", pins=[15, 22, 23])
                pins = {p["pin"]: p for p in result["pins"]}
                observation = {"pass": True, "injected": vector, "telemetry": pins[15]["decoded"],
                               "physical": {"22": pins[22]["value"], "23": pins[23]["value"]}}
                row.update(observation)
                issues = verify(observation)
                for key, value in expected.items():
                    if observation["telemetry"].get(key) != value:
                        issues.append(f"expected {key}={value}")
                latency += result["latency_ms"]
            elif "toggle_pin" in expected:
                pin = expected["toggle_pin"]
                result = checked(client, cmd="sample", pins=[pin], duration_ms=200, rate_hz=50)
                values = {sample["pins"][0]["value"] for sample in result["samples"]}
                if values != {0, 1}:
                    issues.append(f"GPIO{pin} did not toggle")
                row["samples"] = result["samples"]
                latency += result["latency_ms"]
            elif profile["id"] == "mpu6050_gyro":
                result = checked(client, cmd="read", pins=[2, 3, 24])
                decoded = result["pins"][0]["decoded"]
                row["decoded"] = decoded
                if "who_am_i" in expected and decoded.get("who_am_i") != expected["who_am_i"]:
                    issues.append("WHO_AM_I mismatch")
                for axis in expected.get("axes", []):
                    value = decoded.get(axis)
                    if (not isinstance(value, (int, float)) or not math.isfinite(value) or
                            not expected["min"] <= value <= expected["max"]):
                        issues.append(f"{axis} outside expected range")
                latency += result["latency_ms"]
            else:
                result = checked(client, cmd="read", pins=expected["data_pins"])
                number = sum(pin["value"] << index for index, pin in enumerate(result["pins"]))
                row["number"] = number
                if number != expected["number"]:
                    issues.append(f"expected display {expected['number']}, measured {number}")
                latency += result["latency_ms"]
            row.update(issues=issues, latency_ms=round(latency, 3), **{"pass": not issues})
            rows.append(row)
            if display:
                display(row)
    finally:
        if profile["id"] == "esp32_safety":
            for command in ({"cmd": "write", "pin": 17, "val": 0},
                            {"cmd": "pwm", "pin": 18, "duty": 0, "freq": 100},
                            {"cmd": "write", "pin": 27, "val": 0}):
                checked(client, **command)
    return {"profile": profile["id"], "mode": "gpio", "status": "complete",
            "pass": not any(row["issues"] for row in rows), "steps": rows}
