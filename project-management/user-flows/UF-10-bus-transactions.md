# UF-10 — UART, I2C and SPI transactions

**Persona:** Tester / Agent / Firmware developer · **Epics:** EPIC-01, EPIC-03, EPIC-04 · **Stories:** US-07, US-08, US-23, US-33 · **Review items:** R-2, R-7, R-9

## Preconditions
- Controller on a rig. Virtual Rig scenarios: `uart_echo` (echo + `ping`→`pong`) and
  `esp32_safety` (MPU6050 register device at `0x68`). No shipped scenario has an SPI
  device yet: add `{"type": "SpiLoopback", "cs": 0}` to read back what was written;
  without one the Virtual Rig clocks back zeros, like an unconnected MISO pulled low.

## UART

| # | Actor | Action | Result |
|---|---|---|---|
| 1 | Agent | `uart_open(tx=14, rx=15, baud=115200)` | Port 0; GPIO14/15 show as UART in the pin map and refuse `pin.mode` (`err:pin_mode`) |
| 2 | Agent | `uart_send(port=0, data="ping\n")` | `bus.bytes dir:tx` in the session; mono transcript row `UART0 ▸ "ping\n"` |
| 3 | Device | Echoes and answers | `uart.rx` events → `bus.bytes dir:rx` rows `UART0 ◂ "ping\n"`, `◂ "pong\n"` |
| 4 | Agent | `uart_expect(port=0, pattern="pong", timeout_ms=1000)` | Match with text and bridge timestamp; consumed through the match |

Observers see the same `uart.rx` rows but cannot `uart.read` while a controller holds the
rig (R-2), so they never steal bytes from the controller's `uart_expect`.

## I2C

| # | Action | Result |
|---|---|---|
| 1 | `i2c_scan(bus=1)` | `[0x68]` |
| 2 | `i2c_read(bus=1, addr=0x68, n=1, reg=0x75)` | `0x68` (MPU6050 `WHO_AM_I`); an assertion compares it |
| 3 | `i2c_read` at an address with no device | `err:io` "no device acknowledged at 0x50" |

## SPI

| # | Action | Result |
|---|---|---|
| 1 | `spi_xfer(bus=0, cs=0, data=[0x9F, 0, 0, 0])` | Bytes of equal length clocked back (e.g. a flash JEDEC ID) |
| 2 | `spi_xfer` on a bus not in the manifest | `err:unsupported` |

## Acceptance
- Every byte in either direction appears exactly once in the session with a timestamp and
  survives export/replay; byte fields have the same Python type on every transport (R-7).
- On a Pi 4 the manifest reports the UART device in use (`/dev/serial0` → mini-UART) so a
  baud mismatch is diagnosable (R-9).
