"""ESP32 host tools: program_esp32, led, lcd_print, control re-take, TCP targets and intents.

A scripted bridge double stands in for the Pi, so nothing here depends on the Virtual Rig's
flash ops, the esp32_dut firmware or a PlatformIO install.
"""
import json
import subprocess
from types import SimpleNamespace

import pytest

from aperture import firmware as fw
from aperture.intent import interpret
from aperture.session import Session
from aperture.tools import (
    SPECS,
    ToolError,
    Tools,
    default_transport_factory,
    schemas,
    validate_args,
)
from aperture.transport.base import Response, Transport
from aperture.transport.tcp import TcpTransport

APP = bytes(range(256)) * 300  # 76800 bytes: two full 32 KiB chunks and a tail


class FakeBridge(Transport):
    """Protocol v1 bridge double following the Pi bridge contract; records every op it receives."""

    kind = "tcp"

    def __init__(self, bridge: str = "aperture-bridge"):
        super().__init__("test-host")
        self.bridge = bridge
        self.rig = SimpleNamespace(clock=SimpleNamespace(realtime=False))  # uart_expect counts, never sleeps
        self.ops: list[tuple[str, dict]] = []
        self.failures: dict[str, list[tuple[str, str]]] = {}
        self.replies: dict[bytes, bytes] = {}
        self.staged: dict[int, bytes] = {}
        self.ports: dict[int, tuple[int, int, int]] = {}
        self.rx: dict[int, bytearray] = {}
        self.next_port = 0
        self.booting = False

    def _connect(self) -> None:
        pass

    def _disconnect(self) -> None:
        pass

    def _send(self, message: dict, timeout: float) -> Response:
        op = message["op"]
        args = {k: v for k, v in message.items() if k not in ("v", "id", "op")}
        self.ops.append((op, args))
        if self.failures.get(op):
            err, msg = self.failures[op].pop(0)
            return Response(message["id"], False, 0, {}, err, msg)
        handler = getattr(self, "op_" + op.replace(".", "_"), None)
        return Response(message["id"], True, 0, handler(**args) if handler else {})

    def sent(self, op: str) -> list[dict]:
        return [args for name, args in self.ops if name == op]

    def op_hello(self, **_):
        return {"manifest": {"proto": 1, "bridge": self.bridge, "limits": {}}}

    def op_control_take(self, **_):
        return {"controller": True, "token": "tok", "watchdog_s": 5.0}

    def op_pin_write(self, pin, level):
        return {"level": level}

    def op_pin_read(self, pin):
        return {"level": 1}

    def op_flash_begin(self):
        self.staged = {}
        return {}

    def op_flash_write(self, offset, data):
        self.staged[offset] = self.staged.get(offset, b"") + data
        return {"offset": offset, "size": len(self.staged[offset])}

    def op_flash_run(self, baud=115200, manual_boot=False):
        closed, self.ports = sorted(self.ports), {}
        images = [{"offset": offset, "size": len(data)} for offset, data in sorted(self.staged.items())]
        self.staged, self.booting = {}, True
        return {"flashed": True, "seconds": 1.5, "images": images, "uart_closed": closed, "log": "verified"}

    def op_uart_open(self, tx, rx, baud):
        port, self.next_port = self.next_port, self.next_port + 1
        self.ports[port] = (tx, rx, baud)
        self.rx[port] = bytearray(b"BOOT OK\r\n" if self.booting else b"")
        self.booting = False
        return {"port": port, "tx": tx, "rx": rx, "baud": baud}

    def op_uart_close(self, port):
        del self.ports[port]
        return {}

    def op_uart_write(self, port, data):
        self.rx[port] += self.replies.get(data, b"")
        return {"n": len(data)}

    def op_uart_read(self, port, max=4096, timeout_ms=0):
        data = bytes(self.rx[port][:max])
        del self.rx[port][:max]
        return {"data": data}


