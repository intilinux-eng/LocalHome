"""Screenshots of the home page on a board of simulated devices - to look at
a change to the page without writing a script for it each time.

    python tools/screenshot_home.py OUT_DIR [--width 390] [--width 1280]
                                    [--dark] [--state hourly] [--state all]
                                    [--lang en]

The board: two zones as a real home's - Stanze, with a week (24° at night,
23° by day), and Bagni, left out of the summer - cooling, the simulated
VMC, dehumidifier, outdoor sensor and a light; with --lang en the page in
English and the zones and shutters named in English too (the README's
pictures). States, each a full page:
hourly (as found), details (the rooms' details open), manual / off (the
choice open), held (manual applied for 2 h), away (24 h), covers (four
simulated shutters in config.yaml, one of them off the network: all opened,
one stopped half way, one closing - the page and the card alone, as
covers_card_*.png; with it, the other states have the shutters' card too),
admin (the admin panel's first screen: its overview and menu - the window,
not the whole page), outdoor (the outdoor sensor's sheet over two years of
made-up Turin days, each of its views as outdoor_<view>_*.png - the
window), vmc (the VMC's card alone in each of its cases, as
vmc_<case>_*.png: manual, a program, a speed picked by hand over it, the
maximum for a while, an automatic (a shower), off, the filters' reminder,
the programs open, away). Needs the browser tests' setup: pip install -e
".[dev,ui]", playwright install chromium.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.ui.conftest import CONFIG, _launch  # noqa: E402
from tests.ui.helpers import SIMULATED_ZONE  # noqa: E402

# The names, in the page's language: the two zones, the shutters' shared
# word and their rooms.
NAMES = {
    "it": {"rooms": "Stanze", "baths": "Bagni", "cover": "Tapparella",
           "covers": ("Soggiorno", "Cucina", "Camera", "Studio")},
    "en": {"rooms": "Rooms", "baths": "Bathrooms", "cover": "Shutter",
           "covers": ("Living room", "Kitchen", "Bedroom", "Study")},
}
WEEK = {d: [24.0] * 7 + [23.0] * 17 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
STATES = ["hourly", "details", "manual", "off", "held", "away", "covers", "admin", "outdoor", "vmc"]
OUTDOOR_VIEWS = ["24h", "days", "weeks", "months", "years"]
# The VMC's "day and night" program, as the home page's ready-made one:
# night until 7, day until 22.
VMC_WEEK = {d: [25] * 7 + [50] * 15 + [25] * 2 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def outdoor_days(today: date) -> list[dict]:
    """Two years of made-up days as a Turin balcony has them - about 3° in
    January and 24° in July, a wider day in summer - with a three-day gap,
    as /api/climate/outdoor gives them."""
    days = []
    for back in range(730, 0, -1):
        day = today - timedelta(days=back)
        if 150 <= back <= 152:
            continue
        season = math.sin(2 * math.pi * (day.timetuple().tm_yday - 110) / 365)
        mean = 13.5 + 10.5 * season + 3 * math.sin(back * 1.7) * math.cos(back * 0.31)
        half = 3.5 + 1.5 * season
        low_at = datetime.combine(day, datetime.min.time()) + timedelta(hours=6, minutes=30)
        days.append({"day": day.isoformat(), "mean": round(mean, 2), "min": round(mean - half, 1), "max": round(mean + half, 1),
                     "min_at": int(low_at.timestamp()), "max_at": int((low_at + timedelta(hours=8)).timestamp())})
    return days


def outdoor_line(now: float) -> list[dict]:
    """The last 24 hours, every 5 minutes: lowest at dawn, highest after noon."""
    points = []
    for k in range(288):
        ts = int(now // 300 * 300 - (287 - k) * 300)
        hour = datetime.fromtimestamp(ts).hour + datetime.fromtimestamp(ts).minute / 60
        points.append({"ts": ts, "value": round(11 + 4.5 * math.sin(2 * math.pi * (hour - 10.5) / 24), 2)})
    return points


def vmc_cases(page, call, out: Path, width: int, scheme: str, bathroom: str) -> None:
    """The VMC's card in each of its cases, one after the other on the
    simulated VMC - then as it was: no program, on, at Giorno."""
    speed = next(d for d in call("/api/devices")["sensors"] if d["kind"] == "number" and d.get("presets"))
    name, power = (urllib.parse.quote(n, safe="") for n in (speed["name"], speed["paired_switch"]))
    card = page.locator("#vmc-card")

    def shot(case: str) -> None:
        page.evaluate("refreshDevices()")
        page.wait_for_timeout(800)
        path = out / f"vmc_{case}_{width}_{scheme}.png"
        card.screenshot(path=str(path))
        print(path)

    def plain() -> None:
        if "override" in call("/api/vmc-schedule")["status"]:
            call("/api/vmc-schedule/boost/cancel", {})
        call("/api/vmc-schedule", {"enabled": False, "week": VMC_WEEK})
        call(f"/api/sensors/{power}/on", {})
        call(f"/api/sensors/{name}/set-value", {"value": 50})

    try:
        plain()
        # Cleaned a month ago: how long they have left, under the card.
        call("/api/climate-admin/vmc-filters", {"interval_days": 90,
                                                "cleaned_on": (date.today() - timedelta(days=30)).isoformat()})
        shot("manual")
        call("/api/vmc-schedule", {"enabled": True, "target": speed["name"], "week": VMC_WEEK})
        shot("program")
        call(f"/api/sensors/{name}/set-value", {"value": 62})
        shot("hand")
        call("/api/vmc-schedule/boost", {"device": speed["name"], "value": 100, "minutes": 30})
        shot("timed")
        call("/api/vmc-schedule/boost/cancel", {})
        page.evaluate("refreshDevices()")
        page.wait_for_timeout(800)
        # A shower, as the status would bring it: the page is told, not the board.
        page.evaluate("auto => { state.vmc.status = { ...(state.vmc.status || {}), auto }; renderPlant(); }",
                      {"reason": "shower", "value": 100, "zone": bathroom, "humidity": 78, "baseline": 55})
        path = out / f"vmc_auto_{width}_{scheme}.png"
        card.screenshot(path=str(path))
        print(path)
        call(f"/api/sensors/{power}/off", {})
        shot("off")
        call(f"/api/sensors/{power}/on", {})
        call("/api/climate-admin/vmc-filters", {"cleaned_on": (date.today() - timedelta(days=85)).isoformat()})
        shot("filters")
        page.evaluate("refreshDevices()")
        page.wait_for_timeout(800)
        card.locator("#vmc-plan > summary").click()
        shot("plan")
        page.click("#away-chip")
        page.locator("#sheet button[data-away='24']").click()
        page.wait_for_selector("#vmc-card.vmc-held", timeout=10000)
        shot("away")
    finally:
        if call("/api/climate/zones").get("away_until"):
            call("/api/climate/away/cancel", {})
        plain()
        call("/api/climate-admin/vmc-filters", {"interval_days": 90, "cleaned_on": None})


def covers_config(names: dict) -> str:
    """The shutters' card needs shutters in config.yaml: four simulated ones,
    3 s up or down, the last one off the network."""
    *moving, off = (f'{names["cover"]} {room}' for room in names["covers"])
    return "".join(f'  - kind: cover\n    type: simulated\n    name: "{name}"\n    travel_time_seconds: 3\n'
                   for name in moving) + f'  - kind: cover\n    type: simulated\n    name: "{off}"\n    unreachable: true\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--width", type=int, action="append", help="390 and 1280 if not given")
    parser.add_argument("--dark", action="store_true", help="the dark scheme too")
    parser.add_argument("--state", action="append", choices=STATES + ["all"], help="hourly if not given")
    parser.add_argument("--lang", choices=sorted(NAMES), default="it", help="the page's language (it if not given)")
    args = parser.parse_args()
    widths = args.width or [390, 1280]
    schemes = ["light", "dark"] if args.dark else ["light"]
    states = STATES if not args.state or "all" in args.state else args.state
    args.out.mkdir(parents=True, exist_ok=True)
    names = NAMES[args.lang]
    rooms, cover = names["rooms"], lambda i: f'{names["cover"]} {names["covers"][i]}'

    config = CONFIG.replace('language: "it"', f'language: "{args.lang}"')
    if "covers" in states:
        config = config.replace("integrations:\n", "integrations:\n" + covers_config(names), 1)
    base, proc = _launch(Path(tempfile.mkdtemp()),
                         [dict(SIMULATED_ZONE, name=rooms), dict(SIMULATED_ZONE, name=names["baths"], cool_enabled=False)],
                         config)

    def call(path, body=None):
        req = urllib.request.Request(base + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.load(res)

    def reset():
        call(f"/api/climate/zones/{urllib.parse.quote(rooms)}/override/cancel", {})
        if call("/api/climate/zones").get("away_until"):
            call("/api/climate/away/cancel", {})

    from playwright.sync_api import sync_playwright
    try:
        call(f"/api/climate/zones/{urllib.parse.quote(rooms)}/schedule", {"week": WEEK, "mode": "cool"})
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width in widths:
                for scheme in schemes:
                    for state in states:
                        reset()
                        page = browser.new_page(viewport={"width": width, "height": 900})
                        page.emulate_media(color_scheme=scheme)
                        if state == "admin":
                            page.goto(base + "/climate-admin")
                            page.wait_for_function("() => document.querySelectorAll('.ov-tile .ov-value').length"
                                                   " && ![...document.querySelectorAll('.ov-tile .ov-value')]"
                                                   ".some(v => v.textContent.includes('…'))", timeout=30000)
                            page.wait_for_timeout(1500)
                            shot = args.out / f"{state}_{width}_{scheme}.png"
                            page.screenshot(path=str(shot))
                            print(shot)
                            page.close()
                            continue
                        page.goto(base + "/")
                        page.wait_for_selector(".zone .mode-row", timeout=30000)
                        page.wait_for_timeout(1200)
                        card = page.locator(f'.zone[data-zone="{rooms}"]')
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
                        elif state == "outdoor":
                            # Two years of days and a day's line, made up: a new
                            # board has neither. Each view, the window.
                            days, line = outdoor_days(date.today()), outdoor_line(time.time())
                            page.route("**/api/climate/outdoor", lambda route: route.fulfill(
                                json={"ok": True, "configured": True, "days": days}))
                            page.route("**/api/sensors/Outdoor%20Sensor/history*", lambda route: route.fulfill(
                                json={"ok": True, "series": {"temperature_c": line}}))
                            page.click("#outdoor-chip")
                            page.wait_for_selector("#outdoor-sheet .outdoor-views", timeout=10000)
                            for view in OUTDOOR_VIEWS:
                                page.click(f'#outdoor-sheet button[data-outdoor-view="{view}"]')
                                page.wait_for_timeout(400)
                                shot = args.out / f"outdoor_{view}_{width}_{scheme}.png"
                                page.screenshot(path=str(shot))
                                print(shot)
                            page.close()
                            continue
                        elif state == "vmc":
                            vmc_cases(page, call, args.out, width, scheme, names["baths"])
                            page.close()
                            continue
                        elif state == "covers":
                            # All opened (a full run: each one's place is known),
                            # the second stopped half way down, the third on its way down.
                            page.click('#house-body button[data-cover="all"][data-cover-action="open"]')
                            page.wait_for_selector("#house-body .cover-note", timeout=10000)
                            page.wait_for_timeout(3800)
                            page.click(f'#house-body button[data-cover="{cover(1)}"][data-cover-action="close"]')
                            page.wait_for_timeout(1500)
                            page.click(f'#house-body button[data-cover="{cover(1)}"][data-cover-action="stop"]')
                            page.click(f'#house-body button[data-cover="{cover(2)}"][data-cover-action="close"]')
                            page.wait_for_timeout(900)
                        page.wait_for_timeout(600)
                        shot = args.out / f"{state}_{width}_{scheme}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        print(shot)
                        if state == "covers":
                            card = args.out / f"covers_card_{width}_{scheme}.png"
                            page.locator("#house-body .tile.covers").screenshot(path=str(card))
                            print(card)
                        page.close()
            browser.close()
    finally:
        reset()
        proc.terminate()


if __name__ == "__main__":
    main()
