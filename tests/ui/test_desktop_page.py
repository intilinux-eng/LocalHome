"""The Windows app's page (desktop/web/) on a board of simulated devices, the
way the program serves it: from its own address, each /api/ request passed on
to the board - and refused unless MainForm.cs's lists allow it, so a request
the page makes and the program wouldn't pass fails here. The program's half
(window.chrome.webview) is a stand-in that keeps what it's told.
"""
import json
import re
import urllib.parse
from pathlib import Path

import pytest

from .conftest import CONFIG
from .helpers import OVERFLOW_JS, SIMULATED_ZONE

pytestmark = pytest.mark.ui

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "desktop" / "web"
ORIGIN = "https://localhome.example"
ROOMS, BATHS = "Stanze", "Bagni"
HEATING = CONFIG.replace('\n  mode: "cool"', '\n  mode: "heat"', 1)
SHUTTERS = """\
  - kind: cover
    type: simulated
    name: "Tapparella Cucina"
    travel_time_seconds: 3
  - kind: cover
    type: simulated
    name: "Tapparella Bagno"
    travel_time_seconds: 3
  - kind: cover
    type: simulated
    name: "Tapparella Camera"
    unreachable: true
"""
HOST = """
window.__told = [];
window.chrome = window.chrome || {};
window.chrome.webview = { postMessage: m => window.__told.push(JSON.parse(JSON.stringify(m))) };
"""
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}


def allowed_lists():
    source = (ROOT / "desktop" / "MainForm.cs").read_text(encoding="utf-8")
    reads, commands = re.findall(r'new Regex\(\s*@"([^"]+)"\)', source)
    return re.compile(reads), re.compile(commands)


READS, COMMANDS = allowed_lists()


@pytest.fixture(scope="module")
def board(start_server):
    config = HEATING.replace("integrations:\n", "integrations:\n" + SHUTTERS, 1)
    return start_server([dict(SIMULATED_ZONE, name=ROOMS), dict(SIMULATED_ZONE, name=BATHS, cool_enabled=False)], config)


class Window:
    """The window: the page, what the board is made to say, what the tray was told."""

    def __init__(self, browser, board, width=360, height=780, scheme="light", lang="it", clock=False, name="Casa"):
        self.board = board
        self.context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
        self.page = self.context.new_page()
        self.errors = []
        self.refused = []
        self.problems = []       # what /api/problems says (the shutter off the network would)
        self.valves = None       # zone -> valve_on, when made up
        self.down = False
        self.climate = None      # fields of /api/climate/zones made up
        self.answers = {}        # commands answered here: path -> its answer
        self.sent = []           # the commands that reached this point
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.add_init_script(HOST)
        if clock:
            self.page.clock.install()
        self.context.route(f"{ORIGIN}/**", self.serve)
        self.page.goto(f"{ORIGIN}/index.html?lang={lang}&board=192.0.2.1:5000&name={urllib.parse.quote(name)}")
        self.page.wait_for_selector(".zone")

    def serve(self, route):
        request = route.request
        path = urllib.parse.urlsplit(request.url).path
        if not path.startswith("/api/"):
            file = WEB / path.lstrip("/")
            route.fulfill(status=200, body=file.read_bytes(), content_type=TYPES[file.suffix])
            return
        listed = READS if request.method == "GET" else COMMANDS
        if not listed.match(path):
            self.refused.append(f"{request.method} {path}")
            route.fulfill(status=403, json={"ok": False})
            return
        if self.down:
            route.fulfill(status=502, json={"ok": False, "unreachable": True})
            return
        if request.method == "POST":
            self.sent.append((path, request.post_data_json))
            if path in self.answers:
                route.fulfill(json=self.answers[path])
                return
        if path == "/api/problems":
            route.fulfill(json={"ok": True, "problems": self.problems})
            return
        answer = route.fetch(url=self.board + path)
        if path == "/api/climate/zones" and (self.valves is not None or self.climate):
            data = answer.json()
            for zone in data["zones"]:
                if self.valves and zone["name"] in self.valves:
                    zone["valve_on"] = self.valves[zone["name"]]
            data.update(self.climate or {})
            route.fulfill(response=answer, json=data)
            return
        route.fulfill(response=answer)

    def api(self, path):
        return self.page.request.get(self.board + path).json()

    def zone(self, name=ROOMS):
        return next(z for z in self.api("/api/climate/zones")["zones"] if z["name"] == name)

    def card(self, name=ROOMS):
        return self.page.locator(f'.zone[data-zone="{name}"]')

    def told(self, kind=None):
        messages = self.page.evaluate("window.__told")
        return [m for m in messages if m["type"] == "notify" and (kind is None or m["kind"] == kind)]

    def last_status(self):
        return [m for m in self.page.evaluate("window.__told") if m["type"] == "status"][-1]

    def refresh(self):
        self.page.evaluate("refresh()")

    def sane(self):
        assert not self.errors, self.errors
        assert not self.refused, f"the program wouldn't pass: {self.refused}"
        assert self.page.evaluate(OVERFLOW_JS) <= 0

    def speed(self):
        devices = self.api("/api/devices")["sensors"]
        speed = next(d for d in devices if d["kind"] == "number" and d.get("paired_switch") and d.get("presets"))
        return speed, self.api("/api/readings")["readings"][speed["name"]]["value"]


