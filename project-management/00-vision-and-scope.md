# 00 — Vision and scope

## One-liner

Aperture OS is an agentic hardware-in-the-loop (HIL) platform: an AI agent drives a physical test rig through a Raspberry Pi bridge while a hardware tester watches every GPIO pin change state live on one screen.

## The problem

Firmware is verified by hand: a human wires a breadboard, pokes inputs, reads a scope or a serial console, and eyeballs the result. Coding agents can now write firmware but cannot *prove* it works on real silicon. Aperture closes that loop with a rig the agent can operate, under human supervision, with every action and every pin transition recorded.

## Personas

| Persona | Goal | Pain today |
|---|---|---|
| **Tester** (hardware QA / embedded engineer at the bench) | Exercise a DUT without writing firmware or re-wiring mid-test; trust what the screen says | Scope + serial terminal + notebook; no record of what happened |
| **Agent** (LLM with tools, acting for the tester) | Turn a natural-language test into deterministic actions and assertions | No safe, bounded hardware API; no ground truth |
| **Firmware developer** | Re-run the same plan after every build, in CI, with and without hardware | Tests live in someone's head |
| **Rig admin** | Keep one rig shared by several people without them fighting over it | Nothing arbitrates control |

## System topology

```
Aperture OS host (laptop/server) ── BLE GATT ──▶ Raspberry Pi bridge daemon
                                                   └─ 40-pin header ─▶ harness/breakout ─▶ DUT (ESP32 first)
                                                                                           └─ peripherals (display, sensors, LEDs, buttons, serial)
```

* The **Pi is a dumb, trusted I/O bridge**: owns the pins, executes commands, streams pin state. No intelligence.
* **All intelligence lives in the host** (Aperture OS): agent tools, sessions, plans, UI.
* The **DUT is whatever is wired to the header**. ESP32 is the first target, not a dependency.

## Three components

1. **Pi bridge daemon** — BLE GATT service, versioned command protocol (CBOR, chunked for MTU), pin/bus/capture/stream commands, capability manifest, fail-safe to high-impedance.
2. **Aperture OS host** — transport abstraction (BLE-to-Pi and built-in **Virtual Rig**), agent tool set, request lifecycle, sessions with replay/export, natural-language test plans.
3. **Tester interface** — Rig panel · Current session log · to-scale 40-pin Pin map.

## Guiding principles

1. **Virtual Rig parity.** Everything works with no Pi present. The UI, tools, sessions and logs are identical; CI runs on the Virtual Rig.
2. **Explicit lifecycle.** Every agent action is a request: `requested → acknowledged → executed → observed`. Each stage is visible.
3. **Evidence, not claims.** Pass/fail comes from measured pin state and bus bytes, timestamped on a monotonic clock. Serial claims from the DUT are cross-checked against pins when a plan says so.
4. **Human in the wiring loop.** The agent asks for a wire; a human confirms it. Software never asserts a wire is present.
5. **Fail safe.** Disconnect or watchdog timeout returns every output to high-impedance.
6. **One controller at a time.** Explicit take-control handshake; observers can watch, only the controller can drive.

## In scope (v1)

* Pi 4/5 40-pin header, 3.3 V logic, digital in/out with pulls, PWM, pulse, UART, SPI, I2C, waveform capture (edge list), pin-change streaming.
* BLE GATT transport with CBOR payloads; TCP transport retained for bench/dev; Virtual Rig in-process.
* Agent tool set (see EPIC-03), request lifecycle, sessions (JSON + human-readable report), NL test plans compiled to deterministic plan files.
* Tester UI per `looprig-ui/` design: light theme, three columns.

## Out of scope (v1)

* Analog capture / ADC (Pi has none; a HAT could be a later epic).
* Flashing firmware over USB from the Pi (landing page mentions it; it is a v2 epic — needs USB-serial and `esptool` on the Pi).
* Multi-rig fleets, cloud sync, auth/multi-tenant. The host binds to loopback.
* Logic-analyzer-grade timing. Capture is best-effort software sampling (≤ ~10 kHz on Python; documented per manifest).

## Glossary

| Term | Meaning |
|---|---|
| **Rig** | Pi bridge + harness + DUT as one unit |
| **Bridge** | The Pi daemon that owns the pins |
| **Virtual Rig** | In-memory simulation of a 40-pin header with scriptable behaviors |
| **DUT** | Device under test |
| **BCM** | Broadcom GPIO number (the number in `GPIOnn`) |
| **Physical pin** | 1–40 position on the J8 header |
| **Request** | One agent/tester action with a lifecycle |
| **Event** | Anything that happened: request stage, pin change, bus bytes, note |
| **Session** | One connection + ordered event log |
| **Plan** | Deterministic, re-runnable list of steps + assertions |
| **Manifest** | Bridge's self-description: pins, capabilities, firmware version |
| **Controller** | The one client holding control of the rig |
