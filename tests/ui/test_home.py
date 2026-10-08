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
    # The board's clock and its date - no time zone: it only crowded the
    # date (the admin page shows it, for the installer).
    assert eventually(page, lambda: page.locator("#server-clock-date").inner_text().strip() != "")
    assert page.locator("#server-clock-zone").count() == 0
    assert page.evaluate(UNTRANSLATED_JS) == []
    assert page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_the_top_says_what_the_plant_is_doing_in_words_a_reading_backs(open_page, api):
    """No vague "all calm": with no zone scheduled, it says cooling is off
    for that reason, and the line under it says where each zone stands."""
    for name in (ZONE_A, ZONE_B):
        _set_week(api, name, EMPTY_WEEK)
    page = _open_home(open_page).page
    assert eventually(page, lambda: page.inner_text("#hero-title") == "Raffrescamento spento: nessuna zona programmata adesso")
    detail = page.inner_text("#hero-detail")
    assert f"{ZONE_A} spenta" in detail and f"{ZONE_B} spenta" in detail


def test_names_stay_text_on_the_home_page_and_in_the_plan(open_page):
    dialogs = []
    opened = _open_home(open_page, 1280)
    page = opened.page
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _open_plan(page, ZONE_A)
    assert ZONE_B in page.inner_text("#zones")
    # The other zone's name, as the grid's "copy to" button, stays text too.
    assert _card(page, ZONE_A).locator(".zone-actions button", has_text="Bagno").count() == 1
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
    page.wait_for_selector(f".zone[data-zone={_css_string(ZONE_A)}] .manual-banner button[data-action=resume]")
    card.locator(".manual-banner button[data-action=resume]").click()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)


def test_off_is_one_of_the_manual_changes_not_a_button_of_its_own(open_page, api):
    """Seen on the production board: "Spegni" beside "Torna al programma"
    read as a switch of its own. Off is chosen inside the manual change,
    with a duration like a temperature - and back to the temperature."""
    opened = _open_home(open_page)
    page = opened.page
    card = _card(page, ZONE_A)
    assert card.locator(".quick button", has_text="Spegni").count() == 0
    card.locator("button[data-action=start][data-delta='0.5']").click()
    assert card.locator(".pending-title").inner_text() == "Programmazione manuale"
    shown = card.locator(".pending .stepper-value").inner_text()
    card.locator(".pending button[data-action=pending-off]").click()
    assert card.locator(".pending .stepper").is_hidden()
    card.locator(".pending button[data-action=pending-on]").click()
    assert card.locator(".pending .stepper-value").inner_text() == shown, "the temperature it had"
    card.locator(".pending button[data-action=pending-off]").click()
    card.locator(".pending button[data-hours='2']").click()
    card.locator("button[data-action=apply]").click()
    assert eventually(page, lambda: (_zone(api, ZONE_A)["override"] or {"target": 0})["target"] is None)
    banner = card.locator(".manual-banner")
    banner.wait_for()
    assert banner.inner_text().startswith("Programmazione manuale: spenta")
    assert f"{ZONE_A} spenta a mano" in page.inner_text("#hero-detail")
    # Every zone off, one by hand: not "none planned" - it is planned.
    other = f"/api/climate/zones/{urllib.parse.quote(ZONE_B, safe='')}/override"
    api(other, {"target": None, "hours": 2})
    page.reload()
    page.wait_for_selector(".zone .manual-banner", timeout=20000)
    assert eventually(page, lambda: page.inner_text("#hero-title") == "Raffrescamento spento adesso")
    api(other + "/cancel", {})
    page.reload()
    banner = _card(page, ZONE_A).locator(".manual-banner")
    banner.wait_for(timeout=20000)
    banner.locator("button[data-action=resume]").click()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)
    assert opened.errors == []


def test_a_zone_held_by_hand_says_so_at_the_top(open_page, api):
    """Seen on the production board: a change by hand said only by a small
    box at the bottom of the card, the controls with no word of what they
    are. Now the card's top says it, and the top of the page; and "until
    you change it" is one of its durations."""
    opened = _open_home(open_page)
    page = opened.page
    card = _card(page, ZONE_A)
    assert card.locator(".quick-title").inner_text() == "Cambia la temperatura"
    assert "Tocca − o +" in card.locator(".quick-hint").inner_text()
    card.locator("button[data-action=start][data-delta='0.5']").click()
    durations = card.locator(".pending button[data-action=duration]").all_inner_texts()
    assert durations[:4] == ["2 h", "4 h", "8 h", "24 h"] and durations[-1] == "Finché non lo cambi"
    card.locator(".pending button[data-hours='manual']").click()
    card.locator("button[data-action=apply]").click()
    assert eventually(page, lambda: (_zone(api, ZONE_A)["override"] or {}).get("until", "x") is None)
    banner = card.locator(".manual-banner")
    banner.wait_for()
    assert banner.inner_text().startswith("Programmazione manuale:") and "finché non lo cambi" in banner.inner_text()
    # The way back says what it goes back to: the week's target this hour.
    scheduled = _zone(api, ZONE_A)["scheduled_target_c"]
    back = "spenta" if scheduled is None else f"{scheduled:g}".replace(".", ",") + "°"
    assert banner.locator("button[data-action=resume]").inner_text() == f"Torna alla programmazione ({back})"
    assert card.locator(".quick-title").inner_text() == "Cambiala ancora"
    assert "manuale finché non lo cambi" in page.locator("#hero-detail").inner_text()
    # The week says its edits wait for the way back.
    assert eventually(page, lambda: "Adesso vale la programmazione manuale" in card.locator(".plan-manual").text_content())
    banner.locator("button[data-action=resume]").click()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)
    page.wait_for_timeout(600)
    assert card.locator(".manual-banner").count() == 0
    assert eventually(page, lambda: card.locator(".plan-manual").is_hidden())
    assert opened.errors == []


