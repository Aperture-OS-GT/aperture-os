"""OpenAI function-calling agent for the tester console.

``run_agent`` sends one tester prompt, a compact picture of the bench and the current
connection to a chat-completions model, runs each tool call through ``tools.call`` (the
same validated entry point the console and plans use) and feeds every result back until
the model answers in text. Tool schemas come from ``aperture.tools.schemas()``, so new
tools need no change here. The OpenAI SDK is imported only when no client is injected.
"""
from __future__ import annotations

import json
import os

from .protocol import _jsonable
from .tools import ToolError, schemas
from .transport.base import TransportError

DEFAULT_MODEL = "gpt-5.1"         # fastest model that passed every ESP32 LED / flash prompt in a live check
MAX_TURNS = 12
MAX_RESULT_CHARS = 6000          # per tool result; list_pins on a full header is ~4.3 KB
OPENAI_TIMEOUT_S = 60.0
OPENAI_MAX_RETRIES = 1
UNTYPED_ITEMS = {"type": "integer"}  # e.g. assert_timeline's [t_us, pin, level] edges
NO_ANSWER = "The model returned no answer."

SYSTEM_PROMPT = (
    "You are the Aperture OS bench agent for a Raspberry Pi hardware-in-the-loop rig. The Pi "
    "bridge owns the 40-pin header (tools take BCM GPIO numbers) and an ESP32 DUT is wired to it.\n"
    "Wiring: Pi GPIO17 -> ESP32 D4 trigger, GPIO18 -> D5 PWM 100 Hz, GPIO27 -> D18 fault; ESP32 "
    "D19 -> Pi GPIO22 actuator, D21 -> GPIO23 safety relay. UART0 115200 8N1 (Pi GPIO14 TXD -> "
    "RX0, TX0 -> GPIO15 RXD) carries flashing, commands and telemetry. GPIO25 -> EN resets the "
    "ESP32; GPIO24 -> BOOT/GPIO0 selects the bootloader but may be unwired: then flash with "
    "manual_boot=true while the human holds the BOOT button.\n"
    "ESP32 firmware (esp32_dut): on boot the 16x2 LCD shows 'Aperture OS' / 'rig-01 online', "
    "UART0 prints 'BOOT OK' and the status LED (ESP32 GPIO13) blinks at 2 Hz. Commands, one per "
    "line: 'LCD <line1>|<line2>', 'LED BLINK <hz>' (0 < hz <= 50), 'LED ON', 'LED OFF'; replies "
    "start with OK or ERR. It also runs the safety controller (trigger, PWM and fault drive the "
    "actuator and safety relay) and prints a JSON telemetry line every 200 ms.\n"
    "PIN NUMBERS: every pin argument of a tool is a Raspberry Pi BCM GPIO number. ESP32 pin names "
    "(D4, D13, 'GPIO13 on the ESP32', the LCD pins) are NOT Pi pins and must never be passed to "
    "set_mode, write, read, pulse, pwm, wait_for or request_wiring. The only wired Pi pins are GPIO17, "
    "18, 27 (Pi drives ESP32 D4, D5, D18), GPIO22, 23 (Pi reads ESP32 D19, D21), GPIO14/15 (UART0 to "
    "the ESP32), GPIO25 (EN) and GPIO24 (BOOT); never request wiring for any other Pi pin.\n"
    "THE STATUS LED AND THE LCD ARE ON THE ESP32, not on the Pi header: the Pi cannot drive them with "
    "pin tools. Control them only with led (blink / on / off over UART) and lcd_print. The status LED "
    "is a single-colour LED; its colour cannot be changed, so say so if asked for a colour and still "
    "blink it. To program, flash or code the ESP32 (e.g. 'program the ESP32 to blink the LED'), call "
    "program_esp32 with manual_boot=true (this bench has no BOOT wire), telling the human beforehand "
    "to press and hold the ESP32 BOOT button for a few seconds when flashing starts; then call led.\n"
    "Rules:\n"
    "- Act only through the tools and never invent measurements; report only what tool results "
    "show.\n"
    "- If a tool returns wiring_required, stop and ask the human to confirm those wires (the "
    "confirm buttons in the session log, or 'Confirm bench wiring' in the DUT panel) and then send "
    "the request again; never work around it.\n"
    "- To make the LED blink call led; if the DUT firmware may not be loaded, or the user asks to "
    "program, flash or code the ESP32, call program_esp32 first. To show text call lcd_print.\n"
    "- For tests, drive the inputs, check with assertions (assert_pin, assert_frequency, "
    "assert_timeline, uart_expect) and report each step as PASS or FAIL.\n"
    "- On the Virtual Rig (transport kind virtual) everything is simulated; say so.\n"
    "- Reply in 1-3 short sentences."
)


