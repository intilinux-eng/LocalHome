"""The admin page in a browser: the overview and the section headers fill
in, the bar follows the scroll, page-wide notices come and go, refusals
arrive in the page's language. See conftest.py for the server."""
import pytest

from .helpers import OVERFLOW_JS, UNTRANSLATED_JS, ZONE_A, ZONE_B, eventually

pytestmark = pytest.mark.ui

SECTION_PILLS = ("season-summary", "zones-summary", "devices-summary", "broker-status",
                 "system-status-summary", "diagnostic-summary")


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
    # The LED matrix's card is in the system status: its tile opens that.
    section = page.locator("#system-status")
    assert not section.evaluate("d => d.open")
    page.locator("#ov-led").click()
    assert section.evaluate("d => d.open")
    assert page.locator("#led-matrix-mirror").is_visible()
    assert page.locator("#led-matrix-legend").is_hidden(), "what each picture means stays folded"
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
    page.locator("#devices > summary").click()
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
    card.locator(".zone-name").fill("Stanze/Bagno")
    page.locator("#save-btn").click()
    result = page.locator("#save-result")
    assert eventually(page, lambda: result.inner_text().strip() != "")
    text = result.inner_text()
    assert "il nome non può contenere" in text and "Stanze/Bagno" in text, text
    assert api("/api/climate-admin/map")["zones"][0]["name"] == ZONE_A, "nothing saved"


def test_the_admin_page_says_when_the_board_stops_answering(open_page):
    """Its own checks (every 5 s) failing for over 15 s and it says so; the
    first that answers and it's gone - its clock run forward, not waited."""
    page = open_page("/climate-admin", 390, clock=True).page
    page.wait_for_timeout(1500)
    assert page.locator("#connection-banner").is_hidden()
    page.route("**/api/**", lambda route: route.abort())
    for _ in range(5):
        page.clock.run_for(5000)
        page.wait_for_timeout(300)
    assert eventually(page, page.locator("#connection-banner").is_visible, timeout_ms=5000)
    assert "non raggiungibile" in page.inner_text("#connection-banner")
    page.unroute("**/api/**")
    page.clock.run_for(5000)
    assert eventually(page, page.locator("#connection-banner").is_hidden, timeout_ms=5000), "recovers by itself"


def test_the_vmc_speeds_are_set_here_and_shown_on_the_home_page(open_page, api):
    """Night, day and boost percentages: refused when out of order, in use
    at once when saved - the home card's buttons say so, no restart."""
    before = api("/api/climate-admin/vmc-speeds")["speeds"]
    opened = open_page("/climate-admin", 390)
    page = opened.page
    page.wait_for_selector("#vmc-speeds:not([hidden])", state="attached", timeout=20000)
    page.evaluate("document.getElementById('plant-settings').open = true")
    page.fill("#vmc-speed-night", "60")
    page.click("#save-vmc-speeds-btn")
    assert eventually(page, lambda: "più alta della precedente" in page.inner_text("#save-vmc-speeds-result"))
    page.fill("#vmc-speed-night", "30")
    page.click("#save-vmc-speeds-btn")
    assert eventually(page, lambda: api("/api/climate-admin/vmc-speeds")["speeds"]["night"] == 30)
    home = open_page("/", 390).page
    home.wait_for_selector("#vmc-card button[data-vmc]", timeout=20000)
    assert "30%" in home.locator("#vmc-card button[data-vmc]", has_text="Notte").inner_text()
    api("/api/climate-admin/vmc-speeds", before)
    # The browser logs the refused save's 400; nothing else.
    assert [e for e in opened.errors if "400" not in e] == []


