"""Offline prompt interpreter (US-28): maps tester prompts to tool calls or a plan compile.

Returns ``{"kind": "tools", "calls": [...], "reply": str | callable}``, ``{"kind": "plan", "reply": str}``
or ``{"kind": "reply", "reply": str}``. Anything that reads like a test ("within", "verify", "confirm",
"expect", "measure") becomes a plan so the tester can review it before it runs; text meant for the
ESP32's LCD is payload and never counts. ESP32 prompts map to ``program_esp32``, ``led`` and ``lcd_print``.
"""
from __future__ import annotations

import re

LEVEL = {"high": 1, "low": 0, "1": 1, "0": 0, "on": 1, "off": 0}
TEST_WORDS = re.compile(r"\b(within|verify|confirm|expect|measure|should|assert|follows|mirrors)\b", re.IGNORECASE)
# ESP32 DUT (esp32_dut firmware): LCD text, LED blink/on/off, programming the board.
LCD = re.compile(r"\b(?:print|show|write|display|put)\s+(?!me\b)(?P<text>.+?)\s+(?:on|to)\s+(?:the\s+)?"
                 r"(?:lcd|display|screen)\b", re.IGNORECASE)
LCD_LINE_BREAK = re.compile(r"\s*\|\s*|\s+/\s+")
PROGRAM = re.compile(r"\b(?:program|flash|reflash)\b.*\b(?:esp32|dut|firmware)\b")
MANUAL_BOOT = re.compile(r"\bmanual(?:ly)?\s+boot\b|\bboot button\b|\bhold(?:ing)?\b.*\bboot\b")
SKIP_BUILD = re.compile(r"\b(?:no|skip|without)\s+(?:the\s+|a\s+)?(?:re)?build")
QUESTION = re.compile(r"^(?:is|are|does|do|did|was|what|why|how|when|check)\b|\?\s*$")
HZ = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hz|hertz)\b")


def _pin(text: str) -> int | None:
    m = re.search(r"gpio\s*(\d+)|pin\s*(\d+)", text, re.IGNORECASE)
    return int(m.group(1) or m.group(2)) if m else None


def _dut_reply(what: str):
    return lambda r: (f"ESP32 {what}: {r[0]['result']['reply']}." if r[0]["ok"]
                      else f"ESP32 {what} command failed: {r[0]['error']['msg']}")


def _program_reply(r: list[dict]) -> str:
    if not r[0]["ok"]:
        return f"Programming the ESP32 failed: {r[0]['error']['msg']}"
    result = r[0]["result"]
    booted = "it printed BOOT OK" if result.get("boot_ok") else "no BOOT OK seen yet"
    return f"Programmed the ESP32 ({result.get('build')} images) in {result.get('seconds')} s; {booted}."


def _lcd(text: str) -> dict | None:
    """'print/show/write <text> on the lcd'; a second line follows '|' or ' / '. Test words in the text
    itself are payload, so only the rest of the prompt can turn it into a plan."""
    m = LCD.search(text)
    if m is None or TEST_WORDS.search(text[:m.start("text")] + text[m.end("text"):]):
        return None
    lines = [part.strip().strip("'\"") for part in LCD_LINE_BREAK.split(m.group("text"), maxsplit=1)]
    args = {"line1": lines[0], **({"line2": lines[1]} if len(lines) > 1 else {})}
    return {"kind": "tools", "calls": [{"tool": "lcd_print", "args": args}], "reply": _dut_reply("LCD")}


def _program(low: str) -> dict | None:
    if not PROGRAM.search(low) or QUESTION.search(low):
        return None
    args = {**({"manual_boot": True} if MANUAL_BOOT.search(low) else {}),
            **({"build": False} if SKIP_BUILD.search(low) else {})}
    return {"kind": "tools", "calls": [{"tool": "program_esp32", "args": args}], "reply": _program_reply}


def _led(low: str) -> dict | None:
    """'blink the led [at N hz]', 'turn the led on/off'; a GPIO number means a rig pin, not the ESP32 LED."""
    if not re.search(r"\bleds?\b", low) or QUESTION.search(low) or _pin(low) is not None:
        return None
    if "blink" in low:
        m = HZ.search(low)
        args = {"state": "blink", **({"hz": float(m.group(1))} if m else {})}
    else:
        m = re.search(r"\b(on|off)\b", low)
        if m is None:
            return None
        args = {"state": m.group(1)}
    return {"kind": "tools", "calls": [{"tool": "led", "args": args}], "reply": _dut_reply("LED")}


def interpret(text: str, state: dict | None = None) -> dict:
    t = text.strip()
    low = t.lower()
    behaviors = {b.get("name"): b for b in (state or {}).get("behaviors", [])}
    lcd = _lcd(t)
    if lcd:
        return lcd
    if TEST_WORDS.search(low) and not low.startswith(("read", "what", "show")):
        return {"kind": "plan", "reply": "Compiled your test into a plan."}
    program = _program(low)
    if program:
        return program
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
    led = _led(low)
    if led:
        return led
    if re.search(r"\b(list|which) pins\b|\bpin status\b", low):
        return {"kind": "tools", "calls": [{"tool": "list_pins", "args": {}}],
                "reply": lambda r: "Listed pins." if r[0]["ok"] else r[0]["error"]["msg"]}
    if re.search(r"\brun (?:the )?plan\b", low):
        return {"kind": "reply", "reply": "Pick a plan in the Plans panel and press Run."}
    return {"kind": "reply", "reply": "I can set/pulse/pwm/read/capture pins, press buttons, send UART, "
                                      "program the ESP32, blink its LED, print on its LCD, "
                                      "or compile a test like 'confirm GPIO17 goes high within 50 ms of pressing "
                                      "the button on GPIO22'."}
