"""A board straight after the README's installation - or a factory reset:
Stanze and Bagni with every Shelly still to configure. The admin panel
lists them, by zone, with the model to look for; the home says what isn't
connected yet, offers nothing it can't do, and calls none of it a fault.
"""
import json
import urllib.request

import pytest

from .conftest import EMPTY_CONFIG
from .helpers import OVERFLOW_JS, UNTRANSLATED_JS, Opened, eventually

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def factory(start_server):
    return start_server(None, EMPTY_CONFIG)


def _open(browser, base, path="/", width=390):
    context = browser.new_context(viewport={"width": width, "height": 900})
    opened = Opened(context.new_page())
    opened.page.goto(base + path)
    return context, opened


def test_the_home_says_what_isnt_connected_yet(browser, factory):
    context, opened = _open(browser, factory)
    page = opened.page
    try:
        assert eventually(page, lambda: page.locator(".zone").count() == 2, timeout_ms=20000)
        assert page.locator("#hero-title").inner_text() == "Impianto non ancora collegato"
        assert page.locator(".zone .state").all_inner_texts() == ["Non ancora collegata"] * 2
        # Nothing to change by hand where nothing can act on it: where to set it up.
        assert page.locator(".zone [data-action='start']").count() == 0
        notes = page.locator(".zone .setup-note")
        assert notes.count() == 2 and notes.first.locator("a").get_attribute("href") == "/climate-admin"
        assert "Sensore non ancora collegato" in page.locator("#outdoor-chip").inner_text()

        health = page.locator("#health")
        assert eventually(page, lambda: health.inner_text().strip() == "Da configurare")
        assert health.get_attribute("data-state") == "setup", "not a fault"
        health.click()
        sheet = page.locator("#sheet")
        assert eventually(page, lambda: "Ancora da collegare" in sheet.inner_text())
        text = sheet.inner_text()
        assert "Niente di guasto" in text
        for name in ("Sensore Stanze", "Valvola Bagni", "Valvola bypass bagni", "Deumidificatore",
                     "Sensore temperatura esterna", "Ventilazione"):
            assert name in text, name

        assert page.evaluate(UNTRANSLATED_JS) == []
        assert page.evaluate(OVERFLOW_JS) == 0
        assert opened.errors == []
    finally:
        context.close()


def test_the_admin_lists_every_shelly_to_configure(browser, factory):
    context, opened = _open(browser, factory, "/climate-admin", 1280)
    page = opened.page
    try:
        page.evaluate("document.getElementById('devices').open = true")
        items = page.locator("#device-list > li")
        assert eventually(page, lambda: items.count() == 11, timeout_ms=20000)
        lines = [" ".join(t.split()) for t in items.all_inner_texts()]
        assert [line.lower() for line in lines] == [line.lower() for line in [
            "Zona Stanze",
            "Da configurare Sensore Stanze Shelly H&T Gen3",
            "Da configurare Valvola Stanze Shelly 1 Gen3",
            "Zona Bagni",
            "Da configurare Sensore Bagni Shelly H&T Gen3",
            "Da configurare Valvola Bagni Shelly 1 Gen3",
            "Da configurare Valvola bypass bagni Shelly 1 Gen3",
            "Da configurare Deumidificatore Shelly 1 Gen3",
            "Altri dispositivi",
            "Da configurare Sensore temperatura esterna Shelly H&T Gen3",
            "Da configurare VMC Shelly Dimmer 0/1-10V Gen3",
        ]], lines
        assert page.locator("#devices-summary").inner_text() == "8 Shelly da configurare"
        assert page.locator("#zones-summary").inner_text() == "Shelly da configurare"
        assert page.locator("#ov-zones .ov-value").inner_text() == "Shelly da configurare"
        # The zones as the apartment has them, on Shelly, their ids to type in.
        names = page.locator("#zone-forms .zone-name")
        assert [names.nth(i).input_value() for i in range(2)] == ["Stanze", "Bagni"]
        assert page.locator("#zone-forms .zone-sensor-mode").first.input_value() == "shelly"
        assert page.locator("#extra-outdoor-sensor-mode").input_value() == "shelly"
        assert page.locator("#extra-vmc-mode").input_value() == "shelly"
        assert page.locator("#guide").is_visible(), "the how-to, open on a new board"

        assert page.evaluate(UNTRANSLATED_JS) == []
        assert opened.errors == []
    finally:
        context.close()


def test_one_shelly_at_a_time(browser, factory):
    """Last here: it saves. The valve's id typed in alone - the sensor's
    still to come - saves, leaves the list, and waits for the restart."""
    context, opened = _open(browser, factory, "/climate-admin", 1280)
    page = opened.page
    try:
        page.locator("#zones > summary").click()
        valve = page.locator("#zone-forms .zone-valve-device-id").first
        assert eventually(page, valve.is_visible, timeout_ms=20000)
        valve.fill("shelly1g3-aabbccddeeff")
        page.locator("#save-btn").click()
        assert eventually(page, lambda: "Salvato" in page.locator("#save-result").inner_text())

        with urllib.request.urlopen(factory + "/api/climate/zones") as res:
            todo = [item["name"] for item in json.load(res)["to_configure"]]
        assert "Stanze Valve" not in todo and "Stanze Sensor" in todo
        assert eventually(page, lambda: page.locator("#restart-bar").is_visible())
        page.evaluate("document.getElementById('devices').open = true")
        assert eventually(page, lambda: page.locator("#devices-summary").inner_text() == "7 Shelly da configurare")
        assert opened.errors == []
    finally:
        context.close()
