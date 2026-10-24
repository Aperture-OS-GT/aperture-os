"""Protocol v1: message schemas, validation, JSON/CBOR codecs and the BLE chunker.

Every message is a map. Requests carry ``v``, ``id`` and ``op``; responses echo
``id`` with ``ok``; ``ack`` is sent as soon as a request parses; events carry
``ev``. Byte fields are CBOR byte strings on BLE and base64 strings in JSON.
See project-management/02-architecture.md for the op table this implements.
"""
from __future__ import annotations

import base64
import json
import struct
from typing import Any

from .pins import GPIO_PINS, MODES

VERSION = 1
MAX_JSON_LINE = 65536

ERRORS = ("bad_request", "unknown_op", "unsupported", "not_controller", "busy", "range",
          "pin_mode", "io", "timeout")


class ProtocolError(Exception):
    def __init__(self, code: str, msg: str, **extra):
        assert code in ERRORS, code
        super().__init__(f"{code}: {msg}")
        self.code, self.msg, self.extra = code, msg, extra


# --- schemas -----------------------------------------------------------------
# field -> (type, required, extra) ; types: int, uint, bool, str, bytes, float, pin, list[int], mode, level
OPS: dict[str, dict[str, tuple]] = {
    "hello": {"client": ("str", True), "proto": ("list[int]", True)},
    "control.take": {"token": ("str", False), "force": ("bool", False), "watchdog_s": ("float", False)},
    "control.release": {"token": ("str", True)},
    "ping": {},
    "pin.mode": {"pin": ("pin", True), "mode": ("mode", True), "initial": ("level", False)},
    "pin.write": {"pin": ("pin", True), "level": ("level", True)},
    "pin.read": {"pin": ("pin", True)},
    "pin.pulse": {"pin": ("pin", True), "level": ("level", True), "us": ("uint", True)},
    "pin.pwm": {"pin": ("pin", True), "freq_hz": ("float", True), "duty": ("float", True)},
    "uart.open": {"tx": ("pin", True), "rx": ("pin", True), "baud": ("uint", True), "fmt": ("str", False)},
    "uart.close": {"port": ("uint", True)},
    "uart.write": {"port": ("uint", True), "data": ("bytes", True)},
    "uart.read": {"port": ("uint", True), "max": ("uint", False), "timeout_ms": ("uint", False)},
    "spi.xfer": {"bus": ("uint", True), "cs": ("uint", True), "data": ("bytes", True),
                 "hz": ("uint", False), "mode": ("uint", False)},
    "i2c.write": {"bus": ("uint", True), "addr": ("uint", True), "data": ("bytes", True)},
    "i2c.read": {"bus": ("uint", True), "addr": ("uint", True), "n": ("uint", True), "reg": ("uint", False)},
    "i2c.scan": {"bus": ("uint", True)},
    "capture.start": {"pins": ("list[pin]", True), "rate_hz": ("uint", True), "duration_ms": ("uint", True)},
    "capture.cancel": {"capture_id": ("uint", True)},
    "stream.set": {"pins": ("list[pin]", True), "on": ("bool", True)},
    "flash.begin": {},
    "flash.write": {"offset": ("uint", True), "data": ("bytes", True)},
    "flash.run": {"baud": ("uint", False), "manual_boot": ("bool", False)},
    "list_pins": {},
    "release_all": {},
}
DRIVING_OPS = frozenset({"pin.mode", "pin.write", "pin.pulse", "pin.pwm", "uart.open", "uart.close",
                         "uart.write", "spi.xfer", "i2c.write", "capture.start", "capture.cancel",
                         "release_all", "flash.begin", "flash.write", "flash.run"})
EVENTS = ("pin.change", "uart.rx", "capture.done", "control.changed", "control.lost", "stream.overflow",
          "rig.warning")


def _check(name: str, value: Any, kind: str) -> Any:
    bad = ProtocolError("bad_request", f"{name}: expected {kind}")
    if kind == "bool":
        if type(value) is not bool:
            raise bad
    elif kind in ("int", "uint"):
        if type(value) is not int or (kind == "uint" and value < 0):
            raise bad
    elif kind == "float":
        if type(value) not in (int, float) or isinstance(value, bool):
            raise bad
        value = float(value)
    elif kind == "str":
        if not isinstance(value, str) or len(value) > 4096:
            raise bad
    elif kind == "bytes":
        if isinstance(value, str):
            try:
                value = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                raise bad from None
        if not isinstance(value, (bytes, bytearray)) or len(value) > 32768:
            raise bad
        value = bytes(value)
    elif kind == "pin":
        if type(value) is not int or value not in GPIO_PINS:
            raise ProtocolError("bad_request", f"{name}: GPIO{value} is not an available BCM GPIO")
    elif kind == "level":
        if type(value) is not int or value not in (0, 1):
            raise bad
    elif kind == "mode":
        if value not in MODES:
            raise ProtocolError("bad_request", f"{name}: mode must be one of {', '.join(MODES)}")
    elif kind.startswith("list["):
        inner = kind[5:-1]
        if not isinstance(value, list) or not value or len(value) > 64:
            raise bad
        value = [_check(f"{name}[{i}]", v, inner) for i, v in enumerate(value)]
    else:  # pragma: no cover
        raise AssertionError(kind)
    return value


