"""The VMC's weekly speed program: the week and its older day/night form,
when the speed changes, a controller that writes the speed when the
program changes it and then leaves it alone (so a speed picked by hand
survives until the next change), and the timed boost."""
from datetime import datetime, timedelta

import pytest

from localhome.services.vmc_schedule import (
    DAYS,
    DEFAULT_SCHEDULE,
    VmcScheduleController,
    VmcScheduleStore,
    day_night_week,
    next_change,
    speed_at,
    stretch_start,
    validate,
)

SCHEDULE = {"enabled": True, "target": "VMC Speed",
            "day": {"at": "07:00", "value": 50}, "night": {"at": "22:00", "value": 20}}
TUESDAY = datetime(2026, 9, 22)   # a Tuesday


def at(hour, minute=0, days=0):
    return TUESDAY + timedelta(days=days, hours=hour, minutes=minute)


def week_of(**overrides):
    """Night (20) all week, with day stretches given as day=[(from, to, speed)]."""
    week = {d: [20] * 24 for d in DAYS}
    for day, stretches in overrides.items():
        for start, end, value in stretches:
            week[day][start:end] = [value] * (end - start)
    return week


class FakeNumber:
    def __init__(self):
        self.values = []

    def set_value(self, value):
        self.values.append(value)


class FakeSwitch:
    def __init__(self, on=True):
        self.on = on
        self.calls = []

    def turn_on(self):
        self.on = True
        self.calls.append("on")

    def turn_off(self):
        self.on = False
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
    def __init__(self, numbers, switches=None, paired=None):
        self.numbers = numbers
        self.switches = switches or {}
        self.paired_switch = paired or {}
        self.pollers = {name: FakePoller() for name in [*numbers, *self.switches]}


# --------------------------------------------------------------- the week --

def test_day_and_night_is_a_week_to_the_hour():
    week = day_night_week(SCHEDULE["day"], SCHEDULE["night"])
    assert week["mon"][:8] == [20] * 7 + [50] and week["mon"][21:] == [50, 20, 20]
    assert all(week[d] == week["mon"] for d in DAYS)
    # night before day on the clock: night 01:00, day 09:00
    wrapped = day_night_week({"at": "09:00", "value": 50}, {"at": "01:00", "value": 20})
    assert wrapped["mon"][0] == 50 and wrapped["mon"][1] == 20 and wrapped["mon"][9] == 50


@pytest.mark.parametrize("when,expected", [
    (at(7), 50), (at(12, 30), 50), (at(21, 59), 50), (at(22), 20), (at(3), 20), (at(6, 59), 20),
])
def test_the_speed_at_a_moment(when, expected):
    assert speed_at(day_night_week(SCHEDULE["day"], SCHEDULE["night"]), when) == expected


def test_when_the_speed_next_changes_and_when_it_began():
    week = day_night_week(SCHEDULE["day"], SCHEDULE["night"])
    assert next_change(week, at(12)) == (at(22), 20)
    assert next_change(week, at(23)) == (at(7, days=1), 50)
    assert stretch_start(week, at(12, 30)) == at(7)
    assert stretch_start(week, at(3)) == at(22, days=-1)
    flat = {d: [50] * 24 for d in DAYS}
    assert next_change(flat, at(12)) is None and stretch_start(flat, at(12)) is None


def test_validate_rejects_bad_values():
    for bad, message in [
        ({**SCHEDULE, "day": {"at": "7", "value": 50}}, "07:00"),
        ({**SCHEDULE, "night": {"at": "22:00", "value": 120}}, "between 0 and 100"),
        ({**SCHEDULE, "night": {"at": "07:30", "value": 20}}, "different hours"),
        ({"enabled": False, "week": {"mon": [20] * 24}}, "mon..sun"),
        ({"enabled": False, "week": {**week_of(), "fri": [20] * 23}}, "24 hourly"),
        ({"enabled": False, "week": {**week_of(), "fri": [20] * 23 + [101]}}, "between 0 and 100"),
        ({"enabled": True, "target": None}, "choose which device"),
    ]:
        with pytest.raises(ValueError, match=message):
            validate(bad)


# -------------------------------------------------------------- the store --

def test_store_round_trip_and_defaults(tmp_path):
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    assert store.get() == DEFAULT_SCHEDULE

    store.set({"enabled": True, "target": "VMC Speed", "week": week_of(mon=[(7, 22, 50.4)])})

    assert store.get()["week"]["mon"][7] == 50  # a speed is a whole percent
    assert store.get()["enabled"] is True and store.get()["override"] is None


def test_a_saved_day_and_night_program_is_read_as_its_week(tmp_path):
    """The file a board has from before the week: it keeps running as it did."""
    path = tmp_path / "vmc_schedule.json"
    path.write_text('{"enabled": true, "target": "VMC Speed", "day": {"at": "07:00", "value": 50},'
                    ' "night": {"at": "22:00", "value": 20}}', encoding="utf-8")
    week = VmcScheduleStore(str(path)).get()["week"]
    assert week == day_night_week(SCHEDULE["day"], SCHEDULE["night"])


def test_an_update_changes_only_what_it_names(tmp_path):
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    custom = week_of(sat=[(8, 23, 50)], sun=[(8, 23, 50)])
    store.update({"week": custom})
    store.update({"enabled": True, "target": "VMC Speed"})
    assert store.get()["week"] == custom, "turning it on keeps the week"


