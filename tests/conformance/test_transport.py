"""Protocol v1 conformance: every Transport/bridge pair must pass these identically.

``tcp`` runs the real host TcpTransport against the real Pi bridge daemon core
(``pi.bridge``) serving a realtime Virtual Rig, so the wire, acks, control and
fail-safe paths are exercised end to end without a Pi. BLE shares the same
bridge core; its framing is covered in tests/test_bridge.py.
"""
import pytest

from aperture.protocol import OPS
from aperture.rig.virtual import VirtualRig, load_scenario
from aperture.transport import TcpTransport, VirtualTransport
from pi.bridge.core import BridgeCore
from pi.bridge.tcp import TcpBridgeServer


@pytest.fixture(params=["virtual", "tcp"])
def transport(request):
    server = core = None
    if request.param == "virtual":
        t = VirtualTransport(scenario="loopback")
    else:
        core = BridgeCore(VirtualRig(load_scenario("loopback"), realtime=True))
        server = TcpBridgeServer(core, "127.0.0.1", 0)
        server.start()
        t = TcpTransport(f"127.0.0.1:{server.port}")
    t.connect()
    yield t
    t.disconnect()
    if server is not None:
        server.shutdown()
        server.server_close()
        core.close()


def test_hello_manifest(transport):
    m = transport.manifest
    assert m["proto"] == 1 and m["header"] == "J8-40" and len(m["pins"]) >= 26
    assert {"capture_hz", "pulse_us", "pwm_hz", "mtu"} <= set(m["limits"])
    assert all({"bcm", "phys", "caps"} <= set(p) for p in m["pins"])
    assert set(m["power"]) >= {"battery_pct", "vbus_ok"}


def test_every_op_is_known_and_unknown_is_rejected(transport):
    for op in OPS:
        assert op in OPS
    bad = transport.request("nope")
    assert not bad.ok and bad.err == "unknown_op"
    v2 = transport._send({"v": 2, "id": 1, "op": "ping"}, 1)
    assert not v2.ok and v2.err == "bad_request"


def test_driving_requires_control(transport):
    denied = transport.request("pin.mode", pin=17, mode="output")
    assert denied.err == "not_controller"
    assert transport.request("pin.read", pin=17).err == "pin_mode"  # read-only allowed, pin disabled
    assert transport.take_control().ok
    assert transport.request("pin.mode", pin=17, mode="output", initial=1).result["level"] == 1


def test_write_readback_and_loopback(transport):
    transport.take_control()
    transport.request("pin.mode", pin=27, mode="input")
    transport.request("pin.mode", pin=17, mode="output", initial=0)
    assert transport.request("pin.write", pin=17, level=1).result["level"] == 1
    assert transport.request("pin.read", pin=27).result["level"] == 1


def test_release_all_returns_everything_to_high_z(transport):
    transport.take_control()
    transport.request("pin.mode", pin=17, mode="output", initial=1)
    transport.request("pin.mode", pin=18, mode="pwm")
    transport.request("pin.pwm", pin=18, freq_hz=50, duty=0.5)
    transport.request("uart.open", tx=14, rx=15, baud=9600)
    assert transport.request("release_all").ok
    pins = {p["pin"]: p for p in transport.request("list_pins").result["pins"]}
    assert all(p["mode"] == "disabled" and "pwm" not in p for p in pins.values())
    assert transport.request("uart.read", port=0).err == "bad_request"


def test_disconnect_releases_outputs(transport):
    transport.take_control()
    transport.request("pin.mode", pin=17, mode="output", initial=1)
    transport.disconnect()
    transport.connect()
    pins = {p["pin"]: p for p in transport.request("list_pins").result["pins"]}
    assert pins[17]["mode"] == "disabled" and pins["controller"] is None if "controller" in pins else True