def validate_request(message: Any) -> dict:
    """Return a normalised request (bytes decoded, floats coerced) or raise ProtocolError."""
    if not isinstance(message, dict):
        raise ProtocolError("bad_request", "message must be a map")
    if message.get("v") != VERSION:
        raise ProtocolError("bad_request", f"unsupported protocol version {message.get('v')!r}; supported: [1]")
    if type(message.get("id")) is not int or not 0 <= message["id"] < 2**32:
        raise ProtocolError("bad_request", "id must be a u32")
    op = message.get("op")
    if op not in OPS:
        raise ProtocolError("unknown_op", f"unknown op {op!r}")
    schema = OPS[op]
    extra = set(message) - set(schema) - {"v", "id", "op"}
    if extra:
        raise ProtocolError("bad_request", f"unexpected fields: {', '.join(sorted(extra))}")
    out = {"v": VERSION, "id": message["id"], "op": op}
    for field, (kind, required, *_) in schema.items():
        if field in message:
            out[field] = _check(field, message[field], kind)
        elif required:
            raise ProtocolError("bad_request", f"missing field {field}")
    if op == "pin.pwm" and not (0 <= out["duty"] <= 1):
        raise ProtocolError("range", "duty must be between 0 and 1")
    if op == "pin.pwm" and out["freq_hz"] < 1:
        raise ProtocolError("range", "freq_hz must be >= 1")
    if op == "uart.open" and out["tx"] == out["rx"]:
        raise ProtocolError("bad_request", "tx and rx must differ")
    if op == "capture.start" and (out["rate_hz"] < 1 or out["duration_ms"] < 1):
        raise ProtocolError("range", "rate_hz and duration_ms must be >= 1")
    return out


def response(request_id: int, t_us: int, **result) -> dict:
    return {"v": VERSION, "id": request_id, "ok": True, "t": t_us, **result}


def error(request_id: int | None, t_us: int, code: str, msg: str, **extra) -> dict:
    assert code in ERRORS
    return {"v": VERSION, "id": request_id, "ok": False, "t": t_us, "err": code, "msg": msg, **extra}


def ack(request_id: int, t_us: int) -> dict:
    return {"v": VERSION, "id": request_id, "ack": True, "t": t_us}


def event(name: str, t_us: int, **payload) -> dict:
    assert name in EVENTS, name
    return {"v": VERSION, "ev": name, "t": t_us, **payload}


# --- JSON codec ---------------------------------------------------------------
def _jsonable(value):
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def encode_json(message: dict) -> bytes:
    line = json.dumps(_jsonable(message), separators=(",", ":")).encode()
    if len(line) > MAX_JSON_LINE:
        raise ProtocolError("bad_request", "message too large")
    return line + b"\n"


def decode_json(line: bytes) -> dict:
    if len(line) > MAX_JSON_LINE:
        raise ProtocolError("bad_request", "line too long")
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError("bad_request", "invalid JSON") from None
    if not isinstance(message, dict):
        raise ProtocolError("bad_request", "message must be a map")
    return message


# --- CBOR codec (RFC 8949 subset: ints, bytes, text, arrays, maps, bool, null, float64) -----
def encode_cbor(value: Any) -> bytes:
    out = bytearray()
    _cbor_write(out, value)
    return bytes(out)


def _cbor_head(out: bytearray, major: int, n: int) -> None:
    if n < 24:
        out.append((major << 5) | n)
    elif n < 0x100:
        out += bytes([(major << 5) | 24, n])
    elif n < 0x10000:
        out += bytes([(major << 5) | 25]) + struct.pack(">H", n)
    elif n < 0x100000000:
        out += bytes([(major << 5) | 26]) + struct.pack(">I", n)
    else:
        out += bytes([(major << 5) | 27]) + struct.pack(">Q", n)


