# 01 — Current state and gap analysis

Snapshot of `main` at `ab77c81` (PR #1 `devin/aperture-os-hil-rig`). 72 tests pass, `ruff` clean.

## What exists

| Area | Module(s) | What it does | Verdict |
|---|---|---|---|
| Header model | `pi/header.py` | 40-pin J8 table, BCM↔physical, kinds (gpio/3v3/5v/gnd/reserved), per-pin history (512 samples), pending-request slot | **Keep.** Becomes the shared `PinMap` used by Virtual Rig, bridge and UI |
| Wire protocol | `pi/protocol.py`, `docs/protocol.md` | Strict JSON-lines; legacy ESP32 commands (`set_pin`, `set_pwm`, `inject_fault`, `run_vector`, `assert_output`) + generic (`list_pins`, `configure_pin`, `write`, `pwm`, `read`, `sample`, `release`, `select_profile`) | **Supersede** with protocol v1 (`02-architecture.md`). Keep legacy handlers behind a compatibility shim until the demo is ported |
| Backends | `pi/gpio.py`, `pi/backends.py` | `SimBackend` (scripted ESP32 safety controller, gyro, display) and `RealBackend` (gpiozero, lazy pyserial/smbus2) | **Keep `RealBackend` internals**, refactor into bridge pin drivers. `SimBackend` scenarios become Virtual Rig *behaviors* |
| Daemon | `pi/daemon.py` | Threaded TCP server + Linux RFCOMM; one JSON object per line | **Keep TCP for dev**; add BLE GATT + CBOR chunking. Add control handshake, watchdog, stream mode |
| Agent | `agent/client.py`, `planner.py`, `generic_tools.py`, `profiles.py`, `wiring.py` | Sync JSON-lines client; offline plan + OpenAI tool loop; profile-gated generic tools; human wiring confirmation | **Rewrite** tools on the new transport/lifecycle; keep the wiring-gate concept and the profile runner's "verify physically, don't trust telemetry" rule |
| UI backend | `ui/server.py`, `ui/intent.py` | `http.server` console, SSE ring buffer (1000 events), prompt → intent parsing | **Rewrite** API around sessions/requests; keep SSE + loopback-only + origin checks |
| UI frontend | `ui/static/*` | Dark console: profile picker, chat, request cards, header map, waveform, debug log | **Replace** with the `looprig-ui` design (light, three columns) |
| Profiles | `profiles/*.json` | ESP32 safety (7 wires, 6 tests), MPU6050, display | **Migrate** to plan files (EPIC-05). Wiring lists become plan `requires.wiring` |
| Firmware | `firmware/esp32` | Demo safety controller with planted bug (`DEMO_BUG`) | **Keep** as the canonical bench DUT |
| CI | `docs/ci-workflow.yml` | Not wired up (lives under `docs/`) | **Move** to `.github/workflows/ci.yml` |

## Gaps against the spec

| Spec requirement | Status | Delivered by |
|---|---|---|
| BLE GATT service, host as central | missing (RFCOMM only) | US-01, US-02, US-15 |
| One session + explicit take-control handshake | missing | US-03, US-16 |
| Versioned CBOR/compact-JSON protocol, MTU chunking | missing (JSON-lines) | US-04 |
| Pin modes input / output / pullup / pulldown / PWM / disabled | partial (`tx`/`rx` + pulls) | US-05 |
| Digital read/write | present (generic) | US-05 |
| Pulse (N µs) | missing | US-06 |
| PWM duty + frequency | present | US-06 |
| UART tx/rx with baud | partial (serial used only by legacy telemetry) | US-07 |
| SPI transaction | missing | US-08 |
| I2C read/write | partial (smbus2 opened lazily; no commands) | US-08 |
| Waveform capture → edge list | partial (`sample` returns level list) | US-09 |
| Stream mode (push pin changes) | missing (poll only) | US-10 |
| Capability manifest on connect | missing (`list_pins` only) | US-11 |
| Fail-safe on disconnect/watchdog | partial (`release` on reject) | US-12 |
| Transport abstraction, BLE + Virtual Rig | missing | US-13…US-17 |
| Virtual Rig scriptable behaviors (loopback, UART echo, button) | partial (fixed ESP32 sim) | US-18…US-20 |
| Agent tool set (connect…wait_for) | partial (7 tools, profile-gated) | US-21…US-28 |
| Request lifecycle requested→ack→executed→observed | missing | US-29, US-30 |
| Sessions: ordered log, monotonic clock, save, replay, export JSON + report | partial (SSE buffer, localStorage only) | US-31…US-37 |
| NL test → deterministic plan, saved, runs without agent | partial (fixed profile tests) | US-38…US-44 |
| UI: rig panel / session log / pin map per design | partial, different design | US-45…US-58 |

## Decisions carried forward from the existing code

* BCM numbering is canonical; physical pin is display metadata.
* Human wiring confirmation gates driving a pin; confirmation is metadata, not continuity.
* Physical readback is always compared against DUT telemetry; discrepancies are reported, not hidden.
* The console binds to `127.0.0.1` and rejects cross-origin POSTs. No auth story in v1.
* Legacy ESP32 demo (`make demo`, `Run full safety plan`) must keep working until its plan-file equivalent is merged.

## Known risks

* Python GPIO sampling tops out at a few kHz; edge-list capture needs `lgpio`/`pigpio` or kernel `gpiod` events for µs resolution. Manifest advertises the real limit.
* BLE on Linux (BlueZ) needs `bless`/`dbus-next`; macOS host side needs `bleak`. Both are tested only on the bench (`bench-verify`).
* Another agent is editing the repo concurrently; new work lands in a new `aperture/` package to minimize conflicts, and legacy modules are touched only via shims.