def test_the_week_is_locked_while_a_zone_is_held_by_hand(open_page, api):
    """Seen on the production board: held by hand, the week could still be
    painted - and "back to the schedule (24°)" kept the old hour's target.
    Now the week is shown, locked, with the way back above it; a copy
    from the other zone is refused too (and by the board)."""
    api(f"/api/climate/zones/{urllib.parse.quote(ZONE_A, safe='')}/override", {"target": 22.0, "until": "manual"})
    opened = _open_home(open_page, 1280)
    page = opened.page
    plan = _card(page, ZONE_A).locator(".zone-plan")
    _open_plan(page, ZONE_A)
    assert eventually(page, lambda: plan.locator(".plan-edit.is-locked").count() == 1)
    assert plan.locator(".plan-edit").evaluate("e => e.inert") is True
    assert "la settimana si cambia dopo" in plan.locator(".plan-manual").inner_text()
    before = _week(api, ZONE_A)
    box = plan.locator(".sched-cell").nth(30).bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(1200)
    assert _week(api, ZONE_A) == before, "a tap on the locked week changes nothing"
    _open_plan(page, ZONE_B)
    _card(page, ZONE_B).locator(".zone-plan button.range-btn", has_text=ZONE_A).click()
    assert eventually(page, lambda: "è in programmazione manuale" in page.inner_text("body"))
    assert _week(api, ZONE_A) == before
    plan.locator(".plan-manual button[data-action=resume]").click()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)
    assert eventually(page, lambda: plan.locator(".plan-edit.is-locked").count() == 0)
    assert plan.locator(".plan-manual").is_hidden()
    assert opened.errors == []


def _open_plan(page, name):
    """The week is on the card itself - behind its button on a phone:
    opens it, and waits for its grid to be filled."""
    toggle = _card(page, name).locator(".plan-toggle")
    toggle.wait_for(state="attached", timeout=20000)
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    page.wait_for_selector(f".zone[data-zone={_css_string(name)}] .sched-cell", timeout=20000)
    page.wait_for_timeout(800)


def test_one_click_on_the_plan_paints_one_cell(open_page, api):
    """The weekly grid (static/schedule_grid.js), on the zone's card: the
    empty-week note mustn't move it under the pointer."""
    _set_week(api, ZONE_A, EMPTY_WEEK)
    opened = _open_home(open_page)
    page = opened.page
    _open_plan(page, ZONE_A)
    _card(page, ZONE_A).locator(".sched-cell[data-day='mon'][data-hour='8']").click()
    assert eventually(page, lambda: _week(api, ZONE_A)["mon"][8] is not None)
    page.wait_for_timeout(1000)
    painted = [(d, h) for d, hours in _week(api, ZONE_A).items() for h, v in enumerate(hours) if v is not None]
    assert painted == [("mon", 8)]
    assert opened.errors == []


def test_the_cards_refresh_never_rebuilds_the_grid_under_a_stroke(open_page, api):
    """The live part of a card is refilled every 20 s; the week under it
    must be the very same grid before and after - a stroke keeps going."""
    _set_week(api, ZONE_A, EMPTY_WEEK)
    opened = _open_home(open_page)
    page = opened.page
    _open_plan(page, ZONE_A)
    grid = _card(page, ZONE_A).locator(".sched-grid")
    grid.evaluate("g => { g.dataset.marker = 'same'; }")
    page.evaluate("refreshClimate()")
    page.wait_for_timeout(1500)
    assert grid.evaluate("g => g.dataset.marker") == "same"
    _card(page, ZONE_A).locator(".sched-cell[data-day='tue'][data-hour='9']").click()
    assert eventually(page, lambda: _week(api, ZONE_A)["tue"][9] is not None)
    assert opened.errors == []


def test_two_phones_editing_one_week_both_keep_their_change(open_page, api):
    """B opened the page before A's change; B's save used to send its whole,
    older week and wipe A's hour."""
    _set_week(api, ZONE_A, EMPTY_WEEK)
    phone_a = _open_home(open_page).page
    phone_b = _open_home(open_page).page
    grid = f".zone[data-zone={_css_string(ZONE_A)}] .sched-cell[data-day='mon']"
    for phone in (phone_a, phone_b):
        _open_plan(phone, ZONE_A)

    phone_a.locator(f"{grid}[data-hour='8']").click()
    assert eventually(phone_a, lambda: _week(api, ZONE_A)["mon"][8] is not None)
    phone_b.locator(f"{grid}[data-hour='20']").click()
    assert eventually(phone_b, lambda: _week(api, ZONE_A)["mon"][20] is not None)

    monday = _week(api, ZONE_A)["mon"]
    assert monday[8] is not None, "phone B's save wiped phone A's change"
    shown = phone_b.locator(f"{grid}[data-hour='8']").get_attribute("data-value")
    assert shown not in (None, ""), "phone B shows phone A's change once it has saved"


def test_monday_is_copied_to_the_weekdays_from_the_plan(open_page, api):
    _set_week(api, ZONE_A, EMPTY_WEEK)
    opened = _open_home(open_page)
    page = opened.page
    _open_plan(page, ZONE_A)
    _card(page, ZONE_A).locator(".sched-cell[data-day='mon'][data-hour='8']").click()
    _card(page, ZONE_A).locator("button", has_text="Lun–Ven").click()
    assert eventually(page, lambda: _week(api, ZONE_A)["fri"][8] is not None)
    week = _week(api, ZONE_A)
    assert [week[d][8] is not None for d in ("tue", "wed", "thu", "fri", "sat")] == [True] * 4 + [False]
    assert opened.errors == []


def _value(api, name):
    return api(f"/api/sensors/{urllib.parse.quote(name, safe='')}")["value"]


def test_the_vmc_buttons_set_the_vmc_not_the_first_level_on_the_page(open_page, api):
    """On the real board the Tapo strips' levels come before the VMC's
    speed; the VMC's buttons used to set the first one they found - the
    sofa strip went to 50%, the VMC didn't move."""
    lamp_before = _value(api, "Lampada Livello")
    page = _open_home(open_page).page
    vmc = page.locator("#plant-body button[data-vmc]", has_text="Massimo")
    speed = vmc.get_attribute("data-device")
    assert speed != "Lampada Livello"
    vmc.click()
    assert eventually(page, lambda: _value(api, speed) == 100)
    assert _value(api, "Lampada Livello") == lamp_before, "the lamp's level didn't move"