@pytest.fixture
def window(browser, board):
    opened = []

    def _open(**options):
        w = Window(browser, board, **options)
        opened.append(w)
        return w

    yield _open
    for w in opened:
        # The page keeps reading until it's gone: its requests under way end here.
        w.context.unroute_all(behavior="ignoreErrors")
        w.context.close()
    # The board as the next test expects it: no change by hand, at home.
    request = browser.new_context().request
    for name in (ROOMS, BATHS):
        request.post(f"{board}/api/climate/zones/{name}/override/cancel", data={})
    request.post(f"{board}/api/climate/away/cancel", data={})


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("lang", ["it", "en"])
def test_it_shows_what_the_board_has(window, scheme, lang):
    w = window(scheme=scheme, lang=lang)
    w.page.wait_for_selector("#vmc .levels")
    assert w.page.locator(".zone").count() == 2
    assert w.page.locator("#covers .rows li").count() == 3
    text = w.page.inner_text("body")
    assert "Tapparella" not in w.page.inner_text("#covers .rows"), "the shared word stays in the title"
    # Only what the view is for: another switch of the board has no button.
    assert "Lampada" not in text
    # No text left as its key.
    assert not re.search(r"\b(c_|t_|rh_|src_|vmc_|notify_|hold_|away_|dehum_|until_)[a-z_]+\b", text)
    assert w.page.locator('[data-zone="Stanze"] .comfort').count() == 1
    w.sane()


def test_a_comfort_word_follows_the_readings(window):
    w = window()
    said = w.page.evaluate("""() => {
        const climate = { mode: "heat", away_until: null };
        const of = (t, rh, target, cool) => comfortWord(comfortOf(
            { temperature_c: t, humidity_pct: rh, target_c: target, sensor_ok: true, cool_enabled: cool }, climate));
        return [of(20.4, 47, 20, true), of(18.6, 52, 20, true), of(22, 49, 20, true), of(21, 27, 20, true),
                // No program: seen on the production board, 22° and 49% with the heating off.
                of(22, 49, null, true), of(18, 49, null, true), of(24.5, 49, null, true), of(16, 74, null, true)];
    }""")
    assert said == ["Confortevole", "Fresco", "Caldo", "Aria secca",
                    "Confortevole", "Confortevole", "Caldo", "Freddo e molto umido"]


