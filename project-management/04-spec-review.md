# 04 — Spec review: issues found while building the bridge

Findings from implementing the Pi bridge (EPIC-01) and TCP transport (US-16) against
`02-architecture.md` and the host core in `aperture/` (commit `b5efa20`). Each item has
evidence, the stories it affects, and a proposed resolution. **Status** is `open`
(needs a decision), `resolved-in-bridge` (handled in `pi/bridge`, spec should say so),
or `proposed` (a concrete change is offered; adopt by editing the referenced story).

Reproductions below use only the Virtual Rig at `b5efa20`; no hardware is needed.

## R-1 — Control is not bound to a connection · high · resolved-in-bridge

Protocol v1 driving ops carry no token, so a `VirtualRig` shared by two clients accepts
`pin.write` from whichever client sends it once *anyone* holds control. Any client's
`ping` also refreshes the controller's watchdog, and the controller is named after the
most recent `hello` from any client (two `VirtualTransport`s on one rig: A takes control,
B's `busy` error reports `controller: "b"`).

* **Bridge:** `pi/bridge/server.py` binds control to the connection that took it: other
  connections get `not_controller` (with `controller`) for driving ops, observer pings are
  answered without touching the watchdog, and the controller is named after its own hello.
* **Spec change (US-03):** add "driving ops from a connection that does not hold control
  → `err:not_controller` even while another client holds control" and "only the
  controller's messages refresh the watchdog". For BLE, where the peripheral may not see
  which central wrote (see R-8), either allow one central at a time or add `token` to
  driving ops.

## R-2 — Observers can consume or disturb controller state · medium · resolved-in-bridge

`uart.read` drains the shared RX buffer and `i2c.read`/`i2c.scan` put traffic on the bus,
but none is in `DRIVING_OPS`, so an observer can steal the bytes a controller's
`uart_expect` is waiting for or interleave transactions with a test.

* **Bridge:** while a controller exists these are controller-only (`CONTROLLER_ONLY_READS`).
* **Proposed:** list them in the protocol as controller-only and document that observers
  receive UART bytes through `uart.rx` events instead (US-07, US-08, US-10).

## R-3 — `wait_for` can miss an edge that precedes the call · high · proposed

US-26 resolves `wait_for` on the first matching stream event *after* the call. On real
hardware the DUT reacts in microseconds (the ESP32 demo LED follows the button in < 1 ms)
while a host round trip is 10–50 ms over BLE, so for "GPIO17 goes high within 50 ms of
pressing the button on GPIO22" the edge has usually arrived before `wait_for` starts and
the plan fails on the bench while passing on the Virtual Rig (12 ms scripted delay, zero
transport latency).

* **Proposed (US-26, US-39, US-40):** stream must be enabled on observed pins before the
  stimulus (the runner enables it for every `in` pin in `requires.wiring` at plan start);
  `wait_for` takes `since` (default: bridge `t` of the previous driving step's response)
  and matches edges with `t_bridge_us >= since`, including ones already received; latency
  is reported as `edge.t - since`. Add a Virtual Rig test with `delay_us=0` and injected
  transport latency to keep this from regressing.

## R-4 — `capture.done` does not fit the transports · high · open

Measured on `b5efa20` (Virtual Rig, `pin.pwm 18 1000 Hz 0.5`, then `capture.start`): a 2 s capture at 10 kHz of a
1 kHz PWM is 3 999 edges = **57 917 B** as a JSON line (limit 65 536; 3 s exceeds it) and
**31 837 B** as CBOR, which the BLE chunker **refuses at MTU 23, 185 and 247** ("more than
128 chunks"); only MTU 517 fits (63 chunks). The bridge currently degrades an oversized
TCP event to `rig.warning {warning:"event_too_large"}` — the capture is lost.

Options: (a) stream captures as `capture.data {capture_id, part, edges}` events plus a
final `capture.done` summary; (b) widen the chunk header to a 16-bit sequence;
(c) cap edges per capture with a `truncated` flag. (a) also keeps the event queue
responsive. Needs a decision before US-09/US-24 ship.

## R-5 — The watchdog fires during the controller's own long request · high · proposed

`VirtualRig` measures watchdog idle time while the controller's request is still running.
Reproduced: watchdog 1 s, `uart.read timeout_ms=3000` → `control.lost {reason:"watchdog"}`
and every output (including the UART being read) is released mid-request. The same applies
to `pin.pulse` near `limits.pulse_us`. The host cannot ping because its transport
serialises requests.

* **Proposed (US-12):** the watchdog is suspended while a request from the controller is
  executing and restarts when its response is sent.

## R-6 — `force` take is unreachable while the controller's host is alive · medium · proposed

`idle` is measured from the controller's last message, and the transport keepalive (US-15:
ping every `watchdog_s/2`) counts. Reproduced: after 40 s of nothing but keepalive pings,
`control.take force:true` from B is `busy` with `idle_s: 0.0`. UF-7 scenario D ("force
after A idle ≥ 30 s") can never happen unless A's host freezes — and then the watchdog
already released control after 5 s.

* **Proposed (US-03, UF-7 D):** track `last_active` (last *driving* op) separately from
  `seen` (any controller message); watchdog uses `seen`, `force` uses `last_active`.

## R-7 — Byte fields change type with the transport · medium · proposed

JSON transports (Virtual, TCP) return byte fields as base64 `str`; CBOR (BLE) would return
`bytes`. Tools written against the Virtual Rig would break on BLE.

* **Proposed (US-13):** `Transport.request` normalises byte fields named in the op schemas
  (`data`) to one type for every transport — decoding base64 to `bytes` is the least
  surprising — so tools never see the wire encoding.

## R-8 — BLE peripheral facts the stories assume · high · open (to verify on a bench)

US-01/US-02/US-03 assume the bridge can tell centrals apart, detect a disconnect within
500 ms and send a response to one central. A Python GATT server (`bless` on BlueZ) is expected to deliver
writes without per-central identity in the common path and notifications go to every
subscribed central, so two centrals' request ids can collide in responses.
Disconnects are visible only through BlueZ D-Bus (`org.bluez.Device1.Connected`).

* **Proposed:** allow one connected central at a time (stop advertising while connected,
  reject or ignore others) unless requests carry a client nonce echoed in responses; detect
  disconnect via `Device1.Connected`; keep the watchdog as the guaranteed bound.
  Details land with the BLE PR.

## R-9 — Pi 4 header UART vs Bluetooth · medium · proposed

On Pi 3/4/Zero W the PL011 UART serves Bluetooth, so `/dev/serial0` on GPIO14/15 is the
mini-UART, whose baud follows the core clock (`enable_uart=1` pins it). `dtoverlay=
disable-bt` is not an option because BLE is the transport; `miniuart-bt` swaps them at the
cost of BLE throughput. Pi 5's header UART is independent of Bluetooth.

* **Proposed (US-07, US-62):** the install script sets `enable_uart=1`, disables the serial
  console, keeps BT on PL011, and the manifest reports the UART device and that baud
  accuracy depends on it.

## R-10 — Boot banners differ between Virtual Rig and hardware · medium · proposed

`UartEcho`/`ScriptedDut` print `BOOT OK` on `uart.open`; a real DUT prints once per boot,
usually before the host opened the port. The reference plan passes virtually and fails on
the bench unless it resets the DUT after opening the UART.

* **Proposed (US-40, US-69):** a wiring role `reset` (active-low EN line); the compiler
  inserts `pulse <reset> level 0 us 20000` after `uart_open` when the sentence checks a boot
  message and a reset line is wired.

## R-11 — A realtime Virtual Rig only advances when pumped · low · resolved-in-bridge

`VirtualRig` fires scheduled behaviors, capture ends and the watchdog from `pump()`, which
only runs inside `execute`. Served in realtime with no requests, a capture never completes
and a dead controller never times out.

* **Bridge:** `BridgeServer` pumps every 1 ms. **Proposed (US-45):** the UI server does the same.

## R-12 — One failing event callback starves the rest · low · proposed

`Transport._event` stops at the first callback that raises, so the session recorder could
miss events because the UI callback failed. `TcpTransport` isolates callbacks and logs;
the base class should do the same (US-13).

## R-13 — Messages too large for the wire are fatal on JSON lines · low · resolved-in-bridge

`encode_json` raises past 64 KiB. The TCP bridge turns an oversized reply into
`err:range` and an oversized event into `rig.warning`; `TcpTransport` returns
`err:bad_request` locally for an oversized request and keeps the link. Spec this in US-04.
