# UF-3 — Natural-language test → saved, deterministic plan

**Persona:** Tester + Agent · **Epics:** EPIC-03, EPIC-05, EPIC-06 · **Stories:** US-38, US-40, US-41, US-42, US-43, US-57

## Preconditions
- Connected as Controller (UF-1). Wiring for GPIO17 (LED, in) and GPIO22 (BTN, out) confirmed (UF-2). UART pins 14/15 wired to the ESP32's RX2/TX2 and confirmed.

## Flow

| # | Actor | Action | UI | Events / artifacts |
|---|---|---|---|---|
| 1 | Tester | Types: `confirm GPIO17 goes high within 50 ms of pressing the button on GPIO22; verify the ESP32 prints BOOT OK over UART` | Bubble | `note` |
| 2 | Agent | Compiles (rule-based, or LLM if key present) into a plan; **does not execute yet** | **Plan card** in the thread: name `boot ok`, 6 steps listed (`set_mode 17 input`, `set_mode 22 output 1`, `uart_open 14 15 115200`, `write 22 0`, `wait_for 17 rising ≤50 ms ✔assert`, `uart_expect /BOOT OK/ ≤2000 ms ✔assert`), `requires` (wiring ✓ ✓ ✓, uart ✓), buttons `Run` / `Edit JSON` / `Save` | `plan.compiled {slug:"boot-ok", source, steps}` |
| 3 | Tester | Clicks **Run** | Plan card expands to a step list; each step becomes a tool card with 4 stage chips as it executes; assertions render green/red | `plan.start`, per-step `plan.step` + lifecycle events, `assertion ×2`, `plan.end {pass:true}` |
| 4 | Rig | Button press → LED rises in 12 ms; UART echo device prints `BOOT OK` 300 ms after reset | Amber pin row `GPIO17 ↑ 1 · +12.1 ms`; mono bus row `UART0 ◂ "BOOT OK\n"` | `pin.change`, `bus.bytes dir:rx` |
| 5 | Agent | Summarises: "PASS — rising edge at 12.1 ms (limit 50 ms); `BOOT OK` received at 318 ms." | Agent message + verdict badge on plan card | `note` |
| 6 | Tester | Clicks **Save** | Toast "Saved as plans/boot-ok.json"; Plans sheet lists it with last verdict | file `~/.aperture/plans/boot-ok.json` |

## Variants
- Compile cannot resolve "the button" (no wiring named BTN): plan card shows `needs: [button pin]` and the agent asks "Which GPIO is the button on?"; tester answers, compile reruns.
- LLM output invalid: repair up to 2×, then rule-based fallback with a `note` "compiled offline".
- Tester edits JSON before Run: validation errors show inline with step id; Run disabled until valid.

## Acceptance
- The compiled plan for the reference sentence is byte-identical across runs (snapshot test).
- Running the plan twice on `uart_echo+button_led` scenario yields two sessions with identical assertion verdicts.
