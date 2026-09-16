import pytest

from localhome.services.climate_map_store import (
    ClimateMapStore,
    build_climate_integrations,
    build_zone_configs,
    validate_zones,
)

VALID_ZONES = [
    {"name": "Rooms", "sensor_mode": "shelly", "sensor_device_id": "shellyht-AAAA", "valve_device_id": "shellyplus1-BBBB", "cool_enabled": True},
    {"name": "Bathrooms", "sensor_mode": "manual", "sensor_device_id": None, "valve_device_id": "shellyplus1-CCCC", "cool_enabled": False},
]


# --------------------------------------------------------------- store ----

def test_get_zones_is_none_when_nothing_saved_yet(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    assert store.get_zones() is None


def test_set_and_get_round_trip(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_zones(VALID_ZONES)
    assert store.get_zones() == VALID_ZONES


def test_a_second_store_instance_sees_a_saved_mapping(tmp_path):
    path = str(tmp_path / "climate_map.json")
    ClimateMapStore(path).set_zones(VALID_ZONES)
    assert ClimateMapStore(path).get_zones() == VALID_ZONES


def test_an_invalid_mapping_is_never_written(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    with pytest.raises(ValueError):
        store.set_zones(VALID_ZONES[:1])  # only one zone
    assert store.get_zones() is None


def test_a_corrupt_file_defers_to_none_rather_than_raising(tmp_path):
    path = tmp_path / "climate_map.json"
    path.write_text("not valid json", encoding="utf-8")
    store = ClimateMapStore(str(path))

    assert store.get_zones() is None
    store.set_zones(VALID_ZONES)  # must still be able to save afterward
    assert store.get_zones() == VALID_ZONES


# ---------------------------------------------------------- validation ----

def test_rejects_the_wrong_number_of_zones():
    with pytest.raises(ValueError, match="exactly 2 zones"):
        validate_zones(VALID_ZONES + [VALID_ZONES[0]])


def test_rejects_a_missing_name():
    zones = [dict(VALID_ZONES[0], name=""), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="name"):
        validate_zones(zones)


def test_rejects_duplicate_zone_names():
    zones = [VALID_ZONES[0], dict(VALID_ZONES[1], name=VALID_ZONES[0]["name"])]
    with pytest.raises(ValueError, match="unique"):
        validate_zones(zones)


def test_rejects_an_unknown_sensor_mode():
    zones = [dict(VALID_ZONES[0], sensor_mode="cloud"), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="sensor_mode"):
        validate_zones(zones)


def test_rejects_a_missing_sensor_device_id_in_shelly_mode():
    zones = [dict(VALID_ZONES[0], sensor_device_id=None), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="sensor_device_id"):
        validate_zones(zones)


def test_does_not_require_a_sensor_device_id_in_simulated_or_manual_mode():
    zones = [
        dict(VALID_ZONES[0], sensor_mode="simulated", sensor_device_id=None),
        VALID_ZONES[1],  # already "manual" with sensor_device_id=None
    ]
    validate_zones(zones)  # must not raise


def test_rejects_a_missing_valve_device_id():
    zones = [dict(VALID_ZONES[0], valve_device_id=None), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="valve_device_id"):
        validate_zones(zones)


def test_rejects_a_non_bool_cool_enabled():
    zones = [dict(VALID_ZONES[0], cool_enabled="yes"), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="cool_enabled"):
        validate_zones(zones)


def test_rejects_the_same_device_id_used_for_a_zones_sensor_and_its_own_valve():
    zones = [dict(VALID_ZONES[0], valve_device_id=VALID_ZONES[0]["sensor_device_id"]), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="duplicate id"):
        validate_zones(zones)


def test_rejects_the_same_valve_device_id_reused_across_two_zones():
    zones = [VALID_ZONES[0], dict(VALID_ZONES[1], valve_device_id=VALID_ZONES[0]["valve_device_id"])]
    with pytest.raises(ValueError, match="duplicate id"):
        validate_zones(zones)


# ------------------------------------------------- integration building ----

def test_build_climate_integrations_shelly_mode_shape():
    integrations = build_climate_integrations([VALID_ZONES[0]], "mqtt_broker.json", "manual_override.json")
    sensor = next(i for i in integrations if i.options["name"] == "Rooms Sensor")

    assert sensor.kind == "climate"
    assert sensor.type == "mqtt_json"
    assert sensor.options["state_topic"] == [
        "shellyht-AAAA/status/temperature:0",
        "shellyht-AAAA/status/humidity:0",
        "shellyht-AAAA/status/devicepower:0",
    ]
    assert sensor.options["fields"] == {"temperature_c": "tC", "humidity_pct": "rh", "battery_pct": "battery.percent"}
    assert sensor.options["stale_after_seconds"] == 21600


def test_build_climate_integrations_valve_shape_is_shelly_regardless_of_sensor_mode():
    integrations = build_climate_integrations([VALID_ZONES[1]], "mqtt_broker.json", "manual_override.json")
    valve = next(i for i in integrations if i.options["name"] == "Bathrooms Valve")

    assert valve.kind == "switch"
    assert valve.type == "mqtt_json"
    assert valve.options["state_topic"] == "shellyplus1-CCCC/status/switch:0"
    assert valve.options["command_topic"] == "shellyplus1-CCCC/command/switch:0"
    assert valve.options["state_field"] == "output"
    assert valve.options["payload_on"] is True
    assert valve.options["payload_off"] is False
    assert "stale_after_seconds" not in valve.options


def test_build_climate_integrations_simulated_mode_shape():
    zone = dict(VALID_ZONES[0], sensor_mode="simulated", sensor_device_id=None)
    integrations = build_climate_integrations([zone], "mqtt_broker.json", "manual_override.json")
    sensor = next(i for i in integrations if i.options["name"] == "Rooms Sensor")

    assert sensor.kind == "climate"
    assert sensor.type == "simulated"
    assert sensor.options == {"name": "Rooms Sensor", "dashboard_tab": "climate"}


def test_build_climate_integrations_manual_mode_shape():
    integrations = build_climate_integrations([VALID_ZONES[1]], "mqtt_broker.json", "manual_override.json")
    sensor = next(i for i in integrations if i.options["name"] == "Bathrooms Sensor")

    assert sensor.kind == "climate"
    assert sensor.type == "manual_override"
    assert sensor.options == {
        "name": "Bathrooms Sensor", "dashboard_tab": "climate", "store_path": "manual_override.json",
    }


def test_build_zone_configs_maps_to_the_thermostat_zone_shape():
    configs = build_zone_configs(VALID_ZONES)

    assert configs == [
        {"name": "Rooms", "sensor": "Rooms Sensor", "valve": "Rooms Valve", "cool_enabled": True},
        {"name": "Bathrooms", "sensor": "Bathrooms Sensor", "valve": "Bathrooms Valve", "cool_enabled": False},
    ]
