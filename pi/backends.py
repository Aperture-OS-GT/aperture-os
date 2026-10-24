"""Pin numbers are BCM on the Pi; telemetry uses DUT signal names."""
import json
import time


class SimBackend:
    def __init__(self, demo_bug=True):
        self.demo_bug = demo_bug
        self.inputs = {"trig": 0, "pwm": 0.0, "fault": 0}
        self.safety = 1

    def apply(self, **values):
        self.inputs.update(values)

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
        pass


class RealBackend:
    def __init__(self, serial_port="/dev/ttyUSB0"):
        import serial
        from gpiozero import DigitalInputDevice, OutputDevice, PWMOutputDevice

        self.outputs = {"trig": OutputDevice(17), "pwm": PWMOutputDevice(18, frequency=100),
                        "fault": OutputDevice(27)}
        self.inputs = {"22": DigitalInputDevice(22, pull_up=False),
                       "23": DigitalInputDevice(23, pull_up=False)}
        self.serial = serial.Serial(serial_port, 115200, timeout=0.25)

    def apply(self, **values):
        for key, value in values.items():
            self.outputs[key].value = value

    def read(self):
        # Discard queued packets; only a newly emitted packet may verify this stimulus.
        self.serial.reset_input_buffer()
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                telemetry = json.loads(self.serial.readline())
                if not isinstance(telemetry, dict) or not {
                    "state", "actuator", "safety", "trig", "pwm", "fault"
                } <= telemetry.keys():
                    continue
                return {"telemetry": telemetry,
                        "injected": {k: d.value for k, d in self.outputs.items()},
                        "physical": {k: int(d.value) for k, d in self.inputs.items()}}
            except (ValueError, UnicodeError):
                continue
        raise TimeoutError("no valid fresh DUT telemetry within 600ms")

    def close(self):
        # Deassert trigger/PWM before clearing fault, avoiding accidental actuation.
        for key in ("trig", "pwm", "fault"):
            self.outputs[key].off()
        for device in (*self.outputs.values(), *self.inputs.values()):
            device.close()
        self.serial.close()
