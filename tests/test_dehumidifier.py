"""The dehumidifier (services/dehumidifier.py): switched on from the
home page for a while or until the room is dry enough; by itself only if
the admin panel says so; off when its time is up or the reference room is
dry enough; not started with the air dry enough. Its thresholds and ways
to switch it on are the panel's, effective at once."""
from __future__ import annotations

import pytest

from localhome.errors import InputError
from datetime import datetime

from localhome.services.dehumidifier import (DEFAULTS, START_FROM_PCT, STOP_BELOW_PCT, DehumidifierSettingsStore,
                                             DehumidifierTimer, in_window, validate)


class Manager:
    def __init__(self, humidity=55.0, on=False):
        self.readings = {"Stanze Sensor": {"ok": True, "humidity_pct": humidity},
                         "Deumidificatore": {"ok": True, "is_on": on}}

    def sensor_reading(self, name):
        return self.readings.get(name)


class Clock:
    def __init__(self, hour=12):
        self.t = datetime(2026, 7, 15, hour, 0).timestamp()

    def __call__(self):
        return self.t


def _timer(tmp_path, manager, clock, allowed=lambda: True, settings=None):
    commands = []

    def command(name, on):
        commands.append((name, on))
        manager.readings[name]["is_on"] = on
    store = DehumidifierSettingsStore(str(tmp_path / "dehumidifier_settings.json"))
    if settings:
        store.set(settings)
    timer = DehumidifierTimer(manager, "Deumidificatore", "Stanze Sensor", str(tmp_path / "dehumidifier.json"),
                              command=command, allowed=allowed, clock=clock, humidity_zone="Stanze",
                              settings=store.get)
    timer.settings_store = store
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
    assert refused.value.code == "dehumidifier_humidity_low" and refused.value.params == {"value": 54, "min": 55}
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


def test_on_without_being_asked_here_it_is_switched_off(tmp_path):
    """It draws a lot of power: switched on from outside - the Shelly app,
    a test, the diagnostic mode left behind - with the air still humid,
    it's off at the next check, not left running until the room dries.
    Asked for here, it runs its time."""
    manager, clock = Manager(humidity=70.0), Clock()
    diagnostic = {"on": True}
    timer, commands = _timer(tmp_path, manager, clock, allowed=lambda: not diagnostic["on"])
    manager.readings["Deumidificatore"]["is_on"] = True              # switched on by the installer
    timer.tick()
    assert commands == [], "the diagnostic mode: the installer's"
    diagnostic["on"] = False
    timer.tick()
    assert commands == [("Deumidificatore", False)] and timer.status()["stopped"]["why"] == "unasked"
    timer.switch_on(1)
    clock.t += 1800
    timer.tick()
    assert commands[-1] == ("Deumidificatore", True), "asked for: on for its hour"


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


# -------------------------------------------------------------- settings --

def test_the_standard_thresholds_are_50_and_55():
    assert (DEFAULTS["stop_below"], DEFAULTS["start_from"]) == (50, 55) == (STOP_BELOW_PCT, START_FROM_PCT)
    assert DEFAULTS["auto"] is False, "never by itself unless the panel says so"


def test_settings_are_checked_together():
    for bad, code in (({"start_from": 51}, "dehumidifier_start_above_stop"),
                      ({"durations": [], "until_target": False}, "dehumidifier_no_way_on"),
                      ({"durations": [3]}, "dehumidifier_durations_invalid"),
                      ({"stop_below": 90}, "setting_range"),
                      ({"auto_from_hour": 9, "auto_to_hour": 9}, "dehumidifier_auto_window"),
                      ({"auto": "yes"}, "request_invalid")):
        with pytest.raises(InputError) as refused:
            validate(bad)
        assert refused.value.code == code, bad
    assert validate({"auto_from_hour": 0, "auto_to_hour": 24})["auto_to_hour"] == 24, "the whole day"


def test_only_what_was_changed_is_kept(tmp_path):
    import json
    store = DehumidifierSettingsStore(str(tmp_path / "dehumidifier_settings.json"))
    assert store.get() == DEFAULTS
    store.set({"stop_below": 48})
    store.set({"durations": [0.5, 1]})
    assert store.get()["stop_below"] == 48 and store.get()["durations"] == [0.5, 1]
    saved = json.loads((tmp_path / "dehumidifier_settings.json").read_text(encoding="utf-8"))
    assert saved == {"stop_below": 48, "durations": [0.5, 1]}
    store.set(dict(DEFAULTS))
    assert not (tmp_path / "dehumidifier_settings.json").exists()


def test_a_threshold_set_in_the_panel_takes_effect_at_the_next_check(tmp_path):
    manager, clock = Manager(humidity=56.0), Clock()
    timer, commands = _timer(tmp_path, manager, clock)
    timer.switch_on(2)
    manager.readings["Stanze Sensor"]["humidity_pct"] = 52.0
    timer.tick()
    assert commands[-1] == ("Deumidificatore", True)
    timer.settings_store.set({"stop_below": 53, "start_from": 58})
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False) and timer.status()["stopped"]["why"] == "humidity"
    assert timer.status()["stop_below"] == 53


