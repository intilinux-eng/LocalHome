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
# "reboot" waits for someone to reboot or unplug the board; "soak" takes
# hours (--hours).
EXTRA_PARTS = ("frozen", "power", "restart", "reboot", "soak", "thresholds", "schedule")
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
            the dehumidifier on from the home opens the bathrooms' valve with it
            (its water), both kept on 16 minutes - past a whole "on,900" hold,
            each renewed, the thermostat never closing the valve meanwhile - off
            closes it; away: everything off but the bypass, still closed
away        away and home again and again, with the week asking for heat and with
            no week: from a card and from the sheet, for a day and until a time two
            minutes ahead (it ends by itself); every kind of manual change before
            (dropped); while away, a manual change, the week and a valve switched
            from outside all refused or undone - a stale second phone too; the
            season and diagnostic mode changed while away; rapid toggles
at the end  the season, the weeks, the dehumidifier's threshold and diagnostic mode
            as found
frozen      only when named, and by hand: summer, the dehumidifier on with its water
            valve - then LocalHome stopped ("sudo systemctl stop localhome" over
            SSH, waited for): each relay off by itself at the end of its own hold
            ("on,N"), counted from its last renewal, the dehumidifier never after
            its water; LocalHome started again ("sudo systemctl start localhome"):
            the dehumidifier never on with its water closed
power       only when named, and by hand: the relays' power cut by a switch and given
            back, four times - in winter with a zone open by hand: a flick (under a
            second), about ten seconds, about two minutes; in summer with the
            dehumidifier on, about ten seconds. Each time: every relay back where
            it was by itself or by LocalHome, timed from their return (the bypass
            by its own "restore last") - the dehumidifier off, its run ended, and
            its water valve with it; never on with its water closed, nothing switching for a minute after, the admin counting
            the restarts, no problem left once they're back
restart     only when named: LocalHome restarted from the admin while away - still
            away after it until the same time, the valves closed though the week asks
            for heat, nothing opened meanwhile; the settings, the plant, the weeks and
            the VMC as they were, every device answering again, no new problem; back
            home, the week
reboot      only when named, and by hand: the same with the whole board rebooted - it
            waits for "sudo reboot" over SSH, or the board unplugged and plugged back
            in (a power cut while nobody's home); its clock synchronized again
soak        only when named, for --hours (1 if not given): stress first - the
            clients of a real home asking at once for a minute (--clients, 2: at
            most a PC and a phone are open together), thirty changes in thirty
            seconds, two phones fighting over a zone, forty week saves in a row,
            the clients' pages open together - then the long run: a zone held open
            by hand the whole time (its relay must never drop: the "on,900"
            renewed every 5 minutes), the other zone changed and the clients
            asking every 10 minutes; every
            minute the process's memory and threads, the database, the loops,
            the answers' time and any new problem - summed up at the end
thresholds  only when named: comfort and economy, each zone, in both seasons (the
            zone kept out of cooling: winter only). Its room stood in for at a
            temperature each level's targets can be tried at (22/18 degrees in
            winter, 24/28 in summer): its H&T's message sent every second in its
            place, marked and not kept by the broker - a real report meanwhile has
            the step done again; at the end its real temperature back. A target on
            the room's side closes; one inside the band (the hysteresis, plus the
            economy tolerance for an economy target) stays closed - where a comfort
            one as far would open; past the band opens; inside it again stays open;
            at the target it stops - the thresholds the board states checked too.
            A zone without an H&T on the broker: its real room, where it reaches
