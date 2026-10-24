# 02 — Target architecture

## Package layout (host)

```
aperture/                      # new host package (pure Python ≥3.10, no hard deps)
  pins.py                      # PinMap: 40-pin J8 table, BCM<->physical, kinds, alt functions, capability flags
  protocol.py                  # protocol v1: message schemas, validation, CBOR/JSON codec, MTU chunker
  transport/
    base.py                    # Transport ABC: connect/disconnect/send/recv/events, manifest, control
    virtual.py                 # Virtual Rig transport (in-process, deterministic clock)
    tcp.py                     # JSON/CBOR over TCP (dev bench, Pi over Wi-Fi)
    ble.py                     # BLE central via bleak (bench-verify)
  rig/
    virtual.py                 # VirtualRig: pin state machine, PWM, UART/SPI/I2C models, capture, stream
    behaviors.py               # scriptable behaviors: Loopback, UartEcho, Button, Blinker, ScriptedDut (ESP32 safety)
  requests.py                  # Request + lifecycle (requested→acknowledged→executed→observed), ids, timing
  session.py                   # Session: monotonic clock, ordered event log, save/load, export JSON + report
  tools.py                     # Agent tool set (schemas + implementations) bound to a Session
  plans/
    schema.py                  # plan file format + validation
    runner.py                  # deterministic plan runner (no agent in loop)
    compile.py                 # NL → plan (rule-based offline + LLM-assisted)
  server.py                    # HTTP+SSE API for the UI (replaces ui/server.py)
  static/                      # tester UI (ported from looprig-ui)
pi/                            # bridge daemon (runs on the Pi)
  bridge/
    daemon.py                  # main: transports (BLE GATT via bless, TCP), control, watchdog
    gatt.py                    # service/characteristic definitions, chunk reassembly
    executor.py                # protocol v1 command execution against drivers
    drivers.py                 # gpiozero/lgpio, pyserial, spidev, smbus2 wrappers; fail-safe release
```

Legacy `pi/`, `agent/`, `ui/` stay until each story's "remove legacy" checkbox is done.

## Protocol v1

Transport-agnostic request/response + event stream. Encoded as **CBOR** on BLE (compact, binary-safe) and **JSON** on TCP/dev (same field names). Every message is a map.

### Envelope

```
request  : {"v":1, "id":<u32>, "op":<str>, ...args}
response : {"v":1, "id":<u32>, "ok":true, "t":<u64 µs mono>, ...result}
         | {"v":1, "id":<u32>, "ok":false, "t":<u64>, "err":<code>, "msg":<str>}
ack      : {"v":1, "id":<u32>, "ack":true, "t":<u64>}          # sent immediately on parse, before execution
event    : {"v":1, "ev":<str>, "t":<u64>, ...payload}          # unsolicited (stream mode, control changes)
```

`t` is the bridge's monotonic clock in microseconds. The host records its own monotonic receive time alongside; the session stores both and the estimated offset (from `ping`).

### Ops

| op | args | result | notes |
|---|---|---|---|
| `hello` | `client`, `proto:[1]` | `manifest` | first message; returns manifest (below) |
| `control.take` | `token?`, `force?` | `controller:true`, `token` | one controller at a time; `force` needs the current token or idle timeout |
| `control.release` | `token` | — | |
| `ping` | — | `t` | used for clock offset + watchdog keepalive |
| `pin.mode` | `pin`, `mode` ∈ `input|output|input_pullup|input_pulldown|pwm|disabled`, `initial?` | `pin`, `mode`, `level` | `disabled` = high-Z |
| `pin.write` | `pin`, `level` 0/1 | `level` | pin must be `output` |
| `pin.read` | `pin` | `level` | any mode except `disabled` |
| `pin.pulse` | `pin`, `level`, `us` | `edges:[[t,level],…]` | blocking ≤ 1 s; longer → `err:range` |
| `pin.pwm` | `pin`, `freq_hz`, `duty` 0..1 | `freq_hz`, `duty` | pin must be `pwm` |
| `uart.open` | `tx`, `rx`, `baud`, `fmt?`="8N1" | `port` | `uart.close port` |
| `uart.write` | `port`, `data:bytes` | `n` | |
| `uart.read` | `port`, `max?`, `timeout_ms?` | `data:bytes` | |
| `spi.xfer` | `bus`, `cs`, `data:bytes`, `hz?`, `mode?` | `data:bytes` | |
| `i2c.write` | `bus`, `addr`, `data:bytes` | `n` | |
| `i2c.read` | `bus`, `addr`, `n`, `reg?` | `data:bytes` | |
| `i2c.scan` | `bus` | `addrs:[int]` | |
| `capture.start` | `pins:[int]`, `rate_hz`, `duration_ms` | `capture_id` | result streamed as `capture.done` event: `edges:[[t,pin,level],…]`, `initial:{pin:level}` |
| `capture.cancel` | `capture_id` | — | |
| `stream.set` | `pins:[int]`, `on:bool` | — | emits `pin.change` events `{pin, level, t}` |
| `release_all` | — | — | all outputs → high-Z, PWM off, buses closed |

