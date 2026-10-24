# UF-9 — Capture a waveform and assert its timing

**Persona:** Tester + Agent · **Epics:** EPIC-01, EPIC-03, EPIC-05, EPIC-06 · **Stories:** US-09, US-24, US-25, US-38, US-52 · **Review items:** R-3, R-4

## Preconditions
- Controller on a rig (UF-1). Virtual Rig `button_led` (blinker on GPIO24 at 2 Hz) or the
  ESP32 demo DUT (`firmware/esp32-hil-demo`, heartbeat on GPIO24).

## Flow

| # | Actor | Action | UI | Events |
|---|---|---|---|---|
| 1 | Tester | `capture GPIO24 for 2 seconds and check it blinks at 2 Hz` | Bubble | `note actor:tester` |
| 2 | Agent | `set_mode(24, input)` (wiring-gated, UF-2) then `capture(pins=[24], duration_ms=2000, rate_hz=1000)` | Tool card, stages `requested · acked · executed`; `observed` waits for `capture.done` | `request.stage …` |
| 3 | Bridge | Samples for 2 s, emits `capture.done {initial, edges, rate_hz_actual}` | Card turns `observed`; GPIO24 card in **Receiving** shows the captured trace and `2.0 Hz · 50 % high` | `capture.done` |
| 4 | Agent | `assert_timeline` on the period (or a frequency assertion) with tolerance `≥ 2 / rate_hz` plus the manifest jitter | Green assertion card "period 500.1 ms (± 2 ms)" | `assertion pass:true` |
| 5 | Tester | Saves as a plan (UF-3) | Plan card lists `capture` + assertion with the tolerance used | `plan.compiled` |

## Limits the tester sees
- `rate_hz` above `manifest.limits.capture_hz` is rejected before sending (`err:range`).
- Captures are best-effort software sampling: the tolerance on any timing assertion must be
  at least one sample period plus the reported jitter, or the plan validator warns.
- Large captures (many edges) are bounded by the transport (R-4): until the chunked
  capture format lands, the UI warns when `duration_ms × expected edge rate` approaches the
  limit and suggests a lower `rate_hz` or a shorter window.

## Acceptance
- The same plan passes on the Virtual Rig and on the ESP32 demo DUT.
- Replaying the session (UF-5) redraws the identical trace from the stored `capture.done`.
