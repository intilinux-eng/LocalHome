"""A walk on a real board, to run before connecting the loads and after a
deploy: the home page used as a resident would - manual changes of every
kind, back to the week, away and back, winter and summer - the season
changed, and diagnostic mode as an installer uses it; each time, what the
Shelly relays themselves say on the broker, not only what the page says.

    python tools/live_walk.py --board antonio.local          # the plan only
    python tools/live_walk.py --board antonio.local --yes    # run it all
    python tools/live_walk.py --yes --parts diagnostic       # one part

Made for a plant whose Shellys are powered but not yet wired to their
valves - every relay clicks for real, nothing flows - and safe after:
each valve opens for a second or two, the dehumidifier for a few. Besides
what a resident does it switches the season, uses diagnostic mode and,
when the air is too dry for the dehumidifier to start, lowers its start
threshold for the test. It puts back the season, the threshold and
diagnostic mode as it found them, whatever happens; it never edits the
week, other settings, a VMC or a plug; and it stops at once if a manual
change, an absence or diagnostic mode is already on - someone is using
the house.

Needs Playwright (pip install -e ".[ui]") and paho-mqtt.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

PARTS = ("winter", "diagnostic", "seasons", "summer")
PLAN = """\
winter      each zone with a temperature: a target at the room's own temperature
            stays closed (hysteresis); 1.5 degrees above opens; "Off" for 2 hours
            closes; "until changed" opens with no end; back to the week; away with
            a zone opened by hand closes everything and locks the page; back home
diagnostic  from the admin page: on; each relay switched on and off by hand, and
            left on for a whole thermostat cycle without anything undoing it; off
            with a valve left open and the bypass left closed - both put back
seasons     to summer with a zone changed by hand (the change dropped, the bypass
            closing, the bathrooms closed); to winter with the dehumidifier on (it
            stops, its water valve follows the week, the bypass opens); two
            changes in a row
summer      the zone kept out of cooling: no controls, closed; a cooled zone as
            its week says, "Off" closes, a target above the room stays closed;
            the dehumidifier on from the home opens the bathrooms' valve, off
            closes it; away: everything off but the bypass, still closed
at the end  the season, the dehumidifier's threshold and diagnostic mode as found"""

BYPASS, DEHUMIDIFIER = "Bathroom Bypass Valve", "Dehumidifier"


