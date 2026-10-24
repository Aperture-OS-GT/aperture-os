# EPIC-02 — Transport abstraction & Virtual Rig

**Component:** Host · **Status:** todo

One `Transport` interface, three implementations (Virtual, TCP, BLE). The Virtual Rig is a complete in-memory bridge: same protocol, same manifest shape, deterministic clock, scriptable behaviors. It is how tests are authored and how CI runs.

## Stories

### US-13 — Transport interface · P0 · todo
- `Transport` ABC: `connect() -> Manifest`, `disconnect()`, `request(op, **args) -> Response` (returns after `executed`; fires `on_ack`), `events()` iterator / `on_event(cb)`, `clock_offset_us`.
- Every call is thread-safe; requests are serialised per transport; events are delivered on a dedicated thread.
- Timeouts: `ack_timeout` (default 1 s), `response_timeout` (default 5 s, op-specific override for `pin.pulse`, `capture.start`, `uart.read`).
- A conformance test suite (`tests/conformance/`) runs against any `Transport` fixture.

### US-14 — Virtual Rig transport · P0 · todo
- `VirtualTransport(rig)` implements `Transport` in-process with zero latency by default and optional injected `latency_us` / `drop_rate` for UI realism and robustness tests.
- `ack` is emitted synchronously, then execution, so the lifecycle has all four stages even in simulation.
- Manifest: `bridge:"virtual-rig"`, `fw:"virtual"`, same pins/caps as a Pi 4.

### US-15 — BLE transport (host as central) · P1 · todo
- `BleTransport(address|name)` built on `bleak`; discovers by service UUID, negotiates MTU, maps `cmd/rsp/evt` characteristics, uses the shared chunker/codec.
- Auto-ping every `watchdog_s/2` while connected.
- `bench-verify` on macOS and Linux hosts.

### US-16 — TCP transport · P0 · todo
- `TcpTransport(host, port)` speaks JSON protocol v1 (one message per line, ≤ 64 KiB) to `pi/bridge` in `sim` or `real` mode. Used for CI conformance of the bridge and for Wi-Fi benches.

### US-17 — Virtual Rig pin engine · P0 · todo
- Full 40-pin J8 model (28 GPIO, power, ground, ID_SD/ID_SC reserved); modes and pulls behave electrically: an `input` with no driver reads its pull (`pullup`→1, `pulldown`→0, none→last level / `floating` flag).
- Deterministic virtual clock (`advance(us)`); real-time mode advances with wall clock for the UI.
- PWM is modelled as a square wave visible to captures and loopback peers; `pin.pulse` produces exact edges.
- Capture and stream produce the same event shapes as the bridge.

### US-18 — Behavior: loopback pairs · P0 · todo
- `Loopback(a, b)`: driving `a` as output makes `b` (input) follow within one clock tick; works both ways; conflicting drivers (both outputs, different levels) flag `contention` in the pin state and emit a `rig.warning` event.

### US-19 — Behavior: fake UART echo device · P0 · todo
- `UartEcho(rx_pin, tx_pin, baud, banner=b"BOOT OK\n", delay_ms=…)`: on reset or `uart.open`, emits `banner` after `delay_ms`; echoes received bytes; optionally line-mode responses via a `dict` or callback (`{"ping\n": "pong\n"}`).
- Bytes appear as `uart.rx` events with timestamps spaced by `10/baud` per byte.

### US-20 — Behavior: fake button and scripted DUT · P0 · todo
- `Button(out_pin, pressed_level=0, bounce_ms=0)`: `press()`, `release()`, `click(ms)`; with `bounce_ms>0` produces a bounce train in captures.
- `Blinker(pin, hz, duty)`: free-running square wave for `measure_frequency`-style plans.
- `ScriptedDut` port of today's `SimBackend` ESP32 safety controller (trigger/PWM/fault → actuator/safety + UART telemetry) with `demo_bug` flag so the planted-bug plan can fail/pass in CI.
- Behaviors are attached by name from a `rig.json` scenario file so CI and the UI's "Virtual Rig" picker share definitions.

## Acceptance for the epic
- `tests/conformance/` passes for `VirtualTransport` and `TcpTransport→bridge sim` with the same test bodies.
- `python -m aperture.rig.virtual --scenario esp32_safety` prints the manifest and streams pin changes to stdout (smoke).
