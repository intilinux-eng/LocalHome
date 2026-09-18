import json
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
    # dashboard's schedule editor actually loads, and saveSchedule() in
    # app.js always resubmits the *whole* week on any edit - if get_week()
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


def test_cool_disabled_zone_is_never_touched_while_cooling(tmp_path):
    # A bathroom's radiant panel would condense regardless of its own
    # temperature - cool_enabled=False means "hands off entirely" in cool
    # mode, not even a forced "closed" command (see services/interlock.py
    # for why something else may legitimately need to open that valve).
    zones = [Zone(name="Bathrooms", sensor="Bath Temp", valve="Bath Valve", cool_enabled=False)]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bath Temp": {"ok": True, "temperature_c": 30.0}}, switches={"Bath Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.set_mode("cool")
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [22.0] * 24  # 30C vs target 22C in cool mode would normally open it
    store.set_week("Bathrooms", week)

    controller = ThermostatController(manager, store, zones, hysteresis_c=0.3)
    controller.tick(now=datetime(2026, 9, 14, 14, 0))

    assert switch.calls == []


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
    assert controller.unreachable_hours("Rooms") == pytest.approx(0.0, abs=0.01)


def test_prolonged_sensor_silence_forces_the_valve_closed(tmp_path, monkeypatch):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    switch.calls.append("on")  # pretend it was already open when the sensor went quiet
    manager = FakeManager(readings={"Rooms Temp": {"ok": False}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    controller = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0)
    controller._valve_open["Rooms"] = True

    now = {"t": 1_000_000.0}
    monkeypatch.setattr("localhome.services.thermostat.time.time", lambda: now["t"])

    controller.tick(now=datetime(2026, 9, 14, 8, 0))  # first sighting of the outage
    assert controller.is_failsafe_active("Rooms") is False

    now["t"] += 25 * 3600  # 25h later, still unreachable
    controller.tick(now=datetime(2026, 9, 15, 9, 0))

    assert switch.calls[-1] == "off"
    assert controller.is_failsafe_active("Rooms") is True
    assert controller.unreachable_hours("Rooms") == pytest.approx(25.0, abs=0.01)


def test_failsafe_clears_once_the_sensor_reports_again(tmp_path, monkeypatch):
    zones = [Zone(name="Rooms", sensor="Rooms Temp", valve="Rooms Valve")]
    switch = FakeSwitch()
    manager = FakeManager(readings={"Rooms Temp": {"ok": False}}, switches={"Rooms Valve": switch})
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    controller = ThermostatController(manager, store, zones, sensor_failsafe_hours=24.0)

    now = {"t": 1_000_000.0}
    monkeypatch.setattr("localhome.services.thermostat.time.time", lambda: now["t"])
    controller.tick(now=datetime(2026, 9, 14, 8, 0))
    now["t"] += 25 * 3600
    controller.tick(now=datetime(2026, 9, 15, 9, 0))
    assert controller.is_failsafe_active("Rooms") is True

    manager._readings["Rooms Temp"] = {"ok": True, "temperature_c": 18.0}
    controller.tick(now=datetime(2026, 9, 15, 9, 1))

    assert controller.is_failsafe_active("Rooms") is False
    assert controller.unreachable_hours("Rooms") is None
