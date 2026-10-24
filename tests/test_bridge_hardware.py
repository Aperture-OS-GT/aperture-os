import itertools
import threading
import time

import pytest

from aperture.protocol import OPS, ProtocolError
from aperture.rig.virtual import VirtualRig, load_scenario
from pi.bridge.drivers.fake import FakeDriver
from pi.bridge.hardware import HardwareExecutor
from pi.bridge.hardware_buses import NO_BUSES, BusConfig

IDS = itertools.count(1)
ALL_BUSES = BusConfig(uart="/dev/serial0", i2c=True, spi=True)


def run(rig, op, **args):
    try:
        return {"ok": True, **rig.execute({"v": 1, "id": next(IDS), "op": op, **args})}
    except ProtocolError as exc:
        return {"ok": False, "err": exc.code, "msg": exc.msg, **exc.extra}


@pytest.fixture
def make():
    made = []

    def build(jumpers=((17, 27), (22, 23)), buses=NO_BUSES, driver=None, take=True, **kwargs):
        driver = driver or FakeDriver(jumpers=jumpers)
        rig = HardwareExecutor(driver, "rig-test", buses, **kwargs)
        events = []
        rig.on_event(events.append)
        made.append(rig)
        if take:
            run(rig, "hello", client="alice", proto=[1])
            assert run(rig, "control.take")["ok"]
        return rig, driver, events

    yield build
    for rig in made:
        rig.close()


def names(events, ev):
    return [e for e in events if e["ev"] == ev]


def test_every_protocol_op_has_a_handler():
    missing = [op for op in OPS if not hasattr(HardwareExecutor, "_op_" + op.replace(".", "_"))]
    assert missing == []


def test_startup_releases_every_gpio_except_reserved(make):
    rig, driver, _ = make(buses=BusConfig(uart="/dev/serial0"), take=False)
    disabled = {call[1] for call in driver.calls if call[:3] == ("set_mode", call[1], "disabled")}
    assert disabled == set(rig.pins) - {14, 15}
    assert all(pin.mode == "disabled" for pin in rig.pins.values())


def test_manifest_reflects_driver_buses_and_denied(make):
    rig, _, _ = make(buses=BusConfig(uart="/dev/serial0"), denied=frozenset({4}), take=False)
    manifest = rig.manifest()
    assert manifest["bridge"] == "rig-test" and manifest["fw"].startswith("aperture-bridge-")
    assert manifest["board"] == "Fake Pi (tests)" and manifest["power"]["vbus_ok"] is True
    assert manifest["uart"] == [{"id": 0, "tx": 14, "rx": 15, "device": "/dev/serial0"}]
    assert manifest["spi"] == [] and manifest["i2c"] == []
    assert 4 not in {p["bcm"] for p in manifest["pins"]}


def test_driving_requires_control(make):
    rig, _, _ = make(take=False)
    assert run(rig, "pin.mode", pin=17, mode="output")["err"] == "not_controller"
    assert run(rig, "pin.read", pin=17)["err"] == "pin_mode"


def test_mode_write_read_and_loopback(make):
    rig, driver, _ = make()
    assert run(rig, "pin.mode", pin=27, mode="input")["level"] == 0
    assert run(rig, "pin.mode", pin=17, mode="output", initial=1)["level"] == 1
    assert ("set_mode", 17, "output", 1) in driver.calls  # initial level applied with the mode
    assert run(rig, "pin.read", pin=27) == {"ok": True, "pin": 27, "level": 1, "floating": None}
    assert run(rig, "pin.write", pin=17, level=0)["level"] == 0
    assert run(rig, "pin.read", pin=27)["level"] == 0
    assert run(rig, "pin.write", pin=27, level=1)["err"] == "pin_mode"
    assert run(rig, "pin.mode", pin=22, mode="input_pullup")["level"] == 1
    assert run(rig, "pin.read", pin=22)["floating"] is False


