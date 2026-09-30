"""The redesigned home page in a browser: it lays out at every width in
both colour schemes, names stay text, a zone is changed for a while and
back, the week is edited in bands and saved only on Save, and "away"
works. See conftest.py for the server (cooling, two zones)."""
import urllib.parse

import pytest

from .helpers import OVERFLOW_JS, UNTRANSLATED_JS, ZONE_A, ZONE_B, eventually

pytestmark = pytest.mark.ui

EMPTY_WEEK = {d: [None] * 24 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def _zone(api, name):
    return next(z for z in api("/api/climate/zones")["zones"] if z["name"] == name)


def _week(api, name):
    return api(f"/api/climate/zones/{urllib.parse.quote(name, safe='')}/schedule?mode=cool")["week"]


def _set_week(api, name, week):
    api(f"/api/climate/zones/{urllib.parse.quote(name, safe='')}/schedule", {"week": week, "mode": "cool"})


def _open_home(open_page, width=390, scheme="light"):
    opened = open_page("/", width)
    opened.page.emulate_media(color_scheme=scheme)
    opened.page.wait_for_selector(".zone", timeout=20000)
    opened.page.wait_for_timeout(800)
    return opened


def _card(page, name):
    return page.locator(f".zone[data-zone={_css_string(name)}]")


def _css_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@pytest.mark.parametrize("width", [1280, 390, 320])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_home_page_lays_out_and_says_everything_in_italian(open_page, width, scheme):
    opened = _open_home(open_page, width, scheme)
    page = opened.page
    assert page.locator(".zone").count() == 2
    assert page.locator("#hero-title").inner_text().strip() not in ("", "Un attimo…")
    assert page.evaluate(UNTRANSLATED_JS) == []
    assert page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_names_stay_text_on_the_home_page_and_in_the_plan(open_page):
    dialogs = []
    opened = _open_home(open_page, 1280)
    page = opened.page
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    assert ZONE_B in page.inner_text("#zones")
    _card(page, ZONE_B).locator("button[data-action=plan]").click()
    page.wait_for_selector("#sheet:not([hidden])")
    assert ZONE_B in page.inner_text("#sheet-title")
    assert page.locator("img[src='x']").count() == 0
    assert dialogs == [] and opened.errors == []


def test_a_zone_is_changed_for_a_while_and_back(open_page, api):
    page = _open_home(open_page).page
    card = _card(page, ZONE_A)
    card.locator("button[data-action=start][data-delta='0.5']").click()
    card.locator(".pending button[data-action=step][data-delta='0.5']").click()
    card.locator(".pending button[data-hours='4']").click()
    wanted = float(card.locator(".pending .stepper-value").inner_text().replace(",", ".").rstrip("°"))
    card.locator("button[data-action=apply]").click()
    assert eventually(page, lambda: (_zone(api, ZONE_A)["override"] or {}).get("target") == wanted)
    page.wait_for_selector(f".zone[data-zone={_css_string(ZONE_A)}] button[data-action=resume]")
    card.locator("button[data-action=resume]").click()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)


def test_the_week_is_edited_in_bands_and_saved_only_on_save(open_page, api):
    week = {d: [24.0] * 8 + [26.0] * 16 for d in EMPTY_WEEK}
    _set_week(api, ZONE_A, week)
    opened = _open_home(open_page)
    page = opened.page
    _card(page, ZONE_A).locator("button[data-action=plan]").click()
    page.wait_for_selector("#sheet:not([hidden]) .band")
    page.locator("#sheet button[data-plan=day][data-day='0']").click()          # Monday
    first = page.locator("#sheet .band").first
    first.locator("button[data-plan=temp][data-delta='0.5']").click()
    first = page.locator("#sheet .band").first
    first.locator("button[data-plan=end][data-delta='1']").click()
    assert _week(api, ZONE_A)["mon"][0] == 24.0, "nothing is saved before Save"
    page.locator("#sheet button[data-plan=save]").click()
    assert eventually(page, lambda: _week(api, ZONE_A)["mon"][:10] == [24.5] * 9 + [26.0])
    assert _week(api, ZONE_A)["tue"][0] == 24.0, "only Monday changed"
    assert opened.errors == []


def test_away_is_set_from_the_hero_and_ended(open_page, api):
    page = _open_home(open_page).page
    page.click("#away-chip")
    page.locator("#sheet button[data-away='24']").click()
    assert eventually(page, lambda: api("/api/climate/zones")["away_until"] is not None)
    assert eventually(page, lambda: page.locator("#away-chip").get_attribute("aria-pressed") == "true")
    page.click("#away-chip")
    page.locator("#sheet button[data-away='back']").click()
    assert eventually(page, lambda: api("/api/climate/zones")["away_until"] is None)


def test_the_home_page_says_when_the_board_stops_answering(open_page):
    page = _open_home(open_page).page
    assert page.locator("#offline").is_hidden()
    page.route("**/api/**", lambda route: route.abort())
    assert eventually(page, page.locator("#offline").is_visible, timeout_ms=60000)
    page.unroute("**/api/**")
    assert eventually(page, page.locator("#offline").is_hidden, timeout_ms=60000)
