"""Scriptable Virtual Rig behaviors: the "DUT side" of the header.

A behavior drives pins with ``rig.drive_external`` and reacts to pin changes,
UART traffic and bus transactions. Scenario JSON files instantiate behaviors by
``type`` with the remaining keys as constructor arguments.
"""
from __future__ import annotations

import re

from .. import pins as P

BOOT_DELAY_US = 300_000  # reset released -> the firmware prints its banner


class Behavior:
    name: str = ""

    def __init__(self, name: str = ""):
        self.name = name or type(self).__name__.lower()
        self.rig = None

    def attach(self, rig) -> None:
        self.rig = rig
        self.reset()

    def reset(self) -> None:
        pass

    def on_pin(self, pin: int, level: int, t_us: int) -> None:
        pass

    def on_uart_open(self, tx: int, rx: int, baud: int) -> None:
        pass

    def on_uart(self, pin: int, data: bytes, t_us: int) -> None:
        pass

    def on_flash(self) -> None:
        """The bridge just (re)programmed the DUT's flash; its UART ports are already closed."""

    def describe(self) -> dict:
        return {"type": type(self).__name__, "name": self.name}


def _gpio(pin: int) -> int:
    if pin not in P.GPIO_PINS:
        raise ValueError(f"GPIO{pin} is not an available BCM GPIO")
    return pin


class Loopback(Behavior):
    """A wire between two header pins. Whichever side the rig drives, the other follows."""

    def __init__(self, a: int, b: int, invert: bool = False, delay_us: int = 0, name: str = ""):
        super().__init__(name or f"loop{a}-{b}")
        self.a, self.b, self.invert, self.delay_us = _gpio(a), _gpio(b), invert, int(delay_us)
        self._driving: dict[int, int | None] = {}

    def _drive(self, other: int, level: int | None) -> None:
        self._driving[other] = level
        self.rig.drive_external(other, level)

    def _source_level(self, pin: int) -> int | None:
        """Level this end of the wire is actively sourcing (rig output, or a DUT behavior), else None."""
        state = self.rig.pins[pin]
        if state.mode in ("output", "pwm") and state.drive is not None:
            return state.drive
        if state.external is not None and state.external != self._driving.get(pin):
            return state.external
        return None

    def on_pin(self, pin: int, level: int, t_us: int) -> None:
        if pin not in (self.a, self.b):
            return
        other = self.b if pin == self.a else self.a
        source = self._source_level(pin)
        if source is None:
            if self._driving.get(other) is not None:
                self._drive(other, None)
            return
        target = source ^ int(self.invert)
        if self.delay_us:
            self.rig.after(self.delay_us, lambda: self._drive(other, target))
        else:
            self._drive(other, target)

    def describe(self) -> dict:
        return {**super().describe(), "a": self.a, "b": self.b, "invert": self.invert, "delay_us": self.delay_us}


class Button(Behavior):
    """A push button on ``pin`` with its own pull-up (idle = not pressed_level)."""

    def __init__(self, pin: int, pressed_level: int = 0, bounce_ms: int = 0, name: str = ""):
        super().__init__(name or f"button{pin}")
        self.pin, self.pressed_level, self.bounce_ms = _gpio(pin), int(pressed_level), int(bounce_ms)
        self.pressed = False

    def reset(self) -> None:
        self.pressed = False
        self.rig.drive_external(self.pin, 1 - self.pressed_level)

    def _settle(self, level: int) -> None:
        if self.bounce_ms:
            step = self.bounce_ms * 1000 // 6
            for i in range(1, 6):
                self.rig.after(step * i, lambda i=i: self.rig.drive_external(self.pin, level if i % 2 == 0 else 1 - level))
            self.rig.after(step * 6, lambda: self.rig.drive_external(self.pin, level))
        self.rig.drive_external(self.pin, level)

    def press(self) -> None:
        self.pressed = True
        self._settle(self.pressed_level)

    def release(self) -> None:
        self.pressed = False
        self._settle(1 - self.pressed_level)

    def click(self, ms: int = 100) -> None:
        self.press()
        self.rig.after(ms * 1000, self.release)

    def describe(self) -> dict:
        return {**super().describe(), "pin": self.pin, "pressed_level": self.pressed_level, "pressed": self.pressed}


