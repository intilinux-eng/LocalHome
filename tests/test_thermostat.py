import json
import time as tm
from datetime import datetime, timedelta

import pytest

from localhome.services.thermostat import ScheduleStore, ThermostatController, Zone


class FakeSwitch:
    def __init__(self):
        self.calls: list[str] = []

    def turn_on(self):
        self.calls.append("on")

    def turn_off(self):
        self.calls.append("off")


class FakeManager:
    def __init__(self, readings: dict[str, dict], switches: dict[str, FakeSwitch]):
        self._readings = readings
        self.switches = switches
        self.pollers = {}  # no cache.merge() targets needed for these tests

    def sensor_reading(self, name):
        return self._readings.get(name, {"ok": False})


# --------------------------------------------------------- ScheduleStore ----

def test_unset_hour_has_no_target(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    assert store.get_target("Rooms", datetime(2026, 9, 14, 10, 0)) is None  # a Monday


def test_set_and_get_week_round_trips(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    week = ScheduleStore.empty_week()
    week["mon"][10] = 21.5
    store.set_week("Rooms", week)

    assert store.get_target("Rooms", datetime(2026, 9, 14, 10, 0)) == 21.5  # Monday 10:00
    assert store.get_target("Rooms", datetime(2026, 9, 14, 11, 0)) is None  # Monday 11:00, untouched
    assert store.get_target("Bathrooms", datetime(2026, 9, 14, 10, 0)) is None  # different zone, own schedule


def test_set_week_rejects_wrong_length(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    bad_week = ScheduleStore.empty_week()
    bad_week["mon"] = [20.0] * 23  # not 24 hours
    with pytest.raises(ValueError):
        store.set_week("Rooms", bad_week)


def test_set_week_rejects_a_non_numeric_hour_value(tmp_path):
    # Found via UI stress-testing: an hour value that isn't a number or
    # null used to be accepted and persisted here, only to blow up later
    # in get_target()'s float(value) - from the thermostat's background
    # tick, hours after whoever sent it is gone, with no useful error.
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    bad_week = ScheduleStore.empty_week()
    bad_week["mon"][10] = "hot"
    with pytest.raises(ValueError, match="mon.*10"):
        store.set_week("Rooms", bad_week)


def test_get_week_sanitizes_a_non_numeric_value_already_on_disk(tmp_path):
    # Complements the get_target() test below: get_week() is what the
    # dashboard's schedule grid actually loads to edit - if get_week()
    # didn't clean this up, a single legacy bad cell would ride along on
    # every future save of that zone and get rejected every time, with no
    # way to fix it short of hand-editing the JSON file.
    path = tmp_path / "schedules.json"
    week = ScheduleStore.empty_week()
    week["mon"][10] = "hot"
    week["mon"][11] = 21.0  # a real neighbor - must survive untouched
    path.write_text(json.dumps({"mode": "heat", "zones": {"Rooms": week}}), encoding="utf-8")
    store = ScheduleStore(str(path))

    cleaned = store.get_week("Rooms")

    assert cleaned["mon"][10] is None
    assert cleaned["mon"][11] == 21.0


def test_get_target_degrades_gracefully_on_a_non_numeric_value_already_on_disk(tmp_path):
    # set_week() now rejects this on the way in, but get_target() is read
    # on every /api/climate/zones request and every tick - it must not
    # raise for a bad value that's already on disk (a hand-edited file,
    # or data written before that check existed), or the whole endpoint
    # (not just this one zone) breaks for as long as it sits there.
    path = tmp_path / "schedules.json"
    week = ScheduleStore.empty_week()
    week["mon"][10] = "hot"
    path.write_text(json.dumps({"mode": "heat", "zones": {"Rooms": week}}), encoding="utf-8")
    store = ScheduleStore(str(path))

    assert store.get_target("Rooms", datetime(2026, 9, 14, 10, 0)) is None


def test_mode_defaults_and_persists(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"), default_mode="heat")
    assert store.get_mode() == "heat"
    store.set_mode("cool")
    assert store.get_mode() == "cool"


def test_set_mode_rejects_unknown_value(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    with pytest.raises(ValueError):
        store.set_mode("frobnicate")


def test_away_until_defaults_to_none_and_round_trips(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    assert store.get_away_until() is None

    store.set_away_until(datetime(2026, 9, 15, 12, 0))
    assert store.get_away_until() == datetime(2026, 9, 15, 12, 0)

    store.set_away_until(None)
    assert store.get_away_until() is None


# --------------------------------------------------- ThermostatController ---

def _controller(tmp_path, hysteresis_c=0.3):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    manager = FakeManager(readings={}, switches={"Rooms Valve": FakeSwitch()})
    store = ScheduleStore(str(tmp_path / "schedules.json"))  # decide() never touches disk anyway
    return ThermostatController(manager, store, zones, hysteresis_c=hysteresis_c), manager


def test_the_clock_being_set_at_boot_isnt_a_stalled_loop(tmp_path, monkeypatch):
    """No clock battery: switched back on, the board starts on the last
    time it saved and jumps hours ahead once synchronized. The loop that
    just ticked isn't stalled for that - the service watchdog used to
    restart LocalHome for it on every power-up."""
    from localhome.services import system_status
    controller, _ = _controller(tmp_path)
    controller.tick()
    assert controller.last_tick_ok_ts is not None
    real_time = tm.time
    monkeypatch.setattr(tm, "time", lambda: real_time() + 6 * 3600)
    assert not system_status.loop_health("thermostat", controller.last_tick_ok_ts, 60)["stalled"]


def test_heat_mode_opens_below_target_minus_hysteresis(tmp_path):
    controller, _ = _controller(tmp_path, hysteresis_c=0.3)
    assert controller.decide("Rooms", current_c=19.5, target_c=20.0, mode="heat") is True


def test_heat_mode_closes_above_target_plus_hysteresis(tmp_path):
    controller, _ = _controller(tmp_path, hysteresis_c=0.3)
    assert controller.decide("Rooms", current_c=20.5, target_c=20.0, mode="heat") is False


def test_heat_mode_dead_band_keeps_previous_state(tmp_path):
    controller, _ = _controller(tmp_path, hysteresis_c=0.5)
    # Right at the setpoint - no previous state recorded yet, defaults closed.
    assert controller.decide("Rooms", current_c=20.0, target_c=20.0, mode="heat") is False
    controller._valve_open["Rooms"] = True
    # Same reading, but now it was already open - hysteresis dead band keeps it open.
    assert controller.decide("Rooms", current_c=20.0, target_c=20.0, mode="heat") is True


def test_cool_mode_is_the_mirror_image_of_heat_mode(tmp_path):
    controller, _ = _controller(tmp_path, hysteresis_c=0.3)
    assert controller.decide("Rooms", current_c=25.0, target_c=24.0, mode="cool") is True
    assert controller.decide("Rooms", current_c=23.5, target_c=24.0, mode="cool") is False


def test_no_target_means_valve_stays_closed_regardless_of_temperature(tmp_path):
    controller, _ = _controller(tmp_path)
    assert controller.decide("Rooms", current_c=10.0, target_c=None, mode="heat") is False


def test_tick_commands_the_valve_switch_when_state_changes(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 18.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24
    store.set_week("Rooms", week)

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 8, 0))  # Monday 08:00, target 22, current 18 -> should open

    assert switch.calls == ["on"]


def test_tick_does_not_recommand_an_already_correct_valve_state(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 18.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24
    store.set_week("Rooms", week)

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 8, 0))
    controller.tick(now=datetime(2026, 9, 14, 8, 1))  # still 18 vs 22 - already open, no repeat command

    assert switch.calls == ["on"]


def _cool_excluded_bathroom(tmp_path):
    zones = [Zone(name="Bathrooms", sensor="Bath Temp", valve="Bath Valve", cool_enabled=False)]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bath Temp": {"ok": True, "temperature_c": 30.0}}, switches={"Bath Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("cool")
    store.set_week("Bathrooms", _flat_week(22.0))  # 30C vs 22C would normally open it
    return ThermostatController(manager, store, zones, hysteresis_c=0.3), switch


def test_cool_disabled_zone_is_held_closed_while_cooling(tmp_path):
    # A bathroom's radiant panel would condense regardless of its own
    # temperature: never opened for it in cool mode, and actively closed -
    # a valve left open from the heating season must not stay open.
    controller, switch = _cool_excluded_bathroom(tmp_path)

    controller.tick(now=datetime(2026, 9, 14, 14, 0))

    assert switch.calls == ["off"]


def test_cool_disabled_valve_owned_by_an_interlock_is_left_to_it(tmp_path):
    # A water-based dehumidifier fed through this valve: the interlock opens
    # it while the dehumidifier runs and closes it otherwise - the thermostat
    # mustn't fight it.
    controller, switch = _cool_excluded_bathroom(tmp_path)
    controller.interlock_owned_valves = {"Bath Valve": "cool"}

    controller.tick(now=datetime(2026, 9, 14, 14, 0))

    assert switch.calls == []


def test_interlock_owning_the_valve_only_in_heat_does_not_exempt_it_in_cool(tmp_path):
    controller, switch = _cool_excluded_bathroom(tmp_path)
    controller.interlock_owned_valves = {"Bath Valve": "heat"}

    controller.tick(now=datetime(2026, 9, 14, 14, 0))

    assert switch.calls == ["off"]


def test_cool_disabled_zone_behaves_normally_in_heat_mode(tmp_path):
    zones = [Zone(name="Bathrooms", sensor="Bath Temp", valve="Bath Valve", cool_enabled=False)]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bath Temp": {"ok": True, "temperature_c": 15.0}}, switches={"Bath Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [20.0] * 24
    store.set_week("Bathrooms", week)

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 8, 0))  # 15C vs target 20C in heat mode -> should open

    assert switch.calls == ["on"]


# ------------------------------------------------------------- away mode -----

def test_away_forces_the_valve_closed_even_though_schedule_wants_it_open(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 10.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24  # 10C vs 22C would normally open the valve
    store.set_week("Rooms", week)
    store.set_away_until(datetime(2026, 9, 15, 0, 0))

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 8, 0))  # well before the away deadline

    assert switch.calls == ["off"]  # actively commanded off, not just left alone


def test_away_expiry_clears_itself_and_resumes_the_schedule_on_the_next_tick(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 10.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24
    store.set_week("Rooms", week)
    store.set_away_until(datetime(2026, 9, 14, 8, 0))

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 9, 0))  # one hour past the deadline

    assert switch.calls == ["on"]  # away expired - back to normal schedule logic
    assert store.get_away_until() is None  # and the stale deadline cleaned itself up



