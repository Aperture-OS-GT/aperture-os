import base64
import hashlib
import json
import sys
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agent.flash import CHUNK_BYTES, flash, images, manifest, required_pins
from agent.wiring import Wiring
from pi.backends import RealBackend, SimBackend
from pi.daemon import Harness
from pi.flash import Flasher
from pi.protocol import parse_command
from profiles import load_profile
from ui import server
from ui.intent import interpret

FIXED = b"\xe9" * 64 + b"APERTURE_BUILD esp32_safety DEMO_BUG=0\x00" + b"\xff" * 100
BUGGY = FIXED.replace(b"DEMO_BUG=0", b"DEMO_BUG=1")


def staged(image, offset=0x10000):
    return {"offset": offset, "size": len(image), "sha256": hashlib.sha256(image).hexdigest()}


def test_profile_flash_wires_are_optional_for_tests():
    wires = {wire["pi_pin"]: wire for wire in load_profile("esp32_safety")["wires"]}
    assert wires[14]["dut_pin"] == "RX0" and wires[15]["dut_pin"] == "TX0"
    assert wires[25]["optional"] and wires[24]["optional"]
    assert required_pins() == [14, 15, 25, 24] and required_pins(manual_boot=True) == [14, 15, 25]
    wiring = Wiring(lambda level, data: None)
    wiring.select("esp32_safety")
    assert 24 not in wiring.missing() and 25 not in wiring.missing()
    for pin in (17, 18, 27, 22, 23, 14, 15):
        wiring.confirm_connection(pin)
    assert wiring.status()["ready"]


@pytest.mark.parametrize("command", [
    {"cmd": "flash_begin", "images": []},
    {"cmd": "flash_begin", "images": [{"offset": 0x1000, "size": 0, "sha256": "0" * 64}]},
    {"cmd": "flash_begin", "images": [{"offset": 0x1000, "size": 1, "sha256": "xyz"}]},
    {"cmd": "flash_begin", "images": [{"offset": 0, "size": 3 * 1024 * 1024, "sha256": "0" * 64},
                                      {"offset": 0, "size": 2 * 1024 * 1024, "sha256": "0" * 64}]},
    {"cmd": "flash_chunk", "index": 0, "data": ""},
    {"cmd": "flash_chunk", "index": 0, "data": "A" * 49153},
    {"cmd": "flash_chunk", "index": "0", "data": "AAAA"},
    {"cmd": "flash_run", "baud": 9600, "boot_pin": 24, "en_pin": 25},
    {"cmd": "flash_run", "baud": 460800, "boot_pin": 25, "en_pin": 25},
    {"cmd": "flash_run", "baud": 460800, "boot_pin": 24, "en_pin": None},
    {"cmd": "flash_run", "baud": 460800, "boot_pin": 0, "en_pin": 25},
    {"cmd": "flash_run", "baud": 460800, "en_pin": 25},
])
def test_flash_protocol_rejects(command):
    with pytest.raises(ValueError):
        parse_command(json.dumps(command))