def test_an_unreadable_file_falls_back_to_the_defaults(tmp_path):
    path = tmp_path / "vmc_schedule.json"
    path.write_text("{ not json", encoding="utf-8")

    assert VmcScheduleStore(str(path)).get() == DEFAULT_SCHEDULE


# --------------------------------------------------------- the controller --

def _controller(tmp_path, schedule=SCHEDULE, switch_on=True):
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    store.set(schedule)
    number, switch = FakeNumber(), FakeSwitch(on=switch_on)
    manager = FakeManager({"VMC Speed": number}, {"VMC": switch}, {"VMC Speed": "VMC"})
    manager.pollers["VMC"].reading["is_on"] = switch_on
    return VmcScheduleController(manager, store), number, switch


def test_the_speed_is_set_once_per_stretch_not_every_tick(tmp_path):
    controller, number, _ = _controller(tmp_path)

    controller.tick(now=at(12))
    controller.tick(now=at(13))  # same stretch: left alone
    controller.tick(now=at(22, 1))  # night began

    assert number.values == [50, 20]


def test_a_speed_picked_by_hand_survives_until_the_next_change(tmp_path):
    controller, number, _ = _controller(tmp_path)
    controller.tick(now=at(8))
    number.values.append("boost by hand")  # the dashboard's own button

    controller.tick(now=at(9))

    assert number.values == [50, "boost by hand"]


def test_every_change_of_a_painted_week_is_applied(tmp_path):
    week = week_of(tue=[(7, 8, 100), (8, 19, 50), (19, 20, 100), (20, 22, 50)])
    controller, number, _ = _controller(tmp_path, {"enabled": True, "target": "VMC Speed", "week": week})
    for hour in (6, 7, 8, 12, 19, 20, 22, 23):
        controller.tick(now=at(hour, 5))
    assert number.values == [20, 100, 50, 100, 50, 20]


def test_an_off_hour_switches_the_unit_off_and_the_next_speed_back_on(tmp_path):
    week = week_of(tue=[(9, 17, 0)])
    controller, number, switch = _controller(tmp_path, {"enabled": True, "target": "VMC Speed", "week": week})
    controller.tick(now=at(8))
    controller.tick(now=at(9))
    assert switch.calls == ["off"] and switch.on is False
    controller.tick(now=at(17))
    assert number.values == [20, 20] and switch.calls == ["off", "on"]


def test_turning_the_program_off_stops_touching_the_device(tmp_path):
    controller, number, _ = _controller(tmp_path)
    controller.tick(now=at(12))
    controller.store.set({**SCHEDULE, "enabled": False})

    controller.tick(now=at(22, 1))

    assert number.values == [50]


def test_a_missing_device_is_reported_not_raised(tmp_path):
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    store.set({**SCHEDULE, "target": "Nowhere"})
    controller = VmcScheduleController(FakeManager({}), store)

    controller.tick(now=at(12))  # logged, and the loop carries on


def test_status_for_the_dashboard(tmp_path):
    controller, _, _ = _controller(tmp_path)

    assert controller.status(now=at(12)) == {
        "enabled": True, "target": "VMC Speed", "value": 50, "until": "22:00",
        "until_at": "2026-09-22T22:00", "next_value": 20,
    }

    controller.store.set({**SCHEDULE, "enabled": False})
    assert controller.status(now=at(12)) == {"enabled": False}


# --------------------------------------------------------------- the boost --

def test_a_timed_boost_runs_then_the_program_takes_over_again(tmp_path):
    controller, number, _ = _controller(tmp_path)
    controller.tick(now=at(12))
    controller.boost("VMC Speed", 100, 30, now=at(12, 10))
    assert number.values == [50, 100]
    assert controller.status(now=at(12, 20))["override"]["until_hhmm"] == "12:40"

    controller.tick(now=at(12, 30))     # still on
    controller.tick(now=at(12, 41))     # over: the program's speed again
    assert number.values == [50, 100, 50]
    assert controller.store.get()["override"] is None


def test_with_the_program_off_a_boost_ends_where_it_started(tmp_path):
    controller, number, switch = _controller(tmp_path, {**SCHEDULE, "enabled": False})
    controller.manager.pollers["VMC Speed"].reading["value"] = 20
    controller.boost("VMC Speed", 100, 60, now=at(12))
    controller.tick(now=at(13, 1))
    assert number.values == [100, 20]


def test_a_boost_from_off_ends_off(tmp_path):
    controller, number, switch = _controller(tmp_path, {**SCHEDULE, "enabled": False}, switch_on=False)
    controller.boost("VMC Speed", 100, 30, now=at(12))
    assert switch.on is True
    controller.tick(now=at(12, 31))
    assert switch.on is False


def test_a_boost_can_be_cancelled(tmp_path):
    controller, number, _ = _controller(tmp_path)
    controller.boost("VMC Speed", 100, 120, now=at(12))
    controller.cancel_boost(now=at(12, 5))
    assert number.values == [100, 50]
    assert "override" not in controller.status(now=at(12, 6))


def test_a_boost_needs_a_known_device_and_a_sensible_length(tmp_path):
    controller, _, _ = _controller(tmp_path)
    for device, minutes in (("Nowhere", 30), ("VMC Speed", 0), ("VMC Speed", 24 * 60 + 1), ("VMC Speed", "30")):
        with pytest.raises(ValueError):
            controller.boost(device, 100, minutes, now=at(12))
