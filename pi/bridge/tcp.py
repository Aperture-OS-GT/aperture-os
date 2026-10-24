"""Protocol v1 over TCP: one compact JSON object per line (benches over Wi-Fi/Ethernet, CI).

Unauthenticated and lab-only, like the legacy daemon: bind to loopback unless the bench
network is trusted; use an SSH tunnel otherwise. Oversized lines get an error and the
connection is closed, because a JSON-lines stream cannot resynchronise mid-object.
"""
from __future__ import annotations

import socket
import socketserver
import threading

from aperture.protocol import MAX_JSON_LINE, ProtocolError, decode_json, encode_json, error, event

from .server import BridgeServer, Connection

WRITER_JOIN_S = 2.0


def encode_line(message: dict, now_us: int) -> bytes:
    """Encode ``message``; a reply or event too large for one line degrades explicitly."""
    try:
        return encode_json(message)
    except ProtocolError:
        if "ev" in message:
            return encode_json(event("rig.warning", now_us, warning="event_too_large",
                                     detail=f"{message['ev']} exceeds {MAX_JSON_LINE} bytes"))
        return encode_json(error(message.get("id"), now_us, "range",
                                 f"result exceeds {MAX_JSON_LINE} bytes; request less data"))


class _Handler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.request.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.server.track(self.request, True)

    def finish(self) -> None:
        self.server.track(self.request, False)
        super().finish()

    def handle(self) -> None:
        bridge: BridgeServer = self.server.bridge
        conn = bridge.open("tcp:{}:{}".format(*self.client_address[:2]))
        writer = threading.Thread(target=self._write_loop, args=(bridge, conn), daemon=True)
        writer.start()
        try:
            self._read_loop(bridge, conn)
        except OSError:
            pass
        finally:
            bridge.close(conn)
            writer.join(WRITER_JOIN_S)

    def _read_loop(self, bridge: BridgeServer, conn: Connection) -> None:
        while True:
            line = self.rfile.readline(MAX_JSON_LINE + 1)
            if not line:
                return
            if len(line) > MAX_JSON_LINE:
                conn.send(error(None, bridge.executor.now_us(), "bad_request", "line too long"))
                return
            if not line.strip():
                continue
            try:
                message = decode_json(line)
            except ProtocolError as exc:
                conn.send(error(None, bridge.executor.now_us(), exc.code, exc.msg))
                continue
            bridge.handle(conn, message)

    def _write_loop(self, bridge: BridgeServer, conn: Connection) -> None:
        while (message := conn.outbox.get()) is not None:
            try:
                self.wfile.write(encode_line(message, bridge.executor.now_us()))
                self.wfile.flush()
            except OSError:
                return


class TcpBridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], bridge: BridgeServer):
        self.bridge = bridge
        self._sockets: set[socket.socket] = set()
        self._sockets_lock = threading.Lock()
        super().__init__(address, _Handler)

    def track(self, sock: socket.socket, alive: bool) -> None:
        with self._sockets_lock:
            if alive:
                self._sockets.add(sock)
            else:
                self._sockets.discard(sock)

    def close_connections(self) -> None:
        """Drop every client so each handler runs its release-on-disconnect path."""
        with self._sockets_lock:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
