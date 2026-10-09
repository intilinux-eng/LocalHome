"""The VMC's automatic adjustments: a shower in the bathroom, humid outdoor
air and night cooling in summer - off by default, each with its
thresholds; on top of the program and of a speed picked by hand, never
over a timed boost, never switching the unit on."""
from datetime import datetime, timedelta

import pytest

from localhome.errors import InputError
from localhome.services.vmc_auto import (
    DEFAULTS,
    Air,
    VmcAuto,
    VmcAutomation,
    VmcAutoSettingsStore,
    validate,
    zones_for,
)
from localhome.services.vmc_schedule import DAYS, VmcScheduleController, VmcScheduleStore

SPEEDS = {"night": 25, "day": 50, "boost": 100}
ZONES = [{"name": "Stanze", "sensor": "Stanze Sensor", "cool_enabled": True},
         {"name": "Bagni", "sensor": "Bagni Sensor", "cool_enabled": False}]
ALL_ON = {**DEFAULTS, "shower": True, "humid": True, "cooling": True}
TUESDAY = datetime(2026, 7, 14)     # a Tuesday in summer
MIN = 60


def at(hour, minute=0):
    return TUESDAY + timedelta(hours=hour, minutes=minute)


# ------------------------------------------------------------- settings --

def test_off_by_default_with_the_recommended_thresholds():
    assert not DEFAULTS["shower"] and not DEFAULTS["humid"] and not DEFAULTS["cooling"]
    assert (DEFAULTS["shower_rise"], DEFAULTS["shower_window_minutes"], DEFAULTS["shower_max_minutes"]) == (8, 10, 60)
    assert (DEFAULTS["quiet_from_hour"], DEFAULTS["quiet_to_hour"]) == (22, 7)
    assert DEFAULTS["cooling_outdoor_min_c"] == 16.0


@pytest.mark.parametrize("bad,code", [
    ({"shower": "yes"}, "request_invalid"),
    ({"shower_rise": 50}, "setting_range"),
    ({"shower_min_minutes": 40, "shower_max_minutes": 30}, "vmc_auto_shower_times"),
    ({"quiet_from_hour": 7, "quiet_to_hour": 7}, "vmc_auto_quiet_hours"),
    ({"bathroom_zone": ""}, "request_invalid"),
    ({"nonsense": 1}, "setting_unknown"),
])
def test_bad_settings_are_refused_naming_the_problem(bad, code):
    with pytest.raises(InputError) as refused:
        validate(bad)
    assert refused.value.code == code


def test_degrees_go_by_halves_and_only_changes_are_kept(tmp_path):
    store = VmcAutoSettingsStore(str(tmp_path / "vmc_auto.json"))
    assert store.get() == DEFAULTS and not (tmp_path / "vmc_auto.json").exists()
    saved = store.set({"shower": True, "cooling_from_c": 24.3})
    assert saved["shower"] is True and saved["cooling_from_c"] == 24.5
    assert (tmp_path / "vmc_auto.json").read_text(encoding="utf-8").count(":") == 2
    store.set(dict(DEFAULTS))
    assert not (tmp_path / "vmc_auto.json").exists()


def test_the_bathroom_and_the_reference_room_by_themselves_or_named():
    bathroom, reference = zones_for(DEFAULTS, ZONES)
    assert (bathroom["name"], reference["name"]) == ("Bagni", "Stanze")
    bathroom, reference = zones_for({**DEFAULTS, "bathroom_zone": "Stanze", "reference_zone": "Bagni"}, ZONES)
    assert (bathroom["name"], reference["name"]) == ("Stanze", "Bagni")


# --------------------------------------------------------------- shower --

def shower_run(humidities, settings=ALL_ON, step=MIN, season="heat"):
    """Each humidity a minute apart; what the adjustment is after each."""
    automation, out = VmcAutomation(), []
    for i, rh in enumerate(humidities):
        automation.observe(i * step, settings, season, Air(22, rh), None, None)
        out.append(automation.adjust(25, SPEEDS, quiet=False))
    return automation, out


def test_a_shower_s_fast_rise_runs_it_at_the_maximum():
    _, out = shower_run([55, 55, 58, 62, 66])
    assert out[:3] == [None, None, None]
    assert out[4].reason == "shower" and out[4].value == 100


def test_a_slow_rise_is_not_a_shower():
    # 8 points, but over 16 minutes: a humid day, not a shower
    _, out = shower_run([55 + i * 0.5 for i in range(17)])
    assert all(a is None for a in out)


def test_it_ends_back_near_where_it_started_but_not_before_its_shortest():
    rise = [55, 60, 66, 75, 80]
    _, out = shower_run(rise + [70, 60, 58])          # back within 5 points after 3 minutes
    assert out[-1] is not None, "at least 15 minutes"
    _, out = shower_run(rise + [70] * 5 + [60] * 10)
    assert out[-1] is None


