"""Offline prompt interpreter (US-28): maps tester prompts to tool calls or a plan compile.

Returns ``{"kind": "tools", "calls": [...], "reply": str | callable}``, ``{"kind": "plan", "reply": str}``
or ``{"kind": "reply", "reply": str}``. Anything that reads like a test ("within", "verify", "confirm",
"expect", "measure") becomes a plan so the tester can review it before it runs.
"""
from __future__ import annotations

import re

LEVEL = {"high": 1, "low": 0, "1": 1, "0": 0, "on": 1, "off": 0}
TEST_WORDS = re.compile(r"\b(within|verify|confirm|expect|measure|should|assert|follows|mirrors)\b", re.IGNORECASE)


def _pin(text: str) -> int | None:
    m = re.search(r"gpio\s*(\d+)|pin\s*(\d+)", text, re.IGNORECASE)
    return int(m.group(1) or m.group(2)) if m else None


def interpret(text: str, state: dict | None = None) -> dict:
    t = text.strip()
    low = t.lower()
    behaviors = {b.get("name"): b for b in (state or {}).get("behaviors", [])}
    if TEST_WORDS.search(low) and not low.startswith(("read", "what", "show")):
        return {"kind": "plan", "reply": "Compiled your test into a plan."}
    if re.search(r"\b(reset|reboot|power.?cycle)\b", low):
        return {"kind": "tools", "calls": [{"tool": "dut_action", "args": {"behavior": "dut", "action": "reset"}}],
                "reply": "Reset the DUT."}
    m = re.search(r"\b(press|release|click)\b(?: the)? (\w+)", low)
    if m:
        action, name = m.group(1), m.group(2)
        target = next((b for b in behaviors.values() if b.get("type") == "Button" and
                       (name in (b.get("name") or "") or name == "button")), None)
        if target is None:
            return {"kind": "reply", "reply": f"I don't know a button called {name!r} on this rig."}
        return {"kind": "tools", "calls": [{"tool": "dut_action", "args": {"behavior": target["name"], "action": action}}],
                "reply": f"{action.title()}ed {target['name']}."}
    m = re.search(r"\b(set|drive|write)\b.*?(gpio\s*\d+|pin\s*\d+).*?\b(high|low|1|0|on|off)\b", low)
    if m:
        pin, level = _pin(m.group(2)), LEVEL[m.group(3)]
        return {"kind": "tools", "calls": [{"tool": "set_mode", "args": {"pin": pin, "mode": "output", "initial": level}},
                                           {"tool": "write", "args": {"pin": pin, "level": level}}],
                "reply": lambda r: f"GPIO{pin} driven to {level}; readback {r[-1]['result'].get('level', '?')}." if r[-1]["ok"]
                else f"Could not drive GPIO{pin}: {r[-1]['error']['msg']}"}
    m = re.search(r"\bpulse\b.*?(gpio\s*\d+|pin\s*\d+)(?:.*?(\d+)\s*(ms|us))?", low)
    if m:
        pin = _pin(m.group(1))
        us = int(m.group(2)) * (1000 if m.group(3) == "ms" else 1) if m.group(2) else 100_000
        return {"kind": "tools", "calls": [{"tool": "set_mode", "args": {"pin": pin, "mode": "output", "initial": 0}},
                                           {"tool": "pulse", "args": {"pin": pin, "level": 1, "us": us}}],
                "reply": f"Pulsed GPIO{pin} high for {us} µs."}
    m = re.search(r"\bpwm\b.*?(gpio\s*\d+|pin\s*\d+).*?(\d+(?:\.\d+)?)\s*hz.*?(\d+)\s*%", low)
    if m:
        pin, hz, duty = _pin(m.group(1)), float(m.group(2)), int(m.group(3)) / 100
        return {"kind": "tools", "calls": [{"tool": "set_mode", "args": {"pin": pin, "mode": "pwm"}},
                                           {"tool": "pwm", "args": {"pin": pin, "freq_hz": hz, "duty": duty}}],
                "reply": f"PWM {hz:g} Hz at {duty:.0%} on GPIO{pin}."}
    m = re.search(r"\b(read|what is|show)\b.*?(gpio\s*\d+|pin\s*\d+)", low)
    if m:
        pin = _pin(m.group(2))
        return {"kind": "tools", "calls": [{"tool": "read", "args": {"pin": pin}}],
                "reply": lambda r: f"GPIO{pin} reads {r[0]['result']['level']}." if r[0]["ok"]
                else f"Cannot read GPIO{pin}: {r[0]['error']['msg']} (set a mode first)."}
    m = re.search(r"\bcapture\b.*?(gpio\s*\d+|pin\s*\d+)(?:.*?(\d+)\s*ms)?", low)
    if m:
        pin, ms = _pin(m.group(1)), int(m.group(2)) if m.group(2) else 200
        return {"kind": "tools", "calls": [{"tool": "capture", "args": {"pins": [pin], "duration_ms": ms}}],
                "reply": lambda r: f"Captured {len(r[0]['result']['edges'])} edges on GPIO{pin} in {ms} ms." if r[0]["ok"]
                else f"Capture failed: {r[0]['error']['msg']}"}
    m = re.search(r"\bsend\b\s+['\"](.+?)['\"].*?(?:uart|serial)", t, re.IGNORECASE)
    if m:
        return {"kind": "tools", "calls": [{"tool": "uart_send", "args": {"port": 0, "data": m.group(1) + "\\n"}}],
                "reply": f"Sent {m.group(1)!r} over UART port 0."}
    if re.search(r"\b(list|which) pins\b|\bpin status\b", low):
        return {"kind": "tools", "calls": [{"tool": "list_pins", "args": {}}],
                "reply": lambda r: "Listed pins." if r[0]["ok"] else r[0]["error"]["msg"]}
    if re.search(r"\brun (?:the )?plan\b", low):
        return {"kind": "reply", "reply": "Pick a plan in the Plans panel and press Run."}
    return {"kind": "reply", "reply": "I can set/pulse/pwm/read/capture pins, press buttons, send UART, "
                                      "or compile a test like 'confirm GPIO17 goes high within 50 ms of pressing "
                                      "the button on GPIO22'."}