def test_the_vmc_is_turned_off_from_its_card_and_a_speed_starts_it_again(open_page, api):
    page = _open_home(open_page).page
    off = page.locator("#plant-body button[data-vmc-off]")
    power = off.get_attribute("data-device")
    off.click()
    assert eventually(page, lambda: api(f"/api/sensors/{urllib.parse.quote(power, safe='')}")["is_on"] is False)
    assert eventually(page, lambda: off.get_attribute("aria-pressed") == "true")
    page.locator("#plant-body button[data-vmc]", has_text="Notte").click()
    assert eventually(page, lambda: api(f"/api/sensors/{urllib.parse.quote(power, safe='')}")["is_on"] is True)
    assert eventually(page, lambda: off.get_attribute("aria-pressed") == "false")


def test_away_is_set_from_the_hero_and_ended(open_page, api):
    page = _open_home(open_page).page
    page.click("#away-chip")
    page.locator("#sheet button[data-away='24']").click()
    assert eventually(page, lambda: api("/api/climate/zones")["away_until"] is not None)
    assert eventually(page, lambda: page.locator("#away-chip").get_attribute("aria-pressed") == "true")
    page.click("#away-chip")
    page.locator("#sheet button[data-away='back']").click()
    assert eventually(page, lambda: api("/api/climate/zones")["away_until"] is None)


def test_away_turns_everything_off_and_nothing_changes_until_back(open_page, api):
    """Seen on the production board: a zone changed by hand kept cooling
    after "away". Now away says what it does before it's chosen, turns
    off the zone changed by hand and the dehumidifier, and every card says
    it's off - no controls - until "I'm back"."""
    api(f"/api/climate/zones/{urllib.parse.quote(ZONE_A, safe='')}/override", {"target": 18.0, "until": "manual"})
    api("/api/sensors/Dehumidifier/on", {})
    opened = _open_home(open_page)
    page = opened.page
    card = _card(page, ZONE_A)
    card.locator(".manual-banner").wait_for()
    page.click("#away-chip")
    points = page.locator("#sheet .sheet-points").inner_text()
    assert "anche quelle cambiate a mano" in points and "Si spegne anche: Deumidificatore" in points
    assert "Sono rientrato" in points
    page.locator("#sheet button[data-away='24']").click()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)
    assert api("/api/sensors/Dehumidifier")["is_on"] is False
    assert eventually(page, lambda: card.locator(".away-note").count() == 1)
    assert card.locator(".stepper, .manual-banner, button[data-action=off]").count() == 0
    assert card.locator(".zone-plan").is_hidden(), "the week neither"
    assert "Spento anche: Deumidificatore" in page.inner_text("#hero-detail")
    device = page.locator("#season-devices .season-device", has_text="Deumidificatore")
    assert "sei fuori casa" in device.inner_text() and device.locator("button[data-switch]").count() == 0
    card.locator(".away-note button[data-action=back]").click()
    assert eventually(page, lambda: api("/api/climate/zones")["away_until"] is None)
    assert eventually(page, lambda: card.locator(".stepper").count() == 1)
    assert card.locator(".manual-banner").count() == 0, "back to the schedule, not the old change"
    assert card.locator(".zone-plan").is_visible()
    assert opened.errors == []


def test_away_until_a_day_picked_in_the_calendar(open_page, api):
    from datetime import date, timedelta
    page = _open_home(open_page).page
    page.click("#away-chip")
    day = (date.today() + timedelta(days=3)).isoformat()
    page.fill("#away-day", day)
    page.fill("#away-time", "07:30")
    page.locator("#sheet button[data-away='day']").click()
    assert eventually(page, lambda: api("/api/climate/zones")["away_until"] == f"{day}T07:30:00")
    assert eventually(page, lambda: page.locator("#away-chip").get_attribute("aria-pressed") == "true")
    api("/api/climate/away/cancel", {})


def test_the_home_page_says_when_the_board_stops_answering(open_page):
    page = _open_home(open_page).page
    assert page.locator("#offline").is_hidden()
    page.route("**/api/**", lambda route: route.abort())
    assert eventually(page, page.locator("#offline").is_visible, timeout_ms=60000)
    page.unroute("**/api/**")
    assert eventually(page, page.locator("#offline").is_hidden, timeout_ms=60000)


def _vmc(api):
    return api("/api/vmc-schedule")


