"""Pi bridge daemon: hardware engine (fake driver), BLE GATT framing, TCP fail-safe and control."""
import itertools
import socket
import time

import pytest

from aperture.protocol import decode_json, encode_json
from aperture.rig.virtual import VirtualRig, load_scenario
from aperture.session import Session
from aperture.tools import Tools
from aperture.transport import TcpTransport
from aperture.transport.ble import EVT_UUID, RSP_UUID, BleFraming
from aperture.transport.tcp import parse_target
from pi.bridge.core import BridgeCore
from pi.bridge.daemon import main as bridge_main
from pi.bridge.gatt import GattLink
from pi.bridge.hardware import FakeDriver, HardwareRig
from pi.bridge.tcp import TcpBridgeServer

_ids = itertools.count(1)


def req(rig, op, **args):
    return rig.execute({"v": 1, "id": next(_ids), "op": op, **args})


@pytest.fixture
def hw():
    driver = FakeDriver()
    rig = HardwareRig(driver, limits={"watchdog_s": 0.2})
    req(rig, "hello", client="t", proto=[1])
    req(rig, "control.take")
    yield rig, driver
    rig.shutdown()


def test_hardware_manifest_is_real_header():
    rig = HardwareRig(FakeDriver(), denied_pins=[4])
    m = req(rig, "hello", client="t", proto=[1])["manifest"]
    assert m["bridge"] == "aperture-pi" and m["board"] == "Fake Pi" and m["proto"] == 1
    assert not m["fw"].startswith("virtual")
    assert len(m["pins"]) == 25 and 4 not in {p["bcm"] for p in m["pins"]}


def test_hardware_output_input_and_edges(hw):
    rig, driver = hw
    req(rig, "pin.mode", pin=17, mode="output", initial=1)
    assert ("output", 17, 1) in driver.calls and driver.levels[17] == 1
    req(rig, "pin.write", pin=17, level=0)
    assert driver.levels[17] == 0
    req(rig, "pin.mode", pin=22, mode="input_pullup")
    assert ("input", 22, "up") in driver.calls
    events = []
    rig.on_event(events.append)
    req(rig, "stream.set", pins=[22], on=True)
    driver.dut_drive(22, 0)
    assert req(rig, "pin.read", pin=22)["level"] == 0
    assert any(e["ev"] == "pin.change" and e["pin"] == 22 and e["level"] == 0 for e in events)


def test_hardware_capture_records_real_edges(hw):
    rig, driver = hw
    req(rig, "pin.mode", pin=27, mode="input")
    cap = req(rig, "capture.start", pins=[27], rate_hz=1000, duration_ms=50)
    driver.dut_drive(27, 1)
    driver.dut_drive(27, 0)
    events = []
    rig.on_event(events.append)
    deadline = time.monotonic() + 2
    while not events and time.monotonic() < deadline:
        rig.pump()
        time.sleep(0.005)
    done = next(e for e in events if e["ev"] == "capture.done")
    assert done["capture_id"] == cap["capture_id"] and [e[2] for e in done["edges"]] == [1, 0]


def test_hardware_uart_spi_i2c(hw):
    rig, driver = hw
    with pytest.raises(Exception, match="hardware UART"):
        req(rig, "uart.open", tx=2, rx=3, baud=9600)
    port = req(rig, "uart.open", tx=14, rx=15, baud=115200)["port"]
    req(rig, "uart.write", port=port, data=b"PING")
    assert bytes(driver.uart_tx) == b"PING"
    driver.uart_rx(b"BOOT OK\n")
    assert req(rig, "uart.read", port=port, max=8, timeout_ms=100)["data"] == b"BOOT OK\n"
    assert req(rig, "spi.xfer", bus=0, cs=0, data=b"\x01\x02")["data"] == b"\x01\x02"
    driver.i2c[0x3C] = b"\x10\x20\x30"
    assert req(rig, "i2c.scan", bus=1)["addrs"] == [0x3C]
    assert req(rig, "i2c.read", bus=1, addr=0x3C, n=2, reg=1)["data"] == b"\x20\x30"
    with pytest.raises(Exception, match="no ack"):
        req(rig, "i2c.write", bus=1, addr=0x50, data=b"\x00")


def test_hardware_watchdog_releases_every_output(hw):
    rig, driver = hw
    req(rig, "pin.mode", pin=17, mode="output", initial=1)
    req(rig, "pin.mode", pin=18, mode="pwm")
    req(rig, "pin.pwm", pin=18, freq_hz=100, duty=0.5)
    time.sleep(0.3)
    rig.pump()
    assert rig.controller is None
    assert ("release", 17) in driver.calls and ("release", 18) in driver.calls
    assert ("pwm", 18, 0, 0) in driver.calls


def test_gatt_link_round_trip_through_ble_framing():
    core = BridgeCore(VirtualRig(load_scenario("loopback"), realtime=True))
    received = []
    host = BleFraming(received.append)
    gatt = GattLink(core, lambda uuid, piece: host.feed(uuid, piece), mtu=23)  # tiny MTU forces chunking
    seen = []
    gatt.notify = lambda uuid, piece: (seen.append(uuid), host.feed(uuid, piece))
    for frame in BleFraming.frames({"v": 1, "id": 1, "op": "hello", "client": "ble", "proto": [1]}, 23):
        gatt.on_write(frame)
    assert received[0] == {"v": 1, "id": 1, "ack": True, "t": received[0]["t"]}
    assert received[1]["ok"] and received[1]["manifest"]["proto"] == 1
    assert len(seen) > 10 and set(seen) == {RSP_UUID}
    for frame in BleFraming.frames({"v": 1, "id": 2, "op": "control.take"}, 23):
        gatt.on_write(frame)
    assert gatt.link.token
    gatt.disconnected()  # central dropped -> fail safe
    assert core.engine.controller is None
    core.close()


