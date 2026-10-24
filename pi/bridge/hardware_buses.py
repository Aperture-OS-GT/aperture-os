"""UART, SPI and I2C ops for the hardware executor, and which buses a bridge owns.

A bus that is enabled here owns its header pins (they are reserved for it); a bus that is
not enabled answers ``err:unsupported`` and its pins stay ordinary GPIOs. The DUT's EN and
BOOT wires (``flash``) are reserved too whenever flashing is possible (a UART is enabled),
so a GPIO op can never hold the DUT in reset. Error codes and payloads follow the Virtual
Rig: an unopened port is ``bad_request``, a missing I2C device is ``io`` "no device
acknowledged at 0x..".
"""
from __future__ import annotations

import errno
import logging
import time
from dataclasses import dataclass, field

from aperture import pins as P
from aperture.protocol import ProtocolError

log = logging.getLogger(__name__)
UART_BAUD_MIN, UART_BAUD_MAX = 300, 4_000_000
UART_FORMAT = "8N1"
UART_RX_LIMIT = 65_536
SPI_DEFAULT_HZ = 1_000_000
SPI_MAX_HZ = 32_000_000
SPI_MODES = range(4)
I2C_MAX_ADDR = 0x7F
I2C_NACK_ERRNOS = frozenset({getattr(errno, "EREMOTEIO", 121), errno.ENXIO, errno.EIO})


@dataclass(frozen=True)
class BusConfig:
    uart: str | None = None  # serial device on GPIO14/15, e.g. /dev/serial0
    i2c: bool = False        # I2C1 on GPIO2/3
    spi: bool = False        # SPI0 on GPIO7-11
    flash: tuple[int, int] | None = None  # (BOOT, EN) GPIOs wired to the ESP32, e.g. (24, 25)

    def __post_init__(self) -> None:
        if self.flash is None:
            return
        taken = self._bus_pins()
        if len(set(self.flash)) != 2 or any(p not in P.GPIO_PINS or p in taken for p in self.flash):
            raise ValueError(f"flash pins {self.flash} must be two different GPIOs (BOOT, EN) "
                             "not used by an enabled bus")

    @property
    def flashing(self) -> bool:
        """The DUT can be programmed: esptool on the UART, EN and BOOT driven from GPIO."""
        return bool(self.uart and self.flash)

    def reserved_pins(self) -> dict[int, str]:
        reserved = self._bus_pins()
        if self.flashing:
            reserved.update({pin: "flash" for pin in self.flash})
        return reserved

    def _bus_pins(self) -> dict[int, str]:
        reserved: dict[int, str] = {}
        if self.uart:
            for port in P.UART_PORTS:
                reserved.update({port["tx"]: f"uart{port['id']}", port["rx"]: f"uart{port['id']}"})
        if self.i2c:
            for bus in P.I2C_BUSES:
                reserved.update({bus["sda"]: f"i2c{bus['bus']}", bus["scl"]: f"i2c{bus['bus']}"})
        if self.spi:
            for bus in P.SPI_BUSES:
                for pin in (bus["mosi"], bus["miso"], bus["sclk"], *bus["cs"]):
                    reserved[pin] = f"spi{bus['bus']}"
        return reserved

    def manifest_entries(self) -> dict:
        return {
            "uart": [{**port, "device": self.uart} for port in P.UART_PORTS] if self.uart else [],
            "spi": [{**bus, "cs": list(bus["cs"])} for bus in P.SPI_BUSES] if self.spi else [],
            "i2c": [dict(bus) for bus in P.I2C_BUSES] if self.i2c else [],
        }


NO_BUSES = BusConfig()


@dataclass
class HwPort:
    port: int
    tx: int
    rx: int
    baud: int
    device: object = None
    rxbuf: bytearray = field(default_factory=bytearray)


