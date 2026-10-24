import pytest

from aperture import pins, protocol
from aperture.protocol import ProtocolError, Reassembler, chunk, validate_request


def req(op, **args):
    return {"v": 1, "id": 7, "op": op, **args}


def test_header_and_manifest_shape():
    assert len(pins.HEADER) == 40 and len(pins.GPIO_PINS) == 26
    assert pins.PHYSICAL[17] == 11 and pins.PHYSICAL[2] == 3
    manifest = pins.build_manifest("x", "1", "board")
    assert manifest["proto"] == 1 and len(manifest["pins"]) == 26
    p18 = next(p for p in manifest["pins"] if p["bcm"] == 18)
    assert p18["phys"] == 12 and "pwm" in p18["caps"] and p18["alt"] == "PWM0"
    assert "uart" in next(p for p in manifest["pins"] if p["bcm"] == 14)["caps"]
    assert "i2c" in next(p for p in manifest["pins"] if p["bcm"] == 2)["caps"]
    rows = pins.header_rows()
    assert rows[26]["kind"] == "reserved" and rows[0]["kind"] == "3v3" and rows[5]["kind"] == "gnd"
    assert pins.build_manifest("x", "1", "b", denied={17})["pins"][0]["bcm"] != 17


@pytest.mark.parametrize("message", [
    req("pin.write", pin=17, level=1), req("pin.mode", pin=4, mode="input_pullup"),
    req("pin.pwm", pin=18, freq_hz=100, duty=0.5), req("capture.start", pins=[17, 22], rate_hz=1000, duration_ms=50),
    req("uart.write", port=0, data=b"hi"), req("hello", client="t", proto=[1]), req("ping"), req("release_all"),
])
def test_valid_requests(message):
    out = validate_request(message)
    assert out["op"] == message["op"] and out["id"] == 7


@pytest.mark.parametrize("message,code", [
    ({"v": 2, "id": 1, "op": "ping"}, "bad_request"), (req("nope"), "unknown_op"),
    (req("pin.write", pin=0, level=1), "bad_request"), (req("pin.write", pin=17, level=2), "bad_request"),
    (req("pin.write", pin=17, level=True), "bad_request"), (req("pin.write", pin=17), "bad_request"),
    (req("pin.write", pin=17, level=1, extra=1), "bad_request"), (req("pin.mode", pin=17, mode="tx"), "bad_request"),
    (req("pin.pwm", pin=18, freq_hz=100, duty=1.5), "range"), (req("pin.pwm", pin=18, freq_hz=0, duty=.5), "range"),
    (req("uart.open", tx=14, rx=14, baud=9600), "bad_request"), (req("capture.start", pins=[], rate_hz=1, duration_ms=1), "bad_request"),
    (req("uart.write", port=0, data="not base64!"), "bad_request"), ("ping", "bad_request"),
    ({"v": 1, "id": -1, "op": "ping"}, "bad_request"),
])
def test_invalid_requests(message, code):
    with pytest.raises(ProtocolError) as info:
        validate_request(message)
    assert info.value.code == code


def test_json_codec_round_trips_bytes_as_base64():
    message = protocol.response(3, 10, data=b"\x00\xff")
    line = protocol.encode_json(message)
    assert line.endswith(b"\n") and b'"data":"AP8="' in line
    decoded = protocol.decode_json(line)
    assert validate_request(req("uart.write", port=0, data=decoded["data"]))["data"] == b"\x00\xff"
    with pytest.raises(ProtocolError):
        protocol.decode_json(b"[1,2]")
    with pytest.raises(ProtocolError):
        protocol.decode_json(b"x" * (protocol.MAX_JSON_LINE + 1))


@pytest.mark.parametrize("value", [
    0, 23, 24, 255, 256, 65535, 65536, 2**32, -1, -1000, True, False, None, 1.5, -0.25, "héllo", b"\x01\x02",
    [1, "a", [b"x"]], {"v": 1, "id": 4, "op": "uart.write", "data": b"\xff" * 300, "nested": {"k": [None, 2.0]}},
])
def test_cbor_round_trip(value):
    assert protocol.decode_cbor(protocol.encode_cbor(value)) == value


def test_cbor_rejects_garbage():
    for data in (b"\x1c", b"\x5a\xff\xff\xff\xff", b"\x80\x00", b""):
        with pytest.raises(ProtocolError):
            protocol.decode_cbor(data)


@pytest.mark.parametrize("mtu", [23, 185, 247, 512])
@pytest.mark.parametrize("size", [0, 1, 19, 20, 500, 2000])
def test_chunker_round_trip(mtu, size):
    payload = bytes(range(256)) * (size // 256 + 1)
    payload = payload[:size]
    pieces = chunk(payload, mtu)
    assert all(len(p) <= mtu - 3 for p in pieces)
    assert pieces[-1][0] & protocol.LAST and not any(p[0] & protocol.LAST for p in pieces[:-1])
    reassembler = Reassembler()
    results = [reassembler.feed(p) for p in pieces]
    assert results[:-1] == [None] * (len(pieces) - 1) and results[-1] == payload


def test_chunker_out_of_order_and_reset():
    pieces = chunk(b"a" * 100, 23)
    assert len(pieces) == 6
    reassembler = Reassembler()
    reassembler.feed(pieces[0])
    with pytest.raises(ProtocolError):
        reassembler.feed(pieces[2])
    assert reassembler.feed(pieces[0]) is None  # seq 0 restarts cleanly
    for p in pieces[1:-1]:
        assert reassembler.feed(p) is None
    assert reassembler.feed(pieces[-1]) == b"a" * 100
    with pytest.raises(ProtocolError):
        chunk(b"x" * 20_000, 23)
    with pytest.raises(ProtocolError):
        chunk(b"x", 4)


def test_cbor_and_chunks_carry_every_op():
    for op, schema in protocol.OPS.items():
        args = {}
        for field, (kind, required, *_) in schema.items():
            if not required:
                continue
            args[field] = {"str": "c", "list[int]": [1], "bool": True, "uint": 1, "float": 1.0, "pin": 17,
                           "mode": "output", "level": 1, "bytes": b"\x00", "list[pin]": [17]}[kind]
        if op == "uart.open":
            args["rx"] = 15
        message = validate_request(req(op, **args))
        wire = protocol.encode_cbor(message)
        reassembler = Reassembler()
        back = [reassembler.feed(p) for p in chunk(wire, 23)][-1]
        assert validate_request(protocol.decode_cbor(back)) == message
