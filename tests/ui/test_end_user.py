"""The end-user suite: the home page used as a resident would - the usual
uses and the clumsy ones (a double tap, modes mashed, a reload half-way, the
board gone mid-change, two phones at once, larger text) - on a board of
simulated devices, two zones named as a real home's.

After every step `Walk.check()` asks the same of the page: no script error,
nothing wider than the screen, no untranslated key, no button left waiting -
and the step's own claim, checked against what the board says (its API), not
only against what the page shows. A failed check saves a screenshot next to
the test's temporary folder and says where.

Run it alone:   pytest -m enduser -n 6
Add a use case: a test here taking `walk` (a phone, or `walk(width=1280)`),
doing what a finger would, `w.check("what should be true", condition)`
after each step. Each case found in real use belongs here, so it's tried
every time from then on.
"""
import json
import urllib.parse
import urllib.request

import pytest

from .helpers import OVERFLOW_JS, SIMULATED_ZONE, UNTRANSLATED_JS

pytestmark = [pytest.mark.ui, pytest.mark.enduser]

ROOMS, BATHS = "Stanze", "Bagni"
# A week for the rooms: 24° at night, 23° by day - so there's a next change.
WEEK = {d: [24.0] * 7 + [23.0] * 17 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


class House:
    """The board under test, through its API: what the page must agree with."""

    def __init__(self, base):
        self.base = base

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.load(res)

    def zone(self, name=ROOMS):
        return next(z for z in self.call("/api/climate/zones")["zones"] if z["name"] == name)

    def held(self, name=ROOMS):
        return self.zone(name)["override"]

    def week(self, name=ROOMS):
        return self.call(f"/api/climate/zones/{urllib.parse.quote(name)}/schedule?mode=cool")["week"]

    def reset(self):
        """As a resident finds it: no change by hand, at home, the rooms' week."""
        for name in (ROOMS, BATHS):
            self.call(f"/api/climate/zones/{urllib.parse.quote(name)}/override/cancel", {})
        if self.call("/api/climate/zones").get("away_until"):
            self.call("/api/climate/away/cancel", {})
        self.call(f"/api/climate/zones/{urllib.parse.quote(ROOMS)}/schedule", {"week": WEEK, "mode": "cool"})


@pytest.fixture(scope="module")
def house(start_server):
    return House(start_server([dict(SIMULATED_ZONE, name=ROOMS), dict(SIMULATED_ZONE, name=BATHS)]))


class Walk:
    """One phone (a fresh browser context: its own storage) on the home page."""

    def __init__(self, browser, house, shots, width=390, scheme="light", touch=False, network_errors_expected=False):
        self.house = house
        self.shots = shots
        self.context = browser.new_context(viewport={"width": width, "height": 860}, has_touch=touch, is_mobile=touch)
        self.page = self.context.new_page()
        self.page.emulate_media(color_scheme=scheme)
        self.errors = []
        self.network_errors_expected = network_errors_expected
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.page.goto(house.base + "/")
        self.page.wait_for_selector(".zone .mode-row", timeout=30000)
        self.page.wait_for_timeout(1000)
        self.steps = 0

    def card(self, name=ROOMS):
        return self.page.locator(f'.zone[data-zone="{name}"]')

    def mode(self, key, name=ROOMS):
        return self.card(name).locator(f"button[data-action=mode-{key}]")

    def in_mode(self, key, name=ROOMS):
        return self.mode(key, name).get_attribute("aria-checked") == "true"

    def pending_value(self):
        return float(self.card().locator(".pending .stepper-value").inner_text().replace(",", ".").rstrip("°"))

    def wait(self, predicate, timeout_ms=8000):
        waited = 0
        while not predicate():
            if waited >= timeout_ms:
                return False
            self.page.wait_for_timeout(200)
            waited += 200
        return True

    def check(self, what, ok=True, busy_ok=False):
        """The step's claim, and the page's sanity after it."""
        self.steps += 1
        problems = [] if ok else [f"not so: {what}"]
        errors = [e for e in self.errors if not (self.network_errors_expected and "Failed to load resource" in e)]
        self.errors.clear()
        if errors:
            problems.append(f"script errors: {errors[:3]}")
        overflow = self.page.evaluate(OVERFLOW_JS)
        if overflow > 0:
            problems.append(f"the page is {overflow}px wider than the screen")
        missing = self.page.evaluate(UNTRANSLATED_JS)
        if missing:
            problems.append(f"untranslated: {missing}")
        # Waiting right after a tap is fine (the board answering); still
        # waiting 3 s on is stuck.
        waiting = 0 if busy_ok else (self.page.locator("[aria-busy='true']").count()
                                     and not self.wait(lambda: self.page.locator("[aria-busy='true']").count() == 0, 3000))
        if waiting:
            problems.append("a button left waiting")
        if problems:
            shot = self.shots / f"step{self.steps:02d}.png"
            self.page.screenshot(path=str(shot))
            pytest.fail(f"{what}: {'; '.join(problems)} (screenshot: {shot})", pytrace=False)


@pytest.fixture
def walk(browser, house, tmp_path):
    """walk(width=390, scheme="light", touch=False) -> Walk; the house set
    back as a resident finds it before, every phone closed after."""
    house.reset()
    walks = []

    def _walk(**options):
        w = Walk(browser, house, tmp_path, **options)
        walks.append(w)
        return w

    yield _walk
    for w in walks:
        w.context.close()
    house.reset()


# ------------------------------------------------------------- at a glance --

@pytest.mark.parametrize("width", [320, 390, 768, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_opens_at_every_width_and_in_both_schemes(walk, width, scheme):
    w = walk(width=width, scheme=scheme)
    w.check("both zones", w.page.locator(".zone").count() == 2)
    w.check("the rooms follow the hourly table", w.in_mode("schedule"))


@pytest.mark.parametrize("width", [320, 360, 390])
def test_larger_text_on_a_phone_stays_inside_the_screen(walk, width):
    """A phone's text set larger (130%): nothing goes past the screen's edge."""
    w = walk(width=width)
    w.page.evaluate("document.documentElement.style.fontSize = '130%'")
    w.page.wait_for_timeout(400)
    w.check("the page at 130% text")
    w.mode("manual").click()
    w.check("the manual choice at 130% text")
    w.card().locator("button[data-action=cancel-pending]").click()


# ---------------------------------------------------- changing a zone's mode --

def test_manual_a_degree_and_a_half_warmer_for_two_hours_then_back(walk, house):
    w = walk()
    w.mode("manual").click()
    w.check("Manual opens the choice", w.card().locator(".pending").count() == 1 and w.in_mode("manual"))
    shown = w.pending_value()
    for _ in range(3):
        w.card().locator(".pending button[data-action=step][data-delta='0.5']").click()
    w.card().locator(".pending button[data-action=duration][data-hours='2']").click()
    w.card().locator("button[data-action=apply]").click()
    w.check("the board holds it 1.5° up", w.wait(lambda: (house.held() or {}).get("target") == shown + 1.5))
    w.check("the card says manual", w.wait(lambda: w.in_mode("manual") and w.card().locator(".manual-banner").count() == 1))
    w.check("the top says manual", "manuale" in w.page.inner_text("#hero-detail").lower())
    w.mode("schedule").click()
    w.check("Hourly takes it back", w.wait(lambda: house.held() is None) and w.wait(lambda: w.in_mode("schedule")))


def test_off_until_the_next_change_and_back_from_the_banner(walk, house):
    w = walk()
    w.mode("off").click()
    choice = w.card().locator(".pending")
    w.check("Off asks only for how long", choice.count() == 1 and choice.locator(".stepper").is_hidden())
    w.check("'until the next change' offered", choice.locator("button[data-hours='next']").count() == 1)
    choice.locator("button[data-hours='next']").click()
    w.card().locator("button[data-action=apply]").click()
    w.check("the board holds it off", w.wait(lambda: (house.held() or {"target": 0})["target"] is None))
    w.check("Off is the mode shown", w.wait(lambda: w.in_mode("off")))
    w.card().locator(".manual-banner button[data-action=resume]").click()
    w.check("the banner's way back", w.wait(lambda: house.held() is None))


def test_changed_again_while_held_opens_on_what_it_holds(walk, house):
    w = walk()
    w.mode("manual").click()
    w.card().locator(".pending button[data-action=duration][data-hours='2']").click()
    w.card().locator("button[data-action=apply]").click()
    assert w.wait(lambda: house.held() is not None) and w.wait(lambda: w.in_mode("manual"))
    first = house.held()["target"]
    w.mode("manual").click()
    w.check("Manual again: the temperature held", w.pending_value() == first)
    w.card().locator(".pending button[data-action=step][data-delta='-0.5']").click()
    w.card().locator(".pending button[data-action=duration][data-hours='4']").click()
    w.card().locator("button[data-action=apply]").click()
    w.check("half a degree down", w.wait(lambda: house.held()["target"] == first - 0.5))
    w.mode("off").click()
    w.card().locator("button[data-action=apply]").click()
    w.check("then off", w.wait(lambda: house.held()["target"] is None) and w.wait(lambda: w.in_mode("off")))


def test_the_temperature_stops_at_its_limits(walk):
    w = walk()
    w.mode("manual").click()
    plus = w.card().locator(".pending button[data-action=step][data-delta='0.5']")
    minus = w.card().locator(".pending button[data-action=step][data-delta='-0.5']")
    for _ in range(70):
        plus.click()
    w.check("+ seventy times: no higher than 35°", w.pending_value() <= 35)
    for _ in range(140):
        minus.click()
    w.check("- a hundred and forty times: no lower than 5°", w.pending_value() >= 5)
    w.card().locator("button[data-action=cancel-pending]").click()


# ------------------------------------------------------------- clumsy hands --

def test_apply_tapped_twice_makes_one_change_and_no_error(walk, house):
    w = walk()
    w.mode("manual").click()
    w.card().locator("button[data-action=apply]").dblclick()
    w.page.wait_for_timeout(1500)
    toasts = w.page.locator(".toast-host")
    said = toasts.inner_text().strip() if toasts.count() else ""
    w.check("one change, nothing said wrong", house.held() is not None and said == "")


def test_modes_mashed_then_cancel_change_nothing(walk, house):
    w = walk()
    for key in ("manual", "off", "schedule", "manual", "off", "manual", "schedule", "off", "manual"):
        w.mode(key).click()
    w.card().locator("button[data-action=cancel-pending]").click()
    w.page.wait_for_timeout(500)
    w.check("nothing held, hourly, no choice open", house.held() is None and w.in_mode("schedule")
            and w.card().locator(".pending").count() == 0)
    for _ in range(3):
        w.mode("schedule").click()
    w.check("Hourly tapped while hourly: nothing", house.held() is None)


def test_a_reload_half_way_through_a_choice_applies_nothing(walk, house):
    w = walk()
    w.mode("manual").click()
    w.card().locator(".pending button[data-action=step][data-delta='0.5']").click()
    w.page.reload()
    w.page.wait_for_selector(".zone .mode-row", timeout=30000)
    w.page.wait_for_timeout(800)
    w.check("nothing applied, hourly", house.held() is None and w.in_mode("schedule"))


def test_a_tap_and_a_second_tap_on_the_table_by_finger(walk, house):
    w = walk(touch=True)
    w.card().locator("button[data-action=show-plan]").tap()
    w.page.wait_for_timeout(800)
    before = house.week()
    cell = w.card().locator(".sched-cell").nth(40)
    cell.scroll_into_view_if_needed()
    cell.tap()
    w.page.wait_for_timeout(1500)
    after = house.week()
    w.check("a tap changes one hour", before != after)
    cell.tap()
    w.page.wait_for_timeout(1500)
    w.check("a second tap clears it", house.week() != after)


# ------------------------------------------------------------ the rest of it --

def test_away_and_back(walk, house):
    w = walk()
    w.page.click("#away-chip")
    w.page.locator("#sheet button[data-away='24']").click()
    w.check("away: the cards rest", w.wait(lambda: w.card().locator(".away-note").count() == 1))
    w.check("away: no mode to tap by mistake", w.card().locator("button[data-action^=mode]").count() == 0)
    w.card().locator(".away-note button[data-action=back]").click()
    w.check("back: the modes again", w.wait(lambda: w.card().locator(".mode-row").count() == 1)
            and house.call("/api/climate/zones")["away_until"] is None)


def test_details_charts_and_the_table_and_its_info(walk):
    w = walk()
    w.card().locator(".zone-more > summary").click()
    w.wait(lambda: w.card().get_attribute("data-more") == "open")
    runs = w.card().locator(".zone-runs")
    runs.locator("summary").click()
    runs.locator("button[data-range='30']").click()
    w.check("the hours over 30 days", runs.locator(".energy-bar").count() == 30)
    # The week's temperatures: there once the board has a day's range (not
    # on a board just started).
    temps = w.card().locator(".zone-temps")
    if temps.count():
        temps.locator("summary").click()
        days = temps.locator(".temp-bar:not([disabled])")
        if days.count():
            days.last.click()
            w.check("a day's lowest and highest on a tap", "min" in temps.locator(".energy-total").inner_text())
    w.page.reload()
    w.page.wait_for_selector(".zone .mode-row", timeout=30000)
    w.page.wait_for_timeout(800)
    w.check("the details remembered", w.card().locator(".zone-more").get_attribute("open") is not None)
    w.card().locator(".zone-more > summary").click()
    w.page.wait_for_timeout(300)
    w.card().locator("button[data-action=show-plan]").click()
    w.page.wait_for_timeout(800)
    w.check("'See the table' opens it", w.card().locator(".sched-grid").is_visible())
    w.card().locator("button[data-action=quick-info]").click()
    w.check("the modes' i", w.card().locator(".quick-hint").is_visible())
    w.card().locator("button[data-action=tier-info]").click()
    w.check("Comfort and Economy's i", w.card().locator(".sched-tier-note").is_visible())


@pytest.mark.parametrize("chip", ["#season-chip", "#health", "#away-chip"])
def test_each_sheet_closes_with_escape_and_with_a_tap_outside(walk, chip):
    w = walk()
    w.page.click(chip)
    w.page.wait_for_timeout(300)
    shown = w.page.locator("#sheet").is_visible()
    w.page.keyboard.press("Escape")
    w.page.wait_for_timeout(300)
    w.check("opened, and closed by Escape", shown and w.page.locator("#sheet").is_hidden())
    w.page.click(chip)
    w.page.wait_for_timeout(300)
    w.page.locator("#sheet-backdrop").click(position={"x": 10, "y": 10})
    w.page.wait_for_timeout(300)
    w.check("closed by a tap outside", w.page.locator("#sheet").is_hidden())


def test_the_vmc_its_speeds_a_boost_and_off_and_on(walk):
    w = walk()
    for name in ("Minimo", "Notte", "Giorno", "Alto", "Massimo"):
        preset = w.page.locator("#plant-body button[data-vmc]", has_text=name)
        preset.click()
        w.check(f"{name} picked", w.wait(lambda: preset.get_attribute("aria-pressed") == "true"))
    w.page.locator("#vmc-card button[data-vmc-boost='30']").click()
    cancel = w.page.locator("#vmc-card button[data-vmc-boost-cancel]")
    w.check("a boost, with the way to stop it", w.wait(lambda: cancel.count() == 1))
    cancel.click()
    power = w.page.locator("#plant-body button[data-vmc-power]")
    power.click()
    w.check("off", w.wait(lambda: power.get_attribute("aria-checked") == "false"))
    power.click()
    w.check("on again", w.wait(lambda: power.get_attribute("aria-checked") == "true"))


def test_a_light_switched_and_back(walk):
    w = walk()
    switch = w.page.locator("#house-body button[role=switch]").first
    was = switch.get_attribute("aria-checked")
    other = "false" if was == "true" else "true"
    switch.click()
    w.check("switched", w.wait(lambda: switch.get_attribute("aria-checked") == other))
    switch.click()
    w.check("and back", w.wait(lambda: switch.get_attribute("aria-checked") == was))


def test_manual_and_cancel_by_keyboard(walk):
    w = walk(width=1280)
    w.mode("manual").focus()
    w.page.keyboard.press("Enter")
    w.page.wait_for_timeout(300)
    w.check("Manual by Enter", w.card().locator(".pending").count() == 1)
    w.card().locator("button[data-action=cancel-pending]").focus()
    w.page.keyboard.press("Enter")
    w.page.wait_for_timeout(300)
    w.check("Cancel by Enter", w.card().locator(".pending").count() == 0)


# --------------------------------------------------- when things go wrong --

def test_the_board_gone_in_the_middle_of_a_change(walk, house):
    w = walk(network_errors_expected=True)
    w.mode("manual").click()
    w.page.route("**/api/**", lambda route: route.abort())
    w.card().locator("button[data-action=apply]").click()
    w.page.wait_for_timeout(1500)
    w.check("Apply with the board gone: the button free again",
            w.card().locator("button[data-action=apply]").get_attribute("aria-busy") is None)
    w.page.evaluate("refreshClimate()")
    w.check("the banner: the house isn't answering", w.wait(lambda: w.page.locator("#offline").is_visible()))
    w.page.unroute("**/api/**")
    w.check("the board back: the banner gone", w.wait(lambda: w.page.locator("#offline").is_hidden(), 9000))
    w.check("nothing half-applied", house.held() is None)


def test_a_slow_board_keeps_apply_waiting_then_takes_the_change(walk, house):
    w = walk()
    # The change's request held 4 s in the page, as a slow board would.
    w.page.evaluate("""() => { const real = window.fetch;
      window.fetch = (url, options) => String(url).endsWith('/override')
        ? new Promise(resolve => setTimeout(() => resolve(real(url, options)), 4000)) : real(url, options); }""")
    w.mode("manual").click()
    apply = w.card().locator("button[data-action=apply]")
    apply.click()
    w.page.wait_for_timeout(500)
    w.check("Apply waits, said as busy", apply.get_attribute("aria-busy") == "true", busy_ok=True)
    w.check("done in the end", w.wait(lambda: house.held() is not None, 10000) and w.wait(lambda: w.in_mode("manual")))


def test_two_phones_agree_with_the_board(walk, house):
    a, b = walk(), walk()
    a.mode("manual").click()
    a.card().locator("button[data-action=apply]").click()
    assert a.wait(lambda: house.held() is not None)
    b.page.evaluate("refreshClimate()")
    b.check("the other phone sees manual", b.wait(lambda: b.in_mode("manual")))
    b.mode("schedule").click()
    assert b.wait(lambda: house.held() is None)
    a.page.evaluate("refreshClimate()")
    a.check("and the first hourly again", a.wait(lambda: a.in_mode("schedule")))
    # Both choosing at once: the last Apply wins, and both say which.
    a.mode("manual").click()
    b.mode("off").click()
    a.card().locator("button[data-action=apply]").click()
    b.card().locator("button[data-action=apply]").click()
    a.page.wait_for_timeout(1500)
    for w in (a, b):
        w.page.evaluate("refreshClimate()")
    a.page.wait_for_timeout(800)
    off = house.held()["target"] is None
    a.check("both phones show the board's", a.in_mode("off") == off and b.in_mode("off") == off)


def test_back_to_the_tab_shows_a_change_made_elsewhere(walk, house):
    w = walk()
    house.call(f"/api/climate/zones/{urllib.parse.quote(ROOMS)}/override", {"target": 26.0, "hours": 2})
    w.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    w.check("the change shows", w.wait(lambda: w.in_mode("manual")))