def test_away_leaves_a_valve_an_interlock_owns_to_its_interlock(tmp_path):
    """The one valve away must not touch: a zone valve that also feeds a
    dehumidifier. Closing it here would have the two control loops
    fighting over it every cycle, in the very situation (nobody home)
    where nobody would notice."""
    zones = [Zone(name="Bathrooms", sensor="Bath Temp", valve="Bath Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bath Temp": {"ok": True, "temperature_c": 10.0},
                                    "Bath Valve": {"ok": True, "is_on": True}},
                          switches={"Bath Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("cool")
    store.set_away_until(datetime(2026, 9, 15, 0, 0))
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.interlock_owned_valves = {"Bath Valve": None}   # owned in every mode

    controller.tick(now=datetime(2026, 9, 14, 8, 0))

    assert switch.calls == [], "left open: the interlock decides this one"


def test_away_still_closes_an_ordinary_valve_owned_by_nobody(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 10.0},
                                    "Rooms Valve": {"ok": True, "is_on": True}},
                          switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    store.set_away_until(datetime(2026, 9, 15, 0, 0))
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.interlock_owned_valves = {"Some Other Valve": None}

    controller.tick(now=datetime(2026, 9, 14, 8, 0))

    assert switch.calls == ["off"]


def test_an_interlock_owning_the_valve_in_the_other_mode_does_not_exempt_it_from_away(tmp_path):
    zones = [Zone(name="Bathrooms", sensor="Bath Temp", valve="Bath Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bath Temp": {"ok": True, "temperature_c": 10.0},
                                    "Bath Valve": {"ok": True, "is_on": True}},
                          switches={"Bath Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    store.set_away_until(datetime(2026, 9, 15, 0, 0))
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.interlock_owned_valves = {"Bath Valve": "cool"}   # only while cooling

    controller.tick(now=datetime(2026, 9, 14, 8, 0))

    assert switch.calls == ["off"]


# ------------------------------------------------------ failing hardware -----

def test_a_valve_command_that_fails_is_tried_again_next_tick(tmp_path):
    """A relay that refuses one command must not be written off as done:
    the zone would sit there with the thermostat convinced it acted."""
    class FlakySwitch(FakeSwitch):
        def __init__(self):
            super().__init__()
            self.fail = True

        def turn_on(self):
            super().turn_on()
            if self.fail:
                raise RuntimeError("Bath Valve: not answering")

    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FlakySwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 10.0}},
                          switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24
    store.set_week("Rooms", week)
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)

    controller.tick(now=datetime(2026, 9, 14, 8, 0))
    assert switch.calls == ["on"]

    switch.fail = False
    controller.tick(now=datetime(2026, 9, 14, 8, 1))

    assert switch.calls == ["on", "on"], "tried again, and this time it stuck"
    controller.tick(now=datetime(2026, 9, 14, 8, 2))
    assert switch.calls == ["on", "on"], "and then left alone"


