# Aperture GPIO protocol and console API

## Topology and numbering

Laptop/server ↔ Raspberry Pi uses Linux Bluetooth RFCOMM (TCP fallback). Pi ↔ DUT
uses **only header pins**, with shared GND and 3.3V logic. No DUT USB data link.
BCM numbers identify GPIOs in commands; physical J8 positions are metadata.
Physical 27/28 are reserved ID_SD/ID_SC, not general GPIOs. The full 40-pin map
lives in `pi/header.py`. Power and ground pins cannot be configured or requested.

## Generic daemon commands

All return `pass` and `latency_ms`; errors return `pass:false,error`. `pass` means
execution succeeded, **not that the hardware passed its profile**. See framing and
legacy compatibility below. Unknown, extra and missing fields are rejected.

| cmd | Fields | Response fields beyond pass/latency |
|---|---|---|
| `list_pins` | — | `pins`: full 40-pin snapshot |
| `select_profile` | `profile`: esp32_safety/mpu6050_gyro/display | `profile`; releases old GPIO/bus handles |
| `configure_pin` | `pin`: BCM, `direction`: tx/rx; optional `pull`: none/up/down | `pins` |
| `write` | `pin`, `val`: integer 0/1 | `pins` |
| `pwm` | `pin`, `duty`: 0..1, `freq`: 1..10000 Hz | `pins` including duty |
| `read` | `pins`: nonempty unique BCM array | `pins` with optional decoded bus data |
| `sample` | `pins`, `duration_ms`: 1..2000, `rate_hz`: 1..1000 | `samples:[{t_ms,pins}]`, final `pins` |
| `release` | `pin` | `pins` with null direction; stops PWM, closes GPIO device |

Response pin entries use `{pin:BCM,direction,value,duty?,decoded?}`. `list_pins`
instead uses header objects below. Read/sample require configured pins; write/PWM
additionally require tx. Direction `tx` means Pi output, `rx` means Pi input.
Bus pins are peripheral-owned, never bit-banged by write/PWM. On I2C, tx means
Pi bus ownership, not push-pull drive: SDA is bidirectional. Reads of SDA/SCL
produce `{who_am_i,wx,wy,wz,unit}` using smbus2, bus 1/address 0x68. ESP32 reads
of GPIO15 decode UART JSON at 115200 from `/dev/serial0`, not USB serial.

Sampling is bounded to **200 pin samples** (duration*rate/1000*pin_count), with
at least one frame, floor(duration*rate/1000) frames otherwise. Simulated t_ms
is deterministic; real reads use monotonic timestamps and best-effort software
timing. Bus reads may reduce achieved rate; this is not an oscilloscope or logic
analyzer. Per-pin history retains the last 512 observations in server memory.

## Console HTTP API

Local trusted lab use only. JSON bodies; concurrent writes return HTTP 409;
validation errors 400; transport/SDK errors 502. Completed tests and waiting
reports use HTTP 200 with their own `pass` verdict.

### GET

* `/api/header` → `{pins:[40 header objects]}`.
* `/api/requests` → `{requests:[{pi_pin,dut_pin,component,reason}]}` pending only.
* `/api/profiles` → `{profiles:[full JSON profiles],selected:status|null}`.
* `/api/waveform?pin=24` → `{pi_pin:24,samples:[{timestamp,value,duty?,decoded?}]}`.
  Fetches retained history, not a new capture. Use `/api/gpio` to sample.
* `/api/state` → legacy connection/measurement/LLM fields plus `profile:status|null`.
* `/api/report` → latest report or null.

Header object example:
```json
{"physical":18,"bcm":24,"kind":"GPIO","connected":false,"dut_pin":null,"label":null,"direction":"rx","value":0,"request":{"dut_pin":"INT","component":"MPU6050 gyroscope","reason":"Required for mpu6050_gyro: Data ready"}}
```
Power/GND/ID entries have bcm:null and kind 3V3/5V/GND/ID_SD/ID_SC. Confirmed
connections set connected:true, dut_pin and label; request becomes null. Optional
duty and decoded are the latest measurement. Bus values are not bitstream captures.
Pending direction is the intended direction, **not** an actively configured GPIO.

