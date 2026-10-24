"""In-process transport to a ``VirtualRig``.

Messages are round-tripped through the JSON codec so anything that would not
survive a real wire fails here too. ``latency_us`` adds a deterministic delay
(virtual clock) or a real sleep (realtime clock) for UI realism.
"""
from __future__ import annotations

import time

from ..protocol import ProtocolError, ack, decode_json, encode_json, error, response
from ..rig.virtual import VirtualRig, load_scenario
from .base import Response, Transport


class VirtualTransport(Transport):
    kind = "virtual"

    def __init__(self, rig: VirtualRig | None = None, scenario: str | dict | None = None,
                 realtime: bool = False, latency_us: int = 0, client: str = "aperture-host"):
        super().__init__(client)
        if rig is None:
            data = load_scenario(scenario) if isinstance(scenario, str) else scenario
            rig = VirtualRig(data, realtime=realtime)
        self.rig = rig
        self.latency_us = int(latency_us)
        self._forwarding = False

    def describe(self) -> dict:
        return {**super().describe(), "scenario": self.rig.scenario.get("name", "empty"),
                "realtime": self.rig.clock.realtime}

    def _connect(self) -> None:
        if not self._forwarding:
            self.rig.on_event(self._forward)
            self._forwarding = True

    def _forward(self, message: dict) -> None:
        if self.connected:
            self._event(decode_json(encode_json(message)))

    def _disconnect(self) -> None:
        self.rig.client_disconnected(self.control_token)

    def _delay(self) -> None:
        if not self.latency_us:
            return
        if self.rig.clock.realtime:
            time.sleep(self.latency_us / 1e6)
        else:
            self.rig.advance(self.latency_us)

    def _send(self, message: dict, timeout: float) -> Response:
        wire = decode_json(encode_json(message))
        self._delay()
        self._acked(wire["id"], self.rig.now_us())
        try:
            result = self.rig.execute(wire)
            reply = response(wire["id"], self.rig.now_us(), **result)
        except ProtocolError as exc:
            reply = error(wire["id"], self.rig.now_us(), exc.code, exc.msg, **exc.extra)
        self._delay()
        return Response.from_message(decode_json(encode_json(reply)))

    def ack_message(self, request_id: int) -> dict:
        return ack(request_id, self.rig.now_us())
