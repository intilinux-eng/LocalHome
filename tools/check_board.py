"""A read-only check of a running board, after a deploy: nothing on it is
changed. Every GET endpoint answers - each device's and each zone's too -
and what they say holds: the loops running, the history written, each
answering sensor and valve recorded, each zone's minutes today as its
recorded on/off adds up, no Shelly set otherwise, every protection on.
The static files come versioned, gzipped and kept by the browser, and both
pages open at a phone's and a desk's width, in both schemes, without a
script error, an untranslated text or anything past the screen's edge. On
the home page it opens what a resident opens - each zone's details and
folds, a mode's choice (then Cancel), the info buttons, the table, the
sheets - on the page only: the browser lets no request but a GET through,
so not even a slip can change the house.

    python tools/check_board.py antonio                # http://antonio.local:5000
    python tools/check_board.py 192.168.1.28:5000 --log  # and the board's log since its start

--log reads the board's log from its diagnostics download and lists the
errors since LocalHome started (the download holds a config backup: it's
read in memory, nothing of it saved or printed but those lines). Needs the
browser tests' setup: pip install -e ".[dev,ui]", playwright install chromium.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.ui.helpers import OVERFLOW_JS, UNTRANSLATED_JS  # noqa: E402

ROUTES = ROOT / "src/localhome/web/routes"
# Read-only, but not for a check: one needs an id typed in the panel, the
# other two are downloads (one with the config inside).
SKIP = {"/api/climate-admin/shelly-check", "/api/climate-admin/maintenance/backup.zip",
        "/api/climate-admin/diagnostics.zip"}


class Check:
    def __init__(self, base: str):
        self.base = base
        self.failed: list[str] = []
        self.passed = 0
        self.silent: list[str] = []     # devices that don't answer: their state, not a fault
        self.problems: list[str] = []   # what the board says is wrong - said, for a person to judge

    def ok(self, what: str, good: bool, detail: str = "") -> bool:
        if good:
            self.passed += 1
        else:
            self.failed.append(f"{what}{f' - {detail}' if detail else ''}")
            print(f"  NO  {what}{f' - {detail}' if detail else ''}")
        return good

    def get(self, path: str, raw: bool = False, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read()
            return (res, body) if raw else json.loads(body)


def get_routes() -> list[str]:
    """Every GET endpoint, as this checkout's routes declare them."""
    paths = []
    for module in sorted(ROUTES.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            for deco in node.decorator_list:
                if not (isinstance(deco, ast.Call) and getattr(deco.func, "attr", "") == "route"):
                    continue
                methods = next((ast.literal_eval(k.value) for k in deco.keywords if k.arg == "methods"), ["GET"])
                if "GET" in methods:
                    paths.append(ast.literal_eval(deco.args[0]))
    return [p for p in paths if p not in SKIP]


def check_api(c: Check) -> dict:
    print("API (GET only)")
    devices = c.get("/api/devices")
    zones = c.get("/api/climate/zones").get("zones", [])
    readings = c.get("/api/readings").get("readings", {})
    sensors = [d["name"] for d in devices.get("sensors", [])]
    covers = [d["name"] for d in devices.get("covers", [])]
    meters = [d["name"] for d in devices.get("sensors", [])
              if d.get("kind") == "power_meter" or (readings.get(d["name"]) or {}).get("power_w") is not None]
    fills = {
        "/api/sensors/<name>": [f"/api/sensors/{n}" for n in sensors],
        "/api/sensors/<name>/history": [f"/api/sensors/{z['sensor']}/history?field=temperature_c&range=24h" for z in zones]
                                       + [f"/api/sensors/{z['valve']}/history?field=is_on&range=24h" for z in zones],
        "/api/energy/<name>": [f"/api/energy/{n}" for n in meters],
        "/api/covers/<name>/status": [f"/api/covers/{n}/status" for n in covers],
        "/api/climate/zones/<name>/schedule": [f"/api/climate/zones/{z['name']}/schedule?mode={m}" for z in zones
                                               for m in ("heat", "cool")],
        "/api/climate/zones/<name>/on-hours": [f"/api/climate/zones/{z['name']}/on-hours" for z in zones],
    }
    count = 0
    for route in get_routes():
        if route in ("/", "/climate-admin", "/site.webmanifest"):
            continue
        targets = fills.get(route, [route]) if "<" in route else [route]
        for path in targets:
            url = urllib.parse.quote(path, safe="/?=&")
            try:
                if route.endswith(".csv"):
                    # A download: CSV, its header and a month at least.
                    res, body = c.get(url, raw=True)
                    c.ok(f"GET {path}", res.headers.get("Content-Type", "").startswith("text/csv")
                         and body.count(b"\n") >= 2, res.headers.get("Content-Type", ""))
                    count += 1
                    continue
                data = c.get(url)
                if route == "/api/sensors/<name>" and data.get("ok") is False:
                    # The endpoint answered: the device itself didn't.
                    c.silent.append(f"{path.rsplit('/', 1)[1]}: {str(data.get('error', ''))[:100]}")
                    c.passed += 1
                else:
                    c.ok(f"GET {path}", data.get("ok", True) is not False, str(data.get("error", ""))[:120])
            except urllib.error.HTTPError as e:
                c.ok(f"GET {path}", False, f"HTTP {e.code}")
            except Exception as e:  # noqa: BLE001 - a board that doesn't answer says so
                c.ok(f"GET {path}", False, repr(e)[:120])
            count += 1
    print(f"  {count} answers checked")
    return {"zones": zones}


# The history writes every 5 minutes (services/history.py's
# WRITE_EVERY_SECONDS): LocalHome up longer than this, a valve that
# answers has its on/off in it - off is a 0, not nothing.
HISTORY_DUE_SECONDS = 7 * 60


# A zone's minutes on today, as the page shows them, against its recorded
# on/off added up: this far apart at most (the 5-minute buckets' edges).
MINUTES_SLACK = 5
# The history's chart buckets over 24 hours (services/history.py's RANGE_BUCKETS).
BUCKET_SECONDS = 300


def series(c: Check, sensor: str, field: str) -> list[dict]:
    path = urllib.parse.quote(f"/api/sensors/{sensor}/history?field={field}&range=24h", safe="/?=&")
    return c.get(path).get("series", {}).get(field, [])


def check_values(c: Check, zones: list[dict], clock: dict) -> None:
    """Not that each answer comes - that what it says is right. Found
    cooling for a while with the zone's hours on at 0: the Shelly valves'
    on/off never reached the history, and an empty series answered as well
    as a full one. So: the loops running, the clock and the history going,
    each answering sensor recorded, each valve's minutes today as its
    recorded on/off adds up, no Shelly set otherwise, every protection on."""
    print("Values")
    status = c.get("/api/system-status")
    now = clock["epoch_ms"] / 1000
    for loop in status.get("loops", []):
        late = loop.get("last_ok_seconds_ago")
        c.ok(f"the {loop['name']} loop runs", not loop.get("stalled") and late is not None
             and late <= 3 * loop.get("interval_seconds", 30), f"last cycle {late} s ago")
    off = [name for name, on in status.get("protections", {}).get("items", {}).items() if not on]
    c.ok("every protection of the board on", not off, ", ".join(off))
    wrong = [f"{d['id']}: {', '.join(s['key'] for s in d['settings_wrong'])}"
             for d in c.get("/api/climate-admin/shelly-health").get("devices", []) if d.get("settings_wrong")]
    c.ok("no Shelly with a setting to put right", not wrong, "; ".join(wrong))
    c.problems += [f"{p.get('code')}: {str(p.get('detail') or p.get('subject') or '')[:100]}"
                   for p in status.get("problems", [])]
    up = now - clock.get("started_at", 0)
    database = status.get("database", {})
    c.ok("the clock synchronized, the history recording", clock.get("clock_synced") and not database.get("paused_for_clock"))
    if up < HISTORY_DUE_SECONDS:
        print(f"  the history not checked: LocalHome up {up / 60:.0f} min, its first write is due after "
              f"{HISTORY_DUE_SECONDS // 60}")
        return
    # Its figures are counted every few minutes (what was waiting written
    # first): its newest sample against when they were counted.
    counted_at = now - (database.get("stats_age_seconds") or 0)
    newest = database.get("newest_ts") or 0
    c.ok("the history writing", counted_at - newest <= 6 * 60,
         f"newest sample {(counted_at - newest) / 60:.0f} min before its figures were counted")
    readings = c.get("/api/readings").get("readings", {})
    midnight = now - (now + clock.get("utc_offset_minutes", 0) * 60) % 86400
    for zone in zones:
        sensor, valve = zone["sensor"], zone["valve"]
        if (readings.get(sensor) or {}).get("ok"):
            temps = series(c, sensor, "temperature_c")
            age = now - temps[-1]["ts"] if temps else None
            c.ok(f"{sensor}'s temperature recorded", age is not None and age <= 15 * 60,
                 "nothing in the last 24 hours" if age is None else f"last {age / 60:.0f} min ago")
        if not (readings.get(valve) or {}).get("ok"):
            continue
        runs = series(c, valve, "is_on")
        if not c.ok(f"the history records {valve}'s on/off", bool(runs), "nothing in the last 24 hours"):
            continue
        # Each bucket's mean is the share of it the valve was open; the one
        # still filling counts for its time so far.
        recorded = sum(p["value"] * min(BUCKET_SECONDS, now - p["ts"]) for p in runs if p["ts"] >= midnight) / 60
        name = urllib.parse.quote(zone["name"])
        shown = c.get(f"/api/climate/zones/{name}/on-hours").get("today", 0) * 60
        c.ok(f"{zone['name']}'s minutes on today as recorded", abs(shown - recorded) <= MINUTES_SLACK,
             f"shown {shown:.0f} min, recorded {recorded:.0f} min")


def check_static(c: Check) -> None:
    print("Static files")
    clock = c.get("/api/server-time")
    res, body = c.get("/", raw=True)
    page = body.decode("utf-8")
    scripts = re.findall(r'src="(/static/[^"]+)"', page)
    c.ok("the home page lists its scripts", len(scripts) >= 5, str(scripts))
    for src in scripts:
        res, body = c.get(src, raw=True, headers={"Accept-Encoding": "gzip"})
        cache = res.headers.get("Cache-Control", "")
        version = urllib.parse.parse_qs(urllib.parse.urlparse(src).query).get("v", [""])[0]
        c.ok(f"{src.split('?')[0]}: this version's", version == str(clock.get("app_version")), version)
        c.ok(f"{src.split('?')[0]}: kept by the browser", "immutable" in cache, cache)
        if res.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        c.ok(f"{src.split('?')[0]}: a script", len(body) > 100)


def check_pages(c: Check, zones: list[dict]) -> None:
    from playwright.sync_api import sync_playwright
    print("Pages (nothing but GETs leaves the browser)")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for path in ("/", "/climate-admin"):
            for width in (390, 1280):
                for scheme in ("light", "dark"):
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    page.emulate_media(color_scheme=scheme)
                    errors, sent = [], []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

                    def only_reads(route, sent=sent):
                        if route.request.method in ("GET", "HEAD"):
                            route.continue_()
                        else:
                            sent.append(f"{route.request.method} {route.request.url}")
                            route.abort()
                    page.route("**/*", only_reads)
                    page.goto(c.base + path)
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1500)
                    where = f"{path} {width}px {scheme}"
                    if path == "/" and width == 390 and scheme == "light":
                        walk_home(c, page, zones)
                    c.ok(f"{where}: no script error", not errors, "; ".join(errors[:3]))
                    c.ok(f"{where}: nothing untranslated", page.evaluate(UNTRANSLATED_JS) == [],
                         str(page.evaluate(UNTRANSLATED_JS)))
                    overflow = page.evaluate(OVERFLOW_JS)
                    c.ok(f"{where}: nothing past the screen's edge", overflow <= 0, f"{overflow}px")
                    c.ok(f"{where}: nothing but reads tried", not sent, "; ".join(sent[:3]))
                    context.close()
        browser.close()


