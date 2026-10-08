"""The plant on the home page and the admin panel, as on the production
board: a bypass valve that is normally open (its Shelly on closes it)
and a dehumidifier that draws its water through the bathrooms' valve -
a zone kept out of cooling, whose valve it opens while it runs. Its own
server, in summer (conftest.py's start_server)."""
import urllib.parse

import pytest

from .helpers import OVERFLOW_JS, SIMULATED_ZONE, UNTRANSLATED_JS, ZONE_A, ZONE_B, Opened, eventually

pytestmark = pytest.mark.ui

CONFIG = """\
web:
  host: "127.0.0.1"
  port: {port}
  language: "it"

integrations:
  - kind: switch
    type: simulated
    name: "Deumidificatore"
    dashboard_tab: climate
  - kind: switch
    type: simulated
    name: "Valvola Bypass Bagni"
    dashboard_tab: climate
    normally_open: true

thermostat:
  mode: "cool"
  hysteresis_c: 0.5
  schedules_file: "schedules.json"
  poll_interval_seconds: 60
  mode_switches:
    - switch: "Valvola Bypass Bagni"
      active_in_mode: "cool"
  mode_limited_switches:
    - switch: "Deumidificatore"
      only_in_mode: "cool"
  interlocks:
    - leader: "Deumidificatore"
      follower: '{follower}'
      active_in_mode: "cool"

history:
  db_file: "data/history.db"
  poll_interval_seconds: 30
"""


@pytest.fixture(scope="module")
def plant(start_server):
    # The config's braces are YAML's, not str.format's - only {port} is filled later.
    config = CONFIG.replace("{follower}", f"{ZONE_B} Valve".replace("'", "''"))
    # The first cooled zone's humidity decides on the dehumidifier: typed by
    # hand here, exact, instead of a simulated one wandering about 50%.
    return start_server([dict(SIMULATED_ZONE, name=ZONE_A, sensor_mode="manual"),
                         dict(SIMULATED_ZONE, name=ZONE_B, cool_enabled=False)], config)


def _humidity(base, value):
    call(base, f"/api/climate-admin/zones/{urllib.parse.quote(ZONE_A, safe='')}/manual-value",
         {"temperature_c": 26.0, "humidity_pct": value})


