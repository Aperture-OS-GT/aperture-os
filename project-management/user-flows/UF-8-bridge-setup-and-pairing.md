# UF-8 — Set up a Pi bridge and pair a host

**Persona:** Rig admin · **Epics:** EPIC-01, EPIC-07, EPIC-08 · **Stories:** US-01, US-02, US-11, US-62, US-64, US-70 · **Review items:** R-8, R-9

## Preconditions
- Raspberry Pi 4 or 5 on Raspberry Pi OS Bookworm (64-bit), network access for the install,
  nothing wired to the header yet. Host laptop with Aperture OS installed.

## Flow

| # | Rig admin does | System shows / does | Notes |
|---|---|---|---|
| 1 | Runs `scripts/pi-install.sh --name rig01` on the Pi | Installs the `[pi]` extras, enables I2C/SPI and `enable_uart=1`, disables the serial login console, keeps Bluetooth on the PL011 UART, installs `aperture-bridge.service` | Pi 4: the header UART becomes the mini-UART (R-9); Pi 5 is unaffected |
| 2 | Fits two jumper wires (e.g. GPIO17↔GPIO27, GPIO22↔GPIO23) and runs `aperture-bridge --self-test --jumper 17:27 --jumper 22:23` | Table of checks on the real header: output→input both ways, pull-up/pull-down read back, PWM frequency within tolerance by capture, pulse width, `release_all` leaves every pin `disabled`; exit 0 | US-70. No DUT needed; proves the GPIO stack before anything can be damaged |
| 3 | Removes jumpers, `sudo systemctl enable --now aperture-bridge` | Service starts with every GPIO `disabled` (high-Z) and advertises `aperture-rig01` | `journalctl -u aperture-bridge` shows the manifest summary |
| 4 | On the host: connects with transport `Bluetooth` (UF-1 step 3a) | First connection triggers OS pairing; the bridge accepts the bond | US-64: GATT characteristics require an encrypted link |
| 5 | Inspects the rig panel | Bridge `aperture-rig01`, firmware, board `Raspberry Pi 4 Model B Rev 1.5`, power `VBUS ok` (from `vcgencmd get_throttled`), MTU, clock offset | US-11, US-46 |
| 6 | (Optional) Wi-Fi bench without BLE: `aperture-bridge --transport tcp --host 0.0.0.0` | Host connects with transport `TCP rig01.local:8765` | Unauthenticated; trusted networks or an SSH tunnel only (README) |

## Edge cases
- Bluetooth soft-blocked (`rfkill`) or BlueZ missing → the daemon exits non-zero with the
  fix in the message; `--transport tcp` still works.
- Under-voltage → manifest `power.vbus_ok:false`; the rig panel shows a warning before any
  test runs.
- Serial console still enabled on `/dev/serial0` → `uart.open` fails `io` with a hint to
  run the install script again.
- Self-test fails → the service is not enabled by the install script; output names the
  failing pin and check.

## Acceptance
- A fresh Pi reaches step 5 with only the commands above.
- After step 3 and after any reboot, every GPIO reads `disabled` before a host connects.
