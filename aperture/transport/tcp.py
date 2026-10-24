"""TCP transport (bench/dev): protocol v1 as newline-delimited JSON to ``aperture-bridge --tcp``.

Unauthenticated and lab-only; the bridge binds loopback by default (tunnel over SSH).
"""
from __future__ import annotations

import socket
import threading

from ..protocol import ProtocolError, decode_json, encode_json
from .base import TransportError
from .stream import MessageTransport

DEFAULT_PORT = 8766


def parse_target(target: str | None) -> tuple[str, int]:
    target = (target or "127.0.0.1").removeprefix("tcp://")
    host, _, port = target.rpartition(":") if ":" in target else (target, "", "")
    try:
        return host or "127.0.0.1", int(port) if port else DEFAULT_PORT
    except ValueError:
        raise TransportError(f"bad TCP target {target!r} (want host[:port])") from None


class TcpTransport(MessageTransport):
    kind = "tcp"

    def __init__(self, target: str | None = None, client: str = "aperture-host", connect_timeout: float = 3.0):
        super().__init__(client)
        self.host, self.port = parse_target(target)
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._wlock = threading.Lock()

    def describe(self) -> dict:
        return {**super().describe(), "target": f"{self.host}:{self.port}"}

    def _open(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise TransportError(f"cannot reach bridge at {self.host}:{self.port}: {exc}") from exc
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        threading.Thread(target=self._reader, args=(sock,), name="tcp-reader", daemon=True).start()

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _write(self, message: dict) -> None:
        sock = self._sock
        if sock is None:
            raise OSError("socket closed")
        with self._wlock:
            sock.sendall(encode_json(message))

    def _reader(self, sock: socket.socket) -> None:
        buffer = b""
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        try:
                            self._deliver(decode_json(line))
                        except ProtocolError:
                            continue
        except OSError:
            pass
        if self._sock is sock:
            self._link_lost("bridge closed the connection")