def test_the_vmc_automatics_are_off_until_switched_on_here(open_page, api):
    """Under the VMC's speeds: each automatic off, its recommended values
    ready; switched on and saved, in use at once; the recommended values
    back with one button."""
    assert not any(api("/api/climate-admin/vmc-auto")["settings"][k] for k in ("shower", "humid", "cooling"))
    opened = open_page("/climate-admin", 390)
    page = opened.page
    try:
        page.wait_for_selector("#vmc-auto:not([hidden]) [data-vmc-auto='shower']", state="attached", timeout=20000)
        page.evaluate("document.getElementById('plant-settings').open = true")
        box = page.locator("#vmc-auto")
        assert box.locator("input[type=checkbox]:checked").count() == 0
        assert box.locator("[data-rule='shower'] .vmc-auto-fields").get_attribute("aria-disabled") == "true"
        assert box.locator("[data-vmc-auto='shower_rise']").input_value() == "8"
        assert "Scegli la zona del bagno" in box.inner_text(), "neither test zone is kept out of cooling"
        speeds = api("/api/climate-admin/vmc-speeds")["speeds"]
        said = box.locator(".vmc-auto-speed").all_inner_texts()
        assert said == [f"Se va più piano: Massimo ({speeds['boost']}%). Nelle ore silenziose: Giorno ({speeds['day']}%).",
                        f"Se va più veloce: Notte ({speeds['night']}%), mai spenta.",
                        f"Se va più piano: Giorno ({speeds['day']}%), mai Massimo.",
                        f"Se va più veloce: Notte ({speeds['night']}%), mai spenta."]
        box.locator("[data-vmc-auto='shower']").check()
        box.locator("[data-vmc-auto-zone='bathroom_zone']").select_option(ZONE_B)
        box.locator("[data-vmc-auto='shower_max_minutes']").fill("45")
        box.locator("button.open").click()
        assert eventually(page, lambda: "Salvato" in page.inner_text("#vmc-auto-result"))
        saved = api("/api/climate-admin/vmc-auto")
        assert saved["settings"]["shower"] and saved["settings"]["shower_max_minutes"] == 45
        assert saved["bathroom"] == ZONE_B and "Scegli la zona del bagno" not in box.inner_text()
        assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
        box.locator("button:not(.open)").click()
        assert eventually(page, lambda: "consigliati" in page.inner_text("#vmc-auto-result"))
        assert api("/api/climate-admin/vmc-auto")["settings"]["shower"] is False
    finally:
        api("/api/climate-admin/vmc-auto", {"reset": True})
    assert opened.errors == []


def test_the_vmc_filters_are_set_here_and_cleaned_today(open_page, api):
    """Under the VMC's automatics: no reminder until a cleaning is
    recorded; how often, the last day, and "cleaned today" - the same
    reminder the home card shows."""
    api("/api/climate-admin/vmc-filters", {"interval_days": 90, "cleaned_on": None})
    opened = open_page("/climate-admin", 390)
    page = opened.page
    try:
        page.wait_for_selector("#vmc-filters:not([hidden]) #vmc-filters-cleaned-btn", state="attached", timeout=20000)
        page.evaluate("document.getElementById('plant-settings').open = true")
        box = page.locator("#vmc-filters")
        assert "non ancora registrata" in box.inner_text()
        page.fill("#vmc-filters-interval", "60")
        page.click("#vmc-filters-save-btn")
        assert eventually(page, lambda: api("/api/climate-admin/vmc-filters")["filters"]["interval_days"] == 60)
        page.click("#vmc-filters-cleaned-btn")
        assert eventually(page, lambda: api("/api/climate-admin/vmc-filters")["filters"]["state"] == "ok")
        assert eventually(page, lambda: "tra 60 giorni" in box.inner_text())
        assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    finally:
        api("/api/climate-admin/vmc-filters", {"interval_days": 90, "cleaned_on": None})
    assert opened.errors == []