def _reset_vmc(api):
    week = {d: [25] * 7 + [50] * 15 + [25] * 2 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    api("/api/vmc-schedule", {"enabled": False, "week": week})
    if "override" in _vmc(api)["status"]:
        api("/api/vmc-schedule/boost/cancel", {})


def test_a_vmc_program_is_picked_and_manual_turns_it_off(open_page, api):
    """One choice: a ready-made program switches it on, on this VMC, with
    its week; "manual" switches it off and leaves the speed alone."""
    _reset_vmc(api)
    page = _open_home(open_page).page
    assert page.get_attribute("#vmc-card [data-vmc-choice='manual']", "aria-checked") == "true"
    work = page.locator("#vmc-card [data-vmc-choice='work']")
    speed = work.get_attribute("data-device")
    work.click()
    assert eventually(page, lambda: _vmc(api)["schedule"]["enabled"] is True)
    schedule = _vmc(api)["schedule"]
    assert schedule["target"] == speed
    assert schedule["week"]["mon"][10] == 25 and schedule["week"]["sat"][10] == 50, "night 8-17 on weekdays only"
    assert eventually(page, lambda: page.get_attribute("#vmc-card [data-vmc-choice='work']", "aria-checked") == "true")
    page.click("#vmc-card [data-vmc-choice='manual']")
    assert eventually(page, lambda: _vmc(api)["schedule"]["enabled"] is False)
    assert _vmc(api)["schedule"]["week"]["mon"][10] == 25, "the week stays saved"


def test_a_week_saved_otherwise_shows_as_custom(open_page, api):
    _reset_vmc(api)
    page = _open_home(open_page).page
    speed = page.get_attribute("#vmc-card [data-vmc-choice='work']", "data-device")
    # Day 9-23: none of the ready-made weeks.
    week = {d: [25] * 9 + [50] * 14 + [25] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    api("/api/vmc-schedule", {"enabled": True, "target": speed, "week": week})
    page.evaluate("refreshDevices()")
    assert eventually(page, lambda: page.get_attribute("#vmc-card [data-vmc-choice='custom']", "aria-checked") == "true")
    _reset_vmc(api)


def test_the_vmc_runs_at_its_maximum_for_a_while_and_back(open_page, api):
    _reset_vmc(api)
    page = _open_home(open_page).page
    boost = page.locator("#vmc-card button[data-vmc-boost='30']")
    speed = boost.get_attribute("data-device")
    boost.click()
    assert eventually(page, lambda: "override" in _vmc(api)["status"])
    assert eventually(page, lambda: _value(api, speed) == 100)
    page.click("#vmc-card button[data-vmc-boost-cancel]")
    assert eventually(page, lambda: "override" not in _vmc(api)["status"])
    _reset_vmc(api)


def test_an_air_quality_sensor_shows_its_co2_day_and_levels(open_page):
    """The test server has no Tuya air sensor: its reading - tonight's,
    from the real one - is handed to the page as refreshDevices() would."""
    opened = _open_home(open_page)
    page = opened.page
    page.evaluate("""() => {
      const now = Date.now() / 1000;
      state.devices.sensors.push({ name: "Aria", kind: "climate", tab: "home", controllable: false });
      state.readings["Aria"] = { ok: true, co2_ppm: 1514, pm25_ugm3: 12, pm10_ugm3: 20, voc_mgm3: 0.076,
                                 ch2o_mgm3: 0.006, temperature_c: 21.6, humidity_pct: 60, battery_pct: 40 };
      state.airHistory["Aria"] = [{ ts: now - 7200, value: 420 }, { ts: now - 3600, value: 1514 }, { ts: now - 60, value: 900 }];
      renderHouse();
    }""")
    tile = page.locator(".tile.air")
    assert "CO₂ molto alto" in tile.locator(".state").inner_text()
    assert tile.locator(".air-co2").get_attribute("data-level") == "bad"
    assert "picco 1514 ppm" in tile.inner_text()
    assert [f.get_attribute("data-level") for f in tile.locator(".air-fact").all()] == ["ok", "ok", "ok", "ok"]
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_a_power_meter_shows_its_peak_and_its_week_month_and_year(open_page):
    """The test server has no power meter: its summary (/api/energy) and
    reading are handed to the page as refreshEnergy()/refreshDevices() would."""
    opened = _open_home(open_page)
    page = opened.page
    page.evaluate("""() => {
      const day = (d, kwh, source) => ({ day: d, kwh, source, peak_w: 3300, peak_ts: Date.now() / 1000 - 3600 });
      const week = [day("2026-09-28", 5.1, "power"), day("2026-09-29", 4.9, "power"), null, day("2026-10-01", 5.7, "meter"),
                    day("2026-10-02", 6.4, "meter"), null, null];
      state.devices.sensors.push({ name: "Consumo", kind: "power_meter", tab: "home", controllable: false,
                                   power_budget: { contract_limit_w: 3000, available_power_w: 3300, trip_risk_w: 4000 } });
      state.readings["Consumo"] = { ok: true, power_w: 157, voltage_v: 227, current_a: 1.07, day_kwh: 6.44, yesterday_kwh: 5.73 };
      state.energy["Consumo"] = {
        ok: true, today: week[4], yesterday: week[3], since: "2026-09-22",
        week: { from: "2026-09-28", kwh: 22.1, estimated: true, days: week },
        month: { from: "2026-10-01", kwh: 12.1, estimated: false, days: [week[3], week[4], ...Array(29).fill(null)] },
        year: { from: "2026-01-01", kwh: 66.4, estimated: true,
                months: Array.from({ length: 12 }, (_, i) => ({ month: `2026-${String(i + 1).padStart(2, "0")}`,
                  kwh: i === 8 ? 54.3 : i === 9 ? 12.1 : null, estimated: i === 8 })) },
      };
      renderHouse();
    }""")
    tile = page.locator(".tile.energy")
    assert "Picco oggi 3,3 kW" in tile.inner_text() and "oggi 6,4 kWh" in tile.inner_text()
    assert tile.locator(".energy-total").inner_text().startswith("Questa settimana 22,1 kWh")
    assert "stimati" in tile.inner_text()
    tile.locator("[data-energy-range=year]").click()
    assert page.locator(".tile.energy .energy-total").inner_text().startswith("Da inizio anno 66,4 kWh")
    page.locator(".tile.energy [data-energy-range=week]").click()
    page.locator(".tile.energy .energy-bar:not([disabled])").first.click()
    assert "lunedì 28 settembre: 5,1 kWh" in page.locator(".tile.energy .energy-total").inner_text()
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_a_plug_that_measures_its_power_shows_its_days_with_its_switch(open_page):
    opened = _open_home(open_page)
    page = opened.page
    page.evaluate("""() => {
      const days = Array(7).fill(null);
      days[0] = { day: "2026-09-28", kwh: 1.2, source: "power", peak_w: 160, peak_ts: Date.now() / 1000 - 7200 };
      state.devices.sensors.push({ name: "Scrivania", kind: "switch", tab: "home", controllable: true });
      state.readings["Scrivania"] = { ok: true, is_on: true, power_w: 94.4, voltage_v: 225.6, current_a: 0.56 };
      state.energy["Scrivania"] = { ok: true, today: days[0], yesterday: null, since: "2026-09-22",
        week: { from: "2026-09-28", kwh: 1.2, estimated: true, days },
        month: { from: "2026-10-01", kwh: 0, estimated: false, days: Array(31).fill(null) },
        year: { from: "2026-01-01", kwh: 1.2, estimated: true,
                months: Array.from({ length: 12 }, (_, i) => ({ month: `2026-${String(i + 1).padStart(2, "0")}`, kwh: null, estimated: false })) } };
      renderHouse();
    }""")
    tile = page.locator(".tile.energy[data-sensor='Scrivania']")
    assert tile.locator("button[data-switch='Scrivania']").count() == 1
    assert "94W" in tile.locator(".energy-now").inner_text().replace(" ", "").replace("\n", "")
    assert tile.locator(".energy-total").inner_text().startswith("Questa settimana 1,2 kWh")
    tile.locator("[data-energy-range=month]").click()
    assert page.locator(".tile.energy[data-sensor='Scrivania'] .energy-total").inner_text().startswith("Questo mese")
    assert opened.errors == []


# What a zone's card says, against the thermostat's rule (services/
# thermostat.py's decide(), the same in both seasons mirrored): it stops at
# the target and restarts half a degree away - an economy target a degree.
# The page's words, from the readings - faked here, to set each case exactly.
STATUS_CASES = [
    # mode, room, target, valve open, valve answering -> the card's status, the rule line (or None)
    ("cool", 23.8, 24.0, False, True, "Temperatura giusta", "Riparte a 24,5°"),
    ("cool", 24.3, 24.0, False, True, "In pausa", "Riparte a 24,5°"),   # above it, within the band: not "just right"
    ("cool", 24.3, 24.0, True, True, "Sta raffrescando", "Raffresca fino a 24,0°"),
    ("cool", 22.0, 24.0, False, True, "In pausa", "Riparte a 24,5°"),
    ("cool", 26.8, 26.5, False, True, "In pausa", "Riparte a 27,5°"),   # economy: a degree above
    ("heat", 20.3, 20.0, False, True, "Temperatura giusta", "Riparte a 19,5°"),
    ("heat", 19.8, 20.0, False, True, "In pausa", "Riparte a 19,5°"),  # below it, within the band
    ("heat", 19.8, 20.0, True, True, "Sta scaldando", "Scalda fino a 20,0°"),
    ("heat", 19.2, 19.5, False, True, "In pausa", "Riparte a 18,5°"),  # economy: a degree below
    ("cool", 27.5, 25.9, False, False, "La valvola non risponde", None),  # seen: a Shelly unplugged, "resting"
]


def _limits(mode, target, hysteresis=0.5, economy=0.5):
    """The thermostat's thresholds for this target, as the API says them."""
    from localhome.services.thermostat import target_tier
    spread = hysteresis + (economy if target_tier(target, mode) == "economy" else 0)
    return {"tier": target_tier(target, mode), "valve_stops_at": target,
            "valve_opens_at": round(target - spread if mode == "heat" else target + spread, 2)}


def _fake_zones(page, api, mode, zones):
    """/api/climate/zones as the server says it, with these zones' readings
    replaced - {name: (room, target, valve open, valve answering)}."""
    import json as _json
    data = api("/api/climate/zones")
    data["mode"] = mode
    data["away_until"] = None
    for zone in data["zones"]:
        if zone["name"] in zones:
            room, target, valve_on, valve_ok = zones[zone["name"]]
            zone.update(_limits(mode, target))
            zone.update(temperature_c=room, target_c=target, valve_on=valve_on, valve_ok=valve_ok,
                        valve_error=None if valve_ok else "marked offline on its availability topic",
                        sensor_ok=True, sensor_failsafe_active=False, sensor_waiting=False,
                        override=None, valve_driver=None, cool_enabled=True)
    page.unroute("**/api/climate/zones")
    page.route("**/api/climate/zones", lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(data)))
    page.reload()
    page.wait_for_selector(".zone", timeout=20000)
    page.wait_for_timeout(600)


@pytest.mark.parametrize("mode,room,target,valve_on,valve_ok,status,rule", STATUS_CASES)
def test_a_zone_says_where_it_stands_in_both_seasons(open_page, api, mode, room, target, valve_on, valve_ok, status, rule):
    """Seen on the production board, cooling: a target below the room's,
    and the card said "just right" - within the band the valve waits for,
    but above the target - or "resting" with its Shelly unplugged."""
    opened = _open_home(open_page)
    page = opened.page
    _fake_zones(page, api, mode, {ZONE_A: (room, target, valve_on, valve_ok), ZONE_B: (target, target, False, True)})
    live = _card(page, ZONE_A).locator(".zone-live").inner_text()
    assert status in _card(page, ZONE_A).locator(".zone-head").inner_text()
    if rule:
        assert rule in live
    else:
        assert "Riparte" not in live and "fino a" not in live, "no rule a valve that doesn't answer would follow"
    assert opened.errors == []


def test_the_card_names_the_targets_level_and_the_palette_has_half_degrees(open_page, api):
    """Comfort or economy, on the card next to the target, as the palette
    calls it - and under the palette, what each one does."""
    opened = _open_home(open_page)
    page = opened.page
    _fake_zones(page, api, "heat", {ZONE_A: (19.2, 19.5, False, True), ZONE_B: (20.6, 20.5, False, True)})
    assert "Economy" in _card(page, ZONE_A).locator(".tier-chip").inner_text()
    assert "Comfort" in _card(page, ZONE_B).locator(".tier-chip").inner_text()
    _open_plan(page, ZONE_A)
    swatches = _card(page, ZONE_A).locator(".sched-swatch").all_inner_texts()
    assert "19,5" in swatches and "20,5" in swatches, swatches
    note = _card(page, ZONE_A).locator(".sched-tier-note").inner_text()
    assert "Comfort" in note and "Economy" in note and "0,5" in note and "1,0" in note, note
    assert opened.errors == []


def test_on_a_phone_the_week_waits_behind_its_button(open_page):
    """Two screens of hours between a zone and everything after it: on a
    phone the week is closed, its button says which; wider, it's open
    beside the card and there's no button."""
    phone = _open_home(open_page).page
    toggle = _card(phone, ZONE_A).locator(".plan-toggle")
    toggle.wait_for(timeout=20000)
    assert toggle.is_visible() and "Programmazione" in toggle.text_content()
    phone.wait_for_selector(f".zone[data-zone={_css_string(ZONE_A)}] .sched-cell", state="attached", timeout=20000)
    assert not _card(phone, ZONE_A).locator(".sched-grid").is_visible()
    toggle.click()
    assert _card(phone, ZONE_A).locator(".sched-grid").is_visible() and toggle.get_attribute("aria-expanded") == "true"
    phone.evaluate("refreshClimate()")
    phone.wait_for_timeout(800)
    assert _card(phone, ZONE_A).locator(".sched-grid").is_visible(), "a refresh doesn't close it"
    desktop = _open_home(open_page, 1280).page
    desktop.wait_for_selector(f".zone[data-zone={_css_string(ZONE_A)}] .sched-cell", timeout=20000)
    assert not _card(desktop, ZONE_A).locator(".plan-toggle").is_visible()


def test_a_zone_out_of_the_season_has_no_empty_strip(open_page, api):
    opened = _open_home(open_page)
    page = opened.page
    _fake_zones(page, api, "cool", {ZONE_A: (24.0, 24.0, False, True), ZONE_B: (24.0, 24.0, False, True)})
    assert _card(page, ZONE_B).locator(".ribbon > svg").count() == 1, "in the season it has its strip"
    page.evaluate("name => { state.climate.zones.find(z => z.name === name).cool_enabled = false; renderZones(); }", ZONE_B)
    page.wait_for_timeout(300)
    assert _card(page, ZONE_A).locator(".ribbon > svg").count() == 1
    excluded = _card(page, ZONE_B)
    assert excluded.get_attribute("data-plan") == "none" and excluded.locator(".ribbon").count() == 0
    assert excluded.locator(".excluded-note").is_visible()
    assert opened.errors == []


def test_a_card_takes_its_states_colour(open_page, api):
    """Running in the season's colour, at its target green, a problem
    amber; the top of the page brighter while a zone runs."""
    opened = _open_home(open_page, 1280)
    page = opened.page
    line = lambda name: _card(page, name).evaluate("el => getComputedStyle(el, '::before').backgroundColor")
    _fake_zones(page, api, "heat", {ZONE_A: (19.0, 21.0, True, True), ZONE_B: (21.0, 21.0, False, True)})
    assert [_card(page, z).get_attribute("data-tone") for z in (ZONE_A, ZONE_B)] == ["active", "ok"]
    assert page.evaluate("document.body.dataset.running") == "true"
    assert line(ZONE_A) != line(ZONE_B)
    _fake_zones(page, api, "heat", {ZONE_A: (19.0, 21.0, False, False), ZONE_B: (20.8, 21.0, False, True)})
    assert [_card(page, z).get_attribute("data-tone") for z in (ZONE_A, ZONE_B)] == ["warn", "rest"]
    assert page.evaluate("document.body.dataset.running") == "false"
    assert line(ZONE_B) in ("rgba(0, 0, 0, 0)", "none", ""), "resting inside its band: plain"
    assert opened.errors == []


def test_a_sensor_says_in_plain_words_whether_it_runs_on_battery_or_is_powered(open_page, api):
    """For whoever lives there: "on battery 80%" or "powered" - not "USB-C" -
    and on the card itself, when its batteries want changing soon, or now.
    Either is as good: going from one to the other is no warning."""
    opened = _open_home(open_page)
    page = opened.page
    _fake_zones(page, api, "heat", {ZONE_A: (20.0, 21.0, False, True)})
    set_zone = "([name, values]) => { Object.assign(state.climate.zones.find(z => z.name === name), values); renderZones(); }"
    card = _card(page, ZONE_A)
    page.evaluate(set_zone, [ZONE_A, {"battery_pct": 80, "external_power": False, "sensor_seconds_since_update": 120}])
    assert "a batteria 80%" in card.locator(".zone-foot").inner_text() and card.locator(".sensor-note").count() == 0
    page.evaluate(set_zone, [ZONE_A, {"battery_pct": None, "external_power": True}])
    foot = card.locator(".zone-foot").inner_text()
    assert "alimentato a corrente" in foot and "batteria" not in foot and "USB" not in foot
    page.evaluate(set_zone, [ZONE_A, {"battery_pct": 15, "external_power": False}])
    assert card.locator(".sensor-note").inner_text() == "Batterie del sensore al 15%: cambiale a breve."
    page.evaluate(set_zone, [ZONE_A, {"battery_pct": 8}])
    note = card.locator(".sensor-note")
    assert note.get_attribute("data-level") == "now" and "al più presto" in note.inner_text()
    page.evaluate(set_zone, [ZONE_A, {"battery_pct": 86}])
    assert card.locator(".sensor-note").count() == 0, "from USB-C to its batteries: only said under the card"
    assert opened.errors == []


def test_the_top_says_every_zone_reached_its_target_only_when_it_has(open_page, api):
    opened = _open_home(open_page)
    page = opened.page
    _fake_zones(page, api, "cool", {ZONE_A: (24.3, 24.0, False, True), ZONE_B: (23.9, 24.0, False, True)})
    assert page.inner_text("#hero-title") != "Tutte le zone hanno raggiunto l'obiettivo"
    detail = page.inner_text("#hero-detail")
    assert f"{ZONE_A} 24,3°, più caldo dell'obiettivo di 24°" in detail and f"{ZONE_B} 23,9°, in temperatura" in detail
    _fake_zones(page, api, "cool", {ZONE_A: (23.7, 24.0, False, True), ZONE_B: (23.9, 24.0, False, True)})
    assert page.inner_text("#hero-title") == "Tutte le zone hanno raggiunto l'obiettivo"
    _fake_zones(page, api, "cool", {ZONE_A: (23.7, 24.0, False, False), ZONE_B: (23.9, 24.0, False, True)})
    assert page.inner_text("#hero-title") != "Tutte le zone hanno raggiunto l'obiettivo"
    assert f"{ZONE_A}: la valvola non risponde" in page.inner_text("#hero-detail")
    assert opened.errors == []


# ------------------------------------------------- the week on a phone ----
# A cell of the week was 9 px wide on a phone - too small for a finger. On
# a phone (or a touch screen up to a tablet's) the grid turns: hours down,
# days across; a tap paints an hour, a sideways drag the same hour across
# days, an up-down swipe scrolls. A mouse keeps the hours across.

@pytest.fixture
def phone(browser, server):
    from .helpers import Opened
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    opened = Opened(context.new_page())
    opened.page.goto(server + "/")
    opened.page.wait_for_selector(".zone", timeout=20000)
    yield opened, context
    context.close()


def _center(locator):
    box = locator.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _cell(page, name, day, hour):
    return _card(page, name).locator(f".sched-cell[data-day='{day}'][data-hour='{hour}']")


def _touch_stroke(context, page, start, end, steps=8):
    """A finger from start to end, as touch events - pointerType "touch"."""
    cdp = context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": start[0], "y": start[1]}]})
    for i in range(1, steps + 1):
        x = start[0] + (end[0] - start[0]) * i / steps
        y = start[1] + (end[1] - start[1]) * i / steps
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def test_on_a_phone_the_week_turns_and_its_hours_are_finger_sized(phone, api):
    opened, _ = phone
    page = opened.page
    _open_plan(page, ZONE_A)
    first, next_hour, next_day = (_cell(page, ZONE_A, "mon", 0), _cell(page, ZONE_A, "mon", 1), _cell(page, ZONE_A, "tue", 0))
    box = first.bounding_box()
    assert box["width"] >= 30 and box["height"] >= 28, box
    assert next_hour.bounding_box()["y"] > box["y"], "hours down"
    assert next_day.bounding_box()["x"] > box["x"], "days across"
    assert _card(page, ZONE_A).locator(".sched-hour-label").first.is_visible()
    assert page.evaluate(OVERFLOW_JS) <= 0 and opened.errors == []


def test_on_a_phone_a_tap_paints_an_hour_a_sideways_drag_the_same_hour_and_a_swipe_nothing(phone, api):
    opened, context = phone
    page = opened.page
    _set_week(api, ZONE_A, EMPTY_WEEK)
    page.reload()
    _open_plan(page, ZONE_A)
    _cell(page, ZONE_A, "thu", 7).scroll_into_view_if_needed()
    page.touchscreen.tap(*_center(_cell(page, ZONE_A, "thu", 7)))
    assert eventually(page, lambda: _week(api, ZONE_A)["thu"][7] is not None)
    _cell(page, ZONE_A, "mon", 19).scroll_into_view_if_needed()
    _touch_stroke(context, page, _center(_cell(page, ZONE_A, "mon", 19)), _center(_cell(page, ZONE_A, "wed", 19)))
    assert eventually(page, lambda: [_week(api, ZONE_A)[d][19] is not None for d in ("mon", "tue", "wed", "thu")]
                      == [True, True, True, False])
    _cell(page, ZONE_A, "sat", 2).scroll_into_view_if_needed()
    _touch_stroke(context, page, _center(_cell(page, ZONE_A, "sat", 2)), _center(_cell(page, ZONE_A, "sat", 6)))
    page.wait_for_timeout(1500)
    assert all(v is None for v in _week(api, ZONE_A)["sat"]), "a swipe up or down scrolls, it doesn't paint"
    assert opened.errors == []


def test_on_a_desktop_the_hours_run_across_and_a_mouse_drag_paints_a_range(open_page, api):
    _set_week(api, ZONE_A, EMPTY_WEEK)
    opened = _open_home(open_page, width=1280)
    page = opened.page
    _open_plan(page, ZONE_A)
    first = _cell(page, ZONE_A, "mon", 0)
    assert _cell(page, ZONE_A, "mon", 1).bounding_box()["x"] > first.bounding_box()["x"], "hours across"
    assert not _card(page, ZONE_A).locator(".sched-hour-label").first.is_visible()
    start, end = _center(_cell(page, ZONE_A, "tue", 8)), _center(_cell(page, ZONE_A, "tue", 11))
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=10)
    page.mouse.up()
    assert eventually(page, lambda: [_week(api, ZONE_A)["tue"][h] is not None for h in range(7, 13)]
                      == [False, True, True, True, True, False])
    assert opened.errors == []


