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

    def __init__(self, browser, board, width=360, height=780, scheme="light", lang="it", clock=False):
        self.board = board
        self.context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
        self.page = self.context.new_page()
        self.errors = []
        self.refused = []
        self.problems = []       # what /api/problems says (the shutter off the network would)
        self.valves = None       # zone -> valve_on, when made up
        self.down = False
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.add_init_script(HOST)
        if clock:
            self.page.clock.install()
        self.context.route(f"{ORIGIN}/**", self.serve)
        self.page.goto(f"{ORIGIN}/index.html?lang={lang}&board=192.0.2.1:5000")
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
        if path == "/api/problems":
            route.fulfill(json={"ok": True, "problems": self.problems})
            return
        answer = route.fetch(url=self.board + path)
        if path == "/api/climate/zones" and self.valves is not None:
            data = answer.json()
            for zone in data["zones"]:
                if zone["name"] in self.valves:
                    zone["valve_on"] = self.valves[zone["name"]]
            route.fulfill(response=answer, json=data)
            return
        route.fulfill(response=answer)

    def api(self, path):
        return self.page.request.get(self.board + path).json()

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
    assert not re.search(r"\b(c_|t_|rh_|src_|vmc_|notify_)[a-z_]+\b", text)
    assert w.page.locator('[data-zone="Stanze"] .comfort').count() == 1
    w.sane()


def test_a_comfort_word_follows_the_readings(window):
    w = window()
    said = w.page.evaluate("""() => {
        const climate = { mode: "heat", away_until: null };
        const of = (t, rh, target, cool) => comfortWord(comfortOf(
            { temperature_c: t, humidity_pct: rh, target_c: target, sensor_ok: true, cool_enabled: cool }, climate));
        return [of(20.4, 47, 20, true), of(18.6, 52, 20, true), of(16.8, 74, null, true), of(21, 27, 20, true)];
    }""")
    assert said == ["Confortevole", "Fresco", "Freddo e molto umido", "Aria secca"]


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
    assert notice["title"] == "LocalHome: da controllare", "its lateness follows from it: one thing"
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


def test_a_zone_opens_the_full_page_in_the_browser(window):
    w = window()
    w.page.click('[data-zone="Stanze"]')
    opened = [m for m in w.page.evaluate("window.__told") if m["type"] == "open"]
    assert opened == [{"type": "open", "path": "/"}]
    w.sane()


def test_the_lists_of_allowed_requests_leave_out_the_rest():
    for path in ("/api/climate/zones", "/api/problems", "/api/covers"):
        assert READS.match(path)
    for path in ("/api/covers/all/action/close", "/api/vmc-schedule/boost", "/api/sensors/VMC/set-value"):
        assert COMMANDS.match(path)
    for path in ("/api/sensors/VMC/off", "/api/climate/mode", "/api/system/restart", "/api/covers/all/action/delete",
                 "/api/climate-admin/diagnostics.zip"):
        assert not READS.match(path) and not COMMANDS.match(path), path


def test_both_languages_say_the_same_things():
    source = (WEB / "texts.js").read_text(encoding="utf-8")
    texts = json.loads(source[source.index("{"):source.rindex("}") + 1])
    assert set(texts["it"]) == set(texts["en"])
    assert set(texts["it"]["problems"]) == set(texts["en"]["problems"])
