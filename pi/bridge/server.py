"""Transport-agnostic bridge core: acks, per-connection control and event fan-out.

The executor (``VirtualRig`` in sim mode, the hardware executor on a Pi) owns op
semantics, the take-control handshake and the watchdog. This layer adds what only a
multi-client daemon knows: which connection sent a request, whether that connection
holds control, and whose outputs to release when a link drops. A bug in one op is logged
and answered with ``err:io``; it never takes the connection down.
"""
from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol

from aperture.protocol import (
    DRIVING_OPS,
    ProtocolError,
    ack,
    error,
    event,
    response,
    validate_request,
)

# Reads that consume shared state (UART rx buffer) or put traffic on a bus: an observer must
# not interleave them with the controller's test.
CONTROLLER_ONLY_READS = frozenset({"uart.read", "i2c.read", "i2c.scan"})
OUTBOX_LIMIT = 1000
PUMP_INTERVAL_S = 0.001
log = logging.getLogger(__name__)


class Executor(Protocol):
    def execute(self, message: dict) -> dict: ...
    def now_us(self) -> int: ...
    def on_event(self, callback: Callable[[dict], None]) -> None: ...
    def client_disconnected(self, token: str | None) -> None: ...


class Outbox:
    """Per-connection send queue. Replies are never dropped; past ``limit`` the oldest
    event is dropped and a ``stream.overflow`` event reports the count once drained."""

    def __init__(self, clock: Callable[[], int], limit: int = OUTBOX_LIMIT):
        self._clock, self.limit = clock, limit
        self._items: deque[dict] = deque()
        self._cond = threading.Condition()
        self._dropped = 0
        self.closed = False

    def put(self, message: dict) -> None:
        with self._cond:
            if self.closed:
                return
            self._items.append(message)
            if len(self._items) > self.limit:
                self._drop_oldest_event()
            self._cond.notify()

    def _drop_oldest_event(self) -> None:
        for index, item in enumerate(self._items):
            if "ev" in item:
                del self._items[index]
                self._dropped += 1
                return

    def get(self, timeout: float | None = None) -> dict | None:
        """Next message; None once closed and drained, or when ``timeout`` elapses empty."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._items or self.closed, timeout):
                return None
            if not self._items:
                return None
            message = self._items.popleft()
            if self._dropped and not self._items and not self.closed:
                self._items.append(event("stream.overflow", self._clock(), dropped=self._dropped))
                self._dropped = 0
            return message

    def close(self) -> None:
        """Accept nothing more; already-queued messages (e.g. a final error) still drain."""
        with self._cond:
            self.closed = True
            self._cond.notify_all()


class Connection:
    def __init__(self, cid: int, label: str, outbox: Outbox):
        self.cid, self.label, self.outbox = cid, label, outbox
        self.client: str | None = None
        self.token: str | None = None

    def send(self, message: dict) -> None:
        self.outbox.put(message)

    @property
    def name(self) -> str:
        return self.client or self.label


class BridgeServer:
    def __init__(self, executor: Executor, pump_interval_s: float | None = None):
        self.executor = executor
        self._connections: dict[int, Connection] = {}
        self._controller: Connection | None = None
        self._lock = threading.Lock()        # connection table and controller
        self._exec_lock = threading.Lock()   # one op at a time across all connections
        self._ids = itertools.count(1)
        self._stop = threading.Event()
        self._pump: threading.Thread | None = None
        executor.on_event(self._broadcast)
        if pump_interval_s and hasattr(executor, "pump"):
            self._pump = threading.Thread(target=self._pump_loop, args=(pump_interval_s,),
                                          name="bridge-pump", daemon=True)
            self._pump.start()

    # --- connections -----------------------------------------------------------------
    def open(self, label: str) -> Connection:
        conn = Connection(next(self._ids), label, Outbox(self.executor.now_us))
        with self._lock:
            self._connections[conn.cid] = conn
        return conn

    def close(self, conn: Connection) -> None:
        """Forget ``conn``; if it held control the executor releases every output."""
        with self._lock:
            self._connections.pop(conn.cid, None)
            was_controller = self._controller is conn
            if was_controller:
                self._controller = None
        if was_controller:
            with self._exec_lock:
                self.executor.client_disconnected(conn.token)
        conn.token = None
        conn.outbox.close()

    def connections(self) -> list[Connection]:
        with self._lock:
            return list(self._connections.values())

    @property
    def controller(self) -> Connection | None:
        return self._controller

    def shutdown(self) -> None:
        """Daemon exit: stop pumping and release whatever a controller left driving."""
        self._stop.set()
        if self._pump:
            self._pump.join(timeout=1)
        for conn in self.connections():
            self.close(conn)
        with self._exec_lock:
            self.executor.client_disconnected(None)
            close = getattr(self.executor, "close", None)
            if close:
                close()

    # --- requests --------------------------------------------------------------------
    def handle(self, conn: Connection, message: dict) -> None:
        """Ack, gate, execute and reply to one decoded request map from ``conn``."""
        now = self.executor.now_us
        request_id = message.get("id")
        if type(request_id) is not int or not 0 <= request_id < 2**32:
            conn.send(error(None, now(), "bad_request", "id must be a u32"))
            return
        conn.send(ack(request_id, now()))
        try:
            reply = self._dispatch(conn, validate_request(message), message)
        except ProtocolError as exc:
            reply = error(request_id, now(), exc.code, exc.msg, **exc.extra)
        except Exception as exc:  # a bridge bug: answer the request and keep the link
            log.exception("%s from %s failed unexpectedly", message.get("op"), conn.name)
            reply = error(request_id, now(), "io", f"internal bridge error: {type(exc).__name__}")
        conn.send(reply)

    def _dispatch(self, conn: Connection, request: dict, message: dict) -> dict:
        op = request["op"]
        if op == "hello":
            conn.client = request["client"]
        controller = self._controller
        if (op in DRIVING_OPS or op in CONTROLLER_ONLY_READS) and controller not in (None, conn):
            raise ProtocolError("not_controller", f"rig is controlled by {controller.name}",
                                controller=controller.name)
        if op == "ping" and controller is not conn:
            # Only the controller's keepalive may refresh the watchdog.
            return response(request["id"], self.executor.now_us())
        with self._exec_lock:
            if op == "control.take":
                # The executor names the controller after the last hello it saw.
                self.executor.execute({"v": 1, "id": 0, "op": "hello", "client": conn.name,
                                       "proto": [1]})
            result = self.executor.execute(message)
            if op == "control.take":
                self._set_controller(conn, result["token"])
            elif op == "control.release":
                self._set_controller(None, None)
            return response(request["id"], self.executor.now_us(), **result)

    def _set_controller(self, conn: Connection | None, token: str | None) -> None:
        with self._lock:
            if self._controller is not None and self._controller is not conn:
                self._controller.token = None
            self._controller = conn
            if conn is not None:
                conn.token = token

    # --- events ----------------------------------------------------------------------
    def _broadcast(self, message: dict) -> None:
        if message.get("ev") == "control.lost":
            self._set_controller(None, None)
        for conn in self.connections():
            conn.send(message)

    def _pump_loop(self, interval_s: float) -> None:
        while not self._stop.wait(interval_s):
            self.executor.pump()