def test_the_vmc_by_its_levels_and_for_a_while(window):
    w = window()
    w.page.wait_for_selector("#vmc .levels")
    speed, _ = w.speed()
    night = next(p for p in speed["presets"] if p["key"] == "night")["value"]
    w.page.click(f'#vmc button[data-level-value="{night}"]')
    w.page.wait_for_function(f"""document.querySelector('#vmc button[data-level-value="{night}"]').getAttribute('aria-pressed') === 'true'""")
    assert w.speed()[1] == night

    w.page.click('#vmc button[data-boost="30"]')
    w.page.wait_for_selector("#vmc button[data-boost-cancel]")
    status = w.api("/api/vmc-schedule")["status"]
    assert status["override"]["value"] == max(p["value"] for p in speed["presets"])
    w.page.click("#vmc button[data-boost-more]")
    w.page.wait_for_function("document.querySelector('#vmc button[data-boost-cancel]') !== null")

    # A level picked with a timed speed running ends it.
    w.page.click(f'#vmc button[data-level-value="{night}"]')
    w.page.wait_for_selector('#vmc button[data-boost="30"]')
    assert w.api("/api/vmc-schedule")["status"].get("override") is None
    assert w.speed()[1] == night
    w.sane()


def test_the_vmc_switched_off_offers_to_turn_on_and_nothing_to_turn_it_off(window):
    w = window()
    w.page.wait_for_selector("#vmc .levels")
    speed, _ = w.speed()
    assert w.page.locator("#vmc button[data-power]").count() == 0, "no switching off from the window"
    w.page.request.post(f"{w.board}/api/sensors/{urllib.parse.quote(speed['paired_switch'])}/off", data={})
    w.refresh()
    w.page.wait_for_selector("#vmc button[data-power]")
    w.page.click("#vmc button[data-power]")
    w.page.wait_for_selector("#vmc .levels")
    w.sane()


def test_the_shutters_all_at_once_and_one_by_itself(window):
    w = window()
    w.page.wait_for_selector("#covers .rows li")
    w.page.click('button[data-cover="Tapparella Cucina"][data-action="close"]')
    w.page.wait_for_function("""document.querySelector('#covers .rows').innerText.includes('Si chiude')""", timeout=5000)
    w.page.click('button[data-cover="Tapparella Cucina"][data-action="stop"]')
    w.page.wait_for_function("""!document.querySelector('#covers .rows').innerText.includes('Si chiude')""", timeout=5000)

    # The one off the network is said by name, in the window.
    w.page.click('button[data-cover="all"][data-action="open"]')
    w.page.wait_for_selector("#note:not([hidden])", timeout=20000)
    assert "Camera" in w.page.inner_text("#note")
    assert "Non risponde" in w.page.inner_text("#covers .rows")
    assert w.page.locator("[aria-busy='true']").count() == 0
    w.sane()


def test_a_zone_turning_on_is_told_once_and_not_at_the_start(window):
    w = window()
    w.valves = {ROOMS: False, BATHS: False}
    w.refresh()
    w.page.wait_for_timeout(400)
    assert w.told("zone") == []

    w.valves = {ROOMS: True, BATHS: False}
    w.refresh()
    w.page.wait_for_function("window.__told.some(m => m.kind === 'zone')")
    notice = w.told("zone")[0]
    assert notice["title"] == f"Riscaldamento acceso: {ROOMS}"
    assert ROOMS in notice["text"]
    assert w.last_status()["level"] == "busy"
    assert "Scalda" in w.page.inner_text('[data-zone="Stanze"]')
    assert ROOMS in w.page.inner_text("#say")

    w.refresh()
    w.page.wait_for_timeout(400)
    assert len(w.told("zone")) == 1, "still on: nothing more to say"

    # Two together, one notice.
    w.valves = {ROOMS: False, BATHS: False}
    w.refresh()
    w.page.wait_for_timeout(400)
    w.valves = {ROOMS: True, BATHS: True}
    w.refresh()
    w.page.wait_for_function("window.__told.filter(m => m.kind === 'zone').length === 2")
    assert w.told("zone")[1]["title"] == f"Riscaldamento acceso: {ROOMS}, {BATHS}"
    w.sane()


