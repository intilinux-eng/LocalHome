"""A walk on a real board, to run before connecting the loads and after a
deploy: the home page used as a resident would - manual changes of every
kind, back to the week, away and back, winter and summer - the season
changed, and diagnostic mode as an installer uses it; each time, what the
Shelly relays themselves say on the broker, not only what the page says.

    python tools/live_walk.py --board antonio.local          # the plan only
    python tools/live_walk.py --board antonio.local --yes    # run it all
    python tools/live_walk.py --yes --parts diagnostic       # one part
    python tools/live_walk.py --yes --parts reboot           # then reboot it by hand

Made for a plant whose Shellys are powered but not yet wired to their
valves - every relay clicks for real, nothing flows - and safe after:
each valve opens for a second or two, the dehumidifier for a few. Besides
what a resident does it switches the season, uses diagnostic mode, sets
a week of its own for the "away" part (so that the hour asks for heat)
and, when the air is too dry for the dehumidifier to start, lowers its
start threshold for the test. It puts back the season, the weeks, the
threshold and diagnostic mode as it found them, whatever happens; it
never touches other settings, a VMC or a plug; and it stops at once if a
manual change, an absence or diagnostic mode is already on - someone is
using the house.

Needs Playwright (pip install -e ".[ui]") and paho-mqtt.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

PARTS = ("winter", "diagnostic", "seasons", "summer", "away")
# Run only when named: they restart LocalHome, and with it the VMC's driver;
# "reboot" waits for someone to reboot or unplug the board.
EXTRA_PARTS = ("restart", "reboot")
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
away        away and home again and again, with the week asking for heat and with
            no week: from a card and from the sheet, for a day and until a time two
            minutes ahead (it ends by itself); every kind of manual change before
            (dropped); while away, a manual change, the week and a valve switched
            from outside all refused or undone - a stale second phone too; the
            season and diagnostic mode changed while away; rapid toggles
at the end  the season, the weeks, the dehumidifier's threshold and diagnostic mode
            as found
restart     only when named: LocalHome restarted from the admin while away - still
            away after it until the same time, the valves closed though the week asks
            for heat, nothing opened meanwhile; the settings, the plant, the weeks and
            the VMC as they were, every device answering again, no new problem; back
            home, the week
reboot      only when named, and by hand: the same with the whole board rebooted - it
            waits for "sudo reboot" over SSH, or the board unplugged and plugged back
            in (a power cut while nobody's home); its clock synchronized again"""

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
        self.saved_weeks: dict = {}
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
        # The broker may be the board's own: back within seconds of a reboot.
        self.client.reconnect_delay_set(1, 4)
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
        if path == "/api/climate/away":
            body = {**body, "vmc": "keep"}      # the VMC stays as it is - see keep_vmc()
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.load(res)

    def climate(self):
        return self.get("/api/climate/zones")

    def zone(self, name):
        return next(z for z in self.climate()["zones"] if z["name"] == name)

    def week(self, name, mode):
        return self.get(f"/api/climate/zones/{urllib.parse.quote(name)}/schedule?mode={mode}")["week"]

    def set_week(self, name, mode, week):
        """A week of the test's own - the real one kept first, put back by
        restore_weeks()."""
        key = (name, mode)
        if key not in self.saved_weeks:
            self.saved_weeks[key] = self.week(name, mode)
        self.post(f"/api/climate/zones/{urllib.parse.quote(name)}/schedule", {"week": week, "mode": mode})

    def restore_weeks(self):
        for (name, mode), week in self.saved_weeks.items():
            self.post(f"/api/climate/zones/{urllib.parse.quote(name)}/schedule", {"week": week, "mode": mode})
        back = all(self.week(name, mode) == week for (name, mode), week in self.saved_weeks.items())
        if self.saved_weeks:
            say(f"  weeks put back as they were: {back}")
        self.saved_weeks.clear()
        return back

    def now(self):
        """The board's wall clock - what its calendar and its week go by."""
        from datetime import datetime, timedelta, timezone
        t = self.get("/api/server-time")
        return datetime.fromtimestamp(t["epoch_ms"] / 1000, timezone.utc).replace(tzinfo=None) \
            + timedelta(minutes=t["utc_offset_minutes"])

    def refused(self, path, body, code):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20):
                return False
        except urllib.error.HTTPError as err:
            return json.load(err).get("code") == code

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


