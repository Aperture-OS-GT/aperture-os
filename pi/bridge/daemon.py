"""aperture-bridge: serve protocol v1 to Aperture OS hosts.

``--backend virtual`` (the default) serves a realtime Virtual Rig scenario ("bridge sim"), so
CI and benches without a Pi exercise exactly the daemon, framing and control code a Pi runs.
``--backend real`` drives this Pi's 40-pin header through lgpio (imported only then): GPIO,
the buses that are enabled, and ESP32 flashing over the UART with EN/BOOT on ``--flash-pins``.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading

from aperture import pins as P
from aperture.rig.virtual import VirtualRig, list_scenarios, load_scenario

from .esp32_flash import BOOT_PIN, EN_PIN
from .hardware import HardwareExecutor
from .hardware_buses import BusConfig
from .server import PUMP_INTERVAL_S, BridgeServer
from .tcp import TcpBridge

DEFAULT_PORT = 8765
AUTO_DEVICES = {"uart": "/dev/serial0", "i2c": "/dev/i2c-1", "spi": "/dev/spidev0.0"}
SWITCHES = ("auto", "on", "off")
log = logging.getLogger("aperture.bridge")


def _gpios(text: str) -> tuple[int, ...]:
    try:
        pins = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a comma list of BCM GPIOs") from None
    bad = [str(pin) for pin in pins if pin not in P.GPIO_PINS]
    if bad:
        raise argparse.ArgumentTypeError(f"not header GPIOs: {', '.join(bad)}")
    return pins


def _denied(text: str) -> frozenset[int]:
    return frozenset(_gpios(text))


def _flash_pins(text: str) -> tuple[int, int] | None:
    if text == "off":
        return None
    pins = _gpios(text)
    if len(pins) != 2 or pins[0] == pins[1]:
        raise argparse.ArgumentTypeError("expected BOOT,EN as two different BCM GPIOs, or off")
    return pins[0], pins[1]


def _add_real_args(parser: argparse.ArgumentParser) -> None:
    real = parser.add_argument_group("--backend real (Raspberry Pi header)")
    real.add_argument("--chip", type=int, default=0, help="gpiochip of the 40-pin header")
    real.add_argument("--name", default=f"aperture-{socket.gethostname().split('.')[0]}",
                      help="bridge name reported in the manifest")
    real.add_argument("--uart", default="auto", metavar="DEVICE|auto|off",
                      help=f"serial device on GPIO14/15; auto = {AUTO_DEVICES['uart']} if present")
    real.add_argument("--i2c", choices=SWITCHES, default="auto",
                      help=f"I2C1 on GPIO2/3; auto = {AUTO_DEVICES['i2c']} exists")
    real.add_argument("--spi", choices=SWITCHES, default="auto",
                      help=f"SPI0 on GPIO7-11; auto = {AUTO_DEVICES['spi']} exists")
    real.add_argument("--flash-pins", type=_flash_pins, default=f"{BOOT_PIN},{EN_PIN}",
                      metavar="BOOT,EN|off",
                      help="GPIOs wired to the ESP32 BOOT (GPIO0) and EN pins; needs a UART")
    real.add_argument("--deny", type=_denied, default=frozenset(), metavar="PINS",
                      help="comma list of BCM GPIOs the bridge must never touch")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    scenarios = ", ".join(item["name"] for item in list_scenarios())
    parser = argparse.ArgumentParser(prog="aperture-bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--transport", choices=("tcp",), default="tcp")
    parser.add_argument("--host", default="127.0.0.1",
                        help="TCP bind address; 0.0.0.0 exposes this unauthenticated bridge to the LAN")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--backend", choices=("virtual", "real"), default="virtual")
    parser.add_argument("--scenario", default="empty",
                        help=f"Virtual Rig scenario name or JSON path ({scenarios})")
    parser.add_argument("--watchdog-s", type=float, default=None,
                        help="default controller watchdog when control.take does not set one")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    _add_real_args(parser)
    args = parser.parse_args(argv)
    if args.watchdog_s is not None and args.watchdog_s <= 0:
        parser.error("--watchdog-s must be positive")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0..65535")
    if args.chip < 0:
        parser.error("--chip must be >= 0")
    if args.flash_pins and set(args.flash_pins) & args.deny:
        parser.error("--flash-pins must not be in --deny")
    return args


def _switch(choice: str, bus: str) -> bool:
    return choice == "on" or (choice == "auto" and os.path.exists(AUTO_DEVICES[bus]))


def bus_config(args: argparse.Namespace) -> BusConfig:
    """Resolve auto/on/off against the device nodes this Pi actually has."""
    uart = None if args.uart == "off" else args.uart
    if args.uart == "auto":
        uart = AUTO_DEVICES["uart"] if os.path.exists(AUTO_DEVICES["uart"]) else None
    try:
        return BusConfig(uart=uart, i2c=_switch(args.i2c, "i2c"), spi=_switch(args.spi, "spi"),
                         flash=args.flash_pins if uart else None)
    except ValueError as exc:
        raise SystemExit(f"aperture-bridge: {exc}") from None


def _real_executor(args: argparse.Namespace, limits: dict | None) -> HardwareExecutor:
    from .drivers.lgpio_driver import LgpioDriver  # needs lgpio, which only a Pi has

    buses = bus_config(args)
    try:
        driver = LgpioDriver(args.chip)
    except (ImportError, OSError) as exc:
        raise SystemExit(f"aperture-bridge: cannot open gpiochip{args.chip}: {exc}") from None
    try:
        return HardwareExecutor(driver, args.name, buses, limits=limits, denied=args.deny)
    except OSError as exc:
        driver.close()
        raise SystemExit(f"aperture-bridge: cannot claim the header GPIOs ({exc}); is another "
                         "GPIO program running? --deny skips pins") from None


def build_executor(args: argparse.Namespace):
    limits = {"watchdog_s": args.watchdog_s} if args.watchdog_s else None
    if args.backend == "real":
        return _real_executor(args, limits)
    try:
        scenario = load_scenario(args.scenario)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from None
    return VirtualRig(scenario, realtime=True, limits=limits)


def describe(args: argparse.Namespace, executor) -> str:
    """What the ready line says about the backend."""
    if args.backend == "virtual":
        return f"backend=virtual scenario={args.scenario}"
    buses = executor.buses
    flash = f"boot{buses.flash[0]},en{buses.flash[1]}" if buses.flashing else "off"
    return (f"backend=real name={executor.name} uart={buses.uart or 'off'} "
            f"i2c={'on' if buses.i2c else 'off'} spi={'on' if buses.spi else 'off'} flash={flash}")


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
    print(f"Aperture bridge ready: tcp://{host}:{port} {describe(args, bridge.executor)}",
          flush=True)
    serve(server, bridge)


if __name__ == "__main__":
    main()
