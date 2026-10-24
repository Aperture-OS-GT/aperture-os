"""Local HTTP + SSE API for the tester interface.

One ``ConsoleApp`` owns the current ``Session``/``Tools`` pair; the UI renders
session events through one reducer for live and replay. Binds loopback only and
rejects cross-origin POSTs — this is a bench console, not a multi-user service.
With ``OPENAI_API_KEY`` set, prompts go to the AI agent (``aperture.agent``), else to
the offline interpreter. A keepalive thread feeds the bridge watchdog while the
console idles and re-takes control the watchdog took away.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .agent import run_agent
from .pins import DEFAULT_LIMITS, GPIO_PINS, header_rows
from .plans import PlanError, PlanRunner, compile_plan, list_plans, load_plan, save_plan
from .protocol import DRIVING_OPS
from .report import render_report
from .rig.virtual import list_scenarios
from .session import Session, dump_session, list_sessions, load_session, summarize
from .tools import SPECS, ToolError, Tools, schemas
from .transport.base import TransportError

STATIC = Path(__file__).with_name("static")
MAX_BODY = 1 << 20
KEEPALIVE_TICK_S = 0.1      # short, so even a sub-second watchdog is fed in time
KEEPALIVE_FRACTION = 3      # ping once a third of the watchdog passed without a feeding request
KEEPALIVE_JOIN_S = 1.0
FEEDS_WATCHDOG = DRIVING_OPS | {"ping", "control.take"}  # requests every bridge counts as alive
AGENT_HISTORY_NOTES = 6     # earlier tester/agent notes the model sees, for follow-up prompts
AI_FALLBACK = "AI unavailable ({}); used the offline interpreter. "

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


class ConsoleApp:
    def __init__(self, home: str | None = None, actor: str = "tester"):
        self.home = home
        self.actor = actor
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.session: Session | None = None
        self.tools: Tools | None = None
        self.feed: list[dict] = []          # every session event + console notices, with a global cursor
        self.replay: dict | None = None
        self.busy = False
        self.agent_busy = False             # an AI prompt is running; the lock is free between its tool calls
        self._control_lost = False          # the bridge watchdog took control away; the keepalive re-takes it
        self._fed_at = 0.0                  # monotonic time of the last request that feeds the watchdog
        self._closing = threading.Event()
        self._new_session()
        self._keepalive = threading.Thread(target=self._keepalive_loop, name="console-keepalive", daemon=True)
        self._keepalive.start()

    # --- session plumbing ------------------------------------------------------------
    def _new_session(self) -> Session:
        with self.lock:
            if self.session and self.session.transport and self.session.transport.connected:
                self.session.disconnect("new_session")
            elif self.session:
                self.session.end("new_session")
            self.session = Session(actor=self.actor, home=self.home)
            self.tools = Tools(self.session)
            self.session.on_event(self._push)
            self.replay = None
            self._control_lost = False
            self._notice("session.new", session=self.session.id)
            return self.session

    def _push(self, event: dict) -> None:
        with self.cond:
            self._watch_control(event)
            self.feed.append({"cursor": len(self.feed) + 1, "session": self.session.id, **event})
            self.cond.notify_all()

    def _watch_control(self, event: dict) -> None:
        """Keepalive bookkeeping from the session's own events (runs under the console lock)."""
        if event["kind"] == "control.lost":
            self._control_lost = True
        elif event["kind"] == "request.stage" and event.get("stage") == "requested" \
                and event.get("op") in FEEDS_WATCHDOG:
            self._fed_at = time.monotonic()

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

    # --- control keepalive ------------------------------------------------------------
    def _keepalive_loop(self) -> None:
        while not self._closing.wait(KEEPALIVE_TICK_S):
            if not self.lock.acquire(blocking=False):
                continue  # a tool call holds the console and is talking to the bridge itself
            try:
                self._keepalive_tick()
            except (TransportError, OSError) as exc:
                log.warning("console keepalive: %s", exc)
            finally:
                self.lock.release()

    def _keepalive_tick(self) -> None:
        """Re-take control if the bridge watchdog took it while still connected. Feeding the
        watchdog is the Session's job (its keepalive pings under the session lock); pinging the
        transport from here as well could deadlock against Session.request's lock order."""
        transport = self.session.transport
        if not self._connected() or _manual_clock(transport):
            return
        if transport.control_token is None and self._control_lost:
            self._retake_control()

    def _retake_control(self) -> None:
        """Control was lost to the watchdog while still connected: take it back once and say so."""
        self._control_lost = False
        if self.session.request("control.take", actor="system").ok:
            self.session.note("control re-taken (watchdog)", "system")
            self._stream_all()

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
                "version": __version__, "cursor": len(self.feed), "busy": self.busy or self.agent_busy,
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
                "llm": bool(os.environ.get("OPENAI_API_KEY")),
            }

    # --- actions --------------------------------------------------------------------------
    def connect(self, data: dict) -> dict:
        kind = data.get("transport", "virtual")
        with self.lock:
            if self.session.transport is not None:  # a session holds one connection, even once closed
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
        with self.lock:
            if self.replay:
                raise ApiError(409, "exit replay before using the agent")
            if self.agent_busy:
                raise ApiError(409, "the agent is still working on the previous prompt")
            self.session.note(text.strip(), "tester")
            if not os.environ.get("OPENAI_API_KEY"):
                return self._interpret(text)
            session, tools, context = self.session, _LockedTools(self, self.tools), self._agent_context()
            self.agent_busy = True
        try:
            outcome = run_agent(tools, text.strip(), context)
        except Exception as exc:  # noqa: BLE001 - auth, network, SDK missing...: the offline path still works
            log.warning("AI agent failed, using the offline interpreter: %r", exc)
            return self._interpret(text, AI_FALLBACK.format(_short_reason(exc)))
        finally:
            self.agent_busy = False
        with self.lock:
            session.note(outcome["reply"], "agent")
        return {"reply": outcome["reply"], "results": outcome["results"]}

    def _agent_context(self) -> dict:
        """What the model needs to know about the bench right now (called under the lock)."""
        transport = self.session.transport
        notes = [{"actor": e["actor"], "text": e["text"]} for e in self.session.events if e["kind"] == "note"]
        return {"connected": self._connected(), "transport": transport.describe() if transport else None,
                "wiring": {str(k): v for k, v in self.session.wiring.items()},
                "pending_wiring": {str(k): v for k, v in self.session.pending_wiring.items()},
                "uart_ports": sorted(getattr(self.tools, "_rx", {})),  # ports opened through the tools
                "recent_notes": notes[-AGENT_HISTORY_NOTES - 1:-1]}  # the last note is this prompt

    def _interpret(self, text: str, prefix: str = "") -> dict:
        """The offline intent path; ``prefix`` tells the tester why the AI agent was not used."""
        from .intent import interpret
        with self.lock:
            action = interpret(text, self.state())
            if action["kind"] == "plan":
                try:
                    plan = compile_plan(text, {"wiring": {str(k): v for k, v in self.session.wiring.items()}})
                except PlanError as exc:
                    raise ApiError(400, str(exc)) from None
                self._notice("plan.compiled", plan=plan)
                reply = prefix + action["reply"] + (f" Needs: {', '.join(plan['needs'])}." if plan.get("needs")
                                                    else f" {len(plan['steps'])} steps; press Run to execute.")
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
                reply = prefix + (action["reply"](results) if callable(action["reply"]) else action["reply"])
                self.session.note(reply, "agent")
                return {"reply": reply, "results": results}
            reply = prefix + action["reply"]
            self.session.note(reply, "agent")
            return {"reply": reply}

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
        self._closing.set()
        self._keepalive.join(KEEPALIVE_JOIN_S)
        with self.lock:
            if self.session:
                self.session.close()


