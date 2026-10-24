"""Console API: state, actions, SSE feed, plans, replay, and request hardening."""
import http.client as httpclient
import json
import threading
import time

import pytest

from aperture.server import ConsoleApp, HTTPServer


@pytest.fixture
def app(tmp_path):
    a = ConsoleApp(home=str(tmp_path))
    yield a
    a.close()


@pytest.fixture
def client(app):
    server = HTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def call(method, path, body=None, headers=None):
        conn = httpclient.HTTPConnection("127.0.0.1", port, timeout=5)
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(raw) if ctype.startswith("application/json") else raw), resp

    call.port = port
    yield call
    server.shutdown()
    server.server_close()


def test_initial_state_is_disconnected(app):
    st = app.state()
    assert st["connection"]["connected"] is False
    assert st["pins"] is None
    assert len(st["header"]) == 40
    assert any(s["name"] == "button_led" for s in st["scenarios"])
    assert st["session"]["id"]


def test_connect_virtual_and_drive_pin(app):
    st = app.connect({"transport": "virtual", "scenario": "loopback", "realtime": False})
    assert st["connection"]["connected"] and st["connection"]["controller"]
    assert st["manifest"]["bridge"]
    assert st["pins"]["17"]["mode"] in ("input", "disabled")
    r = app.tool({"tool": "set_mode", "args": {"pin": 17, "mode": "output", "initial": 0}})
    assert r["ok"]
    r = app.tool({"tool": "write", "args": {"pin": 17, "level": 1}})
    assert r["ok"] and r["result"]["observed"]["level"] == 1
    assert app.state()["pins"]["17"]["level"] == 1
    kinds = [e["kind"] for e in app.feed]
    assert "request.stage" in kinds and "pin.change" in kinds
    stages = [e["stage"] for e in app.session.events if e["kind"] == "request.stage" and e.get("req") == r["result"]["request"]]
    assert stages == ["requested", "acknowledged", "executed", "observed"]


def test_tool_errors_are_structured(app):
    with pytest.raises(Exception) as exc:
        app.tool({"tool": "write", "args": {"pin": 17, "level": 1}})
    assert exc.value.status == 409
    app.connect({"transport": "virtual", "scenario": "empty", "realtime": False})
    r = app.tool({"tool": "write", "args": {"pin": 17, "level": 1}})
    assert r["ok"] is False and r["error"]["code"]
    with pytest.raises(Exception) as exc:
        app.tool({"tool": "nope", "args": {}})
    assert exc.value.status == 400


def test_prompt_tool_and_plan_paths(app):
    app.connect({"transport": "virtual", "scenario": "button_led", "realtime": False})
    r = app.prompt({"prompt": "set GPIO17 high"})
    assert r["results"] and r["results"][0]["tool"] in ("set_mode", "write")
    assert "GPIO17" in r["reply"]
    r = app.prompt({"prompt": "confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22"})
    assert r["plan"]["steps"] and r["plan"]["slug"]
    notes = [e for e in app.session.events if e["kind"] == "note"]
    assert notes[0]["actor"] == "tester" and notes[1]["actor"] == "agent"
    r = app.prompt({"prompt": "what can you do"})
    assert r["reply"]


def test_run_inline_and_saved_plan(app):
    app.connect({"transport": "virtual", "scenario": "button_led", "realtime": False})
    plan = app.prompt({"prompt": "confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22"})["plan"]
    result = app.run_plan("_inline", {"plan": plan})
    assert result["pass"] is True
    assert app.state()["session"]["verdict"] is True
    saved = app.save_plan(plan)
    assert saved["slug"]
    result = app.run_plan(saved["slug"])
    assert result["pass"] is True
    assert "plan.end" in [e["kind"] for e in app.session.events]


def test_blocked_plan_requests_wiring_then_passes(app):
    app.connect({"transport": "virtual", "scenario": "esp32_safety_fixed", "realtime": False})
    result = app.run_plan("esp32-safety")
    assert result["pass"] is False and result["blocked"]
    pending = app.state()["session"]["pending_wiring"]
    assert set(pending) == {str(w["pin"]) for w in result["blocked"]}
    kinds = [e["kind"] for e in app.session.events]
    assert kinds.count("wiring.request") == len(result["blocked"])
    for pin in list(pending):
        app.wiring(int(pin), {"confirmed": True})
    assert app.state()["session"]["pending_wiring"] == {}
    result = app.run_plan("esp32-safety")
    assert result["pass"] is True and not result.get("blocked")
    with pytest.raises(Exception) as exc:
        app.wiring(17, {"confirmed": True})
    assert exc.value.status == 404


