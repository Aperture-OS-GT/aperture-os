"""Agent tool set bound to a Session.

Every tool is one ``Request`` with the full lifecycle; tools that drive a pin are
wiring-gated (policy ``ask``) so the agent must have a human mark the wire first.
``schemas()`` generates OpenAI-compatible function schemas from ``SPECS`` so the
LLM, the plan validator and the implementations share one source of truth. The ESP32
tools (``program_esp32``, ``led``, ``lcd_print``) talk to the esp32_dut firmware over the
Pi's UART0 (GPIO14/15); a request refused with ``not_controller`` re-takes control once.
"""
from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable, Iterable

from . import firmware as fw
from .pins import MODES
from .protocol import ProtocolError
from .session import Session
from .transport.base import Transport, TransportError

EDGES = ("rising", "falling", "any")
TRANSPORTS = ("virtual", "tcp", "ble")
LED_STATES = ("blink", "on", "off")
CONTROL_OPS = frozenset({"control.take", "control.release"})
VIRTUAL_BRIDGE = "virtual-rig"
DEFAULT_TCP_HOST = "127.0.0.1"
TCP_TARGET = re.compile(r"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<host>[^\s:\[\]]*))(?::(?P<port>\d{1,5}))?")
UART_READ_MAX = 4096
FLUSH_READS_MAX = 8
# ESP32 DUT wiring (BCM on the Pi side): UART0 for flashing and commands, EN reset, BOOT strap.
ESP32_TX, ESP32_RX, ESP32_BAUD = 14, 15, 115200
ESP32_EN, ESP32_BOOT = 25, 24
ESP32_WIRING = {ESP32_TX: ("RX0", "out"), ESP32_RX: ("TX0", "in"), ESP32_EN: ("EN", "out"),
                ESP32_BOOT: ("BOOT/GPIO0", "out")}
FLASH_CHUNK = 32 * 1024  # protocol limit for one bytes field
FLASH_BAUD = 115200
BOOT_BANNER, BOOT_TIMEOUT_MS = r"BOOT OK", 4000
DUT_REPLY_TIMEOUT_MS = 2000
LED_REPLY = r"(OK LED[^\r\n]*|ERR[^\r\n]*)"
LCD_REPLY = r"(OK LCD|ERR[^\r\n]*)"
LCD_COLS = 16
DEFAULT_BLINK_HZ = 2


class ToolError(Exception):
    def __init__(self, code: str, msg: str, **extra):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg, self.extra = code, msg, extra


def _p(type_: str, description: str, required: bool = True, **extra) -> dict:
    return {"type": type_, "description": description, "required": required, **extra}


