import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pi.backends import SimBackend
from pi.daemon import Harness
from ui.intent import interpret
from ui.server import ConsoleApp, HTTPServer


@pytest.fixture
def console(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = ConsoleApp()
    with HTTPServer(("127.0.0.1", 0), app) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}", app
        server.shutdown()
        thread.join()
    app.close()


def post(base, path, data):
    with urlopen(Request(base + path, json.dumps(data).encode(),
                         {"Content-Type": "application/json"}), timeout=10) as response:
        return json.load(response)


@pytest.mark.parametrize("prompt,expected", [
    ("Run full safety plan", None), ("Set PWM 90%", {"cmd": "set_pwm", "pin": 18, "duty": .9}),
    ("Set trigger high", {"cmd": "set_pin", "pin": 17, "val": 1}),
    ("Set trigger low", {"cmd": "set_pin", "pin": 17, "val": 0}),
    ("Inject fault", {"cmd": "inject_fault", "val": 1}),
    ("Clear fault", {"cmd": "inject_fault", "val": 0}),
    ("Read state", {"cmd": "read_state"}),
    ("Reset to idle", {"cmd": "run_vector", "trig": 0, "pwm": 0, "fault": 0}),
])
def test_intents(prompt, expected):
    command, reply = interpret(prompt)
    assert command == expected
    assert reply


@pytest.mark.parametrize("prompt", ["", "hello", "Set PWM 101%", "PWM -2%", "PWM"])
def test_bad_intents(prompt):
    with pytest.raises(ValueError):
        interpret(prompt)


def test_explicit_pin_mapping():
    backend = SimBackend()
    harness = Harness(backend)
    assert harness.execute({"cmd": "set_pin", "pin": 17, "val": 1})["injected"]["trig"] == 1
    assert harness.execute({"cmd": "set_pin", "pin": 27, "val": 1})["injected"]["fault"] == 1
    backend.apply(fault=0)
    assert not harness.execute({"cmd": "set_pin", "pin": 22, "val": 1})["pass"]
    assert backend.inputs["fault"] == 0


def test_http_plan_and_fixed(console):
    base, app = console
    result = post(base, "/api/prompt", {"prompt": "Run full safety plan"})
    assert not result["report"]["pass"]
    assert "DISCREPANCY" in result["report"]["steps"][2]["issues"][0]
    with urlopen(base + "/api/report") as response:
        assert json.load(response) == result["report"]
    assert app.measurement["telemetry"]["state"] == "IDLE"
    assert {event["level"] for event in app.events} >= {"TX", "RX", "USER", "AGENT", "STEP", "REPORT"}
    rx = next(event for event in app.events if event["level"] == "RX")
    assert rx["latency_ms"] >= 0 and rx["timestamp"]
    connected = post(base, "/api/connect", {"mode": "sim", "demo_bug": False})
    assert connected["connection"]["connected"]
    assert post(base, "/api/prompt", {"prompt": "Run full safety plan"})["report"]["pass"]


def test_http_assets_state_and_sse(console):
    base, _ = console
    for path, content in [("/", b"Aperture OS Console"), ("/style.css", b"--accent"),
                          ("/app.js", b"EventSource")]:
        with urlopen(base + path) as response:
            assert content in response.read()
    with urlopen(base + "/api/state") as response:
        assert json.load(response)["connection"]["mode"] == "sim"
    with urlopen(base + "/api/events", timeout=5) as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.readline().startswith(b"id: ")
        assert json.loads(response.readline().removeprefix(b"data: "))["level"] == "TX"


def test_http_errors(console):
    base, app = console
    for data in ({"prompt": ""}, {"prompt": "PWM 200%"}, {"prompt": []}, []):
        with pytest.raises(HTTPError) as exc:
            post(base, "/api/prompt", data)
        assert exc.value.code == 400
    with pytest.raises(HTTPError) as exc:
        post(base, "/api/connect", {"mode": "bad"})
    assert exc.value.code == 400
    with pytest.raises(HTTPError) as exc:
        urlopen(base + "/missing")
    assert exc.value.code == 404
    app.lock.acquire()
    try:
        with pytest.raises(HTTPError) as exc:
            post(base, "/api/prompt", {"prompt": "Read state"})
        assert exc.value.code == 409
    finally:
        app.lock.release()
    with pytest.raises(HTTPError) as exc:
        urlopen(Request(base + "/api/prompt", b'{}', {"Origin": "http://evil.example"}))
    assert exc.value.code == 403
