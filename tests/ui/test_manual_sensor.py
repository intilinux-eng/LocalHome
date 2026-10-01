"""A zone whose sensor is set to "manual" in the admin panel: the
temperature it reads is written by hand - on the zone's card on the home
page, or in the admin panel - and the valve follows it at once. Its own
server (conftest.py's start_server): the first zone manual, the second
simulated; cooling, so a hot room opens the valve."""
import json
import urllib.parse
import urllib.request

import pytest

from .helpers import OVERFLOW_JS, SIMULATED_ZONE, UNTRANSLATED_JS, ZONE_A, ZONE_B, Opened, eventually

pytestmark = pytest.mark.ui

COOL_ALL_DAY = {d: [24.0] * 24 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


@pytest.fixture(scope="module")
def manual_server(start_server):
    base = start_server([dict(SIMULATED_ZONE, name=ZONE_A, sensor_mode="manual"), dict(SIMULATED_ZONE, name=ZONE_B)])
    call(base, f"/api/climate/zones/{urllib.parse.quote(ZONE_A, safe='')}/schedule", {"week": COOL_ALL_DAY, "mode": "cool"})
    return base


def call(base, path, body=None):
    req = urllib.request.Request(base + path, method="GET" if body is None else "POST",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.load(res)


def _zone(base):
    return next(z for z in call(base, "/api/climate/zones")["zones"] if z["name"] == ZONE_A)


def _open(browser, base, path="/", width=390):
    context = browser.new_context(viewport={"width": width, "height": 900})
    opened = Opened(context.new_page())
    opened.page.goto(base + path)
    return context, opened


def _card(page):
    return page.locator(".zone", has=page.locator("form.manual-set"))


def _write(card, text):
    card.locator(".manual-input").fill(text)
    card.locator("form.manual-set button[type=submit]").click()


def test_a_manual_zone_says_so_and_asks_for_a_temperature(browser, manual_server):
    context, opened = _open(browser, manual_server)
    page = opened.page
    page.wait_for_selector("form.manual-set", timeout=20000)
    card = _card(page)
    assert card.count() == 1, "only the manual zone has the field"
    head = card.locator(".zone-head").inner_text()
    assert "manuale" in head.lower() and "Scrivi la temperatura" in head
    assert "aggiornato" not in card.locator(".zone-foot").inner_text()
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []
    context.close()


def test_the_temperature_written_on_the_card_drives_the_valve(browser, manual_server):
    context, opened = _open(browser, manual_server)
    page = opened.page
    page.wait_for_selector("form.manual-set", timeout=20000)
    card = _card(page)
    _write(card, "27,5")                                   # cooling to 24: too warm
    assert eventually(page, lambda: _zone(manual_server)["temperature_c"] == 27.5)
    assert eventually(page, lambda: _zone(manual_server)["valve_on"] is True)
    assert eventually(page, lambda: "Sta raffrescando" in card.locator(".zone-head").inner_text())
    _write(card, "22")                                     # cool enough: it closes
    assert eventually(page, lambda: _zone(manual_server)["valve_on"] is False)
    assert opened.errors == []
    context.close()


def test_a_typo_on_the_card_is_refused(browser, manual_server):
    before = _zone(manual_server)["temperature_c"]
    context, opened = _open(browser, manual_server)
    page = opened.page
    page.wait_for_selector("form.manual-set", timeout=20000)
    _write(_card(page), "215")
    assert eventually(page, lambda: "tra -30 e 60" in page.inner_text("body"))
    assert _zone(manual_server)["temperature_c"] == before
    context.close()


def test_the_admin_panel_has_the_field_right_under_the_sensor(browser, manual_server):
    context, opened = _open(browser, manual_server, "/climate-admin")
    page = opened.page
    page.wait_for_selector("#zone-forms .admin-manual-box", state="attached", timeout=20000)
    page.evaluate("document.getElementById('zones').open = true")
    box = page.locator("#zone-forms .admin-manual-box")
    assert box.count() == 1
    assert "Temperatura scritta a mano" in box.inner_text()
    box.locator(".manual-temp-input").fill("26")
    box.locator("button").click()
    assert eventually(page, lambda: _zone(manual_server)["temperature_c"] == 26.0)
    assert page.evaluate(OVERFLOW_JS) <= 0 and page.evaluate(UNTRANSLATED_JS) == []
    assert opened.errors == []
    context.close()