def rig(bridge: str = "aperture-bridge", policy: str | None = "off"):
    fake = FakeBridge(bridge)
    session = Session(actor="tester", persist=False)
    session.connect(fake)
    fake.ops.clear()
    return fake, session, Tools(session, wiring_policy=policy)


def cached_build(monkeypatch, images):
    monkeypatch.setattr(fw, "has_build", lambda project, env=fw.DEFAULT_ENV: True)
    monkeypatch.setattr(fw, "images", lambda project, env=fw.DEFAULT_ENV: images)


def stages(session, req_id):
    return [e["stage"] for e in session.events if e["kind"] == "request.stage" and e["req"] == req_id]


# --- program_esp32 -----------------------------------------------------------------------------
def test_program_esp32_builds_stages_in_chunks_and_waits_for_boot(monkeypatch):
    fake, session, tools = rig()
    built = []
    monkeypatch.setattr(fw, "build", lambda project, env=fw.DEFAULT_ENV: built.append(project))
    monkeypatch.setattr(fw, "images", lambda project, env=fw.DEFAULT_ENV:
                        [(0x1000, b"B" * 10), (0x8000, b"P" * 5), (0x10000, APP)])

    result = tools.call("program_esp32")

    assert built == ["esp32_dut"]
    assert [op for op, _ in fake.ops][:8] == ["flash.begin"] + ["flash.write"] * 5 + ["flash.run", "uart.open"]
    writes = fake.sent("flash.write")
    assert [(w["offset"], len(w["data"])) for w in writes] == [(0x1000, 10), (0x8000, 5), (0x10000, 32768),
                                                               (0x10000, 32768), (0x10000, 11264)]
    assert b"".join(w["data"] for w in writes if w["offset"] == 0x10000) == APP
    assert fake.sent("flash.run") == [{"baud": 115200, "manual_boot": False}]
    assert fake.sent("uart.open") == [{"tx": 14, "rx": 15, "baud": 115200}]
    assert result["flashed"] is True and result["boot_ok"] is True and result["build"] == "built"
    assert result["images"] == [{"offset": 0x1000, "size": 10}, {"offset": 0x8000, "size": 5},
                                {"offset": 0x10000, "size": len(APP)}] and result["port"] == 0
    flash_requests = [r for r in session.requests.values() if r.op.startswith("flash.")]
    assert len(flash_requests) == 7
    assert all(stages(session, r.id) == ["requested", "executed", "observed"] for r in flash_requests)


def test_program_esp32_on_virtual_rig_uses_placeholders_without_platformio(monkeypatch):
    fake, _, tools = rig(bridge="virtual-rig")
    monkeypatch.setattr(fw, "find_pio", lambda: None)
    monkeypatch.setattr(fw, "has_build", lambda project, env=fw.DEFAULT_ENV: False)
    monkeypatch.setattr(fw, "build", lambda *a, **k: pytest.fail("the Virtual Rig must not build"))

    result = tools.call("program_esp32")

    assert result["build"] == "placeholder" and result["boot_ok"] is True
    offsets = [w["offset"] for w in fake.sent("flash.write")]
    assert 0x10000 in offsets and offsets == [offset for offset, _ in fw.placeholder_images()]
    cached_build(monkeypatch, [(0x10000, b"\xe9app")])
    assert tools.call("program_esp32")["build"] == "cached"


def test_program_esp32_on_hardware_needs_platformio_or_a_build(monkeypatch, tmp_path):
    (tmp_path / "esp32_dut").mkdir()
    (tmp_path / "esp32_dut" / "platformio.ini").write_text("[env:esp32dev]\n")
    monkeypatch.setattr(fw, "FIRMWARE_DIR", tmp_path)
    monkeypatch.setattr(fw, "find_pio", lambda: None)
    fake, _, tools = rig()
    with pytest.raises(ToolError) as err:
        tools.call("program_esp32")
    assert err.value.code == "build_failed" and "PlatformIO not found" in err.value.msg
    with pytest.raises(ToolError) as err:
        tools.call("program_esp32", build=False)
    assert err.value.code == "bad_request" and "not been built" in err.value.msg
    with pytest.raises(ToolError) as err:
        tools.call("program_esp32", firmware="../../etc")
    assert err.value.code == "bad_request"
    assert fake.ops == []


