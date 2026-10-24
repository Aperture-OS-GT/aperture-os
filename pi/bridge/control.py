"""Take-control handshake and watchdog for the hardware executor.

Same protocol v1 semantics and payloads as ``VirtualRig`` (token, ``force`` after
``idle_s``, ``watchdog_s``, ``control.changed`` / ``control.lost``), plus two fixes from
project-management/04-spec-review.md:

* R-5: the watchdog is suspended while a controller request is executing, so a long
  ``uart.read`` or ``pin.pulse`` cannot release the rig underneath its own caller.
* R-6: ``force`` measures idle time from the controller's last *driving* op; keepalive
  pings keep the watchdog satisfied but do not make an absent tester look busy.
"""
from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass

from aperture.protocol import ProtocolError


@dataclass
class Controller:
    token: str
    client: str
    watchdog_s: float
    seen_us: int          # last message from the controller (watchdog)
    active_us: int        # last driving op (force/idle)
    in_flight: int = 0    # controller requests currently executing


class Control:
    def __init__(self, clock_us: Callable[[], int], limits: dict,
                 release_all: Callable[[], None], emit: Callable[..., None]):
        self._clock, self._limits = clock_us, limits
        self._release_all, self._emit = release_all, emit
        self.controller: Controller | None = None

    @property
    def client(self) -> str | None:
        return self.controller.client if self.controller else None

    def take(self, client: str, token: str | None, force: bool, watchdog_s: float | None) -> dict:
        now = self._clock()
        token = token or secrets.token_hex(8)
        current = self.controller
        previous, reason = None, "take"
        if current and current.token != token:
            idle_s = (now - current.active_us) / 1e6
            if not (force and idle_s >= self._limits["idle_s"]):
                raise ProtocolError("busy", "rig is controlled by another client",
                                    controller=current.client, idle_s=round(idle_s, 1))
            previous, reason = current.client, "force"
            self._release_all()
        watchdog = float(watchdog_s or self._limits["watchdog_s"])
        self.controller = Controller(token, client, watchdog, now, now)
        self._emit("control.changed", controller=client, previous=previous, reason=reason)
        return {"controller": True, "token": token, "watchdog_s": watchdog}

    def release(self, token: str) -> dict:
        if not self.controller or self.controller.token != token:
            raise ProtocolError("not_controller", "token does not hold control")
        self._release_all()
        self.controller = None
        self._emit("control.changed", controller=None, reason="release")
        return {}

    def require(self) -> None:
        if not self.controller:
            raise ProtocolError("not_controller", "take control first")

    def begin(self, driving: bool) -> None:
        """A controller request starts executing: refresh and suspend the watchdog."""
        if self.controller:
            now = self._clock()
            self.controller.seen_us = now
            if driving:
                self.controller.active_us = now
            self.controller.in_flight += 1

    def end(self) -> None:
        if self.controller:
            self.controller.in_flight = max(0, self.controller.in_flight - 1)
            self.controller.seen_us = self._clock()

    def check_watchdog(self) -> bool:
        """Release and emit ``control.lost`` if the controller went silent. True if it fired."""
        current = self.controller
        if not current or current.in_flight:
            return False
        if self._clock() - current.seen_us <= current.watchdog_s * 1e6:
            return False
        self._lose("watchdog")
        return True

    def disconnected(self, token: str | None) -> None:
        if self.controller and (token is None or self.controller.token == token):
            self._lose("disconnect")

    def _lose(self, reason: str) -> None:
        self._release_all()
        self.controller = None
        self._emit("control.lost", reason=reason)