def say(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


class Board:
    """The board's API and its relays on the broker."""

    def __init__(self, host: str):
        self.base = f"http://{host}:5000"
        self.relay_state: dict[str, bool | None] = {}
        self.commands: list[tuple[float, str, str]] = []
        self.results: list[tuple[str, bool]] = []
        maps = self.get("/api/climate-admin/map")["zones"]
        switches = self.get("/api/climate-admin/extra-switches")
        self.relays = {z["valve_device_id"]: f"{z['name']} Valve" for z in maps if z.get("valve_device_id")}
        for key, name in (("bypass_valve_device_id", BYPASS), ("dehumidifier_device_id", DEHUMIDIFIER)):
            if switches.get(key):
                self.relays[switches[key]] = name
        self.poll_seconds = next((s["value"] for s in self.get("/api/climate-admin/settings")["settings"]
                                  if s["key"] == "thermostat_poll_seconds"), 60)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"live-walk-{int(time.time())}")
        self.client.on_message = self._on_message
        self.client.on_connect = lambda c, u, f, r, p=None: [
            c.subscribe(f"{d}/{t}") for d in self.relays for t in ("status/switch:0", "command/switch:0")]
        self.client.connect(host, 1883, 60)
        self.client.loop_start()
        time.sleep(2)
        # Their status isn't kept by the broker: what LocalHome reads of them.
        for name in self.relays.values():
            self.relay_state.setdefault(name, self.get(f"/api/sensors/{urllib.parse.quote(name)}").get("is_on"))

    def has(self, name):
        return name in self.relay_state

    # --------------------------------------------------------------- API --
    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=20) as res:
            return json.load(res)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.load(res)

    def climate(self):
        return self.get("/api/climate/zones")

    def zone(self, name):
        return next(z for z in self.climate()["zones"] if z["name"] == name)

    def season(self, mode):
        say(f"  season -> {'summer' if mode == 'cool' else 'winter'}")
        self.post("/api/climate/mode", {"mode": mode})

    # ------------------------------------------------------------ relays --
    def _on_message(self, client, userdata, msg):
        device, rest = msg.topic.split("/", 1)
        name = self.relays[device]
        text = msg.payload.decode("utf-8", "replace")
        if rest == "status/switch:0":
            try:
                output = json.loads(text).get("output")
            except ValueError:
                return
            if self.relay_state.get(name) != output and not msg.retain:
                say(f"   relay {name}: {'ON' if output else 'off'}")
            self.relay_state[name] = output
        elif rest.startswith("command/") and text != "status_update":
            self.commands.append((time.time(), name, text))
            say(f"   LocalHome -> {name}: {text}")

    def becomes(self, name, output, seconds=40):
        end = time.time() + seconds
        while time.time() < end:
            if self.relay_state.get(name) is output:
                return True
            time.sleep(0.5)
        return False

    def stays(self, name, output, seconds=20):
        """Kept as it is: the relay never says otherwise, and LocalHome
        sends it no command the other way."""
        since, other = time.time(), ("on" if output is False else "off")
        while time.time() < since + seconds:
            if self.relay_state.get(name) is (not output) or any(
                    at >= since and n == name and text.startswith(other) for at, n, text in self.commands):
                return False
            time.sleep(0.5)
        return True

    def check(self, label, ok, extra=""):
        self.results.append((label, bool(ok)))
        say(f"{'PASS' if ok else 'FAIL'} {label}" + (f"  -- {extra}" if extra else ""))

    def week_expectation(self, name):
        """What the thermostat must do with this zone now, by its own
        thresholds: True open, False closed, None either (inside the band
        it keeps doing what it did). A zone out of this season: closed."""
        climate = self.climate()
        z = next(z for z in climate["zones"] if z["name"] == name)
        cool = climate["mode"] == "cool"
        if cool and not z["cool_enabled"]:
            return False
        room, opens, stops = z["temperature_c"], z["valve_opens_at"], z["valve_stops_at"]
        if room is None or opens is None:
            return False
        if (room >= opens) if cool else (room <= opens):
            return True
        if (room <= stops) if cool else (room >= stops):
            return False
        return None

    def zones_as_their_week(self, label):
        for z in self.climate()["zones"]:
            want = self.week_expectation(z["name"])
            if want is None:
                say(f"  {z['name']}: inside its band - its valve may keep its state")
                continue
            self.check(f"{label}: {z['name']} as its week says ({'open' if want else 'closed'})",
                       self.becomes(f"{z['name']} Valve", want))