def test_something_to_check_is_told_then_that_its_solved(window):
    w = window()
    offline = {"code": "device_offline", "subject": "Stanze Valve"}
    late = {"code": "device_report_late", "subject": "Stanze Valve"}
    w.problems = [offline, late]
    w.refresh()
    w.page.wait_for_function("window.__told.some(m => m.kind === 'problem')")
    notice = w.told("problem")[0]
    assert "La valvola di Stanze non risponde." in notice["text"], notice
    assert notice["title"] == "Casa: da controllare", "its lateness follows from it: one thing"
    assert w.last_status()["level"] == "warn"
    assert "1 da controllare" in w.page.inner_text("#health")
    assert "non risponde" in w.page.inner_text("#alerts")

    w.refresh()
    w.page.wait_for_timeout(400)
    assert len(w.told("problem")) == 1, "still wrong: said once"

    w.problems = []
    w.refresh()
    w.page.wait_for_function("window.__told.some(m => m.kind === 'fixed')")
    assert w.last_status()["level"] == "ok"
    assert w.page.locator("#alerts").is_hidden()

    # Coming straight back, it isn't told as new.
    w.problems = [offline]
    w.refresh()
    w.page.wait_for_timeout(400)
    assert len(w.told("problem")) == 1
    w.sane()


def test_a_problem_this_app_has_no_words_for_still_says_what_it_is(window):
    w = window()
    w.problems = [{"code": "something_new", "subject": "Scheda"}]
    w.refresh()
    w.page.wait_for_function("window.__told.some(m => m.kind === 'problem')")
    assert "something_new" in w.told("problem")[0]["text"]
    w.sane()


def test_the_board_out_of_reach_old_values_faded_and_told_after_a_while(window):
    w = window(clock=True)
    w.page.wait_for_selector("#vmc .levels")
    w.down = True
    w.refresh()
    w.page.wait_for_selector("#body.is-old")
    assert w.last_status()["level"] == "off"
    assert "Non raggiungibile" in w.page.inner_text("#health")
    assert "192.0.2.1:5000" in w.page.inner_text("#alerts")
    assert "Stato attuale non noto" in w.page.inner_text("#say")
    assert w.told("down") == [], "a short gap isn't worth a notice"

    w.page.clock.run_for(4 * 60 * 1000)
    w.page.wait_for_function("window.__told.some(m => m.kind === 'down')")
    assert len(w.told("down")) == 1

    w.down = False
    w.refresh()
    w.page.wait_for_function("window.__told.some(m => m.kind === 'back')")
    w.page.wait_for_selector("#body:not(.is-old)")
    assert w.last_status()["level"] != "off"
    w.sane()


def test_a_command_with_the_board_gone_is_said_and_the_button_comes_back(window):
    w = window()
    w.page.wait_for_selector("#vmc .levels")
    w.down = True
    w.page.click('#vmc button[data-boost="30"]')
    w.page.wait_for_selector("#body.is-old")
    assert w.page.locator("[aria-busy='true']").count() == 0
    w.down = False
    w.refresh()
    w.page.wait_for_selector('#body:not(.is-old) #vmc button[data-boost="30"]:not([disabled])')
    w.sane()


def test_a_zone_warmer_for_a_while_only_once_the_time_is_picked(window):
    w = window()
    card = w.card()
    assert w.zone()["target_c"] is None, "the simulated zones have no program"
    assert "Spenta" in card.locator(".adjust").inner_text()

    # A zone off starts from a usual temperature: nothing sent yet.
    card.locator('button[data-step="0.5"]').click()
    assert card.locator(".adjust b").inner_text() == "21°"
    card.locator('button[data-step="0.5"]').click()
    card.locator('button[data-step="0.5"]').click()
    card.locator('button[data-step="-0.5"]').click()
    assert card.locator(".adjust b").inner_text() == "21,5°"
    assert w.sent == [] and w.zone()["override"] is None

    # Still chosen after a reading of the board redraws the card.
    w.refresh()
    w.page.wait_for_timeout(400)
    assert card.locator(".adjust b").inner_text() == "21,5°"

    card.locator('button[data-hold="2"]').click()
    card.locator("button[data-resume]").wait_for()
    assert w.sent == [(f"/api/climate/zones/{ROOMS}/override", {"target": 21.5, "hours": 2})]
    assert w.zone()["override"]["target"] == 21.5
    assert "Obiettivo 21,5°" in card.inner_text() and "fino" in card.inner_text()

    # Changed again, from what it's at now.
    card.locator('button[data-step="-0.5"]').click()
    assert card.locator(".adjust b").inner_text() == "21°"
    card.locator("button[data-pending-cancel]").click()
    assert w.zone()["override"]["target"] == 21.5, "cancelled: nothing changed"

    card.locator("button[data-resume]").click()
    w.page.wait_for_function("""!document.querySelector('[data-zone="Stanze"] button[data-resume]')""")
    assert w.zone()["override"] is None
    assert w.page.locator("[aria-busy='true']").count() == 0
    w.sane()


