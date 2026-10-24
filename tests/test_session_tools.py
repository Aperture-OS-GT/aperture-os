import json

import pytest

from aperture.report import render_report
from aperture.requests import LifecycleError, Request
from aperture.session import Session, dump_session, list_sessions, load_session
from aperture.tools import SPECS, ToolError, Tools, schemas, validate_args
from aperture.transport import TransportError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("APERTURE_HOME", str(tmp_path))
    return tmp_path


def connected(home, scenario="button_led", policy="off"):
    session = Session(actor="austin", home=home)
    tools = Tools(session, wiring_policy=policy)
    tools.call("connect", transport="virtual", scenario=scenario)
    return session, tools


def stages(session, req_id):
    return [e["stage"] for e in session.events if e["kind"] == "request.stage" and e["req"] == req_id]


def test_request_lifecycle_rules():
    request = Request("r1", "pin.write", {"pin": 17, "level": 1}, "agent")
    assert request.stage is None
    request.advance("requested", t_host_us=0)
    with pytest.raises(LifecycleError):
        request.advance("observed", t_host_us=1)
    with pytest.raises(LifecycleError):
        request.advance("requested", t_host_us=1)
    request.advance("executed", t_host_us=5, ack="implicit", ok=True, result={"level": 1})
    assert request.ok and request.stage == "executed" and request.duration_us("requested", "executed") == 5
    request.advance("observed", t_host_us=9, observe="readback", level=1, mismatch=False)
    assert request.as_dict()["stage"] == "observed"
    with pytest.raises(LifecycleError):
        request.advance("bogus")


def test_every_tool_has_schema_and_implementation():
    names = {s["function"]["name"] for s in schemas()}
    assert names == set(SPECS)
    for name in SPECS:
        assert callable(getattr(Tools, name)), name
    schema = next(s for s in schemas() if s["function"]["name"] == "write")["function"]["parameters"]
    assert schema["required"] == ["pin", "level"] and schema["properties"]["level"]["enum"] == [0, 1]
    assert validate_args("set_mode", {"pin": 17, "mode": "output"}) == {"pin": 17, "mode": "output"}
    for bad in ({"pin": "17", "mode": "output"}, {"pin": 17, "mode": "tx"}, {"pin": 17},
                {"pin": 17, "mode": "output", "x": 1}):
        with pytest.raises(ToolError):
            validate_args("set_mode", bad)
    with pytest.raises(ToolError):
        validate_args("nope", {})


def test_connect_records_four_stage_lifecycle(home):
    session, _ = connected(home)
    kinds = [e["kind"] for e in session.events]
    assert kinds[0] == "request.stage" and "session.start" in kinds
    assert stages(session, "r0") == ["requested", "acknowledged", "executed", "observed"]  # hello
    assert stages(session, "r1") == ["requested", "acknowledged", "executed", "observed"]  # control.take
    assert session.manifest["bridge"] == "virtual-rig" and session.transport.control_token
    seqs = [e["seq"] for e in session.events]
    assert seqs == list(range(1, len(seqs) + 1))
    assert all(a["t_host_us"] <= b["t_host_us"] for a, b in zip(session.events, session.events[1:]))


def test_write_readback_observation_and_errors(home):
    session, tools = connected(home)
    tools.call("set_mode", pin=27, mode="input")
    result = tools.call("set_mode", pin=17, mode="output", initial=0)
    assert result["stage"] == "observed" and result["level"] == 0
    written = tools.call("write", pin=17, level=1)
    assert written["level"] == 1 and written["observed"]["mismatch"] is False
    request = session.requests[written["request"]]
    assert stages(session, request.id) == ["requested", "acknowledged", "executed", "observed"]
    assert request.stages["acknowledged"]["t_bridge_us"] <= request.stages["executed"]["t_bridge_us"]
    with pytest.raises(ToolError) as info:
        tools.call("write", pin=27, level=1)
    assert info.value.code == "pin_mode"
    failed = session.requests[info.value.extra["request"]]
    assert failed.stage == "observed" and failed.err == "pin_mode" and failed.observation["result"] == "failed"
    assert tools.call("read", pin=17)["level"] == 1
    assert tools.call("read", pins=[17, 27])["levels"] == {17: 1, 27: 0}
    with pytest.raises(ToolError):
        tools.call("read")
    pulse = tools.call("pulse", pin=17, level=0, us=1000)
    assert len(pulse["edges"]) == 2
    listing = tools.call("list_pins")
    p17 = next(p for p in listing["pins"] if p["pin"] == 17)
    assert p17["phys"] == 11 and p17["mode"] == "output" and listing["controller"] == "aperture-host"


