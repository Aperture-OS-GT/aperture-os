"""An idle session keeps its control lease: the bridge watchdog must not fire while the host is alive,
but must still fire once the host stops pinging."""
import time

from aperture.session import Session
from aperture.transport import VirtualTransport


def _session(home) -> Session:
    session = Session(home=home, persist=False)
    session.connect(VirtualTransport(scenario="empty", realtime=True), take_control=False)
    return session


def test_idle_session_keeps_control_past_the_watchdog(tmp_path):
    session = _session(tmp_path)
    assert session.request("control.take", watchdog_s=0.6).ok

    time.sleep(1.5)  # idle well past the watchdog
    request = session.request("pin.mode", pin=17, mode="output", initial=0)

    assert request.ok, request.err
    assert not [e for e in session.events if e["kind"] == "control.lost"]
    session.disconnect()


def test_keepalive_pings_are_not_logged(tmp_path):
    session = _session(tmp_path)
    assert session.request("control.take", watchdog_s=0.75).ok
    before = len(session.events)

    time.sleep(0.9)  # several keepalive pings at 0.25 s

    assert not [e for e in session.events[before:] if e["kind"] == "request.stage"]
    session.disconnect()


def test_watchdog_still_fires_once_the_host_stops_pinging(tmp_path):
    session = _session(tmp_path)
    assert session.request("control.take", watchdog_s=0.6).ok

    session._keepalive_gen += 1  # the host "dies": no more pings, token still held
    time.sleep(1.0)
    request = session.request("pin.mode", pin=17, mode="output", initial=0)

    assert not request.ok and request.err == "not_controller"
    assert [e for e in session.events if e["kind"] == "control.lost" and e.get("reason") == "watchdog"]
    session.disconnect()