def test_a_target_at_the_end_of_the_range_stays_there(window):
    w = window()
    w.page.evaluate("now.pending['Stanze'] = 34.5; drawClimate()")
    card = w.card()
    card.locator('button[data-step="0.5"]').click()
    card.locator('button[data-step="0.5"]').click()
    assert card.locator(".adjust b").inner_text() == "35°"
    w.sane()


def test_a_command_refused_by_the_board_keeps_the_choice_and_says_so(window):
    w = window()
    card = w.card()
    card.locator('button[data-step="0.5"]').click()
    w.down = True
    card.locator('button[data-hold="4"]').click()
    w.page.wait_for_selector("#note:not([hidden])")
    w.down = False
    w.refresh()
    w.page.wait_for_selector("#body:not(.is-old)")
    assert card.locator('button[data-hold="4"]').is_enabled(), "the choice is still there to try again"
    w.sane()


def test_away_and_back_with_nothing_sent_until_its_length_is_picked(window):
    w = window()
    w.page.wait_for_selector("#vmc .levels")
    w.page.click("#away-chip")
    assert w.page.is_visible("#away button[data-away-hours]")
    assert "spengono" in w.page.inner_text("#away")
    assert w.sent == []
    w.page.click("#away button[data-away-cancel]")
    assert w.page.is_visible("#away-chip") and w.page.locator("#away").is_hidden()
    assert w.sent == []

    w.page.click("#away-chip")
    w.page.click('#away button[data-away-hours="24"]')
    w.page.wait_for_selector("#away button[data-away-back]")
    assert w.sent == [("/api/climate/away", {"hours": 24})]
    assert w.api("/api/climate/zones")["away_until"]
    assert "fino a domani" in w.page.inner_text("#away")
    assert "fuori casa" in w.page.inner_text("#say")
    # Nothing to change on a zone meanwhile: the board would refuse it.
    assert w.card().locator("button.step").count() == 0
    assert w.page.locator("#away-chip").is_hidden()

    w.page.click("#away button[data-away-back]")
    w.page.wait_for_selector("#away-chip:not([hidden])")
    assert w.api("/api/climate/zones")["away_until"] is None
    assert w.card().locator("button.step").count() == 2
    w.sane()


DEHUM = {"on": False, "humidity": 66, "start_from": 55, "stop_below": 50, "durations": [1, 2, 4], "until_target": True,
         "mode": None, "until_at": None}


def test_the_dehumidifier_in_its_season(window):
    w = window()
    w.page.wait_for_selector("#vmc .levels")
    assert w.page.locator("#dehum").is_hidden(), "winter: not for now"

    w.climate = {"mode": "cool", "dehumidifier": DEHUM}
    w.refresh()
    w.page.wait_for_selector('#dehum button[data-dehum-hours="2"]')
    w.answers["/api/dehumidifier/on"] = {"ok": True}
    w.page.click('#dehum button[data-dehum-hours="2"]')
    w.page.wait_for_function("!document.querySelector('#dehum button[aria-busy]')")
    assert w.sent[-1] == ("/api/dehumidifier/on", {"hours": 2})
    w.page.click("#dehum button[data-dehum-target]")
    w.page.wait_for_function("!document.querySelector('#dehum button[aria-busy]')")
    assert w.sent[-1] == ("/api/dehumidifier/on", {"until": "target"})

    w.climate = {"mode": "cool", "dehumidifier": dict(DEHUM, on=True, until_at="2030-01-01T18:00")}
    w.refresh()
    w.page.wait_for_selector("#dehum button[data-dehum-off]")
    w.answers["/api/dehumidifier/off"] = {"ok": True}
    w.page.click("#dehum button[data-dehum-off]")
    w.page.wait_for_function("!document.querySelector('#dehum button[aria-busy]')")
    assert w.sent[-1] == ("/api/dehumidifier/off", {})

    # The air dry enough: said, nothing to press.
    w.climate = {"mode": "cool", "dehumidifier": dict(DEHUM, humidity=48)}
    w.refresh()
    w.page.wait_for_function("document.querySelector('#dehum').innerText.includes('48%')")
    assert w.page.locator("#dehum button").count() == 0
    w.sane()