def test_a_second_tap_clears_an_hour_and_a_stroke_does_what_its_first_cell_decides(phone, api):
    """No trip to the palette's OFF: a tap on an hour that already has the
    chosen temperature clears it. A stroke started on such an hour clears
    every hour it crosses; started elsewhere it paints them all."""
    opened, context = phone
    page = opened.page
    _set_week(api, ZONE_A, EMPTY_WEEK)
    page.reload()
    _open_plan(page, ZONE_A)
    cell = _cell(page, ZONE_A, "fri", 9)
    cell.scroll_into_view_if_needed()
    page.touchscreen.tap(*_center(cell))
    assert eventually(page, lambda: _week(api, ZONE_A)["fri"][9] is not None)
    page.wait_for_timeout(300)
    page.touchscreen.tap(*_center(cell))
    assert eventually(page, lambda: _week(api, ZONE_A)["fri"][9] is None), "a second tap clears it"
    row = [_cell(page, ZONE_A, d, 21) for d in ("mon", "tue", "wed")]
    row[0].scroll_into_view_if_needed()
    _touch_stroke(context, page, _center(row[0]), _center(row[2]))
    assert eventually(page, lambda: [_week(api, ZONE_A)[d][21] is not None for d in ("mon", "tue", "wed")] == [True] * 3)
    page.wait_for_timeout(300)
    _touch_stroke(context, page, _center(row[0]), _center(row[2]))
    assert eventually(page, lambda: [_week(api, ZONE_A)[d][21] for d in ("mon", "tue", "wed")] == [None] * 3), \
        "started on a painted hour, the stroke clears"
    assert opened.errors == []



