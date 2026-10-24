import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from agent.client import Client
from agent.flash import ENVS, FLASH_PINS, flash, images, required_pins
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
    parser.add_argument("--flash", choices=ENVS, metavar="ENV",
                        help="flash a prebuilt firmware/esp32 env over the header UART, then exit")
    parser.add_argument("--manual-boot", action="store_true",
                        help="no BOOT wire: hold the ESP32 BOOT button while flashing")
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
        if args.flash:
            checked(client, cmd="select_profile", profile="esp32_safety")
            console.print("Wire Pi GPIO14→RX0, GPIO15←TX0, GPIO25→EN"
                          + ("" if args.manual_boot else ", GPIO24→BOOT (GPIO0)")
                          + f" (BCM {required_pins(args.manual_boot)}), shared GND, 3.3V logic.")
            if input("Wired? Type yes to flash: ").strip() != "yes":
                raise SystemExit(1)
            if args.manual_boot:
                input("Hold the ESP32 BOOT button, then press Enter...")
            result = flash(client, images(args.flash),
                           boot_pin=None if args.manual_boot else FLASH_PINS["boot"],
                           progress=lambda p: console.print(
                               f"{p['name']}: {p['received']}/{p['size']} bytes", markup=False))
            console.print(f"Flashed {args.flash} in {result['seconds']} s: "
                          f"{result.get('build_tag') or 'no build tag'}", markup=False)
            Path(args.report).write_text(json.dumps(result, indent=2) + "\n")
            raise SystemExit(0)
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
