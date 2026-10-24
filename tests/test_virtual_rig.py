import pytest

from aperture.protocol import ProtocolError
from aperture.rig import VirtualRig, load_scenario
from aperture.rig.behaviors import make_behavior
from aperture.rig.virtual import list_scenarios
from aperture.transport import TransportError, VirtualTransport


def rig_with(scenario=None, **kwargs):
    rig = VirtualRig(load_scenario(scenario) if scenario else None, **kwargs)
    events = []
    rig.on_event(events.append)
    return rig, events


def run(rig, op, **args):
    run.counter += 1
    return rig.execute({"v": 1, "id": run.counter, "op": op, **args})


run.counter = 0


def controlled(scenario=None):
    rig, events = rig_with(scenario)
    run(rig, "hello", client="test", proto=[1])
    run(rig, "control.take")
    return rig, events


def test_manifest_and_scenarios():
    rig, _ = rig_with()
    manifest = run(rig, "hello", client="t", proto=[1])["manifest"]
    assert manifest["bridge"] == "virtual-rig" and manifest["proto"] == 1 and len(manifest["pins"]) == 26
    names = {s["name"] for s in list_scenarios()}
    assert {"empty", "loopback", "uart_echo", "button_led", "esp32_safety", "esp32_safety_fixed"} <= names
    with pytest.raises(FileNotFoundError):
        load_scenario("nope")
    with pytest.raises(ValueError):
        make_behavior({"type": "Nope"})


def test_control_handshake_and_watchdog():
    rig, events = rig_with()
    with pytest.raises(ProtocolError) as info:
        run(rig, "pin.mode", pin=17, mode="output")
    assert info.value.code == "not_controller"
    assert run(rig, "list_pins")["controller"] is None  # read-only ops never need control
    run(rig, "hello", client="A", proto=[1])
    took = run(rig, "control.take", watchdog_s=2)
    assert took["controller"] and took["watchdog_s"] == 2
    assert events[-1]["ev"] == "control.changed" and events[-1]["controller"] == "A"
    run(rig, "hello", client="B", proto=[1])
    with pytest.raises(ProtocolError) as info:
        run(rig, "control.take", token="other")
    assert info.value.code == "busy" and info.value.extra["controller"] == "A"
    with pytest.raises(ProtocolError):
        run(rig, "control.take", token="other", force=True)  # not idle yet
    run(rig, "pin.mode", pin=17, mode="output", initial=1)
    assert rig.pins[17].level == 1
    rig.advance(2_500_000)  # watchdog expires: outputs released
    assert rig.controller is None and rig.pins[17].mode == "disabled"
    assert events[-1]["ev"] == "control.lost" and events[-1]["reason"] == "watchdog"
    run(rig, "control.take", token="other", watchdog_s=60)
    rig.advance(31_000_000)  # B idle for 31 s (> idle_s) but within its watchdog
    run(rig, "hello", client="C", proto=[1])
    assert run(rig, "control.take", force=True)["controller"]
    assert events[-1]["previous"] == "B" and events[-1]["reason"] == "force"
    with pytest.raises(ProtocolError):
        run(rig, "control.release", token="wrong")


def test_pin_modes_pulls_and_rw():
    rig, _ = controlled()
    assert run(rig, "pin.mode", pin=4, mode="input_pullup")["level"] == 1
    assert run(rig, "pin.mode", pin=4, mode="input_pulldown")["level"] == 0
    assert run(rig, "pin.read", pin=4) == {"pin": 4, "level": 0, "floating": False}
    assert run(rig, "pin.mode", pin=17, mode="output", initial=1)["level"] == 1
    assert run(rig, "pin.write", pin=17, level=0)["level"] == 0
    for op, args, code in [("pin.write", {"pin": 4, "level": 1}, "pin_mode"),
                           ("pin.read", {"pin": 5}, "pin_mode"),
                           ("pin.pwm", {"pin": 17, "freq_hz": 10, "duty": .5}, "pin_mode"),
                           ("pin.pulse", {"pin": 17, "level": 1, "us": 2_000_000}, "range")]:
        with pytest.raises(ProtocolError) as info:
            run(rig, op, **args)
        assert info.value.code == code
    listed = run(rig, "list_pins")
    assert listed["controller"] == "test" and {p["pin"]: p["mode"] for p in listed["pins"]}[17] == "output"
    run(rig, "release_all")
    assert all(p["mode"] == "disabled" for p in run(rig, "list_pins")["pins"])


def test_denied_pins_are_unsupported():
    rig, _ = rig_with()
    rig.scenario["denied_pins"] = [17]
    run(rig, "hello", client="t", proto=[1])
    run(rig, "control.take")
    with pytest.raises(ProtocolError) as info:
        run(rig, "pin.mode", pin=17, mode="output")
    assert info.value.code == "unsupported"
    assert 17 not in {p["bcm"] for p in rig.manifest()["pins"]}


