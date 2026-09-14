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