class Blinker(Behavior):
    """Free-running square wave on ``pin`` (e.g. an LED the DUT blinks)."""

    def __init__(self, pin: int, hz: float = 2.0, duty: float = 0.5, name: str = ""):
        super().__init__(name or f"blink{pin}")
        self.pin, self.hz, self.duty = _gpio(pin), float(hz), float(duty)
        self._gen = 0

    def reset(self) -> None:
        self._gen += 1
        self._edge(self._gen, 1)

    def _edge(self, gen: int, level: int) -> None:
        if gen != self._gen:
            return
        self.rig.drive_external(self.pin, level)
        period = 1e6 / self.hz
        self.rig.after(round(period * (self.duty if level else 1 - self.duty)),
                       lambda: self._edge(gen, 1 - level))

    def describe(self) -> dict:
        return {**super().describe(), "pin": self.pin, "hz": self.hz, "duty": self.duty}


class UartEcho(Behavior):
    """A serial device: prints ``banner`` after ``delay_ms`` when the port opens (or on reset),
    echoes everything it receives, and answers lines found in ``responses``.

    ``rx_pin`` is the pin the *host* transmits on (its tx); ``tx_pin`` is where the device talks back.
    """

    def __init__(self, rx_pin: int = 14, tx_pin: int = 15, baud: int = 115200, banner: str = "BOOT OK\n",
                 delay_ms: int = 300, echo: bool = True, responses: dict | None = None, name: str = ""):
        super().__init__(name or "uart")
        self.rx_pin, self.tx_pin, self.baud = _gpio(rx_pin), _gpio(tx_pin), int(baud)
        self.banner, self.delay_ms, self.echo = banner.encode(), int(delay_ms), echo
        self.responses = {k.encode(): v.encode() for k, v in (responses or {}).items()}
        self._line = b""

    def reset(self) -> None:
        self._line = b""
        if self.banner and self.rig.uart_open_for(self.tx_pin):
            self.rig.after(self.delay_ms * 1000, lambda: self.rig.uart_deliver(self.tx_pin, self.banner))

    def on_uart_open(self, tx: int, rx: int, baud: int) -> None:
        if tx == self.rx_pin and rx == self.tx_pin:
            self.reset()

    def on_uart(self, pin: int, data: bytes, t_us: int) -> None:
        if pin != self.rx_pin:
            return
        if self.echo:
            self.rig.uart_deliver(self.tx_pin, data)
        self._line += data
        while b"\n" in self._line:
            line, self._line = self._line.split(b"\n", 1)
            reply = self.responses.get(line + b"\n") or self.responses.get(line)
            if reply:
                self.rig.uart_deliver(self.tx_pin, reply)

    def describe(self) -> dict:
        return {**super().describe(), "rx_pin": self.rx_pin, "tx_pin": self.tx_pin, "baud": self.baud}


class I2cRegisterDevice(Behavior):
    """A register-mapped I2C peripheral (e.g. MPU6050 WHO_AM_I at 0x75 -> 0x68)."""

    def __init__(self, addr: int, registers: dict | None = None, name: str = ""):
        super().__init__(name or f"i2c{addr:02x}")
        self.addr, self.registers = int(addr), {int(k, 0) if isinstance(k, str) else int(k): int(v)
                                                for k, v in (registers or {}).items()}
        self._pointer = 0

    def attach(self, rig) -> None:
        super().attach(rig)
        rig.i2c_devices[self.addr] = self

    def i2c_write(self, data: bytes) -> None:
        if data:
            self._pointer = data[0]
            for offset, value in enumerate(data[1:]):
                self.registers[self._pointer + offset] = value

    def i2c_read(self, n: int, reg: int | None) -> bytes:
        start = self._pointer if reg is None else reg
        return bytes(self.registers.get(start + i, 0) & 0xFF for i in range(n))

    def describe(self) -> dict:
        return {**super().describe(), "addr": self.addr}


class SpiLoopback(Behavior):
    """MOSI tied to MISO behind chip-select ``cs``: every transfer reads back what was written."""

    def __init__(self, cs: int = 0, name: str = ""):
        super().__init__(name or f"spi{cs}")
        self.cs = int(cs)

    def attach(self, rig) -> None:
        super().attach(rig)
        rig.spi_devices[self.cs] = self

    def spi_xfer(self, data: bytes) -> bytes:
        return bytes(data)


