import json
import sys
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agent.wiring import Wiring
from pi.backends import RealBackend, SimBackend
from pi.daemon import Harness
from pi.header import HEADER, Header
from pi.protocol import parse_command
from ui.intent import interpret
from ui.server import ConsoleApp, HTTPServer


def test_header_map():
    assert len(HEADER) == 40
    assert {i for i, pin in enumerate(HEADER, 1) if pin == "GND"} == {6, 9, 14, 20, 25, 30, 34, 39}
    assert {i for i, pin in enumerate(HEADER, 1) if pin == "3V3"} == {1, 17}
    assert {i for i, pin in enumerate(HEADER, 1) if pin == "5V"} == {2, 4}
    assert HEADER[26:28] == ("ID_SD", "ID_SC")
    assert HEADER[7] == 14 and HEADER[9] == 15
    header = Header()
    assert header.gpio(17)["physical"] == 11
    for _ in range(600):
        header.update([{"pin": 17, "value": 1}])
    assert len(header.history[17]) == 512
    with pytest.raises(ValueError):
        header.gpio(0)


def test_wiring_gates_and_rejection():
    events = []
    wiring = Wiring(lambda level, data: events.append((level, data)))
    wiring.select("mpu6050_gyro")
    assert wiring.missing() == [2, 3, 24]
    wiring.confirm_connection(2)
    wiring.confirm_connection(3, False)
    assert wiring.missing() == [3, 24]
    assert wiring.header.gpio(3)["request"] is None
    with pytest.raises(ValueError):
        wiring.confirm_connection(3)
    wiring.select("mpu6050_gyro")
    for pin in (2, 3, 24):
        wiring.confirm_connection(pin)
    assert wiring.status()["ready"]
    assert {level for level, _ in events} == {"REQUEST", "PIN_STATE"}


def test_generic_protocol_and_backend():
    harness = Harness(SimBackend(profile="display"))
    def execute(**command):
        response = harness.execute(command)
        assert response["pass"], response
        return response

    assert len(execute(cmd="list_pins")["pins"]) == 40
    execute(cmd="configure_pin", pin=17, direction="tx")
    assert execute(cmd="write", pin=17, val=1)["pins"][0]["value"] == 1
    assert execute(cmd="pwm", pin=17, duty=.4, freq=100)["pins"][0]["duty"] == .4
    execute(cmd="configure_pin", pin=24, direction="rx", pull="down")
    assert not harness.execute({"cmd": "write", "pin": 24, "val": 1})["pass"]
    result = execute(cmd="sample", pins=[17, 24], duration_ms=100, rate_hz=50)
    assert len(result["samples"]) == 5
    assert result["samples"][-1]["t_ms"] == 80
    execute(cmd="release", pin=17)
    assert not harness.execute({"cmd": "read", "pins": [17]})["pass"]


@pytest.mark.parametrize("command", [
    {"cmd": "write", "pin": 0, "val": 1}, {"cmd": "write", "pin": 17, "val": True},
    {"cmd": "read", "pins": []}, {"cmd": "read", "pins": [17, 17]},
    {"cmd": "read", "pins": [True]}, {"cmd": "configure_pin", "pin": 17, "direction": "out"},
    {"cmd": "configure_pin", "pin": 17, "direction": "tx", "pull": "bad"},
    {"cmd": "pwm", "pin": 17, "duty": .5, "freq": 0},
    {"cmd": "sample", "pins": [17], "duration_ms": 2000, "rate_hz": 1000},
    {"cmd": "sample", "pins": [17], "duration_ms": 0, "rate_hz": 10},
    {"cmd": "select_profile", "profile": "unknown"},
])
def test_generic_protocol_rejects(command):
    with pytest.raises(ValueError):
        parse_command(json.dumps(command))


@pytest.fixture
def universal(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = ConsoleApp(profile="mpu6050_gyro")
    with HTTPServer(("127.0.0.1", 0), app) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}", app
        server.shutdown()
        thread.join()
    app.close()


