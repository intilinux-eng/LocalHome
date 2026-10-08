"""The home page used as a resident would, summer and winter, on a phone -
the week, manual changes (a temperature or off; for a while, until the
next change, until changed back), away (a duration, a date, back from a
card), the dehumidifier - each step checked on the page AND on what the
valves and the dehumidifier actually do. Its own server, as the
production board is set up: one zone cooled, the bathrooms left out of
cooling, the dehumidifier drawing its water through their valve, the
bypass; sensors typed by hand, so each room is as a step needs it."""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import pytest

from .conftest import CONFIG as BASE_CONFIG
from .helpers import OVERFLOW_JS, Opened, eventually

pytestmark = pytest.mark.ui

ROOMS, BATHS = "Stanze", "Bagni"
CONFIG = BASE_CONFIG.replace('''      only_in_mode: "cool"
''', '''      only_in_mode: "cool"
  interlocks:
    - leader: "Dehumidifier"
      follower: "Bagni Valve"
      active_in_mode: "cool"
''', 1)
TYPED = {"sensor_mode": "manual", "sensor_device_id": None, "valve_mode": "simulated", "valve_device_id": None}


@pytest.fixture(scope="module")
def house(start_server):
    base = start_server([dict(TYPED, name=ROOMS, cool_enabled=True), dict(TYPED, name=BATHS, cool_enabled=False)], CONFIG)
    # This hour's target is the one for this hour and the next, another
    # before and after: "until the next change" is an hour or two away.
    hour = datetime.now().hour
    week = lambda now, other: {d: [now if hour <= h < hour + 2 else other for h in range(24)]
                               for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    call(base, f"/api/climate/zones/{ROOMS}/schedule", {"week": week(24.0, 26.0), "mode": "cool"})
    call(base, f"/api/climate/zones/{ROOMS}/schedule", {"week": week(21.0, 19.0), "mode": "heat"})
    call(base, f"/api/climate/zones/{BATHS}/schedule", {"week": week(22.0, None), "mode": "heat"})
    return base


def call(base, path, body=None):
    req = urllib.request.Request(base + path, method="GET" if body is None else "POST",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as res:
            return json.load(res)
    except urllib.error.HTTPError as err:
        return dict(json.load(err), status=err.code)


class Home:
    """The page and the board, as a resident's steps need them."""

    def __init__(self, browser, base, season, rooms=(27.0, 50.0), baths=(23.0, 70.0)):
        self.base = base
        self.call(f"/api/climate/away/cancel", {})
        self.call("/api/climate/mode", {"mode": season})       # drops every manual change
        self.call("/api/dehumidifier/off", {})
        self.room(ROOMS, *rooms)
        self.room(BATHS, *baths)
        self.context = browser.new_context(viewport={"width": 390, "height": 900})
        self.opened = Opened(self.context.new_page())
        self.page = self.opened.page
        self.page.goto(base)
        self.page.wait_for_selector(".zone", timeout=20000)
        self.page.wait_for_timeout(1000)
        self.t = self.page.evaluate("window.I18N.home")

    def call(self, path, body=None):
        return call(self.base, path, body)

    def room(self, name, temperature, humidity):
        assert self.call(f"/api/climate-admin/zones/{name}/manual-value",
                         {"temperature_c": temperature, "humidity_pct": humidity})["ok"]

    def on(self, name):
        return self.call(f"/api/sensors/{urllib.parse.quote(name, safe='')}").get("is_on")

    def zone(self, name):
        return next(z for z in self.call("/api/climate/zones")["zones"] if z["name"] == name)

    def refresh(self):
        self.page.evaluate("Promise.all([refreshClimate(), refreshDevices(), refreshProblems()])")
        self.page.wait_for_timeout(500)

    def card(self, name):
        return self.page.locator(f".zone[data-zone='{name}']")

    def state(self, name):
        return self.card(name).locator(".zone-head .state").inner_text().strip()

    def title(self):
        return self.page.inner_text("#hero-title").strip()

    def manual(self, name, steps=0, hours="2", off=False):
        """A manual change from the card, as a finger would."""
        card = self.card(name)
        card.locator("button[data-action=start][data-delta='0.5']").click()
        for _ in range(steps):
            card.locator(".pending button[data-action=step][data-delta='0.5']").click()
        if off:
            card.locator(".pending button[data-action=pending-off]").click()
        card.locator(f".pending button[data-action=duration][data-hours='{hours}']").click()
        card.locator(".pending button[data-action=apply]").click()
        eventually(self.page, lambda: self.zone(name)["override"] is not None)
        self.page.wait_for_timeout(500)

    def back_to_the_week(self, name):
        self.card(name).locator(".manual-banner button[data-action=resume]").click()
        assert eventually(self.page, lambda: self.zone(name)["override"] is None)
        # Its card redrawn before the next tap: a full run's busier machine
        # had the next card's button tapped while the page was re-anchored.
        assert eventually(self.page, lambda: self.card(name).locator(".manual-banner").count() == 0)
        self.page.wait_for_timeout(300)

    def close(self):
        assert self.page.evaluate(OVERFLOW_JS) <= 0
        assert self.opened.errors == []
        self.context.close()


def test_summer_the_week_and_every_manual_change(browser, house):
    home = Home(browser, house, "cool")
    t, page = home.t, home.page
    assert eventually(page, lambda: home.on(f"{ROOMS} Valve") is True), "27° with the week's 24°: cooling"
    home.refresh()
    assert home.state(ROOMS) == t["st_cooling"] and t["hero_cooling"].format(zones=ROOMS) in home.title()
    assert home.state(BATHS) == t["st_excluded"] and home.card(BATHS).locator(".stepper").count() == 0
    assert home.on(f"{BATHS} Valve") is False and home.on("Bathroom Bypass Valve") is True, "bypass closed in summer"

    # Annulla changes nothing
    home.card(ROOMS).locator("button[data-action=start][data-delta='0.5']").click()
    assert home.card(ROOMS).locator(".pending-title").inner_text() == t["pending_title"]
    home.card(ROOMS).locator(".pending button[data-action=cancel-pending]").click()
    assert home.zone(ROOMS)["override"] is None

    # 28° for 2 hours, the room at 27: the valve stops; said at the top of
    # the card, with the way back and what it goes back to
    home.manual(ROOMS, steps=7, hours="2")
    assert home.zone(ROOMS)["override"]["target"] == 28.0 and home.on(f"{ROOMS} Valve") is False
    banner = home.card(ROOMS).locator(".manual-banner")
    assert banner.inner_text().startswith("Programmazione manuale: 28°")
    assert "Torna alla programmazione (24°)" in banner.inner_text()
    assert "manuale fino alle" in page.inner_text("#hero-detail")
    # its week is shown, not changed
    home.card(ROOMS).locator(".plan-toggle").click()
    assert eventually(page, lambda: home.card(ROOMS).locator(".plan-edit.is-locked").count() == 1)
    week = home.call(f"/api/climate/zones/{ROOMS}/schedule?mode=cool")["week"]
    cell = home.card(ROOMS).locator(".sched-cell").nth(50)
    cell.scroll_into_view_if_needed()
    box = cell.bounding_box()
    page.mouse.click(box["x"] + 5, box["y"] + 5)
    page.wait_for_timeout(1000)
    assert home.call(f"/api/climate/zones/{ROOMS}/schedule?mode=cool")["week"] == week
    home.card(ROOMS).locator(".plan-toggle").click()
    home.back_to_the_week(ROOMS)
    assert eventually(page, lambda: home.on(f"{ROOMS} Valve") is True)

    # Off until the next change: closed, said as by hand - not "none planned"
    home.manual(ROOMS, hours="next", off=True)
    override = home.zone(ROOMS)["override"]
    assert override["target"] is None and override["until"] and home.on(f"{ROOMS} Valve") is False
    assert home.title() == t["hero_off_now_cool"]
    assert home.card(ROOMS).locator(".quick .stepper-value").inner_text().strip() == t["st_off"]
    home.card(ROOMS).locator("button[data-action=start][data-delta='0.5']").click()
    assert home.card(ROOMS).locator(".pending .stepper-value").inner_text() in ("24°", "26°"), "from off: its usual"
    home.card(ROOMS).locator(".pending button[data-action=cancel-pending]").click()
    home.back_to_the_week(ROOMS)

    # Until changed back: no end
    home.manual(ROOMS, hours="manual")
    assert home.zone(ROOMS)["override"]["until"] is None
    assert "finché non lo cambi" in home.card(ROOMS).locator(".manual-banner").inner_text()
    home.back_to_the_week(ROOMS)
    # The bathrooms, left out of cooling: the board refuses them too
    assert home.call(f"/api/climate/zones/{BATHS}/override", {"target": 20, "hours": 1})["code"] == "override_zone_excluded"
    home.close()


def test_summer_the_dehumidifier_and_away(browser, house):
    home = Home(browser, house, "cool")
    t, page = home.t, home.page
    dehumidifier = page.locator("#season-devices .season-device")
    assert page.locator("#plant #season-devices").count() == 1
    assert "già asciutta (50%)" in dehumidifier.inner_text() and dehumidifier.locator("button").count() == 0

    home.room(ROOMS, 27.0, 66.0)
    home.refresh()
    dehumidifier.locator("button[data-dehum-hours='1']").click()
    assert eventually(page, lambda: home.on("Dehumidifier") is True)
    assert eventually(page, lambda: home.on(f"{BATHS} Valve") is True), "its water, through the bathrooms' valve"
    home.refresh()
    assert home.state(BATHS) == t["st_excluded"], "the bathrooms aren't cooling for it"
    assert "Deumidificatore acceso fino alle" in page.inner_text("#hero-detail")
    dehumidifier.locator("button[data-dehum-off]").click()
    assert eventually(page, lambda: home.on("Dehumidifier") is False and home.on(f"{BATHS} Valve") is False)

    # Away, with the dehumidifier running and a zone held by hand
    dehumidifier.locator("button[data-dehum-hours='1']").click()
    assert eventually(page, lambda: home.on("Dehumidifier") is True)
    home.manual(ROOMS, hours="manual")
    page.click("#away-chip")
    sheet = page.locator("#sheet").inner_text()
    assert "anche quelle cambiate a mano" in sheet and "Deumidificatore" in sheet and "Sono rientrato" in sheet
    page.locator("#sheet button[data-away='24']").click()
    assert eventually(page, lambda: home.on("Dehumidifier") is False)
    assert home.on(f"{ROOMS} Valve") is False and home.on(f"{BATHS} Valve") is False
    assert home.on("Bathroom Bypass Valve") is True, "the bypass keeps the season"
    assert home.zone(ROOMS)["override"] is None
    home.refresh()
    assert home.card(ROOMS).locator(".stepper").count() == 0 and home.card(ROOMS).locator(".away-note").count() == 1
    assert t["away_device_off"] in page.locator("#season-devices").inner_text()
    for path, body, code in ((f"/api/climate/zones/{ROOMS}/override", {"target": 22, "hours": 1}, "override_while_away"),
                             ("/api/dehumidifier/on", {"hours": 1}, "dehumidifier_away"),
                             ("/api/sensors/Dehumidifier/on", {}, "switch_away")):
        assert home.call(path, body)["code"] == code

    # A day back far ahead: said whole; the sheet keeps it; the past refused
    page.click("#away-chip")
    back = date.today() + timedelta(days=17)
    page.fill("#away-day", back.isoformat())
    page.fill("#away-time", "07:30")
    page.locator("#sheet button[data-away='day']").click()
    assert eventually(page, lambda: f"{back.day} " in home.title() and str(back.year) in home.title())
    page.click("#away-chip")
    assert page.input_value("#away-day") == back.isoformat() and page.input_value("#away-time") == "07:30"
    page.click("#sheet-close")
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert home.call("/api/climate/away", {"until": past})["code"] == "away_until_invalid"

    # Back from a card: the week, not the old change; the dehumidifier off
    home.card(ROOMS).locator(".away-note button[data-action=back]").click()
    assert eventually(page, lambda: home.call("/api/climate/zones")["away_until"] is None)
    assert eventually(page, lambda: home.on(f"{ROOMS} Valve") is True)
    assert home.zone(ROOMS)["override"] is None and home.on("Dehumidifier") is False
    home.close()


def test_winter_both_zones_heat_and_away_closes_them(browser, house):
    home = Home(browser, house, "cool")
    home.manual(ROOMS, steps=2, hours="4")                       # held by hand in summer
    home.call("/api/climate/mode", {"mode": "heat"})
    home.room(ROOMS, 18.0, 50.0)
    home.room(BATHS, 19.0, 60.0)
    page = home.page
    page.reload()
    page.wait_for_selector(".zone", timeout=20000)
    t = home.t
    assert home.zone(ROOMS)["override"] is None, "the season drops a manual change"
    assert eventually(page, lambda: home.on(f"{ROOMS} Valve") is True and home.on(f"{BATHS} Valve") is True)
    home.refresh()
    assert home.state(ROOMS) == t["st_heating"] and home.state(BATHS) == t["st_heating"]
    assert home.card(BATHS).locator(".stepper").count() == 1, "the bathrooms are the resident's again"
    assert home.on("Bathroom Bypass Valve") is False, "bypass open in winter"
    assert page.locator("#season-devices .season-device").count() == 0
    assert home.call("/api/dehumidifier/on", {"hours": 1})["status"] == 409

    home.manual(ROOMS, hours="2", off=True)
    home.manual(BATHS, hours="4", off=True)
    assert home.on(f"{ROOMS} Valve") is False and home.on(f"{BATHS} Valve") is False
    assert home.title() == t["hero_off_now_heat"]
    home.back_to_the_week(BATHS)
    home.back_to_the_week(ROOMS)
    assert eventually(page, lambda: home.on(f"{ROOMS} Valve") is True and home.on(f"{BATHS} Valve") is True)

    page.click("#away-chip")
    assert "Deumidificatore" not in page.locator("#sheet").inner_text(), "none in winter"
    page.locator("#sheet button[data-away='72']").click()
    assert eventually(page, lambda: home.on(f"{ROOMS} Valve") is False and home.on(f"{BATHS} Valve") is False)
    home.refresh()
    home.card(BATHS).locator(".away-note button[data-action=back]").click()
    assert eventually(page, lambda: home.on(f"{ROOMS} Valve") is True and home.on(f"{BATHS} Valve") is True)
    page.click("#season-chip")
    sheet = page.locator("#sheet").inner_text()
    assert t["season_sheet_heat"] in sheet and "Deumidificatore" not in sheet
    page.click("#sheet-close")
    home.call("/api/climate/mode", {"mode": "cool"})
    home.close()