def test_it_ends_at_its_longest_even_with_the_air_still_humid():
    _, out = shower_run([55, 66] + [85] * 62)
    assert out[60] is not None and out[62] is None


def test_in_the_quiet_hours_the_day_speed_not_the_maximum():
    automation, _ = shower_run([55, 66])
    assert automation.adjust(25, SPEEDS, quiet=True).value == 50


def test_off_or_already_fast_it_s_left_alone():
    automation, _ = shower_run([55, 66])
    assert automation.adjust(0, SPEEDS, quiet=False) is None, "never switched on"
    assert automation.adjust(None, SPEEDS, quiet=False) is None
    assert automation.adjust(100, SPEEDS, quiet=False) is None


def test_switched_off_in_the_settings_nothing_happens():
    _, out = shower_run([55, 66, 80], settings=DEFAULTS)
    assert out == [None, None, None]


# --------------------------------------------------------------- summer --

def summer(indoor, outdoor, automation=None, settings=ALL_ON, season="cool"):
    automation = automation or VmcAutomation()
    automation.observe(0, settings, season, None, Air(*indoor), Air(*outdoor))
    return automation


def test_more_humid_outside_the_night_speed():
    # inside 26° 50% (dew ~15°), outside 28° 60% (dew ~19.5°)
    automation = summer((26, 50), (28, 60))
    assert automation.humid and automation.adjust(50, SPEEDS, quiet=False).value == 25
    assert automation.adjust(25, SPEEDS, quiet=False) is None, "already at night"


def test_humid_outside_has_a_margin_to_start_and_stops_at_the_indoor_dew_point():
    inside = Air(26, 50).dew_point_c
    automation = VmcAutomation()
    close = (26, 52.5)   # a dew point about half a degree over the indoor one
    assert Air(*close).dew_point_c - inside < 1
    summer((26, 50), close, automation)
    assert not automation.humid
    summer((26, 50), (28, 60), automation)
    assert automation.humid
    summer((26, 50), close, automation)
    assert automation.humid, "under the margin, still over the indoor dew point: stays"
    summer((26, 50), (26, 48), automation)
    assert not automation.humid


def test_only_in_summer():
    assert not summer((26, 50), (28, 60), season="heat").humid
    assert not summer((27, 50), (20, 50), season="heat").cooling


def test_night_cooling_the_day_speed_when_the_house_is_warm_and_the_air_cool_and_dry():
    automation = summer((26, 50), (20, 50))
    assert automation.cooling and not automation.humid
    assert automation.adjust(25, SPEEDS, quiet=True).value == 50
    assert automation.adjust(100, SPEEDS, quiet=True) is None


@pytest.mark.parametrize("indoor,outdoor,why", [
    ((23.5, 50), (18, 50), "the house isn't warm"),
    ((26, 50), (24, 40), "not cool enough outside"),
    ((26, 50), (15, 60), "under the unit's bypass floor"),
    ((26, 40), (21, 80), "the outdoor air more humid"),
])
def test_no_night_cooling(indoor, outdoor, why):
    assert not summer(indoor, outdoor).cooling, why


def test_night_cooling_stops_a_little_past_where_it_started():
    automation = summer((26, 50), (20, 50))
    summer((23.8, 50), (20, 50), automation)
    assert automation.cooling, "under 24 but not by half a degree"
    summer((23.4, 50), (20, 50), automation)
    assert not automation.cooling


def test_the_shower_comes_first():
    automation = summer((26, 50), (28, 60))
    automation.observe(60, ALL_ON, "cool", Air(24, 55), Air(26, 50), Air(28, 60))
    automation.observe(120, ALL_ON, "cool", Air(24, 70), Air(26, 50), Air(28, 60))
    assert automation.adjust(50, SPEEDS, quiet=False).reason == "shower"


def test_a_speed_picked_by_hand_holds_it_back_until_its_reason_passes():
    automation = summer((26, 50), (28, 60))
    automation.dismiss()
    assert automation.adjust(50, SPEEDS, quiet=False) is None
    summer((26, 50), (26, 48), automation)        # the outdoor air dries
    summer((26, 50), (28, 60), automation)        # and is humid again: a new reason
    assert automation.adjust(50, SPEEDS, quiet=False).value == 25


# ----------------------------------------------------------- controller --

class FakeNumber:
    def __init__(self):
        self.values = []

    def set_value(self, value):
        self.values.append(value)


class FakeSwitch:
    def __init__(self):
        self.calls = []

    def turn_on(self):
        self.calls.append("on")

    def turn_off(self):
        self.calls.append("off")


class FakePoller:
    def __init__(self, reading=None):
        self.reading = dict(reading or {})
        self.cache = self

    def merge(self, data):
        self.reading.update(data)

    def get(self):
        return dict(self.reading)