def keep_vmc(page):
    """The page's absences leave the VMC as it is - the one device in real
    use: the away sheet asks for its night speed or off, this asks the
    board to keep it."""
    def keep(route, request):
        if request.method != "POST":
            route.continue_()
            return
        body = json.loads(request.post_data or "{}")
        route.continue_(post_data=json.dumps({**body, "vmc": "keep"}))
    page.route("**/api/climate/away", keep)


class Home:
    """The home page, tapped as a resident would."""

    def __init__(self, board: Board, page):
        self.board, self.page = board, page
        keep_vmc(page)

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

    def away_until(self, day, hhmm):
        """Away until a day and time picked in the sheet's calendar."""
        self.page.click("#away-chip")
        self.page.wait_for_timeout(700)
        self.page.fill("#away-day", day)
        self.page.fill("#away-time", hhmm)
        self.page.locator("#sheet button[data-away='day']").click()
        end = time.time() + 15
        while time.time() < end and not self.board.climate()["away_until"]:
            time.sleep(0.5)
        self.refresh()

    def back_home_from_sheet(self):
        self.page.click("#away-chip")
        self.page.wait_for_timeout(700)
        self.page.locator("#sheet button[data-away='back']").click()
        end = time.time() + 15
        while time.time() < end and self.board.climate()["away_until"]:
            time.sleep(0.5)
        self.refresh()

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


