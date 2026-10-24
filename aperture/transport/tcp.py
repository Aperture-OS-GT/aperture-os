"""Protocol v1 over TCP (JSON lines) to a ``pi.bridge`` daemon (``aperture-bridge``).

A reader thread routes acks and responses to the pending request; events go to a
separate dispatcher thread so an event callback may itself issue requests. While this
client holds control a keepalive pings at half the watchdog period, outside the
lifecycle callbacks, so an idle tester does not trip the bridge's fail-safe.
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field

from ..protocol import MAX_JSON_LINE, ProtocolError, decode_json, encode_json
from .base import Response, Transport, TransportError

DEFAULT_PORT = 8765
CONNECT_TIMEOUT_S = 5.0
KEEPALIVE_TIMEOUT_S = 2.0
KEEPALIVE_TICK_S = 0.05
DEFAULT_WATCHDOG_S = 5.0
JOIN_TIMEOUT_S = 1.0

log = logging.getLogger(__name__)


@dataclass
class _Pending:
    acked: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    ack_t: int | None = None
    response: Response | None = None


class _Link:
    """State of one TCP connection; a reconnect builds a fresh one."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.reader = sock.makefile("rb")
        self.closing = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.pending: dict[int, _Pending] = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def register(self, request_id: int) -> _Pending:
        with self.pending_lock:
            pending = self.pending[request_id] = _Pending()
        return pending

    def forget(self, request_id: int) -> None:
        with self.pending_lock:
            self.pending.pop(request_id, None)

    def write(self, data: bytes) -> None:
        with self.write_lock:
            self.sock.sendall(data)

    def fail_all(self) -> None:
        with self.pending_lock:
            pending, self.pending = list(self.pending.values()), {}
        for item in pending:
            item.acked.set()
            item.done.set()


class TcpTransport(Transport):
    kind = "tcp"

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 client: str = "aperture-host", keepalive: bool = True):
        super().__init__(client)
        self.host, self.port, self.keepalive = host, int(port), keepalive
        self.watchdog_s: float | None = None
        self._link: _Link | None = None

    def describe(self) -> dict:
        return {**super().describe(), "host": self.host, "port": self.port}

    # --- link ------------------------------------------------------------------------
    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
        except OSError as exc:
            raise TransportError(f"cannot reach bridge at {self.host}:{self.port}: {exc}") from exc
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        link = self._link = _Link(sock)
        for target in (self._read_loop, self._dispatch_loop, self._keepalive_loop):
            thread = threading.Thread(target=target, args=(link,), daemon=True,
                                      name=f"tcp-{target.__name__.strip('_')}")
            link.threads.append(thread)
            thread.start()

    def _disconnect(self) -> None:
        link, self._link = self._link, None
        if link is None:
            return
        link.closing.set()
        self._close_socket(link)
        link.fail_all()
        link.events.put(None)
        for thread in link.threads:
            if thread is not threading.current_thread():
                thread.join(JOIN_TIMEOUT_S)

    @staticmethod
    def _close_socket(link: _Link) -> None:
        try:
            link.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        link.sock.close()

    # --- requests --------------------------------------------------------------------
    def _send(self, message: dict, timeout: float) -> Response:
        response = self._round_trip(message, timeout, notify_ack=True)
        if message.get("op") == "control.take" and response.ok:
            self.watchdog_s = response.result.get("watchdog_s")
        return response

    def _round_trip(self, message: dict, timeout: float, notify_ack: bool) -> Response:
        link = self._link
        if link is None or link.closing.is_set():
            raise TransportError("not connected")
        request_id = message["id"]
        pending = link.register(request_id)
        try:
            link.write(encode_json(message))
        except ProtocolError as exc:
            link.forget(request_id)
            return Response(request_id, False, 0, {}, exc.code, exc.msg)
        except OSError as exc:
            link.forget(request_id)
            raise TransportError(f"link lost: {exc}") from exc
        ack_timeout = self.timeouts["ack"]
        if not pending.acked.wait(ack_timeout):
            link.forget(request_id)
            return Response(request_id, False, 0, {}, "timeout", f"no ack within {ack_timeout} s")
        if notify_ack and pending.ack_t is not None:
            self._acked(request_id, pending.ack_t)
        if not pending.done.wait(timeout):
            link.forget(request_id)
            return Response(request_id, False, 0, {}, "timeout", f"no response within {timeout} s")
        if pending.response is None:
            raise TransportError("link lost before the bridge responded")
        return pending.response

    # --- threads ---------------------------------------------------------------------
    def _read_loop(self, link: _Link) -> None:
        try:
            while True:
                line = link.reader.readline(MAX_JSON_LINE + 1)
                if not line or len(line) > MAX_JSON_LINE:
                    return
                try:
                    self._route(link, decode_json(line))
                except ProtocolError:
                    log.warning("ignoring malformed line from bridge %s:%s", self.host, self.port)
        except (OSError, ValueError):
            return
        finally:
            self._link_lost(link)

    def _route(self, link: _Link, message: dict) -> None:
        if "ev" in message:
            link.events.put(message)
            return
        request_id = message.get("id")
        if message.get("ack") is True:
            with link.pending_lock:
                pending = link.pending.get(request_id)
            if pending is not None:
                pending.ack_t = message.get("t")
                pending.acked.set()
            return
        with link.pending_lock:
            pending = link.pending.pop(request_id, None)
        if pending is not None:
            pending.response = Response.from_message(message)
            pending.done.set()
            pending.acked.set()  # a reply without a preceding ack still completes the request

    def _link_lost(self, link: _Link) -> None:
        if link.closing.is_set():
            return
        link.closing.set()
        self._close_socket(link)
        link.fail_all()
        held_control = bool(self.control_token)
        self.connected = False
        t_us = self.host_now_us() + (self.clock_offset_us or 0)
        if held_control:
            link.events.put({"v": 1, "ev": "control.lost", "t": t_us, "reason": "link_lost"})
        link.events.put({"v": 1, "ev": "rig.warning", "t": t_us, "warning": "link_lost",
                         "source": "host", "detail": f"TCP link to {self.host}:{self.port} closed"})
        link.events.put(None)

    def _dispatch_loop(self, link: _Link) -> None:
        while (message := link.events.get()) is not None:
            self._event(message)

    def _event(self, message: dict) -> None:
        # Same contract as Transport._event, but one failing callback cannot starve the others.
        if message.get("ev") == "control.lost":
            self.control_token = None
        for callback in list(self._event_callbacks):
            try:
                callback(message)
            except Exception:
                log.exception("event callback failed for %s", message.get("ev"))

    def _keepalive_loop(self, link: _Link) -> None:
        last_ping = time.monotonic()
        while not link.closing.wait(KEEPALIVE_TICK_S):
            if not (self.keepalive and self.control_token):
                last_ping = 0.0  # ping as soon as control is (re)taken
                continue
            if time.monotonic() - last_ping < self._keepalive_interval_s():
                continue
            if not self._lock.acquire(blocking=False):
                continue  # a request is in flight; it refreshes the watchdog itself
            try:
                request_id = self._next_id
                self._next_id = (self._next_id + 1) % 2**32
                self._round_trip({"v": 1, "id": request_id, "op": "ping"}, KEEPALIVE_TIMEOUT_S,
                                 notify_ack=False)
            except TransportError:
                pass
            finally:
                self._lock.release()
                last_ping = time.monotonic()

    def _keepalive_interval_s(self) -> float:
        manifest_watchdog = ((self.manifest or {}).get("limits") or {}).get("watchdog_s")
        return float(self.watchdog_s or manifest_watchdog or DEFAULT_WATCHDOG_S) / 2