def test_hours_and_the_heat_meter_a_year_month_by_month(open_page, api):
    """A year's months beside the plant's hours; the meter's month-end
    values typed for the whole year at once, each month's kWh worked out;
    a value below the month before's refused; this month still running."""
    from datetime import date
    this_year = date.today().year
    last_year = this_year - 1
    opened = open_page("/climate-admin", 390)
    page = opened.page
    try:
        page.wait_for_selector("#usage-body .usage-years", state="attached", timeout=20000)
        page.evaluate("document.getElementById('usage').open = true")
        assert page.locator("#usage .usage-years button.active").inner_text() == str(this_year)
        assert "finora" in page.inner_text("#usage .usage-table"), "this month, still running"
        assert "meno quella del mese prima" in page.inner_text("#usage .usage-legend"), "each column says what it holds"
        assert "Come funziona" in page.inner_text("#usage .usage-how")
        # The six years kept, each one open to fill in; one with nothing yet greyed and said so.
        years = page.locator("#usage .usage-years button")
        assert years.all_inner_texts() == [str(y) for y in range(this_year, this_year - 6, -1)]
        oldest = page.locator(f"#usage .usage-years button:has-text('{this_year - 5}')")
        assert "usage-year-empty" in oldest.get_attribute("class") and "ancora niente" in oldest.get_attribute("title")
        oldest.click()
        assert eventually(page, lambda: page.locator(f"#usage button:has-text('Valori del contatore {this_year - 5}')").count() == 1)
        with page.expect_download() as download:
            page.click("#usage a:has-text('Esporta CSV')")
        assert download.value.suggested_filename.startswith("ore-e-consumi-")
        page.click(f"#usage .usage-years button:has-text('{last_year}')")
        assert eventually(page, lambda: page.locator("#usage .usage-table tr").count() == 14)   # head, 12, total
        page.click(f"#usage button:has-text('Valori del contatore {last_year}')")
        page.fill("#usage .usage-edit input[data-month='11'][data-register='heat_kwh']", "1000")
        page.fill("#usage .usage-edit input[data-month='12'][data-register='heat_kwh']", "1200")
        page.fill("#usage .usage-edit input[data-month='12'][data-register='cool_kwh']", "50")
        december = page.locator("#usage .usage-edit input[data-month='12'][data-register='heat_kwh']")
        assert december.get_attribute("placeholder") == "al 31/12"
        # As it's typed: the month's usage, or why there's none.
        assert december.locator("xpath=following-sibling::span").inner_text() == "consumo 200 kWh"
        november = page.locator("#usage .usage-edit input[data-month='11'][data-register='heat_kwh']")
        assert november.locator("xpath=following-sibling::span").inner_text() == "consumo: manca ottobre"
        page.click(f"#usage button:has-text('Salva {last_year}')")
        assert eventually(page, lambda: "Salvato" in page.inner_text("#usage-result"))
        assert "200 caldo" in page.inner_text("#usage .usage-table"), "December: its value less November's"
        assert page.locator("#usage .usage-edit").count() == 0, "closed once saved"
        page.click(f"#usage button:has-text('Valori del contatore {last_year}')")
        page.fill("#usage .usage-edit input[data-month='12'][data-register='heat_kwh']", "900")
        assert december.locator("xpath=following-sibling::span").inner_text() == "più bassa di novembre (1000)"
        page.click(f"#usage button:has-text('Salva {last_year}')")
        assert eventually(page, lambda: "più basso del mese prima" in page.inner_text("#usage-result"))
        assert f"12/{last_year}" in page.inner_text("#usage-result")
        assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
        page.click("#usage button:has-text('Annulla')")
        # A year with the outdoor sensor, the year before and this month's estimate.
        last = {"kwh": 100, "plant_hours": None, "kwh_per_hour": None, "kwh_per_dd": None, "degree_days": None}
        month = {"heat_meter": None, "cool_meter": None, "days_known": 30, "zones": {"Stanze": 120, "Bagni": 120}}
        page.evaluate("data => renderUsage(data)", {
            "ok": True, "zones": ["Stanze", "Bagni"], "years": [2026, 2025], "year": 2026, "this_month_hours": 67,
            "before": {"heat_kwh": None, "cool_kwh": None},
            "months": [
                {**month, "month": "2026-09", "open": False, "heat_kwh": 90, "cool_kwh": 0, "kwh": 90, "days": 30,
                 "plant_hours": 180, "outdoor_c": 14, "degree_days": 180, "kwh_per_hour": 0.5, "kwh_per_dd": 0.5,
                 "last_year": last},
                {**month, "month": "2026-10", "open": True, "heat_kwh": None, "cool_kwh": None, "kwh": None, "days": 12,
                 "days_known": 12, "plant_hours": 67, "outdoor_c": 12, "degree_days": 96, "kwh_per_hour": None,
                 "kwh_per_dd": None, "last_year": {**last, "kwh": 150},
                 "estimate": {"kwh": 34, "per_hour": 0.5, "kind": "heat", "months": ["2026-09"]}}],
            "total": {"heat_kwh": 90, "cool_kwh": 0, "kwh": 90, "plant_hours": 247, "zones": {"Stanze": 150, "Bagni": 140},
                      "kwh_per_hour": 0.5, "months_paired": 1, "degree_days": 276, "kwh_per_dd": 0.5,
                      "last_year": {"months": 1, "kwh": 90, "last_kwh": 100}}})
        table = page.inner_text("#usage .usage-table").replace(" ", " ")
        assert "esterno 14,0 °C · 180 GG" in table and "0,50 kWh/GG" in table and "276 GG" in table
        assert "2025: 100 kWh (−10%)" in table and "2025, stesso mese: 100 kWh (−10%)" in table
        assert "≈ 34 kWh, stima" in table and "67 h × 0,5 kWh/ora, media di set" in table and "2025: 150 kWh" in table
        assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
        assert api(f"/api/climate-admin/usage?year={last_year}")["months"][11]["heat_meter"] == 1200
    finally:
        api("/api/climate-admin/heat-meter/year", {"year": last_year, "months": {}})
    assert [e for e in opened.errors if "400" not in e] == []