class Home:
    """The home page, tapped as a resident would."""

    def __init__(self, board: Board, page):
        self.board, self.page = board, page

    def open(self):
        self.page.goto(self.board.base)
        self.page.wait_for_selector(".zone", timeout=30000)
        self.page.wait_for_timeout(1500)

    def refresh(self):
        self.page.evaluate("Promise.all([refreshClimate(), refreshDevices(), refreshProblems()])")
        self.page.wait_for_timeout(800)

    def card(self, name):
        return self.page.locator(f".zone[data-zone='{name}']")

    def state(self, name):
        return self.card(name).locator(".zone-head .state").inner_text().strip()

    def manual(self, name, target=None, hours="2", off=False):
        """A manual change from the card: a target or off, for 2 hours,
        "next" or "manual" (until changed)."""
        self.refresh()
        card = self.card(name)
        card.locator("button[data-action=start][data-delta='0.5']").click()
        if off:
            card.locator(".pending button[data-action=pending-off]").click()
        else:
            # From the value the first tap shows - its target and a half, or
            # off, its usual one - as someone looking at it would.
            shown = card.locator(".pending .stepper-value").inner_text()
            steps = round((target - float(shown.replace("°", "").replace(",", ".").strip())) / 0.5)
            delta = "0.5" if steps >= 0 else "-0.5"
            for _ in range(abs(steps)):
                card.locator(f".pending button[data-action=step][data-delta='{delta}']").click()
        card.locator(f".pending button[data-action=duration][data-hours='{hours}']").click()
        card.locator(".pending button[data-action=apply]").click()
        end = time.time() + 15
        while time.time() < end and self.board.zone(name)["override"] is None:
            time.sleep(0.5)
        override = self.board.zone(name)["override"]
        say(f"  {name}: {'off' if off else f'{target}°'} for {hours} -> {override}")
        self.refresh()
        return override

    def back_to_the_week(self, name):
        self.card(name).locator(".manual-banner button[data-action=resume]").click()
        end = time.time() + 15
        while time.time() < end and self.board.zone(name)["override"] is not None:
            time.sleep(0.5)
        self.refresh()
        return self.board.zone(name)["override"] is None

    def away(self):
        self.page.click("#away-chip")
        self.page.wait_for_timeout(700)
        sheet = self.page.locator("#sheet").inner_text()
        self.page.locator("#sheet button[data-away='24']").click()
        end = time.time() + 15
        while time.time() < end and not self.board.climate()["away_until"]:
            time.sleep(0.5)
        self.refresh()
        return sheet

    def back_home(self):
        self.page.locator(".zone .away-note button[data-action=back]").first.click()
        end = time.time() + 15
        while time.time() < end and self.board.climate()["away_until"]:
            time.sleep(0.5)
        self.refresh()

    def dehumidifier(self, hours=None):
        """The dehumidifier's card: on for `hours`, or off (None). False if
        it offers neither (out of season, or the air dry enough)."""
        self.refresh()
        card = self.page.locator("#season-devices .season-device")
        button = card.locator(f"button[data-dehum-hours='{hours}']" if hours else "button[data-dehum-off]")
        if not button.count():
            return False
        button.click()
        self.page.wait_for_timeout(800)
        return True


class Dehumidifier:
    """Its start threshold lowered for the test when the air is too dry for
    it to start - and put back exactly as it was."""

    def __init__(self, board: Board):
        self.board, self.original = board, None

    def allow(self):
        status = self.board.climate().get("dehumidifier") or {}
        settings = self.board.get("/api/dehumidifier/settings")["settings"]
        humidity = status.get("humidity")
        if humidity is None or humidity >= settings["start_from"]:
            return humidity is not None
        start = max(settings["stop_below"] + 2, int(humidity) - 1)
        if start > humidity:
            say(f"  the air at {humidity}%: too dry to start it even at {start}% - skipped")
            return False
        self.original = settings["start_from"]
        self.board.post("/api/dehumidifier/settings", {"start_from": start})
        say(f"  dehumidifier: start threshold {self.original}% -> {start}% for the test (the air at {humidity}%)")
        return True

    def restore(self):
        if self.original is not None:
            self.board.post("/api/dehumidifier/settings", {"start_from": self.original})
            say(f"  dehumidifier: start threshold back to {self.original}%")
            self.original = None


# ------------------------------------------------------------- the walk --

def zone_week(board, home, name, label):
    want = board.week_expectation(name)
    if want is None:
        say(f"  {name}: inside its band - the valve may keep its state")
        return
    board.check(f"{label}: {name} as its week says ({'open' if want else 'closed'})",
                board.becomes(f"{name} Valve", want), f"card: {home.state(name)!r}")


def winter(board, home):
    say("== WINTER")
    if board.climate()["mode"] != "heat":
        board.season("heat")
        home.open()
    for name in [z["name"] for z in board.climate()["zones"]]:
        z = board.zone(name)
        valve = f"{name} Valve"
        if z["temperature_c"] is None:
            home.manual(name, 28.0)
            board.check(f"{name} without a temperature: 28° asked, stays closed", board.stays(valve, False))
            home.back_to_the_week(name)
            continue
        room = z["temperature_c"]
        same = round(room * 2) / 2
        home.manual(name, same)
        board.check(f"{name} at {same}° with the room at {room}°: stays closed", board.stays(valve, False),
                    f"card: {home.state(name)!r}")
        home.manual(name, same + 1.5)
        board.check(f"{name} at {same + 1.5}°: opens", board.becomes(valve, True), f"card: {home.state(name)!r}")
        home.manual(name, off=True)
        board.check(f"{name} off for 2 h: closes", board.becomes(valve, False), f"card: {home.state(name)!r}")
        home.manual(name, same + 1.5, hours="manual")
        o = board.zone(name)["override"]
        board.check(f"{name} at {same + 1.5}° until changed: opens, no end",
                    board.becomes(valve, True) and o and o["until"] is None)
        home.back_to_the_week(name)
        zone_week(board, home, name, "winter, back to the week")
    first = next((z for z in board.climate()["zones"] if z["temperature_c"] is not None), None)
    if first:
        away_walk(board, home, first["name"], winter=True)


