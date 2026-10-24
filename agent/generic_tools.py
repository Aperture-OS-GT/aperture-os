"""Bounded agent tools: only a human endpoint may confirm wiring."""
from agent.flash import ENVS, FLASH_PINS, flash, images, required_pins
from agent.profiles import checked, run_profile


class GenericTools:
    def __init__(self, client, wiring, display=None):
        self.client, self.wiring, self.display = client, wiring, display
        self.observations = []
        descriptions = [
            ("select_profile", "Select a DUT profile and request human wiring confirmation",
             {"profile": {"type": "string", "enum": ["esp32_safety", "mpu6050_gyro", "display"]}}),
            ("request_connection", "Ask human to connect a Pi BCM pin to a DUT pin",
             {"pi_pin": {"type": "integer"}, "dut_pin": {"type": "string"},
              "component": {"type": "string"}, "reason": {"type": "string"}}),
            ("read_pins", "Read confirmed GPIO pins", {"pins": {"type": "array", "items": {"type": "integer"}}}),
            ("write_pin", "Write a confirmed tx pin", {"pin": {"type": "integer"},
                                                     "val": {"type": "integer", "enum": [0, 1]}}),
            ("sample_pins", "Sample confirmed pins, bounded by daemon limits",
             {"pins": {"type": "array", "items": {"type": "integer"}},
              "duration_ms": {"type": "integer"}, "rate_hz": {"type": "number"}}),
            ("run_profile", "Run selected profile only when all required wires are confirmed", {}),
            ("configure_pin", "Configure a human-confirmed connection",
             {"pin": {"type": "integer"}, "direction": {"type": "string", "enum": ["tx", "rx"]}}),
            ("flash_firmware", ("Flash a prebuilt ESP32 image over the Pi header UART (TX0/RX0, "
                                "EN, BOOT must be confirmed); esp32dev has the planted bug, "
                                "fixed does not"),
             {"env": {"type": "string", "enum": list(ENVS)}}),
        ]
        self.schemas = [{"type": "function", "function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": props,
                                        "required": list(props), "additionalProperties": False}}}
                        for name, desc, props in descriptions]
        self.functions = {name: getattr(self, name) for name, _, _ in descriptions}

    def context(self):
        return {"selected": self.wiring.status() if self.wiring.profile else None,
                "requests": self.wiring.requests()}

    def select_profile(self, profile):
        checked(self.client, cmd="select_profile", profile=profile)
        status = self.wiring.select(profile)
        self.wiring.emit("PROFILE", status)
        return self.record({"pass": False, "status": "waiting_for_connections", **status})

    def record(self, result):
        result = {"latency_ms": 0, **result,
                  "issues": result.get("issues", [] if result.get("pass") else
                                                [result.get("error", "wiring required")])}
        self.observations.append(result)
        return result

    def request_connection(self, pi_pin, dut_pin, component, reason):
        self.wiring.header.gpio(pi_pin)
        checked(self.client, cmd="release", pin=pi_pin)
        self.wiring.request_connection(pi_pin, dut_pin, component, reason)
        return self.record({"pass": False, "status": "waiting_for_connections", "latency_ms": 0})

    def prepare(self, pins):
        if self.wiring.profile and self.wiring.missing():
            raise ValueError(f"confirm all required profile wires first: {self.wiring.missing()}")
        for pin in pins:
            state = self.wiring.header.gpio(pin)
            if not state["connected"] or state["direction"] not in ("tx", "rx"):
                raise ValueError(f"GPIO{pin} requires confirmed wiring and direction")
            # Do not reconfigure existing outputs: it would reset their levels.
            configured = checked(self.client, cmd="list_pins")["pins"]
            actual = next(item for item in configured if item["bcm"] == pin)
            if actual["direction"] != state["direction"]:
                checked(self.client, cmd="configure_pin", pin=pin, direction=state["direction"])

    def configure_pin(self, pin, direction):
        state = self.wiring.header.gpio(pin)
        if not state["connected"]:
            raise ValueError("confirm connection before configuring GPIO")
        if self.wiring.profile:
            for wire in self.wiring.profile["wires"]:
                if wire["pi_pin"] == pin and wire["direction"] != direction:
                    raise ValueError("direction conflicts with selected profile")
        result = checked(self.client, cmd="configure_pin", pin=pin, direction=direction)
        state["direction"] = direction
        self.wiring.emit_header()
        return self.record(result)

    def read_pins(self, pins):
        self.prepare(pins)
        return self.record(checked(self.client, cmd="read", pins=pins))

    def write_pin(self, pin, val):
        self.prepare([pin])
        return self.record(checked(self.client, cmd="write", pin=pin, val=val))

    def sample_pins(self, pins, duration_ms, rate_hz):
        self.prepare(pins)
        return self.record(checked(self.client, cmd="sample", pins=pins,
                                   duration_ms=duration_ms, rate_hz=rate_hz))

    def run_profile(self):
        return self.record(run_profile(self.client, self.wiring, self.display))

    def flash_firmware(self, env):
        if not self.wiring.profile or self.wiring.profile["id"] != "esp32_safety":
            raise ValueError("select the esp32_safety profile before flashing")
        manual = not self.wiring.header.gpio(FLASH_PINS["boot"])["connected"]
        missing = [pin for pin in required_pins(manual)
                   if not self.wiring.header.gpio(pin)["connected"]]
        if missing:
            return self.record({"pass": False, "status": "waiting_for_connections",
                                "missing": missing, "env": env})
        result = flash(self.client, images(env), boot_pin=None if manual else FLASH_PINS["boot"])
        return self.record({**result, "env": env, "status": "complete"})

    def cleanup(self):
        # Outputs stay under the selected profile's explicit control; no legacy GPIO writes.
        pass
