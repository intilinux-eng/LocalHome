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
    return start_server([dict(SIMULATED_ZONE, name=ZONE_A), dict(SIMULATED_ZONE, name=ZONE_B, cool_enabled=False)], config)


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


def test_the_bypass_says_closed_and_the_dehumidifier_what_it_opens(browser, plant):
    context, opened = _open(browser, plant)
    page = opened.page
    page.wait_for_selector("#plant:not([hidden]) .tile", timeout=20000)
    page.wait_for_timeout(1500)
    tiles = {t.locator(".tile-name").inner_text(): t for t in page.locator("#plant-body .tile").all()}
    bypass = tiles["Valvola Bypass Bagni"]
    assert bypass.locator(".dot-state").inner_text() == "chiusa", "its Shelly is on in summer: the valve closed"
    assert "normalmente aperta" in bypass.inner_text()
    assert ZONE_B in tiles["Deumidificatore"].inner_text()
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []
    context.close()


def test_switching_the_dehumidifier_on_opens_the_bathrooms_valve_and_says_so(browser, plant):
    context, opened = _open(browser, plant)
    page = opened.page
    page.wait_for_selector("#plant-body button[data-switch='Deumidificatore']", timeout=20000)
    page.click("#plant-body button[data-switch='Deumidificatore']")
    assert eventually(page, lambda: _valve_on(plant, f"{ZONE_B} Valve") is True)
    card = page.locator(".zone", has_text=ZONE_B)
    assert eventually(page, lambda: "Deumidificatore acceso: valvola aperta" in card.locator(".zone-head").inner_text(),
                      timeout_ms=30000)
    page.click("#plant-body button[data-switch='Deumidificatore']")
    assert eventually(page, lambda: _valve_on(plant, f"{ZONE_B} Valve") is False)
    assert opened.errors == []
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