def test_loopback_pulse_and_stream():
    rig, events = controlled("loopback")
    run(rig, "pin.mode", pin=27, mode="input")
    run(rig, "stream.set", pins=[27], on=True)
    run(rig, "pin.mode", pin=17, mode="output", initial=0)
    assert rig.pins[27].level == 0 and not rig.pins[27].floating
    run(rig, "pin.write", pin=17, level=1)
    assert rig.pins[27].level == 1
    changes = [e for e in events if e["ev"] == "pin.change"]
    assert changes[-1]["pin"] == 27 and changes[-1]["level"] == 1
    t0 = rig.now_us()
    result = run(rig, "pin.pulse", pin=17, level=0, us=500)
    assert result["edges"] == [[t0, 0], [t0 + 500, 1]] and rig.pins[27].level == 1
    run(rig, "pin.mode", pin=27, mode="output", initial=0)  # both ends driven -> contention warning
    assert rig.pins[27].contention and events[-1]["ev"] == "rig.warning"
    run(rig, "pin.mode", pin=17, mode="disabled")
    run(rig, "pin.mode", pin=27, mode="input")
    assert rig.pins[27].floating


def test_pwm_capture_edge_list():
    rig, events = controlled()
    run(rig, "pin.mode", pin=18, mode="pwm")
    run(rig, "pin.pwm", pin=18, freq_hz=100, duty=0.25)
    capture = run(rig, "capture.start", pins=[18], rate_hz=10_000, duration_ms=100)
    with pytest.raises(ProtocolError) as info:
        run(rig, "capture.start", pins=[18], rate_hz=10, duration_ms=1)
    assert info.value.code == "busy"
    rig.advance(100_000)
    done = [e for e in events if e["ev"] == "capture.done"][-1]
    assert done["capture_id"] == capture["capture_id"] and not done["cancelled"]
    rising = [e for e in done["edges"] if e[2] == 1]
    assert 9 <= len(rising) <= 11
    high = next(e for e in done["edges"] if e[2] == 1)[0]
    low = next(e[0] for e in done["edges"] if e[2] == 0 and e[0] > high)
    assert abs((low - high) - 2500) <= 100  # 25 % of a 10 ms period
    run(rig, "pin.pwm", pin=18, freq_hz=100, duty=0)
    rig.advance(50_000)
    assert rig.pins[18].level == 0 and rig.pins[18].pwm is None
    with pytest.raises(ProtocolError):
        run(rig, "capture.start", pins=[18], rate_hz=1_000_000, duration_ms=1)
    cap = run(rig, "capture.start", pins=[18], rate_hz=100, duration_ms=1000)
    run(rig, "capture.cancel", capture_id=cap["capture_id"])
    assert events[-1]["ev"] == "capture.done" and events[-1]["cancelled"]


def test_button_led_scenario():
    rig, events = controlled("button_led")
    run(rig, "pin.mode", pin=17, mode="input")
    run(rig, "pin.mode", pin=22, mode="input")
    run(rig, "stream.set", pins=[17, 22], on=True)
    assert rig.pins[22].level == 1 and rig.pins[17].level == 0  # idle: button high, LED off
    t0 = rig.now_us()
    rig.behavior("button").press()
    assert rig.pins[22].level == 0 and rig.pins[17].level == 0
    rig.advance(20_000)
    led = next(e for e in events if e["ev"] == "pin.change" and e["pin"] == 17 and e["level"] == 1)
    assert led["t"] - t0 == 12_000
    rig.behavior("button").release()
    rig.advance(20_000)
    assert rig.pins[17].level == 0
    # the rig can also drive the button line itself (output overrides the pull-up)
    run(rig, "pin.mode", pin=22, mode="output", initial=0)
    rig.advance(20_000)
    assert rig.pins[17].level == 1
    blink = run(rig, "capture.start", pins=[24], rate_hz=1000, duration_ms=1000)
    rig.advance(1_000_000)
    done = next(e for e in events if e["ev"] == "capture.done" and e["capture_id"] == blink["capture_id"])
    assert len([e for e in done["edges"] if e[2] == 1]) == 2