def test_program_esp32_requests_labelled_wiring_before_anything_else(monkeypatch):
    fake, session, tools = rig(policy=None)
    with pytest.raises(ToolError) as err:
        tools.call("program_esp32", manual_boot=True)
    assert err.value.code == "wiring_required" and fake.ops == []
    assert sorted(session.pending_wiring) == [14, 15, 25]
    assert session.pending_wiring[25]["dut_pin"] == "EN" and session.pending_wiring[15]["dir"] == "in"
    for pin in (14, 15, 25):
        session.confirm_wiring(pin, True)
    cached_build(monkeypatch, [(0x10000, b"\xe9app")])
    assert tools.call("program_esp32", build=False, manual_boot=True)["boot_ok"] is True
    assert fake.sent("flash.run") == [{"baud": 115200, "manual_boot": True}]
    with pytest.raises(ToolError) as err:
        tools.call("program_esp32", build=False)
    assert err.value.code == "wiring_required" and session.pending_wiring[24]["dut_pin"] == "BOOT/GPIO0"


def test_flash_drops_cached_uart_ports_and_reopens(monkeypatch):
    fake, _, tools = rig()
    cached_build(monkeypatch, [(0x10000, b"\xe9app")])
    fake.replies[b"LED ON\n"] = b"OK LED ON\r\n"
    assert tools.call("led", state="on")["port"] == 0

    flashed = tools.call("program_esp32", build=False)

    assert flashed["uart_closed"] == [0] and flashed["port"] == 1 and flashed["build"] == "cached"
    assert tools.call("led", state="on")["port"] == 1
    assert len(fake.sent("uart.open")) == 2


def test_failed_flash_forgets_ports_and_a_staging_mismatch_aborts(monkeypatch):
    fake, _, tools = rig()
    cached_build(monkeypatch, [(0x10000, b"\xe9app")])
    fake.replies[b"LED ON\n"] = b"OK LED ON\r\n"
    tools.call("led", state="on")
    fake.ports.clear()  # the flasher closed the UART before esptool failed
    fake.failures["flash.run"] = [("io", "esptool failed (2): A fatal error occurred")]
    with pytest.raises(ToolError) as err:
        tools.call("program_esp32", build=False)
    assert err.value.code == "io" and err.value.msg == "esptool failed (2): A fatal error occurred"
    assert tools.call("led", state="on")["port"] == 1

    fake.ops.clear()
    monkeypatch.setattr(fake, "op_flash_write", lambda offset, data: {"offset": offset, "size": 0})
    with pytest.raises(ToolError, match="nothing was flashed"):
        tools.call("program_esp32", build=False)
    assert fake.sent("flash.run") == []


def test_missing_boot_banner_is_reported_not_raised(monkeypatch):
    fake, _, tools = rig()
    cached_build(monkeypatch, [(0x10000, b"\xe9app")])
    monkeypatch.setattr(fake, "op_flash_run", lambda **_: {"flashed": True, "seconds": 1.0, "images": [],
                                                          "uart_closed": []})
    result = tools.call("program_esp32", build=False)
    assert result["flashed"] is True and result["boot_ok"] is False and "timeout" in result["boot_error"]


