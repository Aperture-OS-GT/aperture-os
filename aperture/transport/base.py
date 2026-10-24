"""Transport abstraction: one interface for the Virtual Rig, TCP and BLE bridges.

``request`` returns once the bridge has *executed* the op (ok or error). The
bridge's ``ack`` and unsolicited events are delivered to registered callbacks so
the request lifecycle (requested -> acknowledged -> executed -> observed) can be
recorded by the session layer.
"""
from __future__ import annotations

import contextlib
import statistics
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from ..protocol import ProtocolError

DEFAULT_TIMEOUTS = {"ack": 1.0, "response": 5.0, "pin.pulse": 7.0, "uart.read": 30.0, "capture.start": 5.0}


class TransportError(Exception):
    pass


@dataclass
class Response:
    id: int
    ok: bool
    t_bridge_us: int
    result: dict = field(default_factory=dict)
    err: str | None = None
    msg: str | None = None

    def raise_for_error(self) -> Response:
        if not self.ok:
            raise ProtocolError(self.err or "io", self.msg or "bridge error")
        return self

    @classmethod
    def from_message(cls, message: dict) -> Response:
        if message.get("ok"):
            result = {k: v for k, v in message.items() if k not in ("v", "id", "ok", "t")}
            return cls(message["id"], True, message["t"], result)
        extra = {k: v for k, v in message.items() if k not in ("v", "id", "ok", "t", "err", "msg")}
        return cls(message.get("id"), False, message.get("t", 0), extra, message.get("err", "io"),
                   message.get("msg", ""))


class Transport(ABC):
    """Base class. Subclasses implement ``_connect``, ``_disconnect`` and ``_send``."""

    kind = "abstract"

    def __init__(self, client: str = "aperture-host"):
        self.client = client
        self.manifest: dict | None = None
        self.connected = False
        self.clock_offset_us: int | None = None
        self.control_token: str | None = None
        self.timeouts = dict(DEFAULT_TIMEOUTS)
        self._ack_callbacks: list[Callable[[int, int], None]] = []
        self._event_callbacks: list[Callable[[dict], None]] = []
        self._next_id = 1
        self._lock = threading.RLock()

    # --- registration -------------------------------------------------------------
    def on_ack(self, callback: Callable[[int, int], None]) -> None:
        self._ack_callbacks.append(callback)

    def on_event(self, callback: Callable[[dict], None]) -> None:
        self._event_callbacks.append(callback)

    def _acked(self, request_id: int, t_bridge_us: int) -> None:
        for callback in list(self._ack_callbacks):
            callback(request_id, t_bridge_us)

    def _event(self, message: dict) -> None:
        if message.get("ev") == "control.lost":
            self.control_token = None
        for callback in list(self._event_callbacks):
            callback(message)

    # --- lifecycle -------------------------------------------------------------------
    def connect(self) -> dict:
        with self._lock:
            if self.connected:
                return self.manifest
            self._connect()
            self.connected = True
            try:
                hello = self.request("hello", client=self.client, proto=[1]).raise_for_error()
            except Exception:
                self.connected = False
                self._disconnect()
                raise
            self.manifest = hello.result["manifest"]
            if self.manifest.get("proto") != 1:
                self.disconnect()
                raise TransportError(f"bridge speaks protocol v{self.manifest.get('proto')}; this host supports v1")
            self.sync_clock()
            return self.manifest

    def disconnect(self) -> None:
        with self._lock:
            if not self.connected:
                return
            try:
                if self.control_token:
                    with contextlib.suppress(TransportError, ProtocolError, OSError):
                        self.request("release_all")
                        self.request("control.release", token=self.control_token)
            finally:
                self.control_token = None
                self.connected = False
                self._disconnect()

    def describe(self) -> dict:
        return {"kind": self.kind, "connected": self.connected, "client": self.client,
                "controller": bool(self.control_token)}

    # --- requests --------------------------------------------------------------------
    def request(self, op: str, **args) -> Response:
        with self._lock:
            if not self.connected:
                raise TransportError("not connected")
            request_id = self._next_id
            self._next_id = (self._next_id + 1) % 2**32
            message = {"v": 1, "id": request_id, "op": op, **args}
            timeout = self.timeouts.get(op, self.timeouts["response"])
            response = self._send(message, timeout)
            if op == "control.take" and response.ok:
                self.control_token = response.result["token"]
            elif op == "control.release" and response.ok:
                self.control_token = None
            return response

    def take_control(self, force: bool = False, watchdog_s: float | None = None) -> Response:
        args = {"force": force}
        if watchdog_s is not None:
            args["watchdog_s"] = watchdog_s
        if self.control_token:
            args["token"] = self.control_token
        return self.request("control.take", **args)

    def release_control(self) -> Response:
        if not self.control_token:
            raise TransportError("not controller")
        return self.request("control.release", token=self.control_token)

    def sync_clock(self, samples: int = 5) -> int:
        """Estimate bridge_clock - host_clock (µs) from ping round trips (median)."""
        offsets = []
        for _ in range(samples):
            t0 = self.host_now_us()
            response = self.request("ping")
            t1 = self.host_now_us()
            if response.ok:
                offsets.append(response.t_bridge_us - (t0 + t1) // 2)
        self.clock_offset_us = int(statistics.median(offsets)) if offsets else None
        return self.clock_offset_us

    @staticmethod
    def host_now_us() -> int:
        return time.monotonic_ns() // 1000

    # --- to implement -------------------------------------------------------------
    @abstractmethod
    def _connect(self) -> None: ...

    @abstractmethod
    def _disconnect(self) -> None: ...

    @abstractmethod
    def _send(self, message: dict, timeout: float) -> Response:
        """Deliver ``message``; call ``self._acked`` on ack; return the executed Response."""
