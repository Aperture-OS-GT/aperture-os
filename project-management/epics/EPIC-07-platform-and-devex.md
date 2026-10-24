# EPIC-07 — Platform, CI, packaging, safety

**Component:** All · **Status:** todo

## Stories

### US-59 — CI on the Virtual Rig · P0 · todo
- `.github/workflows/ci.yml`: `pip install -e '.[dev]'`, `ruff check .`, `pytest -q`, run example plans with `aperture plan run … --transport virtual`, upload `report.md` artifacts. Python 3.10 and 3.12 matrix.
- `docs/ci-workflow.yml` deleted.

### US-60 — Packaging and CLI · P0 · todo
- `pyproject` extras: `[pi]` (`lgpio`, `pyserial`, `spidev`, `smbus2`, `bless`, `cbor2`), `[ble]` (`bleak`, `cbor2`), `[llm]`, `[dev]`.
- Console scripts: `aperture` (sub-commands `ui`, `plan run|list`, `sessions list|export`, `rig virtual`), `aperture-bridge` (Pi daemon). Legacy `aperture-daemon/agent/ui` removed in the final cleanup PR.

### US-61 — Protocol documentation · P0 · todo
- `docs/protocol-v1.md` generated from `aperture/protocol.py` schemas (ops, args, errors, manifest, chunking) so docs can't drift; test asserts the generated file matches.

### US-62 — Pi install script and systemd unit · P1 · todo
- `scripts/pi-install.sh` installs extras, enables BlueZ experimental features if needed, installs `aperture-bridge.service` (restart=always, `ExecStopPost` runs `release_all` safeguard via `aperture-bridge --release`).

### US-63 — Safety rails · P0 · todo
- Pin allowlist/denylist per scenario or rig config (`rig.json`): denied pins return `err:unsupported`; default denies I2C ID pins (physical 27/28).
- Rate limit: ≤ 200 driving requests/s per controller; excess → `err:busy` and a `rig.warning`.
- Voltage note in every wiring request card: "3.3 V logic only — never connect 5 V signals to GPIO".

### US-64 — Security posture · P0 · todo
- Host server binds `127.0.0.1` only; POST requires same-origin (`Origin`/`Sec-Fetch-Site`); no secrets logged; BLE pairing with bonding required for the GATT service (`bench-verify`).

### US-65 — Developer docs · P1 · todo
- README rewrite: quickstart on Virtual Rig, bench setup (TCP then BLE), writing a scenario, writing a plan, exporting a session; `docs/adr/` with decisions (BCM numbering, CBOR on BLE, wiring-as-metadata, one controller).

### US-66 — Remove legacy modules · P1 · todo
- Delete `pi/daemon.py`, `pi/protocol.py`, `agent/*`, `ui/*` once their stories are `done`; keep `pi/header.py` table merged into `aperture/pins.py`; `make demo` runs the plan CLI; all old tests replaced.
