"""The VMC's day/night speed program: which period a moment falls in, and
a controller that writes the speed at a period change and then leaves it
alone (so a speed picked by hand survives until the next change)."""
from datetime import datetime

import pytest

from localhome.services.vmc_schedule import (
    DEFAULT_SCHEDULE,
    VmcScheduleController,
    VmcScheduleStore,
    current_period,
    next_change,
    validate,
)

SCHEDULE = {"enabled": True, "target": "VMC Speed",
            "day": {"at": "07:00", "value": 50}, "night": {"at": "22:00", "value": 20}}


class FakeNumber:
    def __init__(self):
        self.values = []

    def set_value(self, value):
        self.values.append(value)


class FakePoller:
    class cache:
        merged = []

        @classmethod
        def merge(cls, data):
            cls.merged.append(data)


class FakeManager:
    def __init__(self, numbers):
        self.numbers = numbers
        self.pollers = {name: FakePoller() for name in numbers}


@pytest.mark.parametrize("when,expected", [
    ("2026-09-22 07:00", "day"),
    ("2026-09-22 12:30", "day"),
    ("2026-09-22 21:59", "day"),
    ("2026-09-22 22:00", "night"),
    ("2026-09-22 03:00", "night"),
    ("2026-09-22 06:59", "night"),
])
def test_which_period_a_moment_falls_in(when, expected):
    assert current_period(SCHEDULE, datetime.fromisoformat(when)) == expected


def test_periods_can_wrap_the_other_way():
    # Night before day on the clock: night 01:00, day 09:00.
    schedule = {**SCHEDULE, "day": {"at": "09:00", "value": 50}, "night": {"at": "01:00", "value": 20}}
    assert current_period(schedule, datetime(2026, 9, 22, 2, 0)) == "night"
    assert current_period(schedule, datetime(2026, 9, 22, 10, 0)) == "day"
    assert current_period(schedule, datetime(2026, 9, 22, 23, 0)) == "day"


def test_when_the_current_period_ends():
    assert next_change(SCHEDULE, datetime(2026, 9, 22, 12, 0)) == datetime(2026, 9, 22, 22, 0)
    assert next_change(SCHEDULE, datetime(2026, 9, 22, 23, 0)) == datetime(2026, 9, 23, 7, 0)


def test_validate_rejects_bad_values():
    for bad, message in [
        ({**SCHEDULE, "day": {"at": "7", "value": 50}}, "07:00"),
        ({**SCHEDULE, "night": {"at": "22:00", "value": 120}}, "between 0 and 100"),
        ({**SCHEDULE, "night": {"at": "07:00", "value": 20}}, "different times"),
        ({"enabled": True, "target": None}, "choose which device"),
    ]:
        with pytest.raises(ValueError, match=message):
            validate(bad)


def test_store_round_trip_and_defaults(tmp_path):
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    assert store.get() == DEFAULT_SCHEDULE

    store.set({**SCHEDULE, "night": {"at": "23:30", "value": 20.4}})

    assert store.get()["night"] == {"at": "23:30", "value": 20}  # a speed is a whole percent
    assert store.get()["enabled"] is True


def test_an_unreadable_file_falls_back_to_the_defaults(tmp_path):
    path = tmp_path / "vmc_schedule.json"
    path.write_text("{ not json", encoding="utf-8")

    assert VmcScheduleStore(str(path)).get() == DEFAULT_SCHEDULE


def _controller(tmp_path, schedule=SCHEDULE):
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    store.set(schedule)
    number = FakeNumber()
    return VmcScheduleController(FakeManager({"VMC Speed": number}), store), number


def test_the_speed_is_set_once_per_period_not_every_tick(tmp_path):
    controller, number = _controller(tmp_path)

    controller.tick(now=datetime(2026, 9, 22, 12, 0))
    controller.tick(now=datetime(2026, 9, 22, 13, 0))  # same period: left alone
    controller.tick(now=datetime(2026, 9, 22, 22, 1))  # night began

    assert number.values == [50, 20]


def test_a_speed_picked_by_hand_survives_until_the_next_period(tmp_path):
    controller, number = _controller(tmp_path)
    controller.tick(now=datetime(2026, 9, 22, 8, 0))
    number.values.append("boost by hand")  # the dashboard's own button

    controller.tick(now=datetime(2026, 9, 22, 9, 0))

    assert number.values == [50, "boost by hand"]


def test_turning_the_program_off_stops_touching_the_device(tmp_path):
    controller, number = _controller(tmp_path)
    controller.tick(now=datetime(2026, 9, 22, 12, 0))
    controller.store.set({**SCHEDULE, "enabled": False})

    controller.tick(now=datetime(2026, 9, 22, 22, 1))

    assert number.values == [50]


def test_a_missing_device_is_reported_not_raised(tmp_path):
    store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
    store.set({**SCHEDULE, "target": "Nowhere"})
    controller = VmcScheduleController(FakeManager({}), store)

    controller.tick(now=datetime(2026, 9, 22, 12, 0))  # logged, and the loop carries on


def test_status_for_the_dashboard(tmp_path):
    controller, _ = _controller(tmp_path)

    assert controller.status(now=datetime(2026, 9, 22, 12, 0)) == {
        "enabled": True, "target": "VMC Speed", "period": "day", "value": 50, "until": "22:00",
    }

    controller.store.set({**SCHEDULE, "enabled": False})
    assert controller.status(now=datetime(2026, 9, 22, 12, 0)) == {"enabled": False}
