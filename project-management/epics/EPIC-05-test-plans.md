# EPIC-05 — Natural-language test plans

**Component:** Host · **Status:** todo

The agent turns a natural-language test into a deterministic, re-runnable sequence of tool calls plus pass/fail assertions. Saved plans run without the agent in the loop. Plan format is in `02-architecture.md`.

Reference example (from the brief):
> "confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22; verify the ESP32 prints BOOT OK over UART"

## Stories

### US-38 — Plan file format and validation · P0 · todo
- `schema:1`; `name`, `source` (the NL text), `requires` (`wiring`, `uart`, `scenario?`), `steps[]` (`id`, `tool`, `args`, `assert?`, `timeout_ms?`, `on_fail?` = `stop|continue`), `cleanup[]`.
- Validation uses the tool schemas from US-27; unknown tool or bad args → load error with step id.
- Stored under `~/.aperture/plans/<slug>.json`; repo ships examples in `plans/`.

### US-39 — Deterministic plan runner · P0 · todo
- `PlanRunner(session, plan).run()` executes steps in order, no LLM, fixed per-step timeouts; `assert:true` steps decide the verdict; `cleanup` always runs (even on exception), ending with `release_all`.
- Runner emits `plan.start`, `plan.step {id, request}`, `plan.end {pass, failed_steps}` events; the run is itself a session, so it replays and exports.
- Re-running the same plan on the same Virtual Rig scenario yields identical assertion results (determinism test runs it 3×).

### US-40 — Compile NL → plan (offline, rule-based) · P0 · todo
- `compile(text, context)` handles the reference sentence and these patterns without an LLM: "GPIOx goes high/low within N ms of pressing the button on GPIOy", "verify … prints `<text>` over UART", "measure frequency on GPIOx is F Hz ± tol", "set GPIOx high/low", "pulse GPIOx for N ms", "expect GPIOx to follow GPIOy", "press the button on GPIOx".
- Uses session wiring and manifest to fill `requires` and choose UART pins; unresolved references produce a plan with `needs:[…]` instead of guessing.

### US-41 — Compile NL → plan (LLM-assisted) · P1 · todo
- With `OPENAI_API_KEY`, the agent is given the tool schemas and asked to emit a plan JSON (not to act); the output must validate (US-38) — up to 2 repair rounds, then fall back to US-40 with a `note`.
- The compiled plan is shown to the tester for approval before its first run (UF-3).

### US-42 — Save, list, edit plans · P0 · todo
- `GET/POST /api/plans`, `GET/PUT/DELETE /api/plans/{slug}`; UI lists plans with last verdict; a plan can be duplicated and its steps edited as JSON in the UI.

### US-43 — Run plans from the UI and CLI · P0 · todo
- `POST /api/plans/{slug}/run` and `aperture plan run <slug> --transport virtual --scenario esp32_safety` run without the agent; CLI exits 0/1 by verdict and writes `session.json` + `report.md`.
- Missing wiring (`requires.wiring` not confirmed) blocks the run with a list of pins to confirm (UF-4).

### US-44 — Port legacy profiles to plans · P1 · todo
- `profiles/esp32_safety.json` (6 tests), `mpu6050_gyro.json`, `display.json` become `plans/*.json`; the planted-bug scenario fails `Fault while trigger HIGH` and passes with `demo_bug:false`; `make demo` runs the plan via CLI. Legacy `agent/profiles.py` removed when this merges.
