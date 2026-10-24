"""Rule-based natural-language -> plan compiler (works with no LLM).

Understands the clause patterns listed in EPIC-05 / US-40. Unresolved references
("the button" with no known pin) are reported in ``plan["needs"]`` instead of guessed.
"""
from __future__ import annotations

import re

from .schema import slugify, validate_plan

DEFAULT_UART = {"tx": 14, "rx": 15, "baud": 115200}
LEVEL = {"high": 1, "low": 0, "1": 1, "0": 0, "on": 1, "off": 0}


class _Builder:
    def __init__(self, context: dict | None):
        context = context or {}
        self.names = {k.lower(): v for k, v in context.get("names", {}).items()}
        self.wiring = {int(k): v for k, v in context.get("wiring", {}).items()}
        for pin, w in self.wiring.items():
            for key in ("component", "dut_pin"):
                if w.get(key):
                    self.names.setdefault(str(w[key]).lower(), pin)
        self.uart = context.get("uart") or DEFAULT_UART
        self.steps: list[dict] = []
        self.modes: dict[int, str] = {}
        self.port_opened = False
        self.needs: list[str] = []
        self.pins: dict[int, str] = {}

    def add(self, tool: str, assert_: bool = False, name: str | None = None, **args) -> None:
        step = {"id": f"s{len(self.steps) + 1}", "tool": tool, "args": args}
        if assert_:
            step["assert"] = True
            step["on_fail"] = "continue"
        if name:
            step["name"] = name
        self.steps.append(step)

    def mode(self, pin: int, mode: str, initial: int | None = None) -> None:
        self.pins[pin] = "out" if mode in ("output", "pwm") else "in"
        if self.modes.get(pin) == mode:
            return
        self.modes[pin] = mode
        args = {"pin": pin, "mode": mode}
        if initial is not None:
            args["initial"] = initial
        self.add("set_mode", **args)

    def uart_port(self) -> None:
        if not self.port_opened:
            self.add("uart_open", tx=self.uart["tx"], rx=self.uart["rx"], baud=self.uart["baud"])
            self.pins[self.uart["tx"]] = "out"
            self.pins[self.uart["rx"]] = "in"
            self.port_opened = True

    def resolve(self, text: str) -> int | None:
        match = re.search(r"gpio\s*(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        for key, pin in self.names.items():
            if key and key in text.lower():
                return int(pin)
        self.needs.append(text.strip())
        return None


PATTERNS: list[tuple[str, str]] = [
    (r"gpio\s*(?P<led>\d+) goes (?P<level>high|low) within (?P<ms>\d+)\s*ms of pressing (?P<btn>.+?)$",
     "button_edge"),
    (r"(?:verify|check|confirm|expect)(?: that)? .*?(?:prints?|outputs?|says?|logs?) ['\"]?(?P<text>[^'\"]+?)['\"]? "
     + r"(?:over|on) (?:the )?(?:uart|serial)", "uart"),
    (r"measure (?:the )?frequency on gpio\s*(?P<pin>\d+) (?:is|should be|=) (?P<hz>[\d.]+)\s*hz"
     + r"(?:\s*(?:±|\+/-|within)\s*(?P<tol>[\d.]+))?", "frequency"),
    (r"(?:set|drive) gpio\s*(?P<pin>\d+) (?P<level>high|low|1|0|on|off)", "set"),
    (r"pulse gpio\s*(?P<pin>\d+)(?: (?P<level>high|low))? for (?P<n>\d+)\s*(?P<unit>ms|us)", "pulse"),
    (r"(?:expect|confirm|check|verify|assert)(?: that)? gpio\s*(?P<pin>\d+) (?:to be|is|reads|stays|should be) "
     + r"(?P<level>high|low|1|0)", "assert_level"),
    (r"gpio\s*(?P<a>\d+) (?:follows|mirrors|tracks) gpio\s*(?P<b>\d+)", "follow"),
    (r"press (?P<btn>.+?)$", "press"),
    (r"reset the (?:dut|board|device)", "reset"),
]


def compile_plan(text: str, context: dict | None = None, name: str | None = None) -> dict:
    b = _Builder(context)
    clauses = [c.strip().rstrip(".") for c in re.split(r";|\n|\band then\b|\bthen\b", text) if c.strip()]
    unmatched = []
    for clause in clauses:
        for pattern, kind in PATTERNS:
            m = re.search(pattern, clause, re.IGNORECASE)
            if m:
                getattr(_Rules, kind)(b, m)
                break
        else:
            unmatched.append(clause)
    plan = {"schema": 1, "name": name or (text.strip()[:60] or "untitled"), "source": text.strip(),
            "requires": {"wiring": [{"pin": pin, "dir": d, "dut": (b.wiring.get(pin) or {}).get("dut_pin", "?")}
                                    for pin, d in sorted(b.pins.items())],
                         "uart": [b.uart] if b.port_opened else []},
            "steps": b.steps or [{"id": "s1", "tool": "note", "args": {"text": "nothing to do"}}],
            "cleanup": [{"id": "c1", "tool": "release_all", "args": {}}]}
    plan["slug"] = slugify(plan["name"])
    needs = b.needs + [f"could not understand: {c}" for c in unmatched]
    if needs:
        plan["needs"] = needs
    return validate_plan(plan)


class _Rules:
    @staticmethod
    def button_edge(b: _Builder, m):
        led, level, ms = int(m["led"]), LEVEL[m["level"].lower()], int(m["ms"])
        btn = b.resolve(m["btn"])
        b.mode(led, "input")
        if btn is None:
            return
        b.mode(btn, "output", initial=1)
        b.add("write", pin=btn, level=0)
        b.add("wait_for", True, f"GPIO{led} {'rising' if level else 'falling'} within {ms} ms",
              pin=led, edge="rising" if level else "falling", timeout_ms=ms)
        b.add("write", pin=btn, level=1)

    @staticmethod
    def uart(b: _Builder, m):
        b.uart_port()
        text = m["text"].strip()
        b.add("uart_expect", True, f"UART prints {text!r}", port=0, pattern=re.escape(text), timeout_ms=2000)

    @staticmethod
    def frequency(b: _Builder, m):
        pin, hz = int(m["pin"]), float(m["hz"])
        tol = float(m["tol"]) if m["tol"] else max(0.05 * hz, 0.1)
        b.mode(pin, "input")
        b.add("capture", pins=[pin], duration_ms=1000)
        b.add("assert_frequency", True, f"GPIO{pin} ~ {hz} Hz", pin=pin, hz=hz, tolerance_hz=tol)

    @staticmethod
    def set(b: _Builder, m):
        pin, level = int(m["pin"]), LEVEL[m["level"].lower()]
        b.mode(pin, "output", initial=level if pin not in b.modes else None)
        b.add("write", pin=pin, level=level)

    @staticmethod
    def pulse(b: _Builder, m):
        pin = int(m["pin"])
        level = LEVEL[(m["level"] or "high").lower()]
        us = int(m["n"]) * (1000 if m["unit"].lower() == "ms" else 1)
        b.mode(pin, "output", initial=1 - level)
        b.add("pulse", pin=pin, level=level, us=us)

    @staticmethod
    def assert_level(b: _Builder, m):
        pin, level = int(m["pin"]), LEVEL[m["level"].lower()]
        if b.modes.get(pin) not in ("output", "pwm"):
            b.mode(pin, "input")
        b.add("assert_pin", True, f"GPIO{pin} == {level}", pin=pin, level=level)

    @staticmethod
    def follow(b: _Builder, m):
        a, src = int(m["a"]), int(m["b"])
        b.mode(a, "input")
        b.mode(src, "output", initial=0)
        for level in (1, 0):
            b.add("write", pin=src, level=level)
            b.add("assert_pin", True, f"GPIO{a} follows GPIO{src} -> {level}", pin=a, level=level)

    @staticmethod
    def press(b: _Builder, m):
        btn = b.resolve(m["btn"])
        if btn is None:
            return
        b.mode(btn, "output", initial=1)
        b.add("pulse", pin=btn, level=0, us=100_000)

    @staticmethod
    def reset(b: _Builder, m):
        b.add("dut_action", behavior="dut", action="reset")