def test_a_zone_left_out_of_cooling_has_nothing_to_change_in_summer(window):
    w = window()
    w.climate = {"mode": "cool"}
    w.refresh()
    w.page.wait_for_function("document.body.dataset.season === 'cool'")
    assert w.card(BATHS).locator("button.step").count() == 0
    assert "raffrescamento" in w.card(BATHS).inner_text().lower()
    assert w.card(ROOMS).locator("button.step").count() == 2
    w.sane()


def test_the_board_is_called_by_its_name(window):
    w = window(name="Antonio <b>casa</b>")
    assert w.page.inner_text("#brand") == "Antonio <b>casa</b>", "shown as it's written, not as a page's code"
    assert w.page.title() == "Antonio <b>casa</b>"
    assert "Apri Antonio" in w.page.inner_text("#open-full")
    w.problems = [{"code": "device_offline", "subject": "Stanze Valve"}]
    w.refresh()
    w.page.wait_for_function("window.__told.some(m => m.kind === 'problem')")
    assert w.told("problem")[0]["title"].startswith("Antonio <b>casa</b>:")
    w.sane()


def test_a_long_name_stays_on_its_line_and_no_name_is_localhome(window):
    w = window(name="Una scheda con un nome lungo quaranta car")
    assert w.page.locator("#brand").bounding_box()["height"] < 30
    w.sane()
    w = window(name="")
    assert w.page.inner_text("#brand") == "LocalHome"


def test_the_gear_asks_the_program_for_the_settings(window):
    w = window()
    w.page.click("#settings")
    assert {"type": "settings"} in w.page.evaluate("window.__told")
    w.sane()


def test_what_was_seen_of_one_board_says_nothing_of_another(window):
    w = window()
    w.page.evaluate("KEEP_KEY").index("192.0.2.1:5000")
    w.sane()


def test_a_zone_opens_the_full_page_in_the_browser(window):
    w = window()
    w.page.click('[data-zone="Stanze"] .zone-main')
    opened = [m for m in w.page.evaluate("window.__told") if m["type"] == "open"]
    assert opened == [{"type": "open", "path": "/"}]
    w.sane()


def test_the_lists_of_allowed_requests_leave_out_the_rest():
    for path in ("/api/climate/zones", "/api/problems", "/api/covers"):
        assert READS.match(path)
    for path in ("/api/covers/all/action/close", "/api/vmc-schedule/boost", "/api/sensors/VMC/set-value",
                 "/api/climate/zones/Stanze/override", "/api/climate/zones/Stanze/override/cancel",
                 "/api/climate/away", "/api/climate/away/cancel", "/api/dehumidifier/on", "/api/dehumidifier/off"):
        assert COMMANDS.match(path)
    for path in ("/api/sensors/VMC/off", "/api/climate/mode", "/api/system/restart", "/api/covers/all/action/delete",
                 "/api/climate/zones/Stanze/schedule", "/api/dehumidifier/settings",
                 "/api/climate-admin/diagnostics.zip"):
        assert not READS.match(path) and not COMMANDS.match(path), path


def test_both_languages_say_the_same_things():
    source = (WEB / "texts.js").read_text(encoding="utf-8")
    texts = json.loads(source[source.index("{"):source.rindex("}") + 1])
    assert set(texts["it"]) == set(texts["en"])
    assert set(texts["it"]["problems"]) == set(texts["en"]["problems"])