schedule    only when named, for --hours changes of the hour (1 if not given): each
            zone with a temperature has its week painted for the hours ahead, a
            target 2.5 degrees past its room's own on one side then the other - one
            zone opening as the other closes. At each change of the hour, nobody
            touching anything: each relay as the week says, timed from the hour -
            LocalHome's command and the Shelly's own report - then kept so until
            the next change, an open one's hold renewed every 5 minutes; the
            weeks put back"""

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
        # Each zone's H&T on the broker - what the thresholds part stands in for.
        self.sensors = {z["name"]: z["sensor_device_id"] for z in maps
                        if z.get("sensor_mode", "shelly") == "shelly" and z.get("sensor_device_id")}
        self.real_reports: list[tuple[float, str, float]] = []
        self.standing_in: dict[str, float] = {}
        self.real_before: dict[str, float] = {}
        for key, name in (("bypass_valve_device_id", BYPASS), ("dehumidifier_device_id", DEHUMIDIFIER)):
            if switches.get(key):
                self.relays[switches[key]] = name
        self.poll_seconds = next((s["value"] for s in self.get("/api/climate-admin/settings")["settings"]
                                  if s["key"] == "thermostat_poll_seconds"), 60)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"live-walk-{int(time.time())}")
        self.client.on_message = self._on_message
        # The broker may be the board's own: back within seconds of a reboot.
        self.client.reconnect_delay_set(1, 4)
        self.boots: list[tuple[float, str, str]] = []      # (when, relay, "online"/"offline")
        self.statuses: list[tuple[float, str, bool]] = []  # (when, relay, its output) - each live report
        self.client.on_connect = lambda c, u, f, r, p=None: [
            c.subscribe(f"{d}/{t}") for d in self.relays for t in ("status/switch:0", "command/switch:0", "online")] + [
            c.subscribe(f"{d}/status/temperature:0") for d in self.sensors.values()]
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
        if device not in self.relays:
            # A sensor: its own report, not one of ours standing in for it.
            zone = next((z for z, d in self.sensors.items() if d == device), None)
            try:
                payload = json.loads(msg.payload)
            except ValueError:
                return
            if zone and isinstance(payload, dict) and not payload.get("stand_in") and not msg.retain:
                self.real_reports.append((time.time(), zone, payload.get("tC")))
            return
        name = self.relays[device]
        text = msg.payload.decode("utf-8", "replace")
        if rest == "online":
            if not msg.retain:
                self.boots.append((time.time(), name, "online" if text.strip() == "true" else "offline"))
                say(f"   {name}: {'back online' if text.strip() == 'true' else 'offline'}")
            return
        if rest == "status/switch:0":
            try:
                status = json.loads(text)
                output = status.get("output")
            except ValueError:
                return
            # Its "source" stays "init" in every status after a restart until
            # the relay moves - LocalHome's own status asks included: not a
            # restart. Its "online" is (above).
            if not msg.retain:
                self.statuses.append((time.time(), name, output))
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

    # ---------------------------------------------- a sensor stood in for --
    def stand_in(self, zone, temperature):
        """`zone`'s room at `temperature`: its H&T's message, sent every second
        in its place (marked, not kept by the broker) until stand_down()."""
        import threading

        if zone not in self.real_before:
            self.real_before[zone] = self.zone(zone)["temperature_c"]
        self.standing_in[zone] = temperature
        if not getattr(self, "_standing", None):
            self._standing = threading.Event()
            threading.Thread(target=self._stand, args=(self._standing,), daemon=True).start()

    def _stand(self, stop):
        while not stop.is_set():
            for zone, value in list(self.standing_in.items()):
                self._publish_temperature(zone, value, stand_in=True)
            time.sleep(1)

    def _publish_temperature(self, zone, value, stand_in):
        body = {"id": 0, "tC": value, "tF": round(value * 9 / 5 + 32, 1)}
        if stand_in:
            body["stand_in"] = True
        self.client.publish(f"{self.sensors[zone]}/status/temperature:0", json.dumps(body), qos=1, retain=False)

    def stand_down(self, zones=None):
        """These zones stood in for (all: None) back at their real
        temperature - their own last report, or the reading found before -
        until they speak again: {zone: temperature}."""
        zones = list(self.real_before) if zones is None else [z for z in zones if z in self.real_before]
        for zone in zones:
            self.standing_in.pop(zone, None)
        if not self.standing_in and getattr(self, "_standing", None):
            self._standing.set()
            self._standing = None
        time.sleep(1.5)                      # the last message in its place out first
        back = {}
        for zone in zones:
            before = self.real_before.pop(zone)
            last = next((value for at, z, value in reversed(self.real_reports) if z == zone and value is not None), None)
            back[zone] = last if last is not None else before
            self._publish_temperature(zone, back[zone], stand_in=False)
        return back

    def reads(self, zone, temperature, seconds=30):
        """The board reading `zone`'s room at `temperature`."""
        end = time.time() + seconds
        while time.time() < end:
            if abs((self.zone(zone)["temperature_c"] or -99) - temperature) < 0.05:
                return True
            time.sleep(1)
        return False

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

    def state_now(self, name):
        """The card as it is now: the page asked again first, not left to its next refresh."""
        self.refresh()
        return self.state(name)

    def manual(self, name, target=None, hours="2", off=False):
        """A manual change from the card: a target or off, for 2 hours,
        "next" or "manual" (until changed)."""
        self.refresh()
        card = self.card(name)
        if off:
            card.locator("button[data-action=mode-off]").click()
        else:
            # "Manual", then from the value it opens on - the target held or
            # its usual one - as someone looking at it would.
            card.locator("button[data-action=mode-manual]").click()
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


# The lowest thresholds the panel takes (services/dehumidifier.py's RANGES)
# and how far apart they must be.
DEHUMIDIFIER_STOP_MIN, DEHUMIDIFIER_START_MIN, DEHUMIDIFIER_GAP = 30, 32, 2


class Dehumidifier:
    """Its thresholds lowered for the test when the air is too dry for it to
    start - the start a few points under the air, the stop under that, so it
    runs as long as the test needs - and put back exactly as they were."""

    def __init__(self, board: Board):
        self.board, self.original = board, None

    def allow(self):
        status = self.board.climate().get("dehumidifier") or {}
        settings = self.board.get("/api/dehumidifier/settings")["settings"]
        humidity = status.get("humidity")
        if humidity is None or humidity >= settings["start_from"]:
            return humidity is not None
        start = max(DEHUMIDIFIER_START_MIN, int(humidity) - 3)
        stop = max(DEHUMIDIFIER_STOP_MIN, start - DEHUMIDIFIER_GAP)
        if start > humidity or start - stop < DEHUMIDIFIER_GAP:
            say(f"  the air at {humidity}%: too dry to start it even at {start}% - skipped")
            return False
        if self.original is None:
            self.original = {"start_from": settings["start_from"], "stop_below": settings["stop_below"]}
        self.board.post("/api/dehumidifier/settings", {"start_from": start, "stop_below": stop})
        say(f"  dehumidifier: on from {start}%, off under {stop}% for the test (the air at {humidity}%; it was "
            f"{self.original['start_from']}% and {self.original['stop_below']}%)")
        return True

    def restore(self):
        if self.original is not None:
            self.board.post("/api/dehumidifier/settings", self.original)
            say(f"  dehumidifier: thresholds back to {self.original['start_from']}% and {self.original['stop_below']}%")
            self.original = None


# ------------------------------------------------------------- the walk --

def zone_week(board, home, name, label):
    want = board.week_expectation(name)
    if want is None:
        say(f"  {name}: inside its band - the valve may keep its state")
        return
    board.check(f"{label}: {name} as its week says ({'open' if want else 'closed'})",
                board.becomes(f"{name} Valve", want), f"card: {home.state_now(name)!r}")


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
                    f"card: {home.state_now(name)!r}")
        home.manual(name, same + 1.5)
        board.check(f"{name} at {same + 1.5}°: opens", board.becomes(valve, True), f"card: {home.state_now(name)!r}")
        home.manual(name, off=True)
        board.check(f"{name} off for 2 h: closes", board.becomes(valve, False), f"card: {home.state_now(name)!r}")
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
    board.check("away: nothing to change on the cards", home.page.locator(".zone .mode-row").count() == 0)
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