def test_output_mode_keeps_previous_drive_like_the_virtual_rig(make):
    rig, _, _ = make()
    run(rig, "pin.mode", pin=17, mode="output", initial=1)
    assert run(rig, "pin.mode", pin=17, mode="pwm")["level"] == 1
    assert run(rig, "pin.mode", pin=17, mode="output")["level"] == 1
    run(rig, "pin.mode", pin=17, mode="input")
    assert run(rig, "pin.mode", pin=17, mode="output")["level"] == 0


def test_reserved_and_denied_pins(make):
    rig, _, _ = make(buses=BusConfig(uart="/dev/serial0"), denied=frozenset({4}))
    reserved = run(rig, "pin.mode", pin=14, mode="output")
    assert reserved["err"] == "pin_mode" and "reserved for uart0" in reserved["msg"]
    assert run(rig, "pin.mode", pin=4, mode="output")["err"] == "unsupported"
    assert run(rig, "capture.start", pins=[15], rate_hz=100, duration_ms=10)["err"] == "pin_mode"
    pins = {p["pin"]: p for p in run(rig, "list_pins")["pins"]}
    assert pins[14]["reserved"] == "uart0" and pins[14]["level"] is None and 4 not in pins


def test_pulse(make):
    rig, _, events = make()
    run(rig, "pin.mode", pin=17, mode="output", initial=0)
    run(rig, "stream.set", pins=[17], on=True)
    assert run(rig, "pin.pulse", pin=27, level=1, us=100)["err"] == "pin_mode"
    assert run(rig, "pin.pulse", pin=17, level=1, us=2_000_000)["err"] == "range"
    result = run(rig, "pin.pulse", pin=17, level=1, us=2000)
    (start, high), (end, low) = result["edges"]
    assert (high, low) == (1, 0) and end - start >= 2000 and result["jitter_us"] >= 0
    changes = [(e["t"], e["level"]) for e in names(events, "pin.change")]
    assert changes[-2:] == [(start, 1), (end, 0)]
    assert run(rig, "pin.read", pin=17)["level"] == 0


def test_pwm(make):
    rig, driver, _ = make()
    assert run(rig, "pin.pwm", pin=18, freq_hz=100, duty=0.5)["err"] == "pin_mode"
    run(rig, "pin.mode", pin=18, mode="pwm")
    assert run(rig, "pin.pwm", pin=18, freq_hz=20_000, duty=0.5)["err"] == "range"
    assert run(rig, "pin.pwm", pin=18, freq_hz=100, duty=0.25)["ok"]
    assert driver.pwm[18] == (100.0, 0.25)
    assert run(rig, "list_pins")["pins"][[p["pin"] for p in run(rig, "list_pins")["pins"]].index(18)]["pwm"] == {
        "freq_hz": 100.0, "duty": 0.25}
    assert run(rig, "pin.pwm", pin=18, freq_hz=100, duty=1)["ok"] and 18 not in driver.pwm
    assert driver.read(18) == 1
    run(rig, "pin.pwm", pin=18, freq_hz=100, duty=0.5)
    run(rig, "pin.mode", pin=18, mode="output")
    assert 18 not in driver.pwm and ("stop_pwm", 18, 1) in driver.calls


def test_stream_from_inputs_and_commanded_outputs(make):
    rig, driver, events = make()
    run(rig, "pin.mode", pin=24, mode="input")
    assert run(rig, "stream.set", pins=[24, 17], on=True)["pins"] == [17, 24]
    driver.set_external(24, 1)
    driver.set_external(24, 1)  # no change, no duplicate event
    driver.set_external(24, 0)
    run(rig, "pin.mode", pin=17, mode="output", initial=1)
    run(rig, "pin.write", pin=17, level=0)
    changes = [(e["pin"], e["level"]) for e in names(events, "pin.change")]
    assert changes == [(24, 1), (24, 0), (17, 1), (17, 0)]
    assert run(rig, "stream.set", pins=[24], on=False)["pins"] == [17]
    driver.set_external(24, 1)
    assert len(names(events, "pin.change")) == 4