class ScriptedDut(Behavior):
    """The ESP32 safety-controller demo DUT (port of the legacy SimBackend / firmware).

    Rig drives: trigger (GPIO17 -> ESP GPIO4), PWM (GPIO18 -> GPIO5), fault (GPIO27 -> GPIO18).
    DUT drives: actuator (GPIO22 <- GPIO19), safety relay (GPIO23 <- GPIO21), UART telemetry on GPIO15.
    With ``demo_bug`` the firmware skips updating the safety relay when a fault arrives while the
    trigger is high or PWM duty > 80 %, so the relay stays engaged - the planted bug plans must catch.
    """

    banner = b"BOOT OK\n"

    def __init__(self, demo_bug: bool = True, trigger: int = 17, pwm: int = 18, fault: int = 27,
                 actuator: int = 22, safety: int = 23, uart_tx: int = 15, uart_rx: int = 14,
                 baud: int = 115200, name: str = ""):
        super().__init__(name or "esp32")
        self.demo_bug = bool(demo_bug)
        self.trigger, self.pwm, self.fault = _gpio(trigger), _gpio(pwm), _gpio(fault)
        self.actuator, self.safety, self.uart_tx, self.uart_rx, self.baud = (
            _gpio(actuator), _gpio(safety), _gpio(uart_tx), _gpio(uart_rx), int(baud))
        self._pwm_window: list[tuple[int, int]] = []
        self.state = "IDLE"
        self.telemetry: dict = {}

    def reset(self) -> None:
        self._pwm_window = []
        self.state = "IDLE"
        self.rig.drive_external(self.actuator, 0)
        self.rig.drive_external(self.safety, 1)
        self._schedule_banner()
        self._update()

    def on_uart_open(self, tx: int, rx: int, baud: int) -> None:
        if rx == self.uart_tx:
            self._schedule_banner()

    def _schedule_banner(self) -> None:
        if self.rig.uart_open_for(self.uart_tx):
            self.rig.after(BOOT_DELAY_US, lambda: self.rig.uart_deliver(self.uart_tx, self.banner))

    def duty(self) -> float:
        pin = self.rig.pins[self.pwm]
        if pin.pwm:
            return pin.pwm[1]
        return float(pin.level) if pin.mode in ("output", "pwm") else 0.0

    def on_pin(self, pin: int, level: int, t_us: int) -> None:
        if pin in (self.trigger, self.fault) or (pin == self.pwm and self.rig.pins[pin].pwm is None):
            self._update()

    def _update(self) -> None:
        trig = self.rig.pins[self.trigger].level
        fault = self.rig.pins[self.fault].level
        duty = self.duty()
        actuator = int(trig and not fault and duty > 0)
        safety = int(not fault)
        skipped = self.demo_bug and fault and (trig or duty > 0.8)
        self.rig.drive_external(self.actuator, actuator)
        if not skipped:
            self.rig.drive_external(self.safety, safety)
        self.state = "FAULT" if fault else ("RUN" if actuator else "IDLE")
        telemetry = {"state": self.state, "trig": trig, "duty": round(duty, 2), "fault": fault,
                     "actuator": actuator, "safety": safety}
        if telemetry != self.telemetry:
            self.telemetry = telemetry
            line = " ".join(f"{k}={v}" for k, v in telemetry.items()).encode() + b"\n"
            self.rig.uart_deliver(self.uart_tx, line)

    def describe(self) -> dict:
        return {**super().describe(), "demo_bug": self.demo_bug, "state": self.state}