PIN = _p("integer", "BCM GPIO number (e.g. 17 for GPIO17)")
SPECS: dict[str, dict] = {
    "connect": {"description": "Connect to a rig (Virtual Rig, TCP bridge or BLE bridge) and take control.",
                "params": {"transport": _p("string", "Transport kind", enum=list(TRANSPORTS)),
                           "scenario": _p("string", "Virtual Rig scenario name", False),
                           "target": _p("string", "host:port (tcp) or device name/address (ble)", False),
                           "take_control": _p("boolean", "Take control after connecting (default true)", False)}},
    "disconnect": {"description": "Release all outputs, release control and disconnect.", "params": {}},
    "take_control": {"description": "Become the rig controller.",
                     "params": {"force": _p("boolean", "Force if the current controller is idle", False)}},
    "release_control": {"description": "Give up control (outputs are released).", "params": {}},
    "list_pins": {"description": "Every GPIO with mode, level, capabilities and wiring.", "params": {}},
    "set_mode": {"description": "Configure a pin. Modes: input, output, input_pullup, input_pulldown, pwm, disabled.",
                 "params": {"pin": PIN, "mode": _p("string", "Pin mode", enum=list(MODES)),
                            "initial": _p("integer", "Initial level for output (0/1)", False, enum=[0, 1])}},
    "write": {"description": "Drive an output pin to 0 or 1 and read it back.",
              "params": {"pin": PIN, "level": _p("integer", "0 or 1", enum=[0, 1])}},
    "read": {"description": "Read one or more pins.",
             "params": {"pin": _p("integer", "Single BCM GPIO", False),
                        "pins": _p("array", "List of BCM GPIOs", False, items={"type": "integer"})}},
    "pulse": {"description": "Drive a pin to level for N microseconds then restore it.",
              "params": {"pin": PIN, "level": _p("integer", "0 or 1", enum=[0, 1]),
                         "us": _p("integer", "Pulse width in microseconds")}},
    "pwm": {"description": "Start PWM on a pin in pwm mode (duty 0 stops).",
            "params": {"pin": PIN, "freq_hz": _p("number", "Frequency in Hz"),
                       "duty": _p("number", "Duty cycle 0..1")}},
    "uart_open": {"description": "Open a UART on two pins.",
                  "params": {"tx": _p("integer", "Rig TX pin (BCM)"), "rx": _p("integer", "Rig RX pin (BCM)"),
                             "baud": _p("integer", "Baud rate")}},
    "uart_close": {"description": "Close a UART port.", "params": {"port": _p("integer", "Port id from uart_open")}},
    "uart_send": {"description": "Send text (\\n escapes honoured) or hex:XX.. bytes over a UART port.",
                  "params": {"port": _p("integer", "Port id"), "data": _p("string", "Text or hex:...")}},
    "uart_expect": {"description": "Wait until a regex matches the received UART text.",
                    "params": {"port": _p("integer", "Port id"), "pattern": _p("string", "Python regex"),
                               "timeout_ms": _p("integer", "Timeout in ms")}},
    "spi_xfer": {"description": "Full-duplex SPI transfer; returns hex read back.",
                 "params": {"bus": _p("integer", "SPI bus"), "cs": _p("integer", "Chip select index"),
                            "data": _p("string", "hex bytes, e.g. 9f00")}},
    "i2c_scan": {"description": "List I2C addresses that acknowledge.", "params": {"bus": _p("integer", "I2C bus")}},
    "i2c_read": {"description": "Read n bytes from an I2C device (optionally from a register).",
                 "params": {"bus": _p("integer", "I2C bus"), "addr": _p("integer", "7-bit address"),
                            "n": _p("integer", "Bytes to read"), "reg": _p("integer", "Register", False)}},
    "i2c_write": {"description": "Write hex bytes to an I2C device.",
                  "params": {"bus": _p("integer", "I2C bus"), "addr": _p("integer", "7-bit address"),
                             "data": _p("string", "hex bytes")}},
    "capture": {"description": "Sample pins for a window and return an edge-list timeline.",
                "params": {"pins": _p("array", "BCM GPIOs", items={"type": "integer"}),
                           "duration_ms": _p("integer", "Window length"),
                           "rate_hz": _p("integer", "Sample rate (default: bridge max)", False)}},
    "assert_pin": {"description": "Assert a pin reads the expected level.",
                   "params": {"pin": PIN, "level": _p("integer", "Expected level", enum=[0, 1]),
                              "name": _p("string", "Assertion name", False)}},
    "assert_timeline": {"description": "Assert captured edges match expected [[t_us, pin, level], ...] within tolerance.",
                        "params": {"edges": _p("array", "Expected edges relative to capture start",
                                               items={"type": "array"}),
                                   "tolerance_us": _p("integer", "Allowed timing error"),
                                   "capture_id": _p("integer", "Capture to check (default: last)", False),
                                   "allow_extra": _p("boolean", "Ignore extra edges (default false)", False),
                                   "name": _p("string", "Assertion name", False)}},
    "wait_for": {"description": "Wait for an edge on a pin (streams the pin).",
                 "params": {"pin": PIN, "edge": _p("string", "rising, falling or any", enum=list(EDGES)),
                            "timeout_ms": _p("integer", "Timeout in ms")}},
    "request_wiring": {"description": "Ask the human to wire a rig pin to a DUT pin before driving it.",
                       "params": {"pin": PIN, "dut_pin": _p("string", "DUT pin label"),
                                  "component": _p("string", "What it connects to"),
                                  "reason": _p("string", "Why the test needs it"),
                                  "dir": _p("string", "out = rig drives, in = rig observes", enum=["in", "out"])}},
    "assert_frequency": {"description": "Assert the last capture shows a square wave of the given frequency on a pin.",
                         "params": {"pin": PIN, "hz": _p("number", "Expected frequency"),
                                    "tolerance_hz": _p("number", "Allowed error in Hz"),
                                    "name": _p("string", "Assertion name", False)}},
    "release_all": {"description": "Return every output to high-impedance, stop PWM, close buses.", "params": {}},
    "note": {"description": "Record a note in the session log.", "params": {"text": _p("string", "Note text")}},
    "dut_action": {"description": "Virtual Rig only: act on a behavior (e.g. press/release/click a button, reset).",
                   "params": {"behavior": _p("string", "Behavior name, or 'dut' for reset"),
                              "action": _p("string", "press, release, click, reset"),
                              "ms": _p("integer", "Click duration", False)}},
    "program_esp32": {"description": "Build ESP32 firmware with PlatformIO and flash it from the Pi over UART0 "
                                     "(GPIO14 TXD -> ESP32 RX0, GPIO15 RXD <- TX0), resetting through GPIO25 -> EN "
                                     "and entering the ROM bootloader through GPIO24 -> BOOT/GPIO0. Needs GPIO14, 15, "
                                     "25 and 24 marked as wired (not 24 with manual_boot). Every open UART port is "
                                     "closed first, so earlier port ids become invalid; the ESP32 UART is reopened "
                                     "(result 'port') and boot_ok says whether the firmware printed 'BOOT OK'. Can "
                                     "take minutes. On the Virtual Rig nothing is built: the flash is simulated with "
                                     "the last build, or placeholder images when there is none.",
                      "params": {"firmware": _p("string", "PlatformIO project under firmware/ (default esp32_dut)",
                                                False),
                                 "build": _p("boolean", "Build before flashing (default true); false flashes the "
                                                        "last build. Ignored on the Virtual Rig", False),
                                 "manual_boot": _p("boolean", "Set when GPIO24 is not wired to BOOT: the human holds "
                                                              "the ESP32 BOOT button through the reset and lets go "
                                                              "once flashing starts (default false)", False)}},
    "led": {"description": "Drive the ESP32 status LED (ESP32 GPIO13) through the esp32_dut firmware's UART0 "
                           "command port (Pi GPIO14/15 at 115200 baud, opened on first use): blink at hz, or hold "
                           "it on or off. Returns the firmware's reply ('OK LED ...'); an 'ERR ...' reply fails "
                           "with dut_error.",
            "params": {"state": _p("string", "blink, on or off", enum=list(LED_STATES)),
                       "hz": _p("number", "Blink frequency in Hz, 0 < hz <= 50 (default 2); blink only", False)}},
    "lcd_print": {"description": "Show text on the ESP32's 16x2 LCD through the esp32_dut firmware's UART0 command "
                                 "port (Pi GPIO14/15 at 115200 baud, opened on first use). Each line is cut to 16 "
                                 "characters and '|' and line breaks are removed. Waits for 'OK LCD'; an 'ERR ...' "
                                 "reply fails with dut_error.",
                  "params": {"line1": _p("string", "Top line (16 characters max)"),
                             "line2": _p("string", "Bottom line (16 characters max, default blank)", False)}},
}
WIRING_GATED = {"set_mode", "write", "pulse", "pwm", "uart_open", "program_esp32", "led", "lcd_print"}


