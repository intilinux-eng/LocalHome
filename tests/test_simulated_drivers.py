from localhome.drivers.simulated import world
from localhome.drivers.simulated.climate import SimulatedClimateDriver
from localhome.drivers.simulated.switch import SimulatedSwitchDriver


def test_switch_starts_at_its_initial_state():
    driver = SimulatedSwitchDriver(name="Test Valve A", initial_on=True)
    assert driver.read()["is_on"] is True


def test_switch_turn_on_off_is_reflected_in_read():
    driver = SimulatedSwitchDriver(name="Test Valve B")
    driver.turn_on()
    assert driver.read()["is_on"] is True
    driver.turn_off()
    assert driver.read()["is_on"] is False


def test_switch_history_field_is_is_on():
    driver = SimulatedSwitchDriver(name="Test Valve C")
    assert driver.history_fields == ("is_on",)


def test_climate_reading_has_ok_and_expected_fields():
    driver = SimulatedClimateDriver(name="Test Sensor A", base_temperature_c=19.0)
    reading = driver.read()
    assert reading["ok"] is True
    assert "temperature_c" in reading
    assert "humidity_pct" in reading


def test_climate_drifts_toward_influence_target_while_switch_is_on():
    world.set_switch("Test Valve D", True)
    driver = SimulatedClimateDriver(
        name="Test Sensor B",
        base_temperature_c=18.0,
        influenced_by="Test Valve D",
        influence_target_c=26.0,
        drift_rate_c_per_min=1000.0,  # effectively instant, for a deterministic test
    )
    # Force a large elapsed time so the drift step isn't clamped by real
    # wall-clock time between two read() calls in the same test.
    driver._last_tick -= 60

    reading = driver.read()

    assert reading["temperature_c"] > 20.0  # moved well past the base toward the influence target


def test_climate_drifts_back_toward_base_once_switch_is_off():
    world.set_switch("Test Valve E", False)
    driver = SimulatedClimateDriver(
        name="Test Sensor C",
        base_temperature_c=18.0,
        influenced_by="Test Valve E",
        influence_target_c=26.0,
        drift_rate_c_per_min=1000.0,
    )
    driver._temperature = 26.0
    driver._last_tick -= 60

    reading = driver.read()

    assert reading["temperature_c"] < 20.0  # moved back down toward the 18.0 base


def test_climate_without_influenced_by_ignores_any_switch_state():
    world.set_switch("Unrelated Switch", True)
    driver = SimulatedClimateDriver(name="Test Sensor D", base_temperature_c=21.0, influenced_by=None)
    driver._last_tick -= 60

    reading = driver.read()

    assert 19.0 < reading["temperature_c"] < 23.0  # stays near the base, no influence target pulling it


# ------------------------------------------- a simulated Shelly H&T Gen3 --
# What the admin panel's "simulated" zone sensor is: drivers/simulated/
# climate.py with mimic_ht_gen3 - the real device's fields and rhythm, and
# a room that follows its zone's valve and the season.

import json as _json

from localhome.drivers.simulated import climate as sim_climate