class BusOps:
    """Mixin for ``HardwareExecutor`` (uses ``driver``, ``buses``, ``ports``, ``lock``, ``_rx``,
    ``_emit``). Handlers run with the executor lock held."""

    # --- UART ---------------------------------------------------------------------------
    def _op_uart_open(self, r):
        config = next((p for p in P.UART_PORTS if (p["tx"], p["rx"]) == (r["tx"], r["rx"])), None)
        if config is None or not self.buses.uart:
            raise ProtocolError("unsupported", f"no UART on tx GPIO{r['tx']} / rx GPIO{r['rx']} "
                                               "on this bridge")
        if config["id"] in self.ports:
            raise ProtocolError("pin_mode", f"GPIO{r['tx']} is already in use")
        if not UART_BAUD_MIN <= r["baud"] <= UART_BAUD_MAX:
            raise ProtocolError("range", f"baud must be {UART_BAUD_MIN}..{UART_BAUD_MAX}")
        if r.get("fmt", UART_FORMAT) != UART_FORMAT:
            raise ProtocolError("unsupported", f"only {UART_FORMAT} framing is supported")
        port = HwPort(config["id"], r["tx"], r["rx"], r["baud"])
        port.device = self.driver.open_uart(self.buses.uart, r["baud"],
                                            lambda data, t_us: self._uart_rx(port, data, t_us))
        self.ports[port.port] = port
        return {"port": port.port, "tx": port.tx, "rx": port.rx, "baud": port.baud}

    def _uart_rx(self, port: HwPort, data: bytes, t_us: int) -> None:
        """Driver reader thread: buffer bytes for uart.read and stream them as uart.rx."""
        with self.lock:
            if self.ports.get(port.port) is not port:
                return  # bytes from a port that has since been closed
            port.rxbuf += data
            overflow = len(port.rxbuf) - UART_RX_LIMIT
            if overflow > 0:
                del port.rxbuf[:overflow]
                self._emit("rig.warning", warning="uart_overflow", port=port.port, dropped=overflow,
                           detail=f"UART port {port.port} buffer full; oldest bytes dropped")
            self._emit("uart.rx", t_us, port=port.port, data=bytes(data))
            self._rx.notify_all()

    def _port(self, number: int) -> HwPort:
        if number not in self.ports:
            raise ProtocolError("bad_request", f"port {number} is not open")
        return self.ports[number]

    def _op_uart_close(self, r):
        port = self.ports.pop(self._port(r["port"]).port)
        port.device.close()
        self._rx.notify_all()
        return {}

    def _op_uart_write(self, r):
        return {"n": self._port(r["port"]).device.write(r["data"])}

    def _op_uart_read(self, r):
        port = self._port(r["port"])
        deadline = time.monotonic() + r.get("timeout_ms", 0) / 1000
        while not port.rxbuf and self.ports.get(port.port) is port:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._rx.wait(remaining)  # releases the executor lock while waiting
        n = r.get("max", len(port.rxbuf)) or len(port.rxbuf)
        data = bytes(port.rxbuf[:n])
        del port.rxbuf[:n]
        return {"data": data}

    def _close_ports(self) -> None:
        for port in list(self.ports.values()):
            try:
                port.device.close()
            except OSError:
                log.exception("failed to close UART port %s", port.port)
        self.ports.clear()

    # --- SPI ----------------------------------------------------------------------------
    def _op_spi_xfer(self, r):
        bus = next((b for b in P.SPI_BUSES if b["bus"] == r["bus"]), None)
        if bus is None or not self.buses.spi:
            raise ProtocolError("unsupported", f"SPI bus {r['bus']} is not enabled on this bridge")
        if r["cs"] >= len(bus["cs"]):
            raise ProtocolError("bad_request", f"cs must be 0..{len(bus['cs']) - 1}")
        mode = r.get("mode", 0)
        if mode not in SPI_MODES:
            raise ProtocolError("bad_request", "mode must be 0..3")
        hz = r.get("hz", SPI_DEFAULT_HZ)
        if not 1 <= hz <= SPI_MAX_HZ:
            raise ProtocolError("range", f"hz must be 1..{SPI_MAX_HZ}")
        return {"data": bytes(self.driver.spi_xfer(r["bus"], r["cs"], r["data"], hz, mode))}

    # --- I2C ----------------------------------------------------------------------------
    def _i2c(self, r) -> None:
        if not self.buses.i2c or all(b["bus"] != r["bus"] for b in P.I2C_BUSES):
            raise ProtocolError("unsupported", f"I2C bus {r['bus']} is not enabled on this bridge")
        if r.get("addr", 0) > I2C_MAX_ADDR:
            raise ProtocolError("bad_request", "addr must be a 7-bit address (0..0x7f)")

    def _i2c_call(self, r, call):
        try:
            return call()
        except OSError as exc:
            if exc.errno in I2C_NACK_ERRNOS:
                raise ProtocolError("io", f"no device acknowledged at 0x{r['addr']:02x}") from exc
            raise

    def _op_i2c_write(self, r):
        self._i2c(r)
        self._i2c_call(r, lambda: self.driver.i2c_write(r["bus"], r["addr"], r["data"]))
        return {"n": len(r["data"])}

    def _op_i2c_read(self, r):
        self._i2c(r)
        data = self._i2c_call(r, lambda: self.driver.i2c_read(r["bus"], r["addr"], r["n"], r.get("reg")))
        return {"data": bytes(data)}

    def _op_i2c_scan(self, r):
        self._i2c(r)
        return {"addrs": sorted(self.driver.i2c_scan(r["bus"]))}
