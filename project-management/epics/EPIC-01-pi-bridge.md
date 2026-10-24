# EPIC-01 — Pi bridge daemon

**Component:** Raspberry Pi · **Owner persona:** Rig admin / Tester · **Status:** todo

The Pi is a dumb, trusted I/O bridge. It advertises a BLE GATT service, accepts one controller at a time, executes protocol v1 commands against the 40-pin header, streams pin state, and fails safe. See `02-architecture.md` for the protocol.

## Stories

### US-01 — Advertise a BLE GATT service · P0 · todo
*As a tester I want the Pi to show up as a BLE peripheral so my laptop can connect without cables or Wi-Fi.*
- Given the daemon starts with `--transport ble`, when I scan from the host, then I see a device named `aperture-<hostname>` advertising service UUID `6e0a7e00-4150-4552-5455-524500000001`.
- Given BlueZ is unavailable (dev box), when I start with `--transport tcp`, then the same executor serves protocol v1 over TCP (JSON) on `--port` (default 8765).
- Characteristics: `cmd` (write-without-response + write), `rsp` (notify), `evt` (notify).
- `bench-verify`: connect from macOS with `bleak`.

### US-02 — Host connects as BLE central · P0 · todo
*As Aperture OS I want to connect, negotiate MTU, and exchange `hello`/manifest within 2 s.*
- Given the Pi is advertising, when the host connects and sends `hello`, then it receives the manifest in ≤ 2 s and the UI shows bridge name + firmware.
- Given the host disconnects (cleanly or by range loss), when the link drops, then the bridge executes `release_all` within 500 ms (US-12).

### US-03 — Explicit take-control handshake · P0 · todo
*As a rig admin I want only one controller at a time so two testers can't fight over the rig.*
- Given no controller, when client A sends `control.take`, then A gets `controller:true` + token and the bridge emits `control.changed {controller:"A"}`.
- Given A controls, when B sends `control.take` without `force`, then B gets `err:busy` with `controller:"A"`, and B can still issue read-only ops (`hello`, `ping`, `pin.read`, `stream.set`).
- Given A controls, when B sends `control.take force:true`, then it succeeds only if A has not pinged for ≥ `idle_s` (default 30) — otherwise `err:busy`.
- Given A controls, when A disconnects, then control is released and outputs go high-Z.
- Driving ops (`pin.mode`, `pin.write`, `pin.pulse`, `pin.pwm`, `uart.*`, `spi.*`, `i2c.write`, `capture.start`, `release_all`) without control → `err:not_controller`.

### US-04 — Versioned, chunked command protocol · P0 · todo
*As a developer I want one versioned protocol so host and bridge can evolve independently.*
- Every message carries `v:1`; unknown `v` → `err:bad_request` with `supported:[1]`.
- CBOR on BLE, JSON on TCP; identical field names; byte fields are CBOR byte strings / JSON base64.
- Chunker: 1-byte header (`seq` 0–127, bit7 = last); payloads up to MTU−4; reassembly resets on `seq=0`; out-of-order → drop + `err:bad_request`.
- An `ack` is sent as soon as a request parses, before execution; `id` echoes back on `ack` and response.
- Unit tests: round-trip every op through codec + chunker at MTU 23, 185, 247, 512.

### US-05 — Pin mode, digital write/read · P0 · todo
- Modes: `input`, `output`, `input_pullup`, `input_pulldown`, `pwm`, `disabled` (high-Z). `pin.mode` on a non-GPIO physical pin → `err:bad_request`.
- `pin.write` on a pin not in `output` → `err:pin_mode`. `pin.read` works in any mode but `disabled`.
- `pin.mode … initial` sets the level atomically with the mode change (no glitch to the default level).
- Response includes the readback level after the operation.

### US-06 — Pulse and PWM · P0 · todo
- `pin.pulse pin level us`: drives `level` for `us` µs then returns to the previous level; result `edges` has the two measured timestamps. `us > limits.pulse_us` → `err:range`.
- `pin.pwm pin freq_hz duty`: 1 Hz – `limits.pwm_hz`, duty 0–1; pin must be in `pwm` mode; `duty:0` stops output.
- Jitter is reported in the result (`jitter_us`) so plans can use a tolerance.

### US-07 — UART transmit/receive · P1 · todo
- `uart.open tx rx baud fmt` returns `port`; opening pins already in another mode → `err:pin_mode`.
- `uart.write` returns bytes written; `uart.read max timeout_ms` returns what arrived (may be empty).
- In stream mode, received bytes are also pushed as `uart.rx {port, data, t}` events so the session gets every byte with a timestamp.
- Supported ports from manifest (`/dev/serial0` on Pi 4: tx 14 / rx 15).

### US-08 — SPI and I2C · P1 · todo
- `spi.xfer bus cs data hz mode` full-duplex; returns read bytes of equal length.
- `i2c.write bus addr data`, `i2c.read bus addr n reg?`, `i2c.scan bus`.
- Any bus op on a bus not in the manifest → `err:unsupported`.
- Real drivers: `spidev`, `smbus2`; both lazy-imported so the daemon runs on a box without them (`err:unsupported`).

### US-09 — Waveform capture → edge list · P0 · todo
- `capture.start pins rate_hz duration_ms` → `capture_id`; on completion emits `capture.done {capture_id, initial:{pin:level}, edges:[[t,pin,level],…], rate_hz_actual}`.
- `rate_hz > limits.capture_hz` → `err:range`; more than one concurrent capture → `err:busy`.
- `capture.cancel` emits `capture.done` with `cancelled:true` and the edges so far.
- Implementation: dedicated thread; on Pi uses `lgpio` edge callbacks when available, else polling at `rate_hz`.

### US-10 — Stream mode · P0 · todo
- `stream.set pins on` toggles `pin.change {pin, level, t}` events for those pins (debounced to `rate_hz` ≥ 1 kHz on Pi).
- Streaming never blocks command handling (separate notify queue; drop-oldest with `stream.overflow` event if the queue exceeds 1000).
- Observers (non-controllers) may subscribe.

### US-11 — Capability manifest · P0 · todo
- `hello` returns the manifest described in `02-architecture.md`; `pins[].caps` reflect the actual board (PWM-capable: 12, 13, 18, 19 hardware + software PWM everywhere flagged `pwm_sw`).
- `limits` reflect the backend (`capture_hz`, `pulse_us`, `pwm_hz`, `mtu`).
- `power` is `null`-filled when unknown; populated from `vcgencmd get_throttled` (undervoltage bit) when available.
- UI renders the header from the manifest, not from a hard-coded table.

### US-12 — Fail safe on disconnect / watchdog · P0 · todo
*As a tester I want the rig to never leave a pin driving the DUT when nobody is in control.*
- Watchdog: if the controller sends no message for `watchdog_s` (default 5; set in `control.take`), the bridge runs `release_all`, emits `control.lost {reason:"watchdog"}` and clears the controller.
- On transport disconnect of the controller: `release_all` within 500 ms.
- On daemon shutdown (SIGTERM) and on uncaught exception in the executor: `release_all`.
- `release_all`: every GPIO → `disabled` (input, no pull), PWM stopped, UART/SPI/I2C handles closed, capture cancelled, streams cleared.
- Test: kill the client mid-PWM in `sim` mode → all sim pins report `disabled` within 500 ms.

## Non-functional
- Daemon runs as a `systemd` service; installs via `scripts/pi-install.sh` (US-62).
- No Python deps required for TCP mode; `bless`, `cbor2`, `lgpio`, `pyserial`, `spidev`, `smbus2` optional extras `[pi]`.
