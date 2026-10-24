# ESP32 HIL demo DUT

Bench device-under-test for the reference test in the product brief:

> confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22; verify the
> ESP32 prints BOOT OK over UART

It behaves like the Virtual Rig scenario `button_led` (the button + LED + UART
scenario of `project-management/user-flows/UF-6-virtual-rig-in-ci.md`), so a plan
authored and passing against the Virtual Rig exercises the same behaviour on real
hardware. The legacy safety-controller DUT in `firmware/esp32` is unchanged.

## Behaviour

| DUT behaviour | Observable on the Pi | Virtual Rig `button_led` behavior |
|---|---|---|
| LED output (ESP32 GPIO2) is HIGH while the button input (ESP32 GPIO4) is LOW | GPIO17 rises < 1 ms after GPIO22 is driven LOW | `Loopback a=22 b=17 invert delay_us=12000` |
| Prints `BOOT OK\n` on UART2 300 ms after reset | `BOOT OK\n` received on the Pi UART (GPIO15 RXD0) | `UartEcho banner="BOOT OK\n" delay_ms=300` |
| Echoes every UART byte it receives | bytes sent on GPIO14 TXD0 come back on GPIO15 | `UartEcho` echo |
| 2 Hz, 50 % square wave on ESP32 GPIO18 | GPIO24 toggles every 250 ms | `Blinker pin=24 hz=2.0 duty=0.5` |

The real LED follows the button in well under 1 ms instead of the scenario's 12 ms, so
both pass a "within 50 ms" assertion; a plan's timing tolerance should not assume either.

The button is active-low with the ESP32's internal pull-up, so a released (high-impedance)
Pi pin reads as "not pressed": the bridge's fail-safe release never presses the button.

## Wiring (Pi numbering is BCM)

| Raspberry Pi | ESP32 DevKit | Pi pin mode | Purpose |
|---|---|---|---|
| GND, physical 6 | GND | — | Common ground (required) |
| GPIO22, physical 15 | GPIO4 | `output`, idle HIGH | Button; drive LOW to press |
| GPIO17, physical 11 | GPIO2 | `input` | LED mirror |
| GPIO14 TXD0, physical 8 | GPIO16 RX2 | UART TX | Pi → DUT bytes (echoed) |
| GPIO15 RXD0, physical 10 | GPIO17 TX2 | UART RX | DUT → Pi: `BOOT OK`, echo |
| GPIO25, physical 22 | EN | `output`, idle HIGH | Reset: pulse LOW ≥ 20 ms |
| GPIO24, physical 18 | GPIO18 | `input` | 2 Hz heartbeat |

**3.3 V logic only.** Power the ESP32 from its own supply (USB charger or bench supply),
not from a Pi GPIO, and share ground. 330 Ω–1 kΩ series resistors on each signal are
recommended. Wire with both boards powered off.

The banner is printed once per boot. A plan that checks it on real hardware must open
the UART first and then reset the DUT by pulsing GPIO25 LOW (EN), which the Virtual Rig
does implicitly on `uart.open`.

## Build and flash

```sh
pip install platformio
pio run -d firmware/esp32-hil-demo -t upload
```

Flash over USB as a separate provisioning step. Disconnect the Pi's GPIO25 → EN wire
(or leave GPIO25 `disabled`) while the USB programmer's auto-reset circuit drives EN,
and disconnect the USB data cable before HIL operation.

## Bench check without Aperture

With the wiring above and a USB-serial adapter on GPIO16/GPIO17 instead of the Pi, a
terminal at 115200 8N1 shows `BOOT OK` after pressing EN and echoes typed characters.
Grounding GPIO4 lights the DevKit's GPIO2 LED (on boards that have one).
