"""rig-01 on the Virtual Rig: the Esp32Dut model of firmware/esp32_dut/esp32_dut.ino and the flash ops."""
import base64
import json

import pytest

from aperture.rig.virtual import list_scenarios
from aperture.transport import VirtualTransport

BOOTLOADER, APP = 0x1000, 0x10000
BOOT_LCD = ["Aperture OS", "rig-01 online"]
BOOT_LED = {"mode": "blink", "hz": 2.0}
BANNER = b"BOOT OK\r\n"  # Serial.println ends lines with CRLF
HZ_ERROR = b"ERR LED hz must be 0..50\r\n"


@pytest.fixture
def rig01():
    transport = VirtualTransport(scenario="rig01")
    transport.connect()
    transport.take_control().raise_for_error()
    yield transport
    transport.disconnect()


def ok(transport, op, **args):
    return transport.request(op, **args).raise_for_error().result


def err(transport, op, **args):
    response = transport.request(op, **args)
    assert not response.ok, response
    return response.err


def received(transport, port, ms=20):
    """Advance the virtual clock by ``ms`` and return everything the DUT sent on ``port``."""
    transport.rig.advance(ms * 1000)
    return base64.b64decode(ok(transport, "uart.read", port=port)["data"])


def booted(transport):
    port = ok(transport, "uart.open", tx=14, rx=15, baud=115200)["port"]
    assert received(transport, port, ms=400) == BANNER
    return port


def send(transport, port, data):
    ok(transport, "uart.write", port=port, data=data if isinstance(data, bytes) else data.encode() + b"\n")
    return received(transport, port)


def dut(transport):
    return transport.rig.behavior("dut").describe()


def level(transport, pin):
    return ok(transport, "pin.read", pin=pin)["level"]


def test_scenario_is_listed_and_boots_with_the_banner(rig01):
    assert {"name": "rig01", "title": "rig-01: ESP32 DevKit V1 + 16x2 LCD + status LED"}.items() <= next(
        s for s in list_scenarios() if s["name"] == "rig01").items()
    port = ok(rig01, "uart.open", tx=14, rx=15, baud=115200)["port"]
    assert received(rig01, port, ms=250) == b""  # setup() has not printed yet
    assert received(rig01, port, ms=100) == BANNER
    state = json.loads(json.dumps(dut(rig01)))  # the console serialises describe()
    assert state["type"] == "Esp32Dut" and state["lcd"] == BOOT_LCD and state["led"] == BOOT_LED


def test_lcd_commands_split_on_the_first_bar_and_clip_to_16_columns(rig01):
    port = booted(rig01)
    assert send(rig01, port, "LCD Hello|World") == b"OK LCD\r\n"
    assert dut(rig01)["lcd"] == ["Hello", "World"]
    assert send(rig01, port, "LCD  first line is too long|a|b") == b"OK LCD\r\n"
    assert dut(rig01)["lcd"] == [" first line is t", "a|b"]
    assert send(rig01, port, "LCD only the top") == b"OK LCD\r\n"
    assert dut(rig01)["lcd"] == ["only the top", ""]


def test_led_commands_and_their_errors(rig01):
    port = booted(rig01)
    assert send(rig01, port, "LED BLINK 5") == b"OK LED 5\r\n"
    assert dut(rig01)["led"] == {"mode": "blink", "hz": 5.0}
    assert send(rig01, port, "LED BLINK 2.50  ") == b"OK LED 2.50\r\n"  # trimmed, hz echoed as sent
    assert send(rig01, port, b"LED ON\r\n") == b"OK LED ON\r\n"
    assert dut(rig01)["led"] == {"mode": "on", "hz": 2.5}
    assert send(rig01, port, "LED OFF") == b"OK LED OFF\r\n"
    for bad in ("LED BLINK 0", "LED BLINK -1", "LED BLINK 51", "LED BLINK fast"):
        assert send(rig01, port, bad) == HZ_ERROR
    assert dut(rig01)["led"] == {"mode": "off", "hz": 2.5}  # rejected rates change nothing
    for unknown in ("LED DIM", "led on", "LCD", "LED BLINK"):
        assert send(rig01, port, unknown) == b"ERR unknown command\r\n"
    assert send(rig01, port, "   ") == b""  # blank lines are ignored
    assert send(rig01, port, b"LED ON\nLCD a|b\n") == b"OK LED ON\r\nOK LCD\r\n"


