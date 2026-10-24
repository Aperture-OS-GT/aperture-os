"""LgpioDriver against a fake lgpio module: the calls python3-lgpio on the Pi needs."""
import errno
import itertools

import pytest

from aperture.protocol import ProtocolError
from pi.bridge.drivers.lgpio_driver import LgpioDriver
from pi.bridge.hardware import HardwareExecutor

HANDLE = 7
IDS = itertools.count(1)


class FakeCallback:
    def __init__(self, func):
        self.func, self.cancelled = func, False

    def cancel(self):
        self.cancelled = True


class FakeLgpio:
    """The slice of lgpio the driver uses. Calls are logged without the chip handle; a name in
    ``fail`` raises ``error`` like lgpio does, and so does ``tx_pwm`` at frequency 0, as the
    Pi's lgpio rejects ``tx_pwm(gpio, 0, 0)`` with "bad PWM micros"."""

    SET_PULL_UP, SET_PULL_DOWN, SET_PULL_NONE = 32, 64, 128
    BOTH_EDGES = 3

    class error(Exception):
        pass

    def __init__(self):
        self.calls, self.callbacks, self.levels, self.fail = [], {}, {}, {}

    def _log(self, name, handle, *args):
        assert handle == HANDLE
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.error(self.fail[name])

    def gpiochip_open(self, chip):
        self.calls.append(("gpiochip_open", chip))
        if "gpiochip_open" in self.fail:
            raise self.error(self.fail["gpiochip_open"])
        return HANDLE

    def gpiochip_close(self, handle):
        self._log("gpiochip_close", handle)

    def gpio_claim_output(self, handle, pin, level, flags):
        self._log("gpio_claim_output", handle, pin, level, flags)
        self.levels[pin] = level

    def gpio_claim_input(self, handle, pin, flags):
        self._log("gpio_claim_input", handle, pin, flags)

    def gpio_claim_alert(self, handle, pin, edges, flags):
        self._log("gpio_claim_alert", handle, pin, edges, flags)

    def gpio_free(self, handle, pin):
        self._log("gpio_free", handle, pin)

    def gpio_read(self, handle, pin):
        self._log("gpio_read", handle, pin)
        return self.levels.get(pin, 0)

    def gpio_write(self, handle, pin, level):
        self._log("gpio_write", handle, pin, level)
        self.levels[pin] = level

    def tx_pwm(self, handle, pin, freq, duty):
        self._log("tx_pwm", handle, pin, freq, duty)
        if freq == 0:
            raise self.error("bad PWM micros")

    def callback(self, handle, pin, edges, func):
        self._log("callback", handle, pin, edges)
        self.callbacks[pin] = FakeCallback(func)
        return self.callbacks[pin]


@pytest.fixture
def lg():
    return FakeLgpio()


def only(lg, *names):
    return [call for call in lg.calls if call[0] in names]


def test_modes_claim_lines_with_the_matching_pull(lg):
    driver = LgpioDriver(3, lgpio_module=lg)
    for mode in ("output", "input_pullup", "input_pulldown", "input", "disabled"):
        driver.set_mode(17, mode, 1)
    alert = ("gpio_claim_alert", 17, lg.BOTH_EDGES)
    assert lg.calls == [("gpiochip_open", 3),
                        ("gpio_claim_output", 17, 1, 0),
                        ("gpio_free", 17), (*alert, lg.SET_PULL_UP),
                        ("gpio_free", 17), (*alert, lg.SET_PULL_DOWN),
                        ("gpio_free", 17), (*alert, lg.SET_PULL_NONE),
                        ("gpio_free", 17), ("gpio_claim_input", 17, lg.SET_PULL_NONE)]


def test_pwm_stops_with_duty_zero_and_only_when_running(lg):
    driver = LgpioDriver(0, lgpio_module=lg)
    driver.set_mode(18, "pwm", 0)
    driver.stop_pwm(18, 1)            # nothing running: just hold the level
    driver.start_pwm(18, 100, 0.25)
    driver.stop_pwm(18, 0)
    driver.start_pwm(18, 50, 0.5)
    driver.set_mode(18, "disabled")   # a mode change stops a running PWM first
    assert only(lg, "tx_pwm", "gpio_write", "gpio_free") == [
        ("gpio_write", 18, 1),
        ("tx_pwm", 18, 100, 25.0), ("tx_pwm", 18, 100, 0), ("gpio_write", 18, 0),
        ("tx_pwm", 18, 50, 50.0), ("tx_pwm", 18, 50, 0), ("gpio_free", 18)]


def test_watch_drops_watchdog_ticks_stamps_with_the_bridge_clock_and_cancels(lg):
    driver = LgpioDriver(0, lgpio_module=lg)
    driver.set_mode(23, "input")
    edges = []
    driver.watch(23, lambda pin, level, t_us: edges.append((pin, level, t_us)))
    assert ("callback", 23, lg.BOTH_EDGES) in lg.calls
    first = lg.callbacks[23]
    for level in (1, 2, 0):  # 2 is lgpio's watchdog tick, not an edge
        first.func(0, 23, level, 10**15)
    assert [edge[:2] for edge in edges] == [(23, 1), (23, 0)]
    assert all(t_us <= driver.now_us() for _, _, t_us in edges)  # not lgpio's timestamp
    driver.watch(23, lambda *edge: None)
    assert first.cancelled and not lg.callbacks[23].cancelled
    driver.watch(23, None)
    assert lg.callbacks[23].cancelled


def test_pulse_restores_the_previous_level(lg):
    driver = LgpioDriver(0, lgpio_module=lg)
    driver.set_mode(17, "output", 0)
    start, end = driver.pulse(17, 1, 300)
    assert end - start >= 300
    assert only(lg, "gpio_write") == [("gpio_write", 17, 1), ("gpio_write", 17, 0)]


def test_lgpio_errors_become_oserror_and_err_io(lg):
    lg.fail["gpiochip_open"] = "can not open gpiochip"
    with pytest.raises(OSError, match="lgpio: can not open gpiochip"):
        LgpioDriver(0, lgpio_module=lg)
    del lg.fail["gpiochip_open"]
    driver = LgpioDriver(0, lgpio_module=lg)
    lg.fail["gpio_claim_output"] = "GPIO busy"
    with pytest.raises(OSError) as raised:
        driver.set_mode(17, "output", 1)
    assert raised.value.errno == errno.EIO and raised.value.strerror == "lgpio: GPIO busy"
    rig = HardwareExecutor(driver, "rig-test")
    for op, args in (("hello", {"client": "alice", "proto": [1]}), ("control.take", {})):
        rig.execute({"v": 1, "id": next(IDS), "op": op, **args})
    with pytest.raises(ProtocolError) as failed:
        rig.execute({"v": 1, "id": next(IDS), "op": "pin.mode", "pin": 17, "mode": "output"})
    assert (failed.value.code, failed.value.msg) == ("io", "pin.mode: lgpio: GPIO busy")


def test_close_frees_every_claimed_line_then_the_chip(lg):
    driver = LgpioDriver(0, lgpio_module=lg)
    driver.set_mode(18, "pwm", 0)
    driver.start_pwm(18, 100, 0.5)
    driver.set_mode(23, "input")
    driver.watch(23, lambda *edge: None)
    lg.calls.clear()
    driver.close()
    assert lg.callbacks[23].cancelled and ("tx_pwm", 18, 100, 0) in lg.calls
    assert sorted(only(lg, "gpio_free")) == [("gpio_free", 18), ("gpio_free", 23)]
    assert lg.calls[-1] == ("gpiochip_close",)
