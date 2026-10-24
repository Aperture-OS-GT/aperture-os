# UF-1 — Connect to a rig and take control

**Persona:** Tester · **Epics:** EPIC-01, EPIC-02, EPIC-06 · **Stories:** US-01, US-02, US-03, US-11, US-14, US-46, US-47

## Preconditions
- Host UI is open (`aperture ui` → http://127.0.0.1:8080). No session is live.
- Either a Pi bridge is advertising over BLE / listening on TCP, or nothing is reachable.

## Flow

| # | Tester does | System shows / does | Events recorded |
|---|---|---|---|
| 1 | Opens the UI | Rig panel reads **Disconnected**; connection pill in the top bar says `Not connected`; sessions sidebar lists recent sessions | — |
| 2 | Clicks **Connect** | Sheet with three choices: `Virtual Rig` (scenario dropdown: `empty`, `loopback`, `uart_echo`, `button_led`, `esp32_safety`, `esp32_safety_fixed`), `Bluetooth` (scanning… list of `aperture-*` devices with RSSI), `TCP` (host:port) | — |
| 3a | Picks `Bluetooth → aperture-rig01` | Pill: `Connecting…`; host connects as central, negotiates MTU, sends `hello` | `session.start`, `request.stage hello requested/acknowledged/executed/observed` |
| 3b | Picks `Virtual Rig → button_led` | Identical UI path, instant | same events, `transport.kind:"virtual"` |
| 3c | Picks Bluetooth, nothing found after 5 s | Sheet says "No bridge reachable — continue with Virtual Rig?" `[Use Virtual Rig]` | — |
| 4 | — | Manifest arrives: rig panel shows bridge `aperture-rig01`, fw `1.0.0`, board `Raspberry Pi 4 Model B`, power `VBUS ok`, MTU `247`, clock offset `+1.2 ms`. Pin map rebuilds from `manifest.pins` (PWM-capable pins get a `~` badge). State: **Connected · observer** | `manifest` stored on session |
| 5 | Clicks **Take Control** | `control.take` sent; on success state becomes **Controller**; composer and pin controls enable; pill turns green | `request.stage control.take ×4`, `control.changed {controller:"host-austin"}` |
| 5′ | Someone else holds control | Button shows `Controlled by <name> · idle 12 s`; `Force take` appears only once idle ≥ 30 s; pressing it before then gives `err:busy` toast | `request.stage … executed err:busy` |
| 6 | Starts working (UF-2 / UF-3) | — | — |

## Edge cases
- Link drops at step 4–6: pill turns red `Reconnecting…`, rig panel state `Disconnected`, bridge has already released outputs (UF-7); session stays open and the UI offers `Reconnect` (same session, new connection = new session per US-31, with a link in the thread).
- Manifest `v` unsupported → toast "Bridge speaks protocol vN; this host supports v1. Update the bridge." and disconnect.
- Virtual Rig never fails to connect; it is always the fallback so the UI "keeps working".

## Acceptance (UI)
- "Virtual Rig" label visible in the rig panel and the pill when no Pi is connected.
- Every row in step 3–5 is visible in the Current session column with its four stage chips.