def walk_home(c: Check, page, zones: list[dict]) -> None:
    """What a resident opens, on the page only."""
    for zone in zones:
        card = page.locator(f'.zone[data-zone="{zone["name"]}"]')
        if not c.ok(f"{zone['name']}: its card", card.count() == 1):
            continue
        more = card.locator(".zone-more")
        if more.count() and more.get_attribute("open") is None:
            more.locator(":scope > summary").click()
            page.wait_for_timeout(300)
        for fold in (".zone-runs", ".zone-temps"):
            if card.locator(fold).count():
                card.locator(f"{fold} > summary").click()
                page.wait_for_timeout(200)
        if card.locator(".mode-row").count():
            for mode in ("manual", "off"):
                card.locator(f"button[data-action=mode-{mode}]").click()
                opened = card.locator(".pending").count() == 1
                card.locator("button[data-action=cancel-pending]").click()
                page.wait_for_timeout(200)
                c.ok(f"{zone['name']}: {mode}'s choice opens and Cancel closes it",
                     opened and card.locator(".pending").count() == 0)
            card.locator("button[data-action=quick-info]").click()
            c.ok(f"{zone['name']}: the modes' i", card.locator(".quick-hint").count() == 1)
            if card.locator("button[data-action=show-plan]").count():
                card.locator("button[data-action=show-plan]").click()
                page.wait_for_timeout(600)
                c.ok(f"{zone['name']}: 'See the table' opens it", card.locator(".sched-grid").is_visible())
    for chip in ("#season-chip", "#health", "#away-chip"):
        if page.locator(chip).is_visible():
            page.click(chip)
            page.wait_for_timeout(300)
            shown = page.locator("#sheet").is_visible()
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            c.ok(f"{chip}'s sheet opens and closes", shown and page.locator("#sheet").is_hidden())