# --- led / lcd_print ---------------------------------------------------------------------------
def test_led_commands_reuse_the_port_and_parse_replies():
    fake, _, tools = rig()
    fake.replies = {b"LED BLINK 5\n": b'{"t_ms":200}\r\nOK LED 5\r\n', b"LED BLINK 2\n": b"OK LED 2\r\n",
                    b"LED BLINK 0.5\n": b"OK LED 0.5\r\n", b"LED OFF\n": b"OK LED OFF\r\n",
                    b"LED BLINK 99\n": b"ERR LED hz must be 0..50\r\n"}
    assert tools.call("led", state="blink", hz=5)["reply"] == "OK LED 5"
    assert tools.call("led", state="blink")["reply"] == "OK LED 2"
    assert tools.call("led", state="blink", hz=0.5)["reply"] == "OK LED 0.5"
    assert tools.call("led", state="off") == {"reply": "OK LED OFF", "port": 0, "command": "LED OFF"}
    with pytest.raises(ToolError) as err:
        tools.call("led", state="blink", hz=99)
    assert err.value.code == "dut_error" and err.value.msg == "ERR LED hz must be 0..50"
    assert fake.sent("uart.open") == [{"tx": 14, "rx": 15, "baud": 115200}]
    assert [w["data"] for w in fake.sent("uart.write")] == [b"LED BLINK 5\n", b"LED BLINK 2\n", b"LED BLINK 0.5\n",
                                                            b"LED OFF\n", b"LED BLINK 99\n"]


def test_stale_uart_input_is_flushed_before_a_command():
    fake, _, tools = rig()
    fake.replies[b"LED ON\n"] = b"OK LED ON\r\n"
    port = tools.call("uart_open", tx=14, rx=15, baud=115200)["port"]
    fake.rx[port] += b"ERR unknown command\r\n" * 400  # more than one read's worth
    assert tools.call("led", state="on") == {"reply": "OK LED ON", "port": port, "command": "LED ON"}
    assert len(fake.sent("uart.open")) == 1


def test_led_without_a_reply_times_out():
    _, _, tools = rig()
    with pytest.raises(ToolError) as err:
        tools.call("led", state="on")
    assert err.value.code == "timeout"


def test_lcd_print_sanitises_lines_and_checks_the_reply():
    fake, _, tools = rig()
    fake.replies[b"LCD HelloWorld|Line two is way \n"] = b"OK LCD\r\n"
    result = tools.call("lcd_print", line1="Hello|World\n", line2="Line two is way too long")
    assert result["reply"] == "OK LCD" and result["port"] == 0
    fake.replies[b"LCD Hi|\n"] = b"ERR unknown command\r\n"
    with pytest.raises(ToolError, match="dut_error"):
        tools.call("lcd_print", line1="Hi")


# --- control -----------------------------------------------------------------------------------
def test_not_controller_retakes_control_once_and_retries():
    fake, session, tools = rig()
    session.record("control.lost", "bridge", reason="watchdog")
    fake.failures["pin.write"] = [("not_controller", "take control first")]

    assert tools.call("write", pin=17, level=1)["level"] == 1

    assert [op for op, _ in fake.ops] == ["pin.write", "control.take", "pin.write", "pin.read"]
    notes = [e["text"] for e in session.events if e["kind"] == "note"]
    assert notes == ["control re-taken after control.lost (watchdog); retrying pin.write"]

    fake.ops.clear()
    fake.failures["pin.write"] = [("not_controller", "take control first")] * 2
    with pytest.raises(ToolError) as err:
        tools.call("write", pin=17, level=1)
    assert err.value.code == "not_controller"
    assert [op for op, _ in fake.ops] == ["pin.write", "control.take", "pin.write"]


def test_retake_refused_or_control_ops_are_not_retried():
    fake, session, tools = rig()
    fake.failures["pin.write"] = [("not_controller", "take control first")]
    fake.failures["control.take"] = [("busy", "rig is controlled by another client")]
    with pytest.raises(ToolError) as err:
        tools.call("write", pin=17, level=1)
    assert err.value.code == "not_controller"
    assert [op for op, _ in fake.ops] == ["pin.write", "control.take"]
    assert not [e for e in session.events if e["kind"] == "note"]

    fake.ops.clear()
    fake.failures["control.release"] = [("not_controller", "token does not hold control")]
    with pytest.raises(ToolError):
        tools.call("release_control")
    assert [op for op, _ in fake.ops] == ["control.release"]


