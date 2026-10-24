# EPIC-03 — Agent tool set & request lifecycle

**Component:** Host · **Status:** todo

The rig is exposed to the agent as a bounded tool set. Every tool call becomes a `Request` with the lifecycle `requested → acknowledged → executed → observed`, and every stage is a session event the UI renders.

## Tool catalogue

| Tool | Args | Returns | Observed by |
|---|---|---|---|
| `connect` | `transport` (`virtual|tcp|ble`), `target?`, `scenario?` | manifest | manifest received |
| `disconnect` | — | — | transport closed |
| `take_control` / `release_control` | `force?` | controller state | `control.changed` event |
| `list_pins` | — | pins with mode/level/caps/wiring | — (read) |
| `set_mode` | `pin`, `mode`, `initial?` | mode, level | readback |
| `write` | `pin`, `level` | level | `pin.read` readback (or stream event) |
| `read` | `pin` or `pins` | levels | — |
| `pulse` | `pin`, `level`, `us` | edges | measured edges |
| `pwm` | `pin`, `freq_hz`, `duty` | freq, duty | short capture confirms frequency ±5 % |
| `uart_open` / `uart_close` | `tx`, `rx`, `baud` | port | — |
| `uart_send` | `port`, `data` (str or hex) | n | — |
| `uart_expect` | `port`, `pattern` (regex), `timeout_ms` | match, bytes consumed, `t` | match time |
| `spi_xfer` | `bus`, `cs`, `data` | data | — |
| `i2c_scan` / `i2c_read` / `i2c_write` | per protocol | per protocol | — |
| `capture` | `pins`, `duration_ms`, `rate_hz?` | edges, initial | `capture.done` |
| `assert_pin` | `pin`, `level` | pass/fail | `pin.read` |
| `assert_timeline` | `edges:[[t_us,pin,level]]`, `tolerance_us`, `capture_id?` | pass/fail + diff | capture |
| `wait_for` | `pin`, `edge` (`rising|falling|any`), `timeout_ms` | `t` or timeout | stream event |
| `request_wiring` | `pin`, `dut_pin`, `component`, `reason`, `dir` | request id | human confirm (US-30) |
| `note` | `text` | — | — |

## Stories

### US-21 — Connect / disconnect / control tools · P0 · todo
- `connect` picks the transport, stores the manifest in the session, auto-`take_control` unless `observe_only`.
- `disconnect` releases control first; session records `session.end`.

### US-22 — Pin tools (`list_pins`, `set_mode`, `write`, `read`, `pulse`, `pwm`) · P0 · todo
- Argument validation mirrors protocol v1; invalid args fail at `requested` with `err:bad_request` and never reach the transport.
- `write` and `set_mode` are **wiring-gated**: a pin with no confirmed wiring (US-30) returns `err:wiring_required` and auto-creates a wiring request unless `wiring_policy="off"` (Virtual Rig default).
- Results include the readback so the agent never has to issue a separate `read` to know what happened.

### US-23 — Bus tools (UART/SPI/I2C) · P1 · todo
- `uart_send` accepts text (UTF-8, `\n` escapes) or `hex:…`; `uart_expect` uses Python `re` on the accumulated rx buffer, returns the matched text and consumes through the match.
- Every byte in/out is logged as `bus.bytes` events (US-33).

### US-24 — `capture` · P0 · todo
- Default `rate_hz` = manifest `limits.capture_hz`; result normalised to host time (`t_host_us`) using the clock offset and also keeps `t_bridge_us`.

### US-25 — `assert_pin` and `assert_timeline` · P0 · todo
- `assert_pin` reads and compares; records an `assertion` event with `pass`, expected, actual.
- `assert_timeline` matches expected edges to captured edges by pin and order within `tolerance_us`; reports missing/extra/out-of-tolerance edges; pass only if all expected edges match and no extra edges on the asserted pins (configurable `allow_extra`).

### US-26 — `wait_for` · P0 · todo
- Subscribes to stream events for `pin`, resolves on the first matching edge, returns `t_host_us` and `t_bridge_us`; times out with `err:timeout` (an assertion failure if `assert:true` in a plan).
- Works identically on Virtual Rig (virtual clock) and bridge.

### US-27 — Tool schemas for LLMs · P0 · todo
- `aperture.tools.schemas()` returns OpenAI-compatible JSON schemas generated from one source of truth (no hand-written duplicates); `tests/test_tools.py` asserts every tool has a schema and every schema has an implementation.

### US-28 — Offline agent (no API key) · P1 · todo
- A rule-based `interpret(prompt)` covers the demo prompts (`press the button`, `set GPIO17 high`, `read pin 22`, `pulse`, `pwm 100 Hz 50%`, `send "x" over uart`, `expect "BOOT OK"`, `capture 17 22 for 200 ms`, `reset`, `run plan <name>`), so the UI is useful without an LLM.

### US-29 — Request lifecycle · P0 · todo
*As a tester I want to see exactly where an action is: asked, heard, done, or confirmed.*
- `Request(id, op, args, actor)`; stages `requested`, `acknowledged`, `executed`, `observed`; each with `t_host_us`, optional `t_bridge_us`, optional `err`.
- Stage transitions are monotonic and cannot be skipped except `acknowledged` when the transport cannot ack (flag `ack:"implicit"`).
- A request stuck in `requested` past `ack_timeout` → `executed` with `err:timeout` and `observed` with `result:"unknown"`.
- `observed` is produced by the tool's observation strategy (readback / stream / capture); tools with no observable effect (`read`, `list_pins`) mark `observed` = `executed` with `observe:"n/a"`.
- Unit tests cover happy path, ack timeout, response timeout, observe mismatch (`write 1`, readback 0 → `observed` with `mismatch:true`).

### US-30 — Human wiring requests · P0 · todo
*As a tester I want the agent to ask before it drives a pin I haven't wired.*
- `request_wiring` creates a `wiring.request` event and a pending card; `POST /api/wiring/{pin}` with `confirmed:true|false` resolves it (`wiring.confirmed` / `wiring.rejected`).
- Confirmed wiring is stored in the session's `wiring` map (`pin → {dut_pin, component, dir}`) and in `plan.requires.wiring` when a plan is saved.
- Rejection releases the pin (`pin.mode disabled`).
- Confirmation is metadata only; the UI copy says "marked as wired by <user>", never "connected".