def away_walk(board, home, name, winter):
    say(f"== away, {name} opened by hand")
    room = board.zone(name)["temperature_c"]
    home.manual(name, round(room * 2) / 2 + (1.5 if winter else -1.5))
    board.check(f"{name} opened before going away", board.becomes(f"{name} Valve", True))
    sheet = home.away()
    say("  away sheet: " + " / ".join(line for line in sheet.splitlines() if line.strip())[:300])
    climate = board.climate()
    for z in climate["zones"]:
        board.check(f"away: {z['name']}'s valve closed", board.becomes(f"{z['name']} Valve", False))
    board.check("away: no manual change left", all(z["override"] is None for z in climate["zones"]))
    board.check("away: nothing to change on the cards", home.page.locator(".zone button[data-action=start]").count() == 0)
    if board.has(DEHUMIDIFIER):
        board.check("away: the dehumidifier off", board.becomes(DEHUMIDIFIER, False))
    if board.has(BYPASS):
        board.check(f"away: the bypass as the season wants ({'open' if winter else 'closed'})",
                    board.stays(BYPASS, not winter, 10))
    home.back_home()
    board.check("back home: no absence left", board.climate()["away_until"] is None)
    zone_week(board, home, name, "back home")


def diagnostic(board, page):
    """Diagnostic mode from the admin page, as an installer: every relay by
    hand, nothing undoing it, and all put back when it's switched off."""
    say("== DIAGNOSTIC MODE (admin page)")
    page.goto(board.base + "/climate-admin")
    page.wait_for_timeout(2500)
    page.evaluate("document.getElementById('diagnostic').open = true")
    page.locator("#diagnostic-mode-toggle").check()
    page.locator(".confirm-yes").click()
    end = time.time() + 15
    while time.time() < end and not board.climate()["diagnostic_mode"]:
        time.sleep(0.5)
    board.check("diagnostic mode on", board.climate()["diagnostic_mode"])
    words = page.evaluate("""() => ({open: tr('climate_admin.diag_open_valve'), close: tr('climate_admin.diag_close_valve'),
                                     on: tr('climate_admin.diag_turn_on'), off: tr('climate_admin.diag_turn_off')})""")

    def press(name, word):
        label = page.evaluate("name => displayName(name)", name)
        row = page.locator("#diagnostic-valves .admin-diagnostic-row", has=page.locator(f"strong:text-is('{label}')"))
        row.locator("button", has_text=word).click()
        page.wait_for_timeout(500)

    # Each relay on, kept on for a whole cycle of the thermostat (and the
    # interlock, and the dehumidifier's timer) - nobody undoes it - then off.
    cycle = max(board.poll_seconds, 30) + 5
    for name in [z["name"] + " Valve" for z in board.climate()["zones"]] + [BYPASS, DEHUMIDIFIER]:
        if not board.has(name):
            continue
        valve, normally_open = name != DEHUMIDIFIER, name == BYPASS
        on_word = (words["close"] if normally_open else words["open"]) if valve else words["on"]
        off_word = (words["open"] if normally_open else words["close"]) if valve else words["off"]
        press(name, on_word)
        board.check(f"diagnostic: {name} on by hand", board.becomes(name, True, 15))
        board.check(f"diagnostic: {name} left on for a whole cycle ({cycle} s)", board.stays(name, True, cycle))
        press(name, off_word)
        board.check(f"diagnostic: {name} off by hand", board.becomes(name, False, 15))
    # Leaving it with things as the installer left them: put back at once.
    zones = board.climate()["zones"]
    left_open = next((z["name"] + " Valve" for z in zones if board.week_expectation(z["name"]) is False), None)
    if left_open:
        press(left_open, words["open"])
        board.becomes(left_open, True, 15)
    if board.has(BYPASS) and board.climate()["mode"] == "heat":
        press(BYPASS, words["close"])
        board.becomes(BYPASS, True, 15)
    page.locator("#diagnostic-mode-toggle").uncheck()
    end = time.time() + 15
    while time.time() < end and board.climate()["diagnostic_mode"]:
        time.sleep(0.5)
    board.check("diagnostic mode off", not board.climate()["diagnostic_mode"])
    if left_open:
        board.check(f"diagnostic off: {left_open}, left open, closed at once (its week wants it closed)",
                    board.becomes(left_open, False, 15))
    if board.has(BYPASS) and board.climate()["mode"] == "heat":
        board.check("diagnostic off: the bypass, left closed, opened at once (winter)", board.becomes(BYPASS, False, 15))


