import json

import pytest

from aperture import cli
from aperture.plans import (
    PlanError,
    PlanRunner,
    compile_plan,
    list_plans,
    load_plan,
    save_plan,
    validate_plan,
)
from aperture.plans.schema import dump_plan
from aperture.session import Session, load_session
from aperture.tools import Tools

REFERENCE = ("confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22; "
             "verify the ESP32 prints BOOT OK over UART")
CONTEXT = {"wiring": {"22": {"dut_pin": "BTN", "component": "button"}, "17": {"dut_pin": "LED", "component": "led"}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("APERTURE_HOME", str(tmp_path))
    return tmp_path


def run_plan(home, plan, scenario, policy="off"):
    session = Session(actor="t", home=home)
    tools = Tools(session, wiring_policy=policy)
    tools.call("connect", transport="virtual", scenario=scenario)
    result = PlanRunner(tools, plan).run()
    session.close()
    return session, result


def test_validate_plan_errors():
    good = {"name": "x", "steps": [{"tool": "note", "args": {"text": "hi"}}]}
    plan = validate_plan(good)
    assert plan["slug"] == "x" and plan["steps"][0]["id"] == "s1" and plan["cleanup"] == []
    for bad, needle in [({}, "name"), ({"name": "x"}, "no steps"), ({"name": "x", "steps": "no"}, "list"),
                        ({"name": "x", "steps": [{"tool": "nope"}]}, "unknown tool"),
                        ({"name": "x", "steps": [{"tool": "write", "args": {"pin": 17}}]}, "missing"),
                        ({"name": "x", "steps": [{"id": "a", "tool": "note", "args": {"text": "1"}},
                                                 {"id": "a", "tool": "note", "args": {"text": "2"}}]}, "unique"),
                        ({"name": "x", "steps": [{"tool": "note", "args": {"text": "1"}, "on_fail": "x"}]}, "on_fail"),
                        ({"name": "x", "schema": 2, "steps": [{"tool": "note", "args": {"text": "1"}}]}, "schema"),
                        ({"name": "x", "requires": {"wiring": [{"pin": "17"}]},
                          "steps": [{"tool": "note", "args": {"text": "1"}}]}, "wiring")]:
        with pytest.raises(PlanError) as info:
            validate_plan(bad)
        assert needle in str(info.value)


def test_compile_reference_sentence_matches_example_and_is_deterministic():
    plan = compile_plan(REFERENCE, CONTEXT, name="Boot OK")
    assert [s["tool"] for s in plan["steps"]] == ["set_mode", "set_mode", "write", "wait_for", "write", "uart_open",
                                                  "uart_expect"]
    assert plan["steps"][3]["assert"] and plan["steps"][3]["args"] == {"pin": 17, "edge": "rising", "timeout_ms": 50}
    assert plan["steps"][6]["args"]["pattern"] == "BOOT\\ OK"
    assert [w["pin"] for w in plan["requires"]["wiring"]] == [14, 15, 17, 22]
    assert plan["requires"]["uart"] == [{"tx": 14, "rx": 15, "baud": 115200}] and "needs" not in plan
    assert dump_plan(plan) == dump_plan(compile_plan(REFERENCE, CONTEXT, name="Boot OK"))
    example = load_plan("boot-ok")
    plan["requires"]["scenario"] = "button_led"
    assert dump_plan(validate_plan(plan)) == dump_plan(example)


def test_compile_other_patterns_and_needs():
    plan = compile_plan("set GPIO5 high; pulse GPIO6 low for 20 ms; expect GPIO4 is high; GPIO27 follows GPIO17; "
                        "measure frequency on GPIO24 is 2 Hz ± 0.2; reset the DUT; dance")
    tools = [s["tool"] for s in plan["steps"]]
    assert tools[:3] == ["set_mode", "write", "set_mode"] and "pulse" in tools and "assert_frequency" in tools
    assert "dut_action" in tools and plan["needs"] == ["could not understand: dance"]
    unresolved = compile_plan("confirm GPIO17 goes high within 50 ms of pressing the button")
    assert unresolved["needs"] == ["the button"] and [s["tool"] for s in unresolved["steps"]] == ["set_mode"]
    by_name = compile_plan("press the button", {"names": {"button": 22}})
    assert by_name["steps"][-1]["args"] == {"pin": 22, "level": 0, "us": 100000}
    assert compile_plan("")["steps"][0]["tool"] == "note"


def test_runner_passes_on_button_led_and_records_events(home):
    session, result = run_plan(home, load_plan("boot-ok"), "button_led")
    assert result["pass"] and result["failed_steps"] == [] and not result["aborted"]
    kinds = [e["kind"] for e in session.events]
    assert kinds.count("plan.step") == 8 and "plan.start" in kinds and "plan.end" in kinds
    assert session.verdict is True and len(session.assertions) == 2
    assert session.transport.rig.pins[22].mode == "disabled"  # cleanup released everything
    saved = load_session(session.id)
    assert saved["verdict"] is True


def test_runner_blocks_on_missing_wiring_and_stops_on_error(home):
    session, result = run_plan(home, load_plan("boot-ok"), "button_led", policy="ask")
    assert not result["pass"] and [w["pin"] for w in result["blocked"]] == [14, 15, 17, 22]
    assert any(e["kind"] == "plan.blocked" for e in session.events)
    broken = {"name": "broken", "steps": [{"tool": "write", "args": {"pin": 17, "level": 1}},
                                          {"tool": "note", "args": {"text": "unreachable"}}]}
    session, result = run_plan(home, broken, "empty")
    assert not result["pass"] and result["aborted"] and result["failed_steps"] == ["s1"]
    assert result["steps"][0]["error"]["code"] == "pin_mode" and len(result["steps"]) == 1


@pytest.mark.parametrize("scenario,expected", [("esp32_safety", False), ("esp32_safety_fixed", True)])
def test_esp32_safety_plan_detects_planted_bug(home, scenario, expected):
    verdicts = []
    for _ in range(3):
        session, result = run_plan(home, load_plan("esp32-safety"), scenario)
        verdicts.append(result["pass"])
        names = {a["name"]: a["pass"] for a in session.assertions}
        assert names["Boot banner"] and names["Nominal: actuator runs"] and names["Recovery: relay re-engaged"]
        assert names["Fault while trigger HIGH: safety relay released"] is expected
        assert names["Telemetry claims safety=0"]  # the DUT lies about the relay when the bug is planted
    assert verdicts == [expected] * 3


def test_save_list_load(home):
    plan = compile_plan("set GPIO5 high", name="My Plan")
    path = save_plan(plan, home)
    assert path.name == "my-plan.json" and load_plan("my-plan")["name"] == "My Plan"
    listing = list_plans()
    assert {p["slug"] for p in listing} >= {"my-plan", "boot-ok", "esp32-safety"}
    assert next(p for p in listing if p["slug"] == "my-plan")["origin"] == "user"
    with pytest.raises(FileNotFoundError):
        load_plan("missing")
    assert load_plan(path)["slug"] == "my-plan"


def test_cli(home, capsys):
    assert cli.main(["plan", "run", "esp32-safety", "--scenario", "esp32_safety_fixed"]) == 0
    assert cli.main(["plan", "run", "esp32-safety", "--scenario", "esp32_safety", "--out", str(home / "out")]) == 1
    out = capsys.readouterr().out
    assert "FAIL s16  assert_pin (assert)" in out and (home / "out" / "report.md").exists()
    assert (home / "out" / "session.json").exists()
    assert cli.main(["plan", "run", "boot-ok", "--scenario", "button_led", "--wiring-policy", "ask"]) == 2
    wiring = home / "wiring.json"
    wiring.write_text(json.dumps({str(p): {"dut_pin": "x", "component": "y", "dir": "out"} for p in (14, 15, 17, 22)}))
    assert cli.main(["plan", "run", "boot-ok", "--scenario", "button_led", "--wiring-policy", "ask",
                     "--wiring", str(wiring)]) == 0
    assert cli.main(["plan", "list"]) == 0 and "boot-ok" in capsys.readouterr().out
    assert cli.main(["sessions", "list"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 4 and listed[0].split()[1] == "PASS"
    session_id = listed[0].split()[0]
    assert cli.main(["sessions", "export", session_id, "-o", str(home / "exp")]) == 0
    assert (home / "exp" / f"{session_id}.json").exists() and (home / "exp" / f"{session_id}.md").exists()
    assert cli.main(["rig", "scenarios"]) == 0 and "button_led" in capsys.readouterr().out