def test_uart_echo_scenario():
    rig, events = controlled("uart_echo")
    port = run(rig, "uart.open", tx=14, rx=15, baud=115200)["port"]
    with pytest.raises(ProtocolError):
        run(rig, "pin.mode", pin=14, mode="output")
    assert run(rig, "uart.read", port=port, timeout_ms=0)["data"] == b""
    assert run(rig, "uart.read", port=port, timeout_ms=400)["data"] == b"BOOT OK\n"
    assert run(rig, "uart.write", port=port, data=b"ping\n")["n"] == 5
    rig.advance(5_000)
    assert run(rig, "uart.read", port=port)["data"] == b"ping\npong\n"
    rx = [e for e in events if e["ev"] == "uart.rx"]
    assert b"".join(e["data"] for e in rx) == b"BOOT OK\nping\npong\n"
    assert run(rig, "uart.read", port=port, max=0)["data"] == b""
    run(rig, "uart.close", port=port)
    with pytest.raises(ProtocolError):
        run(rig, "uart.read", port=port)
    with pytest.raises(ProtocolError) as info:
        run(rig, "uart.open", tx=14, rx=15, baud=100)
    assert info.value.code == "range"


def test_i2c_and_spi_devices():
    rig, _ = controlled("esp32_safety")
    assert run(rig, "i2c.scan", bus=1)["addrs"] == [0x68]
    assert run(rig, "i2c.read", bus=1, addr=0x68, n=1, reg=0x75)["data"] == b"\x68"
    run(rig, "i2c.write", bus=1, addr=0x68, data=b"\x3b\x09")
    assert run(rig, "i2c.read", bus=1, addr=0x68, n=2)["data"] == b"\x09\x02"
    with pytest.raises(ProtocolError) as info:
        run(rig, "i2c.read", bus=1, addr=0x10, n=1)
    assert info.value.code == "io"
    with pytest.raises(ProtocolError) as info:
        run(rig, "i2c.scan", bus=0)
    assert info.value.code == "unsupported"
    assert run(rig, "spi.xfer", bus=0, cs=0, data=b"\x01\x02")["data"] == b"\x00\x00"
    rig.add_behavior(make_behavior({"type": "SpiLoopback", "cs": 0}))
    assert run(rig, "spi.xfer", bus=0, cs=0, data=b"\x01\x02")["data"] == b"\x01\x02"


@pytest.mark.parametrize("scenario,expect_bug", [("esp32_safety", True), ("esp32_safety_fixed", False)])
def test_scripted_dut_planted_bug(scenario, expect_bug):
    rig, _ = controlled(scenario)
    for pin in (22, 23):
        run(rig, "pin.mode", pin=pin, mode="input")
    run(rig, "pin.mode", pin=17, mode="output", initial=0)
    run(rig, "pin.mode", pin=27, mode="output", initial=0)
    run(rig, "pin.mode", pin=18, mode="pwm")
    port = run(rig, "uart.open", tx=14, rx=15, baud=115200)["port"]
    assert rig.pins[23].level == 1 and rig.pins[22].level == 0
    run(rig, "pin.pwm", pin=18, freq_hz=100, duty=0.5)
    run(rig, "pin.write", pin=17, level=1)
    assert rig.pins[22].level == 1  # actuator runs
    run(rig, "pin.write", pin=27, level=1)  # fault while trigger high
    assert rig.pins[22].level == 0
    assert rig.pins[23].level == (1 if expect_bug else 0)
    rig.advance(400_000)
    telemetry = run(rig, "uart.read", port=port)["data"].decode()
    assert "BOOT OK" in telemetry and "state=FAULT" in telemetry and "safety=0" in telemetry
    run(rig, "pin.write", pin=17, level=0)
    run(rig, "pin.write", pin=27, level=0)
    assert rig.pins[23].level == 1 and rig.behavior("esp32").state == "IDLE"


def test_virtual_transport_lifecycle_and_errors():
    transport = VirtualTransport(scenario="loopback", latency_us=100)
    acks, events = [], []
    transport.on_ack(lambda rid, t: acks.append((rid, t)))
    transport.on_event(events.append)
    manifest = transport.connect()
    assert manifest["bridge"] == "virtual-rig" and transport.clock_offset_us is not None
    assert transport.describe()["scenario"] == "loopback"
    assert transport.take_control().ok and transport.control_token
    bad = transport.request("pin.write", pin=17, level=1)
    assert not bad.ok and bad.err == "pin_mode"
    transport.request("pin.mode", pin=27, mode="input").raise_for_error()
    transport.request("stream.set", pins=[27], on=True)
    ok = transport.request("pin.mode", pin=17, mode="output", initial=1).raise_for_error()
    assert ok.result["level"] == 1 and acks[-1][0] == ok.id
    assert events[-1]["ev"] == "pin.change" and events[-1]["pin"] == 27
    with pytest.raises(ProtocolError):
        transport.request("pin.pwm", pin=17, freq_hz=1, duty=.5).raise_for_error()
    transport.disconnect()
    assert not transport.connected and transport.rig.pins[17].mode == "disabled"
    with pytest.raises(TransportError):
        transport.request("ping")
    assert transport.connect()["proto"] == 1  # reconnect works