def test_sim_flash_flips_planted_bug_and_validates_images():
    harness = Harness(SimBackend())
    backend = harness.backend
    assert backend.demo_bug
    assert not harness.execute({"cmd": "flash_run", "baud": 460800, "boot_pin": 24,
                                "en_pin": 25})["pass"]
    assert harness.execute({"cmd": "flash_begin", "images": [staged(FIXED)]})["pass"]
    short = harness.execute({"cmd": "flash_chunk", "index": 0,
                             "data": base64.b64encode(FIXED[:10]).decode()})
    assert short["received"] == 10 and short["size"] == len(FIXED)
    incomplete = harness.execute({"cmd": "flash_run", "baud": 460800, "boot_pin": 24, "en_pin": 25})
    assert not incomplete["pass"] and "incomplete" in incomplete["error"]
    assert not harness.execute({"cmd": "flash_chunk", "index": 0, "data": "AAAA"})["pass"]

    class Client:
        def command(self, **command):
            return harness.execute(command)

    harness.execute({"cmd": "configure_pin", "pin": 25, "direction": "tx"})
    result = flash(Client(), [{"offset": 0x10000, "name": "firmware.bin", "data": FIXED}])
    assert result["flashed"] and result["build_tag"].endswith("DEMO_BUG=0")
    assert result["pins"] == [{"pin": 24, "direction": None, "value": 0},
                              {"pin": 25, "direction": None, "value": 0}]
    assert not backend.demo_bug and 25 not in backend.gpio_config
    assert backend.flasher.staged is None
    flash(Client(), [{"offset": 0x10000, "data": BUGGY}], boot_pin=None)
    assert backend.demo_bug
    corrupt = {**staged(FIXED), "sha256": "0" * 64}
    harness.execute({"cmd": "flash_begin", "images": [corrupt]})
    harness.execute({"cmd": "flash_chunk", "index": 0, "data": base64.b64encode(FIXED).decode()})
    assert "corrupt" in harness.execute({"cmd": "flash_run", "baud": 115200, "boot_pin": None,
                                         "en_pin": 25})["error"]
    harness.execute({"cmd": "select_profile", "profile": "display"})
    assert not harness.execute({"cmd": "flash_begin", "images": [staged(FIXED)]})["pass"]


