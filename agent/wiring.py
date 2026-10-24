"""Human-confirmed wiring; metadata never claims electrical continuity detection."""
from copy import deepcopy

from pi.header import Header
from profiles import load_profile


class Wiring:
    def __init__(self, emit):
        self.header = Header()
        self.emit = emit
        self.profile = None

    def request_connection(self, pi_pin, dut_pin, component, reason):
        pin = self.header.gpio(pi_pin)
        for value in (dut_pin, component, reason):
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError("dut_pin, component and reason must be nonempty text up to 500 chars")
        request = {"dut_pin": dut_pin, "component": component, "reason": reason}
        pin.update(connected=False, dut_pin=None, label=None, request=request)
        if self.profile:
            for wire in self.profile["wires"]:
                if wire["pi_pin"] == pi_pin and wire["dut_pin"] == dut_pin:
                    pin["direction"] = wire["direction"]
        self.emit("REQUEST", {"pi_pin": pi_pin, "status": "pending", "request": request})
        self.emit_header()
        return deepcopy(pin)

    def confirm_connection(self, pi_pin, confirmed=True):
        pin = self.header.gpio(pi_pin)
        request = pin["request"]
        if request is None:
            raise ValueError("no pending connection request for pin")
        if confirmed:
            pin.update(connected=True, dut_pin=request["dut_pin"],
                       label=f"{request['component']} {request['dut_pin']}")
        else:
            pin.update(connected=False, dut_pin=None, label=None, direction=None)
        pin["request"] = None
        self.emit("REQUEST", {"pi_pin": pi_pin, "status": "confirmed" if confirmed else "rejected",
                              "request": request})
        self.emit_header()
        return deepcopy(pin)

    def select(self, profile):
        self.profile = load_profile(profile)
        self.header = Header()
        for wire in self.profile["wires"]:
            self.header.gpio(wire["pi_pin"])["direction"] = wire["direction"]
            self.request_connection(wire["pi_pin"], wire["dut_pin"], self.profile["component"],
                                    f"Required for {profile}: {wire['label']}")
        return self.status()

    def missing(self):
        if self.profile is None:
            raise ValueError("select a DUT profile first")
        return [wire["pi_pin"] for wire in self.profile["wires"]
                if not self.header.gpio(wire["pi_pin"])["connected"] or
                self.header.gpio(wire["pi_pin"])["dut_pin"] != wire["dut_pin"] or
                self.header.gpio(wire["pi_pin"])["direction"] != wire["direction"]]

    def status(self):
        return {"profile": self.profile, "missing": self.missing(), "ready": not self.missing()}

    def requests(self):
        return [{"pi_pin": pin["bcm"], **deepcopy(pin["request"])}
                for pin in self.header.pins if pin["request"] is not None]

    def emit_header(self):
        self.emit("PIN_STATE", {"pins": self.header.snapshot()})

    def update(self, values):
        self.header.update(values)
        self.emit_header()
