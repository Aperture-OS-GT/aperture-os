"""Human-readable session report (Markdown). Deterministic for a given session file."""
from __future__ import annotations

import base64
from collections import Counter, defaultdict


def _ms(us: int | None) -> str:
    return "—" if us is None else f"{us / 1000:.1f} ms"


def _args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else "—"


def render_report(data: dict) -> str:
    events = data.get("events", [])
    manifest = data.get("manifest") or {}
    transport = data.get("transport") or {}
    duration_us = events[-1]["t_host_us"] if events else 0
    verdict = data.get("verdict")
    verdict_text = "PASS" if verdict else ("FAIL" if verdict is False else "no assertions")

    requests: dict[str, dict] = {}
    order: list[str] = []
    for event in events:
        if event["kind"] != "request.stage":
            continue
        req = requests.setdefault(event["req"], {"stages": {}})
        if event["req"] not in order:
            order.append(event["req"])
        req["stages"][event["stage"]] = event
        if event["stage"] == "requested":
            req.update(op=event.get("op"), args=event.get("args", {}), actor=event.get("actor"))
        if event["stage"] == "executed":
            req.update(ok=event.get("ok"), err=event.get("err"))

    lines = [f"# Aperture session `{data['id']}`", "",
             f"* Created: {data['created_at']}",
             (f"* Bridge: {manifest.get('bridge', '—')} · firmware {manifest.get('fw', '—')} · "
              f"board {manifest.get('board', '—')}"),
             f"* Transport: {transport.get('kind', '—')}"
             + (f" · scenario `{transport['scenario']}`" if transport.get("scenario") else ""),
             f"* Duration: {_ms(duration_us)} · {len(events)} events · {len(order)} requests",
             f"* Ended: {(data.get('ended') or {}).get('reason', 'open')}", "",
             f"## Verdict: {verdict_text}", ""]

    assertions = [e for e in events if e["kind"] == "assertion"]
    if assertions:
        lines += ["## Assertions", "", "| # | Name | Expected | Actual | Result |", "|---|---|---|---|---|"]
        for n, a in enumerate(assertions, 1):
            lines.append(f"| {n} | {a['name']} | `{a.get('expected')}` | `{a.get('actual')}` | "
                         f"{'PASS' if a['pass'] else 'FAIL'} |")
        lines.append("")

    wiring = data.get("wiring") or {}
    if wiring:
        lines += ["## Wiring (marked by a human; not electrically verified)", "",
                  "| GPIO | DUT pin | Component | Direction | Marked by |", "|---|---|---|---|---|"]
        for pin in sorted(wiring, key=int):
            w = wiring[pin]
            lines.append(f"| GPIO{pin} | {w.get('dut_pin')} | {w.get('component')} | {w.get('dir')} | {w.get('by')} |")
        lines.append("")

    lines += ["## Requests", "", "| Req | Actor | Op | Args | Ack Δ | Exec Δ | Observed Δ | Result |",
              "|---|---|---|---|---|---|---|---|"]
    for rid in order:
        r = requests[rid]
        s = r["stages"]
        t0 = s.get("requested", {}).get("t_host_us")
        deltas = [_ms(s[name]["t_host_us"] - t0) if name in s and t0 is not None else "—"
                  for name in ("acknowledged", "executed", "observed")]
        result = "ok" if r.get("ok") else f"err:{r.get('err')}"
        if "observed" in s and s["observed"].get("mismatch"):
            result += " · MISMATCH"
        lines.append(f"| {rid} | {r.get('actor')} | `{r.get('op')}` | {_args(r.get('args', {}))} | "
                     f"{deltas[0]} | {deltas[1]} | {deltas[2]} | {result} |")
    lines.append("")

    changes = [e for e in events if e["kind"] == "pin.change"]
    if changes:
        per_pin = Counter(e["pin"] for e in changes)
        spontaneous = sum(1 for e in changes if e.get("source") == "stream")
        lines += ["## Pin activity", "", f"{len(changes)} level changes ({spontaneous} streamed)", "",
                  "| GPIO | Edges |", "|---|---|"]
        for pin in sorted(per_pin):
            lines.append(f"| GPIO{pin} | {per_pin[pin]} |")
        lines.append("")

    bus = [e for e in events if e["kind"] == "bus.bytes"]
    if bus:
        lines += ["## Bus transcript", ""]
        streams: dict[tuple, bytearray] = defaultdict(bytearray)
        for e in bus:
            streams[(e.get("bus"), e.get("port"), e.get("dir"))] += base64.b64decode(e.get("data") or "")
        for (name, port, direction), payload in streams.items():
            arrow = "host → DUT" if direction == "tx" else "DUT → host"
            lines += [f"### {name} port {port} ({arrow})", "", "```text",
                      payload.decode("utf-8", "replace").rstrip("\n") or "(no bytes)", "```", ""]

    warnings = [e for e in events if e["kind"] in ("rig.warning", "control.lost", "stream.overflow")]
    if warnings:
        lines += ["## Warnings", ""]
        for w in warnings:
            detail = w.get("detail") or w.get("reason") or ""
            lines.append(f"* `{w['kind']}` at {_ms(w['t_host_us'])}: {detail}")
        lines.append("")

    notes = [e for e in events if e["kind"] == "note"]
    if notes:
        lines += ["## Notes", ""]
        for n in notes:
            lines.append(f"* **{n.get('actor')}** ({_ms(n['t_host_us'])}): {n.get('text')}")
        lines.append("")
    return "\n".join(lines)
