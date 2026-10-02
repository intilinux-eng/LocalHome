"""The admin page in a browser: the overview and the section headers fill
in, the bar follows the scroll, page-wide notices come and go, refusals
arrive in the page's language. See conftest.py for the server."""
import pytest

from .helpers import OVERFLOW_JS, UNTRANSLATED_JS, ZONE_A, eventually

pytestmark = pytest.mark.ui

SECTION_PILLS = ("season-summary", "zones-summary", "broker-status", "extra-switches-summary",
                 "extra-sensor-summary", "vmc-summary", "system-status-summary", "led-matrix-summary",
                 "diagnostic-summary")


@pytest.mark.parametrize("width", [1280, 390, 320])
def test_the_overview_and_every_header_say_how_things_are(open_page, width):
    opened = open_page("/climate-admin", width)
    page = opened.page
    tiles = page.locator(".ov-tile")
    assert tiles.count() == 6
    assert eventually(page, lambda: "…" not in tiles.locator(".ov-value").all_inner_texts())
    assert tiles.nth(1).get_attribute("data-state") == "cool"
    assert eventually(page, lambda: all(page.locator(f"#{pid}").inner_text().strip() for pid in SECTION_PILLS))
    assert page.evaluate(UNTRANSLATED_JS) == []
    body = page.inner_text("body")
    assert "Bathroom Bypass Valve" not in body and "Dehumidifier" not in body, "fixed names shown in Italian"
    assert page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_the_bar_follows_the_scroll(open_page):
    page = open_page("/climate-admin", 390).page
    page.wait_for_timeout(1500)
    current = lambda: page.locator("#admin-nav a.current").all_inner_texts()
    assert eventually(page, lambda: current() == ["Panoramica"])
    # Sections start closed; one open gives the page room to scroll past the group.
    page.evaluate("document.getElementById('system-status').open = true")
    page.wait_for_timeout(500)
    page.evaluate("document.getElementById('group-board').scrollIntoView()")
    assert eventually(page, lambda: current() == ["Stato sistema"])
    assert abs(page.locator("#admin-nav").bounding_box()["y"]) < 1, "the bar stays at the top"
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    assert eventually(page, lambda: current() == ["Strumenti"])


def test_a_tile_opens_its_section_and_it_stays_open_after_a_reload(open_page):
    page = open_page("/climate-admin", 1280).page
    section = page.locator("#led-matrix")
    assert not section.evaluate("d => d.open")
    page.locator("#ov-led").click()
    assert section.evaluate("d => d.open")
    # The restart button reloads the page: what was open stays open.
    page.reload()
    page.wait_for_timeout(500)
    assert section.evaluate("d => d.open")


def test_diagnostic_mode_turned_on_elsewhere_is_said_at_the_top(open_page, api):
    page = open_page("/climate-admin", 390).page
    alert = page.locator("#diagnostic-alert")
    page.wait_for_timeout(1500)
    assert alert.is_hidden()
    try:
        api("/api/climate/diagnostic-mode", {"enabled": True})    # from another phone
        assert eventually(page, alert.is_visible)
        assert page.locator("#diagnostic").evaluate("d => d.open"), "its section opens by itself"
        assert page.locator("#ov-season").get_attribute("data-state") == "bad"
        alert.locator("a").click()
        page.wait_for_timeout(600)
        top = page.locator("#diagnostic").bounding_box()["y"]
        assert 30 < top < 150, f"lands on the section, below the bar (y={top})"
    finally:
        api("/api/climate/diagnostic-mode", {"enabled": False})
    assert eventually(page, alert.is_hidden)


def test_a_save_waiting_for_a_restart_is_named_at_the_top(open_page, api):
    before = api("/api/climate-admin/extra-sensors")
    page = open_page("/climate-admin", 1280).page
    bar = page.locator("#restart-bar")
    page.wait_for_timeout(1000)
    assert bar.is_hidden()
    page.locator("#extra-sensor > summary").click()
    page.select_option("#extra-outdoor-sensor-mode", "none" if before["outdoor_sensor_mode"] != "none" else "simulated")
    page.locator("#save-extra-sensor-btn").click()
    try:
        assert eventually(page, bar.is_visible)
        assert "Sensore temperatura esterna" in page.inner_text("#restart-bar-text")
        assert page.locator("#restart-bar button").count() == 1
        assert "Riavvio" in page.inner_text("#extra-sensor-summary")
    finally:
        api("/api/climate-admin/extra-sensors", {"outdoor_sensor_mode": before["outdoor_sensor_mode"],
                                                 "outdoor_sensor_device_id": before["outdoor_sensor_device_id"]})
    page.reload()
    page.wait_for_timeout(1500)
    assert bar.is_hidden(), "put back as it was: nothing waits"


def test_a_refusal_is_explained_in_the_page_language(open_page, api):
    page = open_page("/climate-admin", 1280).page
    page.locator("#zones > summary").click()          # closed until needed
    card = page.locator("#zone-forms .admin-zone-card").first
    assert eventually(page, lambda: card.count() == 1)
    card.locator(".zone-sensor-mode").select_option("shelly")    # and no device id typed
    page.locator("#save-btn").click()
    result = page.locator("#save-result")
    assert eventually(page, lambda: result.inner_text().strip() != "")
    text = result.inner_text()
    assert "inserisci l'ID del sensore Shelly" in text and ZONE_A in text, text
    assert api("/api/climate-admin/map")["zones"][0]["sensor_mode"] == "simulated", "nothing saved"


def test_both_pages_say_when_the_board_stops_answering(open_page):
    pages = [open_page("/climate-admin", 390).page, open_page("/classic", 390).page]
    for page in pages:
        page.wait_for_timeout(1500)
        assert page.locator("#connection-banner").is_hidden()
        page.route("**/api/**", lambda route: route.abort())
    for page in pages:
        assert eventually(page, page.locator("#connection-banner").is_visible, timeout_ms=40000)
        assert "non raggiungibile" in page.inner_text("#connection-banner")
    for page in pages:
        page.unroute("**/api/**")
    for page in pages:
        assert eventually(page, page.locator("#connection-banner").is_hidden, timeout_ms=40000), "recovers by itself"


def test_the_vmc_speeds_are_set_here_and_shown_on_the_home_page(open_page, api):
    """Night, day and boost percentages: refused when out of order, in use
    at once when saved - the home card's buttons say so, no restart."""
    before = api("/api/climate-admin/vmc-speeds")["speeds"]
    opened = open_page("/climate-admin", 390)
    page = opened.page
    page.wait_for_selector("#vmc-speeds:not([hidden])", state="attached", timeout=20000)
    page.evaluate("document.getElementById('vmc').open = true")
    page.fill("#vmc-speed-night", "60")
    page.click("#save-vmc-speeds-btn")
    assert eventually(page, lambda: "Notte deve essere" in page.inner_text("#save-vmc-speeds-result"))
    page.fill("#vmc-speed-night", "30")
    page.click("#save-vmc-speeds-btn")
    assert eventually(page, lambda: api("/api/climate-admin/vmc-speeds")["speeds"]["night"] == 30)
    home = open_page("/", 390).page
    home.wait_for_selector("#vmc-card button[data-vmc]", timeout=20000)
    assert "30%" in home.locator("#vmc-card button[data-vmc]").first.inner_text()
    api("/api/climate-admin/vmc-speeds", before)
    # The browser logs the refused save's 400; nothing else.
    assert [e for e in opened.errors if "400" not in e] == []