def seasons(board, home, dehumidifier):
    say("== SEASON CHANGES")
    if board.climate()["mode"] != "heat":
        board.season("heat")
        home.open()
    # To summer with a zone changed by hand.
    cooled = next((z for z in board.climate()["zones"] if z["cool_enabled"] and z["temperature_c"] is not None), None)
    if cooled:
        home.manual(cooled["name"], round(cooled["temperature_c"] * 2) / 2 + 1.5, hours="manual")
        board.becomes(f"{cooled['name']} Valve", True)
    board.season("cool")
    home.open()
    board.check("to summer: every manual change dropped", all(z["override"] is None for z in board.climate()["zones"]))
    if board.has(BYPASS):
        board.check("to summer: the bypass closes", board.becomes(BYPASS, True))
    board.zones_as_their_week("to summer")
    # To winter with the dehumidifier on.
    if board.has(DEHUMIDIFIER) and dehumidifier.allow() and home.dehumidifier(1):
        board.check("summer: the dehumidifier on", board.becomes(DEHUMIDIFIER, True))
        board.season("heat")
        home.open()
        board.check("to winter: the dehumidifier stops", board.becomes(DEHUMIDIFIER, False))
        board.check("to winter: the home doesn't offer it", home.page.locator("button[data-dehum-hours]").count() == 0)
    else:
        board.season("heat")
        home.open()
    if board.has(BYPASS):
        board.check("to winter: the bypass opens", board.becomes(BYPASS, False))
    board.zones_as_their_week("to winter")
    # Two changes in a row: where it ends is what counts.
    board.season("cool")
    board.season("heat")
    home.open()
    if board.has(BYPASS):
        board.check("summer and back at once: the bypass open", board.becomes(BYPASS, False))
    if board.has(DEHUMIDIFIER):
        board.check("summer and back at once: the dehumidifier off", board.stays(DEHUMIDIFIER, False, 5))
    board.zones_as_their_week("summer and back at once")