# --------------------------------------------------------- ticks overlap -----

def test_two_ticks_at_once_do_not_interleave(tmp_path):
    """Every season/schedule/away change ticks from the request thread
    while the control loop ticks on its own. Two of them running through
    the same zone at the same time would double-count a silent sensor's
    clock and race each other's valve commands."""
    import threading

    class SlowSwitch(FakeSwitch):
        def __init__(self):
            super().__init__()
            self.inside = 0
            self.overlapped = False

        def turn_on(self):
            self.inside += 1
            self.overlapped = self.overlapped or self.inside > 1
            tm.sleep(0.05)
            self.inside -= 1
            super().turn_on()

    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = SlowSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 10.0}},
                          switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24
    store.set_week("Rooms", week)
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)

    when = datetime(2026, 9, 14, 8, 0)
    threads = [threading.Thread(target=controller.tick, kwargs={"now": when}) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not switch.overlapped, "two ticks were inside the same valve command at once"

# ------------------------------------------------------- long-run stability --

def test_long_run_simulation_stays_stable_across_many_simulated_days(tmp_path):
    # There's no real hardware yet, so "will this misbehave after days of
    # actual uptime" can't be checked by just running it - this compresses
    # 10 simulated days of hourly control-loop activity (with a mode
    # switch and an away period along the way) into one fast, deterministic
    # test, driving a simple closed-loop temperature model the same way
    # drivers/simulated/climate.py does (an open valve nudges the room
    # toward a warmer/cooler pull depending on mode; closed, it drifts
    # toward a neutral outdoor pull).
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 18.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [21.0] * 24
    store.set_week("Rooms", week)

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)

    current_c = 18.0
    start = datetime(2026, 9, 14, 0, 0)  # a Monday
    for hour in range(24 * 10):  # 10 simulated days
        now = start + timedelta(hours=hour)

        if hour == 24 * 4:  # day 4: leave for 2 days
            store.set_away_until(now + timedelta(days=2))
        if hour == 24 * 7:  # day 7: switch the whole plant to cooling
            store.set_mode("cool")
            week_cool = ScheduleStore.empty_week()
            for day in week_cool:
                week_cool[day] = [24.0] * 24
            store.set_week("Rooms", week_cool)

        manager._readings["Rooms Temp"] = {"ok": True, "temperature_c": current_c}
        controller.tick(now=now)  # must never raise, whatever state things are in

        valve_on = bool(switch.calls) and switch.calls[-1] == "on"
        mode = store.get_mode()
        pull = (26.0 if mode == "heat" else 18.0) if valve_on else 15.0
        current_c += (pull - current_c) * (0.4 if valve_on else 0.15)

        assert len(controller._valve_open) <= len(zones)  # never leaks
        assert -10.0 < current_c < 50.0  # never diverges into nonsense

    assert 10.0 < current_c < 26.0  # settled somewhere sane, not pegged at an extreme


