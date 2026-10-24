# UF-6 — Virtual Rig in CI

**Persona:** Firmware developer · **Epics:** EPIC-02, EPIC-05, EPIC-07 · **Stories:** US-17–US-20, US-39, US-43, US-59

## Flow

1. Developer opens a PR touching `aperture/` or `plans/`.
2. GitHub Actions (`.github/workflows/ci.yml`) on Python 3.10 and 3.12:
   ```sh
   pip install -e '.[dev]'
   ruff check .
   pytest -q                                   # unit + conformance (virtual, bridge-sim over TCP)
   aperture plan run boot-ok      --transport virtual --scenario uart_echo+button_led
   aperture plan run esp32-safety --transport virtual --scenario esp32_safety_fixed
   aperture plan run esp32-safety --transport virtual --scenario esp32_safety && exit 1 || true   # must FAIL
   ```
3. `report.md` and `session.json` for each plan are uploaded as artifacts.
4. A failing assertion fails the job with the assertion table in the log.

## Scenario files (`aperture/rig/scenarios/*.json`)

```json
{ "name": "uart_echo+button_led",
  "behaviors": [
    {"type": "Button",   "pin": 22, "pressed_level": 0, "wired_to": {"dut": "BTN"}},
    {"type": "Loopback", "a": 22, "b": 17, "invert": true, "delay_us": 12000},
    {"type": "UartEcho", "rx_pin": 14, "tx_pin": 15, "baud": 115200, "banner": "BOOT OK\n", "delay_ms": 300}
  ],
  "wiring_policy": "off" }
```
Scenarios are the same files the UI's `Virtual Rig` picker lists, so what CI runs is what the tester sees.

## Acceptance
- CI is green on `main`; the planted-bug scenario is asserted to fail (guards against a "passes everything" regression).
- Conformance tests are parametrised: `@pytest.fixture(params=["virtual", "bridge-sim"])`, identical bodies.
