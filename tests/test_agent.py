"""AI agent loop (scripted fake OpenAI client), prompt routing, control keepalive, reconnect."""
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from aperture import server
from aperture.agent import MAX_RESULT_CHARS, run_agent
from aperture.server import ApiError, ConsoleApp
from aperture.session import Session
from aperture.tools import SPECS, Tools

OFFLINE = "used the offline interpreter. "
RETAKEN = "control re-taken (watchdog)"


class FakeCompletions:
    """Scripted ``chat.completions``: each create() returns the next message or raises it."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(choices=[SimpleNamespace(message=step)])


def fake_client(*script):
    completions = FakeCompletions(script)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def say(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


def call(call_id, name, args):
    arguments = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments=arguments))


def tool_messages(request):
    return [m for m in request["messages"] if m["role"] == "tool"]


def wait_until(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def rig(tmp_path):
    def connect(scenario="button_led", policy=None):
        session = Session(home=tmp_path)
        tools = Tools(session, wiring_policy=policy)
        tools.call("connect", transport="virtual", scenario=scenario)
        return session, tools
    return connect


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    console = ConsoleApp(home=str(tmp_path))
    yield console
    console.close()


# --- run_agent ------------------------------------------------------------------------------
def test_agent_runs_tool_calls_on_the_virtual_rig_and_replies(rig, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    session, tools = rig()
    client, completions = fake_client(
        say(None, call("c1", "set_mode", {"pin": 17, "mode": "output", "initial": 0}),
            call("c2", "write", {"pin": 17, "level": 1})),
        say(None, call("c3", "read", {"pin": 17})),
        say(" GPIO17 reads 1. "))
    context = {"connected": True, "transport": {"kind": "virtual", "scenario": "button_led"}}
    out = run_agent(tools, "drive GPIO17 high", context, client=client)
    assert out["reply"] == "GPIO17 reads 1."
    assert [(r["tool"], r["ok"]) for r in out["results"]] == [("set_mode", True), ("write", True),
                                                               ("read", True)]
    assert out["results"][1]["result"]["observed"]["level"] == 1
    assert out["results"][2]["result"]["level"] == 1
    first, second, third = completions.requests
    assert first["model"] == "gpt-5.1"
    assert {t["function"]["name"] for t in first["tools"]} == set(SPECS)
    system, user = first["messages"]
    assert system["role"] == "system" and "GPIO17 -> ESP32 D4" in system["content"]
    assert '"scenario":"button_led"' in system["content"]
    assert user == {"role": "user", "content": "drive GPIO17 high"}
    assistant = second["messages"][2]
    assert [c["function"]["name"] for c in assistant["tool_calls"]] == ["set_mode", "write"]
    assert [m["tool_call_id"] for m in tool_messages(second)] == ["c1", "c2"]
    assert json.loads(tool_messages(second)[1]["content"])["result"]["observed"]["level"] == 1
    assert len(third["messages"]) == 7
    writes = [e for e in session.events if e["kind"] == "request.stage" and e.get("op") == "pin.write"]
    assert writes and writes[0]["actor"] == "agent"


def test_agent_feeds_tool_errors_back_to_the_model(rig):
    session, tools = rig(policy="ask")
    client, completions = fake_client(
        say(None, call("c1", "write", "{not json"), call("c2", "frobnicate", {}),
            call("c3", "write", {"pin": 17, "level": 1})),
        say("Please confirm GPIO17 is wired first."))
    out = run_agent(tools, "set GPIO17 high", {}, client=client)
    bad_json, unknown, gated = out["results"]
    assert not any(r["ok"] for r in out["results"])
    assert bad_json["error"]["code"] == "bad_request" and "JSON" in bad_json["error"]["msg"]
    assert unknown["error"]["code"] == "unknown_tool"
    assert gated["error"]["code"] == "wiring_required" and gated["error"]["pin"] == 17
    assert 17 in session.pending_wiring
    fed = [json.loads(m["content"]) for m in tool_messages(completions.requests[1])]
    assert [f["error"]["code"] for f in fed] == ["bad_request", "unknown_tool", "wiring_required"]
    assert out["reply"] == "Please confirm GPIO17 is wired first."


def test_agent_stops_at_the_turn_limit_and_uses_openai_model(rig, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    _, tools = rig()
    client, completions = fake_client(*[say(None, call(f"c{i}", "note", {"text": f"step {i}"}))
                                        for i in range(3)])
    out = run_agent(tools, "keep going", {}, client=client, max_turns=2)
    assert [r["ok"] for r in out["results"]] == [True, True]
    assert "2 model turns" in out["reply"]
    assert [r["model"] for r in completions.requests] == ["gpt-test", "gpt-test"]


def test_model_api_errors_raise_until_a_tool_has_run(rig):
    _, tools = rig()
    client, _ = fake_client(ConnectionError("network down"))
    with pytest.raises(ConnectionError):
        run_agent(tools, "hi", {}, client=client)
    client, _ = fake_client(say(None, call("c1", "note", {"text": "x"})), ConnectionError("down"))
    out = run_agent(tools, "note x", {}, client=client)
    assert out["results"][0]["ok"] and "ConnectionError" in out["reply"]


def test_results_are_json_safe_and_size_capped():
    class StubTools:
        def call(self, tool, /, **args):
            return {"raw": b"\x00\xff", "pins": (1, 2)} if tool == "read" else {"blob": "x" * 50_000}

    client, completions = fake_client(say(None, call("c1", "read", {"pin": 1}),
                                          call("c2", "capture", {})), say("done"))
    out = run_agent(StubTools(), "go", {}, client=client)
    small, big = (r["result"] for r in out["results"])
    assert small == {"raw": "AP8=", "pins": [1, 2]}
    assert big["truncated"] is True and len(big["preview"]) == MAX_RESULT_CHARS
    json.dumps(out)
    assert all(len(m["content"]) < MAX_RESULT_CHARS + 200 for m in tool_messages(completions.requests[1]))


def test_every_array_schema_sent_to_the_model_has_items(rig):
    _, tools = rig()
    client, completions = fake_client(say("hi"))
    run_agent(tools, "hi", {}, client=client)

    def arrays(node):
        if isinstance(node, list):
            for item in node:
                yield from arrays(item)
        elif isinstance(node, dict):
            if node.get("type") == "array":
                yield node
            for value in node.values():
                yield from arrays(value)

    found = list(arrays(completions.requests[0]["tools"]))
    assert found and all("items" in schema for schema in found)


# --- console prompt routing ------------------------------------------------------------------
def test_prompt_without_a_key_keeps_the_offline_interpreter(app, monkeypatch):
    monkeypatch.setattr(server, "run_agent", lambda *a, **k: pytest.fail("no key: agent must not run"))
    app.connect({"transport": "virtual", "scenario": "button_led", "realtime": False})
    r = app.prompt({"prompt": "set GPIO17 high"})
    assert r["reply"].startswith("GPIO17 driven to 1") and r["results"][-1]["ok"]
    assert app.state()["llm"] is False


def test_prompt_falls_back_offline_when_the_agent_fails(app, monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(server, "run_agent", boom)
    app.connect({"transport": "virtual", "scenario": "button_led", "realtime": False})
    r = app.prompt({"prompt": "set GPIO17 high"})
    assert r["reply"].startswith(f"AI unavailable (ConnectionError); {OFFLINE}GPIO17 driven to 1")
    notes = [e["text"] for e in app.session.events if e["kind"] == "note"]
    assert notes[-2:] == ["set GPIO17 high", r["reply"]]
    st = app.state()
    assert st["llm"] is True and st["busy"] is False


def test_prompt_falls_back_when_the_openai_sdk_is_missing(app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "openai", None)
    r = app.prompt({"prompt": "what can you do"})
    assert r["reply"].startswith(f"AI unavailable (openai package not installed); {OFFLINE}I can")


def test_prompt_runs_the_agent_without_holding_the_console_lock(app, monkeypatch):
    app.connect({"transport": "virtual", "scenario": "button_led", "realtime": False})
    app.prompt({"prompt": "what can you do"})  # an earlier (offline) exchange the model should see
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def fake_agent(tools, text, context):
        probe = threading.Thread(target=lambda: seen.update(state=app.state()))
        probe.start()
        probe.join(2)
        with pytest.raises(ApiError) as exc:
            app.prompt({"prompt": "again"})
        seen.update(context=context, text=text, second=exc.value.status)
        result = tools.call("set_mode", pin=17, mode="output", initial=1)
        return {"reply": "GPIO17 is high.", "results": [{"tool": "set_mode", "ok": True, "result": result}]}

    monkeypatch.setattr(server, "run_agent", fake_agent)
    r = app.prompt({"prompt": "  drive GPIO17 high  "})
    assert r["reply"] == "GPIO17 is high." and r["results"][0]["ok"]
    assert seen["state"]["busy"] is True and seen["second"] == 409
    assert seen["text"] == "drive GPIO17 high"
    context = seen["context"]
    assert context["connected"] and context["transport"]["kind"] == "virtual"
    assert [n["actor"] for n in context["recent_notes"]] == ["tester", "agent"]
    notes = [(e["actor"], e["text"]) for e in app.session.events if e["kind"] == "note"]
    assert notes[-2:] == [("tester", "drive GPIO17 high"), ("agent", "GPIO17 is high.")]
    st = app.state()
    assert st["busy"] is False and st["pins"]["17"]["level"] == 1


# --- keepalive and reconnect -------------------------------------------------------------------
def test_keepalive_holds_control_past_a_short_watchdog(app):
    app.connect({"transport": "virtual", "scenario": "button_led", "realtime": True})
    transport = app.session.transport
    with app.lock:  # through the Session, as the console does, so its keepalive learns the period
        assert app.session.request("control.take", "system", token=transport.control_token,
                                   watchdog_s=0.5).ok
    time.sleep(1.5)
    assert app.state()["connection"]["controller"] is True  # state() pumps the rig's watchdog check
    events = app.session.events
    assert "control.lost" not in [e["kind"] for e in events]
    assert not [e for e in events if e["kind"] == "request.stage" and e.get("op") == "ping"]


def test_keepalive_retakes_control_lost_to_the_watchdog(app):
    app.connect({"transport": "virtual", "scenario": "button_led", "realtime": True})
    transport = app.session.transport
    with app.lock:  # starve the keepalive so the watchdog fires
        assert transport.take_control(watchdog_s=0.2).ok
        time.sleep(0.4)
        assert app.state()["connection"]["controller"] is False
    assert wait_until(lambda: any(e["kind"] == "note" and e["text"] == RETAKEN for e in app.session.events))
    assert transport.control_token
    assert [e["kind"] for e in app.session.events].count("control.lost") == 1


def test_connect_after_disconnect_starts_a_fresh_session(app):
    first = app.connect({"transport": "virtual", "scenario": "uart_echo", "realtime": False})
    assert app.disconnect()["connection"]["connected"] is False
    second = app.connect({"transport": "virtual", "scenario": "uart_echo", "realtime": False})
    assert second["connection"]["connected"] and second["connection"]["controller"]
    assert second["session"]["id"] != first["session"]["id"]