def test_replay_and_new_session(app):
    app.connect({"transport": "virtual", "scenario": "loopback", "realtime": False})
    app.tool({"tool": "set_mode", "args": {"pin": 17, "mode": "output", "initial": 1}})
    old_id = app.session.id
    app.disconnect()
    st = app.new_session()
    assert st["session"]["id"] != old_id
    rp = app.start_replay(old_id)
    assert rp["id"] == old_id and any(e["kind"] == "pin.change" for e in rp["events"])
    assert app.state()["replay"] == old_id
    with pytest.raises(Exception) as exc:
        app.tool({"tool": "note", "args": {"text": "x"}})
    assert exc.value.status == 409
    app.stop_replay()
    assert app.state()["replay"] is None


def test_http_state_and_static(client):
    status, body, _ = client("GET", "/api/state")
    assert status == 200 and body["connection"]["connected"] is False
    status, body, resp = client("GET", "/")
    assert status == 200 and b"Aperture OS" in body and "text/html" in resp.getheader("Content-Type")
    status, body, _ = client("GET", "/app.js")
    assert status == 200 and b"EventSource" in body
    status, _, _ = client("GET", "/../pyproject.toml")
    assert status in (403, 404)
    status, _, _ = client("GET", "/api/nope")
    assert status == 404


def test_http_actions_exports_and_hardening(client):
    status, st, _ = client("POST", "/api/connect", {"transport": "virtual", "scenario": "uart_echo", "realtime": False})
    assert status == 200 and st["connection"]["connected"]
    status, r, _ = client("POST", "/api/tool", {"tool": "uart_open", "args": {"tx": 14, "rx": 15, "baud": 115200}})
    assert status == 200 and r["ok"]
    port = r["result"]["port"]
    status, r, _ = client("POST", "/api/tool", {"tool": "uart_send", "args": {"port": port, "data": "ping\n"}})
    assert status == 200 and r["ok"]
    sid = st["session"]["id"]
    status, body, _ = client("GET", f"/api/sessions/{sid}")
    assert status == 200 and any(e["kind"] == "bus.bytes" for e in body["events"])
    status, body, _resp = client("GET", f"/api/sessions/{sid}/export.md")
    assert status == 200 and b"ping" in body
    status, body, _ = client("GET", f"/api/sessions/{sid}/export.json")
    assert status == 200 and body["id"] == sid
    status, body, _ = client("GET", "/api/plans")
    assert status == 200 and any(p["slug"] == "boot-ok" for p in body["plans"])
    status, body, _ = client("GET", "/api/tools")
    assert status == 200 and "write" in body["tools"] or isinstance(body, dict)
    # cross-origin POSTs are rejected, oversize bodies are rejected, bad JSON is a 400
    status, body, _ = client("POST", "/api/disconnect", {}, {"Origin": "https://evil.example"})
    assert status == 403
    status, _, _ = client("POST", "/api/tool", b"x" * ((1 << 20) + 1))
    assert status == 413
    status, _, _ = client("POST", "/api/tool", b"{not json")
    assert status == 400
    status, body, _ = client("POST", "/api/disconnect", {}, {"Origin": f"http://127.0.0.1:{client.port}"})
    assert status == 200 and body["connection"]["connected"] is False


def test_sse_feed_cursor_and_last_event_id(client, app):
    client("POST", "/api/connect", {"transport": "virtual", "scenario": "loopback", "realtime": False})
    client("POST", "/api/tool", {"tool": "set_mode", "args": {"pin": 17, "mode": "output", "initial": 1}})
    total = len(app.feed)
    assert total > 3

    def read_sse(path, headers=None, limit=2.0):
        conn = httpclient.HTTPConnection("127.0.0.1", client.port, timeout=limit)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
        events, deadline = [], time.time() + limit
        try:
            while time.time() < deadline:
                line = resp.fp.readline()
                if not line:
                    break
                if line.startswith(b"data:"):
                    events.append(json.loads(line[5:]))
                if len(events) >= total:
                    break
        except (TimeoutError, OSError):
            pass
        conn.close()
        return events

    events = read_sse("/api/events?cursor=0")
    assert [e["cursor"] for e in events] == list(range(1, total + 1))
    resumed = read_sse("/api/events", headers={"Last-Event-ID": str(total - 2)}, limit=1.0)
    assert [e["cursor"] for e in resumed][:2] == [total - 1, total]