def test_the_plants_tuning_and_the_boards_settings_are_one_form(open_page, api):
    """Shown in two places - the thermostat's next to the zones, the rest
    under Board - and either Save sends every field: none is lost."""
    before = {r["key"]: r for r in api("/api/climate-admin/settings")["settings"]}
    opened = open_page("/climate-admin", 1280)
    page = opened.page
    page.evaluate("for (const id of ['plant-settings', 'settings']) document.getElementById(id).open = true")
    assert eventually(page, lambda: page.locator("#plant-settings-form [data-setting='hysteresis_c']").count() == 1)
    assert page.locator("#settings-form [data-setting='hysteresis_c']").count() == 0
    assert page.locator("#settings-form [data-setting='history_retention_days']").count() == 1
    page.fill("[data-setting='history_retention_days']", "20")
    page.fill("[data-setting='hysteresis_c']", "0.7")
    page.click("#save-plant-settings-btn")
    assert eventually(page, lambda: "Salvato" in page.inner_text("#plant-settings-result"))
    saved = {r["key"]: r["value"] for r in api("/api/climate-admin/settings")["settings"]}
    assert (saved["hysteresis_c"], saved["history_retention_days"]) == (0.7, 20)
    api("/api/climate-admin/settings", {"values": {k: r["value"] for k, r in before.items() if r["source"] == "panel"}})
    assert opened.errors == []


def test_the_history_size_says_what_the_disk_allows(open_page, api):
    """The maximum history size is the disk's, and the field says so."""
    row = next(r for r in api("/api/climate-admin/settings")["settings"] if r["key"] == "history_max_db_mb")
    opened = open_page("/climate-admin", 390)
    page = opened.page
    page.evaluate("document.getElementById('settings').open = true")
    field = page.locator("#settings-form .admin-field", has=page.locator("[data-setting='history_max_db_mb']"))
    assert eventually(page, lambda: "Su questo disco al massimo" in field.inner_text())
    assert f"50–{row['max']}" in field.inner_text()
    assert page.locator("[data-setting='history_max_db_mb']").get_attribute("max") == str(row["max"])
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_diagnostics_and_maintenance_are_one_section_how_it_works_is_the_guide(open_page):
    """For production, a shorter page: how the software and the plant work
    is in the administrator's guide (the link at the top), not here; the
    two rarely needed tools are one section, maintenance set apart in red
    under diagnostic mode. The four-step guide is for an installation with
    no zones."""
    opened = open_page("/climate-admin", 390)
    page = opened.page
    assert page.locator("#guide").is_hidden(), "zones are set up here"
    assert page.locator("#how-it-works").count() == 0
    assert page.locator("header .admin-guide-link").get_attribute("href") == "/static/guide/shelly-plus1-valvole.html"
    section = page.locator("#diagnostic")
    assert section.locator("summary .sec-title").inner_text() == "Diagnosi e manutenzione"
    assert eventually(page, lambda: section.locator("#diagnostic-summary").inner_text() == "Diagnosi spenta")
    page.evaluate("document.getElementById('diagnostic').open = true")
    assert [h.inner_text() for h in section.locator("h3.admin-subheading").all()] == ["Modalità diagnosi", "Manutenzione"]
    assert section.locator("#diagnostic-mode-toggle").is_visible()
    assert section.locator("#maintenance .maint-row").count() == 4
    assert page.locator("details#maintenance").count() == 0
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_every_device_is_in_one_list_a_shelly_opens_to_its_details(open_page, api):
    """Devices: one row each, those not answering first; a Shelly's row
    (here the simulated H&Ts) opens to what it says about itself, and stays
    open across refreshes. System status doesn't list them a second time."""
    names = {d["name"] for d in api("/api/system-status")["devices"]}
    opened = open_page("/climate-admin", 390)
    page = opened.page
    page.evaluate("document.getElementById('devices').open = true")
    rows = page.locator("#device-list > li:not(.device-group)")
    assert eventually(page, lambda: rows.count() == len(names))
    # Each zone's sensor and valve together, under the zone's name, before
    # the rest - grouped once the zones have come too (a busy machine had
    # the devices first: seen in a full run).
    groups = page.locator("#device-list > li.device-group")
    assert eventually(page, lambda: groups.count() >= 3)
    items = page.locator("#device-list > li").all_inner_texts()
    assert items[0].strip().lower() == f"zona {ZONE_A}".lower()
    assert f"Sensore {ZONE_A}" in items[1] and f"Valvola {ZONE_A}" in items[2]
    # Seen on the production board: two valves left simulated, their
    # Shellys connected, read "OK" like real ones. A stand-in says so.
    assert "SIMULATO" in items[2].upper(), items[2]
    assert items[3].strip().lower() == f"zona {ZONE_B}".lower()
    assert "altri dispositivi" in [i.strip().lower() for i in items]
    # Right after the server starts a device may not have answered yet.
    assert eventually(page, lambda: "tutti ok" in page.inner_text("#devices-summary"))
    shelly = page.locator("#device-list details.device-row", has_text=ZONE_A).first
    assert "a batteria" in shelly.locator("summary").inner_text()
    shelly.locator("summary").click()
    assert "Ultimo invio" in shelly.inner_text()
    page.evaluate("refreshSystemStatus(true)")
    page.wait_for_timeout(1000)
    assert page.locator("#device-list details.device-row[open]").count() == 1, "still open after a refresh"
    page.evaluate("document.getElementById('system-status').open = true")
    assert "Dispositivi (" not in page.inner_text("#system-status")
    assert page.evaluate(UNTRANSLATED_JS) == [] and page.evaluate(OVERFLOW_JS) <= 0
    assert opened.errors == []


