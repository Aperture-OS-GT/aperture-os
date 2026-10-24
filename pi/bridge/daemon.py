"""``aperture-bridge``: the Pi bridge daemon.

    aperture-bridge --ble                       # production: BLE GATT peripheral, real GPIO
    aperture-bridge --tcp 0.0.0.0:8766          # bench: JSON lines over TCP (lab only)
    aperture-bridge --virtual button_led --tcp  # no Pi: realtime Virtual Rig behind the same protocol

Every link shares one engine, so control ownership, the watchdog and the fail-safe
release are identical whatever the transport.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import threading

from aperture.rig.virtual import VirtualRig, load_scenario

from .core import BridgeCore

log = logging.getLogger("aperture.bridge")


def build_engine(args):
    if args.virtual:
        return VirtualRig(load_scenario(args.virtual), realtime=True,
                          limits={"watchdog_s": args.watchdog} if args.watchdog else None)
    from .hardware import HardwareRig, LgpioDriver
    limits = {"watchdog_s": args.watchdog} if args.watchdog else None
    return HardwareRig(LgpioDriver(chip=args.chip, serial_device=args.serial), limits=limits,
                       denied_pins=args.deny)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aperture-bridge", description=__doc__.split("\n\n")[0])
    parser.add_argument("--ble", action="store_true", help="advertise the Aperture GATT service (needs bless + BlueZ)")
    parser.add_argument("--name", default="aperture-pi", help="BLE local name")
    parser.add_argument("--mtu", type=int, default=185, help="BLE notification MTU")
    parser.add_argument("--tcp", nargs="?", const="127.0.0.1:8766", metavar="HOST:PORT",
                        help="also serve JSON lines over TCP (default 127.0.0.1:8766; lab only)")
    parser.add_argument("--virtual", metavar="SCENARIO", help="serve a realtime Virtual Rig instead of real GPIO")
    parser.add_argument("--watchdog", type=float, help="default control watchdog seconds")
    parser.add_argument("--chip", type=int, help="gpiochip number (auto: Pi 5 = 4, else 0)")
    parser.add_argument("--serial", default="/dev/serial0", help="UART device for GPIO14/15")
    parser.add_argument("--deny", type=int, action="append", default=[], help="BCM pin the bridge must never drive")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    if not (args.ble or args.tcp):
        parser.error("choose at least one link: --ble and/or --tcp")

    core = BridgeCore(build_engine(args))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    server = None
    if args.tcp:
        from .tcp import TcpBridgeServer
        host, _, port = args.tcp.rpartition(":")
        server = TcpBridgeServer(core, host or "127.0.0.1", int(port or 8766))
        server.start()
        log.info("TCP bridge on %s:%d (unauthenticated: keep it on a lab network)", *server.server_address[:2])
    try:
        if args.ble:
            from .gatt import serve_ble

            async def run_ble():
                event = asyncio.Event()
                loop = asyncio.get_running_loop()
                threading.Thread(target=lambda: (stop.wait(), loop.call_soon_threadsafe(event.set)),
                                 daemon=True).start()
                await serve_ble(core, args.name, args.mtu, event)
            asyncio.run(run_ble())
        else:
            stop.wait()
    finally:
        if server is not None:
            server.shutdown()
        core.close()  # releases every output (high-Z) before exit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
