"""Generic GPIO backend interface; optional bus adapters are opened only on demand."""
import math
import time

from pi.header import GPIO_PINS


class GenericGPIO:
    def init_gpio(self, profile="esp32_safety"):
        self.profile = profile
        self.gpio_config = {}
        self.gpio_values = {pin: 0 for pin in GPIO_PINS}
        self.gpio_duties = {}
        self.devices = {}
        self.bus = None
        self.uart = None
        self.tick = 0
        self.gyro_decoded = None

    def select_profile(self, profile):
        self.close_gpio()
        self.profile = profile
        for name in ("outputs", "inputs"):
            if self.is_real and hasattr(self, name):
                delattr(self, name)

    def configure(self, pin, direction, pull="none"):
        self.release(pin)
        self.gpio_config[pin] = direction
        self.gpio_values[pin] = 0
        if self.is_real and not self.bus_pin(pin):
            from gpiozero import DigitalInputDevice, OutputDevice

            self.devices[pin] = (OutputDevice(pin, initial_value=False) if direction == "tx" else
                                 DigitalInputDevice(pin, pull_up={"up": True, "down": False,
                                                                "none": None}[pull],
                                                    active_state=True if pull == "none" else None))
        return [{"pin": pin, "direction": direction, "value": 0}]

    def bus_pin(self, pin):
        return ((self.profile == "mpu6050_gyro" and pin in (2, 3)) or
                (self.profile == "esp32_safety" and pin in (14, 15)))

    def require(self, pin, direction=None):
        if pin not in self.gpio_config:
            raise ValueError(f"GPIO{pin} is not configured")
        if direction and self.gpio_config[pin] != direction:
            raise ValueError(f"GPIO{pin} must be configured {direction}")

    def write_gpio(self, pin, value):
        self.require(pin, "tx")
        if self.bus_pin(pin):
            raise ValueError("bus pins cannot be bit-banged; use profile reads")
        self.gpio_values[pin] = value
        self.gpio_duties.pop(pin, None)
        if self.is_real:
            self.devices[pin].value = value
        elif self.profile == "esp32_safety" and pin in (17, 18, 27):
            self.apply(**{{17: "trig", 18: "pwm", 27: "fault"}[pin]: value})
        return self.read_pins([pin])

    def pwm_gpio(self, pin, duty, freq):
        self.require(pin, "tx")
        if self.bus_pin(pin):
            raise ValueError("cannot PWM a bus pin")
        if self.is_real:
            from gpiozero import PWMOutputDevice

            self.devices[pin].close()
            self.devices[pin] = PWMOutputDevice(pin, frequency=freq, initial_value=duty)
        self.gpio_duties[pin] = duty
        self.gpio_values[pin] = int(duty > 0)
        if not self.is_real and self.profile == "esp32_safety" and pin == 18:
            self.apply(pwm=duty)
        return self.read_pins([pin])

    def read_gyro(self):
        if not self.is_real:
            self.tick += 1
            return {"who_am_i": 0x68, "wx": round(math.sin(self.tick / 5) * 12, 3),
                    "wy": round(math.cos(self.tick / 5) * 8, 3), "wz": 0.25,
                    "unit": "deg/s"}
        if self.bus is None:
            from smbus2 import SMBus

            self.bus = SMBus(1)
            self.bus.write_byte_data(0x68, 0x6B, 0)  # Wake MPU6050; default ±250 deg/s.
            self.bus.write_byte_data(0x68, 0x1B, 0)
            self.bus.write_byte_data(0x68, 0x38, 1)  # Enable data-ready interrupt.
        data = self.bus.read_i2c_block_data(0x68, 0x43, 6)
        decoded = {"who_am_i": self.bus.read_byte_data(0x68, 0x75), "unit": "deg/s"}
        for name, offset in (("wx", 0), ("wy", 2), ("wz", 4)):
            raw = (data[offset] << 8) | data[offset + 1]
            decoded[name] = (raw - 65536 if raw >= 32768 else raw) / 131
        self.bus.read_byte_data(0x68, 0x3A)  # Clear latched interrupt after status read.
        return decoded

    def read_pins(self, pins):
        for pin in pins:
            self.require(pin)
        decoded = None
        if self.profile == "mpu6050_gyro":
            if any(pin in (2, 3) for pin in pins):
                self.require(2)
                self.require(3)
                decoded = self.read_gyro()
                self.gyro_decoded = decoded
            elif not self.is_real:
                self.tick += 1
        safety = None
        if not self.is_real and self.profile == "esp32_safety":
            safety = self.read()
        if self.profile == "display" and not self.is_real:
            # A DUT presents binary 5 on D0..D3, with a periodic data strobe.
            self.tick += 1
            self.gpio_values.update({5: 1, 6: 0, 13: 1, 19: 0, 26: self.tick % 2})
        values = []
        for pin in pins:
            value = self.gpio_values[pin]
            extra = {}
            if self.is_real and pin in self.devices:
                value = int(self.devices[pin].value > 0)
            if safety:
                value = ({17: safety["injected"]["trig"], 27: safety["injected"]["fault"],
                          22: safety["physical"]["22"], 23: safety["physical"]["23"]}).get(pin, value)
                if pin == 15:
                    extra["decoded"] = safety["telemetry"]
            if self.profile == "esp32_safety" and self.is_real and pin == 15:
                extra["decoded"] = self.read_uart()
            if decoded and pin in (2, 3):
                extra["decoded"] = decoded
            if self.profile == "mpu6050_gyro" and pin == 24 and not self.is_real:
                value = self.tick % 2
            if self.profile == "mpu6050_gyro" and pin == 24 and self.gyro_decoded:
                extra["decoded"] = self.gyro_decoded
            if pin in self.gpio_duties:
                extra["duty"] = self.gpio_duties[pin]
            values.append({"pin": pin, "direction": self.gpio_config[pin], "value": int(value),
                           **extra})
        return values

    def release(self, pin):
        if pin in self.devices:
            self.devices.pop(pin).close()
        self.gpio_config.pop(pin, None)
        self.gpio_duties.pop(pin, None)
        self.gpio_values[pin] = 0
        if self.profile == "mpu6050_gyro" and pin in (2, 3) and self.bus:
            self.bus.close()
            self.bus = None
        if self.profile == "esp32_safety" and pin in (14, 15) and self.uart:
            self.uart.close()
            self.uart = None
        if not self.is_real and self.profile == "esp32_safety" and pin in (17, 18, 27):
            self.apply(**{{17: "trig", 18: "pwm", 27: "fault"}[pin]: 0})

    def close_gpio(self):
        for pin in list(self.gpio_config):
            self.release(pin)
        if self.bus:
            self.bus.close()
            self.bus = None
        if self.uart:
            self.uart.close()
            self.uart = None

    def sample(self, pins, duration_ms, rate_hz):
        count = max(1, int(duration_ms * rate_hz / 1000))
        start = time.perf_counter()
        samples = []
        for index in range(count):
            if self.is_real:
                time.sleep(max(0, start + index / rate_hz - time.perf_counter()))
            samples.append({"t_ms": round((time.perf_counter()-start)*1000, 3)
                            if self.is_real else round(index*1000/rate_hz, 3),
                            "pins": self.read_pins(pins)})
        return samples