class _LockedTools:
    """The agent's handle on the tools: the console lock is held for one tool call at a time and
    never while the model is thinking, so /api/state and /api/events stay live during a prompt."""

    def __init__(self, app: ConsoleApp, tools: Tools):
        self._app, self._tools = app, tools

    def call(self, tool: str, /, **args) -> dict:
        with self._app.lock:
            if self._app.replay:
                raise ToolError("replay", "exit replay before driving the rig")
            return self._tools.call(tool, **args)


def _manual_clock(transport) -> bool:
    """A Virtual Rig on a manual clock has no wall-clock watchdog (and tests step it deterministically)."""
    rig = getattr(transport, "rig", None)
    return rig is not None and not rig.clock.realtime


def _watchdog_s(transport) -> float:
    """The watchdog in force: TcpTransport remembers it, the Virtual Rig holds it, else the manifest."""
    rig_controller = getattr(getattr(transport, "rig", None), "controller", None) or {}
    limits = (transport.manifest or {}).get("limits") or {}
    return float(getattr(transport, "watchdog_s", None) or rig_controller.get("watchdog_s")
                 or limits.get("watchdog_s") or DEFAULT_LIMITS["watchdog_s"])


def _short_reason(exc: Exception) -> str:
    """A few words for the tester; the full error goes to the server log."""
    return "openai package not installed" if isinstance(exc, ImportError) else type(exc).__name__


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


def serve(host: str = "127.0.0.1", port: int = 8080, home: str | None = None, open_browser: bool = False):
    app = ConsoleApp(home=home)
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
    args = parser.parse_args(argv)
    serve(port=args.port, home=args.home, open_browser=args.open)


if __name__ == "__main__":  # pragma: no cover
    main()
