"""MCP server exposing the Aperture rig tool set (the agent's only path to hardware).

Every tool in ``aperture.tools.SPECS`` (connect, set_mode, write, uart_expect,
capture, assert_timeline, ...) is published with the same JSON schema the agent
sees, plus plan tools (``save_plan``, ``run_plan``, ``list_plans``) and
``session_log``. Calls dispatch to a ``Tools`` instance bound to a ``Session``,
so each one is a logged request with the full requested -> acknowledged ->
executed -> observed lifecycle, over whichever transport is connected (Virtual
Rig, TCP or BLE bridge).

    aperture mcp                                  # stdio (Claude Desktop, Cursor, OpenAI agents, ...)
    aperture mcp --http 8765                      # streamable HTTP on 127.0.0.1:8765/mcp
    aperture mcp --transport tcp --target pi.local:8766
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import anyio
import mcp.types as mt
from mcp.server import Server

from . import __version__
from .plans import PlanError, PlanRunner, list_plans, load_plan, save_plan
from .protocol import _jsonable
from .session import Session
from .tools import SPECS, ToolError, Tools, schemas

INSTRUCTIONS = (
    "Aperture OS hardware-in-the-loop rig. Tools drive a Raspberry Pi GPIO bridge (or the Virtual Rig) "
    "wired to a device under test. Pins use BCM numbering. Connect first (transport 'virtual' works with no "
    "hardware), drive inputs with set_mode/write/pulse/pwm/uart_send, observe with read/capture/uart_expect/"
    "wait_for, and decide pass/fail with assert_pin/assert_timeline/assert_frequency. Call release_all when done. "
    "Use save_plan to store the passing sequence as a deterministic plan that run_plan re-runs without an agent.")

_STEP = {"type": "object", "properties": {"tool": {"type": "string"}, "args": {"type": "object"},
                                          "assert": {"type": "boolean"}, "name": {"type": "string"}},
         "required": ["tool"]}
EXTRA_TOOLS: dict[str, dict] = {
    "save_plan": {"description": "Save a deterministic, re-runnable test plan (tool calls + assertions). "
                                 "Steps use the same tool names/args as this server; mark checks with assert=true.",
                  "inputSchema": {"type": "object", "properties": {
                      "name": {"type": "string"}, "source": {"type": "string", "description": "the NL test"},
                      "steps": {"type": "array", "items": _STEP}, "cleanup": {"type": "array", "items": _STEP},
                      "requires": {"type": "object", "description": "{wiring: [{pin, dir: in|out, dut_pin?}]}"}},
                      "required": ["name", "steps"]}},
    "run_plan": {"description": "Run a saved plan by slug on the connected rig, without the agent. Returns per-step "
                                "pass/fail and the overall verdict.",
                 "inputSchema": {"type": "object", "properties": {"slug": {"type": "string"}},
                                 "required": ["slug"]}},
    "list_plans": {"description": "List saved and example test plans.",
                   "inputSchema": {"type": "object", "properties": {}}},
    "session_log": {"description": "Recent events of the current session (requests, acks, pin changes, bus bytes).",
                    "inputSchema": {"type": "object", "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                        "kind": {"type": "string", "description": "filter by event kind prefix, e.g. pin. or bus."}}}},
}


def tool_definitions() -> list[mt.Tool]:
    tools = [mt.Tool(name=s["function"]["name"], description=s["function"]["description"],
                     inputSchema=s["function"]["parameters"]) for s in schemas()]
    tools += [mt.Tool(name=name, description=spec["description"], inputSchema=spec["inputSchema"])
              for name, spec in EXTRA_TOOLS.items()]
    return tools


class RigHost:
    """What MCP calls run against: a ``Tools`` (and its session) plus the lock serialising access.

    The console passes its own getter/lock so the UI and the agent share one session.
    """

    def __init__(self, get_tools: Callable[[], Tools] | None = None, lock: threading.RLock | None = None,
                 home: str | None = None, actor: str = "agent", wiring_policy: str | None = None):
        self.home = home
        self.lock = lock or threading.RLock()
        if get_tools is None:
            session = Session(actor=actor, home=home)
            tools = Tools(session, wiring_policy=wiring_policy)
            get_tools = lambda: tools
        self.get_tools = get_tools

    def call(self, name: str, args: dict) -> dict:
        with self.lock:
            tools = self.get_tools()
            if name in SPECS:
                return tools.call(name, **args)
            if name == "save_plan":
                try:
                    path = save_plan(args, self.home)
                except PlanError as exc:
                    raise ToolError("bad_request", str(exc)) from None
                plan = load_plan(path)
                return {"slug": plan["slug"], "path": str(path), "steps": len(plan["steps"])}
            if name == "run_plan":
                try:
                    plan = load_plan(args["slug"], self.home)
                except (PlanError, FileNotFoundError, KeyError) as exc:
                    raise ToolError("bad_request", f"no such plan: {exc}") from None
                return PlanRunner(tools, plan).run()
            if name == "list_plans":
                return {"plans": list_plans(self.home)}
            if name == "session_log":
                events = tools.session.events
                if args.get("kind"):
                    events = [e for e in events if str(e.get("kind", "")).startswith(args["kind"])]
                return {"session": tools.session.id, "events": events[-int(args.get("limit") or 50):]}
            raise ToolError("unknown_op", f"unknown tool {name!r}")

    def close(self) -> None:
        with self.lock:
            session = self.get_tools().session
            if session.transport and session.transport.connected:
                session.disconnect("mcp_shutdown")


def _result(payload: Any, error: bool = False) -> mt.CallToolResult:
    data = _jsonable(payload)
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=json.dumps(data, default=str))],
                             structuredContent=data if isinstance(data, dict) else {"result": data},
                             isError=error)


def build_server(host: RigHost, name: str = "aperture") -> Server:
    definitions = tool_definitions()
    known = {tool.name for tool in definitions}

    async def list_tools(ctx, params) -> mt.ListToolsResult:
        return mt.ListToolsResult(tools=definitions)

    async def call_tool(ctx, params: mt.CallToolRequestParams) -> mt.CallToolResult:
        if params.name not in known:
            return _result({"error": {"code": "unknown_op", "msg": f"unknown tool {params.name!r}"}}, True)
        args = dict(params.arguments or {})
        try:
            # tool calls block (pulses, uart_expect, captures): keep the event loop free
            payload = await anyio.to_thread.run_sync(host.call, params.name, args)
        except ToolError as exc:
            return _result({"error": {"code": exc.code, "msg": exc.msg, **exc.extra}}, True)
        except Exception as exc:  # noqa: BLE001 - surface as a tool error, never crash the server
            return _result({"error": {"code": "io", "msg": f"{type(exc).__name__}: {exc}"}}, True)
        return _result(payload)

    return Server(name, version=__version__, instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool)


def serve_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    anyio.run(run)


def http_app(server: Server, host: str = "127.0.0.1"):
    return server.streamable_http_app(host=host)


def serve_http(server: Server, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    uvicorn.run(http_app(server, host), host=host, port=port, log_level="warning")


def start_http_thread(server: Server, host: str = "127.0.0.1", port: int = 8765):
    """Serve streamable HTTP in a background thread (used by the console). Returns the uvicorn Server."""
    import uvicorn
    config = uvicorn.Config(http_app(server, host), host=host, port=port, log_level="warning")
    uv = uvicorn.Server(config)
    threading.Thread(target=uv.run, name="mcp-http", daemon=True).start()
    return uv


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="aperture-mcp", description="Aperture rig tools over MCP")
    add_arguments(parser)
    args = parser.parse_args(argv)
    return run(args, sys.stderr)


def add_arguments(parser) -> None:
    parser.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP bind address (default loopback)")
    parser.add_argument("--transport", choices=["virtual", "tcp", "ble"],
                        help="connect to this rig at startup (else the agent calls connect)")
    parser.add_argument("--scenario", default="empty")
    parser.add_argument("--target", help="tcp host:port or BLE name/address")
    parser.add_argument("--wiring-policy", choices=["ask", "off"],
                        help="ask: the human must mark each pin wired before it is driven (default: scenario's)")
    parser.add_argument("--home", help="data directory (default $APERTURE_HOME or ~/.aperture)")


def run(args, log) -> int:
    host = RigHost(home=getattr(args, "home", None), actor="agent:mcp",
                   wiring_policy=getattr(args, "wiring_policy", None))
    if args.transport:
        result = host.call("connect", {"transport": args.transport, "scenario": args.scenario,
                                       "target": args.target})
        print(f"aperture-mcp: connected {result['manifest']['bridge']} ({args.transport})", file=log)
    server = build_server(host)
    try:
        if args.http:
            print(f"aperture-mcp: http://{args.bind}:{args.http}/mcp", file=log)
            serve_http(server, args.bind, args.http)
        else:
            serve_stdio(server)
    finally:
        host.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