def check_log(c: Check) -> None:
    print("The board's log since LocalHome started")
    started = c.get("/api/server-time").get("started_at")
    _, body = c.get("/api/climate-admin/diagnostics.zip", raw=True)
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        log = z.read("localhome.log").decode("utf-8", errors="replace")
    since = datetime.fromtimestamp(started).astimezone() if started else None
    bad = []
    for line in log.splitlines():
        stamp = line[:25]
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if since and when < since:
            continue
        if re.search(r"ERROR|Traceback|CRITICAL", line):
            bad.append(line[:220])
    c.ok("no error in the log since the start", not bad, f"{len(bad)} line(s)")
    for line in bad[:15]:
        print("      " + line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("board", help="a board's name (antonio) or host:port")
    parser.add_argument("--log", action="store_true", help="and its log since LocalHome started")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")    # devices' names as written, on Windows' console too
    host = args.board if ":" in args.board or "." in args.board else f"{args.board}.local:5000"
    c = Check(f"http://{host}")
    clock = c.get("/api/server-time")
    print(f"{host}: version {clock.get('app_version')}, up since "
          f"{datetime.fromtimestamp(clock['started_at']):%Y-%m-%d %H:%M}" if clock.get("started_at") else host)
    zones = check_api(c)["zones"]
    check_values(c, zones, clock)
    check_static(c)
    check_pages(c, zones)
    if args.log:
        check_log(c)
    if c.silent:
        print("\nDevices not answering (their state, said by the board - not a fault of the pages):")
        for line in c.silent:
            print(f"  - {line}")
    if c.problems:
        print("\nProblems the board shows (in the admin too - to judge, not counted here):")
        for line in c.problems:
            print(f"  - {line}")
    print(f"\n{c.passed} checks passed, {len(c.failed)} not")
    sys.exit(1 if c.failed else 0)


if __name__ == "__main__":
    main()