# ------------------------------------------ updated in place, held still ----
# A refresh, or a tap, used to rebuild the cards: everything blinked, and
# on an iPhone - Safari has no scroll anchoring - the page slid under the
# finger. Now what changed is changed in place (static/morph.js), and the
# page is held where it was.

# Elements not seen before: marked as they're counted.
FRESH_JS = """() => { let fresh = 0;
  document.querySelectorAll('.hero *, #zones .zone-live *, #plant-body *, #house-body *').forEach(e => {
    if (!e.__seen) { e.__seen = true; fresh++; } });
  return fresh; }"""


@pytest.fixture
def iphone(browser, server):
    """A phone that doesn't hold the page itself, as Safari on an iPhone."""
    from .helpers import Opened
    context = browser.new_context(viewport={"width": 390, "height": 760}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.add_init_script("document.addEventListener('DOMContentLoaded', () => { const s = document.createElement('style');"
                         " s.textContent = '* { overflow-anchor: none !important; }'; document.head.appendChild(s); });")
    opened = Opened(page)
    page.goto(server + "/")
    page.wait_for_selector(".zone .quick button", timeout=20000)
    page.wait_for_timeout(1500)
    yield opened
    context.close()


def test_the_page_opens_at_the_top(iphone):
    """Holding still is for what's touched or read: the page filling in
    (the week loading, its empty note) once opened it half-way down."""
    page = iphone.page
    page.wait_for_timeout(1500)
    assert page.evaluate("scrollY") == 0
    page.reload()
    page.wait_for_selector(".zone .sched-cell", state="attached", timeout=20000)
    page.wait_for_timeout(2500)
    assert page.evaluate("scrollY") == 0


def test_a_refresh_rebuilds_nothing(iphone):
    page = iphone.page
    page.evaluate(FRESH_JS)
    page.evaluate("Promise.all([refreshClimate(), refreshDevices(), refreshHistory()])")
    page.wait_for_timeout(500)
    # A row may come or go with what it says (a reading's trend): not the cards.
    assert page.evaluate(FRESH_JS) <= 3
    assert iphone.errors == []


def test_a_tap_keeps_the_card_under_the_finger_and_its_nodes(iphone, api):
    page = iphone.page
    card = _card(page, ZONE_A)
    quick = card.locator(".quick")
    quick.scroll_into_view_if_needed()
    page.evaluate("window.scrollBy(0, -250)")        # its controls mid-screen, the top above
    card.locator(".zone-head").evaluate("e => { e.__kept = true; }")
    before = quick.bounding_box()["y"]
    card.locator("button[data-action=start][data-delta='0.5']").tap()
    page.wait_for_selector(f".zone[data-zone={_css_string(ZONE_A)}] .pending")
    assert abs(quick.bounding_box()["y"] - before) < 1
    apply = card.locator("button[data-action=apply]")
    apply.scroll_into_view_if_needed()
    before = quick.bounding_box()["y"]
    apply.tap()
    page.wait_for_selector(f".zone[data-zone={_css_string(ZONE_A)}] .manual-banner button[data-action=resume]")
    page.wait_for_timeout(800)                         # and the valve's answer after it
    assert abs(quick.bounding_box()["y"] - before) < 1
    assert card.locator(".zone-head").evaluate("e => e.__kept === true"), "updated, not rebuilt"
    card.locator(".manual-banner button[data-action=resume]").tap()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)
    assert iphone.errors == []