# --------------------------------------------------------- diagnostic mode ---

def test_diagnostic_mode_defaults_off_and_round_trips(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    assert store.get_diagnostic_mode() is False
    store.set_diagnostic_mode(True)
    assert store.get_diagnostic_mode() is True
    store.set_diagnostic_mode(False)
    assert store.get_diagnostic_mode() is False


def test_diagnostic_mode_stops_the_thermostat_touching_any_valve(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 10.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24  # 10C vs 22C would normally open it
    store.set_week("Rooms", week)
    store.set_diagnostic_mode(True)

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 8, 0))

    assert switch.calls == []  # not even a forced close - hands off entirely


def test_leaving_diagnostic_mode_resumes_normal_control_on_the_next_tick(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 10.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24
    store.set_week("Rooms", week)
    store.set_diagnostic_mode(True)

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 8, 0))
    assert switch.calls == []

    store.set_diagnostic_mode(False)
    controller.tick(now=datetime(2026, 9, 14, 8, 1))
    assert switch.calls == ["on"]  # reading sensors normally again


def test_leaving_diagnostic_mode_closes_a_zone_whose_sensor_is_unreachable(tmp_path):
    # Real bug found during live verification: a zone whose sensor is
    # unreachable (e.g. a battery H&T that's actually offline) has its
    # valve manually opened during diagnostic mode - tick()'s own
    # per-zone loop *skips* an unreachable zone entirely (by design, so
    # a brief outage doesn't flap the valve), so on its own it would
    # never revisit and close that valve again after diagnostic mode
    # ends, leaving it stuck open indefinitely with zero temperature
    # justification. resync_after_diagnostic_mode() (called by
    # web/app.py right before the resuming tick()) must force it closed
    # instead of leaving tick() to skip it as usual.
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": False, "error": "sensor unreachable"}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_diagnostic_mode(True)
    controller = ThermostatController(manager, store, zones)

    # The installer's manual "open" through /climate-admin's diagnostic
    # section, while diagnostic mode is on - out from under the
    # controller entirely, same as web/app.py's api_sensor_action().
    switch.turn_on()
    controller._valve_open["Rooms"] = True  # what _apply() would have recorded, had it been the one to open it

    store.set_diagnostic_mode(False)
    controller.forget_valve_state()
    controller.resync_after_diagnostic_mode()
    controller.tick(now=datetime(2026, 9, 14, 8, 1))  # sensor is still unreachable - tick() alone would just skip this zone

    assert switch.calls[-1] == "off"  # forced closed, not left open


def test_resync_after_diagnostic_mode_leaves_a_reachable_zone_for_tick_to_recompute(tmp_path):
    # The other half of the fix above - a zone whose sensor *is*
    # reachable must not be force-closed by resync_after_diagnostic_mode()
    # itself; the tick() called right after it is what recomputes the
    # real desired state from the real reading.
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 18.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    controller = ThermostatController(manager, store, zones)

    controller.resync_after_diagnostic_mode()

    assert switch.calls == []  # untouched here - left for the immediately-following tick()


