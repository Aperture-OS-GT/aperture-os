"""Transport-independent bridge core: ack -> execute -> respond, event fan-out, fail-safe.

``BridgeCore`` wraps an *engine* exposing the VirtualRig interface (``execute``,
``on_event``, ``now_us``, ``pump``, ``client_disconnected``). Engines: ``VirtualRig``
(realtime clock, for CI/demo bridges) and ``HardwareRig`` (real GPIO on the Pi).
Servers (TCP, BLE GATT) create one ``Link`` per connected client.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from aperture.protocol import ProtocolError, ack, error, response

PUMP_INTERVAL_S = 0.001
log = logging.getLogger("aperture.bridge")


class Link:
    """One connected client. ``send`` delivers a protocol message map to it."""

    def __init__(self, core: BridgeCore, send: Callable[[dict], None], name: str):
        self.core, self._send, self.name = core, send, name
        self.token: str | None = None
        self.open = True
        self._send_lock = threading.Lock()

    def send(self, message: dict) -> None:
        if not self.open:
            return
        with self._send_lock:
            try:
                self._send(message)
            except OSError:
                self.open = False

    def handle(self, message) -> None:
        """Handle one decoded request map: ack immediately, execute, respond."""
        request_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(request_id, int):
            self.send(error(None, self.core.now_us(), "bad_request", "id must be a u32"))
            return
        self.send(ack(request_id, self.core.now_us()))
        try:
            result = self.core.execute(message)
            if message.get("op") == "control.take":
                self.token = result.get("token")
            elif message.get("op") == "control.release":
                self.token = None
            self.send(response(request_id, self.core.now_us(), **result))
        except ProtocolError as exc:
            self.send(error(request_id, self.core.now_us(), exc.code, exc.msg, **exc.extra))
        except Exception as exc:  # noqa: BLE001 - a driver fault must not kill the bridge
            self.send(error(request_id, self.core.now_us(), "io", f"{type(exc).__name__}: {exc}"))

    def close(self) -> None:
        if not self.open and self not in self.core.links:
            return
        self.open = False
        self.core.detach(self)


class BridgeCore:
    def __init__(self, engine, pump_interval_s: float = PUMP_INTERVAL_S):
        self.engine = engine
        self.links: list[Link] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        engine.on_event(self._broadcast)
        self._pump = threading.Thread(target=self._pump_loop, args=(pump_interval_s,), name="bridge-pump",
                                      daemon=True)
        self._pump.start()

    def now_us(self) -> int:
        return self.engine.now_us()

    def execute(self, message: dict) -> dict:
        return self.engine.execute(message)

    def attach(self, send: Callable[[dict], None], name: str) -> Link:
        link = Link(self, send, name)
        with self._lock:
            self.links.append(link)
        return link

    def detach(self, link: Link) -> None:
        with self._lock:
            if link in self.links:
                self.links.remove(link)
        if link.token:
            # fail safe: the controlling client went away -> every output back to high-Z
            self.engine.client_disconnected(link.token)
            link.token = None

    def _broadcast(self, message: dict) -> None:
        if message.get("ev") == "control.lost":
            for link in list(self.links):
                link.token = None
        for link in list(self.links):
            link.send(message)

    def _pump_loop(self, interval: float) -> None:
        """Runs due scheduled work (capture ends, PWM edges, behaviors) and the watchdog."""
        while not self._stop.wait(interval):
            try:
                self.engine.pump()
            except Exception:  # keep the watchdog alive whatever a behavior does
                log.exception("pump failed")

    def close(self) -> None:
        self._stop.set()
        for link in list(self.links):
            link.close()
        release = getattr(self.engine, "shutdown", None)
        if release:
            release()
