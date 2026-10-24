"""MCP server (rig tools over MCP) and the OpenAI agent that drives hardware through it."""
import json
from types import SimpleNamespace as NS

import anyio
import pytest
from mcp import Client

from aperture.agent import RigAgent, ToolCall, mcp_tools_to_openai, plan_from_calls
from aperture.mcp_server import RigHost, build_server, tool_definitions
from aperture.server import ConsoleApp
from aperture.tools import SPECS


def run(coro_fn):
    return anyio.run(coro_fn)


def test_every_rig_tool_is_published_over_mcp():
    names = {t.name for t in tool_definitions()}
    assert set(SPECS) <= names and {"save_plan", "run_plan", "list_plans", "session_log"} <= names


def test_mcp_drives_the_rig_with_lifecycle(tmp_path):
    host = RigHost(home=str(tmp_path))

    async def go():
        async with Client(build_server(host)) as c:
            listed = await c.list_tools()
            assert len(listed.tools) == len(SPECS) + 4
            r = await c.call_tool("connect", {"transport": "virtual", "scenario": "loopback"})
            assert not r.is_error and r.structured_content["manifest"]["proto"] == 1
            await c.call_tool("set_mode", {"pin": 27, "mode": "input"})
            await c.call_tool("set_mode", {"pin": 17, "mode": "output", "initial": 0})
            await c.call_tool("write", {"pin": 17, "level": 1})
            ok = await c.call_tool("assert_pin", {"pin": 27, "level": 1})
            assert not ok.is_error and ok.structured_content["pass"] is True
            bad = await c.call_tool("write", {"pin": 17})
            assert bad.is_error and json.loads(bad.content[0].text)["error"]["code"] == "bad_request"
            log = await c.call_tool("session_log", {"kind": "request.stage", "limit": 500})
            stages = {e["stage"] for e in log.structured_content["events"]}
            assert {"requested", "acknowledged", "executed", "observed"} <= stages
    run(go)
    host.close()


def test_mcp_plan_tools_save_and_run_without_agent(tmp_path):
    host = RigHost(home=str(tmp_path))
    plan = {"name": "loop check", "steps": [
        {"tool": "set_mode", "args": {"pin": 27, "mode": "input"}},
        {"tool": "set_mode", "args": {"pin": 17, "mode": "output", "initial": 1}},
        {"tool": "assert_pin", "args": {"pin": 27, "level": 1}, "assert": True}],
        "cleanup": [{"tool": "release_all", "args": {}}]}

    async def go():
        async with Client(build_server(host)) as c:
            await c.call_tool("connect", {"transport": "virtual", "scenario": "loopback"})
            saved = await c.call_tool("save_plan", plan)
            assert saved.structured_content["slug"] == "loop-check"
            listed = await c.call_tool("list_plans", {})
            assert any(p["slug"] == "loop-check" for p in listed.structured_content["plans"])
            result = await c.call_tool("run_plan", {"slug": "loop-check"})
            assert result.structured_content["pass"] is True
            missing = await c.call_tool("run_plan", {"slug": "nope"})
            assert missing.is_error
    run(go)
    host.close()


