"""Pin numbers are BCM on the Pi; telemetry uses DUT signal names."""
import json
import time

from pi.gpio import GenericGPIO


class SimBackend(GenericGPIO):
    is_real = False

    def __init__(self, demo_bug=True, profile="esp32_safety"):
        self.init_gpio(profile)
        self.demo_bug = demo_bug
        self.inputs = {"trig": 0, "pwm": 0.0, "fault": 0}
        self.safety = 1

    def select_profile(self, profile):
        super().select_profile(profile)
        self.inputs = {"trig": 0, "pwm": 0.0, "fault": 0}
        self.safety = 1
        self.tick = 0

    def apply(self, **values):
        self.inputs.update(values)

    def on_flashed(self, result):
        tag = result.get("build_tag") or ""
        if "DEMO_BUG=" in tag:
            self.demo_bug = tag.split("DEMO_BUG=", 1)[1][:1] == "1"
        self.inputs = {"trig": 0, "pwm": 0.0, "fault": 0}
        self.safety = 1

    def read(self):
        trig, pwm, fault = (self.inputs[k] for k in ("trig", "pwm", "fault"))
        state = "FAULT_SHUTDOWN" if fault else (
            "THROTTLING" if trig and pwm > 0.8 else "ACTIVE_DRIVE" if trig else "IDLE"
        )
        actuator = int(bool(trig) and not fault and pwm <= 0.8)
        expected_safe = int(not fault)
        if not (self.demo_bug and fault and (trig or pwm > 0.8)):
            self.safety = expected_safe
        return {
            "injected": dict(self.inputs),
            "telemetry": {**self.inputs, "state": state, "actuator": actuator,
                          "safety": expected_safe},
            "physical": {"22": actuator, "23": self.safety},
        }

    def close(self):
        self.close_gpio()


class RealBackend(GenericGPIO):
    is_real = True

    def __init__(self, serial_port="/dev/serial0", profile="esp32_safety"):
        self.serial_port = serial_port
        self.init_gpio(profile)

    def legacy_setup(self):
        for pin, direction in ((17, "tx"), (18, "tx"), (27, "tx"), (22, "rx"), (23, "rx")):
            if pin not in self.gpio_config:
                self.configure(pin, direction, "down" if direction == "rx" else "none")
        self.outputs = {"trig": self.devices[17], "pwm": self.devices[18], "fault": self.devices[27]}
        self.inputs = {"22": self.devices[22], "23": self.devices[23]}

    def apply(self, **values):
        self.legacy_setup()
        for key, value in values.items():
            if key == "pwm":
                self.pwm_gpio(18, value, 100)
                self.outputs[key] = self.devices[18]
                continue
            self.outputs[key].value = value

    def read(self):
        if hasattr(self, "gpio_config"):
            self.legacy_setup()
            telemetry = self.read_uart()
            return {"telemetry": telemetry,
                    "injected": {k: d.value for k, d in self.outputs.items()},
                    "physical": {k: int(d.value) for k, d in self.inputs.items()}}
        return self._read_packet()

    def read_uart(self):
        if self.uart is None:
            import serial

            self.uart = serial.Serial(self.serial_port, 115200, timeout=0.25)
        return self._read_packet(self.uart)["telemetry"]

    def _read_packet(self, uart=None):
        uart = uart if uart is not None else self.serial
        # Discard queued packets; only a newly emitted packet may verify this stimulus.
        uart.reset_input_buffer()
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                telemetry = json.loads(uart.readline())
                if not isinstance(telemetry, dict) or not {
                    "state", "actuator", "safety", "trig", "pwm", "fault"
                } <= telemetry.keys():
                    continue
                return {"telemetry": telemetry}
            except (ValueError, UnicodeError):
                continue
        raise TimeoutError("no valid fresh DUT telemetry within 600ms")

    def close(self):
        self.close_gpio()
