# UF-4 — Re-run a saved plan without the agent

**Persona:** Firmware developer / Tester · **Epics:** EPIC-05, EPIC-04 · **Stories:** US-39, US-43, US-44, US-31, US-37

## From the UI

| # | Tester does | System |
|---|---|---|
| 1 | Opens **Plans** sheet | List: `boot-ok` (last: PASS, 2 min ago), `esp32-safety` (last: FAIL), … |
| 2 | Clicks **Run** on `esp32-safety` while connected to the real ESP32 | Pre-flight: `requires.wiring` vs session wiring → 7 wires, 2 unconfirmed → modal "Confirm wiring for GPIO27 (FAULT→ESP32 GPIO18), GPIO15 (UART RX)" with per-pin `Mark as wired` toggles; pin map pulses those pins |
| 3 | Confirms both | Run starts; **no agent involved** — the runner executes steps; thread shows tool cards and assertions exactly like UF-3 step 3 |
| 4 | — | Step `Fault while trigger HIGH` fails: assertion card red "expected SAFETY=1, observed 0 (readback) while telemetry claims safety=1 — DISCREPANCY"; `on_fail:continue` so remaining steps run; `cleanup` runs `release_all` |
| 5 | — | Plan card verdict **FAIL (1/6)**; session saved; `Export report` available |

## From the CLI (no UI, no agent)

```sh
aperture plan run esp32-safety --transport virtual --scenario esp32_safety          # exit 1 (planted bug)
aperture plan run esp32-safety --transport virtual --scenario esp32_safety_fixed    # exit 0
aperture plan run esp32-safety --transport tcp --target rig01.local:8765 --wiring wiring.json
```
- Writes `~/.aperture/sessions/<id>/session.json` and `report.md`; prints the assertion table; exit code = verdict.
- `--wiring` supplies pre-confirmed wiring (CI/bench automation) and is recorded as `wiring.confirmed by:"cli:<user>"`.

## Acceptance
- Runner never imports the LLM client; `aperture plan run` works with `OPENAI_API_KEY` unset.
- Legacy `make demo` behaviour (planted bug detected, fixed firmware passes) is reproduced by the two CLI invocations above.
