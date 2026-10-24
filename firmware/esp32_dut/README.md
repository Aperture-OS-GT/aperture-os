# rig-01 DUT firmware (ESP32 DevKit V1 + 16x2 LCD + status LED)

The device under test on the bench: the safety-controller test logic from `firmware/esp32`
plus a 16x2 LCD and a status LED that the agent drives over UART0.

## Behaviour

* Boot: LCD shows `Aperture OS` / `rig-01 online`, UART0 prints `BOOT OK` on its own line,
  the status LED on GPIO13 blinks at 2 Hz (timed with `millis()`, so the LCD and UART stay
  responsive).
* UART0 at 115200 8N1, one command per line:

| Command | Reply |
|---|---|
| `LCD <line1>\|<line2>` | `OK LCD` |
| `LED BLINK <hz>` (0 < hz ≤ 50) | `OK LED <hz>` |
| `LED ON` / `LED OFF` | `OK LED ON` / `OK LED OFF` |
| anything else | `ERR …` |

* Test logic, unchanged: trigger D4, PWM D5 (100 Hz), fault D18 → actuator D19, safety relay
  D21, JSON telemetry every 200 ms. `-DDEMO_BUG=1` (env `esp32dev`) keeps the planted relay
  bug; env `fixed` builds it with `DEMO_BUG=0`.

## Wiring

| Pi 4 | ESP32 | Purpose |
|---|---|---|
| pin 2 (5V) | VIN | power (no micro-USB) |
| pin 6 (GND) | GND | common ground |
| GPIO17 (pin 11) | D4 | test trigger |
| GPIO18 (pin 12) | D5 | PWM 100 Hz |
| GPIO27 (pin 13) | D18 | fault inject |
| GPIO22 (pin 15) | D19 | actuator readback |
| GPIO23 (pin 16) | D21 | safety relay readback |
| GPIO14 TXD (pin 8) | RX0 | flash data and commands |
| GPIO15 RXD (pin 10) | TX0 | flash replies and telemetry |
| GPIO25 (pin 22) | EN | reset pulse |
| GPIO24 (pin 18) | BOOT button leg (GPIO0) | bootloader select |

Peripherals on the ESP32: LCD RS 14, E 27, DB4–DB7 26/25/33/32 (VSS, V0, R/W, LED− to GND;
VDD, LED+ to 3V3); status LED GPIO13 → 330 Ω → LED → GND. Put a 330 Ω–1 kΩ resistor in series
with every Pi↔ESP32 signal; 3.3 V logic only.

## Build and flash

```sh
pio run -d firmware/esp32_dut -e esp32dev
```

The Pi flashes it over the header (no USB): `program_esp32` in the console, or on the Pi
`python -m pi.bridge.esp32_flash <dir with the .bin files> --port /dev/serial0`. BOOT is
driven from GPIO24; if that wire is not fitted, hold the ESP32's BOOT button and pass
`--manual-boot`.