class FakeLLM:
    """Scripted stand-in for the OpenAI Chat Completions API."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self.script.pop(0)
        if isinstance(step, str):
            message = NS(content=step, tool_calls=None)
        else:
            message = NS(content=None, tool_calls=[
                NS(id=f"c{i}", function=NS(name=name, arguments=json.dumps(args))) for i, (name, args) in
                enumerate(step)])
        return NS(choices=[NS(message=message)])


SCRIPT = [
    [("connect", {"transport": "virtual", "scenario": "loopback"})],
    [("set_mode", {"pin": 27, "mode": "input"}), ("set_mode", {"pin": 17, "mode": "output", "initial": 0})],
    [("write", {"pin": 17, "level": 1})],
    [("assert_pin", {"pin": 27, "level": 1, "name": "loopback high"})],
    [("release_all", {})],
    "PASS\nloopback high: GPIO27 followed GPIO17",
]


def test_openai_agent_uses_mcp_tools_and_saves_plan(tmp_path):
    host = RigHost(home=str(tmp_path))
    llm = FakeLLM(SCRIPT)
    steps = []
    result = RigAgent(build_server(host), llm=llm, model="test-model", on_step=steps.append).run(
        "GPIO27 must follow GPIO17", save_as="follow")
    assert result.verdict is True and result.reply.startswith("PASS")
    tools_sent = {t["function"]["name"] for t in llm.requests[0]["tools"]}
    assert set(SPECS) <= tools_sent and llm.requests[0]["model"] == "test-model"
    assert llm.requests[1]["messages"][-1]["role"] == "tool"
    assert [s["tool"] for s in steps] == ["connect", "set_mode", "set_mode", "write", "assert_pin", "release_all"]
    assert all(s["ok"] for s in steps)
    assert [s["tool"] for s in result.plan["steps"]] == ["set_mode", "set_mode", "write", "assert_pin"]
    assert result.saved["slug"] == "follow"
    actors = {e.get("actor") for e in host.get_tools().session.events if e["kind"] == "request.stage"}
    assert "agent" in actors or any(str(a).startswith("agent") for a in actors)
    host.close()


def test_agent_reports_tool_errors_back_to_the_model(tmp_path):
    host = RigHost(home=str(tmp_path))
    llm = FakeLLM([[("write", {"pin": 17, "level": 1})], "FAIL\nnot connected"])
    result = RigAgent(build_server(host), llm=llm).run("set 17 high")
    assert result.verdict is False and result.calls[0].ok is False
    assert "error" in llm.requests[1]["messages"][-1]["content"]
    assert result.plan is None


def test_agent_step_budget():
    llm = FakeLLM([[("list_pins", {})]] * 3)
    result = RigAgent(build_server(RigHost()), llm=llm, max_steps=3).run("loop forever")
    assert result.verdict is False and "stopped after 3" in result.reply


def test_plan_from_calls_marks_assertions_and_wiring():
    plan = plan_from_calls("x", "src", [
        ToolCall("connect", {"transport": "virtual"}, True, {}),
        ToolCall("set_mode", {"pin": 22, "mode": "output"}, True, {}),
        ToolCall("set_mode", {"pin": 17, "mode": "input"}, True, {}),
        ToolCall("pulse", {"pin": 22, "us": 1000}, True, {}),
        ToolCall("write", {"pin": 5, "level": 1}, False, {}),
        ToolCall("wait_for", {"pin": 17, "edge": "rising", "timeout_ms": 50}, True, {})])
    assert [s["tool"] for s in plan["steps"]] == ["set_mode", "set_mode", "pulse", "wait_for"]
    assert plan["steps"][-1]["assert"] is True
    assert plan["requires"]["wiring"] == [{"pin": 17, "dir": "in"}, {"pin": 22, "dir": "out"}]
    assert mcp_tools_to_openai([NS(name="a", description=None, input_schema=None)])[0]["function"]["name"] == "a"


def test_console_prompt_runs_openai_agent_over_shared_session(tmp_path):
    llm = FakeLLM(SCRIPT)
    app = ConsoleApp(home=str(tmp_path), llm=True, agent_factory=lambda server: RigAgent(server, llm=llm))
    try:
        assert app.state()["llm"] is True
        r = app.prompt({"prompt": "GPIO27 must follow GPIO17"})
        assert r["llm"] and r["verdict"] is True and r["plan"]["steps"]
        assert app.state()["connection"]["connected"]  # the agent connected the console's own session
        kinds = [e["kind"] for e in app.session.events]
        assert "request.stage" in kinds
        assert any(f.get("kind") == "plan.compiled" for f in app.feed)
        rules = app.prompt({"prompt": "what can you do", "mode": "rules"})
        assert "llm" not in rules
    finally:
        app.close()


def test_console_reports_agent_failure(tmp_path):
    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("401 invalid api key")
    app = ConsoleApp(home=str(tmp_path), llm=True, agent_factory=lambda server: Boom())
    try:
        with pytest.raises(Exception, match="agent failed"):
            app.prompt({"prompt": "anything"})
        assert app.busy is False
    finally:
        app.close()
