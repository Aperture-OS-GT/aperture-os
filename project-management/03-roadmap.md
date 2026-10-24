# 03 — Roadmap and PR sequence

Each row is one PR. Everything must run against the Virtual Rig in CI; `bench-verify` items are checked on real hardware afterwards.

| # | PR | Stories | Depends on | Exit criteria |
|---|---|---|---|---|
| 0 | `project-management/` spec (this) | — | — | Epics, stories, flows reviewed |
| 1 | Host core: `aperture/pins.py`, `protocol.py`, `transport/base.py`, `rig/virtual.py`, `transport/virtual.py`, behaviors | US-05…US-11 (virtual side), US-13, US-14, US-17…US-20 | 0 | Virtual Rig passes protocol conformance tests; manifest identical in shape to bridge |
| 2 | Requests + sessions + agent tools | US-21…US-37 | 1 | Every tool records a 4-stage lifecycle; session save/load/replay/export round-trips |
| 3 | Test plans | US-38…US-44 | 2 | Example NL test compiles to `plan.json`, runs without agent, pass/fail correct on Virtual Rig (incl. planted-bug DUT) |
| 4 | Tester UI (looprig-ui design) + `aperture/server.py` | US-45…US-58 | 2 | Three-column UI live on Virtual Rig; lifecycle stages visible; replay works |
| 5 | Pi bridge: protocol v1 executor, TCP transport, control handshake, watchdog, fail-safe | US-01 (TCP), US-03, US-04 (JSON), US-05…US-12 | 1 | Bridge in `sim` mode passes same conformance tests as Virtual Rig |
| 6 | BLE: GATT server on Pi (`bless`), BLE central on host (`bleak`), CBOR chunking | US-01, US-02, US-04 (CBOR), US-15, US-16 | 5 | Chunker unit-tested; `bench-verify` end-to-end |
| 7 | CI, packaging, docs, legacy removal | US-59…US-66 | 1–6 | `.github/workflows/ci.yml` green; README reflects new CLI |

## Milestones

* **M1 — Virtual-only vertical slice** (PRs 0–4): a tester can open the UI, see "Virtual Rig", ask the agent to press a fake button and watch GPIO17 go high, save the session, replay it.
* **M2 — Bench over TCP** (PR 5): same flow against a Pi on Wi-Fi with the ESP32 safety DUT.
* **M3 — BLE** (PR 6): same flow over BLE with take-control and watchdog fail-safe verified by pulling the plug.
* **M4 — CI & cleanup** (PR 7).

## Working agreements

* Branch names `devin/<unix-ts>-<slug>`; PR title starts with the story IDs it closes, e.g. `US-13 US-14: transport ABC + Virtual Rig`.
* Tests go in `tests/test_<area>.py`; conformance tests are parametrised over `[virtual, bridge-sim]` transports so the two never drift.
* No PR merges with `ruff` or `pytest` red. `make demo` must keep passing until PR 7 replaces it.