def test_the_shelly_list_follows_what_the_shellys_say(open_page):
    """Not every minute only: a mark of what the Shellys said, asked with
    every refresh, and the list read again as soon as it moves."""
    revision = {"now": "a"}
    reads = []
    opened = open_page("/climate-admin", 1280)
    page = opened.page
    page.route("**/api/climate-admin/shelly-health/revision",
               lambda route: route.fulfill(json={"ok": True, "revision": revision["now"]}))
    page.on("request", lambda r: reads.append(1) if r.url.endswith("/api/climate-admin/shelly-health") else None)
    page.wait_for_timeout(8000)
    before = len(reads)
    revision["now"] = "b"
    assert eventually(page, lambda: len(reads) > before, timeout_ms=8000), "read again in seconds, not in a minute"
    assert opened.errors == []


def test_a_shellys_safety_timer_is_said_in_system_status_and_in_the_history(open_page, api):
    """Its hold ran out: for 72 hours in its own neutral box, with the
    time - not with the problems, it's over - and in the watchdog history
    under "Shelly"; each with the device's own name as the page shows it,
    any name escaped."""
    import json as _json
    status = api("/api/system-status")
    status["problems"] = []
    status["recent"] = [
        {"code": "safety_timer", "subject": "Dehumidifier", "at": 1791098539.0},
        {"code": "safety_timer", "subject": f"{ZONE_B} Valve", "at": 1791098599.0}]
    status["watchdog_history"] = [
        {"when": "2026-10-04 09:22:19", "source": "shelly", "code": "safety_timer", "values": "Dehumidifier"},
        {"when": "2026-10-04 09:23:19", "source": "shelly", "code": "safety_timer", "values": f"{ZONE_B} Valve"}]
    opened = open_page("/climate-admin", 390)
    page = opened.page
    page.route("**/api/system-status*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(status)))
    page.reload()
    page.evaluate("document.getElementById('system-status').open = true")
    assert eventually(page, lambda: "timer di sicurezza" in page.inner_text("#system-status"))
    text = page.inner_text("#system-status")
    assert "Successo nelle ultime 72 ore" in page.inner_text("#system-recent")
    assert "Deumidificatore: è scattato il timer di sicurezza dello Shelly" in page.inner_text("#system-recent")
    assert page.locator("#system-problems").is_hidden(), "not a problem: it's over"
    assert "Nessuna anomalia" in page.inner_text("#system-status-summary")
    assert text.count("è scattato il timer di sicurezza dello Shelly") == 4, "twice in the box, twice in the history"
    assert f"{ZONE_B} Valve: è scattato" in text
    assert page.locator("#system-status img[src='x']").count() == 0, "a name is text, never markup"
    history = page.locator("#system-status li", has_text="comando non rinnovato").first
    assert "Shelly" in history.locator(".admin-pill").inner_text()
    assert page.evaluate(UNTRANSLATED_JS) == [] and opened.errors == []
