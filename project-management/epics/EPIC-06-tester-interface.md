# EPIC-06 — Tester interface

**Component:** UI (`aperture/static`) · **Design reference:** `looprig-ui/app.html`, `app.css`, `app.js` · **Status:** todo

The thing on the big screen. Layout left → right: **Rig panel** · **Current session** · **Pin map**. The `looprig-ui` prototype places sessions on the left, agent chat in the centre, and the rig/pins on the right; we keep its visual system (light theme, Inter + mono, 44 px top bar, card styles, pin dots) and reconcile the two layouts as below.

## Layout decision

| Region | Brief says | Prototype has | Decision |
|---|---|---|---|
| Top bar | — | brand, Bluetooth pill, Reset device, Record, avatar | Keep. Bluetooth pill becomes the **connection pill** (Virtual Rig / BLE name / TCP host) and opens the Rig panel popover |
| Left | Rig panel | Sessions list | **Left column = Rig panel on top (status, bridge, firmware, power, Connect / Disconnect / Take Control) + Sessions list below** |
| Centre | Current session (filterable log) | Agent chat + composer | **Centre = Current session**: chat-style thread where agent messages, tool cards (with 4 lifecycle stages), bridge acks, and spontaneous pin changes are distinct rows; filter chips at top; composer at bottom |
| Right | Pin map | Header board + pending requests + RX/TX lists | Keep prototype: to-scale header, legend, pending wiring requests, receiving/transmitting lists with mini-waveforms |

## Stories

### US-45 — Three-column shell and visual system · P0 · todo
- Grid `204px minmax(520px,1fr) 382px`, rows `44px 1fr`; light palette, tokens from `app.css` (`--bg #f6f5f1`, `--ink`, `--line`, `--green`, `--mono`).
- Responsive: ≤ 1150 px narrows columns; ≤ 850 px stacks pin map under the session.
- Served by `aperture/server.py` at `/`; no build step (plain HTML/CSS/JS, ES2020).

### US-46 — Rig panel: status, bridge, firmware, power · P0 · todo
- Shows connection state (`disconnected / connecting / connected / controller / observer / replay`), bridge name, firmware, board, transport, power (`battery %` / `VBUS ok` / `—`), MTU, clock offset.
- When no Pi is reachable it reads **"Virtual Rig"** and everything keeps working.

### US-47 — Connect / Disconnect / Take Control · P0 · todo
- Connect opens a sheet: transport (`Virtual Rig` with scenario picker · `Bluetooth` with scan list · `TCP` host:port); Disconnect confirms if the rig has outputs driving.
- Take Control shows the current controller; `Force` is offered only when the controller is idle; result reflects `control.changed`.
- All buttons disabled in replay mode.

### US-48 — Current session: event rows · P0 · todo
- Row types with distinct styling: **tester message** (right-aligned bubble), **agent message** (mark + body), **tool card** (icon, name, args mono, stage chips `requested · acked · executed · observed` lighting up in order with Δt), **bridge ack** (small grey system row), **spontaneous pin change** (amber dot row `GPIO17 ↑ 1` with time), **bus bytes** (mono transcript row), **assertion** (green/red card), **wiring request** (yellow card with Deny/Allow), **control change**, **warning**.
- Rows render from session events through one reducer shared with replay.

### US-49 — Filterable, scrolling log · P0 · todo
- Filter chips: `All · Agent · Tools · Bridge · Pins · Bus · Assertions · Warnings`; text search; `Pause` autoscroll when the user scrolls up; `Jump to latest`.
- Keeps ≥ 10 000 rows smooth (virtualised list or windowed rendering).

### US-50 — Composer and suggested prompts · P0 · todo
- Textarea with Enter-to-send, Shift+Enter newline; chips for the demo prompts (US-28); disabled while a request is in flight unless `Allow concurrent` is on.
- Shows "Agent: thinking…" dots while the agent runs.

### US-51 — Pin map: to-scale 40-pin header from the manifest · P0 · todo
- 2 × 20 grid, 2.54 mm pitch rendering (66 px board width in the prototype → 1 pitch ≈ 20 px); physical numbers 1–40, BCM labels and alt functions beside each pin; power/ground/reserved pins styled as in `app.css` (`pwr`, `gnd`, `v5`, `v3`).
- Pin dot states: `nc`, `conn` (low), `high`, `req` (pulsing), `contention`; selected pin outlined.
- Header is built from the manifest `pins[]`, so a different board renders correctly.

### US-52 — Pin inspector: receiving / transmitting lists · P0 · todo
- Each active pin gets a card: name, `GPIOx · pin n · DUT label`, live value text (`sending 1 · 3.3 V`, `steady 0`, `2.0 Hz · 50 % high`, decoded UART line), mini waveform canvas (last 1–10 s), controls (`Pulse 100 ms`, level toggle, `Use as receive/transmit`, `Disconnect`, decoder select, `Record` / `Export CSV`).
- Values come from stream events and captures, not from a local simulation.

### US-53 — Pending wiring requests · P0 · todo
- Cards at the top of the pin inspector mirror `wiring.request` events: "Transmit on GPIO17?" with pin/DUT/component/reason, Deny / Allow (US-30); the pin dot pulses `req`.

### US-54 — Sessions sidebar · P0 · todo
- `New session`, current session, recent sessions (from `/api/sessions`), verdict dot; selecting a past session enters replay (US-35).

### US-55 — Replay controls · P0 · todo
- Timeline scrubber, play/pause, speed `0.5× 1× 4× ⏭`, current `t`; banner `Replay · <bridge> · <date>`; `Exit replay`.

### US-56 — Export buttons · P0 · todo
- Session menu: `Export JSON`, `Export report (.md)`, `Copy share text`; per-pin `Export CSV` from recordings (US-52).

### US-57 — Plans in the UI · P1 · todo
- `Plans` sheet: list, run, view steps JSON, duplicate, delete; a compiled plan appears in the thread as a card with `Run` / `Save` before first execution (UF-3).

### US-58 — Keyboard, a11y, reduced motion · P2 · todo
- All controls reachable by keyboard; pin buttons have `aria-label` with physical, BCM and state; `prefers-reduced-motion` disables pulse/blink; colour never the only signal (icons/text for state).

## Legacy
- `ui/static` and `ui/server.py` are removed when US-45–US-56 are merged; `tests/test_ui.py` is replaced by `tests/test_server.py`.