def away_and_home(board, home, browser):
    """Away and home again and again - looking for what breaks."""
    say("== AWAY AND HOME, MANY TIMES")
    if board.climate()["mode"] != "heat":
        board.season("heat")
        home.open()
    zones = [z for z in board.climate()["zones"] if z["temperature_c"] is not None]
    if len(zones) < 2:
        say("  needs two zones with a temperature - skipped")
        return
    warm, bare = zones[0]["name"], zones[1]["name"]
    hour = board.now().hour

    def week(target=None, hours=3):
        """This hour and the next ones at `target`, the rest of the week off."""
        days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        return {d: [target if hour <= h < hour + hours else None for h in range(24)] for d in days}

    heat_target = round((board.zone(warm)["temperature_c"] + 2) * 2) / 2
    board.set_week(warm, "heat", week(heat_target))     # the hour asks for heat
    board.set_week(bare, "heat", week())                 # no week at all
    home.open()
    board.check(f"a week asking for {heat_target}°: {warm} opens", board.becomes(f"{warm} Valve", True))
    board.check(f"no week: {bare} closed", board.becomes(f"{bare} Valve", False))

    def both_closed(label):
        board.check(f"{label}: {warm} closed", board.becomes(f"{warm} Valve", False))
        board.check(f"{label}: {bare} closed", board.becomes(f"{bare} Valve", False))

    def as_home(label):
        board.check(f"{label}: nobody away", board.climate()["away_until"] is None)
        board.check(f"{label}: {warm} open again (its week)", board.becomes(f"{warm} Valve", True))
        board.check(f"{label}: {bare} still closed (no week)", board.stays(f"{bare} Valve", False, 3))

    # 1. Away from the sheet, with a stale second phone; refusals; back from a card.
    stale = browser.new_page(viewport={"width": 390, "height": 900})
    keep_vmc(stale)
    stale.goto(board.base)
    stale.wait_for_selector(".zone", timeout=30000)
    stale.wait_for_timeout(1000)
    home.away()
    both_closed("away for a day")
    board.check("away: the title says until when", home.page.inner_text("#hero-title").startswith("Fuori casa"))
    board.check("away: the week can't be painted (locked)",
                home.page.locator(f".zone[data-zone='{warm}'] .zone-plan").count() == 0
                or home.page.locator(f".zone[data-zone='{warm}'] .plan-edit[inert], .zone[data-zone='{warm}'] .zone-plan[hidden]").count() > 0
                or not home.page.locator(f".zone[data-zone='{warm}'] .zone-plan").is_visible())
    board.check("away: a manual change refused by the board",
                board.refused(f"/api/climate/zones/{urllib.parse.quote(warm)}/override", {"target": 26, "hours": 1},
                              "override_while_away"))
    board.check("away: the week refused by the board",
                board.refused(f"/api/climate/zones/{urllib.parse.quote(warm)}/schedule",
                              {"week": week(heat_target), "mode": "heat"}, "schedule_while_away"))
    # A valve switched on from outside the page (another app, a script):
    # the thermostat closes it again within a cycle.
    board.post(f"/api/sensors/{urllib.parse.quote(warm + ' Valve')}/on", {})
    board.becomes(f"{warm} Valve", True, 10)
    board.check(f"away: {warm} switched on from outside, closed again within a cycle",
                board.becomes(f"{warm} Valve", False, board.poll_seconds + 15))
    # The stale phone still shows the cards: its tap is refused, and said.
    card = stale.locator(f".zone[data-zone='{warm}']")
    if card.locator("button[data-action=start][data-delta='0.5']").count():
        card.locator("button[data-action=start][data-delta='0.5']").click()
        card.locator(".pending button[data-action=duration][data-hours='2']").click()
        card.locator(".pending button[data-action=apply]").click()
        stale.wait_for_timeout(2500)
        message = stale.evaluate("tr('errors.override_while_away')")
        board.check("a stale phone's tap while away: refused, and it says why", message in stale.inner_text("body"))
        board.check("the stale phone's tap changed nothing", board.zone(warm)["override"] is None
                    and board.stays(f"{warm} Valve", False, 3))
    stale.close()
    home.back_home()
    as_home("back from a card")

    # 2. Every kind of manual change, then away: all dropped; back: the week.
    for label, kwargs in (("27° for 2 h", {"target": heat_target + 1, "hours": "2"}),
                          ("off until the next change", {"off": True, "hours": "next"}),
                          ("27° until changed", {"target": heat_target + 1, "hours": "manual"}),
                          ("off for 2 h", {"off": True, "hours": "2"})):
        home.manual(warm, **kwargs)
        home.manual(bare, target=heat_target + 1, hours="2")
        board.becomes(f"{bare} Valve", True)
        home.away()
        both_closed(f"away after {label}")
        board.check(f"away after {label}: every manual change dropped",
                    all(z["override"] is None for z in board.climate()["zones"]))
        home.back_home_from_sheet()
        as_home(f"back after {label}")

    # 3. Rapid toggles, as two phones at once: where it ends is what counts.
    for _ in range(3):
        board.post("/api/climate/away", {"hours": 24})
        board.post("/api/climate/away/cancel", {})
    home.refresh()
    as_home("away and back three times in a row")
    board.post("/api/climate/away", {"hours": 24})
    board.post("/api/climate/away/cancel", {})
    board.post("/api/climate/away", {"hours": 72})
    home.refresh()
    both_closed("ending away after quick changes")
    board.post("/api/climate/away/cancel", {})
    home.refresh()
    as_home("and back")

    # 4. The season changed while away: still away, all closed, the bypass
    # with the new season; back: the new season's week.
    home.away()
    board.season("cool")
    home.open()
    board.check("season changed while away: still away", board.climate()["away_until"] is not None)
    both_closed("summer while away")
    if board.has(BYPASS):
        board.check("summer while away: the bypass closes", board.becomes(BYPASS, True))
    board.season("heat")
    home.open()
    board.check("winter again while away: still away", board.climate()["away_until"] is not None)
    if board.has(BYPASS):
        board.check("winter again while away: the bypass opens", board.becomes(BYPASS, False))
    both_closed("winter again while away")

    # 5. Diagnostic mode while away: the installer's hand wins; off: away again.
    board.post("/api/climate/diagnostic-mode", {"enabled": True})
    board.post(f"/api/sensors/{urllib.parse.quote(warm + ' Valve')}/on", {})
    board.check("diagnostic while away: a valve switched on by hand", board.becomes(f"{warm} Valve", True, 15))
    board.check("diagnostic while away: left on for a cycle", board.stays(f"{warm} Valve", True, board.poll_seconds + 5))
    board.post("/api/climate/diagnostic-mode", {"enabled": False})
    board.check("diagnostic off while away: the valve closed at once", board.becomes(f"{warm} Valve", False, 15))
    home.open()
    home.back_home()
    as_home("back after all that")

    # 6. Away until a time two minutes ahead, from the calendar: it ends by
    # itself and the week takes over.
    until = board.now().replace(second=0, microsecond=0)
    from datetime import timedelta
    until += timedelta(minutes=3)
    home.away_until(until.strftime("%Y-%m-%d"), until.strftime("%H:%M"))
    away = board.climate()["away_until"]
    board.check(f"away until {until:%H:%M} from the calendar", away is not None and away.startswith(until.strftime("%Y-%m-%dT%H:%M")),
                str(away))
    both_closed("away until a time")
    wait = (until - board.now()).total_seconds() + board.poll_seconds + 10
    say(f"  waiting {wait:.0f} s for the absence to end by itself")
    end = time.time() + wait + 30
    while time.time() < end and board.climate()["away_until"]:
        time.sleep(2)
    # The page left open says so by itself, at its own next refresh.
    end = time.time() + 30
    while time.time() < end and home.page.inner_text("#hero-title").startswith("Fuori casa"):
        time.sleep(1)
    board.check("its end: a page left open stops saying away by itself",
                not home.page.inner_text("#hero-title").startswith("Fuori casa"))
    as_home("the absence ended by itself")