def test_en_low_holds_the_chip_in_reset_and_release_reboots_it(rig01):
    port = booted(rig01)
    send(rig01, port, "LCD before|reset")
    send(rig01, port, "LED ON")
    ok(rig01, "pin.mode", pin=23, mode="input")
    assert level(rig01, 23) == 1
    ok(rig01, "pin.mode", pin=25, mode="output", initial=0)  # EN low
    assert dut(rig01)["state"] == "RESET" and ok(rig01, "pin.read", pin=23)["floating"]
    assert send(rig01, port, "LED OFF") == b""  # a chip in reset does not answer
    ok(rig01, "pin.write", pin=25, level=1)
    assert received(rig01, port, ms=400) == BANNER
    assert dut(rig01)["lcd"] == BOOT_LCD and dut(rig01)["led"] == BOOT_LED and level(rig01, 23) == 1
    send(rig01, port, "LED OFF")
    ok(rig01, "pin.pulse", pin=25, level=0, us=100_000)
    assert received(rig01, port, ms=400) == BANNER and dut(rig01)["led"] == BOOT_LED
    ok(rig01, "pin.write", pin=25, level=0)
    ok(rig01, "release_all")  # EN back to high-Z: the DevKit pull-up boots the chip
    assert dut(rig01)["state"] == "IDLE"


def test_flash_cycle_closes_the_uart_and_reboots_to_defaults(rig01):
    port = booted(rig01)
    send(rig01, port, "LCD flashing|...")
    send(rig01, port, "LED BLINK 10")
    ok(rig01, "flash.begin")
    assert ok(rig01, "flash.write", offset=BOOTLOADER, data=b"\xe9" * 100) == {"offset": BOOTLOADER, "size": 100}
    assert err(rig01, "flash.run") == "bad_request"  # no app image at 0x10000
    assert received(rig01, port) == b""  # a rejected run leaves the UART open
    ok(rig01, "flash.write", offset=APP, data=b"\xe9" * 32768)
    assert ok(rig01, "flash.write", offset=APP, data=bytes(1000)) == {"offset": APP, "size": 33768}
    ok(rig01, "flash.write", offset=BOOTLOADER, data=b"\xe9" * 100)  # the rejected run consumed it
    result = ok(rig01, "flash.run", baud=460800, manual_boot=True)
    assert result["flashed"] and result["simulated"] and result["seconds"] == 0.0
    assert result["images"] == [{"offset": BOOTLOADER, "size": 100}, {"offset": APP, "size": 33768}]
    assert result["uart_closed"] == [port] and "460800" in result["log"]
    assert err(rig01, "uart.read", port=port) == "bad_request"  # callers must reopen
    assert dut(rig01)["lcd"] == BOOT_LCD and dut(rig01)["led"] == BOOT_LED
    port = booted(rig01)
    assert send(rig01, port, "LED ON") == b"OK LED ON\r\n"
    assert err(rig01, "flash.run") == "bad_request"  # the run cleared the staging


def test_flash_ops_need_control_and_staging_is_capped_at_4_mib(rig01):
    observer = VirtualTransport(scenario="rig01")
    observer.connect()
    assert {err(observer, op) for op in ("flash.begin", "flash.run")} == {"not_controller"}
    observer.disconnect()
    for _ in range(4 * 1024 * 1024 // 32768):
        ok(rig01, "flash.write", offset=APP, data=bytes(32768))
    assert err(rig01, "flash.write", offset=BOOTLOADER, data=b"\x00") == "range"
    result = ok(rig01, "flash.run")
    assert result["images"] == [{"offset": APP, "size": 4 * 1024 * 1024}] and result["uart_closed"] == []
    assert "115200" in result["log"]
    ok(rig01, "flash.write", offset=APP, data=b"\x00")
    assert err(rig01, "flash.run", baud=100) == "range"


def test_safety_controller_keeps_the_planted_bug(rig01):
    for pin in (22, 23):
        ok(rig01, "pin.mode", pin=pin, mode="input")
    ok(rig01, "pin.mode", pin=17, mode="output", initial=0)
    ok(rig01, "pin.mode", pin=27, mode="output", initial=0)
    ok(rig01, "pin.mode", pin=18, mode="pwm")
    ok(rig01, "pin.pwm", pin=18, freq_hz=100, duty=0.5)
    ok(rig01, "pin.write", pin=17, level=1)
    assert level(rig01, 22) == 1 and level(rig01, 23) == 1  # nominal: actuator runs, relay engaged
    ok(rig01, "pin.write", pin=27, level=1)  # fault while the trigger is high
    assert level(rig01, 22) == 0 and level(rig01, 23) == 1  # DEMO_BUG: relay left engaged
    ok(rig01, "pin.write", pin=17, level=0)
    assert level(rig01, 23) == 0  # fault with the trigger low releases it
    ok(rig01, "pin.write", pin=27, level=0)
    assert level(rig01, 23) == 1 and dut(rig01)["state"] == "IDLE"
