"""Raspberry Pi 40-pin J8 header. GPIO identifiers are BCM, never physical."""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone

HEADER = (
    "3V3", "5V", 2, "5V", 3, "GND", 4, 14, "GND", 15,
    17, 18, 27, "GND", 22, 23, "3V3", 24, 10, "GND",
    9, 25, 11, 8, "GND", 7, "ID_SD", "ID_SC", 5, "GND",
    6, 12, 13, "GND", 19, 16, 26, 20, "GND", 21,
)
GPIO_PINS = frozenset(pin for pin in HEADER if type(pin) is int)


class Header:
    def __init__(self):
        self.pins = []
        self.history = {pin: deque(maxlen=512) for pin in GPIO_PINS}
        for physical, signal in enumerate(HEADER, 1):
            gpio = type(signal) is int
            self.pins.append({"physical": physical, "bcm": signal if gpio else None,
                              "kind": "GPIO" if gpio else signal,
                              "connected": False, "dut_pin": None, "label": None,
                              "direction": None, "value": 0, "request": None})

    def gpio(self, pin):
        if type(pin) is not int or pin not in GPIO_PINS:
            raise ValueError("pin must be an available BCM GPIO (ID_SD/ID_SC reserved)")
        return next(item for item in self.pins if item["bcm"] == pin)

    def update(self, values):
        for value in values:
            pin = self.gpio(value["pin"])
            for key in ("duty", "decoded"):
                if key not in value:
                    pin.pop(key, None)
            for key in ("value", "duty", "decoded", "direction"):
                if key in value:
                    pin[key] = deepcopy(value[key])
            self.history[value["pin"]].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **{k: deepcopy(pin[k]) for k in ("value", "duty", "decoded") if k in pin}})

    def snapshot(self):
        return deepcopy(self.pins)