def kept(board):
    """What a restart must leave as it was: the settings, the plant, the
    weeks (not the history's figures, beside the settings)."""
    out = {"settings": board.get("/api/climate-admin/settings")["settings"]}
    for path in ("/api/climate-admin/map", "/api/climate-admin/extra-switches", "/api/climate-admin/extra-sensors",
                 "/api/climate-admin/extra-dimmers", "/api/climate-admin/vmc-speeds",
                 "/api/climate-admin/season-rules", "/api/vmc-schedule", "/api/dehumidifier/settings"):
        out[path] = board.get(path)
    for z in board.climate()["zones"]:
        for mode in ("heat", "cool"):
            out[f"week {z['name']} {mode}"] = board.week(z["name"], mode)
    return out


def answering(board):
    """The devices LocalHome reads fine, and how it reads the VMC."""
    devices = {d["name"]: board.get(f"/api/sensors/{urllib.parse.quote(d['name'])}")
               for d in board.get("/api/devices")["sensors"]}
    vmc = {n: {k: d.get(k) for k in ("is_on", "value")} for n, d in devices.items() if n.startswith("VMC")}
    return {n for n, d in devices.items() if d.get("ok")}, vmc


def problems(board):
    return {(p["code"], p["subject"]) for p in board.get("/api/problems")["problems"]}


def back_within(seconds, ready):
    """Seconds until ready() holds - None if it never does in time."""
    since = time.time()
    while time.time() < since + seconds:
        try:
            if ready():
                return time.time() - since
        except (OSError, ValueError):
            pass
        time.sleep(2)
    return None