### POST

* `/api/profiles/select {"profile":"mpu6050_gyro"}` → status
  `{profile:full_profile,missing:[BCM...],ready:false}`. Releases previous GPIOs,
  clears server header/history and queues required wires.
* `/api/requests {"pi_pin":24,"dut_pin":"INT","component":"MPU6050 gyroscope","reason":"data ready"}`
  → updated header pin. Releases previous drive and queues a new human request.
* `/api/requests/confirm {"pi_pin":24}` or `/api/requests/reject` with same body
  → updated pin. Confirmation is **human metadata, not continuity detection**.
  Neither LLM tools nor profile selection confirms. Reject clears pending state;
  a rejected required wire must be requested again before testing can proceed.
* `/api/profiles/run {}` → `{profile,status,pass,steps}`. Missing wires return
  `{status:"waiting_for_connections",pass:false,missing:[...],steps:[]}` without
  configuring or driving hardware. Confirm all required wires and call again.
* `/api/gpio {"cmd":"configure_pin","pin":24,"direction":"rx"}` configures a
  confirmed custom wire. Selected profile directions cannot be overridden.
* `/api/gpio {"cmd":"read","pins":[24]}`;
  `{"cmd":"write","pin":17,"val":1}`;
  `{"cmd":"sample","pins":[24],"duration_ms":200,"rate_hz":50}`.
  Requires confirmed wires; selected profiles additionally require all required
  wires. Returns daemon response plus `issues`.
* `/api/prompt {"prompt":"test the gyroscope"}` selects/requests the profile,
  or runs it if already selected and ready. `connect GPIO17 to ESP32 GPIO4` queues
  a manual request; `read pin 24` reads a confirmed pin. With OPENAI_API_KEY the
  prompt goes through the existing LLM loop with select_profile, request_connection, configure_pin,
  read_pins, write_pin, sample_pins and run_profile tools. Only HTTP human endpoints
  confirm or reject wiring.
* `/api/connect {"mode":"sim|tcp|rfcomm","host":"127.0.0.1","port":8765,"demo_bug":true}`
  reconnects, invalidates confirmations and re-requests selected profile wires.
  CLI `--profile` preselects a profile. `--daemon-port` is the Bluetooth channel
  in RFCOMM mode, TCP port otherwise.

Compatibility: with **no profile selected**, old ESP32 prompt chips/demo still
work ungated for the old UI. This explicit legacy bench mode is not the universal
workflow. Select a profile or start `--profile ...` for wiring gating. The old UI
artwork is ESP32-specific; redesign is a separate handoff.

### SSE event shapes

`GET /api/events`: `id:N` and `data:<JSON>`, with heartbeat comments and
Last-Event-ID replay from a bounded 1,000-event ring. Envelope:
```json
{"id":42,"timestamp":"2026-01-01T12:00:00+00:00","level":"REQUEST","data":{"pi_pin":24,"status":"confirmed","request":{"dut_pin":"INT","component":"MPU6050 gyroscope","reason":"data ready"}},"latency_ms":null}
```
* `PIN_STATE`: data `{pins:[full 40-pin snapshot]}` after request/confirm/configure/
  read/sample; sample history is updated before the final state event.
* `REQUEST`: data `{pi_pin,status:pending|confirmed|rejected,request:{dut_pin,component,reason}}`.
* `PROFILE`: data `{profile,missing,ready}` after selection.
* `STEP`: `{step,reasoning,injected,latency_ms,pass,issues,...evidence}`.
* `REPORT`: `{profile,mode,status,pass,steps}` or waiting report with `missing`.
* `USER`/`AGENT`: `{reply:text}`; `ERROR`: `{error:text}`.
* `TX`: raw command, envelope latency null; `RX`: raw response with measured
  round-trip envelope latency_ms. Samples/decoded evidence are included in RX.

State, reports and events are in-memory only and reset on restart. Header wiring
metadata belongs to the server; daemon list_pins contains GPIO configuration/value
but does not assert a human has actually connected a wire.

## Legacy ESP32 protocol and shared framing

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
packets are ignored until a valid JSON telemetry packet arrives. No shell execution,
firmware flashing or remote code execution command is exposed.