# firmware/esp32_dut/esp32_dut.ino constants
LCD_COLS = 16
COMMAND_MAX = 64            # line buffer: characters past this are dropped
LED_HZ_MAX = 50.0
BOOT_LCD = ("Aperture OS", "rig-01 online")
BOOT_LED = ("blink", 2.0)
EOL = b"\r\n"               # Serial.println
ARDUINO_SPACE = " \t\n\v\f\r"  # what String::trim() strips
_LEADING_FLOAT = re.compile(r"[ \t\n\v\f\r]*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def _atof(text: str) -> float:
    """String::toFloat (C atof): the leading decimal number, 0.0 when there is none."""
    match = _LEADING_FLOAT.match(text)
    return float(match.group(1)) if match else 0.0


class Esp32Dut(ScriptedDut):
    """rig-01's ESP32 DevKit V1 running firmware/esp32_dut/esp32_dut.ino.

    The ScriptedDut safety controller plus the 16x2 LCD, the status LED on ESP32 GPIO13 (not wired
    to the Pi) and the line-based UART0 commands with the firmware's replies. ``en`` is the Pi pin on
    ESP32 EN: driven low it holds the chip in reset (outputs high-Z); released or driven high the
    DevKit pull-up boots it again. Every boot (power-on, EN release, after a flash) restores the
    LCD/LED defaults and prints the banner if a UART is open. GPIO24 -> BOOT is not modelled.
    """

    banner = b"BOOT OK" + EOL

    def __init__(self, en: int = 25, **kwargs):
        super().__init__(**kwargs)
        self.en = _gpio(en)
        self.lcd, (self.led_mode, self.led_hz) = BOOT_LCD, BOOT_LED
        self.running, self._boot, self._line = False, 0, b""

    def _en_level(self) -> int:
        pin = self.rig.pins[self.en]
        return pin.drive if pin.mode in ("output", "pwm") and pin.drive is not None else 1

    def reset(self) -> None:
        if self._en_level() == 0:
            self._hold_in_reset()
            return
        self.running, self._boot, self._line = True, self._boot + 1, b""
        self.lcd, (self.led_mode, self.led_hz) = BOOT_LCD, BOOT_LED
        super().reset()

    def _hold_in_reset(self) -> None:
        self.running, self._boot, self.state = False, self._boot + 1, "RESET"
        self.rig.drive_external(self.actuator, None)
        self.rig.drive_external(self.safety, None)

    def on_flash(self) -> None:
        self.reset()  # the new image boots; its banner follows the next uart.open

    def on_pin(self, pin: int, level: int, t_us: int) -> None:
        if pin != self.en:
            if self.running:
                super().on_pin(pin, level, t_us)
        elif self.running and self._en_level() == 0:
            self._hold_in_reset()
        elif not self.running and self._en_level() == 1:
            self.reset()

    def _schedule_banner(self) -> None:
        if self.running and self.rig.uart_open_for(self.uart_tx):
            boot = self._boot
            self.rig.after(BOOT_DELAY_US, lambda: self._print_banner(boot))

    def _print_banner(self, boot: int) -> None:
        if boot == self._boot:  # not reset again before setup() got there
            self.rig.uart_deliver(self.uart_tx, self.banner)

    def on_uart(self, pin: int, data: bytes, t_us: int) -> None:
        if pin != self.uart_rx or not self.running:
            return
        for byte in data:
            if byte in b"\r\n":
                line, self._line = self._line, b""
                self._command(line.decode("latin-1").strip(ARDUINO_SPACE))
            elif len(self._line) < COMMAND_MAX:
                self._line += bytes([byte])

    def _command(self, cmd: str) -> None:
        if cmd.startswith("LCD "):
            top, _, bottom = cmd[4:].partition("|")
            self.lcd = (top[:LCD_COLS], bottom[:LCD_COLS])
            self._println("OK LCD")
        elif cmd in ("LED ON", "LED OFF"):
            self.led_mode = cmd[4:].lower()
            self._println(f"OK {cmd}")
        elif cmd.startswith("LED BLINK "):
            self._blink(cmd[len("LED BLINK "):])
        elif cmd:
            self._println("ERR unknown command")

    def _blink(self, text: str) -> None:
        hz = _atof(text)
        if not 0 < hz <= LED_HZ_MAX:
            self._println("ERR LED hz must be 0..50")
            return
        self.led_mode, self.led_hz = "blink", hz
        self._println(f"OK LED {text}")

    def _println(self, text: str) -> None:
        self.rig.uart_deliver(self.uart_tx, text.encode("latin-1") + EOL)

    def describe(self) -> dict:
        return {**super().describe(), "lcd": list(self.lcd), "led": {"mode": self.led_mode, "hz": self.led_hz}}


BEHAVIORS = {cls.__name__: cls for cls in (Loopback, Button, Blinker, UartEcho, I2cRegisterDevice,
                                            SpiLoopback, ScriptedDut, Esp32Dut)}


def make_behavior(spec: dict) -> Behavior:
    spec = dict(spec)
    kind = spec.pop("type", None)
    if kind not in BEHAVIORS:
        raise ValueError(f"unknown behavior type {kind!r}; known: {', '.join(sorted(BEHAVIORS))}")
    spec.pop("wired_to", None)
    return BEHAVIORS[kind](**spec)