def test_led_reopens_the_uart_when_control_was_retaken_mid_command():
    fake, _, tools = rig()
    fake.replies[b"LED ON\n"] = b"OK LED ON\r\n"
    assert tools.call("led", state="on")["port"] == 0
    fake.ports.clear()  # the watchdog fired: the bridge released everything, port 0 is gone
    fake.failures["uart.read"] = [("not_controller", "take control first"), ("bad_request", "port 0 is not open")]

    assert tools.call("led", state="on") == {"reply": "OK LED ON", "port": 1, "command": "LED ON"}
    assert len(fake.sent("control.take")) == 1


# --- transports and schemas --------------------------------------------------------------------
@pytest.mark.parametrize("target, host, port", [
    ("10.90.45.158", "10.90.45.158", 8765), ("10.90.45.158:9000", "10.90.45.158", 9000),
    (None, "127.0.0.1", 8765), ("", "127.0.0.1", 8765), (":8800", "127.0.0.1", 8800),
    ("[::1]:8765", "::1", 8765), ("rig-01.local", "rig-01.local", 8765)])
def test_tcp_factory_parses_targets(target, host, port):
    transport = default_transport_factory("tcp", target=target)
    assert isinstance(transport, TcpTransport) and (transport.host, transport.port) == (host, port)
    assert not transport.connected


@pytest.mark.parametrize("target", ["pi:http", "pi:70000", "pi:0", "pi:", "a b:1", "::1"])
def test_tcp_factory_rejects_bad_targets(target):
    with pytest.raises(ToolError) as err:
        default_transport_factory("tcp", target=target)
    assert err.value.code == "bad_request"


def test_schemas_cover_every_tool_including_esp32():
    by_name = {s["function"]["name"]: s["function"] for s in schemas()}
    assert set(by_name) == set(SPECS) and {"program_esp32", "led", "lcd_print"} <= set(by_name)
    assert all(callable(getattr(Tools, name)) for name in SPECS)
    assert all(s["description"] and s["parameters"]["type"] == "object" for s in by_name.values())
    assert by_name["program_esp32"]["parameters"]["required"] == []
    assert by_name["led"]["parameters"]["required"] == ["state"]
    assert by_name["led"]["parameters"]["properties"]["state"]["enum"] == ["blink", "on", "off"]
    assert by_name["lcd_print"]["parameters"]["required"] == ["line1"]
    assert validate_args("led", {"state": "blink", "hz": 2.5}) == {"state": "blink", "hz": 2.5}
    for name, bad in (("led", {"state": "strobe"}), ("led", {"state": "on", "hz": "2"}),
                      ("program_esp32", {"manual_boot": "yes"}), ("lcd_print", {"line2": "x"})):
        with pytest.raises(ToolError):
            validate_args(name, bad)
    json.dumps(schemas())