def _cbor_write(out: bytearray, value: Any) -> None:
    if value is None:
        out.append(0xF6)
    elif value is True:
        out.append(0xF5)
    elif value is False:
        out.append(0xF4)
    elif type(value) is int:
        if value >= 0:
            _cbor_head(out, 0, value)
        else:
            _cbor_head(out, 1, -1 - value)
    elif isinstance(value, float):
        out.append(0xFB)
        out += struct.pack(">d", value)
    elif isinstance(value, (bytes, bytearray)):
        _cbor_head(out, 2, len(value))
        out += value
    elif isinstance(value, str):
        data = value.encode()
        _cbor_head(out, 3, len(data))
        out += data
    elif isinstance(value, (list, tuple)):
        _cbor_head(out, 4, len(value))
        for item in value:
            _cbor_write(out, item)
    elif isinstance(value, dict):
        _cbor_head(out, 5, len(value))
        for key, item in value.items():
            _cbor_write(out, key)
            _cbor_write(out, item)
    else:
        raise ProtocolError("bad_request", f"cannot encode {type(value).__name__}")


def decode_cbor(data: bytes) -> Any:
    value, offset = _cbor_read(bytes(data), 0)
    if offset != len(data):
        raise ProtocolError("bad_request", "trailing bytes after CBOR item")
    return value


def _cbor_read(data: bytes, offset: int):
    try:
        initial = data[offset]
        major, info = initial >> 5, initial & 0x1F
        offset += 1
        if info < 24:
            n = info
        elif info == 24:
            n, offset = data[offset], offset + 1
        elif info == 25:
            n, offset = struct.unpack_from(">H", data, offset)[0], offset + 2
        elif info == 26:
            n, offset = struct.unpack_from(">I", data, offset)[0], offset + 4
        elif info == 27:
            n, offset = struct.unpack_from(">Q", data, offset)[0], offset + 8
        else:
            raise ValueError  # indefinite lengths and reserved additional info are not supported
        if major == 0:
            return n, offset
        if major == 1:
            return -1 - n, offset
        if major in (2, 3):
            chunk = data[offset:offset + n]
            if len(chunk) != n:
                raise IndexError
            return (chunk if major == 2 else chunk.decode()), offset + n
        if major == 4:
            items = []
            for _ in range(n):
                item, offset = _cbor_read(data, offset)
                items.append(item)
            return items, offset
        if major == 5:
            result = {}
            for _ in range(n):
                key, offset = _cbor_read(data, offset)
                value, offset = _cbor_read(data, offset)
                result[key] = value
            return result, offset
        if major == 7:
            if info == 20:
                return False, offset
            if info == 21:
                return True, offset
            if info == 22:
                return None, offset
            if info == 26:
                return struct.unpack(">f", struct.pack(">I", n))[0], offset
            if info == 27:
                return struct.unpack(">d", struct.pack(">Q", n))[0], offset
        raise ValueError
    except (IndexError, ValueError, UnicodeDecodeError, struct.error):
        raise ProtocolError("bad_request", "invalid CBOR") from None


# --- BLE chunker ---------------------------------------------------------------
# Chunk = 1 header byte (bit7 = last, bits0-6 = seq 0..127) + payload. Payload per chunk = mtu - 4
# (ATT opcode + handle use 3 bytes). Reassembly resets on seq 0.
HEADER_BYTES, ATT_OVERHEAD = 1, 3
LAST = 0x80


def chunk(payload: bytes, mtu: int) -> list[bytes]:
    size = mtu - ATT_OVERHEAD - HEADER_BYTES
    if size < 1:
        raise ProtocolError("bad_request", f"mtu {mtu} too small")
    pieces = [payload[i:i + size] for i in range(0, len(payload), size)] or [b""]
    if len(pieces) > 128:
        raise ProtocolError("bad_request", "payload needs more than 128 chunks")
    return [bytes([seq | (LAST if seq == len(pieces) - 1 else 0)]) + piece for seq, piece in enumerate(pieces)]


class Reassembler:
    def __init__(self):
        self._parts: list[bytes] = []
        self._expect = 0

    def feed(self, piece: bytes) -> bytes | None:
        """Return the complete payload when the last chunk arrives, else None."""
        if not piece:
            raise ProtocolError("bad_request", "empty chunk")
        head, body = piece[0], piece[1:]
        seq, last = head & 0x7F, bool(head & LAST)
        if seq == 0:
            self._parts, self._expect = [], 0
        if seq != self._expect:
            self._parts, self._expect = [], 0
            raise ProtocolError("bad_request", f"chunk out of order (got {seq})")
        self._parts.append(body)
        self._expect += 1
        if not last:
            return None
        payload = b"".join(self._parts)
        self._parts, self._expect = [], 0
        return payload