def test_forget_valve_state_forces_a_real_recheck_after_a_manual_change(tmp_path):
    # The bug this guards: an installer manually turns a valve on during
    # diagnostic mode; _valve_open never heard about that (it's a
    # bookkeeping cache of what the controller itself last commanded,
    # not a live read of the switch) and still says "closed" from
    # before diagnostic mode started. Without forget_valve_state(),
    # decide()==False vs. the stale _valve_open==False "already
    # matches", so _apply() wrongly skips commanding it off - the valve
    # would stay open, invisibly out of sync, until something else
    # changed the temperature enough to trigger a real transition.
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 20.0}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))  # no schedule set -> target is always None -> decide() is always False
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 8, 0))  # establishes _valve_open["Rooms"] = False
    assert controller._valve_open["Rooms"] is False

    # Simulate a manual "on" made through /climate-admin's diagnostic
    # section while diagnostic mode was active - out from under the
    # controller's own bookkeeping.
    switch.calls.clear()

    controller.forget_valve_state()
    controller.tick(now=datetime(2026, 9, 14, 8, 1))

    assert switch.calls == ["off"]  # actually recommanded, not skipped


# ------------------------------------------------------ sensor failsafe -----

def test_unreachable_sensor_leaves_the_valve_alone_before_the_threshold(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": False}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    controller = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0)

    controller.tick(now=datetime(2026, 9, 14, 8, 0))

    assert switch.calls == []  # existing base behavior: skipped, not forced
    assert controller.is_failsafe_active("Rooms") is False
    assert controller.unreachable_hours("Rooms") is None


def test_prolonged_sensor_silence_forces_the_valve_closed(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    switch.calls.append("on")  # pretend it was already open when the sensor went quiet
    manager = FakeManager(readings={"Rooms Temp": {"ok": False}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    controller = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0)
    controller._valve_open["Rooms"] = True

    for _ in range(25):  # 25 hours of watched silence, an hour per cycle
        controller._on_sensor_unreachable(zones[0], 3600)

    assert controller.is_failsafe_active("Rooms") is True
    assert switch.calls == ["on", "off"]
    assert controller.unreachable_hours("Rooms") == pytest.approx(25, abs=0.1)


def test_silence_is_counted_across_restarts(tmp_path):
    """A board that restarts every day used to restart the countdown with
    it, so a genuinely dead sensor never tripped the failsafe."""
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": False}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    silence = str(tmp_path / "sensor_silence.json")

    def run_for(hours):
        controller = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0,
                                          poll_interval_seconds=3600, silence_file=silence)
        controller.tick(now=datetime(2026, 9, 25, 8, 0))       # first tick of this run: no gap counted
        for _ in range(hours):
            controller._on_sensor_unreachable(zones[0], 3600)  # an hour of watched silence
        return controller

    run_for(13)
    controller = run_for(13)   # a restart in between, 26h of silence in total

    assert controller.is_failsafe_active("Rooms") is True
    assert controller.unreachable_hours("Rooms") >= 26
    assert switch.calls[-1] == "off"


def test_time_with_the_board_switched_off_is_not_silence(tmp_path):
    """Three days off is time nobody was listening - the sensors were
    talking to an empty room. Coming back must not declare them dead."""
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    manager = FakeManager(readings={"Rooms Temp": {"ok": False}}, switches={"Rooms Valve": FakeSwitch()})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    controller = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0,
                                      poll_interval_seconds=60, silence_file=str(tmp_path / "s.json"))
    real_time = tm.time

    # One tick, then the clock jumps three days: the board was off.
    controller.tick(now=datetime(2026, 9, 22, 8, 0))
    controller._last_tick_ts = real_time() - 3 * 86400
    controller.tick(now=datetime(2026, 9, 25, 8, 0))

    assert controller.is_failsafe_active("Rooms") is False
    # At most a couple of cycles counted, not three days.
    assert controller.unreachable_hours("Rooms") < 0.2


def test_the_failsafe_lifts_and_forgets_when_the_sensor_reports_again(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    manager = FakeManager(readings={"Rooms Temp": {"ok": False}}, switches={"Rooms Valve": FakeSwitch()})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    silence = str(tmp_path / "s.json")
    controller = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0, silence_file=silence)
    controller._on_sensor_unreachable(zones[0], 25 * 3600)
    assert controller.is_failsafe_active("Rooms") is True

    manager._readings["Rooms Temp"] = {"ok": True, "temperature_c": 18.0}
    controller.tick(now=datetime(2026, 9, 25, 9, 0))

    assert controller.is_failsafe_active("Rooms") is False
    assert controller.unreachable_hours("Rooms") is None
    # ...and a restart doesn't bring the old total back.
    fresh = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0, silence_file=silence)
    assert fresh.unreachable_hours("Rooms") is None


# ----------------------------------------------------- per-season grids -----
# Heating and cooling each have their own weekly grid. With one shared grid,
# switching the real deployment to cooling applied the winter setpoints as
# cooling targets - it was cooling a 26.5C room toward 19C.