def test_manifest_and_chunking():
    big = bytes(range(256)) * 600  # 150 KiB spans several chunks
    sent = []

    class Client:
        def command(self, **command):
            sent.append(command)
            if command["cmd"] == "flash_chunk":
                return {"pass": True, "received": 1, "size": len(big)}
            return {"pass": True}

    flash(Client(), [{"offset": 0x10000, "data": big}], progress=sent.append)
    assert sent[0]["images"] == manifest([{"offset": 0x10000, "data": big}])
    chunks = [c for c in sent if c.get("cmd") == "flash_chunk"]
    assert len(chunks) == -(-len(big) // CHUNK_BYTES)
    assert all(len(c["data"]) <= 49152 for c in chunks)
    assert b"".join(base64.b64decode(c["data"]) for c in chunks) == big
    assert sent[-1] == {"cmd": "flash_run", "baud": 460800, "boot_pin": 24, "en_pin": 25}
    with pytest.raises(ValueError):
        images("nope")
    with pytest.raises(ValueError, match="missing"):
        images("fixed", project="/nonexistent")


def test_real_flasher_drives_boot_and_en_then_runs_esptool(monkeypatch, tmp_path):
    timeline = []

    class Device:
        def __init__(self, pin, initial_value=False):
            self.pin = pin
            timeline.append(("open", pin, initial_value))

        def on(self):
            timeline.append(("on", self.pin))

        def off(self):
            timeline.append(("off", self.pin))

        def close(self):
            timeline.append(("close", self.pin))

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        timeline.append(("esptool",))
        return SimpleNamespace(returncode=0, stdout="Hash of data verified.\n", stderr="")

    monkeypatch.setitem(sys.modules, "gpiozero", SimpleNamespace(OutputDevice=Device))
    monkeypatch.setattr("pi.flash.subprocess.run", run)
    monkeypatch.setattr("pi.flash.time.sleep", lambda seconds: None)
    backend = RealBackend(serial_port="/dev/ttyAMA0")
    backend.uart = SimpleNamespace(close=lambda: timeline.append(("uart_closed",)))
    flasher = backend.flasher
    assert isinstance(flasher, Flasher) and flasher.is_real
    flasher.begin([staged(FIXED, 0x1000), staged(BUGGY, 0x10000)])
    flasher.chunk(0, base64.b64encode(FIXED).decode())
    flasher.chunk(1, base64.b64encode(BUGGY).decode())
    result = backend.flash_run(921600, 24, 25)
    assert backend.uart is None and result["build_tag"].endswith("DEMO_BUG=1")
    assert timeline == [("uart_closed",), ("open", 25, True), ("open", 24, True), ("off", 24),
                        ("off", 25), ("on", 25), ("esptool",), ("on", 24), ("off", 25), ("on", 25),
                        ("close", 24), ("close", 25)]
    args = calls[0]
    assert args[1:3] == ["-m", "esptool"] and "/dev/ttyAMA0" in args and "921600" in args
    assert args[args.index("--before") + 1] == "no_reset"
    assert args[args.index("--after") + 1] == "no_reset"
    assert args[args.index("write_flash") + 2] == "0x1000" and "0x10000" in args
    assert flasher.staged is None

    def fail(args, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="A fatal error occurred")

    monkeypatch.setattr("pi.flash.subprocess.run", fail)
    flasher.begin([staged(FIXED)])
    flasher.chunk(0, base64.b64encode(FIXED).decode())
    with pytest.raises(ValueError, match="esptool failed"):
        backend.flash_run(460800, None, 25)


def test_flash_intents():
    assert interpret("flash the fixed firmware")[0] == {"action": "flash", "env": "fixed"}
    assert interpret("Flash firmware")[0] == {"action": "flash", "env": "esp32dev"}
    assert interpret("flash the buggy build")[0]["env"] == "esp32dev"


@pytest.fixture
def console(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(server, "load_images", lambda env: [
        {"offset": 0x1000, "name": "bootloader.bin", "data": b"\xe9boot"},
        {"offset": 0x10000, "name": "firmware.bin", "data": FIXED if env == "fixed" else BUGGY}])
    monkeypatch.setattr(server, "build", lambda env: "built")
    app = server.ConsoleApp(profile="esp32_safety")
    with server.HTTPServer(("127.0.0.1", 0), app) as http:
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{http.server_address[1]}", app
        http.shutdown()
        thread.join()
    app.close()


def api(base, path, data=None):
    request = Request(base + path, json.dumps(data).encode() if data is not None else None,
                      {"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def test_http_flash_gates_then_fixes_the_bug(console):
    base, app = console
    waiting = api(base, "/api/flash", {"env": "fixed"})
    assert waiting["status"] == "waiting_for_connections" and waiting["missing"] == [14, 15, 25, 24]
    for pin in (17, 18, 27, 22, 23, 14, 15, 25):
        api(base, "/api/requests/confirm", {"pi_pin": pin})
    assert api(base, "/api/profiles/run", {})["pass"] is False
    assert api(base, "/api/flash", {"env": "fixed"})["missing"] == [24]
    assert api(base, "/api/prompt", {"prompt": "flash the fixed firmware"})["flash"]["missing"] == [24]
    cursor = app.sequence
    result = api(base, "/api/flash", {"env": "fixed", "manual_boot": True, "build": True})
    assert result["status"] == "complete" and result["manual_boot"] and result["env"] == "fixed"
    assert result["build_tag"].endswith("DEMO_BUG=0") and len(result["images"]) == 2
    assert api(base, "/api/state")["report"] is None
    stages = [e["data"].get("stage") for e in app.events if e["id"] > cursor and e["level"] == "FLASH"]
    assert stages[:3] == ["build", "start", "upload"] and stages[-1] == "done"
    chunk_tx = next(e for e in app.events if e["level"] == "TX" and e["data"]["cmd"] == "flash_chunk")
    assert chunk_tx["data"]["data"].startswith("<") and "base64" in chunk_tx["data"]["data"]
    assert api(base, "/api/profiles/run", {})["pass"] is True
    api(base, "/api/requests/confirm", {"pi_pin": 24})
    prompt = api(base, "/api/prompt", {"prompt": "flash firmware"})
    assert prompt["flash"]["status"] == "complete" and "DEMO_BUG=1" in prompt["reply"]
    assert api(base, "/api/profiles/run", {})["pass"] is False
    api(base, "/api/profiles/select", {"profile": "display"})
    with pytest.raises(HTTPError) as exc:
        api(base, "/api/flash", {"env": "fixed"})
    assert exc.value.code == 400
