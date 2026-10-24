import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from aperture.protocol import MAX_JSON_LINE, ProtocolError
from aperture.rig.virtual import VirtualRig, load_scenario
from aperture.transport import TcpTransport, TransportError
from pi.bridge import daemon
from pi.bridge.server import BridgeServer
from pi.bridge.tcp import TcpBridge, encode_line

WAIT_S = 5


@pytest.fixture
def make_bridge():
    started = []

    def start(scenario="loopback", limits=None):
        bridge = BridgeServer(VirtualRig(load_scenario(scenario), realtime=True, limits=limits),
                              pump_interval_s=0.001)
        server = TcpBridge(("127.0.0.1", 0), bridge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, bridge, thread))
        return server, bridge

    yield start
    for server, bridge, thread in started:
        server.shutdown()
        server.close_connections()
        bridge.shutdown()
        server.server_close()
        thread.join()


def client(server, name, **kwargs):
    transport = TcpTransport("127.0.0.1", server.server_address[1], client=name, **kwargs)
    transport.connect()
    return transport


def wait_event(transport, predicate):
    """Collect events until ``predicate(event)`` holds; returns the matching event."""
    found, ready = [], threading.Event()

    def on_event(message):
        if not ready.is_set() and predicate(message):
            found.append(message)
            ready.set()

    transport.on_event(on_event)
    return found, ready


def test_end_to_end_over_tcp(make_bridge):
    server, _ = make_bridge()
    acks = []
    transport = TcpTransport("127.0.0.1", server.server_address[1], client="alice")
    transport.on_ack(lambda request_id, t: acks.append(request_id))
    manifest = transport.connect()
    try:
        assert manifest["bridge"] == "virtual-rig" and manifest["proto"] == 1
        assert transport.clock_offset_us is not None and transport.describe()["port"] > 0
        assert transport.take_control().ok and transport.watchdog_s == 5.0
        assert transport.request("pin.mode", pin=27, mode="input").ok
        assert transport.request("pin.mode", pin=17, mode="output", initial=0).ok
        assert transport.request("pin.write", pin=17, level=1).result["level"] == 1
        assert transport.request("pin.read", pin=27).result["level"] == 1
        assert len(acks) == len(set(acks)) >= 9  # hello + 5 clock pings + take + 4 ops
    finally:
        transport.disconnect()


def test_bytes_survive_the_json_wire(make_bridge):
    server, _ = make_bridge("uart_echo")
    transport = client(server, "alice")
    try:
        transport.take_control()
        port = transport.request("uart.open", tx=14, rx=15, baud=115200).result["port"]
        assert transport.request("uart.write", port=port, data=b"\x00\xff\n").ok  # not UTF-8
        assert transport.request("uart.write", port=port, data=b"ping\n").ok
        received = b""
        deadline = time.monotonic() + WAIT_S
        while b"pong" not in received and time.monotonic() < deadline:
            data = transport.request("uart.read", port=port, timeout_ms=100).result["data"]
            received += base64.b64decode(data)  # JSON transports carry byte fields as base64
        assert b"\x00\xff\nping\n" in received and b"pong\n" in received
    finally:
        transport.disconnect()


def test_observer_watches_but_cannot_drive(make_bridge):
    server, _ = make_bridge()
    alice, bob = client(server, "alice"), client(server, "bob")
    try:
        assert alice.take_control().ok
        denied = bob.request("pin.mode", pin=17, mode="output")
        assert denied.err == "not_controller" and denied.result["controller"] == "alice"
        busy = bob.take_control()
        assert busy.err == "busy" and busy.result["controller"] == "alice"
        found, ready = wait_event(bob, lambda m: m.get("ev") == "pin.change" and m["level"] == 1)
        assert bob.request("stream.set", pins=[17], on=True).ok
        alice.request("pin.mode", pin=17, mode="output", initial=0)
        alice.request("pin.write", pin=17, level=1)
        assert ready.wait(WAIT_S) and found[0]["pin"] == 17
    finally:
        alice.disconnect()
        bob.disconnect()


def test_controller_link_drop_releases_outputs(make_bridge):
    server, bridge = make_bridge()
    alice, bob = client(server, "alice"), client(server, "bob")
    try:
        alice.take_control()
        alice.request("pin.mode", pin=17, mode="output", initial=1)
        found, ready = wait_event(bob, lambda m: m.get("ev") == "control.lost")
        alice._link.sock.shutdown(socket.SHUT_RDWR)  # pull the plug: no release, no goodbye
        assert ready.wait(WAIT_S) and found[0]["reason"] == "disconnect"
        pins = {p["pin"]: p for p in bob.request("list_pins").result["pins"]}
        assert pins[17]["mode"] == "disabled" and bridge.controller is None
    finally:
        bob.disconnect()


