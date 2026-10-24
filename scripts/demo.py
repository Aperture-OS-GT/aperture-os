"""One-process lifecycle management; daemon still uses real TCP JSON-lines."""
import json
import subprocess
import sys
import threading
from pathlib import Path

from agent.client import Client
from pi.backends import SimBackend
from pi.daemon import Harness, Server


def main():
    with Server(("127.0.0.1", 0), Harness(SimBackend())) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run([sys.executable, "-m", "agent.main", "--port",
                                     str(server.server_address[1]), "--report", "demo-report.json"],
                                    check=False)
            report = json.loads(Path("demo-report.json").read_text())
            caught = any("DISCREPANCY" in issue for step in report["steps"]
                         for issue in step["issues"])
            if result.returncode != 1 or not caught:
                raise SystemExit("Demo failed: expected a detected discrepancy")
            with Client(port=server.server_address[1]) as client:
                assert client.command(cmd="ping")["pong"]
            print("DEMO SUCCESS: planted firmware lie caught via physical readback.")
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
