"""``aperture`` command line: run plans and manage sessions without the agent or the UI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .plans import PlanRunner, list_plans, load_plan
from .report import render_report
from .rig.virtual import list_scenarios
from .session import Session, dump_session, list_sessions, load_session
from .tools import Tools


def _connect(args) -> tuple[Session, Tools]:
    session = Session(actor=f"cli:{args.actor}", home=args.home)
    tools = Tools(session, wiring_policy=args.wiring_policy)
    tools.call("connect", transport=args.transport, scenario=args.scenario, target=args.target)
    if args.wiring:
        for pin, entry in json.loads(Path(args.wiring).read_text()).items():
            session.request_wiring(int(pin), entry.get("dut_pin", "?"), entry.get("component", "?"),
                                   entry.get("reason", "pre-confirmed"), entry.get("dir", "out"), "cli")
            session.confirm_wiring(int(pin), True, by=f"cli:{args.actor}")
    return session, tools


def cmd_plan_run(args) -> int:
    plan = load_plan(args.plan, args.home)
    session, tools = _connect(args)
    try:
        result = PlanRunner(tools, plan).run()
    finally:
        session.close()
    out_dir = Path(args.out) if args.out else session.dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_report(session.to_dict()), encoding="utf-8")
    if args.out:
        (out_dir / "session.json").write_text(dump_session(session.to_dict()), encoding="utf-8")
    if result.get("blocked"):
        pins = ", ".join(f"GPIO{w['pin']}" for w in result["blocked"])
        print(f"BLOCKED: confirm wiring for {pins}")
        return 2
    for step in result["steps"]:
        flag = "PASS" if step["pass"] else "FAIL"
        extra = f" — {step['error']['code']}: {step['error']['msg']}" if step.get("error") else ""
        print(f"{flag:4} {step['id']:4} {step['tool']}{' (assert)' if step['assert'] else ''}{extra}")
    print(f"{'PASS' if result['pass'] else 'FAIL'} {plan['slug']} · session {session.id} · {out_dir / 'report.md'}")
    return 0 if result["pass"] else 1


def cmd_plan_list(args) -> int:
    for plan in list_plans(args.home):
        print(f"{plan['slug']:28} {plan['steps']:3} steps {plan['assertions']:2} asserts  [{plan['origin']}] {plan['name']}")
    return 0


def cmd_sessions_list(args) -> int:
    for item in list_sessions(args.home):
        verdict = {True: "PASS", False: "FAIL"}.get(item["verdict"], "—")
        print(f"{item['id']}  {verdict:4}  {item['event_count']:5} events  {item['bridge'] or '—'}")
    return 0


def cmd_sessions_export(args) -> int:
    data = load_session(args.session, args.home)
    out = Path(args.out or ".")
    out.mkdir(parents=True, exist_ok=True)
    if args.json or not args.md:
        (out / f"{data['id']}.json").write_text(dump_session(data), encoding="utf-8")
    if args.md or not args.json:
        (out / f"{data['id']}.md").write_text(render_report(data), encoding="utf-8")
    print(f"exported {data['id']} to {out}")
    return 0


def cmd_rig_scenarios(args) -> int:
    for item in list_scenarios():
        print(f"{item['name']:20} {item['description']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aperture", description="Aperture OS hardware-in-the-loop CLI")
    parser.add_argument("--home", help="data directory (default $APERTURE_HOME or ~/.aperture)")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    run = plan.add_parser("run", help="run a saved plan without the agent")
    run.add_argument("plan")
    run.add_argument("--transport", default="virtual", choices=["virtual", "tcp", "ble"])
    run.add_argument("--scenario", default="empty")
    run.add_argument("--target")
    run.add_argument("--wiring", help="JSON file {pin: {dut_pin, component, dir}} of pre-confirmed wiring")
    run.add_argument("--wiring-policy", choices=["ask", "off"], default="off",
                     help="ask = block unless requires.wiring is confirmed (use --wiring); off = trust the bench")
    run.add_argument("--actor", default="user")
    run.add_argument("--out", help="write session.json + report.md here (default: the session folder)")
    run.set_defaults(func=cmd_plan_run)
    plan.add_parser("list").set_defaults(func=cmd_plan_list)

    sessions = sub.add_parser("sessions").add_subparsers(dest="action", required=True)
    sessions.add_parser("list").set_defaults(func=cmd_sessions_list)
    export = sessions.add_parser("export")
    export.add_argument("session")
    export.add_argument("--json", action="store_true")
    export.add_argument("--md", action="store_true")
    export.add_argument("-o", "--out")
    export.set_defaults(func=cmd_sessions_export)

    rig = sub.add_parser("rig").add_subparsers(dest="action", required=True)
    rig.add_parser("scenarios").set_defaults(func=cmd_rig_scenarios)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