def restarted_while_away(board, home, how):
    """Away with the week asking for heat, and then LocalHome restarted
    from the admin ("restart") or the whole board rebooted or unplugged by
    hand ("reboot": a power cut while nobody's home)."""
    say(f"== {'LOCALHOME RESTARTED' if how == 'restart' else 'THE BOARD REBOOTED'} WHILE AWAY")
    if board.climate()["mode"] != "heat":
        board.season("heat")
        home.open()
    zones = [z for z in board.climate()["zones"] if z["temperature_c"] is not None]
    if len(zones) < 2:
        say("  needs two zones with a temperature - skipped")
        return
    warm, bare = zones[0]["name"], zones[1]["name"]
    hour = board.now().hour
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    target = round((board.zone(warm)["temperature_c"] + 2) * 2) / 2
    board.set_week(warm, "heat", {d: [target if hour <= h < hour + 3 else None for h in range(24)] for d in days})
    board.set_week(bare, "heat", {d: [None] * 24 for d in days})
    home.open()
    board.check(f"before: {warm}'s week asks for {target}° - open", board.becomes(f"{warm} Valve", True))
    home.away()
    board.check(f"away: {warm} closed", board.becomes(f"{warm} Valve", False))
    away_until = board.climate()["away_until"]
    settings_before = kept(board)
    ok_before, vmc_before = answering(board)
    problems_before = problems(board)
    say(f"  answering: {len(ok_before)} devices; VMC {vmc_before}; problems {sorted(problems_before)}")
    clock = board.get("/api/server-time")
    key = "started_at" if how == "restart" else "booted_at"
    since = time.time()
    if how == "restart":
        say("  restarting LocalHome")
        board.post("/api/climate-admin/restart", {})
    else:
        say(f"  >>> now reboot {board.base.split('//')[1].split(':')[0]}: sudo reboot over SSH, or unplug it "
            "and plug it back in - waiting up to 15 minutes")
    down_at, back = None, None
    while time.time() < since + (60 * 15 if how == "reboot" else 180):
        time.sleep(1)
        try:
            now = board.get("/api/server-time")
        except (OSError, ValueError):
            down_at = down_at or time.time()
            continue
        if now[key] != clock[key]:
            back = time.time() - (down_at or since)
            break
    board.check(f"{'LocalHome' if how == 'restart' else 'the board'} back", back is not None,
                f"LocalHome answering {back:.0f} s after it went down" if back is not None else "not seen in time")
    if back is None:
        return
    if how == "reboot":
        boots = board.get("/api/system-status").get("boots") or []
        if len(boots) > 1:
            say(f"  the board's last boot ended: {boots[1].get('ended')}")
        synced = back_within(300, lambda: board.get("/api/server-time")["clock_synced"] is not False)
        board.check("after the reboot: the clock synchronized", synced is not None,
                    f"{synced:.0f} s after LocalHome was back" if synced is not None else "not in 5 minutes")
    took = back_within(180, lambda: ok_before <= answering(board)[0])
    board.check("after it: every device answering before answers again", took is not None,
                f"in {took:.0f} s" if took is not None else str(sorted(ok_before - answering(board)[0])))
    climate = board.climate()
    board.check("after it: still away, until the same time", climate["away_until"] == away_until,
                f"{climate['away_until']} (was {away_until})")
    board.check("after it: no manual change came back", all(z["override"] is None for z in climate["zones"]))
    board.poll_seconds = next((x["value"] for x in board.get("/api/climate-admin/settings")["settings"]
                               if x["key"] == "thermostat_poll_seconds"), board.poll_seconds)
    # A whole cycle: the relays' own status comes in on it, so an opening
    # sent before this script was back on the broker would show here too.
    board.check(f"after it: {warm} stays closed for a whole cycle though its week asks for heat",
                board.stays(f"{warm} Valve", False, board.poll_seconds + 10))
    board.check(f"after it: {bare} stays closed", board.stays(f"{bare} Valve", False, 3))
    opened = [(n, text) for at, n, text in board.commands if at >= since and text.startswith("on") and n.endswith("Valve")]
    board.check("across it: no valve was told to open", not opened, str(opened))
    settings_after = kept(board)
    changed = [k for k in settings_before if settings_after.get(k) != settings_before[k]]
    board.check("across it: the settings, the plant and the weeks as they were", not changed, str(changed))
    vmc_after = answering(board)[1]
    board.check("across it: the VMC as it was", vmc_after == vmc_before, str(vmc_after))
    # A problem of the first minutes (the clock, the network coming up)
    # goes by itself: what's new and stays is.
    settled = back_within(180, lambda: problems(board) <= problems_before)
    board.check("after it: no new problem", settled is not None, str(sorted(problems(board) - problems_before)))
    home.open()
    board.check("after it: the home says away", home.page.inner_text("#hero-title").startswith("Fuori casa"))
    home.back_home()
    board.check(f"back home: {warm} opens (its week)", board.becomes(f"{warm} Valve", True))
    board.check(f"back home: {bare} closed (no week)", board.stays(f"{bare} Valve", False, 3))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--board", default="antonio.local")
    parser.add_argument("--parts", default=",".join(PARTS),
                        help=f"some of: {', '.join(PARTS + EXTRA_PARTS)} ({', '.join(EXTRA_PARTS)} only when named)")
    parser.add_argument("--yes", action="store_true", help="run it - it drives the real relays")
    args = parser.parse_args(argv)
    parts = [p for p in args.parts.split(",") if p]
    unknown = set(parts) - set(PARTS) - set(EXTRA_PARTS)
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
            for part in PARTS + EXTRA_PARTS:
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
                elif part == "away":
                    away_and_home(board, home, browser)
                elif part in EXTRA_PARTS:
                    restarted_while_away(board, home, part)
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
            if board.saved_weeks:
                board.check("at the end: the weeks put back exactly", board.restore_weeks())
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
