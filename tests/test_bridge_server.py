import itertools
import threading
import time

import pytest

from aperture.rig.virtual import VirtualRig, load_scenario
from pi.bridge.server import BridgeServer, Outbox

IDS = itertools.count(1)


def drain(conn):
    messages = []
    while (message := conn.outbox.get(timeout=0)) is not None:
        messages.append(message)
    return messages


def call(bridge, conn, op, **args):
    """Send one request; return (reply, every message drained from the connection)."""
    request_id = next(IDS)
    bridge.handle(conn, {"v": 1, "id": request_id, "op": op, **args})
    messages = drain(conn)
    reply = next(m for m in messages if m.get("id") == request_id and "ok" in m)
    return reply, messages


@pytest.fixture
def rig():
    return VirtualRig(load_scenario("loopback"))


@pytest.fixture
def bridge(rig):
    server = BridgeServer(rig)
    yield server
    server.shutdown()


def hello(bridge, name):
    conn = bridge.open(f"test:{name}")
    reply, _ = call(bridge, conn, "hello", client=name, proto=[1])
    assert reply["ok"]
    return conn


def test_ack_precedes_reply(bridge):
    conn = bridge.open("test")
    bridge.handle(conn, {"v": 1, "id": 7, "op": "ping"})
    ack, reply = drain(conn)
    assert ack == {"v": 1, "id": 7, "ack": True, "t": ack["t"]}
    assert reply["id"] == 7 and reply["ok"] and reply["t"] >= ack["t"]


@pytest.mark.parametrize("bad_id", ["7", None, -1, 2**32, 1.5, True])
def test_request_without_u32_id_is_rejected_without_ack(bridge, bad_id):
    conn = bridge.open("test")
    bridge.handle(conn, {"v": 1, "id": bad_id, "op": "ping"})
    (reply,) = drain(conn)
    assert reply["id"] is None and reply["err"] == "bad_request"


def test_unknown_op_is_acked_then_rejected(bridge):
    conn = bridge.open("test")
    reply, messages = call(bridge, conn, "nope")
    assert messages[0]["ack"] and reply["err"] == "unknown_op"


