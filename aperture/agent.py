"""OpenAI-driven test agent that reaches the hardware only through the Aperture MCP server.

    agent = RigAgent(mcp_target)            # mcp Server (in-process), "http://host:8765/mcp", or StdioServerParameters
    result = agent.run("confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22")

The agent lists the MCP tools, hands them to the OpenAI Chat Completions API as
function tools, and executes every tool call the model makes via MCP. The
successful hardware calls become a deterministic plan (assertions marked) that
can be saved through the MCP ``save_plan`` tool and re-run without the agent.

Configuration: ``OPENAI_API_KEY`` (required), ``OPENAI_MODEL`` (default
``gpt-4o-mini``), ``OPENAI_BASE_URL`` (optional, any compatible endpoint).
"""
from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "gpt-4o-mini"
ASSERT_TOOLS = {"assert_pin", "assert_timeline", "assert_frequency", "uart_expect", "wait_for"}
# tools that never belong in a re-runnable plan (session plumbing, not test steps)
PLAN_SKIP = {"connect", "disconnect", "take_control", "release_control", "list_pins", "note", "request_wiring",
             "save_plan", "run_plan", "list_plans", "session_log", "release_all"}
DRIVE_TOOLS = {"write", "pulse", "pwm"}

SYSTEM_PROMPT = """You are the Aperture OS test agent. You exercise a device under test (DUT, usually an ESP32) \
wired to a Raspberry Pi bridge by calling hardware tools. Pins are BCM numbers on the 40-pin header.

Rules:
- If the rig is not connected, call connect (transport "virtual" when no bridge is named) before anything else.
- Configure a pin with set_mode before driving or reading it. Inputs to the DUT are outputs of the rig.
- Prefer deterministic checks: assert_pin, assert_timeline (with a tolerance), assert_frequency, uart_expect, \
wait_for. Every requirement in the test must map to at least one assertion.
- Never guess results; read them from tool output. If a tool errors, explain why and adapt or stop.
- Finish with release_all, then reply with PASS or FAIL on the first line and one line per assertion."""


@dataclass
class ToolCall:
    tool: str
    args: dict
    ok: bool
    result: Any

    def as_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args, "ok": self.ok, "result": self.result}


@dataclass
class AgentResult:
    reply: str
    calls: list[ToolCall] = field(default_factory=list)
    plan: dict | None = None
    saved: dict | None = None
    verdict: bool | None = None

    def as_dict(self) -> dict:
        return {"reply": self.reply, "calls": [c.as_dict() for c in self.calls], "plan": self.plan,
                "saved": self.saved, "verdict": self.verdict}


def openai_available() -> bool:
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("OPENAI_API_KEY"))


def mcp_tools_to_openai(tools) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description or "",
                          "parameters": t.input_schema or {"type": "object", "properties": {}}}}
            for t in tools]


def plan_from_calls(name: str, source: str, calls: list[ToolCall]) -> dict | None:
    """Turn the successful tool calls of a run into a deterministic plan (validated by the MCP server)."""
    steps, wiring = [], {}
    for call in calls:
        if not call.ok or call.tool in PLAN_SKIP:
            continue
        step = {"tool": call.tool, "args": call.args}
        if call.tool in ASSERT_TOOLS:
            step["assert"] = True
        steps.append(step)
        pin = call.args.get("pin")
        if isinstance(pin, int):
            mode = call.args.get("mode", "")
            direction = "out" if call.tool in DRIVE_TOOLS or mode in ("output", "pwm") else "in"
            if call.tool == "set_mode" or pin not in wiring:
                wiring[pin] = direction
    if not any(s.get("assert") for s in steps):
        return None
    return {"name": name, "source": source, "steps": steps,
            "requires": {"wiring": [{"pin": p, "dir": d} for p, d in sorted(wiring.items())]},
            "cleanup": [{"tool": "release_all", "args": {}}]}


def _verdict(reply: str) -> bool | None:
    head = reply.strip().split("\n", 1)[0].upper()
    if "FAIL" in head:
        return False
    if "PASS" in head:
        return True
    return None


def _text(result) -> str:
    parts = [getattr(c, "text", "") for c in (result.content or [])]
    return "\n".join(p for p in parts if p) or json.dumps(result.structured_content or {})


class RigAgent:
    def __init__(self, mcp_target, llm=None, model: str | None = None, max_steps: int = 40,
                 on_step: Callable[[dict], None] | None = None, system_prompt: str = SYSTEM_PROMPT):
        self.mcp_target = mcp_target
        self._llm = llm
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self.max_steps = max_steps
        self.on_step = on_step or (lambda _: None)
        self.system_prompt = system_prompt

    @property
    def llm(self):
        if self._llm is None:
            from openai import AsyncOpenAI  # reads OPENAI_API_KEY / OPENAI_BASE_URL
            self._llm = AsyncOpenAI()
        return self._llm

    async def _complete(self, messages, tools):
        reply = self.llm.chat.completions.create(model=self.model, messages=messages, tools=tools)
        return await reply if inspect.isawaitable(reply) else reply

    async def arun(self, prompt: str, save_as: str | None = None, context: str | None = None) -> AgentResult:
        from mcp import Client
        async with Client(self.mcp_target) as client:
            listed = await client.list_tools()
            tools = mcp_tools_to_openai(listed.tools)
            messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
            if context:
                messages.append({"role": "system", "content": context})
            messages.append({"role": "user", "content": prompt})
            calls: list[ToolCall] = []
            reply = ""
            for _ in range(self.max_steps):
                completion = await self._complete(messages, tools)
                message = completion.choices[0].message
                tool_calls = getattr(message, "tool_calls", None) or []
                messages.append({"role": "assistant", "content": message.content or "",
                                 **({"tool_calls": [{"id": tc.id, "type": "function",
                                                     "function": {"name": tc.function.name,
                                                                  "arguments": tc.function.arguments or "{}"}}
                                                    for tc in tool_calls]} if tool_calls else {})})
                if not tool_calls:
                    reply = message.content or ""
                    break
                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        args, out, ok, payload = {}, json.dumps({"error": f"bad JSON arguments: {exc}"}), False, None
                    else:
                        result = await client.call_tool(name, args)
                        ok, out = not result.is_error, _text(result)
                        payload = result.structured_content
                    calls.append(ToolCall(name, args, ok, payload))
                    self.on_step({"tool": name, "args": args, "ok": ok})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out[:20_000]})
            else:
                reply = f"FAIL\nstopped after {self.max_steps} model turns without a conclusion"
            result = AgentResult(reply, calls, verdict=_verdict(reply))
            result.plan = plan_from_calls(save_as or prompt[:60], prompt, calls)
            if save_as and result.plan:
                saved = await client.call_tool("save_plan", result.plan)
                result.saved = saved.structured_content if not saved.is_error else {"error": _text(saved)}
            return result

    def run(self, prompt: str, save_as: str | None = None, context: str | None = None) -> AgentResult:
        import anyio
        return anyio.run(lambda: self.arun(prompt, save_as, context))
