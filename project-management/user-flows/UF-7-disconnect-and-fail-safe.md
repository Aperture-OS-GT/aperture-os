# UF-7 — Disconnect, watchdog and fail-safe

**Persona:** Tester / Rig admin · **Epics:** EPIC-01, EPIC-06 · **Stories:** US-03, US-12, US-46, US-47

## Scenario A — clean disconnect while driving

| # | Actor | Action | Result |
|---|---|---|---|
| 1 | Tester | GPIO18 is in `pwm` at 100 Hz, GPIO22 `output` high; clicks **Disconnect** | Confirm dialog: "2 pins are driving (GPIO18 PWM, GPIO22 = 1). Disconnecting will release them to high-impedance." `[Release & disconnect]` |
| 2 | Host | Sends `release_all`, `control.release`, closes transport | Thread: tool cards for both with 4 stages; pin dots on 12 and 15 go to `nc`; rig panel **Disconnected**; session ends `session.end {reason:"user"}` |

## Scenario B — link loss (walk away with the laptop)

| # | Actor | Result |
|---|---|---|
| 1 | BLE link drops | Bridge detects disconnect → `release_all` within 500 ms, clears controller, logs locally |
| 2 | Host | Pill red `Reconnecting…`; rig panel `Disconnected (link lost)`; thread row (warning style) "Link lost at 17:51:02 — bridge released all outputs"; all driving controls disabled |
| 3 | Host | Retries connect every 2 s for 60 s; on success starts a new session linked from the old one (`session.end {reason:"link_lost"}`); tester must **Take Control** again |

## Scenario C — host hangs (watchdog)

| # | Actor | Result |
|---|---|---|
| 1 | Host process is frozen; no `ping` for `watchdog_s` (5 s) | Bridge runs `release_all`, emits `control.lost {reason:"watchdog"}`, controller cleared; the BLE link may still be up |
| 2 | Host resumes | Receives `control.lost`; UI shows warning row "Control lost (watchdog) — outputs released"; state **Connected · observer**; `Take Control` re-enabled |

## Scenario D — another tester forces control

| # | Actor | Result |
|---|---|---|
| 1 | Tester B sends `control.take force:true` after A idle ≥ 30 s | Bridge: `release_all`, `control.changed {controller:"B", previous:"A", reason:"force"}` |
| 2 | A's UI | Warning row "Control taken by B"; state **observer**; still receives stream events and can watch |

## Bridge-side invariants (tested in `sim` mode)
- `release_all` is idempotent and is called from: controller disconnect, watchdog, `SIGTERM`, executor exception, force-take.
- After `release_all`, `list_pins` reports every GPIO `disabled`, no PWM, no open ports, no active capture, no streams.
- A kill of the client socket mid-PWM → `disabled` on all pins within 500 ms (automated test with TCP transport).

## Acceptance (UI)
- Rig panel state and pill reflect each scenario within one SSE tick.
- Warning rows are visually distinct from agent/bridge/pin rows.
