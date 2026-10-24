"""Shared machinery for transports that talk to a real bridge over a byte link.

Subclasses implement ``_open`` / ``_close`` / ``_write(message)`` and feed every
decoded incoming message to ``_deliver``. Requests are matched to responses by
id; acks fire the lifecycle callback; events go to ``_event``. While this host
holds control a keepalive ping runs so the bridge watchdog only fires when the
host is really gone.
"""
from __future__ import annotations

import contextlib
import threading
from abc import abstractmethod

from ..protocol import ProtocolError
from .base import Response, Transport, TransportError


class _Pending:
    __slots__ = ("done", "reply")

    def __init__(self):
        self.done = threading.Event()
        self.reply: dict | None = None


class MessageTransport(Transport):
    keepalive = True

    def __init__(self, client: str = "aperture-host"):
        super().__init__(client)
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._keepalive_gen = 0
        self.link_error: str | None = None

    # --- Transport hooks ------------------------------------------------------------
    def _connect(self) -> None:
        self.link_error = None
        self._open()

    def _disconnect(self) -> None:
        self._keepalive_gen += 1
        try:
            self._close()
        finally:
            self._fail_pending("disconnected")

    def _send(self, message: dict, timeout: float) -> Response:
        pending = _Pending()
        with self._pending_lock:
            self._pending[message["id"]] = pending
        try:
            try:
                self._write(message)
            except (OSError, ProtocolError) as exc:
                raise TransportError(f"send failed: {exc}") from exc
            if not pending.done.wait(timeout):
                raise TransportError(f"timeout waiting for {message['op']} response ({timeout:.1f}s)")
        finally:
            with self._pending_lock:
                self._pending.pop(message["id"], None)
        if pending.reply is None:
            raise TransportError(f"link lost: {self.link_error or 'disconnected'}")
        return Response.from_message(pending.reply)

    def request(self, op: str, **args) -> Response:
        response = super().request(op, **args)
        if op == "control.take" and response.ok and self.keepalive:
            self._start_keepalive(float(response.result.get("watchdog_s") or 3.0))
        return response

    # --- incoming -------------------------------------------------------------------
    def _deliver(self, message) -> None:
        if not isinstance(message, dict):
            return
        if "ev" in message:
            if self.connected:
                self._event(message)
            return
        request_id = message.get("id")
        if message.get("ack"):
            self._acked(request_id, message.get("t", 0))
            return
        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is not None:
            pending.reply = message
            pending.done.set()

    def _link_lost(self, reason: str) -> None:
        """Called by subclasses when the link drops underneath us."""
        self.link_error = reason
        was_connected, was_controller = self.connected, bool(self.control_token)
        self.connected = False
        self._keepalive_gen += 1
        self._fail_pending(reason)
        if was_connected and was_controller:
            # the bridge fails safe on its side; mirror that in the session log
            self._event({"v": 1, "ev": "control.lost", "t": 0, "reason": "link", "msg": reason})

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
        for item in pending:
            item.done.set()

    # --- keepalive ------------------------------------------------------------------
    def _start_keepalive(self, watchdog_s: float) -> None:
        self._keepalive_gen += 1
        interval = max(0.2, watchdog_s / 3)
        threading.Thread(target=self._keepalive_loop, args=(self._keepalive_gen, interval),
                         name=f"{self.kind}-keepalive", daemon=True).start()

    def _keepalive_loop(self, gen: int, interval: float) -> None:
        stop = threading.Event()
        while not stop.wait(interval):
            if gen != self._keepalive_gen or not (self.connected and self.control_token):
                return
            # skip if a real request is in flight; it refreshes the watchdog anyway
            if not self._lock.acquire(timeout=interval / 2):
                continue
            try:
                with contextlib.suppress(TransportError, ProtocolError, OSError):
                    if self.connected:
                        self._send({"v": 1, "id": self._take_id(), "op": "ping"}, self.timeouts["response"])
            finally:
                self._lock.release()

    def _take_id(self) -> int:
        request_id = self._next_id
        self._next_id = (self._next_id + 1) % 2**32
        return request_id

    # --- to implement -------------------------------------------------------------
    @abstractmethod
    def _open(self) -> None: ...

    @abstractmethod
    def _close(self) -> None: ...

    @abstractmethod
    def _write(self, message: dict) -> None: ...
