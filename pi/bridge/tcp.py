"""Bench/dev transport: protocol v1 as JSON lines over TCP (one Link per socket).

Unauthenticated, lab-only. Binds loopback by default; use an SSH tunnel across networks.
"""
from __future__ import annotations

import socket
import socketserver
import threading

from aperture.protocol import ProtocolError, decode_json, encode_json, error

from .core import BridgeCore

MAX_LINE = 256 * 1024


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        core: BridgeCore = self.server.core
        sock = self.request
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        link = core.attach(lambda m: sock.sendall(encode_json(m)), f"tcp:{self.client_address[0]}")
        try:
            while link.open:
                line = self.rfile.readline(MAX_LINE)
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    message = decode_json(line)
                except ProtocolError as exc:
                    link.send(error(None, core.now_us(), exc.code, exc.msg))
                    continue
                link.handle(message)
        except OSError:
            pass
        finally:
            link.close()


class TcpBridgeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, core: BridgeCore, host: str = "127.0.0.1", port: int = 8766):
        self.core = core
        super().__init__((host, port), _Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="bridge-tcp", daemon=True)
        thread.start()
        return thread