def test_capture_records_quantised_edges(make):
    rig, _, events = make()
    run(rig, "pin.mode", pin=27, mode="input")
    run(rig, "pin.mode", pin=17, mode="output", initial=0)
    started = run(rig, "capture.start", pins=[27], rate_hz=1000, duration_ms=150)
    assert started == {"ok": True, "capture_id": 1, "rate_hz_actual": 1000}
    assert run(rig, "capture.start", pins=[27], rate_hz=1000, duration_ms=10)["err"] == "busy"
    for level in (1, 0, 1):
        time.sleep(0.03)
        run(rig, "pin.write", pin=17, level=level)
    deadline = time.monotonic() + 3
    while not names(events, "capture.done") and time.monotonic() < deadline:
        time.sleep(0.01)
    (done,) = names(events, "capture.done")
    assert done["capture_id"] == 1 and not done["cancelled"] and done["initial"] == {"27": 0}
    assert [edge[1:] for edge in done["edges"]] == [[27, 1], [27, 0], [27, 1]]
    assert all(edge[0] % 1000 == 0 for edge in done["edges"]) and done["rate_hz_actual"] > 500
    assert run(rig, "capture.start", pins=[27], rate_hz=20_000, duration_ms=10)["err"] == "range"


def test_capture_cancel_and_release(make):
    rig, _, events = make()
    capture_id = run(rig, "capture.start", pins=[27], rate_hz=100, duration_ms=5000)["capture_id"]
    assert run(rig, "capture.cancel", capture_id=99)["err"] == "bad_request"
    assert run(rig, "capture.cancel", capture_id=capture_id)["ok"]
    assert names(events, "capture.done")[0]["cancelled"]
    run(rig, "capture.start", pins=[27], rate_hz=100, duration_ms=5000)
    run(rig, "release_all")
    assert [d["cancelled"] for d in names(events, "capture.done")] == [True, True]


def test_uart_open_rules(make):
    rig, _, _ = make()
    assert run(rig, "uart.open", tx=14, rx=15, baud=9600)["err"] == "unsupported"  # UART not enabled
    rig, _, _ = make(buses=BusConfig(uart="/dev/serial0"))
    assert run(rig, "uart.open", tx=4, rx=5, baud=9600)["err"] == "unsupported"
    assert run(rig, "uart.open", tx=14, rx=15, baud=100)["err"] == "range"
    assert run(rig, "uart.open", tx=14, rx=15, baud=9600, fmt="7E1")["err"] == "unsupported"
    assert run(rig, "uart.open", tx=14, rx=15, baud=9600) == {"ok": True, "port": 0, "tx": 14,
                                                              "rx": 15, "baud": 9600}
    assert run(rig, "uart.open", tx=14, rx=15, baud=9600)["err"] == "pin_mode"
    rig, _, _ = make(buses=BusConfig(uart="/dev/missing"))
    failed = run(rig, "uart.open", tx=14, rx=15, baud=9600)
    assert failed["err"] == "io" and "No such file" in failed["msg"]


def test_uart_echo_read_and_events(make):
    rig, driver, events = make(buses=BusConfig(uart="/dev/serial0"))
    port = run(rig, "uart.open", tx=14, rx=15, baud=115200)["port"]
    assert run(rig, "uart.write", port=port, data=b"BOOT OK\n") == {"ok": True, "n": 8}
    assert names(events, "uart.rx")[-1]["data"] == b"BOOT OK\n"
    assert run(rig, "uart.read", port=port, max=4)["data"] == b"BOOT"
    assert run(rig, "uart.read", port=port)["data"] == b" OK\n"
    assert run(rig, "uart.read", port=port, timeout_ms=50)["data"] == b""
    assert run(rig, "uart.close", port=port)["ok"] and driver.uarts[0].closed
    assert run(rig, "uart.read", port=port)["err"] == "bad_request"


def test_uart_read_waits_for_bytes_without_blocking_the_reader_thread(make):
    rig, driver, _ = make(buses=BusConfig(uart="/dev/serial0"))
    driver.uart_echo = False
    port = run(rig, "uart.open", tx=14, rx=15, baud=115200)["port"]
    uart = driver.uarts[0]
    threading.Timer(0.1, lambda: uart.on_rx(b"late", driver.now_us())).start()
    started = time.monotonic()
    assert run(rig, "uart.read", port=port, timeout_ms=2000)["data"] == b"late"
    assert time.monotonic() - started < 1.5