def test_ble_events_use_event_characteristic():
    core = BridgeCore(VirtualRig(load_scenario("loopback"), realtime=True))
    uuids = []
    gatt = GattLink(core, lambda uuid, piece: uuids.append(uuid))
    gatt.connected()
    core.engine._emit("pin.change", pin=17, level=1)
    assert EVT_UUID in uuids
    core.close()


@pytest.fixture
def bridge():
    core = BridgeCore(VirtualRig(load_scenario("loopback"), realtime=True, limits={"watchdog_s": 0.3}))
    server = TcpBridgeServer(core, "127.0.0.1", 0)
    server.start()
    yield core, server.port
    server.shutdown()
    server.server_close()
    core.close()


def test_second_tester_cannot_steal_control(bridge):
    _core, port = bridge
    a, b = TcpTransport(f"127.0.0.1:{port}", client="a"), TcpTransport(f"127.0.0.1:{port}", client="b")
    a.connect()
    b.connect()
    assert a.take_control().ok
    busy = b.take_control()
    assert not busy.ok and busy.err == "busy"
    still_busy = b.take_control(force=True)  # force only wins once the holder has gone idle
    assert not still_busy.ok and still_busy.err == "busy"
    a.release_control()
    assert b.take_control().ok
    a.disconnect()
    b.disconnect()


def test_socket_drop_fails_safe(bridge):
    core, port = bridge
    sock = socket.create_connection(("127.0.0.1", port))
    f = sock.makefile("rb")
    for i, msg in enumerate([{"op": "hello", "client": "x", "proto": [1]}, {"op": "control.take"},
                             {"op": "pin.mode", "pin": 17, "mode": "output", "initial": 1}], 1):
        sock.sendall(encode_json({"v": 1, "id": i, **msg}))
        assert decode_json(f.readline()).get("ack")
        reply = decode_json(f.readline())
        while "ev" in reply:
            reply = decode_json(f.readline())
        assert reply["ok"]
    assert core.engine.pins[17].mode == "output"
    sock.close()
    deadline = time.monotonic() + 2
    while core.engine.controller and time.monotonic() < deadline:
        time.sleep(0.01)
    assert core.engine.controller is None and core.engine.pins[17].mode == "disabled"


def test_keepalive_holds_control_past_watchdog(bridge):
    core, port = bridge
    t = TcpTransport(f"127.0.0.1:{port}")
    t.connect()
    assert t.take_control(watchdog_s=0.3).ok
    t.request("pin.mode", pin=17, mode="output", initial=1)
    time.sleep(0.9)
    assert t.control_token and core.engine.controller is not None
    t.disconnect()
    assert core.engine.pins[17].mode == "disabled"


def test_bridge_restart_surfaces_link_loss(bridge):
    _core, port = bridge
    t = TcpTransport(f"127.0.0.1:{port}")
    t.connect()
    t.take_control()
    events = []
    t.on_event(events.append)
    t._sock.shutdown(socket.SHUT_RDWR)
    deadline = time.monotonic() + 2
    while t.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not t.connected and t.control_token is None
    assert any(e["ev"] == "control.lost" and e["reason"] == "link" for e in events)


def test_tcp_target_parsing_and_daemon_args():
    assert parse_target(None) == ("127.0.0.1", 8766)
    assert parse_target("tcp://pi.local:9000") == ("pi.local", 9000)
    assert parse_target("pi.local") == ("pi.local", 8766)
    with pytest.raises(SystemExit):
        bridge_main([])


def test_session_lifecycle_over_tcp(bridge, tmp_path):
    """Acks arrive on the reader thread while the session is mid-request; they must not deadlock."""
    _core, port = bridge
    session = Session(actor="t", home=tmp_path)
    tools = Tools(session, wiring_policy="off")
    tools.call("connect", transport="tcp", target=f"127.0.0.1:{port}")
    tools.call("set_mode", pin=27, mode="input")
    tools.call("set_mode", pin=17, mode="output")
    tools.call("write", pin=17, level=1)
    assert tools.call("assert_pin", pin=27, level=1)["pass"]
    stages = [e for e in session.events if e["kind"] == "request.stage"]
    acked = {e["req"] for e in stages if e["stage"] == "acknowledged"}
    executed = {e["req"] for e in stages if e["stage"] == "executed"}
    assert executed and executed <= acked
    tools.call("disconnect")


def test_console_connects_to_tcp_bridge(bridge, tmp_path):
    from aperture.server import ConsoleApp
    _core, port = bridge
    app = ConsoleApp(home=str(tmp_path), llm=False)
    state = app.connect({"transport": "tcp", "target": f"127.0.0.1:{port}"})
    assert state["connection"]["connected"] and state["connection"]["controller"]
    assert any(e["kind"] == "request.stage" and e["stage"] == "acknowledged" for e in app.events_since(0))
    app.session.disconnect("test")
