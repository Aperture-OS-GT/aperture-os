# EPIC-08 — Bench DUT and hardware parity

**Component:** Firmware · Pi · Host · **Owner persona:** Tester / Rig admin · **Status:** in-progress

Virtual Rig parity is only worth something if the same plan can be run, and can fail, on
real hardware. This epic supplies a bench DUT that mirrors the Virtual Rig scenarios, a way
to prove the Pi's header works before a DUT is attached, and a report that compares a
virtual run with a bench run. Flows: UF-8, UF-11.

## Stories

### US-67 — ESP32 HIL demo DUT firmware · P1 · in-review (#2)
*As a tester I want a bench DUT that behaves like the `button_led` scenario so a plan that
passes in CI exercises the same behaviour on real silicon.*
- `firmware/esp32-hil-demo` builds with PlatformIO for `esp32dev`.
- LED (→ Pi GPIO17) is HIGH while the active-low button (← Pi GPIO22) is pressed; `BOOT OK\n`
  on UART2 (→ Pi GPIO15) 300 ms after reset; byte echo (← Pi GPIO14); 2 Hz heartbeat
  (→ Pi GPIO24).
- The button uses the ESP32 pull-up, so a released (high-Z) Pi pin never presses it.
- README: wiring table (BCM + physical), 3.3 V and common-ground rules, flashing, and the
  reset-after-open requirement for the banner (R-10).
- `bench-verify`: terminal shows the banner after EN and echoes input.

### US-68 — Wiring card for a plan or scenario · P2 · todo
- `aperture plan wiring <plan> --format md` prints Pi BCM, physical pin, direction, DUT pin
  and purpose for `requires.wiring`, plus the 3.3 V / common-ground / power-off rules.
- The UI shows the same card in the pre-flight of a bench run (UF-4 step 2).

### US-69 — Bench variant of the reference plan · P1 · todo
- `plans/boot-ok-bench.json`: enable stream on GPIO17, `uart_open`, pulse the `reset` line
  low 20 ms, `uart_expect BOOT OK ≤ 2 s`, press GPIO22, `wait_for(17, rising, 50 ms,
  since=press)` (R-3), release.
- Passes on the Virtual Rig `button_led` scenario (reset is harmless there) and on the
  US-67 DUT; fails on both when the LED does not follow the button.

### US-70 — Bridge self-test with loopback jumpers · P1 · todo
*As a rig admin I want to prove the Pi's GPIO stack before wiring a DUT.*
- `aperture-bridge --self-test --jumper A:B [--jumper C:D …]` on the real hardware backend.
- Checks, per jumper and in both directions: output → input, pull-up and pull-down read
  back with the far side `disabled`, PWM frequency within 5 % by capture, pulse width
  within the manifest jitter, and `release_all` leaving both pins `disabled`.
- Prints a table and exits non-zero on any failure; never drives a pin that is not part of
  a declared jumper.

### US-71 — Virtual ↔ bench parity report · P2 · todo
- `aperture plan parity <plan> --virtual <scenario> --target <transport>` runs the plan on
  both and renders one report: verdict per assertion side by side, timing per assertion
  (e.g. edge latency 12 ms virtual vs 0.4 ms bench), and any step that only one side
  executed.
- A verdict mismatch exits non-zero, so CI on a bench runner flags a scenario that no
  longer models its DUT.
