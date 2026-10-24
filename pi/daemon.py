"""Serialized harness access over TCP or Linux Bluetooth RFCOMM."""
import argparse
import json
import socket
import socketserver
import threading
import time

from pi.backends import RealBackend, SimBackend
from pi.protocol import MAX_LINE, parse_command


class Harness:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()

    def execute(self, command):
        start = time.perf_counter()
        with self.lock:
            try:
                cmd = command["cmd"]
                if cmd == "ping":
                    result = {"pong": True}
                else:
                    if cmd == "set_pin":
                        self.backend.apply(**{ "trig" if command["pin"] == 17 else "fault":
                                              command["val"]})
                    elif cmd == "set_pwm":
                        self.backend.apply(pwm=command["duty"])
                    elif cmd == "inject_fault":
                        self.backend.apply(fault=command["val"])
                    elif cmd == "run_vector":
                        self.backend.apply(**{k: command[k] for k in ("trig", "pwm", "fault")})
                    result = self.backend.read()
                    if cmd == "assert_output":
                        deadline = start + command["timeout_ms"] / 1000
                        while result["physical"][str(command["pin"])] != command["expected"]:
                            if time.perf_counter() >= deadline:
                                return {**result, "pass": False, "error": "assertion timed out",
                                        "latency_ms": round((time.perf_counter()-start)*1000, 3)}
                            time.sleep(0.005)
                            result = self.backend.read()
                return {**result, "pass": True,
                        "latency_ms": round((time.perf_counter() - start) * 1000, 3)}
            except (OSError, ValueError, TimeoutError) as exc:
                return {"pass": False, "error": str(exc),
                        "latency_ms": round((time.perf_counter() - start) * 1000, 3)}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        while True:
            try:
                line = self.rfile.readline(MAX_LINE + 1)
                if not line:
                    return
                start = time.perf_counter()
                try:
                    command = parse_command(line)
                    response = self.server.harness.execute(command)
                except (ValueError, UnicodeError) as exc:
                    response = {"pass": False, "error": str(exc),
                                "latency_ms": round((time.perf_counter()-start)*1000, 3)}
                self.wfile.write(json.dumps(response).encode() + b"\n")
                self.wfile.flush()
                if len(line) > MAX_LINE:
                    return
            except (OSError, TimeoutError):
                return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, harness):
        self.harness = harness
        super().__init__(address, Handler)


class RFCOMMServer(Server):
    address_family = getattr(socket, "AF_BLUETOOTH", 31)

    def server_bind(self):
        self.socket.close()
        self.socket = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self.socket.bind(self.server_address)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["sim", "real"], default="sim")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--serial-port", default="/dev/ttyUSB0")
    parser.add_argument("--fixed", action="store_true")
    parser.add_argument("--rfcomm", metavar="ADAPTER_MAC")
    parser.add_argument("--channel", type=int, default=1)
    args = parser.parse_args()
    backend = SimBackend(not args.fixed) if args.backend == "sim" else RealBackend(args.serial_port)
    server_type = RFCOMMServer if args.rfcomm else Server
    address = (args.rfcomm, args.channel) if args.rfcomm else (args.host, args.port)
    try:
        with server_type(address, Harness(backend)) as server:
            print(f"Aperture HIL ready: {address} backend={args.backend}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()


if __name__ == "__main__":
    main()