def test_keepalive_holds_control_past_the_watchdog(make_bridge):
    server, _ = make_bridge()
    alice = client(server, "alice")
    try:
        assert alice.take_control(watchdog_s=0.6).ok
        time.sleep(1.8)
        assert alice.control_token and alice.request("pin.mode", pin=17, mode="output").ok
    finally:
        alice.disconnect()


def test_watchdog_releases_a_silent_controller(make_bridge):
    server, _ = make_bridge()
    alice = client(server, "alice", keepalive=False)
    try:
        found, ready = wait_event(alice, lambda m: m.get("ev") == "control.lost")
        alice.take_control(watchdog_s=0.4)
        alice.request("pin.mode", pin=17, mode="output", initial=1)
        assert ready.wait(WAIT_S) and found[0]["reason"] == "watchdog"
        deadline = time.monotonic() + WAIT_S
        while alice.control_token and time.monotonic() < deadline:
            time.sleep(0.01)
        assert alice.control_token is None
        assert alice.request("pin.write", pin=17, level=0).err == "not_controller"
    finally:
        alice.disconnect()


def raw_lines(sock, count):
    reader = sock.makefile("rb")
    return [json.loads(reader.readline()) for _ in range(count)]


def test_malformed_line_keeps_the_connection(make_bridge):
    server, _ = make_bridge()
    with socket.create_connection(server.server_address) as sock:
        sock.sendall(b"not json\n\n{\"v\":1,\"id\":3,\"op\":\"ping\"}\n")
        bad, ack, reply = raw_lines(sock, 3)
        assert bad["id"] is None and bad["err"] == "bad_request"
        assert ack["ack"] and ack["id"] == 3 and reply["ok"] and reply["id"] == 3


def test_oversized_line_is_rejected_and_closed(make_bridge):
    server, _ = make_bridge()
    with socket.create_connection(server.server_address) as sock:
        sock.sendall(b"x" * (MAX_JSON_LINE + 1) + b"\n")
        reader = sock.makefile("rb")
        assert json.loads(reader.readline())["msg"] == "line too long"
        assert reader.readline() == b""


def test_encode_line_degrades_oversized_messages():
    big = "x" * MAX_JSON_LINE
    event_line = json.loads(encode_line({"v": 1, "ev": "capture.done", "t": 1, "blob": big}, 5))
    assert event_line["ev"] == "rig.warning" and event_line["warning"] == "event_too_large"
    reply_line = json.loads(encode_line({"v": 1, "id": 9, "ok": True, "t": 1, "blob": big}, 5))
    assert reply_line["id"] == 9 and reply_line["err"] == "range" and reply_line["t"] == 5


def test_client_detects_link_loss_and_reconnects(make_bridge):
    server, _ = make_bridge()
    alice = client(server, "alice")
    try:
        alice.take_control()
        lost, lost_ready = wait_event(alice, lambda m: m.get("ev") == "control.lost")
        warn, warn_ready = wait_event(alice, lambda m: m.get("warning") == "link_lost")
        server.close_connections()
        assert lost_ready.wait(WAIT_S) and lost[0]["reason"] == "link_lost"
        assert warn_ready.wait(WAIT_S) and warn[0]["source"] == "host"
        assert not alice.connected and alice.control_token is None
        with pytest.raises(TransportError):
            alice.request("ping")
        assert alice.connect()["proto"] == 1
        assert alice.take_control().ok
    finally:
        alice.disconnect()


def test_oversized_request_fails_locally_and_link_survives(make_bridge):
    server, _ = make_bridge()
    alice = client(server, "alice")
    try:
        alice.take_control()
        port = alice.request("uart.open", tx=14, rx=15, baud=9600).result["port"]
        too_big = alice.request("uart.write", port=port, data=b"x" * 60_000)
        assert too_big.err == "bad_request" and "too large" in too_big.msg
        assert alice.request("ping").ok
    finally:
        alice.disconnect()


def test_link_loss_during_a_request_raises(make_bridge):
    server, _ = make_bridge()
    alice = client(server, "alice")
    alice.take_control()
    port = alice.request("uart.open", tx=14, rx=15, baud=9600).result["port"]
    threading.Timer(0.3, server.close_connections).start()
    with pytest.raises(TransportError, match="link lost"):
        alice.request("uart.read", port=port, timeout_ms=3000)
    assert not alice.connected