def test_uart_overflow_drops_oldest(make, monkeypatch):
    monkeypatch.setattr("pi.bridge.hardware_buses.UART_RX_LIMIT", 8)
    rig, _driver, events = make(buses=BusConfig(uart="/dev/serial0"))
    port = run(rig, "uart.open", tx=14, rx=15, baud=115200)["port"]
    run(rig, "uart.write", port=port, data=b"0123456789")
    assert names(events, "rig.warning")[-1]["dropped"] == 2
    assert run(rig, "uart.read", port=port)["data"] == b"23456789"


def test_bytes_from_a_closed_port_are_dropped(make):
    rig, driver, _events = make(buses=BusConfig(uart="/dev/serial0"))
    run(rig, "uart.open", tx=14, rx=15, baud=115200)
    stale = driver.uarts[0]
    run(rig, "uart.close", port=0)
    port = run(rig, "uart.open", tx=14, rx=15, baud=115200)["port"]
    stale.on_rx(b"stale", driver.now_us())
    assert run(rig, "uart.read", port=port)["data"] == b""


def test_spi(make):
    rig, _, _ = make()
    assert run(rig, "spi.xfer", bus=0, cs=0, data=b"\x9f")["err"] == "unsupported"
    rig, driver, _ = make(buses=BusConfig(spi=True))
    assert run(rig, "spi.xfer", bus=0, cs=2, data=b"\x9f")["err"] == "bad_request"
    assert run(rig, "spi.xfer", bus=0, cs=0, data=b"\x9f", mode=4)["err"] == "bad_request"
    assert run(rig, "spi.xfer", bus=0, cs=0, data=b"\x9f", hz=64_000_000)["err"] == "range"
    assert run(rig, "spi.xfer", bus=0, cs=1, data=b"\x9f\x00", mode=3)["data"] == b"\x9f\x00"
    assert driver.calls[-1] == ("spi_xfer", 0, 1, 1_000_000, 3)


def test_i2c(make):
    rig, _, _ = make()
    assert run(rig, "i2c.scan", bus=1)["err"] == "unsupported"
    driver = FakeDriver(i2c_devices={0x68: {0x75: 0x68}})
    rig, _, _ = make(buses=BusConfig(i2c=True), driver=driver)
    assert run(rig, "i2c.scan", bus=1)["addrs"] == [0x68]
    assert run(rig, "i2c.read", bus=1, addr=0x68, n=1, reg=0x75)["data"] == b"\x68"
    assert run(rig, "i2c.write", bus=1, addr=0x68, data=b"\x10\x01\x02") == {"ok": True, "n": 3}
    assert run(rig, "i2c.read", bus=1, addr=0x68, n=2, reg=0x10)["data"] == b"\x01\x02"
    missing = run(rig, "i2c.read", bus=1, addr=0x50, n=1)
    assert missing["err"] == "io" and missing["msg"] == "no device acknowledged at 0x50"
    assert run(rig, "i2c.read", bus=1, addr=0x80, n=1)["err"] == "bad_request"
    assert run(rig, "i2c.read", bus=0, addr=0x68, n=1)["err"] == "unsupported"


def test_release_all_is_complete_and_survives_a_failing_pin(make):
    rig, driver, events = make(buses=BusConfig(uart="/dev/serial0"))
    run(rig, "pin.mode", pin=17, mode="output", initial=1)
    run(rig, "pin.mode", pin=18, mode="pwm")
    run(rig, "pin.pwm", pin=18, freq_hz=50, duty=0.5)
    run(rig, "stream.set", pins=[24], on=True)
    run(rig, "uart.open", tx=14, rx=15, baud=9600)
    driver.fail_release = {17}
    assert run(rig, "release_all")["ok"]
    warning = names(events, "rig.warning")[-1]
    assert warning["warning"] == "release_failed" and warning["pin"] == 17
    assert rig.pins[18].mode == "disabled" and 18 not in driver.pwm and driver.uarts[0].closed
    assert not rig.ports and not rig.streams
    driver.fail_release = set()
    run(rig, "release_all")
    assert all(p.mode == "disabled" for p in rig.pins.values())


