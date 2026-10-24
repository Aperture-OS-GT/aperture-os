import pytest

from aperture.protocol import ProtocolError
from pi.bridge.control import Control

LIMITS = {"watchdog_s": 5, "idle_s": 30}


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += int(seconds * 1e6)


@pytest.fixture
def setup():
    clock, released, events = Clock(), [], []
    control = Control(clock, LIMITS, lambda: released.append(clock.t),
                      lambda name, **payload: events.append((name, payload)))
    return control, clock, released, events


def test_take_and_release(setup):
    control, _, released, events = setup
    result = control.take("alice", None, False, None)
    assert result["controller"] and len(result["token"]) == 16 and result["watchdog_s"] == 5.0
    assert events[-1] == ("control.changed", {"controller": "alice", "previous": None, "reason": "take"})
    with pytest.raises(ProtocolError) as exc:
        control.release("wrong")
    assert exc.value.code == "not_controller"
    control.release(result["token"])
    assert control.controller is None and released
    assert events[-1] == ("control.changed", {"controller": None, "reason": "release"})


def test_retake_with_own_token_is_not_busy(setup):
    control, _, released, _ = setup
    token = control.take("alice", None, False, 2.0)["token"]
    assert control.take("alice", token, False, None)["token"] == token
    assert not released


def test_busy_reports_controller_and_idle(setup):
    control, clock, _, _ = setup
    control.take("alice", None, False, None)
    clock.advance(3)
    with pytest.raises(ProtocolError) as exc:
        control.take("bob", None, True, None)
    assert exc.value.code == "busy" and exc.value.extra == {"controller": "alice", "idle_s": 3.0}


def test_force_after_idle_releases_and_names_previous(setup):
    control, clock, released, events = setup
    control.take("alice", None, False, 600)
    clock.advance(31)
    result = control.take("bob", None, True, None)
    assert result["controller"] and control.client == "bob" and released
    assert events[-1][1] == {"controller": "bob", "previous": "alice", "reason": "force"}


def test_keepalive_does_not_block_force_r6(setup):
    control, clock, _, _ = setup
    control.take("alice", None, False, 600)
    for _ in range(20):            # 40 s of keepalive pings, no driving
        clock.advance(2)
        control.begin(driving=False)
        control.end()
    assert control.take("bob", None, True, None)["controller"]


def test_driving_resets_idle(setup):
    control, clock, _, _ = setup
    control.take("alice", None, False, 600)
    clock.advance(29)
    control.begin(driving=True)
    control.end()
    clock.advance(2)
    with pytest.raises(ProtocolError):
        control.take("bob", None, True, None)


def test_watchdog_fires_on_silence(setup):
    control, clock, released, events = setup
    control.take("alice", None, False, 1.0)
    clock.advance(0.9)
    assert not control.check_watchdog()
    clock.advance(0.2)
    assert control.check_watchdog() and control.controller is None and released
    assert events[-1] == ("control.lost", {"reason": "watchdog"})
    assert not control.check_watchdog()


def test_watchdog_suspended_while_controller_request_runs_r5(setup):
    control, clock, released, _ = setup
    control.take("alice", None, False, 1.0)
    control.begin(driving=False)       # e.g. a 3 s uart.read
    clock.advance(3)
    assert not control.check_watchdog()
    control.end()                      # response sent: the watchdog restarts from now
    clock.advance(0.9)
    assert not control.check_watchdog() and not released
    clock.advance(0.2)
    assert control.check_watchdog()


def test_disconnect_only_for_the_controllers_token(setup):
    control, _, released, events = setup
    token = control.take("alice", None, False, None)["token"]
    control.disconnected("someone-else")
    assert control.controller is not None and not released
    control.disconnected(token)
    assert control.controller is None and events[-1] == ("control.lost", {"reason": "disconnect"})
    control.disconnected(None)  # no controller: nothing to do
    assert len(released) == 1


def test_require_and_end_without_controller(setup):
    control, _, _, _ = setup
    with pytest.raises(ProtocolError) as exc:
        control.require()
    assert exc.value.code == "not_controller"
    control.begin(driving=True)
    control.end()  # tolerated when nobody holds control
