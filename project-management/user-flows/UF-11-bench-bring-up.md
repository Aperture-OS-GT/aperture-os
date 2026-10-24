# UF-11 — Bench bring-up of the reference test

**Persona:** Tester / Firmware developer · **Epics:** EPIC-01, EPIC-05, EPIC-08 · **Stories:** US-67, US-69, US-70, US-71, US-01…US-12 · **Review items:** R-3, R-10 · **Milestones:** M2 (TCP), M3 (BLE)

The reference test from the brief, on real hardware:

> confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22; verify the ESP32
> prints BOOT OK over UART

## Preconditions
- Bridge installed and self-tested (UF-8). ESP32 DevKit flashed with
  `firmware/esp32-hil-demo` (US-67) and wired per its README (GPIO22→BTN, GPIO17←LED,
  GPIO14/15↔UART2, GPIO25→EN, GPIO24←heartbeat, common ground). The same plan already
  passes on the Virtual Rig scenario `button_led`.

## Flow

| # | Actor | Action | Result |
|---|---|---|---|
| 1 | Tester | Starts the bridge on the hardware backend over TCP (M2) | Rig panel: real board, all pins `disabled` |
| 2 | Tester | Runs plan `boot-ok-bench` (US-69) | Pre-flight confirms wiring for 22, 17, 25 and UART (UF-4) |
| 3 | Runner | Enables stream on GPIO17, opens UART, pulses GPIO25 low 20 ms (DUT reset, R-10) | `BOOT OK` row ≈ 300 ms after the reset edge; assertion 1 green |
| 4 | Runner | Drives GPIO22 low (press), `wait_for(17, rising, 50 ms, since=press)` (R-3) | Edge ~0.4 ms after the press; assertion 2 green; verdict PASS |
| 5 | Tester | Opens the parity report against the Virtual Rig run (US-71) | Same verdicts; latency 12 ms virtual vs ~0.4 ms bench, both inside 50 ms |
| 6 | Tester | Switches transport to Bluetooth (M3), reruns | PASS over BLE |
| 7 | Tester | Mid-run, powers off the laptop's Bluetooth | Bridge releases every output (UF-7 B); the ESP32's pull-up reads the button as released |

## Acceptance
- Steps 2–6 pass with no manual edits to the plan between Virtual Rig, TCP and BLE.
- A deliberately broken firmware (LED never follows the button) fails assertion 2 on all
  three transports — the bench must be able to fail, not only pass.