class _Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _ht(tmp_path, monkeypatch, season="heat", valve="HT Valve"):
    clock = _Clock()
    monkeypatch.setattr(sim_climate.time, "time", clock)
    monkeypatch.setattr(sim_climate.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(sim_climate.random, "gauss", lambda mu, sigma: mu)
    season_file = tmp_path / "schedules.json"
    season_file.write_text(_json.dumps({"mode": season}), encoding="utf-8")
    world.set_switch(valve, False)
    driver = SimulatedClimateDriver(name="HT Sensor", mimic_ht_gen3=True, influenced_by=valve,
                                    season_file=str(season_file), state_file=str(tmp_path / "simulated_sensors.json"))
    return driver, clock, season_file


def test_a_simulated_ht_reports_the_real_ones_fields(tmp_path, monkeypatch):
    driver, clock, _ = _ht(tmp_path, monkeypatch)
    reading = driver.read()
    assert set(reading) == {"ok", "name", "temperature_c", "humidity_pct", "battery_pct", "wifi_rssi_dbm"}
    assert reading["temperature_c"] == sim_climate.HT_REST_C["heat"]
    assert 80 <= reading["battery_pct"] <= 99 and -73 <= reading["wifi_rssi_dbm"] <= -55
    assert driver.history_fields == ("temperature_c", "humidity_pct", "battery_pct", "wifi_rssi_dbm")
    assert driver.last_update_ts == clock.t


def test_it_reports_on_a_half_degree_or_its_two_hour_heartbeat_only(tmp_path, monkeypatch):
    driver, clock, _ = _ht(tmp_path, monkeypatch)
    first = driver.read()
    world.set_switch("HT Valve", True)                 # warms 0.6 degrees an hour
    clock.t += 30 * 60                                 # +0.3: under the threshold
    assert driver.read()["temperature_c"] == first["temperature_c"]
    assert driver.last_update_ts == clock.t - 30 * 60, "nothing new sent"
    clock.t += 30 * 60                                 # +0.6 in all: a change report
    assert driver.read()["temperature_c"] == round(first["temperature_c"] + 0.6, 1)
    changed_at = driver.last_update_ts
    world.set_switch("HT Valve", False)
    clock.t = changed_at + 50 * 60                     # 1 h 50 after the first: still quiet
    driver.read()
    assert driver.last_update_ts == changed_at
    clock.t = changed_at + 61 * 60                     # 2 h after the first report: the heartbeat
    driver.read()
    assert driver.last_update_ts == clock.t, "the heartbeat kept its own clock"


def test_an_open_valve_cools_the_room_in_summer(tmp_path, monkeypatch):
    driver, clock, season_file = _ht(tmp_path, monkeypatch, season="cool")
    start = driver.read()["temperature_c"]
    assert start == sim_climate.HT_REST_C["cool"]
    world.set_switch("HT Valve", True)
    clock.t += 3600
    assert driver.read()["temperature_c"] == round(start - 0.6, 1)
    season_file.write_text(_json.dumps({"mode": "heat"}), encoding="utf-8")
    clock.t += 3600                                    # winter now: heating never cools a room
    assert driver.read()["temperature_c"] == round(start - 0.6, 1)


def test_a_quiet_room_reports_as_often_as_a_real_one_whatever_the_poll(monkeypatch):
    """12 heartbeats a day and two or three humidity reports, as the real
    ones in docs/shelly-gen3.md - the room's pace, not the poll's."""
    import random
    clock = _Clock()
    monkeypatch.setattr(sim_climate.time, "time", clock)
    world.set_switch("Quiet Valve", False)
    for poll in (10, 60):
        random.seed(7)
        driver = SimulatedClimateDriver(name=f"Quiet {poll}", mimic_ht_gen3=True, influenced_by="Quiet Valve")
        reports, last = 0, None
        for _ in range(3 * 86400 // poll):
            driver.read()
            reports += driver.last_update_ts != last
            last = driver.last_update_ts
            clock.t += poll
        assert 12 <= reports / 3 <= 17, (poll, reports / 3)


def test_after_a_restart_it_carries_on_where_it_was(tmp_path, monkeypatch):
    """The last report with its real age, the heartbeat on its own clock,
    the room as warm as it had got - not back to the resting temperature."""
    driver, clock, season_file = _ht(tmp_path, monkeypatch)
    first = driver.read()
    world.set_switch("HT Valve", True)
    clock.t += 3600
    warmer = driver.read()                             # +0.6: a change report
    reported_at = driver.last_update_ts
    clock.t += 600                                     # LocalHome restarts
    again = SimulatedClimateDriver(name="HT Sensor", mimic_ht_gen3=True, influenced_by="HT Valve",
                                   season_file=str(season_file), state_file=str(tmp_path / "simulated_sensors.json"))
    world.set_switch("HT Valve", False)
    assert again.read() == warmer and again.last_update_ts == reported_at
    clock.t = reported_at + 3600 - 60                  # the heartbeat is two hours after the first report
    again.read()
    assert again.last_update_ts == reported_at
    clock.t = reported_at + 3600 + 1
    assert again.read()["temperature_c"] > first["temperature_c"] and again.last_update_ts == clock.t


def test_each_report_is_published_like_a_real_ones(tmp_path, monkeypatch):
    driver, clock, _ = _ht(tmp_path, monkeypatch)
    heard = []
    world.subscribe("HT Sensor/status/devicepower:0", lambda topic, payload: heard.append((topic, _json.loads(payload))))
    world.subscribe("HT Sensor/status/wifi", lambda topic, payload: heard.append((topic, _json.loads(payload))))
    reading = driver.read()
    driver.read()                                      # nothing new: nothing sent
    assert heard == [
        ("HT Sensor/status/devicepower:0", {"id": 0, "battery": {"percent": reading["battery_pct"]}, "external": {"present": False}}),
        ("HT Sensor/status/wifi", {"rssi": reading["wifi_rssi_dbm"]}),
    ]


def test_an_unreadable_state_file_starts_afresh(tmp_path, monkeypatch):
    (tmp_path / "simulated_sensors.json").write_text('{"HT Sensor": {"room": 1}}', encoding="utf-8")
    driver, _, _ = _ht(tmp_path, monkeypatch)
    assert driver.read()["temperature_c"] == sim_climate.HT_REST_C["heat"]


def test_the_plain_simulator_is_unchanged(tmp_path, monkeypatch):
    driver = SimulatedClimateDriver(name="Plain Sensor", base_temperature_c=19.0)
    reading = driver.read()
    assert set(reading) == {"ok", "name", "temperature_c", "humidity_pct"}
    assert driver.last_update_ts is None and driver.history_fields == ("temperature_c", "humidity_pct")


from localhome.drivers.simulated.number import SimulatedNumberDriver


def test_picking_a_speed_starts_a_simulated_vmc_like_the_real_dimmer():
    vmc = SimulatedSwitchDriver("Sim VMC", initial_on=False)
    speed = SimulatedNumberDriver("Sim VMC Speed", value=50, on_with_value=True, paired_switch="Sim VMC")
    speed.set_value(20)
    assert speed.read()["value"] == 20 and vmc.read()["is_on"] is True


def test_a_plain_simulated_number_leaves_its_switch_alone():
    lamp = SimulatedSwitchDriver("Sim Lamp", initial_on=False)
    SimulatedNumberDriver("Sim Lamp Level", paired_switch="Sim Lamp").set_value(80)
    assert lamp.read()["is_on"] is False