def test_watchdog_via_pump_releases(make):
    rig, _, events = make()
    run(rig, "control.take", token=rig.controller.token, watchdog_s=0.05)
    run(rig, "pin.mode", pin=17, mode="output", initial=1)
    time.sleep(0.1)
    rig.pump()
    assert names(events, "control.lost")[-1]["reason"] == "watchdog"
    assert rig.pins[17].mode == "disabled" and rig.controller is None


def test_watchdog_does_not_fire_during_own_long_uart_read_r5(make):
    rig, driver, events = make(buses=BusConfig(uart="/dev/serial0"))
    driver.uart_echo = False
    run(rig, "control.take", token=rig.controller.token, watchdog_s=0.1)
    port = run(rig, "uart.open", tx=14, rx=15, baud=9600)["port"]
    pumping = threading.Event()

    def pump():
        while not pumping.is_set():
            rig.pump()
            time.sleep(0.005)

    pumper = threading.Thread(target=pump)
    pumper.start()
    try:
        assert run(rig, "uart.read", port=port, timeout_ms=400)["ok"]
    finally:
        pumping.set()
        pumper.join()
    assert not names(events, "control.lost") and rig.controller is not None


def test_disconnect_and_close_release(make):
    rig, driver, events = make()
    run(rig, "pin.mode", pin=17, mode="output", initial=1)
    rig.client_disconnected(rig.controller.token)
    assert names(events, "control.lost")[-1]["reason"] == "disconnect"
    assert rig.pins[17].mode == "disabled"
    rig.close()
    assert driver.closed


PARITY_SEQUENCE = [
    ("pin.read", {"pin": 5}),
    ("pin.mode", {"pin": 5, "mode": "output"}),
    ("hello", {"client": "alice", "proto": [1]}),
    ("control.take", {}),
    ("pin.mode", {"pin": 5, "mode": "output", "initial": 1}),
    ("pin.write", {"pin": 5, "level": 0}),
    ("pin.pwm", {"pin": 5, "freq_hz": 100, "duty": 0.5}),
    ("pin.mode", {"pin": 6, "mode": "pwm"}),
    ("pin.pwm", {"pin": 6, "freq_hz": 20000, "duty": 0.5}),
    ("pin.pwm", {"pin": 6, "freq_hz": 100, "duty": 0.5}),
    ("pin.pulse", {"pin": 5, "level": 1, "us": 2_000_000}),
    ("pin.pulse", {"pin": 6, "level": 1, "us": 100}),
    ("pin.write", {"pin": 6, "level": 1}),
    ("pin.mode", {"pin": 12, "mode": "input_pullup"}),
    ("pin.read", {"pin": 12}),
    ("pin.mode", {"pin": 13, "mode": "input_pulldown"}),
    ("pin.read", {"pin": 13}),
    ("capture.start", {"pins": [12], "rate_hz": 20000, "duration_ms": 10}),
    ("capture.cancel", {"capture_id": 99}),
    ("uart.read", {"port": 3}),
    ("uart.close", {"port": 0}),
    ("uart.write", {"port": 0, "data": "AA=="}),
    ("spi.xfer", {"bus": 1, "cs": 0, "data": "AA=="}),
    ("i2c.read", {"bus": 0, "addr": 104, "n": 1}),
    ("i2c.scan", {"bus": 0}),
    ("stream.set", {"pins": [12], "on": True}),
    ("release_all", {}),
    ("pin.write", {"pin": 5, "level": 1}),
    ("control.release", {"token": "not-the-token"}),
    ("nope", {}),
]
COMPARED = ("err", "level", "mode", "pins", "freq_hz", "duty")


def test_error_and_result_parity_with_the_virtual_rig(make):
    virtual = VirtualRig(load_scenario("empty"))
    hardware, _, _ = make(jumpers=(), buses=ALL_BUSES, take=False)
    for op, args in PARITY_SEQUENCE:
        a, b = run(virtual, op, **args), run(hardware, op, **args)
        assert a["ok"] == b["ok"], (op, args, a, b)
        assert {k: a.get(k) for k in COMPARED} == {k: b.get(k) for k in COMPARED}, (op, args, a, b)
