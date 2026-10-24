# EPIC-04 — Sessions, logging, replay, export

**Component:** Host · **Status:** todo

A session is one connection plus an ordered log of every request stage, response, pin event and bus byte, timestamped against a monotonic clock. Sessions are saved, replayable without hardware, and exportable as JSON and a human-readable report.

## Stories

### US-31 — Session lifecycle · P0 · todo
- `Session.start(transport, actor)` → `session.start` event with manifest, transport descriptor, host clock origin.
- Exactly one connection per session; `connect` on a live session → new session (previous one ends with `session.end {reason:"reconnect"}`).
- Session IDs: `s_<YYYYMMDD>_<HHMMSS>_<4hex>`; stored under `~/.aperture/sessions/<id>/session.json` (path configurable `APERTURE_HOME`).

### US-32 — Monotonic, ordered event log · P0 · todo
- Every event has `seq` (dense, 1-based), `t_host_us` (from `time.monotonic_ns()` relative to session start), optional `t_bridge_us`, `kind`, `actor` (`agent|tester|bridge|rig|system`).
- Appends are thread-safe; `seq` never has gaps; events are written to disk incrementally (JSONL journal) and compacted to `session.json` on end so a crash loses at most the last event.
- Clock offset `offset_us` is estimated from `ping` round-trips (median of 5) and refreshed every 30 s; stored in `clock`.

### US-33 — Capture every request, response, pin event and bus byte · P0 · todo
- Event kinds: `request.stage`, `pin.change`, `bus.bytes`, `capture.done`, `control.changed`, `wiring.*`, `assertion`, `note`, `rig.warning`, `session.*`.
- `bus.bytes` holds base64 `data`, `dir`, `port`/`bus`, and the decoded text preview when valid UTF-8.
- Pin events from stream mode are stored even when no request is pending ("spontaneous" — the UI styles them differently).

### US-34 — Save and list sessions · P0 · todo
- `GET /api/sessions` lists `{id, created_at, transport, bridge, event_count, verdict}`; `GET /api/sessions/{id}` returns the full file.
- Sessions persist across server restarts; the UI sidebar is populated from the API (not localStorage).

### US-35 — Replay without hardware · P0 · todo
- `POST /api/sessions/{id}/replay` puts the UI in replay mode: the same reducer consumes stored events; the pin map and log animate on the original timeline with speed `0.5×/1×/4×/instant`; scrubbing by `seq`.
- Replay never touches a transport; the rig panel shows `Replay · <bridge>` and all driving controls are disabled.

### US-36 — Export JSON · P0 · todo
- `GET /api/sessions/{id}/export.json` returns `session.json` with `schema:1`; `aperture sessions export <id> --json` does the same from the CLI.
- Round-trip test: load → save → identical bytes.

### US-37 — Export human-readable report · P0 · todo
- `GET …/export.md` renders: header (bridge, firmware, transport, duration), verdict, wiring table, assertions table (name, expected, actual, pass), request table (op, args, stages with Δt), pin activity summary (edges per pin), UART transcript, warnings.
- Markdown is deterministic for a given session (no timestamps of generation inside the body); tests snapshot a Virtual Rig session.