def call(base, path, body=None):
    import json
    import urllib.request
    req = urllib.request.Request(base + path, method="GET" if body is None else "POST",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.load(res)


def _open(browser, base, path="/", width=390):
    context = browser.new_context(viewport={"width": width, "height": 900})
    opened = Opened(context.new_page())
    opened.page.goto(base + path)
    return context, opened


def _valve_on(base, name):
    return call(base, f"/api/sensors/{urllib.parse.quote(name, safe='')}")["is_on"]


def test_the_home_page_has_the_dehumidifier_in_the_plant_and_none_of_the_plants_valves(browser, plant):
    """The home is for whoever lives there: the dehumidifier, switched by
    hand in summer, first in the plant's section - always there, on or off;
    the bypass moving with the season and the valve the dehumidifier opens
    are the admin panel's."""
    context, opened = _open(browser, plant)
    page = opened.page
    page.wait_for_selector("#season-devices:not([hidden]) .season-device", timeout=20000)
    page.wait_for_timeout(1500)
    device = page.locator("#season-devices .season-device")
    assert device.count() == 1 and "Deumidificatore" in device.inner_text()
    assert page.locator("#plant #season-devices").count() == 1 and page.locator("#plant").is_visible()
    assert page.locator("#plant-body .tile", has_text="Bypass").count() == 0
    assert page.locator("#plant-body .tile", has_text="Deumidificatore").count() == 0
    assert "Deumidificatore" not in page.locator(".zone", has_text=ZONE_B).inner_text()
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []
    context.close()


def test_the_dehumidifier_is_switched_on_for_a_while_and_opens_the_bathrooms_valve_unsaid(browser, plant):
    """On for 1, 2 or 4 hours - it draws a lot of power - until when, and
    Off; the top of the page says it runs (its card is down in the plant);
    the valve it opens for its water is none of the home's business."""
    _humidity(plant, 60)
    context, opened = _open(browser, plant)
    page = opened.page
    one_hour = "#season-devices button[data-dehum-hours='1']"
    page.wait_for_selector(one_hour, timeout=20000)
    page.click(one_hour)
    assert eventually(page, lambda: _valve_on(plant, f"{ZONE_B} Valve") is True), "it still opens the valve"
    device = page.locator("#season-devices .season-device")
    assert eventually(page, lambda: device.get_attribute("data-on") == "true")
    assert "Acceso fino alle" in device.inner_text()
    assert eventually(page, lambda: "Deumidificatore acceso fino alle" in page.inner_text("#hero-detail"))
    card = page.locator(".zone", has_text=ZONE_B)
    page.wait_for_timeout(1500)
    assert "Esclusa d'estate" in card.locator(".zone-head").inner_text(), "the bathrooms aren't cooling for it"
    assert ZONE_B not in page.inner_text("#hero-title")
    page.click("#season-devices button[data-dehum-off]")
    assert eventually(page, lambda: _valve_on(plant, f"{ZONE_B} Valve") is False)
    assert opened.errors == []
    context.close()


def test_the_dehumidifier_is_off_and_not_offered_with_the_air_dry_enough(browser, plant):
    """Below its start threshold (55% by default) it can't be switched on:
    the card says it's off, the humidity and from when it would be - no
    buttons to tap for nothing."""
    _humidity(plant, 52)
    context, opened = _open(browser, plant)
    page = opened.page
    page.wait_for_selector("#season-devices .season-device", timeout=20000)
    card = page.locator("#season-devices .season-device-dehum")
    assert eventually(page, lambda: card.get_attribute("data-state") == "disabled")
    assert "Spento: l'aria è già asciutta (52%). Si può accendere dal 55%." in card.inner_text()
    assert "Deumidificatore" not in page.inner_text("#hero-detail")
    assert card.locator("button").count() == 0
    res = call(plant, "/api/dehumidifier")["dehumidifier"]
    assert res["humidity"] == 52 and res["on"] is False
    _humidity(plant, 60)
    assert opened.errors == []
    context.close()


def test_the_dehumidifier_runs_until_the_room_is_dry_enough(browser, plant):
    _humidity(plant, 60)
    context, opened = _open(browser, plant)
    page = opened.page
    target = "#season-devices button[data-dehum-target]"
    page.wait_for_selector(target, timeout=20000)
    assert page.inner_text(target) == "Fino al 50%"
    page.click(target)
    card = page.locator("#season-devices .season-device-dehum")
    assert eventually(page, lambda: card.get_attribute("data-on") == "true")
    assert "Acceso finché l'umidità scende al 50%" in card.inner_text()
    assert call(plant, "/api/dehumidifier")["dehumidifier"]["mode"] == "target"
    page.click("#season-devices button[data-dehum-off]")
    assert eventually(page, lambda: card.get_attribute("data-on") == "false")
    assert opened.errors == []
    context.close()


def test_the_dehumidifiers_thresholds_are_set_in_the_admin_and_in_force_at_once(browser, plant):
    """The season section: the thresholds, the durations, "until the
    target" and the automatic switching on (off by default) - saved, no
    restart, and the home page follows."""
    _humidity(plant, 60)
    context, opened = _open(browser, plant, "/climate-admin")
    page = opened.page
    page.evaluate("document.getElementById('season').open = true")
    box = page.locator("#dehum-settings")
    page.wait_for_selector("#dehum-settings [data-dehum='stop_below']", timeout=20000)
    assert box.locator("[data-dehum='auto']").is_checked() is False
    assert box.locator("#dehum-auto-fields input").first.is_disabled(), "the automatic's hours wait for it to be on"
    box.locator("[data-dehum='stop_below']").fill("48")
    box.locator("[data-dehum='start_from']").fill("49")
    box.locator("button.open").click()
    assert eventually(page, lambda: "2 punti" in box.inner_text()), "refused, in words"
    box.locator("[data-dehum='start_from']").fill("53")
    box.locator("[data-dehum-duration='0.5']").check()
    box.locator("button.open").click()
    assert eventually(page, lambda: "vale da subito" in page.inner_text("#dehum-settings"))
    assert eventually(page, lambda: "fino al 48%" in page.inner_text("#season-cards")), "the summer card says it"
    home, home_opened = _open(browser, plant)
    home_page = home_opened.page
    home_page.wait_for_selector("#season-devices button[data-dehum-target]", timeout=20000)
    assert home_page.inner_text("#season-devices button[data-dehum-target]") == "Fino al 48%"
    assert home_page.locator("#season-devices button[data-dehum-hours='0.5']").inner_text() == "30 min"
    call(plant, "/api/dehumidifier/settings", {"reset": True})
    # The refused save is a 400 the browser notes on its own.
    assert [e for e in opened.errors if "status of 400" not in e] == [] and home_opened.errors == []
    home.close()
    context.close()


def test_the_admin_says_closed_open_and_lists_the_link(browser, plant):
    context, opened = _open(browser, plant, "/climate-admin")
    page = opened.page
    page.wait_for_selector("#season-rules .season-rule", state="attached", timeout=20000)
    page.evaluate("document.getElementById('season').open = true")
    assert eventually(page, lambda: "Ora chiusa" in page.inner_text("#season-rules"), timeout_ms=20000)
    # Written in config.yaml: listed once, without the panel's empty places beside them.
    names = page.locator("#season-rules .season-rule strong").all_inner_texts()
    assert sorted(names) == ["Deumidificatore", "Valvola Bypass Bagni"], names
    assert page.locator("#season-rules-actions").is_hidden()
    assert "sempre chiusa (Shelly acceso)" in page.inner_text("#season-rules")
    assert eventually(page, lambda: "Deumidificatore acceso" in page.inner_text("#season-links"))
    # Each season's card says what happens to every device in it - the home page doesn't.
    summer = page.inner_text("#season-cards .season-cool")
    assert "Valvola Bypass Bagni: sempre chiusa (Shelly acceso)" in summer
    assert ("Deumidificatore: a scelta - si accende dalla home per 1 h, 2 h, 4 h o fino al 50% (al massimo 8 h); "
            "mai da solo; si spegne da solo sotto il 50%") in summer
    winter = page.inner_text("#season-cards .season-heat")
    assert "Valvola Bypass Bagni: sempre aperta (Shelly spento)" in winter and "Deumidificatore: sempre spento" in winter
    assert page.evaluate(UNTRANSLATED_JS) == []
    assert opened.errors == []
    context.close()


def test_the_admin_says_live_whether_the_valve_follows_the_dehumidifier(browser, plant):
    context, opened = _open(browser, plant, "/climate-admin")
    page = opened.page
    page.evaluate("document.getElementById('season').open = true")
    links = page.locator("#season-links")
    assert eventually(page, lambda: "Ora: Deumidificatore spento, valvola chiusa" in links.inner_text(), timeout_ms=20000)
    assert "Come previsto" in links.inner_text()
    call(plant, "/api/sensors/Deumidificatore/on", {})
    assert eventually(page, lambda: "Ora: Deumidificatore acceso, valvola aperta" in links.inner_text(), timeout_ms=20000)
    assert "Come previsto" in links.inner_text()
    call(plant, "/api/sensors/Deumidificatore/off", {})
    assert eventually(page, lambda: "Ora: Deumidificatore spento, valvola chiusa" in links.inner_text(), timeout_ms=20000)
    assert page.evaluate(UNTRANSLATED_JS) == []
    assert opened.errors == []
    context.close()



def test_diagnostic_mode_speaks_of_the_valve_and_the_bypass_the_other_way_round(browser, plant):
    """Open and Close mean the valve; the Shelly's contact is said beside
    it. A zone valve opens when the contact closes, the normally open
    bypass closes when it does - its Open button switches the relay off.
    The dehumidifier isn't a valve: switched on and off."""
    call(plant, "/api/climate/diagnostic-mode", {"enabled": True})
    context, opened = _open(browser, plant, "/climate-admin")
    page = opened.page
    try:
        page.evaluate("document.getElementById('diagnostic').open = true")
        rows = page.locator("#diagnostic-valves .admin-diagnostic-row")
        assert eventually(page, lambda: rows.count() >= 3, timeout_ms=20000)
        row = lambda name: page.locator("#diagnostic-valves .admin-diagnostic-row", has_text=name).first
        bypass = row("Valvola Bypass Bagni")
        # summer: the bypass's Shelly is on - the valve closed
        assert "Valvola chiusa" in bypass.inner_text() and "contatto Shelly chiuso" in bypass.inner_text()
        bypass.get_by_role("button", name="Apri valvola").click()
        assert eventually(page, lambda: _valve_on(plant, "Valvola Bypass Bagni") is False), "opening it switches the relay off"
        assert eventually(page, lambda: "Valvola aperta" in row("Valvola Bypass Bagni").inner_text(), timeout_ms=10000)
        zone = row(f"{ZONE_A} Valve")
        zone.get_by_role("button", name="Apri valvola").click()
        assert eventually(page, lambda: _valve_on(plant, f"{ZONE_A} Valve") is True)
        assert eventually(page, lambda: "contatto Shelly chiuso" in row(f"{ZONE_A} Valve").inner_text(), timeout_ms=10000)
        dehumidifier = row("Deumidificatore")
        assert dehumidifier.get_by_role("button", name="Accendi").count() == 1
        assert dehumidifier.get_by_role("button", name="Apri valvola").count() == 0
        assert page.evaluate(UNTRANSLATED_JS) == []
        assert opened.errors == []
    finally:
        call(plant, "/api/climate/diagnostic-mode", {"enabled": False})
        context.close()



def test_the_bathrooms_zone_groups_its_bypass_and_dehumidifier_in_devices(browser, plant):
    """The zone kept out of cooling is the bathrooms': its sensor and valve,
    the dehumidifier that draws water through that valve and the bypass,
    together under its name."""
    context, opened = _open(browser, plant, "/climate-admin")
    page = opened.page
    page.evaluate("document.getElementById('devices').open = true")
    items = page.locator("#device-list > li")
    assert eventually(page, lambda: any(f"Valvola {ZONE_B}" in t for t in items.all_inner_texts()), timeout_ms=20000)
    page.wait_for_timeout(1500)
    texts = [t.strip() for t in items.all_inner_texts()]
    start = next(i for i, t in enumerate(texts) if t.lower() == f"zona {ZONE_B}".lower())
    group = texts[start + 1:start + 5]
    assert f"Sensore {ZONE_B}" in group[0] and f"Valvola {ZONE_B}" in group[1]
    assert {"Deumidificatore", "Valvola Bypass Bagni"} == {next(n for n in ("Deumidificatore", "Valvola Bypass Bagni") if n in t) for t in group[2:]}
    assert opened.errors == []
    context.close()