def test_a_tap_on_the_day_strip_stays_to_be_read(iphone):
    """On a phone the finger leaves the strip as soon as it's lifted: what
    it touched used to vanish with it - a tap showed nothing."""
    page = iphone.page
    svg = _card(page, ZONE_A).locator(".ribbon > svg")
    svg.scroll_into_view_if_needed()
    box = svg.bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] * 0.3, box["y"] + box["height"] / 2)
    page.wait_for_timeout(1000)
    readout = _card(page, ZONE_A).locator(".ribbon-readout").inner_text()
    assert readout.startswith("07:"), readout
    page.wait_for_timeout(4000)
    assert "temperatura" in _card(page, ZONE_A).locator(".ribbon-readout").inner_text().lower(), "the legend again"


def test_what_grows_above_doesnt_push_what_is_being_read(iphone):
    page = iphone.page
    card = _card(page, ZONE_B)
    card.locator(".zone-foot").scroll_into_view_if_needed()
    before = card.bounding_box()["y"]
    page.evaluate("steady(() => { document.getElementById('hero-detail').textContent += ' una riga in più'.repeat(20); })")
    assert abs(card.bounding_box()["y"] - before) < 1
    assert iphone.errors == []


# ------------------------------------------------ when the board misbehaves --

def test_an_action_the_board_never_answers_frees_its_button_and_says_so(iphone):
    """Without a time limit the button waited for ever - and a refresh
    meanwhile enabled it again, so the same action could go twice."""
    page = iphone.page
    page.route("**/override", lambda route: None)          # never answered
    card = _card(page, ZONE_A)
    card.locator("button[data-action=start][data-delta='0.5']").tap()
    apply = card.locator("button[data-action=apply]")
    apply.tap()
    page.evaluate("refreshClimate()")
    page.wait_for_timeout(1500)
    assert apply.get_attribute("aria-busy") == "true" and apply.is_disabled(), "still waiting after a refresh"
    page.wait_for_timeout(15000)
    assert apply.get_attribute("aria-busy") is None and apply.is_enabled()
    assert "non ha risposto" in page.locator(".toast-host").inner_text()
    page.unroute("**/override")
    card.locator("button[data-action=cancel-pending]").tap()


def test_a_week_that_didnt_load_comes_by_itself_once_the_board_answers(browser, server):
    from .helpers import Opened
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    opened = Opened(page)
    failing = {"on": True}
    page.route("**/schedule?mode=*", lambda route: route.abort() if failing["on"] else route.continue_())
    page.goto(server + "/")
    page.wait_for_selector(".zone .quick button", timeout=20000)
    page.wait_for_timeout(1500)
    assert _card(page, ZONE_A).locator(".sched-cell").count() == 0
    failing["on"] = False
    page.evaluate("refreshClimate()")
    assert eventually(page, lambda: _card(page, ZONE_A).locator(".sched-cell").count() == 168)
    assert [e for e in opened.errors if "net::" not in e] == []
    context.close()
