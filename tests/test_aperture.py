import json
import socket
import threading

import pytest

from agent.client import Client
from agent.planner import Tools, run_offline, verify
from pi.backends import RealBackend, SimBackend
from pi.daemon import Harness, Server
from pi.protocol import MAX_LINE, parse_command


@pytest.fixture
def server():
    with Server(("127.0.0.1", 0), Harness(SimBackend())) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        yield instance
        instance.shutdown()
        thread.join()


@pytest.mark.parametrize("line", [
    "", "null", "[]", "{", '{"cmd":[]}', '{"cmd":"unknown"}',
    '{"cmd":"ping","extra":1}', '{"cmd":"set_pin","pin":17,"val":true}',
    '{"cmd":"set_pin","pin":19,"val":1}', '{"cmd":"set_pwm","pin":18,"duty":2}',
    '{"cmd":"set_pwm","pin":18,"duty":NaN}',
    '{"cmd":"assert_output","pin":23,"expected":0,"timeout_ms":-1}',
    '{"cmd":"run_vector","trig":1,"pwm":0.5}',
    '{"cmd":"set_pin","pin":true,"val":1}', " " * (MAX_LINE + 1),
])
def test_invalid_protocol(line):
    with pytest.raises(ValueError):
        parse_command(line)


@pytest.mark.parametrize("command", [
    {"cmd": "ping"}, {"cmd": "read_state"},
    {"cmd": "set_pin", "pin": 17, "val": 1},
    {"cmd": "set_pwm", "pin": 18, "duty": 0.75},
    {"cmd": "inject_fault", "val": 1},
    {"cmd": "run_vector", "trig": 0, "pwm": 0, "fault": 0},
    {"cmd": "assert_output", "pin": 23, "expected": 1, "timeout_ms": 0},
])
def test_protocol(command):
    assert parse_command(json.dumps(command)) == command


@pytest.mark.parametrize("bug", [True, False])
@pytest.mark.parametrize("trig,pwm", [(1, 0.3), (0, 0.9)])
def test_bug_and_fixed(bug, trig, pwm):
    backend = SimBackend(demo_bug=bug)
    backend.apply(trig=trig, pwm=pwm, fault=1)
    state = backend.read()
    assert state["telemetry"]["state"] == "FAULT_SHUTDOWN"
    assert state["telemetry"]["safety"] == 0
    assert state["physical"]["23"] == int(bug)
    backend.apply(trig=0, pwm=0, fault=0)
    assert backend.read()["physical"] == {"22": 0, "23": 1}


def test_sim_states():
    backend = SimBackend()
    assert backend.read()["telemetry"]["state"] == "IDLE"
    backend.apply(trig=1, pwm=0.5)
    assert backend.read()["physical"]["22"] == 1
    backend.apply(pwm=0.9)
    assert backend.read()["telemetry"]["state"] == "THROTTLING"
    backend.apply(trig=0, pwm=0, fault=1)
    assert backend.read()["physical"] == {"22": 0, "23": 0}


def test_end_to_end(server):
    with Client(port=server.server_address[1]) as client:
        report = run_offline(client)
        assert not report["pass"]
        assert len(report["steps"]) == 6
        assert "DISCREPANCY" in report["steps"][2]["issues"][0]
        assert "DISCREPANCY" in report["steps"][4]["issues"][0]
        assert report["steps"][-1]["telemetry"]["state"] == "IDLE"
        server.harness.backend.demo_bug = False
        assert run_offline(client)["pass"]


def test_tools_and_assertion(server):
    with Client(port=server.server_address[1]) as client:
        tools = Tools(client)
        assert not tools.read_hardware_state()["issues"]
        assert not tools.stimulate_hardware("digital", 1)["issues"]
        assert tools.inject_hardware_fault()["issues"]
        failure = client.command(cmd="assert_output", pin=23, expected=0, timeout_ms=5)
        assert not failure["pass"]
        assert failure["latency_ms"] >= 5
        assert client.command(cmd="assert_output", pin=23, expected=1, timeout_ms=0)["pass"]
        assert not client.command(cmd="set_pin", pin=22, val=1)["pass"]


def test_transport_fragmentation_and_multiple_lines(server):
    with socket.create_connection(server.server_address) as sock:
        sock.sendall(b'{"cmd":')
        sock.sendall(b'"ping"}\ninvalid\n{"cmd":"read_state"}\n')
        with sock.makefile("rb") as stream:
            assert json.loads(stream.readline())["pong"]
            assert not json.loads(stream.readline())["pass"]
            assert json.loads(stream.readline())["telemetry"]["state"] == "IDLE"


def test_transport_empty_and_oversized(server):
    with socket.create_connection(server.server_address) as sock:
        sock.sendall(b"\n")
        with sock.makefile("rb") as stream:
            assert not json.loads(stream.readline())["pass"]
            sock.sendall(b"x" * (MAX_LINE + 1) + b"\n")
            assert "too large" in json.loads(stream.readline())["error"]


def test_backend_error():
    class BrokenBackend:
        def read(self):
            raise TimeoutError("telemetry unavailable")

    result = Harness(BrokenBackend()).execute({"cmd": "read_state"})
    assert not result["pass"]
    assert result["error"] == "telemetry unavailable"
    assert verify(result) == ["telemetry unavailable"]
    assert verify({"pass": True}) == ["missing or malformed measurement"]


def test_real_serial_parser_without_hardware():
    class Serial:
        def reset_input_buffer(self):
            pass

        def readline(self):
            return next(self.lines)

    class Pin:
        value = 0

    backend = RealBackend.__new__(RealBackend)
    backend.serial = Serial()
    packet = SimBackend().read()["telemetry"]
    backend.serial.lines = iter([b"boot log\n", b"{}\n", json.dumps(packet).encode()])
    backend.inputs = {"22": Pin(), "23": Pin()}
    backend.outputs = {"trig": Pin(), "pwm": Pin(), "fault": Pin()}
    assert backend.read()["telemetry"] == packet
