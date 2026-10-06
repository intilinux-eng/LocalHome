"""The dehumidifier, switched on by hand for a while (services/
dehumidifier.py): never on by itself; off when its time is up or the
reference room is dry enough; not started with the air dry enough."""
from __future__ import annotations

import pytest

from localhome.errors import InputError
from localhome.services.dehumidifier import START_FROM_PCT, STOP_BELOW_PCT, DehumidifierTimer


class Manager:
    def __init__(self, humidity=55.0, on=False):
        self.readings = {"Stanze Sensor": {"ok": True, "humidity_pct": humidity},
                         "Deumidificatore": {"ok": True, "is_on": on}}

    def sensor_reading(self, name):
        return self.readings.get(name)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _timer(tmp_path, manager, clock, allowed=lambda: True):
    commands = []

    def command(name, on):
        commands.append((name, on))
        manager.readings[name]["is_on"] = on
    timer = DehumidifierTimer(manager, "Deumidificatore", "Stanze Sensor", str(tmp_path / "dehumidifier.json"),
                              command=command, allowed=allowed, clock=clock, humidity_zone="Stanze")
    return timer, commands


def test_on_for_a_while_then_off_by_itself(tmp_path):
    manager, clock = Manager(), Clock()
    timer, commands = _timer(tmp_path, manager, clock)
    status = timer.switch_on(2)
    assert commands == [("Deumidificatore", True)] and status["until"] == clock.t + 7200 and status["on"] is True
    clock.t += 7199
    timer.tick()
    assert commands == [("Deumidificatore", True)], "not before its time"
    clock.t += 2
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False)
    assert timer.status()["until"] is None and timer.status()["stopped"]["why"] == "time"


def test_off_sooner_when_the_room_is_dry_enough_even_if_switched_on_some_other_way(tmp_path):
    manager, clock = Manager(), Clock()
    timer, commands = _timer(tmp_path, manager, clock)
    timer.switch_on(4)
    manager.readings["Stanze Sensor"]["humidity_pct"] = STOP_BELOW_PCT - 0.5
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False) and timer.status()["stopped"]["why"] == "humidity"
    manager.readings["Deumidificatore"]["is_on"] = True           # from the Shelly app, no time set
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False), "dry enough: off whoever switched it on"


def test_not_started_with_the_air_dry_enough(tmp_path):
    manager, clock = Manager(humidity=START_FROM_PCT - 1), Clock()
    timer, commands = _timer(tmp_path, manager, clock)
    with pytest.raises(InputError) as refused:
        timer.switch_on(1)
    assert refused.value.code == "dehumidifier_humidity_low" and refused.value.params == {"value": 46, "min": 47}
    assert commands == []


def test_never_on_by_itself(tmp_path):
    manager, clock = Manager(humidity=90.0), Clock()
    timer, commands = _timer(tmp_path, manager, clock)
    for _ in range(10):
        clock.t += 600
        timer.tick()
    assert commands == []


def test_only_the_offered_durations(tmp_path):
    timer, _ = _timer(tmp_path, Manager(), Clock())
    for hours in (0, 3, 24, True, "2"):
        with pytest.raises(InputError):
            timer.switch_on(hours)


def test_switched_off_some_other_way_its_time_is_forgotten(tmp_path):
    manager, clock = Manager(), Clock()
    timer, commands = _timer(tmp_path, manager, clock)
    timer.switch_on(1)
    manager.readings["Deumidificatore"]["is_on"] = False
    timer.tick()
    assert timer.status()["until"] is None and commands == [("Deumidificatore", True)]


def test_out_of_its_season_it_isnt_switched_and_its_time_is_dropped(tmp_path):
    manager, clock = Manager(), Clock()
    season = {"manual": True}
    timer, commands = _timer(tmp_path, manager, clock, allowed=lambda: season["manual"])
    timer.switch_on(2)
    season["manual"] = False                                        # winter: the season has it now
    clock.t += 3 * 3600
    timer.tick()
    assert commands == [("Deumidificatore", True)] and timer.status()["until"] is None
    with pytest.raises(InputError) as refused:
        timer.switch_on(1)
    assert refused.value.code == "dehumidifier_not_now"


def test_its_time_outlives_a_restart(tmp_path):
    manager, clock = Manager(), Clock()
    timer, _ = _timer(tmp_path, manager, clock)
    timer.switch_on(1)
    again, commands = _timer(tmp_path, manager, clock)               # LocalHome restarted
    clock.t += 3601
    again.tick()
    assert commands == [("Deumidificatore", False)]
    assert again.status()["until_at"] is None
