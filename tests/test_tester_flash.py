import base64
import hashlib

import pytest

from aperture.intent import interpret
from aperture.protocol import ProtocolError, validate_request
from aperture.session import Session
from aperture.tools import ToolError, Tools

FIXED = b"\xe9" * 16 + b"APERTURE_BUILD esp32_safety DEMO_BUG=0\x00"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("APERTURE_HOME", str(tmp_path))
    return tmp_path


def connected(home, policy="off"):
    session = Session(actor="joe", home=home)
    tools = Tools(session, wiring_policy=policy)
    tools.call("connect", transport="virtual", scenario="esp32_safety")
    return session, tools


def dut(session):
    return next(b for b in session.transport.rig.behaviors if hasattr(b, "demo_bug"))


@pytest.mark.parametrize("message", [
    {"op": "flash.begin", "images": []},
    {"op": "flash.begin", "images": [{"offset": 0, "size": 0, "sha256": "0" * 64}]},
    {"op": "flash.begin", "images": [{"offset": 0, "size": 1, "sha256": "nope"}]},
    {"op": "flash.chunk", "index": 0, "data": ""},
    {"op": "flash.chunk", "index": 0, "data": "A" * 49153},
    {"op": "flash.run", "baud": 460800, "boot_pin": 0, "en_pin": 25},
    {"op": "flash.run", "baud": 460800},
])
def test_flash_ops_validate(message):
    with pytest.raises(ProtocolError):
        validate_request({"v": 1, "id": 1, **message})


def test_virtual_rig_flash_flips_planted_bug(home):
    session, tools = connected(home)
    assert dut(session).demo_bug is True
    result = tools.call("flash_firmware", env="fixed")
    assert result["flashed"] and result["simulated"] and result["build_tag"].endswith("DEMO_BUG=0")
    assert dut(session).demo_bug is False
    tools.call("flash_firmware", env="esp32dev", manual_boot=True)
    assert dut(session).demo_bug is True
    assert any(e.get("text", "").startswith("flashed esp32dev") for e in session.events if e["kind"] == "note")
    rig = session.transport.rig
    rig.execute({"v": 1, "id": 90, "op": "flash.begin",
                 "images": [{"offset": 0x10000, "size": len(FIXED), "sha256": hashlib.sha256(FIXED).hexdigest()}]})
    with pytest.raises(ProtocolError, match="incomplete"):
        rig.execute({"v": 1, "id": 91, "op": "flash.run", "baud": 460800, "en_pin": 25})
    with pytest.raises(ProtocolError, match="differ"):
        rig.execute({"v": 1, "id": 92, "op": "flash.run", "baud": 460800, "boot_pin": 25, "en_pin": 25})
    rig.execute({"v": 1, "id": 93, "op": "flash.begin",
                 "images": [{"offset": 0x10000, "size": len(FIXED), "sha256": hashlib.sha256(FIXED).hexdigest()}]})
    rig.execute({"v": 1, "id": 94, "op": "flash.chunk", "index": 0, "data": base64.b64encode(FIXED).decode()})
    assert rig.execute({"v": 1, "id": 95, "op": "flash.run", "baud": 115200, "en_pin": 25})["manual_boot"]


def test_flash_requires_confirmed_wiring(home):
    session, tools = connected(home, policy="ask")
    with pytest.raises(ToolError) as exc:
        tools.call("flash_firmware", env="fixed")
    assert exc.value.code == "wiring_required" and 14 in session.pending_wiring
    for _ in range(4):  # each attempt requests the next unwired pin: 14, 15, 25, 24
        try:
            result = tools.call("flash_firmware", env="fixed")
            break
        except ToolError as exc:
            assert exc.code == "wiring_required"
            session.confirm_wiring(exc.extra["pin"], True, "joe")
    else:
        result = tools.call("flash_firmware", env="fixed")
    assert set(session.wiring) >= {14, 15, 25, 24} and result["flashed"]


def test_flash_intent():
    calls = interpret("flash the fixed firmware")["calls"]
    assert calls == [{"tool": "flash_firmware", "args": {"env": "fixed", "manual_boot": False}}]
    assert interpret("flash firmware, I'll hold boot")["calls"][0]["args"] == {"env": "esp32dev", "manual_boot": True}