def test_mismatch_is_flagged(home, monkeypatch):
    session, tools = connected(home)
    tools.call("set_mode", pin=17, mode="output", initial=0)
    real = session.transport.request

    def flaky(op, **args):
        response = real(op, **args)
        if op == "pin.read":
            response.result["level"] = 0
        return response

    monkeypatch.setattr(session.transport, "request", flaky)
    written = tools.call("write", pin=17, level=1)
    assert written["observed"]["mismatch"] is True


def test_wiring_gate(home):
    session, tools = connected(home, scenario="esp32_safety")  # scenario policy = ask
    tools.wiring_policy = None
    with pytest.raises(ToolError) as info:
        tools.call("set_mode", pin=17, mode="output")
    assert info.value.code == "wiring_required" and 17 in session.pending_wiring
    assert session.events[-1]["kind"] == "wiring.request"
    with pytest.raises(ValueError):
        session.confirm_wiring(22, True)
    session.confirm_wiring(17, False)
    assert session.events[-1]["kind"] == "wiring.rejected" and 17 not in session.wiring
    tools.call("request_wiring", pin=17, dut_pin="GPIO4", component="trigger", reason="drive trigger", dir="out")
    session.confirm_wiring(17, True, by="austin")
    assert session.wiring[17]["component"] == "trigger" and session.wiring[17]["by"] == "austin"
    assert tools.call("set_mode", pin=17, mode="output", initial=1)["level"] == 1
    with pytest.raises(ToolError):
        tools.call("uart_open", tx=14, rx=15, baud=115200)


def test_wait_for_and_button(home):
    session, tools = connected(home)
    tools.call("set_mode", pin=17, mode="input")
    tools.call("set_mode", pin=22, mode="input")
    with pytest.raises(ToolError) as info:
        tools.call("wait_for", pin=17, edge="rising", timeout_ms=5)
    assert info.value.code == "timeout" and session.events[-1]["kind"] == "wait.timeout"
    t_press = session.transport.rig.now_us()
    tools.call("dut_action", behavior="button", action="press")
    hit = tools.call("wait_for", pin=17, edge="rising", timeout_ms=50)
    assert hit["pass"] and hit["level"] == 1 and hit["t_bridge_us"] - t_press == 12_000
    tools.call("dut_action", behavior="button", action="release")
    assert tools.call("wait_for", pin=17, edge="falling", timeout_ms=50)["level"] == 0
    assert tools.call("assert_pin", pin=17, level=0)["pass"] and session.verdict is True
    assert not tools.call("assert_pin", pin=17, level=1, name="LED on")["pass"] and session.verdict is False
    with pytest.raises(ToolError):
        tools.call("dut_action", behavior="nope", action="press")
    with pytest.raises(ToolError):
        tools.call("dut_action", behavior="button", action="_settle")
    tools.call("dut_action", behavior="dut", action="reset")
    changes = [e for e in session.events if e["kind"] == "pin.change"]
    assert changes and all(e["source"] == "stream" and e["actor"] == "rig" for e in changes)


def test_capture_and_assert_timeline(home):
    _, tools = connected(home)
    tools.call("set_mode", pin=18, mode="pwm")
    tools.call("pwm", pin=18, freq_hz=100, duty=0.5)
    cap = tools.call("capture", pins=[18], duration_ms=25, rate_hz=10_000)
    assert cap["capture_id"] == 1 and len(cap["edges"]) >= 4 and cap["edges"][0][0] >= 0
    rising = [e for e in cap["edges"] if e[2] == 1]
    expected = [[rising[0][0], 18, 1], [rising[0][0] + 5000, 18, 0], [rising[0][0] + 10_000, 18, 1]]
    assert tools.call("assert_timeline", edges=expected, tolerance_us=200, allow_extra=True)["pass"]
    strict = tools.call("assert_timeline", edges=expected, tolerance_us=200)
    assert not strict["pass"] and strict["extra"]
    missing = tools.call("assert_timeline", edges=[[0, 22, 1]], tolerance_us=10, allow_extra=True)
    assert not missing["pass"] and missing["missing"] == [[0, 22, 1]]
    with pytest.raises(ToolError):
        tools.call("assert_timeline", edges=[[0, 18]], tolerance_us=1)
    with pytest.raises(ToolError):
        tools.call("assert_timeline", edges=[], tolerance_us=1, capture_id=99)


