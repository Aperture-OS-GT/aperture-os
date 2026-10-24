import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from agent.client import Client
from agent.generic_tools import GenericTools
from agent.planner import run_llm, run_offline
from agent.profiles import checked, run_profile
from agent.wiring import Wiring


def main():
    parser = argparse.ArgumentParser(description="Aperture OS physical truth verifier")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--rfcomm", action="store_true", help="host is MAC; port is channel")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--report", default="report.json")
    parser.add_argument("--profile", choices=("esp32_safety", "mpu6050_gyro", "display"))
    args = parser.parse_args()
    console = Console()

    def display(row):
        table = Table(title=row["step"])
        for column in ("Reasoning →", "Pins injected →", "Latency ms →", "Verdict"):
            table.add_column(column)
        issues = row["issues"]
        table.add_row(row["reasoning"], str(row.get("injected", {})),
                      str(row["latency_ms"]), "FAIL" if issues else "PASS")
        console.print(table)
        for issue in issues:
            console.print(issue, style="bold red", markup=False)

    with Client(args.host, args.port, args.rfcomm) as client:
        if args.profile:
            wiring = Wiring(lambda level, data: None)
            checked(client, cmd="select_profile", profile=args.profile)
            wiring.select(args.profile)
            for request in wiring.requests():
                console.print(f"Connect Pi BCM{request['pi_pin']} to {request['component']} "
                              f"{request['dut_pin']}: {request['reason']}", markup=False)
                if input("Wired with shared GND and 3.3V logic? Type yes to confirm: ").strip() == "yes":
                    wiring.confirm_connection(request["pi_pin"])
                else:
                    break
            report = (run_llm(client, args.model, display,
                              toolset=GenericTools(client, wiring, display)) if args.llm else
                      run_profile(client, wiring, display))
        else:
            report = run_llm(client, args.model, display) if args.llm else run_offline(client, display)
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    console.print("VERDICT: " + ("PASS" if report["pass"] else "FAIL — firmware safety violation"))
    console.print(f"JSON report: {args.report}")
    raise SystemExit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