def test_controller_is_named_after_its_own_hello(bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    reply, messages = call(bridge, alice, "control.take")
    changed = next(m for m in messages if m.get("ev") == "control.changed")
    assert reply["ok"] and changed["controller"] == "alice"
    assert any(m.get("ev") == "control.changed" for m in drain(bob))
    assert bridge.controller is alice and alice.token == reply["token"]


def test_observer_cannot_drive_or_use_buses(bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    call(bridge, alice, "control.take")
    reply, _ = call(bridge, bob, "pin.mode", pin=17, mode="output")
    assert reply["err"] == "not_controller" and reply["controller"] == "alice"
    for op, args in (("uart.read", {"port": 0}), ("i2c.read", {"bus": 1, "addr": 0x68, "n": 1}),
                     ("i2c.scan", {"bus": 1}), ("release_all", {})):
        assert call(bridge, bob, op, **args)[0]["err"] == "not_controller", op
    assert call(bridge, bob, "pin.read", pin=17)[0]["err"] == "pin_mode"  # reached the rig
    assert call(bridge, bob, "stream.set", pins=[17], on=True)[0]["ok"]
    assert call(bridge, bob, "list_pins")[0]["ok"]


def test_observer_take_is_busy(bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    call(bridge, alice, "control.take")
    reply, _ = call(bridge, bob, "control.take")
    assert reply["err"] == "busy" and reply["controller"] == "alice"
    assert bridge.controller is alice


def test_observer_ping_does_not_refresh_controller_watchdog(rig, bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    call(bridge, alice, "control.take", watchdog_s=1.0)
    rig.advance(600_000)
    assert call(bridge, bob, "ping")[0]["ok"]
    rig.advance(600_000)
    lost = [m for m in drain(bob) if m.get("ev") == "control.lost"]
    assert lost and lost[0]["reason"] == "watchdog" and bridge.controller is None


def test_controller_ping_refreshes_watchdog(rig, bridge):
    alice = hello(bridge, "alice")
    call(bridge, alice, "control.take", watchdog_s=1.0)
    for _ in range(3):
        rig.advance(600_000)
        assert call(bridge, alice, "ping")[0]["ok"]
    assert bridge.controller is alice
    assert call(bridge, alice, "pin.mode", pin=17, mode="output")[0]["ok"]


def test_controller_disconnect_releases_and_notifies_observers(rig, bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    call(bridge, alice, "control.take")
    call(bridge, alice, "pin.mode", pin=17, mode="output", initial=1)
    drain(bob)
    bridge.close(alice)
    lost = [m for m in drain(bob) if m.get("ev") == "control.lost"]
    assert lost and lost[0]["reason"] == "disconnect"
    assert rig.pins[17].mode == "disabled" and bridge.controller is None
    assert alice.outbox.closed and alice.token is None


def test_observer_disconnect_keeps_control(bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    call(bridge, alice, "control.take")
    bridge.close(bob)
    assert bridge.controller is alice
    assert not [m for m in drain(alice) if m.get("ev") == "control.lost"]


def test_force_take_moves_control_and_locks_out_previous(rig, bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    call(bridge, alice, "control.take", watchdog_s=600.0)
    call(bridge, alice, "pin.mode", pin=17, mode="output", initial=1)
    rig.advance(31_000_000)
    drain(bob)  # discard the broadcast from alice's original take
    reply, messages = call(bridge, bob, "control.take", force=True)
    changed = next(m for m in messages if m.get("ev") == "control.changed")
    assert reply["ok"] and changed["previous"] == "alice" and changed["reason"] == "force"
    assert rig.pins[17].mode == "disabled" and alice.token is None
    denied, _ = call(bridge, alice, "pin.write", pin=17, level=1)
    assert denied["err"] == "not_controller" and denied["controller"] == "bob"


def test_release_clears_server_controller(bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    token = call(bridge, alice, "control.take")[0]["token"]
    assert call(bridge, alice, "control.release", token=token)[0]["ok"]
    assert bridge.controller is None and alice.token is None
    assert call(bridge, bob, "control.take")[0]["ok"]


def test_events_reach_every_connection(bridge):
    alice, bob = hello(bridge, "alice"), hello(bridge, "bob")
    call(bridge, alice, "control.take")
    call(bridge, bob, "stream.set", pins=[17], on=True)
    call(bridge, alice, "pin.mode", pin=17, mode="output", initial=0)
    drain(bob)
    call(bridge, alice, "pin.write", pin=17, level=1)
    changes = [m for m in drain(bob) if m.get("ev") == "pin.change"]
    assert changes and changes[-1]["pin"] == 17 and changes[-1]["level"] == 1


class SpyRig(VirtualRig):
    closed = False

    def close(self):
        self.closed = True


def test_shutdown_releases_outputs_and_closes_executor():
    rig = SpyRig(load_scenario("loopback"))
    bridge = BridgeServer(rig)
    alice = hello(bridge, "alice")
    call(bridge, alice, "control.take")
    call(bridge, alice, "pin.mode", pin=17, mode="output", initial=1)
    bridge.shutdown()
    assert rig.pins[17].mode == "disabled" and rig.controller is None and rig.closed


def test_pump_thread_runs_behaviors_without_requests():
    rig = VirtualRig(load_scenario("button_led"), realtime=True)
    bridge = BridgeServer(rig, pump_interval_s=0.001)
    try:
        conn = bridge.open("observer")
        call(bridge, conn, "stream.set", pins=[24], on=True)
        deadline = time.monotonic() + 3
        changes = []
        while len(changes) < 2 and time.monotonic() < deadline:
            message = conn.outbox.get(timeout=0.1)
            if message and message.get("ev") == "pin.change":
                changes.append(message)
        assert len(changes) >= 2 and {c["pin"] for c in changes} == {24}
    finally:
        bridge.shutdown()


def test_outbox_drops_oldest_events_never_replies():
    box = Outbox(lambda: 99, limit=3)
    box.put({"id": 1, "ok": True})
    for n in range(5):
        box.put({"ev": "pin.change", "n": n})
    box.put({"id": 2, "ok": True})  # over the limit again: the oldest *event* goes, not a reply
    got = [box.get(timeout=0) for _ in range(4)]
    assert got == [{"id": 1, "ok": True}, {"ev": "pin.change", "n": 4}, {"id": 2, "ok": True},
                   {"v": 1, "ev": "stream.overflow", "t": 99, "dropped": 4}]
    assert box.get(timeout=0) is None


def test_outbox_close_drains_queued_then_stops():
    box = Outbox(lambda: 0)
    assert box.get(timeout=0.01) is None and not box.closed
    box.put({"id": 1})
    box.close()
    box.put({"id": 2})
    assert box.get() == {"id": 1}
    assert box.get() is None


def test_outbox_get_blocks_until_put():
    box = Outbox(lambda: 0)
    got = []
    reader = threading.Thread(target=lambda: got.append(box.get()))
    reader.start()
    time.sleep(0.05)
    box.put({"id": 3})
    reader.join(2)
    assert got == [{"id": 3}]