class FakeManager:
    def __init__(self):
        self.numbers = {"VMC Speed": FakeNumber()}
        self.switches = {"VMC": FakeSwitch()}
        self.paired_switch = {"VMC Speed": "VMC"}
        self.pollers = {"VMC Speed": FakePoller({"value": 25}), "VMC": FakePoller({"is_on": True}),
                        "Bagni Sensor": FakePoller(), "Stanze Sensor": FakePoller(), "Outdoor Sensor": FakePoller()}

    def sensor_reading(self, name):
        poller = self.pollers.get(name)
        return poller.get() if poller else {"ok": False}

    def air(self, name, t, rh):
        self.pollers[name].reading = {"ok": True, "temperature_c": t, "humidity_pct": rh}

    @property
    def written(self):
        return self.numbers["VMC Speed"].values


def controller(tmp_path, settings=ALL_ON, program=None, season="heat"):
    manager = FakeManager()
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    if program:
        store.set({"enabled": True, "target": "VMC Speed", "week": {d: list(program) for d in DAYS}})
    auto_store = VmcAutoSettingsStore(str(tmp_path / "vmc_auto.json"))
    auto_store.set(settings)
    auto = VmcAuto(manager, auto_store, season=lambda: season, zones=lambda: ZONES, outdoor_sensor="Outdoor Sensor",
                   device=lambda: "VMC Speed", speeds=lambda device: SPEEDS)
    manager.air("Bagni Sensor", 22, 55)
    manager.air("Stanze Sensor", 21, 50)
    manager.air("Outdoor Sensor", 12, 70)
    return VmcScheduleController(manager, store, auto=auto), manager


def shower(control, manager, start, humidities):
    for i, rh in enumerate(humidities):
        manager.air("Bagni Sensor", 22, rh)
        control.tick(now=start + timedelta(minutes=i))


def test_a_shower_and_back_with_no_program(tmp_path):
    control, manager = controller(tmp_path)
    shower(control, manager, at(18), [55, 55, 66])
    assert manager.written == [100]
    assert control.status(now=at(18, 3))["auto"] == {"reason": "shower", "value": 100, "humidity": 66,
                                                       "baseline": 55, "zone": "Bagni"}
    shower(control, manager, at(18, 3), [80] * 10 + [58] * 6)
    assert manager.written == [100, 25], "back to the speed it had"
    assert "auto" not in control.status(now=at(18, 30))


def test_with_a_program_back_to_its_speed_and_its_next_stretch_adjusted(tmp_path):
    hours = [25] * 7 + [50] * 15 + [25] * 2
    control, manager = controller(tmp_path, program=hours, season="cool")
    control.tick(now=at(21, 50))
    assert manager.written == [50]
    manager.air("Stanze Sensor", 26, 50)
    manager.air("Outdoor Sensor", 20, 50)     # cool and dry: night cooling - already at day speed
    control.tick(now=at(21, 55))
    assert manager.written == [50]
    control.tick(now=at(22, 1))               # the program's night: kept at the day speed
    assert manager.written == [50, 50] and control.status(now=at(22, 1))["auto"]["reason"] == "cooling"
    manager.air("Outdoor Sensor", 25, 50)     # no longer cool outside
    control.tick(now=at(22, 30))
    assert manager.written == [50, 50, 25]


def test_a_speed_picked_by_hand_during_a_shower_stands(tmp_path):
    control, manager = controller(tmp_path)
    shower(control, manager, at(18), [55, 66])
    manager.pollers["VMC Speed"].reading["value"] = 50       # the home page's Day, five minutes on
    shower(control, manager, at(18, 5), [80, 80, 80])
    assert manager.written == [100]
    shower(control, manager, at(18, 30), [57] * 3 + [55, 70])  # over; then a new shower
    assert manager.written == [100, 100]


def test_never_switched_on(tmp_path):
    control, manager = controller(tmp_path)
    manager.pollers["VMC"].reading["is_on"] = False
    shower(control, manager, at(18), [55, 66, 80])
    assert manager.written == [] and manager.switches["VMC"].calls == []


def test_a_timed_boost_wins_and_the_shower_is_seen_after_it(tmp_path):
    control, manager = controller(tmp_path)
    control.boost("VMC Speed", 50, 10, now=at(18))
    shower(control, manager, at(18, 1), [55, 66, 80])
    assert manager.written == [50]
    shower(control, manager, at(18, 11), [80])
    assert manager.written[-1] == 100


def test_all_off_it_s_the_controller_it_was(tmp_path):
    control, manager = controller(tmp_path, settings=DEFAULTS, season="cool")
    manager.air("Stanze Sensor", 26, 50)
    manager.air("Outdoor Sensor", 28, 70)
    shower(control, manager, at(18), [55, 66, 80])
    assert manager.written == [] and "auto" not in control.status(now=at(18, 3))