def schemas() -> list[dict]:
    """OpenAI function-calling schemas generated from SPECS."""
    out = []
    for name, spec in SPECS.items():
        properties, required = {}, []
        for param, meta in spec["params"].items():
            properties[param] = {k: v for k, v in meta.items() if k != "required"}
            if meta["required"]:
                required.append(param)
        out.append({"type": "function", "function": {"name": name, "description": spec["description"],
                                                     "parameters": {"type": "object", "properties": properties,
                                                                    "required": required}}})
    return out


def validate_args(name: str, args: dict) -> dict:
    if name not in SPECS:
        raise ToolError("unknown_tool", f"unknown tool {name!r}")
    spec = SPECS[name]["params"]
    extra = set(args) - set(spec)
    if extra:
        raise ToolError("bad_request", f"{name}: unexpected arguments {sorted(extra)}")
    out = {}
    for param, meta in spec.items():
        if param not in args or args[param] is None:
            if meta["required"]:
                raise ToolError("bad_request", f"{name}: missing argument {param!r}")
            continue
        value = args[param]
        kind = meta["type"]
        ok = {"integer": lambda v: type(v) is int, "number": lambda v: type(v) in (int, float),
              "string": lambda v: isinstance(v, str), "boolean": lambda v: type(v) is bool,
              "array": lambda v: isinstance(v, list)}[kind](value)
        if not ok or ("enum" in meta and value not in meta["enum"]):
            raise ToolError("bad_request", f"{name}: argument {param!r} must be {kind}"
                            + (f" in {meta['enum']}" if "enum" in meta else ""))
        out[param] = value
    return out


def _bytes(data: str) -> bytes:
    data = data.removeprefix("hex:")
    try:
        return bytes.fromhex(data.replace(" ", ""))
    except ValueError:
        raise ToolError("bad_request", f"invalid hex {data!r}") from None


