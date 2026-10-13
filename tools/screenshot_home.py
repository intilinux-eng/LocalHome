"""Screenshots of the home page on a board of simulated devices - to look at
a change to the page without writing a script for it each time.

    python tools/screenshot_home.py OUT_DIR [--width 390] [--width 1280]
                                    [--dark] [--state hourly] [--state all]

The board: two zones as a real home's - Stanze, with a week (24° at night,
23° by day), and Bagni, left out of the summer - cooling, the simulated
VMC, dehumidifier, outdoor sensor and a light. States, each a full page:
hourly (as found), details (the rooms' details open), manual / off (the
choice open), held (manual applied for 2 h), away (24 h). Needs the
browser tests' setup: pip install -e ".[dev,ui]", playwright install chromium.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.ui.conftest import CONFIG, _launch  # noqa: E402
from tests.ui.helpers import SIMULATED_ZONE  # noqa: E402

ROOMS, BATHS = "Stanze", "Bagni"
WEEK = {d: [24.0] * 7 + [23.0] * 17 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
STATES = ["hourly", "details", "manual", "off", "held", "away"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--width", type=int, action="append", help="390 and 1280 if not given")
    parser.add_argument("--dark", action="store_true", help="the dark scheme too")
    parser.add_argument("--state", action="append", choices=STATES + ["all"], help="hourly if not given")
    args = parser.parse_args()
    widths = args.width or [390, 1280]
    schemes = ["light", "dark"] if args.dark else ["light"]
    states = STATES if not args.state or "all" in args.state else args.state
    args.out.mkdir(parents=True, exist_ok=True)

    base, proc = _launch(Path(tempfile.mkdtemp()),
                         [dict(SIMULATED_ZONE, name=ROOMS), dict(SIMULATED_ZONE, name=BATHS, cool_enabled=False)], CONFIG)

    def call(path, body=None):
        req = urllib.request.Request(base + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.load(res)

    def reset():
        call(f"/api/climate/zones/{urllib.parse.quote(ROOMS)}/override/cancel", {})
        if call("/api/climate/zones").get("away_until"):
            call("/api/climate/away/cancel", {})

    from playwright.sync_api import sync_playwright
    try:
        call(f"/api/climate/zones/{urllib.parse.quote(ROOMS)}/schedule", {"week": WEEK, "mode": "cool"})
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width in widths:
                for scheme in schemes:
                    for state in states:
                        reset()
                        page = browser.new_page(viewport={"width": width, "height": 900})
                        page.emulate_media(color_scheme=scheme)
                        page.goto(base + "/")
                        page.wait_for_selector(".zone .mode-row", timeout=30000)
                        page.wait_for_timeout(1200)
                        card = page.locator(f'.zone[data-zone="{ROOMS}"]')
                        if state == "details" and card.locator(".zone-more").get_attribute("open") is None:
                            card.locator(".zone-more > summary").click()
                        elif state in ("manual", "off"):
                            card.locator(f"button[data-action=mode-{state}]").click()
                        elif state == "held":
                            card.locator("button[data-action=mode-manual]").click()
                            card.locator(".pending button[data-hours='2']").click()
                            card.locator("button[data-action=apply]").click()
                            page.wait_for_selector(".zone .manual-banner", timeout=10000)
                        elif state == "away":
                            page.click("#away-chip")
                            page.locator("#sheet button[data-away='24']").click()
                            page.wait_for_selector(".zone .away-note", timeout=10000)
                        page.wait_for_timeout(600)
                        shot = args.out / f"{state}_{width}_{scheme}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        print(shot)
                        page.close()
            browser.close()
    finally:
        reset()
        proc.terminate()


if __name__ == "__main__":
    main()