# --- firmware module ---------------------------------------------------------------------------
def test_firmware_build_images_and_placeholders(monkeypatch, tmp_path):
    project = tmp_path / "fw" / "esp32_dut"
    out = project / ".pio" / "build" / "esp32dev"
    out.mkdir(parents=True)
    (project / "platformio.ini").write_text("[env:esp32dev]\n")
    for name, data in (("bootloader.bin", b"B"), ("partitions.bin", b"P"), ("firmware.bin", b"F")):
        (out / name).write_bytes(data)
    pio_home = tmp_path / "pio"
    boot = pio_home / "packages" / "framework-arduinoespressif32" / "tools" / "partitions" / "boot_app0.bin"
    pio = pio_home / "penv" / "bin" / "pio"
    for path, data in ((boot, b"A"), (pio, b"")):
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
    monkeypatch.setattr(fw, "FIRMWARE_DIR", tmp_path / "fw")
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(pio_home))
    monkeypatch.setattr(fw.shutil, "which", lambda name: None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    assert fw.find_pio() == str(pio) and fw.has_build()
    assert fw.build(run=run) == out
    assert calls == [([str(pio), "run", "-d", str(project), "-e", "esp32dev"], fw.BUILD_TIMEOUT_S)]
    assert fw.images() == [(0x1000, b"B"), (0x8000, b"P"), (0xE000, b"A"), (0x10000, b"F")]

    def failing(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="x" * 5000, stderr="main.cpp:3: error: boom")

    def stuck(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"Compiling...")

    with pytest.raises(fw.FirmwareError, match="failed \\(1\\).*boom") as err:
        fw.build(run=failing)
    assert len(str(err.value)) < fw.OUTPUT_TAIL_CHARS + 100
    with pytest.raises(fw.FirmwareError, match="timed out.*Compiling"):
        fw.build(run=stuck)
    with pytest.raises(fw.FirmwareError):
        fw.build("../escape")
    (out / "firmware.bin").unlink()
    with pytest.raises(fw.FirmwareError, match="no firmware.bin"):
        fw.images()
    placeholders = fw.placeholder_images()
    assert [offset for offset, _ in placeholders] == [0x1000, 0x8000, 0xE000, 0x10000]
    assert all(data.startswith(b"\xe9") and len(data) < 64 for _, data in placeholders)


# --- intents -----------------------------------------------------------------------------------
@pytest.mark.parametrize("prompt, tool, args", [
    ("blink the led at 5 hz", "led", {"state": "blink", "hz": 5.0}),
    ("make the LED blink", "led", {"state": "blink"}),
    ("led on", "led", {"state": "on"}),
    ("turn the led on", "led", {"state": "on"}),
    ("turn off the LED", "led", {"state": "off"}),
    ("led off", "led", {"state": "off"}),
    ("print Hello on the LCD", "lcd_print", {"line1": "Hello"}),
    ("show 'Aperture OS' / 'rig-01' on the lcd", "lcd_print", {"line1": "Aperture OS", "line2": "rig-01"}),
    ("write Should pass|Line 2 on the lcd", "lcd_print", {"line1": "Should pass", "line2": "Line 2"}),
    ("program the esp32", "program_esp32", {}),
    ("flash the ESP32 with manual boot", "program_esp32", {"manual_boot": True}),
    ("reflash the esp32 without rebuilding", "program_esp32", {"build": False}),
])
def test_esp32_intents(prompt, tool, args):
    action = interpret(prompt)
    assert action["kind"] == "tools" and action["calls"] == [{"tool": tool, "args": args}]


def test_esp32_intent_replies():
    blink = interpret("blink the led")["reply"]
    assert blink([{"tool": "led", "ok": True, "result": {"reply": "OK LED 2"}}]) == "ESP32 LED: OK LED 2."
    assert "failed" in blink([{"tool": "led", "ok": False, "error": {"msg": "no reply"}}])
    program = interpret("program the esp32")["reply"]
    done = program([{"tool": "program_esp32", "ok": True,
                     "result": {"build": "built", "seconds": 12.5, "boot_ok": True}}])
    assert "12.5 s" in done and "BOOT OK" in done
    assert program([{"ok": False, "error": {"msg": "GPIO24 has not been marked as wired"}}]).startswith(
        "Programming the ESP32 failed")


def test_existing_intents_still_win():
    assert interpret("set GPIO17 high")["calls"][0]["tool"] == "set_mode"
    assert interpret("set the led on gpio 17 high")["calls"][0] == {"tool": "set_mode", "args": {
        "pin": 17, "mode": "output", "initial": 1}}
    assert interpret('send "LED ON" over uart')["calls"][0]["tool"] == "uart_send"
    assert interpret("reset the dut")["calls"][0]["args"] == {"behavior": "dut", "action": "reset"}
    assert interpret("confirm the led blinks within 600 ms")["kind"] == "plan"
    assert interpret("print hello on the lcd and verify it within 1 s")["kind"] == "plan"
    assert interpret("is the led on?")["kind"] == "reply"
    assert interpret("what can you do")["kind"] == "reply"