# Past one whole hold of the relays ("on,900"): a dehumidifier's water
# valve must be renewed while it runs - the thermostat, which wants the
# zone kept out of cooling closed, never closing it meanwhile.
DEHUMIDIFIER_HELD_MINUTES = 16


def hold_both(board, names, minutes, label):
    """`names` kept on for `minutes`: no report and no command the other way,
    each one's hold renewed never more than 7 minutes apart - and a zone's
    valve among them has the minutes in its hours on (found cooling with
    them at 0: a Shelly valve's on/off never reached the history)."""
    say(f"  {label}: held for {minutes} minutes")
    zones = [name.removesuffix(" Valve") for name in names if name.endswith(" Valve")]
    hours = lambda zone: board.get(f"/api/climate/zones/{zone}/on-hours")["week"]
    hours_before = {zone: hours(zone) for zone in zones}
    since = time.time()
    dropped = {}
    while time.time() < since + minutes * 60:
        for name in names:
            if board.relay_state.get(name) is False:
                dropped.setdefault(name, time.strftime("%H:%M:%S"))
        time.sleep(1)
    for name in names:
        offs = [text for at, n, text in board.commands if n == name and at >= since and text.startswith("off")]
        board.check(f"{label}: {name} on all {minutes} minutes", name not in dropped and not offs,
                    (f"reported off at {dropped[name]}" if name in dropped else "") + (f" told {offs[:2]}" if offs else ""))
        renewals = [at for at, n, text in board.commands if n == name and at >= since and text.startswith("on")]
        gaps = [b - a for a, b in zip([since] + renewals, renewals + [time.time()])]
        board.check(f"{label}: {name}'s hold renewed, never more than 7 minutes apart", gaps and max(gaps) < 420,
                    f"{len(renewals)} renewals, longest gap {max(gaps) / 60:.1f} min")
    for zone in zones:
        # Read with what's waiting to be written; a sample every 30 s.
        added = (hours(zone) - hours_before[zone]) * 60
        board.check(f"{label}: {zone}'s hours on count the {minutes} minutes", added >= minutes - 2,
                    f"{added:+.1f} min this week")


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
                        home.card(name).locator(".mode-row").count() == 0, f"card: {home.state_now(name)!r}")
            board.check(f"summer: {name}'s valve closed", board.becomes(f"{name} Valve", False))
            continue
        if z["temperature_c"] is None:
            continue
        zone_week(board, home, name, "summer, its week")
        home.manual(name, off=True)
        board.check(f"summer: {name} off for 2 h: closes", board.becomes(f"{name} Valve", False))
        home.manual(name, round(z["temperature_c"] * 2) / 2 + 0.5, hours="manual")
        board.check(f"summer: {name} at a target above the room: stays closed", board.stays(f"{name} Valve", False),
                    f"card: {home.state_now(name)!r}")
        home.back_to_the_week(name)
        zone_week(board, home, name, "summer, back to the week")
    kept_out = next((z["name"] for z in climate["zones"] if not z["cool_enabled"]), None)
    if board.has(DEHUMIDIFIER):
        asked = time.time()                    # before the tap: its commands go with it
        if dehumidifier.allow() and home.dehumidifier(1):
            running = board.becomes(DEHUMIDIFIER, True)
            board.check("summer: the dehumidifier on from the home", running)
            if kept_out and running:
                water = f"{kept_out} Valve"
                board.check(f"summer: its water through {kept_out}'s valve - open", board.becomes(water, True))
                first = lambda name: next((at for at, n, text in board.commands
                                           if n == name and at >= asked and text.startswith("on")), None)
                on_at, water_at = first(DEHUMIDIFIER), first(water)
                board.check(f"summer: {kept_out}'s valve told to open with the dehumidifier",
                            on_at is not None and water_at is not None and abs(water_at - on_at) <= 10,
                            f"{water_at - on_at:+.1f} s" if on_at and water_at else "")
                hold_both(board, [DEHUMIDIFIER, water], DEHUMIDIFIER_HELD_MINUTES,
                          f"summer: the dehumidifier and {kept_out}'s valve")
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
    if card.locator("button[data-action=mode-manual]").count():
        card.locator("button[data-action=mode-manual]").click()
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