def _flat_week(value):
    return {day: [value] * 24 for day in ScheduleStore.empty_week()}


def test_each_season_keeps_its_own_grid(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_week("Rooms", _flat_week(20.0), mode="heat")
    store.set_week("Rooms", _flat_week(26.0), mode="cool")

    assert store.get_week("Rooms", "heat")["mon"][10] == 20.0
    assert store.get_week("Rooms", "cool")["mon"][10] == 26.0


def test_no_mode_means_the_current_season(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_week("Rooms", _flat_week(20.0), mode="heat")
    store.set_week("Rooms", _flat_week(26.0), mode="cool")
    when = datetime(2026, 9, 14, 10, 0)

    store.set_mode("heat")
    assert store.get_target("Rooms", when) == 20.0
    store.set_mode("cool")
    assert store.get_target("Rooms", when) == 26.0


def test_a_file_from_before_the_split_is_the_heating_grid(tmp_path):
    # schedules.json files written before seasons were split only have
    # "zones" - that must keep meaning the heating grid, untouched, with
    # an empty (all-off) cooling grid next to it.
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps({"mode": "heat", "zones": {"Rooms": _flat_week(21)}}), encoding="utf-8")
    store = ScheduleStore(str(path))

    assert store.get_week("Rooms", "heat")["mon"][10] == 21
    assert all(v is None for v in store.get_week("Rooms", "cool")["mon"])


def test_an_old_file_left_in_cooling_does_not_cool_toward_winter_setpoints(tmp_path):
    # The exact state of the real deployment when this was found: an old
    # single-grid file, season set to cool. After the split its cooling
    # grid is empty, so the valve stays closed instead of chasing 19C.
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps({"mode": "cool", "zones": {"Rooms": _flat_week(19)}}), encoding="utf-8")
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 26.5}}, switches={"Rooms Valve": switch})
    controller = ThermostatController(manager, ScheduleStore(str(path)), zones, hysteresis_c=0.5)

    controller.tick(now=datetime(2026, 9, 21, 8, 0))

    assert switch.calls == ["off"]


