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
    page.evaluate("document.getElementById('extra-switches').open = true")
    assert eventually(page, lambda: "Ora chiusa" in page.inner_text("#season-rules"), timeout_ms=20000)
    assert "sempre chiusa (Shelly acceso)" in page.inner_text("#season-rules")
    assert eventually(page, lambda: "Deumidificatore acceso" in page.inner_text("#season-links"))
    assert page.evaluate(UNTRANSLATED_JS) == []
    assert opened.errors == []
    context.close()