def test_uart_tools(home):
    session, tools = connected(home, scenario="uart_echo")
    port = tools.call("uart_open", tx=14, rx=15, baud=115200)["port"]
    boot = tools.call("uart_expect", port=port, pattern=r"BOOT (\w+)", timeout_ms=1000)
    assert boot["match"] == "BOOT OK" and boot["groups"] == ["OK"]
    tools.call("uart_send", port=port, data="ping\\n")
    assert tools.call("uart_expect", port=port, pattern="pong", timeout_ms=200)["pass"]
    tools.call("uart_send", port=port, data="hex:4142")
    assert tools.call("uart_expect", port=port, pattern="AB", timeout_ms=200)["pass"]
    with pytest.raises(ToolError) as info:
        tools.call("uart_expect", port=port, pattern="never", timeout_ms=20)
    assert info.value.code == "timeout"
    with pytest.raises(ToolError):
        tools.call("uart_expect", port=port, pattern="(", timeout_ms=1)
    with pytest.raises(ToolError):
        tools.call("uart_send", port=port, data="hex:zz")
    bus = [e for e in session.events if e["kind"] == "bus.bytes"]
    assert {e["dir"] for e in bus} == {"tx", "rx"}
    assert "".join(e["text"] or "" for e in bus if e["dir"] == "rx").startswith("BOOT OK")
    tools.call("uart_close", port=port)


def test_bus_tools(home):
    _, tools = connected(home, scenario="esp32_safety")
    assert tools.call("i2c_scan", bus=1)["hex"] == ["0x68"]
    assert tools.call("i2c_read", bus=1, addr=0x68, n=1, reg=0x75)["hex"] == "68"
    tools.call("i2c_write", bus=1, addr=0x68, data="3b7f")
    assert tools.call("i2c_read", bus=1, addr=0x68, n=1)["hex"] == "7f"
    assert tools.call("spi_xfer", bus=0, cs=0, data="9f00")["hex"] == "0000"


def test_control_tools_and_disconnect(home):
    session, tools = connected(home)
    assert tools.call("release_control")["stage"] == "observed" and not session.transport.control_token
    with pytest.raises(ToolError):
        tools.call("release_control")
    assert tools.call("take_control")["controller"] is True
    tools.call("set_mode", pin=17, mode="output", initial=1)
    tools.call("disconnect")
    assert session.ended["reason"] == "user" and session.events[-1]["kind"] == "session.end"
    assert session.transport.rig.pins[17].mode == "disabled"
    with pytest.raises(TransportError):
        tools.call("connect", transport="virtual")
    with pytest.raises(TransportError):
        Tools(Session(home=home, persist=False)).call("connect", transport="tcp", target="x:1")


def test_save_load_export_round_trip_and_report(home):
    session, tools = connected(home)
    tools.call("set_mode", pin=17, mode="input")
    tools.call("set_mode", pin=22, mode="output", initial=1)
    tools.call("note", text="pressing the button")
    tools.call("write", pin=22, level=0)
    tools.call("wait_for", pin=17, edge="rising", timeout_ms=50)
    tools.call("assert_pin", pin=17, level=1, name="LED follows button")
    tools.call("disconnect")
    folder = home / "sessions" / session.id
    assert (folder / "session.json").exists() and (folder / "journal.jsonl").exists()
    data = load_session(session.id)
    assert data["schema"] == 1 and data["verdict"] is True and data["wiring"] == {}
    assert dump_session(data) == (folder / "session.json").read_text()
    journal = [json.loads(line) for line in (folder / "journal.jsonl").read_text().splitlines()]
    assert journal == data["events"]
    listing = list_sessions()
    assert listing[0]["id"] == session.id and listing[0]["verdict"] is True
    assert listing[0]["scenario"] == "button_led"
    report = render_report(data)
    assert report == render_report(load_session(folder / "session.json"))
    for needle in ("## Verdict: PASS", "LED follows button", "`pin.write`", "## Pin activity",
                   "pressing the button", "virtual-rig", "button_led"):
        assert needle in report
    (folder / "session.json").write_text(json.dumps({**data, "schema": 99}))
    with pytest.raises(ValueError):
        load_session(session.id)


def test_report_handles_failures_and_bus(home):
    session, tools = connected(home, scenario="uart_echo")
    port = tools.call("uart_open", tx=14, rx=15, baud=115200)["port"]
    tools.call("uart_expect", port=port, pattern="BOOT OK", timeout_ms=1000)
    tools.call("uart_send", port=port, data="hello\\n")
    with pytest.raises(ToolError):
        tools.call("write", pin=5, level=1)
    tools.call("set_mode", pin=5, mode="input")
    tools.call("assert_pin", pin=5, level=1, name="never")
    session.close()
    report = render_report(session.to_dict())
    assert "## Verdict: FAIL" in report and "err:pin_mode" in report
    assert "## Bus transcript" in report and "BOOT OK" in report and "hello" in report
    assert "host → DUT" in report and "DUT → host" in report