def test_the_offered_durations_are_the_panels(tmp_path):
    manager, clock = Manager(), Clock()
    timer, commands = _timer(tmp_path, manager, clock, settings={"durations": [0.5, 8]})
    with pytest.raises(InputError):
        timer.switch_on(2)
    assert timer.switch_on(0.5)["until"] == clock.t + 1800


# --------------------------------------------------------- until the target --

def test_until_the_target_runs_until_the_room_is_dry_enough(tmp_path):
    manager, clock = Manager(humidity=60.0), Clock()
    timer, commands = _timer(tmp_path, manager, clock)
    status = timer.switch_on(until="target")
    assert status["mode"] == "target" and status["until"] == clock.t + 8 * 3600, "at most 8 hours"
    clock.t += 3 * 3600
    timer.tick()
    assert commands[-1] == ("Deumidificatore", True), "still humid: still on"
    manager.readings["Stanze Sensor"]["humidity_pct"] = 49.5
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False) and timer.status()["stopped"]["why"] == "humidity"


def test_until_the_target_isnt_offered_when_the_panel_says_no(tmp_path):
    timer, _ = _timer(tmp_path, Manager(), Clock(), settings={"until_target": False})
    with pytest.raises(InputError) as refused:
        timer.switch_on(until="target")
    assert refused.value.code == "dehumidifier_hours_invalid"


# ------------------------------------------------------------- automatic --

AUTO = {"auto": True, "auto_from_hour": 8, "auto_to_hour": 22, "auto_max_hours_day": 3}


def test_automatic_it_switches_itself_on_when_humid_inside_its_hours(tmp_path):
    manager, clock = Manager(humidity=58.0), Clock(hour=10)
    timer, commands = _timer(tmp_path, manager, clock, settings=AUTO)
    timer.tick()
    assert commands == [("Deumidificatore", True)] and timer.status()["mode"] == "auto"
    assert timer.status()["until"] == clock.t + 3 * 3600, "no longer than what's left of its day"
    manager.readings["Stanze Sensor"]["humidity_pct"] = 49.0
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False), "dry enough: off"
    manager.readings["Stanze Sensor"]["humidity_pct"] = 53.0
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False), "between the thresholds: stays off"


def test_automatic_says_it_stops_at_the_end_of_its_hours(tmp_path):
    """Switched on by itself at 21:36 with its hours ending at 22: "at
    most until 22:00", not three hours past it."""
    manager = Manager(humidity=60.0)
    clock = Clock(hour=21)
    clock.t += 36 * 60
    timer, commands = _timer(tmp_path, manager, clock, settings=AUTO)
    timer.tick()
    assert commands == [("Deumidificatore", True)]
    assert timer.status()["until_at"].endswith("T22:00:00")


def test_automatic_not_outside_its_hours_and_off_at_their_end(tmp_path):
    manager, clock = Manager(humidity=60.0), Clock(hour=7)
    timer, commands = _timer(tmp_path, manager, clock, settings=AUTO)
    timer.tick()
    assert commands == [], "07:00, before 08"
    clock.t = datetime(2026, 7, 15, 21, 30).timestamp()
    timer.tick()
    assert commands == [("Deumidificatore", True)]
    clock.t = datetime(2026, 7, 15, 22, 1).timestamp()
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False) and timer.status()["stopped"]["why"] == "window"


def test_automatic_its_hours_a_day_are_a_ceiling(tmp_path):
    manager, clock = Manager(humidity=60.0), Clock(hour=9)
    timer, commands = _timer(tmp_path, manager, clock, settings=AUTO)
    timer.tick()
    clock.t += 3 * 3600 + 1
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False), "its 3 hours are used"
    clock.t += 600
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False), "not again today"
    assert timer.status()["auto"]["used_today_seconds"] >= 3 * 3600
    clock.t = datetime(2026, 7, 16, 9, 0).timestamp()
    timer.tick()
    assert commands[-1] == ("Deumidificatore", True), "a new day"


def test_automatic_switched_off_by_hand_waits_two_hours(tmp_path):
    manager, clock = Manager(humidity=60.0), Clock(hour=10)
    timer, commands = _timer(tmp_path, manager, clock, settings=AUTO)
    timer.tick()
    timer.switch_off()
    clock.t += 3600
    timer.tick()
    assert commands[-1] == ("Deumidificatore", False), "paused by the hand that switched it off"
    assert timer.status()["auto"]["paused_until_at"] is not None
    clock.t += 3601
    timer.tick()
    assert commands[-1] == ("Deumidificatore", True)


def test_automatic_never_out_of_its_season(tmp_path):
    manager, clock = Manager(humidity=70.0), Clock(hour=10)
    timer, commands = _timer(tmp_path, manager, clock, allowed=lambda: False, settings=AUTO)
    timer.tick()
    assert commands == []


def test_a_window_across_midnight():
    assert in_window(23, 22, 6) and in_window(3, 22, 6) and not in_window(12, 22, 6)
    assert in_window(0, 0, 24) and in_window(23, 0, 24)
    assert in_window(8, 8, 22) and not in_window(22, 8, 22)
