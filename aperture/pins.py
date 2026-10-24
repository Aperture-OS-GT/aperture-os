"""Raspberry Pi 40-pin J8 header model and capability manifest.

GPIO identifiers are BCM numbers everywhere; the physical position (1-40) is
display metadata. The manifest returned by ``build_manifest`` is the shape every
bridge (real Pi or Virtual Rig) reports on ``hello`` so the UI never assumes a
header layout.
"""
from __future__ import annotations

from dataclasses import dataclass

HEADER = (
    "3V3", "5V", 2, "5V", 3, "GND", 4, 14, "GND", 15,
    17, 18, 27, "GND", 22, 23, "3V3", 24, 10, "GND",
    9, 25, 11, 8, "GND", 7, "ID_SD", "ID_SC", 5, "GND",
    6, 12, 13, "GND", 19, 16, 26, 20, "GND", 21,
)
GPIO_PINS = frozenset(pin for pin in HEADER if type(pin) is int)
PHYSICAL = {pin: index for index, pin in enumerate(HEADER, 1) if type(pin) is int}
HW_PWM = frozenset({12, 13, 18, 19})
ALT = {2: "SDA1", 3: "SCL1", 4: "GPCLK0", 7: "CE1", 8: "CE0", 9: "MISO", 10: "MOSI", 11: "SCLK",
       12: "PWM0", 13: "PWM1", 14: "TXD0", 15: "RXD0", 18: "PWM0", 19: "PWM1"}
UART_PORTS = ({"id": 0, "tx": 14, "rx": 15},)
SPI_BUSES = ({"bus": 0, "mosi": 10, "miso": 9, "sclk": 11, "cs": [8, 7]},)
I2C_BUSES = ({"bus": 1, "sda": 2, "scl": 3},)

MODES = ("input", "output", "input_pullup", "input_pulldown", "pwm", "disabled")
INPUT_MODES = ("input", "input_pullup", "input_pulldown")
DEFAULT_LIMITS = {"capture_hz": 10_000, "pulse_us": 1_000_000, "pwm_hz": 10_000, "mtu": 244,
                  "watchdog_s": 5, "idle_s": 30}


@dataclass(frozen=True)
class PinInfo:
    bcm: int
    phys: int
    caps: tuple[str, ...]
    alt: str | None

    def as_dict(self) -> dict:
        data = {"bcm": self.bcm, "phys": self.phys, "caps": list(self.caps)}
        if self.alt:
            data["alt"] = self.alt
        return data


def pin_info(bcm: int) -> PinInfo:
    if bcm not in GPIO_PINS:
        raise ValueError(f"GPIO{bcm} is not an available BCM GPIO (ID_SD/ID_SC are reserved)")
    caps = ["in", "out", "pwm" if bcm in HW_PWM else "pwm_sw"]
    for port in UART_PORTS:
        if bcm in (port["tx"], port["rx"]):
            caps.append("uart")
    for bus in SPI_BUSES:
        if bcm in (bus["mosi"], bus["miso"], bus["sclk"], *bus["cs"]):
            caps.append("spi")
    for bus in I2C_BUSES:
        if bcm in (bus["sda"], bus["scl"]):
            caps.append("i2c")
    return PinInfo(bcm, PHYSICAL[bcm], tuple(caps), ALT.get(bcm))


def header_rows() -> list[dict]:
    """All 40 physical positions, for rendering a to-scale header."""
    rows = []
    for phys, signal in enumerate(HEADER, 1):
        if type(signal) is int:
            rows.append({"phys": phys, "kind": "gpio", **pin_info(signal).as_dict()})
        else:
            rows.append({"phys": phys, "kind": {"3V3": "3v3", "5V": "5v", "GND": "gnd"}.get(signal, "reserved"),
                         "label": signal})
    return rows


def build_manifest(bridge: str, fw: str, board: str, limits: dict | None = None,
                   power: dict | None = None, denied: set[int] | frozenset[int] = frozenset()) -> dict:
    return {
        "bridge": bridge, "fw": fw, "proto": 1, "board": board, "header": "J8-40",
        "pins": [pin_info(bcm).as_dict() for bcm in sorted(GPIO_PINS) if bcm not in denied],
        "uart": [dict(p) for p in UART_PORTS],
        "spi": [{**b, "cs": list(b["cs"])} for b in SPI_BUSES],
        "i2c": [dict(b) for b in I2C_BUSES],
        "limits": {**DEFAULT_LIMITS, **(limits or {})},
        "power": {"battery_pct": None, "vbus_ok": None, **(power or {})},
    }
