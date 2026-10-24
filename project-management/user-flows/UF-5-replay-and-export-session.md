# UF-5 — Replay and export a session

**Persona:** Tester / Firmware developer · **Epics:** EPIC-04, EPIC-06 · **Stories:** US-34, US-35, US-36, US-37, US-54, US-55, US-56

## Replay

| # | Tester does | System |
|---|---|---|
| 1 | Clicks a past session in the sidebar (`Boot OK · 17:42 · PASS`) | UI enters **replay mode**: banner `Replay · aperture-rig01 · 24 Oct 17:42`, rig panel shows the stored manifest (greyed), Connect/Take Control disabled, composer hidden, pin map rebuilt from the stored manifest at `t=0` |
| 2 | Presses **Play** | Events are fed through the same reducer on their original `t_host_us` timeline: thread rows appear, stage chips light up with real Δt, pin dots change, mini waveforms redraw from `pin.change` events; speed `1×` |
| 3 | Drags the scrubber to `seq 41` | State is recomputed from `seq 1..41` (reducer is pure); everything after is hidden |
| 4 | Switches speed to `⏭ instant` | Full session rendered; verdict visible |
| 5 | Clicks **Exit replay** | Back to the live session (or empty state if none) |

No transport is touched during replay; the Virtual Rig is not started. Works on a laptop with no Pi and no network.

## Export

| Action | Result |
|---|---|
| Session menu → **Export JSON** | Downloads `session-s_20261024_174201_3f2a.json` (`schema:1`, full events) |
| Session menu → **Export report** | Downloads `report-….md`: header, verdict, wiring table, assertions, request table with stage Δt, pin activity, UART transcript, warnings |
| Pin card → **Export CSV** | `gpio17-LED.csv` with `t_s,level` from the stored `pin.change` events (replay) or recording (live) |
| CLI | `aperture sessions export s_… --json --md -o out/` |

## Acceptance
- Loading an exported JSON back (`aperture sessions import`) and exporting again produces identical bytes.
- Report snapshot test on a Virtual Rig session is stable across runs.
- Replay of the UF-3 session shows the amber `GPIO17 ↑ 1` row at the same relative time as live.
