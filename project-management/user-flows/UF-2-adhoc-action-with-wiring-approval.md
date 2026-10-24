# UF-2 — Ad-hoc action with wiring approval

**Persona:** Tester + Agent · **Epics:** EPIC-03, EPIC-06 · **Stories:** US-22, US-26, US-29, US-30, US-48, US-51, US-52, US-53

Tester asks the agent to do something that needs a pin that has not been marked as wired yet.

## Preconditions
- UF-1 completed; tester is Controller on a rig (virtual scenario `button_led` or real ESP32).

## Flow

| # | Actor | Action | UI | Events |
|---|---|---|---|---|
| 1 | Tester | Types `press the button on GPIO22 and tell me if GPIO17 goes high` | Right-aligned bubble | `note actor:tester` |
| 2 | Agent | Plans: `set_mode(17,input)`, `set_mode(22,output,initial=1)`, `wait_for(17,rising,200ms)`, `write(22,0)`, `write(22,1)` | "thinking…" dots | — |
| 3 | Agent | `set_mode(17, input)` — GPIO17 has no confirmed wiring, policy `ask` | Tool card appears with stage `requested`; then a **wiring request card**: "Receive from GPIO17? pin 11 · DUT LED · reason: observe LED output · 3.3 V logic only" with `Deny` / `Allow`; pin 11 dot pulses yellow; pin inspector shows the same card at the top | `request.stage set_mode requested`, `request.stage … executed err:wiring_required`, `wiring.request pin:17` |
| 4 | Tester | Checks the physical wire, clicks **Allow** | Card turns grey "marked as wired by austin"; pin 11 dot turns solid; GPIO17 appears in **Receiving** list with live value `steady 0 · 0 V` | `wiring.confirmed pin:17 by:"austin"`, retry: `request.stage set_mode requested→acknowledged→executed→observed` |
| 5 | Agent | `set_mode(22, output, initial=1)` → same wiring gate | Second wiring card "Transmit on GPIO22? pin 15 · DUT BTN (active-low)" | `wiring.request pin:22` |
| 6 | Tester | **Allow** | GPIO22 appears in **Transmitting** list with toggle (on) and `Pulse 100 ms` | `wiring.confirmed`, lifecycle ×4 |
| 7 | Agent | `wait_for(17, rising, 200)` then `write(22,0)` | `wait_for` card shows `requested · acked · executed` and a waiting spinner on `observed`; `write` card lights all four stages (observed via readback `level:0`) | `request.stage …`, `pin.change pin:22 level:0 source:readback` |
| 8 | Rig | Scenario `button_led` ties LED to button: GPIO17 rises 12 ms later | Amber **spontaneous pin change** row `GPIO17 ↑ 1 · +12.3 ms`; pin 11 dot turns green; `wait_for` card completes `observed · 12.3 ms`; mini waveform on GPIO17 card shows the step | `pin.change pin:17 level:1 source:stream`, `request.stage wait_for observed t_bridge_us` |
| 9 | Agent | `write(22,1)`; replies "GPIO17 went high 12.3 ms after the button press (threshold 50 ms)." | Agent message | `note actor:agent` |

## Deny path
- Tester clicks **Deny** at step 4: card turns red "rejected"; agent receives `err:wiring_rejected`, replies that it cannot observe GPIO17 and asks for an alternative pin; GPIO17 is released (`pin.mode disabled`).

## Acceptance
- Agent action, bridge ack, and spontaneous pin change are three visually distinct row styles (tool card / grey system row / amber dot row).
- A pin is never driven before `wiring.confirmed` exists for it in the session (test: Virtual Rig with policy `ask` rejects `write` on an unconfirmed pin).
