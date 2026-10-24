import json
import socket

from pi.protocol import MAX_LINE


class Client:
    def __init__(self, host="127.0.0.1", port=8765, rfcomm=False):
        if rfcomm:
            self.socket = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                        socket.BTPROTO_RFCOMM)
            self.socket.settimeout(10)
            self.socket.connect((host, port))
        else:
            self.socket = socket.create_connection((host, port), timeout=10)
        self.file = self.socket.makefile("rwb")

    def command(self, **command):
        self.file.write(json.dumps(command).encode() + b"\n")
        self.file.flush()
        line = self.file.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE:
            raise ConnectionError("missing or oversized daemon response")
        return json.loads(line)

    def close(self):
        self.file.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
