"""Serialized harness access over TCP or Linux Bluetooth RFCOMM."""
import argparse
import json
import socket
import socketserver
import threading
import time

from pi.backends import RealBackend, SimBackend
from pi.header import Header
from pi.protocol import MAX_LINE, parse_command


class Harness:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.header = Header()

    def execute(self, command):
        start = time.perf_counter()
        with self.lock:
            try:
                command = parse_command(json.dumps(command))
                cmd = command["cmd"]
                if cmd == "ping":
                    result = {"pong": True}
                elif cmd in ("list_pins", "configure_pin", "write", "pwm", "read", "sample",
                             "release", "select_profile"):
                    result = self.generic(command)
                else:
                    if cmd == "set_pin":
                        pins = {17: "trig", 27: "fault"}
                        if type(command["pin"]) is not int or command["pin"] not in pins:
                            raise ValueError("digital output pin must be 17 or 27")
                        self.backend.apply(**{pins[command["pin"]]: command["val"]})
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

    def generic(self, command):
        cmd = command["cmd"]
        backend = self.backend
        values = []
        result = {}
        if cmd == "list_pins":
            return {"pins": self.header.snapshot()}
        if cmd == "select_profile":
            backend.select_profile(command["profile"])
            self.header = Header()
            return {"profile": command["profile"]}
        if cmd == "configure_pin":
            values = backend.configure(command["pin"], command["direction"],
                                       command.get("pull", "none"))
        elif cmd == "write":
            values = backend.write_gpio(command["pin"], command["val"])
        elif cmd == "pwm":
            values = backend.pwm_gpio(command["pin"], command["duty"], command["freq"])
        elif cmd == "read":
            values = backend.read_pins(command["pins"])
        elif cmd == "release":
            backend.release(command["pin"])
            state = self.header.gpio(command["pin"])
            state.update(direction=None, value=0)
            state.pop("duty", None)
            state.pop("decoded", None)
            values = [{"pin": command["pin"], "direction": None, "value": 0}]
        elif cmd == "sample":
            samples = backend.sample(command["pins"], command["duration_ms"], command["rate_hz"])
            for sample in samples:
                self.header.update(sample["pins"])
            result["samples"] = samples
            values = samples[-1]["pins"]
        if cmd != "sample":
            self.header.update(values)
        return {**result, "pins": values}


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
    parser.add_argument("--serial-port", default="/dev/serial0")
    parser.add_argument("--profile", choices=("esp32_safety", "mpu6050_gyro", "display"),
                        default="esp32_safety")
    parser.add_argument("--fixed", action="store_true")
    parser.add_argument("--rfcomm", metavar="ADAPTER_MAC")
    parser.add_argument("--channel", type=int, default=1)
    args = parser.parse_args()
    backend = (SimBackend(not args.fixed, args.profile) if args.backend == "sim" else
               RealBackend(args.serial_port, args.profile))
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
