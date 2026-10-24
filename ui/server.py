"""Local-only web console with bounded replayable SSE events."""
import argparse
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.client import Client
from agent.planner import run_llm, run_offline, verify
from pi.backends import SimBackend
from pi.daemon import Harness, Server
from ui.intent import interpret


class LoggedClient:
    def __init__(self, client, app):
        self.client, self.app = client, app

    def command(self, **command):
        start = time.perf_counter()
        self.app.emit("TX", command)
        try:
            result = self.client.command(**command)
        except Exception as exc:
            self.app.emit("ERROR", {"error": str(exc)}, (time.perf_counter()-start)*1000)
            raise
        self.app.emit("RX", result, (time.perf_counter()-start)*1000)
        if "physical" in result:
            self.app.measurement = result
        return result


class ConsoleApp:
    def __init__(self, mode="sim", host="127.0.0.1", port=8765):
        self.lock = threading.Lock()
        self.condition = threading.Condition()
        self.events = deque(maxlen=1000)
        self.sequence = 0
        self.sim = None
        self.thread = None
        self.measurement = None
        self.report = None
        self.connected = False
        self.config = {}
        self.connect(mode, host, port, True)

    def emit(self, level, data, latency_ms=None):
        with self.condition:
            self.sequence += 1
            event = {"id": self.sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
                     "level": level, "data": data, "latency_ms": latency_ms}
            self.events.append(event)
            self.condition.notify_all()

    def close(self):
        if self.sim:
            self.sim.shutdown()
            self.thread.join()
            self.sim.server_close()
            self.sim = None

    def connect(self, mode, host, port, demo_bug):
        if mode not in ("sim", "tcp", "rfcomm") or not isinstance(host, str):
            raise ValueError("invalid connection mode or host")
        if type(port) is not int or not 1 <= port <= 65535 or type(demo_bug) is not bool:
            raise ValueError("invalid port or demo_bug")
        self.close()
        self.connected = False
        self.config = {"mode": mode, "host": host, "port": port, "demo_bug": demo_bug}
        if mode == "sim":
            self.sim = Server(("127.0.0.1", 0), Harness(SimBackend(demo_bug)))
            self.thread = threading.Thread(target=self.sim.serve_forever, daemon=True)
            self.thread.start()
        self.measurement = None
        self.report = None
        with self.client() as raw:
            client = LoggedClient(raw, self)
            client.command(cmd="ping")
            client.command(cmd="read_state")
        self.connected = True
        self.emit("AGENT", {"reply": "Harness connected. Ready for verification."})

    def client(self):
        if self.config["mode"] == "sim":
            return Client(port=self.sim.server_address[1])
        return Client(self.config["host"], self.config["port"], self.config["mode"] == "rfcomm")

    def snapshot(self):
        return {"connection": {**self.config, "connected": self.connected},
                "measurement": self.measurement, "report": self.report,
                "llm": bool(os.environ.get("OPENAI_API_KEY"))}

    def prompt(self, prompt):
        if not isinstance(prompt, str) or len(prompt) > 4000:
            raise ValueError("prompt must be text up to 4000 characters")
        self.emit("USER", {"reply": prompt})

        def display(row):
            self.emit("STEP", row)

        with self.client() as raw:
            client = LoggedClient(raw, self)
            if os.environ.get("OPENAI_API_KEY"):
                self.report = run_llm(client, os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                                      display, prompt=prompt)
                reply = self.report["summary"]
            else:
                command, reply = interpret(prompt)
                if command is None:
                    self.report = run_offline(client, display)
                else:
                    response = client.command(**command)
                    row = {**response, "issues": verify(response), "step": prompt,
                           "reasoning": reply}
                    display(row)
                    self.report = {"mode": "offline", "pass": not row["issues"], "steps": [row]}
        failed = not self.report["pass"]
        reply += " Safety violation detected." if failed else " Physical verification passed."
        self.emit("AGENT", {"reply": reply})
        self.emit("REPORT", self.report)
        return {"reply": reply, "report": self.report}


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, data, content_type="application/json"):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        app = self.server.app
        if self.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                cursor = int(self.headers.get("Last-Event-ID", "0"))
            except ValueError:
                cursor = 0
            try:
                while True:
                    with app.condition:
                        events = [event for event in app.events if event["id"] > cursor]
                        if not events:
                            app.condition.wait(10)
                    for event in events:
                        self.wfile.write(f"id: {event['id']}\ndata: {json.dumps(event)}\n\n".encode())
                        cursor = event["id"]
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (OSError, ValueError):
                return
        elif self.path == "/api/state":
            self.send(200, app.snapshot())
        elif self.path == "/api/report":
            self.send(200, app.report)
        elif self.path in ("/", "/app.js", "/style.css"):
            name = "index.html" if self.path == "/" else self.path[1:]
            kind = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}
            self.send(200, (Path(__file__).parent / "static" / name).read_bytes(), kind[name])
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        app = self.server.app
        # Local console: reject cross-origin browser writes, including DNS rebinding.
        if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
            self.send(403, {"error": "cross-origin request rejected"})
            return
        if self.headers.get("Host", "").split(":")[0] not in ("localhost", "127.0.0.1"):
            self.send(403, {"error": "local host required"})
            return
        if self.path not in ("/api/prompt", "/api/connect"):
            self.send(404, {"error": "not found"})
            return
        if not app.lock.acquire(blocking=False):
            self.send(409, {"error": "console busy"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                raise ValueError("body must be 1..8192 bytes")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise TypeError("body must be an object")
            if self.path == "/api/connect":
                app.connect(data.get("mode", "sim"), data.get("host", "127.0.0.1"),
                            data.get("port", 8765), data.get("demo_bug", True))
                result = app.snapshot()
            else:
                result = app.prompt(data.get("prompt", ""))
            self.send(200, result)
        except (ValueError, TypeError) as exc:
            self.send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- isolate optional SDK and transport failures
            app.emit("ERROR", {"error": str(exc)})
            self.send(502, {"error": str(exc)})
        finally:
            app.lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--backend-mode", choices=("sim", "tcp", "rfcomm"), default="sim")
    parser.add_argument("--host", default="127.0.0.1", help="daemon hostname or Bluetooth MAC")
    parser.add_argument("--daemon-port", type=int, default=8765)
    args = parser.parse_args()
    app = ConsoleApp(args.backend_mode, args.host, args.daemon_port)
    try:
        with HTTPServer(("127.0.0.1", args.port), app) as server:
            print(f"Aperture OS Console: http://localhost:{args.port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


if __name__ == "__main__":
    main()