def api(base, path, data=None):
    request = Request(base + path, json.dumps(data).encode() if data is not None else None,
                      {"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


@pytest.mark.parametrize("profile,bug,passed", [
    ("esp32_safety", True, False), ("esp32_safety", False, True),
    ("mpu6050_gyro", True, True), ("display", True, True),
])
def test_all_profiles_http(universal, profile, bug, passed):
    base, app = universal
    api(base, "/api/connect", {"mode": "sim", "demo_bug": bug})
    selected = api(base, "/api/profiles/select", {"profile": profile})
    assert selected["profile"]["id"] == profile
    waiting = api(base, "/api/profiles/run", {})
    assert waiting["status"] == "waiting_for_connections" and not waiting["steps"]
    assert not app.sim.harness.backend.gpio_config
    requests = api(base, "/api/requests")["requests"]
    for request in requests:
        api(base, "/api/requests/confirm", {"pi_pin": request["pi_pin"]})
    report = api(base, "/api/profiles/run", {})
    assert report["status"] == "complete" and report["pass"] is passed, report
    if profile == "esp32_safety" and bug:
        assert "DISCREPANCY" in report["steps"][2]["issues"][0]
    if profile == "mpu6050_gyro":
        pins = api(base, "/api/header")["pins"]
        sda = next(pin for pin in pins if pin["bcm"] == 2)
        assert "wx" in sda["decoded"]
        interrupt = next(pin for pin in pins if pin["bcm"] == 24)
        assert interrupt["decoded"] == sda["decoded"]
        assert api(base, "/api/waveform?pin=24")["samples"]
    assert len(api(base, "/api/header")["pins"]) == 40
    assert len(api(base, "/api/profiles")["profiles"]) == 3


def test_requests_and_generic_http(universal):
    base, _ = universal
    assert api(base, "/api/prompt", {"prompt": "test the gyroscope"})["report"]["missing"]
    api(base, "/api/requests/reject", {"pi_pin": 24})
    assert len(api(base, "/api/requests")["requests"]) == 2
    api(base, "/api/prompt", {"prompt": "connect GPIO17 to ESP32 GPIO4"})
    api(base, "/api/requests/confirm", {"pi_pin": 17})
    for pin in (2, 3):
        api(base, "/api/requests/confirm", {"pi_pin": pin})
    api(base, "/api/requests", {"pi_pin": 24, "dut_pin": "INT",
                                "component": "MPU6050 gyroscope", "reason": "data ready"})
    api(base, "/api/requests/confirm", {"pi_pin": 24})
    api(base, "/api/gpio", {"cmd": "configure_pin", "pin": 24, "direction": "rx"})
    result = api(base, "/api/prompt", {"prompt": "read pin 24"})
    assert result["report"]["pass"]
    with pytest.raises(HTTPError) as exc:
        api(base, "/api/requests", {"pi_pin": 0, "dut_pin": "X", "component": "Y", "reason": "Z"})
    assert exc.value.code == 400
    with pytest.raises(HTTPError) as exc:
        api(base, "/api/profiles/select", {"profile": "unknown"})
    assert exc.value.code == 400
    with pytest.raises(HTTPError) as exc:
        api(base, "/api/waveform?pin=0")
    assert exc.value.code == 400


def test_universal_intents():
    assert interpret("test the gyroscope")[0]["profile"] == "mpu6050_gyro"
    assert interpret("test the display")[0]["profile"] == "display"
    assert interpret("connect GPIO17 to ESP32 GPIO4")[0]["pi_pin"] == 17
    assert interpret("read pin 24")[0] == {"cmd": "read", "pins": [24]}


def test_request_pin_state_sse_and_empty_waveform(universal):
    base, app = universal
    assert api(base, "/api/waveform?pin=24")["samples"] == []
    cursor = app.sequence
    assert api(base, "/api/state")["event_cursor"] == cursor
    api(base, "/api/requests/confirm", {"pi_pin": 24})
    request = Request(base + "/api/events", headers={"Last-Event-ID": str(cursor)})
    events = []
    with urlopen(request, timeout=5) as response:
        while len(events) < 2:
            line = response.readline()
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
    assert [event["level"] for event in events] == ["REQUEST", "PIN_STATE"]
    assert events[0]["data"]["status"] == "confirmed"
    assert events[0]["timestamp"] and events[0]["id"] > cursor
    assert next(pin for pin in events[1]["data"]["pins"] if pin["bcm"] == 24)["connected"]
    for data in ({"cmd": "read", "pins": [24]}, {"cmd": "sample", "pins": [],
                                                    "duration_ms": 100, "rate_hz": 20}):
        with pytest.raises(HTTPError) as exc:
            api(base, "/api/gpio", data)
        assert exc.value.code == 400
    assert not app.sim.harness.backend.gpio_config


def test_real_adapters_lazy_and_release(monkeypatch):
    backend = RealBackend(profile="mpu6050_gyro")
    assert backend.bus is None and backend.uart is None
    devices = []
    class Device:
        def __init__(self, pin, **kwargs):
            self.value = 0
            self.closed = False
            self.options = kwargs
            devices.append(self)

        def close(self):
            self.closed = True

    class Bus:
        def __init__(self, number):
            assert number == 1
            self.closed = False

        def write_byte_data(self, *args):
            pass

        def read_byte_data(self, address, register):
            assert address == 0x68
            return 0x68 if register == 0x75 else 0

        def read_i2c_block_data(self, *args):
            return [0, 131, 255, 125, 0, 0]

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "gpiozero",
                        SimpleNamespace(DigitalInputDevice=Device, OutputDevice=Device))
    monkeypatch.setitem(sys.modules, "smbus2", SimpleNamespace(SMBus=Bus))
    for pin, direction in ((2, "tx"), (3, "tx"), (24, "rx")):
        backend.configure(pin, direction)
    assert len(devices) == 1 and devices[0].options == {"pull_up": None, "active_state": True}
    decoded = backend.read_pins([2])[0]["decoded"]
    assert decoded["wx"] == 1 and decoded["wy"] == -1 and decoded["who_am_i"] == 104
    bus = backend.bus
    backend.release(2)
    assert bus.closed and backend.bus is None
    with pytest.raises(ValueError):
        backend.read_pins([3])
    backend.close()
    assert devices[0].closed
