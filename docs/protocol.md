# HIL protocol v1

UTF-8 JSON objects, one per newline; one response per command in order. TCP and
RFCOMM share framing. Maximum request/response line is 65,536 bytes; oversized
requests get an error then disconnect. Idle connections time out after 30 seconds.
Multiple clients are supported; one lock serializes each command's stimulus and
measurement. A whole multi-command plan is not transactional: use one agent per rig.

| Command | Required additional fields |
|---|---|
| `ping` | none |
| `read_state` | none |
| `set_pin` | `pin`: 17 or 27, `val`: integer 0/1 |
| `set_pwm` | `pin`: 18, `duty`: number 0..1, 100 Hz fixed |
| `inject_fault` | `val`: integer 0/1 (GPIO27) |
| `run_vector` | `trig`: 0/1, `pwm`: 0..1, `fault`: 0/1 |
| `assert_output` | `pin`: 22 or 23, `expected`: 0/1, `timeout_ms`: 0..5000 |

Unknown or extra fields, nonfinite numbers, booleans in integer fields and malformed
JSON are rejected. Example:

```json
{"cmd":"run_vector","trig":0,"pwm":0.9,"fault":1}
```

```json
{"pass":true,"latency_ms":0.03,"injected":{"trig":0,"pwm":0.9,"fault":1},"telemetry":{"state":"FAULT_SHUTDOWN","trig":0,"pwm":0.9,"fault":1,"actuator":0,"safety":0},"physical":{"22":0,"23":1}}
```

`pass` means harness execution succeeded (or physical assertion matched), **not**
that telemetry is truthful. The agent independently compares actuator ↔ BCM22 and
safety ↔ BCM23 and enforces fault shutdown. `ping` returns `pong:true` without a
measurement. Failures return `pass:false`, `error`, `latency_ms`; failed assertions
also include the last measured state. Assertion timeout is an observation deadline;
a serial read in progress may exceed it by up to 600 ms. Real read_state discards
queued serial data and waits up to 600 ms for fresh valid telemetry, failing closed
on timeout. Simulated reads are immediate and preserve the relay's previous state.

`run_vector` applies all three inputs before reading; real GPIO writes are sequential,
not electrically atomic. Telemetry and digital readback are sampled near each other,
not synchronously. Stable vectors are required. Serial startup logs and incomplete
packets are ignored until a valid JSON telemetry packet arrives. No arbitrary GPIO,
shell execution, firmware flashing or remote code execution command is exposed.
