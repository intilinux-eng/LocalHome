"""The meters' own past days back into a board's history - run it after
"Cancella storico": a Meross plug keeps about a month of daily kWh by
itself (what the Meross app shows), and the board reads them with its own
credentials - nothing of them on this PC. The board also does it once a
day by itself; this is for not waiting.

    python tools/import_meter_history.py antonio              # http://antonio.local:5000
    python tools/import_meter_history.py 192.168.1.28:5000

Only days before today, as the device counted them; today stays the
board's own, from its samples. A meter whose device keeps no days (a
Sonoff POWCT through eWeLink) isn't touched.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def call(base: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        return json.load(e)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("board", help="a board's name (antonio) or host:port")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")    # meters' names as written, on Windows' console too
    host = args.board if ":" in args.board or "." in args.board else f"{args.board}.local:5000"
    base = f"http://{host}"
    answer = call(base, "/api/energy/import", {})
    if not answer.get("ok"):
        print(f"not imported: {answer.get('error') or answer}")
        print("the device's cloud didn't answer, or its plug isn't connected yet - try again in a few minutes")
        return 1
    imported = answer.get("imported", {})
    if not imported:
        print(f"{host}: no meter that keeps its own days")
        return 0
    for meter, written in imported.items():
        days = call(base, f"/api/energy/{urllib.parse.quote(meter)}")
        week = (days.get("week") or {}).get("kwh")
        month = (days.get("month") or {}).get("kwh")
        print(f"{meter}: {written} day(s) written - this week {week} kWh, this month {month} kWh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
