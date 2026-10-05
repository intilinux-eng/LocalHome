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
    # The board's clock, with its date and time zone.
    assert eventually(page, lambda: page.locator("#server-clock-date").inner_text().strip() != "")
    assert "UTC" in page.locator("#server-clock-zone").inner_text()
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
    page.wait_for_selector(f".zone[data-zone={_css_string(ZONE_A)}] button[data-action=resume]")
    card.locator("button[data-action=resume]").click()
    assert eventually(page, lambda: _zone(api, ZONE_A)["override"] is None)


def _open_plan(page, name):
    """The week is on the card itself - waits for its grid to be filled."""
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
        phone.wait_for_selector(grid, timeout=20000)
        phone.wait_for_timeout(1000)

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
# thermostat.py's decide(), the same in both seasons mirrored): the page's
# words, from the readings - faked here, to set each case exactly.
STATUS_CASES = [
    # mode, room, target, valve open, valve answering -> the card's status, the rule line (or None)
    ("cool", 23.8, 24.0, False, True, "Temperatura giusta", "Riparte sopra 24,5°"),
    ("cool", 24.3, 24.0, False, True, "In pausa", "Riparte sopra 24,5°"),   # above it, within the band: not "just right"
    ("cool", 24.3, 24.0, True, True, "Sta raffrescando", "Raffresca fino a 23,5°"),
    ("cool", 22.0, 24.0, False, True, "In pausa", "Riparte sopra 24,5°"),
    ("heat", 20.3, 20.0, False, True, "Temperatura giusta", "Riparte sotto 19,5°"),
    ("heat", 19.8, 20.0, False, True, "In pausa", "Riparte sotto 19,5°"),  # below it, within the band
    ("heat", 19.8, 20.0, True, True, "Sta scaldando", "Scalda fino a 20,5°"),
    ("cool", 27.5, 25.9, False, False, "La valvola non risponde", None),  # seen: a Shelly unplugged, "resting"
]


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


def test_the_top_says_every_zone_reached_its_target_only_when_it_has(open_page, api):
    opened = _open_home(open_page)
    page = opened.page
    _fake_zones(page, api, "cool", {ZONE_A: (24.3, 24.0, False, True), ZONE_B: (23.9, 24.0, False, True)})
    assert page.inner_text("#hero-title") != "Tutte le zone hanno raggiunto l'obiettivo"
    detail = page.inner_text("#hero-detail")
    assert f"{ZONE_A} 24,3°, sopra l'obiettivo di 24°" in detail and f"{ZONE_B} 23,9°, in temperatura" in detail
    _fake_zones(page, api, "cool", {ZONE_A: (23.7, 24.0, False, True), ZONE_B: (23.9, 24.0, False, True)})
    assert page.inner_text("#hero-title") == "Tutte le zone hanno raggiunto l'obiettivo"
    _fake_zones(page, api, "cool", {ZONE_A: (23.7, 24.0, False, False), ZONE_B: (23.9, 24.0, False, True)})
    assert page.inner_text("#hero-title") != "Tutte le zone hanno raggiunto l'obiettivo"
    assert f"{ZONE_A}: la valvola non risponde" in page.inner_text("#hero-detail")
    assert opened.errors == []