def _b64(value) -> bytes:
    """Byte fields arrive base64-encoded after the JSON round trip; accept bytes too."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return base64.b64decode(value) if value else b""


def _text_or_hex(data: str) -> bytes:
    if data.startswith("hex:"):
        return _bytes(data)
    return data.encode("utf-8").decode("unicode_escape").encode("latin-1")


def _lcd_text(text: str) -> str:
    """One LCD row: printable characters only, no '|' (the firmware's line separator), 16 at most."""
    return "".join(ch for ch in text if ch.isprintable() and ch != "|")[:LCD_COLS]


class Tools:
    def __init__(self, session: Session, wiring_policy: str | None = None,
                 transport_factory: Callable[..., Transport] | None = None):
        self.session = session
        self.wiring_policy = wiring_policy
        self.transport_factory = transport_factory or default_transport_factory
        self.last_capture: dict | None = None
        self._rx: dict[int, bytearray] = {}
        self._uart_pins: dict[int, tuple[int, int, int]] = {}  # open port -> (tx, rx, baud)

    # --- dispatch -------------------------------------------------------------------
    def call(self, tool: str, /, **args) -> dict:
        args = validate_args(tool, args)
        return getattr(self, tool)(**args)

    def _policy(self) -> str:
        if self.wiring_policy:
            return self.wiring_policy
        scenario = getattr(getattr(self.session.transport, "rig", None), "scenario", None)
        return (scenario or {}).get("wiring_policy", "ask")

    def _gate(self, *pins: int) -> None:
        if self._policy() == "off":
            return
        for pin in pins:
            if pin not in self.session.wiring:
                if pin not in self.session.pending_wiring:
                    self.session.request_wiring(pin, "?", "unknown", "agent wants to use this pin", "out", "agent")
                raise ToolError("wiring_required", f"GPIO{pin} has not been marked as wired; ask the human to confirm",
                                pin=pin)

    def _req(self, op: str, observe=None, **args):
        request = self.session.request(op, actor="agent", observe=observe, **args)
        if not request.ok and request.err == "not_controller" and op not in CONTROL_OPS \
                and self._retake_control(op):
            request = self.session.request(op, actor="agent", observe=observe, **args)
        if not request.ok:
            raise ToolError(request.err or "io", self._bridge_msg(request), request=request.id,
                            **{k: v for k, v in request.result.items()})
        return request

    def _bridge_msg(self, request) -> str:
        """The bridge's error text: the Request keeps the code, the session's executed event the text."""
        msg = request.stages["executed"].get("msg")
        if not msg:
            event = next((e for e in reversed(self.session.events) if e["kind"] == "request.stage"
                          and e.get("req") == request.id and e.get("stage") == "executed"), None)
            msg = (event or {}).get("msg")
        return msg or "bridge error"

    def _retake_control(self, op: str) -> bool:
        """Take control back once after the bridge refused ``op``. Losing control released the rig, so
        every cached UART port is stale afterwards."""
        transport = self.session.transport
        if transport is None or not transport.connected:
            return False
        reason = self._lost_reason(op)
        token = {"token": transport.control_token} if transport.control_token else {}
        if not self.session.request("control.take", actor="agent", **token).ok:
            return False
        self._forget_ports(self._uart_pins.keys() | self._rx.keys())
        self.session.note(f"control re-taken after {reason}; retrying {op}", "agent")
        return True

    def _lost_reason(self, op: str) -> str:
        last = next((e for e in reversed(self.session.events) if e["kind"] in ("control.lost", "control.changed")),
                    None)
        if last is not None and last["kind"] == "control.lost":
            return f"control.lost ({last.get('reason', 'unknown')})"
        return f"not_controller on {op}"

    def _forget_ports(self, ports: Iterable[int]) -> None:
        for port in list(ports):
            self._rx.pop(port, None)
            self._uart_pins.pop(port, None)

    def _result(self, request, **extra) -> dict:
        return {"request": request.id, "stage": request.stage, **request.result, **extra}

    # --- connection -----------------------------------------------------------------
    def connect(self, transport: str, scenario: str | None = None, target: str | None = None,
                take_control: bool = True) -> dict:
        t = self.transport_factory(transport, scenario=scenario, target=target)
        manifest = self.session.connect(t, take_control=take_control)
        return {"manifest": manifest, "transport": t.describe(), "controller": bool(t.control_token)}

    def disconnect(self) -> dict:
        self.session.disconnect("user")
        return {"connected": False}

    def take_control(self, force: bool = False) -> dict:
        args = {"force": force}
        if self.session.transport.control_token:
            args["token"] = self.session.transport.control_token
        return self._result(self._req("control.take", **args))

    def release_control(self) -> dict:
        token = self.session.transport.control_token
        if not token:
            raise ToolError("not_controller", "this session does not hold control")
        return self._result(self._req("control.release", token=token))

    # --- pins -------------------------------------------------------------------------
    def list_pins(self) -> dict:
        request = self._req("list_pins")
        caps = {p["bcm"]: p for p in (self.session.manifest or {}).get("pins", [])}
        pins = []
        for state in request.result["pins"]:
            info = caps.get(state["pin"], {})
            pins.append({**state, "phys": info.get("phys"), "caps": info.get("caps", []), "alt": info.get("alt"),
                         "wiring": self.session.wiring.get(state["pin"]),
                         "pending_wiring": self.session.pending_wiring.get(state["pin"])})
        return {"request": request.id, "pins": pins, "controller": request.result.get("controller")}

    def _readback(self, pin: int, expected: int | None = None):
        def observe(response):
            read = self.session.transport.request("pin.read", pin=pin)
            if not read.ok:
                return {"observe": "readback", "result": "unavailable", "err": read.err}
            level = read.result["level"]
            out = {"observe": "readback", "level": level, "t_bridge_us": read.t_bridge_us}
            if expected is not None:
                out["mismatch"] = level != expected
            return out
        return observe

    def set_mode(self, pin: int, mode: str, initial: int | None = None) -> dict:
        self._gate(pin)
        args = {"pin": pin, "mode": mode}
        if initial is not None:
            args["initial"] = initial
        observe = None if mode == "disabled" else self._readback(pin, initial if mode == "output" else None)
        return self._result(self._req("pin.mode", observe, **args))

    def write(self, pin: int, level: int) -> dict:
        self._gate(pin)
        request = self._req("pin.write", self._readback(pin, level), pin=pin, level=level)
        return self._result(request, observed=request.observation)

    def read(self, pin: int | None = None, pins: list[int] | None = None) -> dict:
        targets = [pin] if pin is not None else list(pins or [])
        if not targets:
            raise ToolError("bad_request", "read needs pin or pins")
        levels = {}
        for target in targets:
            levels[target] = self._req("pin.read", pin=target).result["level"]
        return {"levels": levels, **({"level": levels[pin]} if pin is not None else {})}

    def pulse(self, pin: int, level: int, us: int) -> dict:
        self._gate(pin)
        request = self._req("pin.pulse", lambda r: {"observe": "edges", "edges": r.result.get("edges")},
                            pin=pin, level=level, us=us)
        return self._result(request)

    def pwm(self, pin: int, freq_hz: float, duty: float) -> dict:
        self._gate(pin)
        return self._result(self._req("pin.pwm", pin=pin, freq_hz=freq_hz, duty=duty))

    # --- buses ------------------------------------------------------------------------
    def uart_open(self, tx: int, rx: int, baud: int) -> dict:
        self._gate(tx, rx)
        request = self._req("uart.open", tx=tx, rx=rx, baud=baud)
        port = request.result["port"]
        self._rx[port] = bytearray()
        self._uart_pins[port] = (tx, rx, baud)
        return self._result(request)

    def uart_close(self, port: int) -> dict:
        self._forget_ports([port])
        return self._result(self._req("uart.close", port=port))

    def uart_send(self, port: int, data: str) -> dict:
        return self._result(self._req("uart.write", port=port, data=_text_or_hex(data)))

    def _drain(self, port: int, timeout_ms: int) -> int:
        request = self._req("uart.read", port=port, max=UART_READ_MAX, timeout_ms=timeout_ms)
        data = _b64(request.result.get("data"))
        self._rx.setdefault(port, bytearray()).extend(data)
        return len(data)

    def uart_expect(self, port: int, pattern: str, timeout_ms: int) -> dict:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError("bad_request", f"invalid regex: {exc}") from None
        start_us = self.session.now_us()
        deadline = time.monotonic() + timeout_ms / 1000
        rig = getattr(self.session.transport, "rig", None)
        manual = rig is not None and not rig.clock.realtime
        waited_ms = 0
        while True:
            text = self._rx.setdefault(port, bytearray()).decode("utf-8", "replace")
            match = regex.search(text)
            if match:
                consumed = len(text[:match.end()].encode("utf-8"))
                del self._rx[port][:consumed]
                self.session.record("uart.match", "agent", port=port, pattern=pattern, match=match.group(0),
                                    elapsed_us=self.session.now_us() - start_us)
                return {"match": match.group(0), "groups": list(match.groups()), "port": port,
                        "elapsed_us": self.session.now_us() - start_us, "pass": True}
            remaining = timeout_ms - waited_ms if manual else int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise ToolError("timeout", f"pattern {pattern!r} not seen within {timeout_ms} ms on port {port}",
                                received=text[-200:])
            slice_ms = min(50, remaining)
            self._drain(port, slice_ms)
            waited_ms += slice_ms

    def spi_xfer(self, bus: int, cs: int, data: str) -> dict:
        request = self._req("spi.xfer", bus=bus, cs=cs, data=_bytes(data))
        return self._result(request, hex=_b64(request.result.get("data")).hex())

    def i2c_scan(self, bus: int) -> dict:
        request = self._req("i2c.scan", bus=bus)
        return self._result(request, hex=[f"0x{a:02x}" for a in request.result.get("addrs", [])])

    def i2c_read(self, bus: int, addr: int, n: int, reg: int | None = None) -> dict:
        args = {"bus": bus, "addr": addr, "n": n}
        if reg is not None:
            args["reg"] = reg
        request = self._req("i2c.read", **args)
        return self._result(request, hex=_b64(request.result.get("data")).hex())

    def i2c_write(self, bus: int, addr: int, data: str) -> dict:
        return self._result(self._req("i2c.write", bus=bus, addr=addr, data=_bytes(data)))

    # --- timing -------------------------------------------------------------------------
    def capture(self, pins: list[int], duration_ms: int, rate_hz: int | None = None) -> dict:
        rate = rate_hz or (self.session.manifest or {}).get("limits", {}).get("capture_hz", 1000)
        request = self._req("capture.start", pins=pins, rate_hz=rate, duration_ms=duration_ms)
        capture_id = request.result["capture_id"]
        done = self.session.wait_for_event(lambda e: e["kind"] == "capture.done" and e.get("capture_id") == capture_id,
                                           duration_ms + 1000)
        if done is None:
            raise ToolError("timeout", "capture did not complete")
        start = done.get("start_us", 0)
        edges = [[e[0] - start, e[1], e[2]] for e in done.get("edges", [])]
        self.last_capture = {"capture_id": capture_id, "pins": pins, "rate_hz": done.get("rate_hz_actual", rate),
                             "duration_ms": duration_ms, "initial": done.get("initial", {}), "edges": edges,
                             "cancelled": done.get("cancelled", False)}
        request.observation.update(observe="capture", edges=len(edges))
        return {"request": request.id, **self.last_capture}

    def wait_for(self, pin: int, edge: str, timeout_ms: int) -> dict:
        self._req("stream.set", pins=[pin], on=True)
        since = self.session._seq
        start_us = self.session.now_us()

        def matches(event):
            if event["kind"] != "pin.change" or event.get("pin") != pin or event["seq"] <= since:
                return False
            return edge == "any" or (edge == "rising") == bool(event["level"])

        hit = self.session.wait_for_event(matches, timeout_ms, since_seq=since)
        if hit is None:
            self.session.record("wait.timeout", "agent", pin=pin, edge=edge, timeout_ms=timeout_ms)
            raise ToolError("timeout", f"no {edge} edge on GPIO{pin} within {timeout_ms} ms", pin=pin)
        return {"pin": pin, "edge": edge, "level": hit["level"], "t_host_us": hit["t_host_us"],
                "t_bridge_us": hit.get("t_bridge_us"), "elapsed_us": hit["t_host_us"] - start_us, "pass": True}

    # --- assertions -----------------------------------------------------------------------
    def assert_pin(self, pin: int, level: int, name: str | None = None) -> dict:
        actual = self._req("pin.read", pin=pin).result["level"]
        event = self.session.assertion(name or f"GPIO{pin} == {level}", actual == level, level, actual, "agent",
                                       pin=pin)
        return {"pass": event["pass"], "expected": level, "actual": actual, "pin": pin}

    def assert_timeline(self, edges: list, tolerance_us: int, capture_id: int | None = None,
                        allow_extra: bool = False, name: str | None = None) -> dict:
        capture = self.last_capture
        if capture is None or (capture_id is not None and capture["capture_id"] != capture_id):
            raise ToolError("bad_request", "no matching capture; run capture first")
        remaining = [list(e) for e in capture["edges"]]
        matched, missing = [], []
        for expected in edges:
            if not (isinstance(expected, list) and len(expected) == 3):
                raise ToolError("bad_request", "edges must be [[t_us, pin, level], ...]")
            t, pin, level = expected
            best = None
            for candidate in remaining:
                close = candidate[1] == pin and candidate[2] == level and abs(candidate[0] - t) <= tolerance_us
                if close and (best is None or abs(candidate[0] - t) < abs(best[0] - t)):
                    best = candidate
            if best is None:
                missing.append(expected)
            else:
                matched.append({"expected": expected, "actual": best, "error_us": best[0] - t})
                remaining.remove(best)
        asserted_pins = {e[1] for e in edges}
        extra = [e for e in remaining if e[1] in asserted_pins]
        passed = not missing and (allow_extra or not extra)
        detail = {"matched": matched, "missing": missing, "extra": extra, "tolerance_us": tolerance_us}
        self.session.assertion(name or "timeline", passed, edges, capture["edges"], "agent", **detail)
        return {"pass": passed, **detail}

    def assert_frequency(self, pin: int, hz: float, tolerance_hz: float, name: str | None = None) -> dict:
        capture = self.last_capture
        if capture is None or pin not in capture["pins"]:
            raise ToolError("bad_request", f"no capture of GPIO{pin}; run capture first")
        rising = [e for e in capture["edges"] if e[1] == pin and e[2] == 1]
        measured = (len(rising) - 1) / ((rising[-1][0] - rising[0][0]) / 1e6) if len(rising) >= 2 else 0.0
        passed = abs(measured - hz) <= tolerance_hz
        self.session.assertion(name or f"GPIO{pin} ~ {hz} Hz", passed, hz, round(measured, 3), "agent", pin=pin,
                               tolerance_hz=tolerance_hz, rising_edges=len(rising))
        return {"pass": passed, "expected_hz": hz, "measured_hz": round(measured, 3), "rising_edges": len(rising)}

    def release_all(self) -> dict:
        self.last_capture = None
        self._rx.clear()
        self._uart_pins.clear()
        return self._result(self._req("release_all"))

    # --- human / misc -----------------------------------------------------------------------
    def request_wiring(self, pin: int, dut_pin: str, component: str, reason: str, dir: str) -> dict:
        entry = self.session.request_wiring(pin, dut_pin, component, reason, dir, "agent")
        return {"pending": True, **entry}

    def note(self, text: str) -> dict:
        self.session.note(text, "agent")
        return {"ok": True}

    def dut_action(self, behavior: str, action: str, ms: int | None = None) -> dict:
        rig = getattr(self.session.transport, "rig", None)
        if rig is None:
            raise ToolError("unsupported", "dut_action is only available on the Virtual Rig")
        if action == "reset":
            rig.reset_dut()
            self.session.record("dut.action", "tester", behavior="dut", action="reset")
            return {"ok": True}
        try:
            target = rig.behavior(behavior)
        except KeyError:
            raise ToolError("bad_request", f"unknown behavior {behavior!r}") from None
        fn = getattr(target, action, None)
        if fn is None or action.startswith("_"):
            raise ToolError("bad_request", f"{behavior} has no action {action!r}")
        with rig.lock:
            fn(ms) if ms is not None and action == "click" else fn()
        self.session.record("dut.action", "tester", behavior=behavior, action=action, ms=ms)
        return {"ok": True, "behavior": target.describe()}

    # --- ESP32 DUT (esp32_dut firmware) ------------------------------------------------------
    def program_esp32(self, firmware: str = fw.DEFAULT_PROJECT, build: bool = True,
                      manual_boot: bool = False) -> dict:
        pins = (ESP32_TX, ESP32_RX, ESP32_EN) + (() if manual_boot else (ESP32_BOOT,))
        self._gate_esp32(pins, f"flash {firmware} onto the ESP32")
        images, origin = self._firmware_images(firmware, build)
        self._req("flash.begin")
        for offset, data in images:
            self._stage(offset, data)
        run = self._flash_run(manual_boot)
        return self._result(run, build=origin, **self._await_boot())

    def led(self, state: str, hz: float = DEFAULT_BLINK_HZ) -> dict:
        line = f"LED BLINK {hz:g}" if state == "blink" else f"LED {state.upper()}"
        return self._esp32_command(line, LED_REPLY)

    def lcd_print(self, line1: str, line2: str = "") -> dict:
        return self._esp32_command(f"LCD {_lcd_text(line1)}|{_lcd_text(line2)}", LCD_REPLY)

    def _virtual_rig(self) -> bool:
        """In-process Virtual Rig or a ``--backend virtual`` bridge: flashing is simulated."""
        return (self.session.manifest or {}).get("bridge") == VIRTUAL_BRIDGE

    def _gate_esp32(self, pins: tuple[int, ...], reason: str) -> None:
        """Ask for every missing ESP32 wire at once, labelled, before the usual gate refuses."""
        if self._policy() != "off":
            for pin in pins:
                if pin not in self.session.wiring and pin not in self.session.pending_wiring:
                    dut_pin, direction = ESP32_WIRING[pin]
                    self.session.request_wiring(pin, dut_pin, "ESP32", reason, direction, "agent")
        self._gate(*pins)

    def _firmware_images(self, firmware: str, build: bool) -> tuple[list[tuple[int, bytes]], str]:
        """Images to flash and their origin: built now, cached (last build) or placeholder (Virtual Rig)."""
        try:
            fw.project_dir(firmware)
        except fw.FirmwareError as exc:
            raise ToolError("bad_request", str(exc)) from None
        virtual = self._virtual_rig()
        try:
            if build and not virtual:
                fw.build(firmware)
                return fw.images(firmware), "built"
            if fw.has_build(firmware):
                return fw.images(firmware), "cached"
        except fw.FirmwareError as exc:
            raise ToolError("build_failed", str(exc), firmware=firmware) from None
        if virtual:
            return fw.placeholder_images(), "placeholder"
        raise ToolError("bad_request", f"{firmware} has not been built; call program_esp32 with build=true")

    def _stage(self, offset: int, data: bytes) -> None:
        """flash.write one image in protocol-sized chunks; the bridge appends them per offset."""
        for start in range(0, len(data), FLASH_CHUNK):
            chunk = data[start:start + FLASH_CHUNK]
            staged = self._req("flash.write", offset=offset, data=chunk).result.get("size")
            if staged != start + len(chunk):
                raise ToolError("io", f"bridge holds {staged} bytes for 0x{offset:x}, expected "
                                      f"{start + len(chunk)}; nothing was flashed")

    def _flash_run(self, manual_boot: bool):
        """flash.run closes every UART port first (the flasher needs the serial device)."""
        try:
            run = self._req("flash.run", baud=FLASH_BAUD, manual_boot=manual_boot)
        except ToolError as exc:
            if exc.code in ("io", "timeout"):  # failed or lost mid-flash: the ports were already closed
                self._forget_ports(self._uart_pins.keys() | self._rx.keys())
            raise
        self._forget_ports(run.result.get("uart_closed") or [])
        return run

    def _await_boot(self) -> dict:
        """Reopen the ESP32 UART and wait for the boot banner; a miss is reported, not raised."""
        port = None
        try:
            port = self._esp32_uart()
            self.uart_expect(port, BOOT_BANNER, BOOT_TIMEOUT_MS)
        except ToolError as exc:
            return {"boot_ok": False, "port": port, "boot_error": f"{exc.code}: {exc.msg}"}
        return {"boot_ok": True, "port": port}

    def _esp32_uart(self) -> int:
        """Port id of the ESP32 UART0 link: an open port on GPIO14/15 at 115200, else a new one."""
        for port, (tx, rx, baud) in list(self._uart_pins.items()):
            if (tx, rx) == (ESP32_TX, ESP32_RX):
                if baud == ESP32_BAUD:
                    return port
                self.uart_close(port)
        self._gate_esp32((ESP32_TX, ESP32_RX), "talk to the esp32_dut firmware on UART0")
        return self.uart_open(ESP32_TX, ESP32_RX, ESP32_BAUD)["port"]

    def _esp32_command(self, line: str, reply: str) -> dict:
        """Send one command line to the firmware and return its reply line; ERR replies raise dut_error.
        A cached port the bridge closed meanwhile (control lost or re-taken) is reopened once."""
        port = self._esp32_uart()
        try:
            text = self._exchange(port, line, reply)
        except ToolError as exc:
            if port in self._uart_pins and exc.code != "bad_request":  # bad_request here: port not open
                raise
            self._forget_ports([port])
            port = self._esp32_uart()
            text = self._exchange(port, line, reply)
        if text.startswith("ERR"):
            raise ToolError("dut_error", text, port=port, command=line)
        return {"reply": text, "port": port, "command": line}

    def _exchange(self, port: int, line: str, reply: str) -> str:
        self._flush_rx(port)
        self._req("uart.write", port=port, data=(line + "\n").encode("ascii", "replace"))
        return self.uart_expect(port, reply, DUT_REPLY_TIMEOUT_MS)["match"].strip()

    def _flush_rx(self, port: int) -> None:
        """Drop input that arrived before a command (telemetry, old replies) so it cannot pass as the answer."""
        for _ in range(FLUSH_READS_MAX):
            if self._drain(port, 0) < UART_READ_MAX:
                break
        self._rx[port] = bytearray()


def _tcp_address(target: str | None, default_port: int) -> tuple[str, int]:
    """``host[:port]`` or ``[v6addr][:port]``; host defaults to 127.0.0.1 and port to the bridge's."""
    match = TCP_TARGET.fullmatch((target or "").strip())
    port = int(match["port"]) if match and match["port"] else default_port
    if match is None or not 0 < port < 65536:
        raise ToolError("bad_request", f"tcp target must be host:port (e.g. 192.168.1.20:{default_port}), "
                                       f"got {target!r}")
    return match["v6"] or match["host"] or DEFAULT_TCP_HOST, port


def default_transport_factory(kind: str, scenario: str | None = None, target: str | None = None,
                              **kwargs) -> Transport:
    if kind == "virtual":
        from .transport.virtual import VirtualTransport
        return VirtualTransport(scenario=scenario or "empty", **kwargs)
    if kind == "tcp":
        from .transport.tcp import DEFAULT_PORT, TcpTransport
        host, port = _tcp_address(target, DEFAULT_PORT)
        return TcpTransport(host, port, **kwargs)
    if kind == "ble":
        raise TransportError("BLE transport lands with the Pi bridge PR (US-15)")
    raise ToolError("bad_request", f"unknown transport {kind!r}")


__all__ = ["SPECS", "ProtocolError", "ToolError", "Tools", "default_transport_factory", "schemas",
           "validate_args"]
