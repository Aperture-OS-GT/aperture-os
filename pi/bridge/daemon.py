"""aperture-bridge: serve protocol v1 to Aperture OS hosts.

``--backend virtual`` serves a realtime Virtual Rig scenario ("bridge sim"), so CI and
benches without a Pi exercise exactly the daemon, framing and control code a Pi runs.
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading

from aperture.rig.virtual import VirtualRig, list_scenarios, load_scenario

from .server import PUMP_INTERVAL_S, BridgeServer
from .tcp import TcpBridge

DEFAULT_PORT = 8765
log = logging.getLogger("aperture.bridge")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    scenarios = ", ".join(item["name"] for item in list_scenarios())
    parser = argparse.ArgumentParser(prog="aperture-bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--transport", choices=("tcp",), default="tcp")
    parser.add_argument("--host", default="127.0.0.1",
                        help="TCP bind address; 0.0.0.0 exposes this unauthenticated bridge to the LAN")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--backend", choices=("virtual",), default="virtual")
    parser.add_argument("--scenario", default="empty",
                        help=f"Virtual Rig scenario name or JSON path ({scenarios})")
    parser.add_argument("--watchdog-s", type=float, default=None,
                        help="default controller watchdog when control.take does not set one")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    if args.watchdog_s is not None and args.watchdog_s <= 0:
        parser.error("--watchdog-s must be positive")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0..65535")
    return args


def build_executor(args: argparse.Namespace):
    limits = {"watchdog_s": args.watchdog_s} if args.watchdog_s else None
    try:
        scenario = load_scenario(args.scenario)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from None
    return VirtualRig(scenario, realtime=True, limits=limits)


def build(args: argparse.Namespace) -> tuple[TcpBridge, BridgeServer]:
    bridge = BridgeServer(build_executor(args), pump_interval_s=PUMP_INTERVAL_S)
    try:
        server = TcpBridge((args.host, args.port), bridge)
    except OSError:
        bridge.shutdown()
        raise
    return server, bridge


def serve(server: TcpBridge, bridge: BridgeServer) -> None:
    """Serve until ``server.shutdown()``; always release outputs on the way out."""
    try:
        server.serve_forever()
    finally:
        server.close_connections()
        bridge.shutdown()
        server.server_close()
        log.info("bridge stopped; all outputs released")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server, bridge = build(args)

    def stop(signum, _frame) -> None:
        log.info("received %s; shutting down", signal.Signals(signum).name)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    host, port = server.server_address[:2]
    print(f"Aperture bridge ready: tcp://{host}:{port} backend={args.backend} "
          f"scenario={args.scenario}", flush=True)
    serve(server, bridge)


if __name__ == "__main__":
    main()