def test_cooling_follows_the_cooling_grid(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": 26.5}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_week("Rooms", _flat_week(20.0), mode="heat")  # heating would keep this closed at 26.5C
    store.set_week("Rooms", _flat_week(25.0), mode="cool")
    store.set_mode("cool")

    ThermostatController(manager, store, zones, hysteresis_c=0.5).tick(now=datetime(2026, 9, 21, 8, 0))

    assert switch.calls == ["on"]  # 26.5 > 25 + 0.5 -> cooling


def test_an_explicit_season_is_saved_there_whatever_the_current_one(tmp_path):
    # The dashboard saves with the season the grid was loaded for, so a
    # season switch between painting and the debounced save can't file
    # the grid under the wrong season.
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("cool")
    store.set_week("Rooms", _flat_week(19.0), mode="heat")

    assert store.get_week("Rooms", "heat")["mon"][0] == 19.0
    assert all(v is None for v in store.get_week("Rooms", "cool")["mon"])


def test_rejects_an_unknown_season(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    with pytest.raises(ValueError):
        store.set_week("Rooms", _flat_week(20.0), mode="spring")


def test_concurrent_saves_do_not_lose_each_other(tmp_path):
    # Read-modify-write of one shared file from a threaded web server:
    # without the store's lock, saves landing together could each read
    # the old file and the last write would drop the others.
    import threading

    store = ScheduleStore(str(tmp_path / "schedules.json"))
    names = [f"Zone{i}" for i in range(12)]
    barrier = threading.Barrier(len(names))

    def save(name, value):
        barrier.wait()
        store.set_week(name, _flat_week(value), mode="heat")

    threads = [threading.Thread(target=save, args=(n, 18.0 + i * 0.5)) for i, n in enumerate(names)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i, name in enumerate(names):
        assert store.get_week(name, "heat")["mon"][0] == 18.0 + i * 0.5


# ------------------------------------------------------ applied targets -----
# What the chart plots as "why the valve moved": only a target that actually
# drove the valve this tick. Anything else (away, diagnostic, excluded,
# unreachable) must read as "no target in control", not a stale value.

def _room_setup(tmp_path, temperature=18.0, ok=True):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    reading = {"ok": True, "temperature_c": temperature} if ok else {"ok": False}
    manager = FakeManager(readings={"Rooms Temp": reading}, switches={"Rooms Valve": FakeSwitch()})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_week("Rooms", _flat_week(21.0), mode="heat")
    return ThermostatController(manager, store, zones, hysteresis_c=0.5), store


def test_applied_target_is_the_one_the_valve_was_driven_against(tmp_path):
    controller, _ = _room_setup(tmp_path)
    controller.tick(now=datetime(2026, 9, 21, 8, 0))
    assert controller.applied_target("Rooms") == 21.0


def test_no_applied_target_while_away(tmp_path):
    controller, store = _room_setup(tmp_path)
    store.set_away_until(datetime(2026, 9, 22, 0, 0))
    controller.tick(now=datetime(2026, 9, 21, 8, 0))
    assert controller.applied_target("Rooms") is None


def test_no_applied_target_in_diagnostic_mode(tmp_path):
    controller, store = _room_setup(tmp_path)
    controller.tick(now=datetime(2026, 9, 21, 8, 0))
    store.set_diagnostic_mode(True)
    controller.tick(now=datetime(2026, 9, 21, 8, 1))  # the previous tick's target must not linger
    assert controller.applied_target("Rooms") is None


def test_no_applied_target_with_the_sensor_unreachable(tmp_path):
    controller, _ = _room_setup(tmp_path, ok=False)
    controller.tick(now=datetime(2026, 9, 21, 8, 0))
    assert controller.applied_target("Rooms") is None


def test_no_applied_target_in_an_off_hour(tmp_path):
    controller, store = _room_setup(tmp_path)
    week = _flat_week(21.0)
    week["mon"][8] = None
    store.set_week("Rooms", week, mode="heat")
    controller.tick(now=datetime(2026, 9, 21, 8, 0))  # a Monday, 08:00 is "off"
    assert controller.applied_target("Rooms") is None


def test_cool_disabled_valve_opened_by_hand_is_closed_again(tmp_path):
    controller, switch = _cool_excluded_bathroom(tmp_path)
    controller.tick(now=datetime(2026, 9, 14, 14, 0))
    assert switch.calls == ["off"]

    # Someone opens it (manual toggle, another app): the next tick sees the
    # real state, not just the command it sent last time, and closes it.
    controller.manager._readings["Bath Valve"] = {"ok": True, "is_on": True}
    controller.tick(now=datetime(2026, 9, 14, 14, 1))

    assert switch.calls == ["off", "off"]


def test_a_valve_that_drifted_from_the_last_command_is_corrected(tmp_path):
    # Heating, valve commanded open - then the relay reboots into "off"
    # (or is switched off from its own app). The decision is still "open",
    # so the old check "same as my last command? skip" never resent it.
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    readings = {"Rooms Temp": {"ok": True, "temperature_c": 18.0}, "Rooms Valve": {"ok": True, "is_on": False}}
    manager = FakeManager(readings=readings, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    store.set_week("Rooms", _flat_week(22.0))
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)

    controller.tick(now=datetime(2026, 9, 14, 8, 0))
    readings["Rooms Valve"] = {"ok": True, "is_on": True}
    controller.tick(now=datetime(2026, 9, 14, 8, 1))  # really open: nothing to send
    readings["Rooms Valve"] = {"ok": True, "is_on": False}  # relay rebooted
    controller.tick(now=datetime(2026, 9, 14, 8, 2))

    assert switch.calls == ["on", "on"]


def test_an_offline_valve_is_not_commanded_on_every_tick(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    readings = {"Rooms Temp": {"ok": True, "temperature_c": 18.0}, "Rooms Valve": {"ok": False, "error": "offline"}}
    manager = FakeManager(readings=readings, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    store.set_week("Rooms", _flat_week(22.0))
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)

    for minute in range(3):
        controller.tick(now=datetime(2026, 9, 14, 8, minute))

    assert switch.calls == ["on"]


@pytest.mark.parametrize("mode, temperature", [("heat", 10.0), ("cool", 32.0)])
def test_an_off_hour_holds_the_valve_closed_even_if_it_was_open(tmp_path, mode, temperature):
    # An hour painted "off" (no target) must close the valve and keep it
    # closed - whatever the room's temperature, in either season, and even
    # if the valve was open (from the previous hour, or opened by hand).
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    readings = {"Rooms Temp": {"ok": True, "temperature_c": temperature}, "Rooms Valve": {"ok": True, "is_on": True}}
    manager = FakeManager(readings=readings, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode(mode)
    week = _flat_week(21.0)
    week["mon"][14] = None  # Monday 14:00-15:00 is off
    store.set_week("Rooms", week)
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)

    controller.tick(now=datetime(2026, 9, 14, 14, 5))
    assert switch.calls == ["off"]
    assert controller.applied_target("Rooms") is None

    readings["Rooms Valve"] = {"ok": True, "is_on": False}
    controller.tick(now=datetime(2026, 9, 14, 14, 30))
    assert switch.calls == ["off"]  # stays closed, nothing more to send

    readings["Rooms Valve"] = {"ok": True, "is_on": True}  # opened by hand
    controller.tick(now=datetime(2026, 9, 14, 14, 45))
    assert switch.calls == ["off", "off"]


def test_a_zone_with_no_schedule_at_all_keeps_its_valve_closed(tmp_path):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    readings = {"Rooms Temp": {"ok": True, "temperature_c": 10.0}, "Rooms Valve": {"ok": True, "is_on": True}}
    manager = FakeManager(readings=readings, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))  # nothing painted, e.g. right after "clear schedules"
    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)

    controller.tick(now=datetime(2026, 9, 14, 8, 0))

    assert switch.calls == ["off"]



# ------------------------------------------- two phones, one schedule -----

def test_cells_from_two_pages_are_merged_not_overwritten(tmp_path):
    """Phone A saves Monday 08:00, then phone B - opened before - saves
    Monday 20:00. Sending whole weeks, B's save wiped A's hour."""
    store = ScheduleStore(str(tmp_path / "schedules.json"))

    store.set_cells("Rooms", [["mon", 8, 21.0]])
    week = store.set_cells("Rooms", [["mon", 20, 19.0]])

    assert week["mon"][8] == 21.0 and week["mon"][20] == 19.0
    assert store.get_week("Rooms")["mon"][8] == 21.0


def test_the_same_hour_edited_twice_keeps_the_later_value(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_cells("Rooms", [["mon", 8, 21.0]])
    store.set_cells("Rooms", [["mon", 8, None]])
    assert store.get_week("Rooms")["mon"][8] is None


def test_cells_go_to_the_season_they_were_edited_for(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    store.set_cells("Rooms", [["tue", 7, 24.0]], mode="cool")
    assert store.get_week("Rooms", "cool")["tue"][7] == 24.0
    assert store.get_week("Rooms", "heat")["tue"][7] is None


@pytest.mark.parametrize("cells", [
    "x", [["mon", 8]], [["xyz", 8, 20]], [["mon", 24, 20]], [["mon", "8", 20]],
    [["mon", 8, "warm"]], [["mon", 8, True]],
])
def test_a_bad_cell_is_refused_and_nothing_is_written(tmp_path, cells):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_cells("Rooms", [["mon", 1, 20.0]])
    with pytest.raises(ValueError):
        store.set_cells("Rooms", cells)
    assert store.get_week("Rooms")["mon"][1] == 20.0


# ------------------------------------------------ temporary targets ----------

def _room(tmp_path, temperature=18.0, scheduled=20.0):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": True, "temperature_c": temperature}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("heat")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [scheduled] * 24
    store.set_week("Rooms", week)
    return ThermostatController(manager, store, zones, hysteresis_c=0.3), store, switch


def test_a_temporary_target_beats_the_schedule_until_its_time_is_up(tmp_path):
    controller, store, switch = _room(tmp_path, temperature=18.0, scheduled=16.0)
    now = datetime(2026, 10, 5, 18, 0)
    controller.tick(now=now)
    assert switch.calls == ["off"]                  # 18 vs 16: closed
    store.set_override("Rooms", 22.0, now + timedelta(hours=2))
    controller.tick(now=now + timedelta(minutes=5))
    assert switch.calls == ["off", "on"] and controller.applied_target("Rooms") == 22.0
    controller.tick(now=now + timedelta(hours=2, minutes=1))
    assert switch.calls == ["off", "on", "off"], "back to the schedule's 16"
    assert store.get_overrides(now + timedelta(hours=3)) == {}
    assert "Rooms" not in json.loads((tmp_path / "schedules.json").read_text())["overrides"], "expired, dropped"


def test_a_temporary_off_holds_the_valve_closed(tmp_path):
    controller, store, switch = _room(tmp_path, temperature=15.0, scheduled=21.0)
    now = datetime(2026, 10, 5, 18, 0)
    store.set_override("Rooms", None, now + timedelta(hours=1))
    controller.tick(now=now)
    assert switch.calls == ["off"] and controller.applied_target("Rooms") is None


def test_a_temporary_target_beats_away_mode(tmp_path):
    """Asked for from the dashboard while "away" still runs: someone is
    coming home early."""
    controller, store, switch = _room(tmp_path, temperature=15.0, scheduled=21.0)
    now = datetime(2026, 10, 5, 18, 0)
    store.set_away_until(now + timedelta(days=2))
    store.set_override("Rooms", 22.0, now + timedelta(hours=2))
    controller.tick(now=now)
    assert switch.calls == ["on"]


def test_a_season_change_drops_the_temporary_targets(tmp_path):
    _, store, _ = _room(tmp_path)
    now = datetime(2026, 10, 5, 18, 0)
    store.set_override("Rooms", 22.0, now + timedelta(hours=2))
    store.set_mode("cool")
    assert store.get_overrides(now) == {}


def test_the_next_change_is_the_next_hour_with_a_different_target(tmp_path):
    _, store, _ = _room(tmp_path, scheduled=20.0)
    week = store.get_week("Rooms")
    week["mon"][21] = 17.0                            # Monday 21:00 goes down
    store.set_week("Rooms", week)
    monday_18_30 = datetime(2026, 10, 5, 18, 30)       # a Monday
    assert store.next_change("Rooms", monday_18_30) == datetime(2026, 10, 5, 21, 0)
    flat = ScheduleStore(str(tmp_path / "flat.json"))
    assert flat.next_change("Rooms", monday_18_30) is None
