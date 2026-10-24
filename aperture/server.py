"""Local HTTP + SSE API for the tester interface.

One ``ConsoleApp`` owns the current ``Session``/``Tools`` pair; the UI renders
session events through one reducer for live and replay. Binds loopback only and
rejects cross-origin POSTs — this is a bench console, not a multi-user service.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .pins import GPIO_PINS, header_rows
from .plans import PlanError, PlanRunner, compile_plan, list_plans, load_plan, save_plan
from .report import render_report
from .rig.virtual import list_scenarios
from .session import Session, dump_session, list_sessions, load_session, summarize
from .tools import SPECS, ToolError, Tools, schemas
from .transport.base import TransportError

STATIC = Path(__file__).with_name("static")
MAX_BODY = 1 << 20


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


class ConsoleApp:
    def __init__(self, home: str | None = None, actor: str = "tester", llm: bool | None = None,
                 agent_factory=None):
        self.home = home
        self.actor = actor
        self.lock = threading.RLock()
        # The feed has its own lock: session events arrive on transport reader threads while a request
        # holds self.lock, so pushing to the feed must never wait for self.lock.
        self.cond = threading.Condition(threading.RLock())
        self.session: Session | None = None
        self.tools: Tools | None = None
        self.feed: list[dict] = []          # every session event + console notices, with a global cursor
        self.replay: dict | None = None
        self.busy = False
        self.mcp_url: str | None = None
        self._mcp_server = None
        self._agent_factory = agent_factory
        if llm is None:
            from .agent import openai_available
            llm = openai_available()
        self.llm = bool(llm)
        self._new_session()

    # --- session plumbing ------------------------------------------------------------
    def _new_session(self) -> Session:
        with self.lock:
            if self.session and self.session.transport and self.session.transport.connected:
                self.session.disconnect("new_session")
            elif self.session:
                self.session.end("new_session")
            self.session = Session(actor=self.actor, home=self.home)
            self.tools = Tools(self.session)
            sid = self.session.id
            self.session.on_event(lambda event: self._push(event, sid))
            self.replay = None
            self._notice("session.new", session=self.session.id)
            return self.session

    def _push(self, event: dict, session_id: str) -> None:
        with self.cond:
            self.feed.append({"cursor": len(self.feed) + 1, "session": session_id, **event})
            self.cond.notify_all()

    def _notice(self, kind: str, **fields) -> None:
        with self.cond:
            self.feed.append({"cursor": len(self.feed) + 1, "session": self.session.id if self.session else None,
                              "seq": None, "t_host_us": None, "kind": kind, "actor": "console", **fields})
            self.cond.notify_all()

    def events_since(self, cursor: int, wait_s: float = 0) -> list[dict]:
        with self.cond:
            if wait_s and len(self.feed) <= cursor:
                self.cond.wait(wait_s)
            return self.feed[cursor:]

    def _connected(self) -> bool:
        return bool(self.session and self.session.transport and self.session.transport.connected)

    def _tick(self) -> None:
        rig = getattr(self.session.transport, "rig", None) if self.session else None
        if rig is not None:
            rig.pump()

    # --- state --------------------------------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            self._tick()
            transport = self.session.transport
            pins = None
            if self._connected():
                try:
                    pins = {str(p["pin"]): p for p in transport.request("list_pins").result["pins"]}
                except (TransportError, ToolError):
                    pins = None
            manifest = self.session.manifest
            rig = getattr(transport, "rig", None)
            return {
                "version": __version__, "cursor": len(self.feed), "busy": self.busy,
                "session": {"id": self.session.id, "created_at": self.session.created_at,
                            "verdict": self.session.verdict, "events": len(self.session.events),
                            "wiring": {str(k): v for k, v in self.session.wiring.items()},
                            "pending_wiring": {str(k): v for k, v in self.session.pending_wiring.items()}},
                "connection": {"connected": self._connected(), "transport": transport.describe() if transport else None,
                               "controller": bool(transport and transport.control_token),
                               "clock_offset_us": transport.clock_offset_us if transport else None},
                "manifest": manifest, "header": header_rows(),
                "pins": pins,
                "behaviors": [b.describe() for b in rig.behaviors] if rig else [],
                "replay": self.replay["id"] if self.replay else None,
                "scenarios": list_scenarios(),
                "llm": self.llm, "mcp": self.mcp_url,
            }

    # --- actions --------------------------------------------------------------------------
    def connect(self, data: dict) -> dict:
        kind = data.get("transport", "virtual")
        with self.lock:
            if self._connected():
                self._new_session()
            self.replay = None
            try:
                realtime = bool(data.get("realtime", True))
                if kind == "virtual":
                    from .transport.virtual import VirtualTransport
                    transport = VirtualTransport(scenario=data.get("scenario") or "empty", realtime=realtime)
                    self.session.connect(transport, take_control=data.get("take_control", True))
                else:
                    self.tools.call("connect", transport=kind, target=data.get("target"),
                                    take_control=data.get("take_control", True))
            except (TransportError, ToolError, FileNotFoundError) as exc:
                raise ApiError(400, str(exc)) from None
            self._stream_all()
            return self.state()

    def _stream_all(self) -> None:
        """Watch every GPIO live: the console streams pin changes for the whole header."""
        transport = self.session.transport
        if transport is None or not transport.control_token:
            return
        try:
            self.session.request("stream.set", "system", pins=sorted(GPIO_PINS), on=True)
        except TransportError:
            pass

    def disconnect(self) -> dict:
        with self.lock:
            if self._connected():
                self.session.disconnect("user")
            return self.state()

    def new_session(self) -> dict:
        self._new_session()
        return self.state()

    def tool(self, data: dict) -> dict:
        name = data.get("tool")
        if name not in SPECS:
            raise ApiError(400, f"unknown tool {name!r}")
        args = data.get("args") or {}
        if not isinstance(args, dict):
            raise ApiError(400, "args must be an object")
        actor = data.get("actor", "tester")
        with self.lock:
            if self.replay:
                raise ApiError(409, "exit replay before driving the rig")
            if not self._connected() and name not in ("connect", "note"):
                raise ApiError(409, "not connected")
            self.busy = True
            try:
                if actor == "tester":
                    self.session.note(f"{name} {json.dumps(args)}", "tester")
                result = self.tools.call(name, **args)
                return {"ok": True, "result": result}
            except ToolError as exc:
                return {"ok": False, "error": {"code": exc.code, "msg": exc.msg, **exc.extra}}
            finally:
                self.busy = False

    def wiring(self, pin: int, data: dict) -> dict:
        with self.lock:
            try:
                return self.session.confirm_wiring(int(pin), bool(data.get("confirmed")), by=self.actor)
            except ValueError as exc:
                raise ApiError(404, str(exc)) from None

    def prompt(self, data: dict) -> dict:
        text = data.get("prompt")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ApiError(400, "prompt must be text up to 4000 characters")
        use_llm = self.llm and data.get("mode") != "rules"
        with self.lock:
            if self.replay:
                raise ApiError(409, "exit replay before using the agent")
            if use_llm and self.busy:
                raise ApiError(409, "the agent is already running")
            self.session.note(text.strip(), "tester")
            if use_llm:
                self.busy = True
        if use_llm:
            try:
                return self._prompt_llm(text.strip(), data.get("save_as"))
            finally:
                with self.lock:
                    self.busy = False
        with self.lock:
            from .intent import interpret
            action = interpret(text, self.state())
            if action["kind"] == "plan":
                try:
                    plan = compile_plan(text, {"wiring": {str(k): v for k, v in self.session.wiring.items()}})
                except PlanError as exc:
                    raise ApiError(400, str(exc)) from None
                self._notice("plan.compiled", plan=plan)
                reply = action["reply"] + (f" Needs: {', '.join(plan['needs'])}." if plan.get("needs") else
                                           f" {len(plan['steps'])} steps; press Run to execute.")
                self.session.note(reply, "agent")
                return {"reply": reply, "plan": plan}
            if action["kind"] == "tools":
                if not self._connected():
                    raise ApiError(409, "not connected")
                results = []
                for call in action["calls"]:
                    try:
                        results.append({"tool": call["tool"], "ok": True,
                                        "result": self.tools.call(call["tool"], **call["args"])})
                    except ToolError as exc:
                        results.append({"tool": call["tool"], "ok": False,
                                        "error": {"code": exc.code, "msg": exc.msg, **exc.extra}})
                        break
                reply = action["reply"](results) if callable(action["reply"]) else action["reply"]
                self.session.note(reply, "agent")
                return {"reply": reply, "results": results}
            self.session.note(action["reply"], "agent")
            return {"reply": action["reply"]}

    # --- OpenAI agent over MCP --------------------------------------------------------------
    @property
    def mcp_server(self):
        """MCP server bound to this console's session: the agent (and external MCP clients) drive the
        same rig, log and lifecycle the tester sees."""
        if self._mcp_server is None:
            from .mcp_server import RigHost, build_server
            self._mcp_server = build_server(RigHost(lambda: self.tools, self.lock, self.home))
        return self._mcp_server

    def serve_mcp(self, port: int, host: str = "127.0.0.1") -> str:
        from .mcp_server import start_http_thread
        start_http_thread(self.mcp_server, host, port)
        self.mcp_url = f"http://{host}:{port}/mcp"
        return self.mcp_url

    def _agent_context(self) -> str:
        with self.lock:
            transport = self.session.transport
            manifest = self.session.manifest or {}
            wiring = {str(k): v for k, v in self.session.wiring.items()}
            return (f"Rig: connected={self._connected()}, transport={transport.kind if transport else None}, "
                    f"bridge={manifest.get('bridge')}, fw={manifest.get('fw')}, "
                    f"controller={bool(transport and transport.control_token)}. Confirmed wiring: {json.dumps(wiring)}")

    def _prompt_llm(self, text: str, save_as: str | None = None) -> dict:
        from .agent import RigAgent
        factory = self._agent_factory or (lambda server: RigAgent(server))
        agent = factory(self.mcp_server)
        self._notice("agent.started", prompt=text)
        try:
            result = agent.run(text, save_as=save_as, context=self._agent_context())
        except Exception as exc:  # noqa: BLE001 - OpenAI/network errors: report, keep the console alive
            self._notice("agent.error", msg=f"{type(exc).__name__}: {exc}")
            raise ApiError(502, f"agent failed: {type(exc).__name__}: {exc}") from None
        plan = None
        with self.lock:
            self.session.note(result.reply, "agent")
            if result.plan:
                try:
                    plan = load_plan_inline(result.plan)
                    self._notice("plan.compiled", plan=plan)
                except (ApiError, PlanError):
                    plan = None
        return {"reply": result.reply, "llm": True, "verdict": result.verdict, "plan": plan, "saved": result.saved,
                "results": [{"tool": c.tool, "ok": c.ok, "args": c.args} for c in result.calls]}

    def run_plan(self, slug: str, data: dict | None = None) -> dict:
        with self.lock:
            if not self._connected():
                raise ApiError(409, "not connected")
            try:
                plan = load_plan(slug, self.home) if slug != "_inline" else load_plan_inline((data or {}).get("plan"))
            except (FileNotFoundError, PlanError) as exc:
                raise ApiError(404 if isinstance(exc, FileNotFoundError) else 400, str(exc)) from None
            self.busy = True
            try:
                result = PlanRunner(self.tools, plan).run()
                for w in result.get("blocked") or []:
                    if w["pin"] not in self.session.pending_wiring and w["pin"] not in self.session.wiring:
                        self.session.request_wiring(w["pin"], w.get("dut_pin") or w.get("dut") or "?",
                                                    w.get("component") or plan["name"],
                                                    f"required by plan {plan['slug']}", w.get("dir", "out"), actor="agent")
                return result
            finally:
                self.busy = False

    def save_plan(self, data: dict) -> dict:
        try:
            path = save_plan(data, self.home)
        except PlanError as exc:
            raise ApiError(400, str(exc)) from None
        return {"saved": str(path), "slug": load_plan(path)["slug"]}

    # --- replay -----------------------------------------------------------------------------
    def start_replay(self, session_id: str) -> dict:
        try:
            data = load_session(session_id, self.home)
        except (FileNotFoundError, ValueError) as exc:
            raise ApiError(404, str(exc)) from None
        with self.lock:
            self.replay = data
            self._notice("replay.start", replay=data["id"])
        return data

    def stop_replay(self) -> dict:
        with self.lock:
            self.replay = None
            self._notice("replay.stop")
        return self.state()

    def close(self) -> None:
        with self.lock:
            if self.session:
                self.session.close()


def load_plan_inline(plan):
    from .plans import validate_plan
    if not isinstance(plan, dict):
        raise PlanError("plan must be an object")
    return validate_plan(plan)


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app: ConsoleApp):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, data, content_type: str = "application/json"):
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: ApiError):
        self._send(exc.status, {"error": exc.message, **exc.extra})

    def do_GET(self):
        app: ConsoleApp = self.server.app
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path == "/api/events":
                return self._sse(app)
            if path == "/api/state":
                return self._send(200, app.state())
            if path == "/api/tools":
                return self._send(200, {"tools": schemas()})
            if path == "/api/sessions":
                sessions = list_sessions(app.home)
                if app.session.id not in {s["id"] for s in sessions}:
                    sessions.insert(0, summarize(app.session.to_dict()))
                return self._send(200, {"sessions": sessions, "current": app.session.id})
            if path.startswith("/api/sessions/"):
                parts = path.split("/")[3:]
                session_id = parts[0]
                if session_id == app.session.id and len(parts) == 1:
                    return self._send(200, app.session.to_dict())
                try:
                    data = load_session(session_id, app.home) if session_id != app.session.id else app.session.to_dict()
                except (FileNotFoundError, ValueError) as exc:
                    raise ApiError(404, str(exc)) from None
                if len(parts) == 1:
                    return self._send(200, data)
                if parts[1] == "export.json":
                    return self._send(200, dump_session(data).encode(), "application/json")
                if parts[1] == "export.md":
                    return self._send(200, render_report(data).encode(), "text/markdown; charset=utf-8")
                raise ApiError(404, "not found")
            if path == "/api/plans":
                return self._send(200, {"plans": list_plans(app.home)})
            if path.startswith("/api/plans/"):
                slug = path.split("/")[3]
                try:
                    return self._send(200, load_plan(slug, app.home))
                except (FileNotFoundError, PlanError) as exc:
                    raise ApiError(404, str(exc)) from None
            if path == "/api/scenarios":
                return self._send(200, {"scenarios": list_scenarios()})
            return self._static(path, query)
        except ApiError as exc:
            self._error(exc)

    def _static(self, path: str, query):
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            raise ApiError(404, "not found")
        types = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
                 ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}
        self._send(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

    def _sse(self, app: ConsoleApp):
        cursor = int(self.headers.get("Last-Event-ID", "0") or 0)
        query = parse_qs(urlparse(self.path).query)
        if "cursor" in query:
            cursor = int(query["cursor"][0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_beat = time.monotonic()
        try:
            while True:
                events = app.events_since(cursor, wait_s=1.0)
                for event in events:
                    cursor = event["cursor"]
                    self.wfile.write(f"id: {cursor}\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                if time.monotonic() - last_beat > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                if self.server.app is None:  # pragma: no cover
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        app: ConsoleApp = self.server.app
        origin = self.headers.get("Origin")
        if origin not in (None, f"http://{self.headers.get('Host')}"):
            return self._send(403, {"error": "cross-origin requests are rejected"})
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            remaining = min(length, 8 * MAX_BODY)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            return self._send(413, {"error": "body too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError  # noqa: TRY004
        except ValueError:
            return self._send(400, {"error": "body must be a JSON object"})
        path = urlparse(self.path).path
        try:
            if path == "/api/connect":
                return self._send(200, app.connect(data))
            if path == "/api/disconnect":
                return self._send(200, app.disconnect())
            if path == "/api/session/new":
                return self._send(200, app.new_session())
            if path == "/api/tool":
                return self._send(200, app.tool(data))
            if path == "/api/prompt":
                return self._send(200, app.prompt(data))
            if path.startswith("/api/wiring/"):
                return self._send(200, app.wiring(int(path.split("/")[3]), data))
            if path == "/api/plans":
                return self._send(200, app.save_plan(data))
            if path.startswith("/api/plans/") and path.endswith("/run"):
                return self._send(200, app.run_plan(path.split("/")[3], data))
            if path == "/api/plans/compile":
                try:
                    return self._send(200, compile_plan(str(data.get("text", "")),
                                                        {"wiring": {str(k): v for k, v in app.session.wiring.items()}},
                                                        name=data.get("name")))
                except PlanError as exc:
                    raise ApiError(400, str(exc)) from None
            if path.startswith("/api/sessions/") and path.endswith("/replay"):
                return self._send(200, app.start_replay(path.split("/")[3]))
            if path == "/api/replay/stop":
                return self._send(200, app.stop_replay())
            raise ApiError(404, "not found")
        except ApiError as exc:
            self._error(exc)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})


def serve(host: str = "127.0.0.1", port: int = 8080, home: str | None = None, open_browser: bool = False,
          mcp_port: int | None = None):
    app = ConsoleApp(home=home)
    if mcp_port:
        print(f"MCP server (shares this session): {app.serve_mcp(mcp_port, host)}")
    print(f"Agent: {'OpenAI ' + __import__('os').environ.get('OPENAI_MODEL', 'gpt-4o-mini') if app.llm else 'rules (set OPENAI_API_KEY for the OpenAI agent)'}")
    server = HTTPServer((host, port), app)
    url = f"http://{host}:{server.server_address[1]}"
    print(f"Aperture OS console: {url}")
    if open_browser:  # pragma: no cover
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        app.close()
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aperture OS tester console (loopback only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--home")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--mcp-port", type=int, help="also expose the rig tools over MCP (streamable HTTP)")
    args = parser.parse_args(argv)
    serve(port=args.port, home=args.home, open_browser=args.open, mcp_port=args.mcp_port)


if __name__ == "__main__":  # pragma: no cover
    main()