def _wait_for_answer(board, answering, seconds):
    """When LocalHome stops answering (answering=False) or answers again."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            board.get("/api/server-time")
            up = True
        except (OSError, ValueError):
            up = False
        if up is answering:
            return time.time()
        time.sleep(1)
    return None


def frozen(board, home, dehumidifier):
    """The board gone with the dehumidifier on - see PLAN."""
    say("== THE BOARD GONE WITH THE DEHUMIDIFIER ON")
    kept_out = next((z["name"] for z in board.climate()["zones"] if not z["cool_enabled"]), None)
    water = f"{kept_out} Valve" if kept_out else None
    if not (board.has(DEHUMIDIFIER) and water and board.has(water)):
        say("  needs the dehumidifier and the valve it draws its water through - skipped")
        return
    if board.climate()["mode"] != "cool":
        board.season("cool")
        home.open()
    if not (dehumidifier.allow() and home.dehumidifier(1)):
        board.check("frozen: the dehumidifier on from the home", False)
        return
    board.check("frozen: the dehumidifier on, its water open",
                board.becomes(DEHUMIDIFIER, True) and board.becomes(water, True))
    host = board.base.split("//")[1].split(":")[0]
    say(f"  >>> now stop LocalHome on {host}: sudo systemctl stop localhome - waiting up to 5 minutes")
    gone = _wait_for_answer(board, False, 300)
    board.check("frozen: LocalHome stopped", gone is not None)
    if gone is None:
        return

    def last_hold(name):
        """When it was last told "on,N" before LocalHome stopped, and N."""
        held = [(at, text) for at, n, text in board.commands if n == name and at <= gone and text.startswith("on")]
        if not held:
            return None, None
        at, text = held[-1]
        return at, int(text.split(",")[1]) if "," in text else None

    off_at = {}
    end = time.time() + 17 * 60
    while time.time() < end and len(off_at) < 2:
        for name in (DEHUMIDIFIER, water):
            if name not in off_at and board.relay_state.get(name) is False:
                off_at[name] = time.time()
                say(f"   {name} off by itself, {(off_at[name] - gone) / 60:.1f} min after LocalHome stopped")
        time.sleep(1)
    for name in (DEHUMIDIFIER, water):
        renewed, hold = last_hold(name)
        late = off_at[name] - (renewed + hold) if name in off_at and renewed and hold else None
        board.check(f"frozen: {name} off by itself at the end of its hold", late is not None and -5 <= late <= 30,
                    f"'on,{hold}' {(gone - renewed) / 60:.1f} min before the stop, off {late:+.0f} s from its end"
                    if late is not None else f"never seen off (hold {hold})")
    board.check("frozen: the dehumidifier off no later than its water",
                DEHUMIDIFIER in off_at and (water not in off_at or off_at[DEHUMIDIFIER] <= off_at[water] + 1),
                f"the dehumidifier {(off_at.get(DEHUMIDIFIER, end) - gone) / 60:.1f} min, its water "
                f"{(off_at.get(water, end) - gone) / 60:.1f} min after LocalHome stopped")
    say(f"  >>> now start LocalHome on {host} again: sudo systemctl start localhome - waiting up to 5 minutes")
    back = _wait_for_answer(board, True, 300)
    board.check("frozen: LocalHome back", back is not None)
    if back is None:
        return
    dry = []
    end = time.time() + board.poll_seconds + 60
    while time.time() < end:
        if board.relay_state.get(DEHUMIDIFIER) and board.relay_state.get(water) is False:
            dry.append(time.strftime("%H:%M:%S"))
        time.sleep(1)
    board.check("frozen: back, the dehumidifier never on with its water closed", len(dry) <= 5,
                f"dehumidifier {board.relay_state.get(DEHUMIDIFIER)}, water {board.relay_state.get(water)}"
                + (f"; on without water {len(dry)} s from {dry[0]}" if dry else ""))
    home.open()
    if board.relay_state.get(DEHUMIDIFIER):
        home.dehumidifier(None)
    board.check("frozen: the dehumidifier off at the end, its water with it",
                board.becomes(DEHUMIDIFIER, False) and board.becomes(water, False))


# The cuts the power part asks for, in order: (season, how long, a long one?).
POWER_CUTS = (("heat", "a flick - off and on again, under a second", False),
              ("heat", "about ten seconds", False),
              ("cool", "about ten seconds, the dehumidifier running", False),
              ("heat", "about two minutes", True))


def _restart_counts(board):
    """Each relay's restarts in the last 7 days, by cause, as the admin counts them."""
    devices = board.get("/api/climate-admin/shelly-health").get("devices") or []
    out = {}
    for d in devices:
        name = next((n for n in board.relays.values() if n in (d.get("names") or [])), None)
        if name:
            stats = d.get("stats") or {}
            out[name] = {k: stats.get(k) or 0 for k in ("restarts_power_7d", "restarts_brownout_7d",
                                                        "restarts_unknown_7d", "restarts_crash_7d")}
    return out


