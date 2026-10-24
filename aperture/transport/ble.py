"""BLE transport: Aperture OS as GATT central to the Pi bridge (``pi.bridge.gatt``).

Requests are CBOR, chunked to the negotiated MTU, written to CMD; acks/responses
arrive as RSP notifications, events as EVT notifications. ``bleak`` runs on a
private asyncio loop thread so the rest of the host stays synchronous.

``target`` is a device address/UUID, a device name, or empty (first device
advertising the Aperture service).
"""
from __future__ import annotations

import asyncio
import contextlib
import threading

from ..protocol import ProtocolError, Reassembler, chunk, decode_cbor, encode_cbor
from .base import TransportError
from .stream import MessageTransport

SERVICE_UUID = "6e0a7e00-5a1d-4e0a-9f3c-617065727475"
CMD_UUID = "6e0a7e01-5a1d-4e0a-9f3c-617065727475"
RSP_UUID = "6e0a7e02-5a1d-4e0a-9f3c-617065727475"
EVT_UUID = "6e0a7e03-5a1d-4e0a-9f3c-617065727475"


class BleFraming:
    """Hardware-free chunk framing for both notify characteristics (unit tested)."""

    def __init__(self, deliver):
        self.deliver = deliver
        self.rsp, self.evt = Reassembler(), Reassembler()

    def feed(self, char_uuid: str, data: bytes) -> None:
        assembler = self.evt if char_uuid.lower() == EVT_UUID else self.rsp
        try:
            payload = assembler.feed(bytes(data))
        except ProtocolError:
            return
        if payload is not None:
            try:
                self.deliver(decode_cbor(payload))
            except ProtocolError:
                pass

    @staticmethod
    def frames(message: dict, mtu: int) -> list[bytes]:
        return chunk(encode_cbor(message), mtu)


class BleTransport(MessageTransport):  # hardware paths: pragma: no cover
    kind = "ble"

    def __init__(self, target: str | None = None, client: str = "aperture-host", scan_timeout: float = 8.0):
        super().__init__(client)
        self.target = target or ""
        self.scan_timeout = scan_timeout
        self.framing = BleFraming(self._deliver)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self.mtu = 23
        self.address: str | None = None

    def describe(self) -> dict:
        return {**super().describe(), "target": self.address or self.target, "mtu": self.mtu}

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _open(self) -> None:
        try:
            import bleak  # noqa: F401
        except ImportError:
            raise TransportError("BLE needs bleak: pip install -e '.[ble]'") from None
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, name="ble-loop", daemon=True).start()
        try:
            self._run(self._aconnect(), self.scan_timeout + 15)
        except TransportError:
            raise
        except Exception as exc:  # bleak raises backend-specific errors
            raise TransportError(f"BLE connect failed: {exc}") from exc

    async def _aconnect(self) -> None:
        from bleak import BleakClient, BleakScanner
        target = self.target
        if not target:
            device = await BleakScanner.find_device_by_filter(
                lambda d, adv: SERVICE_UUID in [u.lower() for u in adv.service_uuids], timeout=self.scan_timeout)
        elif ":" in target or len(target) == 36:
            device = await BleakScanner.find_device_by_address(target, timeout=self.scan_timeout)
        else:
            device = await BleakScanner.find_device_by_name(target, timeout=self.scan_timeout)
        if device is None:
            raise TransportError(f"no Aperture bridge found over BLE ({target or 'any'})")
        client = BleakClient(device, disconnected_callback=lambda _c: self._link_lost("BLE link dropped"))
        await client.connect()
        self.framing = BleFraming(self._deliver)
        await client.start_notify(RSP_UUID, lambda _ch, data: self.framing.feed(RSP_UUID, data))
        await client.start_notify(EVT_UUID, lambda _ch, data: self.framing.feed(EVT_UUID, data))
        self._client, self.address, self.mtu = client, device.address, client.mtu_size or 23

    def _close(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                self._run(client.disconnect(), 5)

    def _write(self, message: dict) -> None:
        if self._client is None:
            raise OSError("BLE not connected")
        frames = BleFraming.frames(message, self.mtu)

        async def write_all():
            for frame in frames:
                await self._client.write_gatt_char(CMD_UUID, frame, response=True)
        try:
            self._run(write_all(), 10)
        except Exception as exc:
            raise OSError(str(exc)) from exc