def run_agent(tools, text: str, context: dict, *, client=None, model: str | None = None,
              max_turns: int = MAX_TURNS) -> dict:
    """Answer one prompt: ``{"reply": str, "results": [{tool, ok, result | error}, ...]}``.

    ``tools`` is a tool backend (``schemas()`` plus ``call(name, args)``, e.g. an MCP client) or
    the console's in-process ``Tools`` (``call(name, **args)``), which is adapted with
    ``LocalTools``. A model API error propagates while no
    tool has run, so the console can fall back to its offline interpreter; after a tool has
    run the agent stops and reports instead, so a fallback never repeats hardware actions.
    """
    if client is None:
        with _default_client() as owned:
            return run_agent(tools, text, context, client=owned, model=model, max_turns=max_turns)
    model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    backend = as_backend(tools)
    specs = _openai_tools(backend.schemas())
    messages = [{"role": "system", "content": system_prompt(context)},
                {"role": "user", "content": text}]
    results: list[dict] = []
    for _ in range(max_turns):
        try:
            completion = client.chat.completions.create(model=model, messages=messages, tools=specs)
        except Exception as exc:
            if not results:
                raise
            return {"reply": f"Stopped after {len(results)} tool call(s): the model API failed "
                             f"({type(exc).__name__}).", "results": results}
        message = completion.choices[0].message
        calls = list(message.tool_calls or [])
        messages.append(_assistant_message(message, calls))
        if not calls:
            return {"reply": (message.content or "").strip() or NO_ANSWER, "results": results}
        for call in calls:
            entry = _execute(backend, call)
            results.append(entry)
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(entry, separators=(",", ":"))})
    return {"reply": f"Stopped after {max_turns} model turns without a final answer.",
            "results": results}


def system_prompt(context: dict) -> str:
    snapshot = json.dumps(_jsonable(context), default=repr, separators=(",", ":"))
    return f"{SYSTEM_PROMPT}\nCurrent context: {snapshot}"


def _default_client():
    from openai import OpenAI  # optional dependency: pip install -e '.[llm]'
    return OpenAI(timeout=OPENAI_TIMEOUT_S, max_retries=OPENAI_MAX_RETRIES)


class LocalTools:
    """The agent's tool seam over the in-process ``Tools``: ``schemas()`` and ``call(name, args)``.
    An MCP-backed implementation of the same two methods can replace it without touching the
    agent loop."""

    def __init__(self, tools):
        self.tools = tools

    def schemas(self) -> list[dict]:
        return schemas()

    def call(self, name: str, args: dict) -> dict:
        return self.tools.call(name, **args)


def as_backend(tools):
    return tools if callable(getattr(tools, "schemas", None)) else LocalTools(tools)


def _openai_tools(specs: list[dict]) -> list[dict]:
    """Tool schemas in a form the API accepts: it rejects an array schema without ``items``."""
    return [_with_items(spec) for spec in specs]


def _with_items(node):
    if isinstance(node, list):
        return [_with_items(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _with_items(value) for key, value in node.items()}
    if out.get("type") == "array" and "items" not in out:
        return {**out, "items": dict(UNTYPED_ITEMS)}
    return out


def _assistant_message(message, calls: list) -> dict:
    """The assistant turn as a plain dict, for SDK objects and test doubles alike."""
    out = {"role": "assistant", "content": message.content}
    if not calls:
        return out
    return {**out, "tool_calls": [{"id": call.id, "type": "function",
                                   "function": {"name": call.function.name,
                                                "arguments": call.function.arguments}}
                                  for call in calls]}


def _execute(backend, call) -> dict:
    """Run one tool call; every failure becomes an error entry the model can read."""
    name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except ValueError as exc:
        return _failure(name, "bad_request", f"arguments are not valid JSON ({exc})")
    if not isinstance(args, dict):
        return _failure(name, "bad_request", "arguments must be a JSON object")
    try:
        return {"tool": name, "ok": True, "result": _json_safe(backend.call(name, args))}
    except ToolError as exc:
        return {"tool": name, "ok": False,
                "error": _json_safe({"code": exc.code, "msg": exc.msg, **exc.extra})}
    except TransportError as exc:
        return _failure(name, "io", str(exc))
    except ValueError as exc:
        return _failure(name, "bad_request", str(exc))


def _failure(name: str, code: str, msg: str) -> dict:
    return {"tool": name, "ok": False, "error": {"code": code, "msg": msg}}


def _json_safe(value):
    """A JSON-safe copy (bytes become base64, other unknown types their repr), or a truncated
    preview when it would exceed MAX_RESULT_CHARS."""
    text = json.dumps(_jsonable(value), default=repr)
    if len(text) <= MAX_RESULT_CHARS:
        return json.loads(text)
    return {"truncated": True, "chars": len(text), "preview": text[:MAX_RESULT_CHARS]}
