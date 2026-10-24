# Aperture OS — Project Management

This folder is the single source of truth for *what* Aperture OS is supposed to do and *how far along* it is. Code PRs reference story IDs (`US-nn`) in their title or body; stories link back to the PRs that deliver them.

## Layout

| Path | Purpose |
|---|---|
| `00-vision-and-scope.md` | Product vision, personas, topology, non-goals, glossary |
| `01-current-state.md` | Gap analysis: what the repo has today vs. the target spec, what is kept/replaced |
| `02-architecture.md` | Target architecture: packages, transports, protocol v1, request lifecycle, session/plan data formats |
| `03-roadmap.md` | Delivery order, milestones, PR sequence |
| `04-spec-review.md` | Issues found while building against this spec, with evidence and proposed resolutions (R-n) |
| `epics/EPIC-0x-*.md` | One file per epic; each holds its user stories with acceptance criteria |
| `user-flows/*.md` | End-to-end flows across epics (what the tester/agent actually does, screen by screen) |

## Status legend

| Status | Meaning |
|---|---|
| `todo` | Not started |
| `in-progress` | Branch open, not merged |
| `in-review` | PR open |
| `done` | Merged to `main` with tests |
| `blocked` | Needs hardware, a decision, or another story |

## Epics at a glance

| ID | Epic | Component | Stories | Status |
|---|---|---|---|---|
| [EPIC-01](epics/EPIC-01-pi-bridge.md) | Pi bridge daemon | Raspberry Pi | US-01…US-12 | todo |
| [EPIC-02](epics/EPIC-02-transport-and-virtual-rig.md) | Transport abstraction & Virtual Rig | Host | US-13…US-20 | todo |
| [EPIC-03](epics/EPIC-03-agent-tools-and-request-lifecycle.md) | Agent tool set & request lifecycle | Host | US-21…US-30 | todo |
| [EPIC-04](epics/EPIC-04-sessions-and-replay.md) | Sessions, logging, replay, export | Host | US-31…US-37 | todo |
| [EPIC-05](epics/EPIC-05-test-plans.md) | Natural-language test plans | Host | US-38…US-44 | todo |
| [EPIC-06](epics/EPIC-06-tester-interface.md) | Tester interface | UI | US-45…US-58 | todo |
| [EPIC-07](epics/EPIC-07-platform-and-devex.md) | Platform, CI, packaging, safety | All | US-59…US-66 | todo |
| [EPIC-08](epics/EPIC-08-bench-dut-and-hardware-parity.md) | Bench DUT and hardware parity | Firmware/Pi/Host | US-67…US-71 | in-progress |

## User flows

| Flow | Covers |
|---|---|
| [UF-1 Connect and take control](user-flows/UF-1-connect-and-take-control.md) | EPIC-01, 02, 06 |
| [UF-2 Ad-hoc pin action with wiring approval](user-flows/UF-2-adhoc-action-with-wiring-approval.md) | EPIC-03, 06 |
| [UF-3 Natural-language test to saved plan](user-flows/UF-3-natural-language-test-to-plan.md) | EPIC-03, 05 |
| [UF-4 Re-run a saved plan without the agent](user-flows/UF-4-rerun-saved-plan.md) | EPIC-05, 04 |
| [UF-5 Replay and export a session](user-flows/UF-5-replay-and-export-session.md) | EPIC-04, 06 |
| [UF-6 Virtual Rig in CI](user-flows/UF-6-virtual-rig-in-ci.md) | EPIC-02, 05, 07 |
| [UF-7 Disconnect, watchdog and fail-safe](user-flows/UF-7-disconnect-and-fail-safe.md) | EPIC-01, 06 |
| [UF-8 Set up a Pi bridge and pair a host](user-flows/UF-8-bridge-setup-and-pairing.md) | EPIC-01, 07, 08 |
| [UF-9 Capture a waveform and assert its timing](user-flows/UF-9-capture-and-timeline-assertion.md) | EPIC-01, 03, 05, 06 |
| [UF-10 UART, I2C and SPI transactions](user-flows/UF-10-bus-transactions.md) | EPIC-01, 03, 04 |
| [UF-11 Bench bring-up of the reference test](user-flows/UF-11-bench-bring-up.md) | EPIC-01, 05, 08 |

## Conventions

* **Story format**: `US-nn — <title>` · persona · priority (P0 ship-blocker / P1 / P2) · status · PRs. Acceptance criteria are Given/When/Then and are the test list for the PR.
* **Pin numbering**: BCM GPIO numbers everywhere in code, protocol and UI labels. Physical header position (1–40) is display metadata only.
* **Virtual Rig parity**: every story that touches hardware behavior must pass against the Virtual Rig in CI. Hardware-only verification is noted as `bench-verify` in the story.
* **Nothing claims continuity**: wiring confirmations are human metadata; the UI never asserts a wire is physically present unless a loopback/continuity capture proves it.
* **Design reference**: `looprig-ui/app.html` (+ `app.css`, `app.js`) is the visual and interaction reference for EPIC-06. `looprig-ui/dashboard.html` is an earlier concept kept for ideas (serial console, test-suite strip) but not the target.