Error codes: `bad_request`, `unknown_op`, `unsupported` (per manifest), `not_controller`, `busy`, `range`, `pin_mode`, `io`, `timeout`.

### Manifest

```
{"bridge":"aperture-pi", "fw":"1.0.0", "proto":1, "board":"Raspberry Pi 4 Model B",
 "header":"J8-40", "pins":[{"bcm":17,"phys":11,"caps":["in","out","pwm"]}, …],
 "uart":[{"id":0,"tx":14,"rx":15}], "spi":[{"bus":0,"mosi":10,"miso":9,"sclk":11,"cs":[8,7]}],
 "i2c":[{"bus":1,"sda":2,"scl":3}], "limits":{"capture_hz":10000,"pulse_us":1000000,"mtu":244},
 "power":{"battery_pct":null,"vbus_ok":true}}
```

The Virtual Rig returns `bridge:"virtual-rig"` and an identical shape so the UI never special-cases.

### Chunking for BLE

GATT characteristics (service UUID `6e0a7e00-…`): `cmd` (write, chunked), `rsp` (notify, chunked), `evt` (notify, chunked). Chunk header: 1 byte `seq` (bit7 = last). Max payload = MTU−3−1. Reassembly resets on `seq=0`.

## Request lifecycle (host)

```
requested   host created the Request (tool call / UI action / plan step)
acknowledged bridge `ack` received (parse ok; or Virtual Rig instantly)
executed    bridge response received (ok or err)
observed    host confirmed the effect: pin.read / stream event / capture / uart match
```

Each stage is an **event** in the session (`kind:"request.stage"`) with host monotonic `t_host_us` and, when present, bridge `t_bridge_us`. Failures carry the error code. Timeouts move the request to `executed` with `err:"timeout"`.

## Session model

```
session.json
{ "schema":1, "id":"s_20261024_173301_3f2a", "created_at":"<iso>", "transport":{...}, "manifest":{...},
  "clock":{"host_mono0_us":…, "offset_us":…},
  "events":[ {"seq":1,"t_host_us":0,"kind":"session.start",…},
             {"seq":2,"kind":"request.stage","req":"r1","stage":"requested","op":"pin.write","args":{…},"actor":"agent"},
             {"seq":3,"kind":"request.stage","req":"r1","stage":"acknowledged","t_bridge_us":…},
             {"seq":4,"kind":"pin.change","pin":17,"level":1,"t_bridge_us":…,"source":"stream"},
             {"seq":5,"kind":"bus.bytes","dir":"rx","port":0,"data":"Qk9PVCBPSw==",…},
             {"seq":6,"kind":"note","actor":"agent","text":"…"},
             {"seq":7,"kind":"assertion","name":"…","pass":true,"detail":{…}} ] }
```

Replay = feeding `events` through the same reducer the live UI uses; no hardware needed. Export: `session.json` (above) and `report.md` (summary, pin table, assertions, timeline excerpt).

## Plan model

```
plan.json
{ "schema":1, "name":"boot ok", "source":"confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22; verify the ESP32 prints BOOT OK over UART",
  "requires":{"wiring":[{"pin":22,"dut":"BTN","dir":"out"},{"pin":17,"dut":"LED","dir":"in"}],"uart":[{"tx":14,"rx":15,"baud":115200}]},
  "steps":[ {"id":"s1","tool":"set_mode","args":{"pin":17,"mode":"input"}},
            {"id":"s2","tool":"set_mode","args":{"pin":22,"mode":"output","initial":0}},
            {"id":"s3","tool":"uart_open","args":{"tx":14,"rx":15,"baud":115200}},
            {"id":"s4","tool":"write","args":{"pin":22,"level":1}},
            {"id":"s5","tool":"wait_for","args":{"pin":17,"edge":"rising","timeout_ms":50},"assert":true},
            {"id":"s6","tool":"uart_expect","args":{"pattern":"BOOT OK","timeout_ms":2000},"assert":true} ],
  "cleanup":[{"tool":"release_all"}] }
```

Steps call tools by name with the same argument schema the agent sees. `assert:true` steps decide pass/fail. The runner is deterministic: no LLM, fixed ordering, per-step timeouts, result saved as a session.

## UI data flow

`aperture/server.py` exposes `GET /api/state`, `GET /api/events` (SSE, replay by `Last-Event-ID`), `POST /api/connect|disconnect|control`, `POST /api/request` (any tool), `POST /api/prompt`, `GET/POST /api/sessions`, `GET/POST /api/plans`, `POST /api/plans/{id}/run`, `GET /api/sessions/{id}/export.{json,md}`. The frontend keeps one reducer over session events; live and replay use the same code path.
