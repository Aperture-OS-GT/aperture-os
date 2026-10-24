"""BLE GATT transport (production link between Aperture OS and the Pi).

Service ``SERVICE_UUID`` with three characteristics:

* ``CMD_UUID``  write / write-without-response: chunked CBOR requests from the host
* ``RSP_UUID``  notify: chunked CBOR acks and responses
* ``EVT_UUID``  notify: chunked CBOR events (pin.change, uart.rx, capture.done, control.*)

Chunk framing is ``aperture.protocol.chunk`` (1 header byte: seq, bit7 = last).
``GattLink`` holds the framing logic and is hardware-free (unit tested);
``serve_ble`` hosts it with ``bless`` on BlueZ. BLE allows one central at a time,
so disconnect detection is polled and the bridge watchdog backs it up.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from aperture.protocol import ProtocolError, Reassembler, chunk, decode_cbor, encode_cbor, error
from aperture.transport.ble import CMD_UUID, EVT_UUID, RSP_UUID, SERVICE_UUID

from .core import BridgeCore, Link

DEFAULT_MTU = 185  # conservative ATT MTU most centrals negotiate; manifest advertises limits.mtu

log = logging.getLogger("aperture.bridge.gatt")


class GattLink:
    """Framing between GATT characteristic I/O and a bridge ``Link``.

    ``notify(char_uuid, chunk_bytes)`` pushes one notification. Acks/responses go to
    RSP, events to EVT.
    """

    def __init__(self, core: BridgeCore, notify: Callable[[str, bytes], None], mtu: int = DEFAULT_MTU):
        self.core, self.notify, self.mtu = core, notify, mtu
        self.reassembler = Reassembler()
        self.link: Link | None = None

    def connected(self) -> Link:
        if self.link is None or not self.link.open:
            self.link = self.core.attach(self._send, "ble")
            self.reassembler = Reassembler()
        return self.link

    def disconnected(self) -> None:
        if self.link is not None:
            self.link.close()
            self.link = None

    def _send(self, message: dict) -> None:
        target = EVT_UUID if "ev" in message else RSP_UUID
        for piece in chunk(encode_cbor(message), self.mtu):
            self.notify(target, piece)

    def on_write(self, data: bytes) -> None:
        link = self.connected()
        try:
            payload = self.reassembler.feed(bytes(data))
            if payload is None:
                return
            message = decode_cbor(payload)
        except ProtocolError as exc:
            link.send(error(None, self.core.now_us(), exc.code, exc.msg))
            return
        link.handle(message)


async def serve_ble(core: BridgeCore, name: str = "aperture-pi", mtu: int = DEFAULT_MTU,
                    stop: asyncio.Event | None = None) -> None:  # pragma: no cover - needs BlueZ + radio
    from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

    loop = asyncio.get_running_loop()
    server = BlessServer(name=name, loop=loop)
    queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

    def notify(char_uuid: str, piece: bytes) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (char_uuid, piece))

    gatt = GattLink(core, notify, mtu)

    def write_request(characteristic, value, **_):
        if str(characteristic.uuid).lower() == CMD_UUID:
            # execute off the event loop: requests may block (pulse, uart.read)
            loop.run_in_executor(None, gatt.on_write, bytes(value))

    def read_request(characteristic, **_):
        return characteristic.value

    server.read_request_func = read_request
    server.write_request_func = write_request
    await server.add_new_service(SERVICE_UUID)
    rw = GATTAttributePermissions.readable | GATTAttributePermissions.writeable
    await server.add_new_characteristic(
        SERVICE_UUID, CMD_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response, None, rw)
    for uuid in (RSP_UUID, EVT_UUID):
        await server.add_new_characteristic(
            SERVICE_UUID, uuid, GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
            bytearray(b""), GATTAttributePermissions.readable)
    await server.start()
    log.info("advertising %s as %r", SERVICE_UUID, name)

    async def pump_notifications():
        while True:
            char_uuid, piece = await queue.get()
            characteristic = server.get_characteristic(char_uuid)
            characteristic.value = bytearray(piece)
            server.update_value(SERVICE_UUID, char_uuid)

    async def watch_connection():
        was = False
        while True:
            await asyncio.sleep(0.5)
            now = await server.is_connected()
            if was and not now:
                log.info("central disconnected; failing safe")
                gatt.disconnected()
            was = now

    tasks = [asyncio.create_task(pump_notifications()), asyncio.create_task(watch_connection())]
    try:
        await (stop or asyncio.Event()).wait()
    finally:
        for task in tasks:
            task.cancel()
        gatt.disconnected()
        await server.stop()