def summer(board, home, dehumidifier):
    say("== SUMMER")
    if board.climate()["mode"] != "cool":
        board.season("cool")
        home.open()
    if board.has(BYPASS):
        board.check("summer: the bypass closed (its Shelly on)", board.becomes(BYPASS, True))
    climate = board.climate()
    for z in climate["zones"]:
        name = z["name"]
        if not z["cool_enabled"]:
            board.check(f"summer: {name} kept out - no controls on its card",
                        home.card(name).locator("button[data-action=start]").count() == 0, f"card: {home.state(name)!r}")
            board.check(f"summer: {name}'s valve closed", board.becomes(f"{name} Valve", False))
            continue
        if z["temperature_c"] is None:
            continue
        zone_week(board, home, name, "summer, its week")
        home.manual(name, off=True)
        board.check(f"summer: {name} off for 2 h: closes", board.becomes(f"{name} Valve", False))
        home.manual(name, round(z["temperature_c"] * 2) / 2 + 0.5, hours="manual")
        board.check(f"summer: {name} at a target above the room: stays closed", board.stays(f"{name} Valve", False),
                    f"card: {home.state(name)!r}")
        home.back_to_the_week(name)
        zone_week(board, home, name, "summer, back to the week")
    kept_out = next((z["name"] for z in climate["zones"] if not z["cool_enabled"]), None)
    if board.has(DEHUMIDIFIER):
        if dehumidifier.allow() and home.dehumidifier(1):
            board.check("summer: the dehumidifier on from the home", board.becomes(DEHUMIDIFIER, True))
            if kept_out:
                board.check(f"summer: its water through {kept_out}'s valve - open",
                            board.becomes(f"{kept_out} Valve", True))
            home.dehumidifier(None)
            board.check("summer: the dehumidifier off from the home", board.becomes(DEHUMIDIFIER, False))
            if kept_out:
                board.check(f"summer: {kept_out}'s valve closes with it", board.becomes(f"{kept_out} Valve", False))
            # Away with it on: off, and its valve with it.
            home.dehumidifier(1)
            board.becomes(DEHUMIDIFIER, True)
        else:
            board.check("summer: the dehumidifier shown disabled, and off", board.stays(DEHUMIDIFIER, False, 5))
    cooled = next((z["name"] for z in board.climate()["zones"] if z["cool_enabled"] and z["temperature_c"] is not None), None)
    if cooled:
        away_walk(board, home, cooled, winter=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--board", default="antonio.local")
    parser.add_argument("--parts", default=",".join(PARTS), help=f"some of: {', '.join(PARTS)}")
    parser.add_argument("--yes", action="store_true", help="run it - it drives the real relays")
    args = parser.parse_args(argv)
    parts = [p for p in args.parts.split(",") if p]
    unknown = set(parts) - set(PARTS)
    if unknown:
        parser.error(f"unknown parts: {', '.join(sorted(unknown))}")
    print(PLAN)
    if not args.yes:
        print("\nThe plan only: add --yes to run it on", args.board)
        return 0
    from playwright.sync_api import sync_playwright

    board = Board(args.board)
    start = board.climate()
    say(f"start: season {start['mode']}, relays {board.relay_state}, thermostat every {board.poll_seconds} s")
    if start["away_until"] or start["diagnostic_mode"] or any(z["override"] for z in start["zones"]):
        say("someone is using the house (a manual change, an absence or diagnostic mode): stopping")
        return 2
    dehumidifier = Dehumidifier(board)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 900})
        admin = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        admin.on("pageerror", lambda e: errors.append(str(e)))
        home = Home(board, page)
        try:
            home.open()
            for part in PARTS:
                if part not in parts:
                    continue
                if part == "winter":
                    winter(board, home)
                elif part == "diagnostic":
                    if board.climate()["mode"] != "heat":
                        board.season("heat")
                    diagnostic(board, admin)
                    home.open()
                elif part == "seasons":
                    seasons(board, home, dehumidifier)
                elif part == "summer":
                    summer(board, home, dehumidifier)
            board.check("no script error on the pages", errors == [], str(errors))
        finally:
            # As it was: diagnostic off, nobody away, nothing by hand, the
            # dehumidifier off and its threshold back, the season found.
            if board.climate()["diagnostic_mode"]:
                board.post("/api/climate/diagnostic-mode", {"enabled": False})
            if board.climate()["away_until"]:
                board.post("/api/climate/away/cancel", {})
            for z in board.climate()["zones"]:
                if z["override"]:
                    board.post(f"/api/climate/zones/{urllib.parse.quote(z['name'])}/override/cancel", {})
            if board.relay_state.get(DEHUMIDIFIER):
                board.post("/api/dehumidifier/off", {})
            dehumidifier.restore()
            if board.climate()["mode"] != start["mode"]:
                board.season(start["mode"])
            browser.close()
    end = board.climate()
    board.check("at the end: the season it found, no manual change, nobody away, diagnostic off",
                end["mode"] == start["mode"] and not end["away_until"] and not end["diagnostic_mode"]
                and not any(z["override"] for z in end["zones"]))
    if board.has(BYPASS):
        board.check("at the end: the bypass as the season wants", board.becomes(BYPASS, start["mode"] == "cool"))
    if board.has(DEHUMIDIFIER):
        board.check("at the end: the dehumidifier off", board.becomes(DEHUMIDIFIER, False))
    board.zones_as_their_week("at the end")
    board.client.loop_stop()
    passed = sum(ok for _, ok in board.results)
    say(f"{passed}/{len(board.results)} passed")
    return 0 if passed == len(board.results) else 1


if __name__ == "__main__":
    sys.exit(main())