def power(board, home, dehumidifier):
    """The relays' power cut and given back, by hand - see PLAN."""
    say("== THE SHELLYS' POWER CUT")
    zones = [z for z in board.climate()["zones"] if z["temperature_c"] is not None and board.has(f"{z['name']} Valve")]
    kept_out = next((z["name"] for z in board.climate()["zones"] if not z["cool_enabled"]), None)
    water = f"{kept_out} Valve" if kept_out else None
    if not zones:
        say("  needs a zone with a temperature and its relay - skipped")
        return
    host = board.base.split("//")[1].split(":")[0]
    for mode, how, long in POWER_CUTS:
        season = "winter" if mode == "heat" else "summer"
        label = f"power, {season}, {how.split(',')[0]}"
        if board.climate()["mode"] != mode:
            board.season(mode)
            home.open()
        held = None
        if mode == "heat":
            held = zones[0]["name"]
            home.manual(held, round(board.zone(held)["temperature_c"] * 2) / 2 + 1.5, hours="manual")
            board.becomes(f"{held} Valve", True)
        elif board.has(DEHUMIDIFIER) and water and dehumidifier.allow() and home.dehumidifier(1):
            board.becomes(DEHUMIDIFIER, True)
            board.becomes(water, True)
        else:
            say(f"  {label}: the dehumidifier can't run - skipped")
            continue
        time.sleep(5)
        want = {name: board.relay_state.get(name) for name in board.relays.values()}
        # Back after it: where they were - but the dehumidifier's run ends with
        # a power cut (found off, as switched off some other way), its water
        # valve with it: like any appliance after a black-out.
        after = dict(want)
        if mode == "cool":
            after.update({DEHUMIDIFIER: False, water: False})
        counts_before, problems_before = _restart_counts(board), problems(board)
        say(f"  {label}: the relays now {want}")
        say(f"  >>> now cut the four Shellys' power on {host}'s plant: {how}, then give it back - "
            "waiting up to 10 minutes")
        asked = time.time()
        first = None
        end = asked + 600
        while time.time() < end and first is None:
            first = next((at for at, _, what in board.boots if at >= asked and what == "online"), None)
            time.sleep(0.5)
        if first is None and "flick" in how:
            say(f"  {label}: no relay restarted - too short for them to notice; nothing to check")
        else:
            board.check(f"{label}: the relays seen restarting", first is not None)
        if first is None:
            if held:
                home.back_to_the_week(held)
            continue
        # Back where they were, each: its last report since it restarted the
        # state it had - timed from the first one back.
        def since_boot(name):
            boot = next((at for at, n, what in board.boots if at >= first - 1 and n == name), None)
            reports = [(at, out) for at, n, out in board.statuses if n == name and boot is not None and at >= boot]
            return boot, reports

        dry, took = [], {}
        end = time.time() + board.poll_seconds + 90
        while time.time() < end:
            for name, state in after.items():
                boot, reports = since_boot(name)
                if name not in took and reports and reports[-1][1] is state:
                    took[name] = reports[-1][0] - first
            if water and board.relay_state.get(DEHUMIDIFIER) and board.relay_state.get(water) is False:
                dry.append(time.time())
            restarted = [name for name in after if since_boot(name)[0] is not None]
            if restarted and all(name in took for name in restarted) and time.time() - first > 20:
                break
            time.sleep(0.5)
        for name, state in after.items():
            boot, reports = since_boot(name)
            if boot is None:
                board.check(f"{label}: {name} {'on' if state else 'off'} - it didn't restart",
                            board.relay_state.get(name) is state, f"it says {board.relay_state.get(name)}")
                continue
            dropped = any(out is not state for _, out in reports)
            ended = " - its run ended with the cut" if state is not want[name] else ""
            board.check(f"{label}: {name} back {'on' if state else 'off'}{ended}", name in took,
                        (f"{took[name]:.0f} s after they were back" + (", after restarting the other way" if dropped
                                                                       else ", as it restarted"))
                        if name in took else f"its reports since: {[out for _, out in reports]}")
        if mode == "cool":
            board.check(f"{label}: the dehumidifier never on with its water closed", len(dry) <= 4,
                        f"{len(dry) / 2:.0f} s on without its water" if dry else "")
        # Nothing switching for a minute after: no report, no command the other way.
        since, moved = time.time(), {}
        while time.time() < since + 60:
            for name, state in after.items():
                if board.relay_state.get(name) is (not state):
                    moved.setdefault(name, time.strftime("%H:%M:%S"))
            time.sleep(1)
        against = sorted({n for at, n, text in board.commands if at >= since and n in after
                          and text.startswith("off" if after[n] else "on")})
        board.check(f"{label}: nothing switching for a minute after", not moved and not against,
                    f"moved {moved} told {against}" if moved or against else "")
        restarted = {name for name in want if since_boot(name)[0] is not None}
        back_within(360 if long else 150, lambda: _restart_counts(board) != counts_before)
        counts_after = _restart_counts(board)
        noticed = {n: {k.replace("restarts_", "").replace("_7d", ""): counts_after[n][k] - counts_before.get(n, {}).get(k, 0)
                       for k in counts_after[n] if counts_after[n][k] != counts_before.get(n, {}).get(k, 0)}
                   for n in counts_after}
        say(f"  {label}: the admin counted {noticed}")
        board.check(f"{label}: the admin counted each restart", all(noticed.get(n) for n in restarted),
                    f"restarted {sorted(restarted)}, counted {noticed}")
        settled = back_within(300, lambda: problems(board) <= problems_before)
        board.check(f"{label}: no problem left once they're back", settled is not None,
                    str(sorted(problems(board) - problems_before)))
        if held:
            home.back_to_the_week(held)
        if mode == "cool" and board.relay_state.get(DEHUMIDIFIER):
            home.dehumidifier(None)
            board.becomes(DEHUMIDIFIER, False)
        say(f"  {label}: done in {(time.time() - asked) / 60:.1f} min")


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