class StubBridge:
    """A TCP peer that acks (optionally) but never answers, to exercise client timeouts.

    With ``script`` it instead answers hello/ping after a garbage line, so the client must
    skip malformed input from the bridge."""

    def __init__(self, ack: bool, script: bool = False):
        self.ack, self.script = ack, script
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.peers = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                peer, _ = self.sock.accept()
            except OSError:
                return
            self.peers.append(peer)
            threading.Thread(target=self._peer, args=(peer,), daemon=True).start()

    def _peer(self, peer):
        reader = peer.makefile("rb")
        for line in reader:
            request = json.loads(line)
            if self.ack:
                peer.sendall(json.dumps({"v": 1, "id": request["id"], "ack": True, "t": 1}).encode() + b"\n")
            if self.script:
                result = {"manifest": {"proto": 1}} if request["op"] == "hello" else {}
                reply = {"v": 1, "id": request["id"], "ok": True, "t": 2, **result}
                peer.sendall(b"}garbage{\n" + json.dumps(reply).encode() + b"\n")

    def close(self):
        self.sock.close()
        for peer in self.peers:
            peer.close()


@pytest.mark.parametrize("acks,expected", [(False, "no ack"), (True, "no response")])
def test_request_timeouts(acks, expected):
    stub = StubBridge(ack=acks)
    transport = TcpTransport("127.0.0.1", stub.port)
    transport.timeouts.update(ack=0.2, response=0.2)
    try:
        with pytest.raises(ProtocolError) as exc:
            transport.connect()
        assert exc.value.code == "timeout" and expected in exc.value.msg
        assert not transport.connected
    finally:
        stub.close()


def test_malformed_lines_from_the_bridge_are_skipped():
    stub = StubBridge(ack=True, script=True)
    transport = TcpTransport("127.0.0.1", stub.port)
    try:
        assert transport.connect() == {"proto": 1}
        assert transport.request("ping").ok and transport.clock_offset_us is not None
    finally:
        transport.disconnect()
        stub.close()


def test_connect_refused_raises_transport_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(TransportError, match="cannot reach bridge"):
        TcpTransport("127.0.0.1", port).connect()


def test_event_callbacks_can_fail_or_issue_requests(make_bridge):
    server, _ = make_bridge()
    alice = client(server, "alice")
    replies, done = [], threading.Event()

    def explode(_message):
        raise RuntimeError("callback bug")

    def reenter(message):
        if message.get("ev") == "control.changed" and not done.is_set():
            replies.append(alice.request("list_pins"))  # runs on the dispatcher thread
            done.set()

    alice.on_event(explode)
    alice.on_event(reenter)
    try:
        assert alice.take_control().ok
        assert done.wait(WAIT_S) and replies[0].ok
    finally:
        alice.disconnect()


def test_parse_args_validates():
    with pytest.raises(SystemExit):
        daemon.parse_args(["--watchdog-s", "0"])
    with pytest.raises(SystemExit):
        daemon.parse_args(["--port", "70000"])
    with pytest.raises(SystemExit, match="unknown scenario"):
        daemon.build_executor(daemon.parse_args(["--scenario", "nope"]))
    args = daemon.parse_args(["--scenario", "button_led", "--watchdog-s", "2"])
    assert daemon.build_executor(args).limits["watchdog_s"] == 2


def test_daemon_build_and_serve_release_on_shutdown(caplog):
    server, bridge = daemon.build(daemon.parse_args(["--port", "0", "--scenario", "loopback"]))
    thread = threading.Thread(target=daemon.serve, args=(server, bridge))
    thread.start()
    transport = TcpTransport("127.0.0.1", server.server_address[1], client="alice")
    transport.connect()
    transport.take_control()
    transport.request("pin.mode", pin=17, mode="output", initial=1)
    with caplog.at_level("INFO", logger="aperture.bridge"):
        server.shutdown()
        thread.join(WAIT_S)
    assert bridge.executor.pins[17].mode == "disabled" and bridge.executor.controller is None
    assert "all outputs released" in caplog.text


def test_daemon_build_fails_cleanly_when_the_port_is_busy(monkeypatch):
    stopped = []
    real_shutdown = BridgeServer.shutdown
    monkeypatch.setattr(BridgeServer, "shutdown", lambda self: stopped.append(real_shutdown(self)))
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        monkeypatch.setattr(TcpBridge, "allow_reuse_address", False)
        with pytest.raises(OSError):
            daemon.build(daemon.parse_args(["--port", str(port)]))
    assert stopped


def test_daemon_serves_and_releases_on_sigterm():
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen([sys.executable, "-m", "pi.bridge", "--port", "0", "--scenario",
                                "loopback"], cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(root)})
    try:
        ready = process.stdout.readline()
        assert ready.startswith("Aperture bridge ready: tcp://127.0.0.1:")
        port = int(ready.split("tcp://127.0.0.1:")[1].split()[0])
        transport = TcpTransport("127.0.0.1", port, client="alice")
        transport.connect()
        transport.take_control()
        transport.request("pin.mode", pin=17, mode="output", initial=1)
        process.send_signal(signal.SIGTERM)
        assert process.wait(WAIT_S) == 0
        assert "all outputs released" in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
