"""The dashboard in a browser: it lays out at every width, names are shown
as text and never run as HTML, and two phones editing the same schedule
both keep their changes. See conftest.py for the server."""
import re
import urllib.parse

import pytest

from .helpers import OVERFLOW_JS, UNTRANSLATED_JS, ZONE_A, ZONE_B, eventually

pytestmark = pytest.mark.ui

EMPTY_WEEK = {d: [None] * 24 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def slug(name):
    return re.sub(r"[^a-z0-9]", "-", name, flags=re.IGNORECASE).lower()


def _schedule(api, zone):
    return api(f"/api/climate/zones/{urllib.parse.quote(zone, safe='')}/schedule?mode=cool")["week"]


def _set_week(api, zone, week):
    api(f"/api/climate/zones/{urllib.parse.quote(zone, safe='')}/schedule", {"week": week, "mode": "cool"})


def open_climate(open_page, width, zone=None, pane="schedule"):
    opened = open_page("/", width)
    page = opened.page
    if zone:
        page.evaluate(f"""() => {{ try {{
            localStorage.setItem('localhome-zone', {slug(zone)!r});
            localStorage.setItem('localhome-zone-pane', {pane!r});
        }} catch (e) {{}} }}""")
        page.reload()
    page.evaluate("switchTab('climate')")
    page.wait_for_selector(".zone-tile", timeout=20000)
    return opened


@pytest.mark.parametrize("width", [1280, 390, 320])
def test_the_climate_tab_lays_out_and_says_everything_in_italian(open_page, width):
    opened = open_climate(open_page, width)
    page = opened.page
    page.wait_for_timeout(1500)
    assert page.locator(".zone-tile").count() == 2
    assert page.evaluate(UNTRANSLATED_JS) == []
    assert page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_names_are_shown_as_text_never_run_as_html(open_page):
    dialogs = []
    for path in ("/", "/climate-admin"):
        opened = open_page(path, 1280)
        page = opened.page
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.reload()
        if path == "/":
            page.evaluate("switchTab('climate')")
        page.wait_for_timeout(2500)
        assert ZONE_B in page.inner_text("body") or page.locator("input.zone-name").evaluate_all(
            "(inputs, name) => inputs.some(i => i.value === name)", ZONE_B)
        assert page.locator("img[src='x']").count() == 0, f"{path}: a name became HTML"
        assert opened.errors == []
    assert dialogs == [], "a name ran a script"


@pytest.mark.parametrize("width", [1280, 390])
def test_one_click_on_an_empty_schedule_paints_one_cell(open_page, api, width):
    """The empty-schedule note above the grid disappears at the first cell;
    it used to go at once, the grid jumped up under the still-pressed
    pointer and a second cell - two or five rows down - got painted too."""
    _set_week(api, ZONE_A, EMPTY_WEEK)
    page = open_climate(open_page, width, ZONE_A).page
    card = f"#zone-{slug(ZONE_A)}"
    page.wait_for_selector(f"{card} .sched-cell", timeout=20000)
    page.wait_for_timeout(800)
    assert page.locator(f"#sched-empty-{slug(ZONE_A)}").is_visible()
    page.locator(f"{card} .sched-cell[data-day='mon'][data-hour='8']").click()
    assert eventually(page, lambda: _schedule(api, ZONE_A)["mon"][8] is not None)
    page.wait_for_timeout(1000)
    painted = [(d, h) for d, hours in _schedule(api, ZONE_A).items() for h, v in enumerate(hours) if v is not None]
    assert painted == [("mon", 8)]
    assert page.locator(f"#sched-empty-{slug(ZONE_A)}").is_hidden(), "the note goes once the click is over"


def test_a_zone_name_with_an_apostrophe_works_in_its_buttons(open_page, api):
    """The copy buttons carry the zone's name into their click handler - an
    apostrophe there used to end the string and break the button."""
    _set_week(api, ZONE_A, EMPTY_WEEK)
    opened = open_climate(open_page, 390, ZONE_A)
    page = opened.page
    card = f"#zone-{slug(ZONE_A)}"
    page.wait_for_selector(f"{card} .sched-cell", timeout=20000)
    page.wait_for_timeout(800)
    page.locator(f"{card} .sched-cell[data-day='mon'][data-hour='8']").click()
    page.locator(f"{card} button", has_text="Lun–Ven").click()
    assert eventually(page, lambda: _schedule(api, ZONE_A)["fri"][8] is not None)
    week = _schedule(api, ZONE_A)
    assert [week[d][8] is not None for d in ("tue", "wed", "thu", "fri", "sat")] == [True] * 4 + [False]
    assert opened.errors == []


def test_two_phones_editing_one_schedule_both_keep_their_change(open_page, api):
    """B opened the page before A's change; B's save used to send its whole,
    older week and wipe A's hour."""
    _set_week(api, ZONE_A, EMPTY_WEEK)
    phone_a = open_climate(open_page, 1280, ZONE_A).page
    phone_b = open_climate(open_page, 1280, ZONE_A).page
    grid = f"#zone-{slug(ZONE_A)} .sched-cell[data-day='mon']"
    for phone in (phone_a, phone_b):
        phone.wait_for_selector(grid, timeout=20000)
        phone.wait_for_timeout(1000)

    phone_a.locator(f"{grid}[data-hour='8']").click()
    assert eventually(phone_a, lambda: _schedule(api, ZONE_A)["mon"][8] is not None)
    phone_b.locator(f"{grid}[data-hour='20']").click()
    assert eventually(phone_b, lambda: _schedule(api, ZONE_A)["mon"][20] is not None)

    monday = _schedule(api, ZONE_A)["mon"]
    assert monday[8] is not None, "phone B's save wiped phone A's change"
    shown = phone_b.locator(f"{grid}[data-hour='8']").get_attribute("data-value")
    assert shown not in (None, ""), "phone B shows phone A's change once it has saved"