def soak(board, home, browser, hours, clients=2):
    """Stress, then a long run - see PLAN. The zones are changed through the
    API (a phone's requests, without its pacing), the pages are real."""
    from concurrent.futures import ThreadPoolExecutor
    quote = urllib.parse.quote
    climate = board.climate()
    mode = climate["mode"]
    cool = mode == "cool"
    zones = [z["name"] for z in climate["zones"]
             if not (cool and not z["cool_enabled"]) and board.has(f"{z['name']} Valve")]
    if not zones:
        say("soak: no zone with a relay to run")
        return
    first, other = zones[0], (zones[1] if len(zones) > 1 else None)
    valve = f"{first} Valve"
    opens, closes = (16.0, 30.0) if cool else (30.0, 16.0)

    def override(name, target, **when):
        return board.post(f"/api/climate/zones/{quote(name)}/override", {"target": target, **when})

    def cancel(name):
        board.post(f"/api/climate/zones/{quote(name)}/override/cancel", {})

    timings: dict[str, list[float]] = {}

    def timed(path):
        t0 = time.time()
        board.get(path)
        timings.setdefault(path, []).append(time.time() - t0)

    samples = []

    def sample():
        s = board.get("/api/system-status")
        samples.append({"at": time.time(), "rss": s["process"]["rss_bytes"], "threads": s["process"]["threads"],
                        "db": s["database"]["file_bytes"],
                        "late": max(lp["last_ok_seconds_ago"] / lp["interval_seconds"] for lp in s["loops"]),
                        "stalled": any(lp["stalled"] for lp in s["loops"]),
                        "safety": {f"{e.get('when')} {e.get('values')}" for e in s.get("watchdog_history") or []
                                   if e.get("code") == "safety_timer"}})

    def problems():
        return {f"{p['code']}:{p.get('subject', '')}" for p in board.get("/api/problems")["problems"]}

    def phones(count, seconds):
        def phone(_):
            failed, end = 0, time.time() + seconds
            while time.time() < end:
                for path in ("/api/climate/zones", "/api/readings", "/api/devices"):
                    try:
                        timed(path)
                    except Exception:   # noqa: BLE001 - counted: a phone left without an answer
                        failed += 1
            return failed
        with ThreadPoolExecutor(count) as pool:
            return sum(pool.map(phone, range(count)))

    before = problems()
    sample()
    say(f"soak: {first} (and {other}), {mode}, for {hours} h of long run after the stress")

    # ------------------------------------------------------------ stress --
    failed = phones(clients, 60)
    board.check(f"stress: {clients} clients asking at once for a minute - every answer came", failed == 0, f"{failed} failed")

    since, refused = time.time(), 0
    for i in range(30):
        try:
            override(first, opens if i % 2 == 0 else closes, hours=1)
        except Exception:   # noqa: BLE001
            refused += 1
        time.sleep(1)
    board.check("stress: thirty changes in thirty seconds - all taken", refused == 0, f"{refused} refused")
    board.check(f"stress: {first} ends as the last change says (closed)", board.becomes(valve, False, 90))
    sent = [c for c in board.commands if c[0] >= since and c[1] == valve]
    board.check("stress: no more commands to the relay than changes", len(sent) <= 31, f"{len(sent)} commands")

    def writer(target, gap, seconds=30):
        done, end = 0, time.time() + seconds
        while time.time() < end:
            try:
                override(first, target, hours=1)
                done += 1
            except Exception:   # noqa: BLE001
                pass
            time.sleep(gap)
        return done
    with ThreadPoolExecutor(2) as pool:
        done = list(pool.map(lambda args: writer(*args), [(opens, 0.5), (closes, 0.7)]))
    override(first, opens, hours=1)
    board.check(f"stress: two phones fighting over {first} ({sum(done)} changes), then one last change - "
                f"the relay follows it", board.becomes(valve, True, 90))
    board.check("stress: and the board says what the relay says", board.zone(first)["valve_on"] is True)
    cancel(first)

    weeks = ({d: [20.0] * 24 for d in DAYS_OF_WEEK}, {d: [None] * 24 for d in DAYS_OF_WEEK})
    refused = 0
    for i in range(40):
        try:
            board.set_week(first, mode, weeks[i % 2])
        except Exception:   # noqa: BLE001
            refused += 1
    board.check("stress: forty week saves in a row - all taken, the last one kept",
                refused == 0 and board.week(first, mode) == weeks[1], f"{refused} refused")
    board.restore_weeks()

    # The clients' pages: a PC's, then phones'.
    pages, errors = [], []
    for i in range(clients):
        page = browser.new_page(viewport={"width": 1280, "height": 900} if i == 0 else {"width": 390, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        keep_vmc(page)
        page.goto(board.base)
        pages.append(page)
    end = time.time() + 120
    while time.time() < end:
        timed("/api/climate/zones")
        time.sleep(5)
    board.check(f"stress: {clients} pages open together for two minutes - no script error", not errors, "; ".join(errors[:2]))
    for page in pages:
        page.close()

    # ---------------------------------------------------------- long run --
    sample()
    start_rss, start_threads = samples[-1]["rss"], samples[-1]["threads"]
    override(first, opens, until="manual")
    board.check(f"long run: {first} held open by hand", board.becomes(valve, True, 90))
    held_since = time.time()
    drops, end = [], held_since + hours * 3600
    next_burst = held_since + 600
    while time.time() < end:
        time.sleep(60)
        try:
            sample()
            timed("/api/climate/zones")
            timed("/api/readings")
        except Exception as e:   # noqa: BLE001 - a board that didn't answer once is said, not fatal
            say(f"  no answer: {e!r}")
        if board.relay_state.get(valve) is not True:
            drops.append(time.strftime("%H:%M:%S"))
            say(f"  {valve} is not on!")
        if time.time() >= next_burst:
            next_burst += 600
            if other:
                override(other, opens, hours=1)
                board.becomes(f"{other} Valve", True, 90)
                cancel(other)
            phones(clients, 20)
            s = samples[-1]
            say(f"  {round((time.time() - held_since) / 60)} min: {s['rss'] / 1e6:.0f} MB, {s['threads']} threads, "
                f"database {s['db'] / 1e6:.1f} MB")
    held = time.time() - held_since
    renewals = [at for at, n, text in board.commands if n == valve and at >= held_since and text.startswith("on")]
    gaps = [b - a for a, b in zip([held_since] + renewals, renewals + [time.time()])]
    cancel(first)

    # ---------------------------------------------------------- summed up --
    rss = [s["rss"] for s in samples]
    threads = [s["threads"] for s in samples]
    growth = (samples[-1]["rss"] - start_rss) / 1e6
    board.check(f"long run: {first}'s relay held for {held / 3600:.1f} h - never dropped", not drops, str(drops[:5]))
    board.check(f"long run: the hold renewed {len(renewals)} times, never more than 7 minutes apart",
                gaps and max(gaps) < 420, f"longest gap {max(gaps) / 60:.1f} min" if gaps else "")
    acted = set().union(*(s["safety"] for s in samples)) - samples[0]["safety"]
    board.check("long run: no relay's safety timer acted", not acted, ", ".join(sorted(acted)))
    board.check(f"long run: memory steady ({start_rss / 1e6:.0f} -> {samples[-1]['rss'] / 1e6:.0f} MB, "
                f"at most {max(rss) / 1e6:.0f})", growth < 40, f"{growth / max(held / 3600, 0.1):.1f} MB/h")
    board.check(f"long run: threads steady ({start_threads} -> {threads[-1]}, at most {max(threads)})",
                threads[-1] <= start_threads + 5)
    board.check("long run: the loops never stalled", not any(s["stalled"] for s in samples),
                f"latest at most {max(s['late'] for s in samples):.1f} intervals behind")
    for path, times in sorted(timings.items()):
        times = sorted(times)
        p95 = times[int(len(times) * 0.95) - 1] if len(times) > 1 else times[0]
        board.check(f"answers: {path} - 95% within 2 s", p95 < 2.0,
                    f"{len(times)} asked, median {times[len(times) // 2]:.2f} s, 95% {p95:.2f} s, slowest {times[-1]:.2f} s")
    new = problems() - before
    # The stress itself switched the relay dozens of times: the board saying
    # it's short-cycling is its own alarm working (gone 24 h later), not news.
    caused = {f"relay_short_cycling:{valve}"} & new
    if caused:
        say(f"  the board noticed the stress's switching ({', '.join(caused)}) - its alarm works; gone in 24 h")
    board.check("no new problem on the board but the stress's own", not new - caused, ", ".join(sorted(new - caused)))
    say(f"database {samples[0]['db'] / 1e6:.1f} -> {samples[-1]['db'] / 1e6:.1f} MB")


DAYS_OF_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# services/thermostat.py's two levels: economy heating below 20, cooling from 26.
ECONOMY_HEAT_BELOW_C, ECONOMY_COOL_FROM_C = 20.0, 26.0


def _tier(target, mode):
    return ("economy" if target < ECONOMY_HEAT_BELOW_C else "comfort") if mode == "heat"         else ("economy" if target >= ECONOMY_COOL_FROM_C else "comfort")


def _threshold_steps(room, mode, spread):
    """(what, target, open?, becomes or stays) - from the room's temperature,
    for a level whose band is `spread` wide."""
    side = 1 if mode == "heat" else -1
    return [("on the room's side: closes", room - side * 0.5, False, "becomes"),
            (f"{spread - 0.25:.2f} degrees in, inside the band: stays closed", room + side * (spread - 0.25), False, "stays"),
            (f"{spread + 0.25:.2f} degrees in, past the band: opens", room + side * (spread + 0.25), True, "becomes"),
            ("inside the band again: stays open", room + side * (spread - 0.25), True, "stays"),
            ("at the target: stops", room - side * 0.25, False, "becomes")]


def _reachable(room, mode, tier, spread):
    return all(_tier(target, mode) == tier for _, target, _, _ in _threshold_steps(room, mode, spread)[1:])


# The room's temperature the thresholds part gives a zone, for each level -
# one its every target can be tried at (_threshold_steps()).
INJECTED_ROOM = {("heat", "comfort"): 22.0, ("heat", "economy"): 18.0,
                 ("cool", "comfort"): 24.0, ("cool", "economy"): 28.0}


def thresholds(board, home):
    """Comfort and economy against the real relays - see PLAN."""
    from datetime import timedelta

    say("== THRESHOLDS")
    climate = board.climate()
    hysteresis, tolerance = climate["hysteresis_c"], climate["economy_tolerance_c"]
    spreads = {"comfort": hysteresis, "economy": hysteresis + tolerance}
    found = climate["mode"]
    say(f"  hysteresis {hysteresis}°, economy tolerance {tolerance}°")

    def zones_in(mode):
        return [z for z in board.climate()["zones"] if (z["cool_enabled"] or mode == "heat")
                and board.has(f"{z['name']} Valve") and (z["name"] in board.sensors or z["temperature_c"] is not None)]

    def paint(name, mode, target):
        """This hour and the next at `target` - a step that crosses the hour keeps it."""
        now = board.now()
        week = board.week(name, mode)
        for at in (now, now + timedelta(hours=1)):
            week[DAYS_OF_WEEK[at.weekday()]][at.hour] = target
        board.set_week(name, mode, week)

    def plan_of(mode):
        """(zone, level, room): a zone with its sensor on the broker at the
        level's own room; one without, at its room's real one if it reaches."""
        plan = []
        for z in zones_in(mode):
            for tier in spreads:
                if z["name"] in board.sensors:
                    plan.append((z["name"], tier, INJECTED_ROOM[(mode, tier)]))
                elif _reachable(z["temperature_c"], mode, tier, spreads[tier]):
                    plan.append((z["name"], tier, None))
                else:
                    say(f"  {z['name']} {tier} in {'winter' if mode == 'heat' else 'summer'}: its room at "
                        f"{z['temperature_c']}° doesn't reach its targets, and its sensor isn't one to stand in for")
        return plan

    try:
        for mode in (found, "cool" if found == "heat" else "heat"):
            plan = plan_of(mode)
            if not plan:
                continue
            if board.climate()["mode"] != mode:
                board.season(mode)
                time.sleep(5)
            for name, tier, injected in plan:
                valve = f"{name} Valve"
                season = "winter" if mode == "heat" else "summer"
                if injected is not None:
                    board.stand_in(name, injected)
                    board.check(f"{season} {tier}: {name}'s room stood in for at {injected}°",
                                board.reads(name, injected), f"the board reads {board.zone(name)['temperature_c']}°")
                for what, _, want, how in _threshold_steps(0.0, mode, spreads[tier]):
                    for attempt in (1, 2):
                        room = injected if injected is not None else board.zone(name)["temperature_c"]
                        step = next(s for s in _threshold_steps(room, mode, spreads[tier]) if s[0] == what)
                        target = round(step[1], 2)
                        since = time.time()
                        paint(name, mode, target)
                        z = board.zone(name)
                        opens = round(target - spreads[tier] if mode == "heat" else target + spreads[tier], 2)
                        stated = (z["valve_opens_at"], z["valve_stops_at"]) == (opens, target)
                        ok = (board.becomes(valve, want, seconds=board.poll_seconds + 30) if how == "becomes"
                              else board.stays(valve, want, seconds=board.poll_seconds + 15))
                        # The real sensor speaking meanwhile, or the real room moving: the step again.
                        spoke = injected is not None and any(at >= since and n == name for at, n, _ in board.real_reports)
                        moved = injected is None and abs(board.zone(name)["temperature_c"] - room) > 0.2
                        if not (spoke or moved) or attempt == 2:
                            break
                        say(f"   {'its real sensor reported meanwhile' if spoke else 'the room moved'}: again")
                    board.check(f"{season} {tier}: {name} at {target}° with the room at {room}°, {what}",
                                ok and stated and _tier(target, mode) == tier,
                                f"the board: opens at {z['valve_opens_at']}, stops at {z['valve_stops_at']} "
                                f"({'as expected' if stated else f'expected {opens}, {target}'}); "
                                f"card: {home.state_now(name)!r}")
                if injected is not None and all(n != name for n, _, _ in plan[plan.index((name, tier, injected)) + 1:]):
                    for zone, real in board.stand_down([name]).items():
                        board.check(f"{season}: {zone}'s real temperature back ({real}°)", board.reads(zone, real))
    finally:
        for name, real in board.stand_down().items():
            board.check(f"thresholds: {name}'s real temperature back ({real}°)", board.reads(name, real))
        if board.climate()["mode"] != found:
            board.season(found)


def schedule(board, home, changes):
    """The week alone opening and closing the relays at the hour - see PLAN."""
    from datetime import timedelta

    say("== SCHEDULE")
    climate = board.climate()
    cool = climate["mode"] == "cool"
    zones = [z for z in climate["zones"] if z["temperature_c"] is not None and (z["cool_enabled"] or not cool)
             and board.has(f"{z['name']} Valve")]
    if not zones:
        board.check("schedule: a zone with a temperature and a relay to watch", False)
        return
    half = lambda v: round(v * 2) / 2
    now = board.now()
    if now.minute >= 55:                           # the first change too close to see the start settle
        say("  the hour is about to change: waiting for it")
        time.sleep((60 - now.minute) * 60 - now.second + 5)
    hour0 = board.now().replace(minute=0, second=0, microsecond=0)
    hours = [hour0 + timedelta(hours=k) for k in range(changes + 1)]

    def opens(i, k):
        return (i + k) % 2 == 1                    # the first zone closed now, the second open

    for i, z in enumerate(zones):
        room = z["temperature_c"]
        warm, cold = half(room + 2.5), half(room - 2.5)
        want_open, want_closed = (cold, warm) if cool else (warm, cold)
        mode = climate["mode"]
        week = board.week(z["name"], mode)
        for k, at in enumerate(hours):
            week[DAYS_OF_WEEK[at.weekday()]][at.hour] = want_open if opens(i, k) else want_closed
        board.set_week(z["name"], mode, week)
        say(f"  {z['name']}: room {room}°, {want_open}° (open) and {want_closed}° (closed) from "
            f"{hours[0]:%H:00} to {hours[-1]:%H:00}")
    for i, z in enumerate(zones):
        board.check(f"schedule {hours[0]:%H}:00: {z['name']} as painted ({'open' if opens(i, 0) else 'closed'})",
                    board.becomes(f"{z['name']} Valve", opens(i, 0), seconds=board.poll_seconds + 30))
    worst = 0.0
    for k in range(1, changes + 1):
        # Asleep until a little before the hour, by the board's clock.
        wait = (hours[k] - board.now()).total_seconds()
        if wait > 5:
            time.sleep(wait - 3)
        hour_at = time.time() + (hours[k] - board.now()).total_seconds()
        say(f"  -- {hours[k]:%H}:00")
        for i, z in enumerate(zones):
            name = f"{z['name']} Valve"
            want = opens(i, k)
            ok = board.becomes(name, want, seconds=max(0.0, hour_at - time.time()) + board.poll_seconds + 60)
            reported = time.time() - hour_at
            sent = next((at - hour_at for at, n, text in board.commands
                         if n == name and at >= hour_at - 2 and text.startswith("on" if want else "off")), None)
            worst = max(worst, reported) if ok else worst
            timing = (f"LocalHome's command {sent:.0f} s after the hour, the Shelly's report {reported:.0f} s"
                      if sent is not None else f"the Shelly's report {reported:.0f} s after the hour")
            board.check(f"schedule {hours[k]:%H}:00: {z['name']} {'opens' if want else 'closes'} by itself, "
                        f"not before the hour", ok and -2 <= reported <= board.poll_seconds + 30,
                        f"{timing}; card: {home.state_now(z['name'])!r}")
        # Kept so until the next change: no report, no command the other way.
        since = time.time()
        until = since + ((hours[k + 1] - board.now()).total_seconds() - 10 if k < changes else 15 * 60)
        moved = {}
        while time.time() < until:
            for i, z in enumerate(zones):
                name, want = f"{z['name']} Valve", opens(i, k)
                if board.relay_state.get(name) is (not want):
                    moved.setdefault(name, f"reported {'on' if not want else 'off'} at {time.strftime('%H:%M:%S')}")
            time.sleep(1)
        for i, z in enumerate(zones):
            name, want = f"{z['name']} Valve", opens(i, k)
            against = [text for at, n, text in board.commands
                       if n == name and at >= since and text.startswith("off" if want else "on")]
            board.check(f"schedule {hours[k]:%H}:00-{(until - since) / 60:.0f} min: {z['name']} stays "
                        f"{'open' if want else 'closed'}", name not in moved and not against,
                        moved.get(name, "") + (f" commands {against[:3]}" if against else ""))
            if want:
                renewals = [at for at, n, text in board.commands if n == name and at >= since and text.startswith("on")]
                gaps = [b - a for a, b in zip([since] + renewals, renewals + [time.time()])]
                board.check(f"schedule {hours[k]:%H}:00: {z['name']}'s hold renewed, never more than 7 minutes apart",
                            gaps and max(gaps) < 420, f"{len(renewals)} renewals, longest gap {max(gaps) / 60:.1f} min")
    say(f"  the slowest relay: {worst:.0f} s after its hour (the thermostat looks every {board.poll_seconds} s)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--board", default="antonio.local")
    parser.add_argument("--parts", default=",".join(PARTS),
                        help=f"some of: {', '.join(PARTS + EXTRA_PARTS)} ({', '.join(EXTRA_PARTS)} only when named)")
    parser.add_argument("--yes", action="store_true", help="run it - it drives the real relays")
    parser.add_argument("--hours", type=float, default=1.0,
                        help="the soak's long run, or the schedule's changes of the hour (with --parts soak/schedule)")
    parser.add_argument("--clients", type=int, default=2,
                        help="the soak's clients at once - a real home has at most two (a PC, a phone)")
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
                elif part == "soak":
                    soak(board, home, browser, args.hours, args.clients)
                elif part == "frozen":
                    frozen(board, home, dehumidifier)
                elif part == "power":
                    power(board, home, dehumidifier)
                elif part == "thresholds":
                    thresholds(board, home)
                elif part == "schedule":
                    schedule(board, home, max(1, round(args.hours)))
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
