import pytest

from localhome.config import Integration, load_config
from localhome.services.climate_map_store import (
    ClimateMapStore,
    apply_to_config,
    build_climate_integrations,
    build_extra_dimmer_integrations,
    build_extra_sensor_integrations,
    build_extra_switch_integrations,
    build_zone_configs,
    integration_device_id,
    snapshot_from_config,
    validate_extra_dimmers,
    validate_extra_sensors,
    validate_extra_switches,
    validate_zones,
)
from localhome.services.thermostat import Zone

VALID_ZONES = [
    {"name": "Rooms", "sensor_mode": "shelly", "sensor_device_id": "shellyht-AAAA", "valve_device_id": "shellyplus1-BBBB", "cool_enabled": True},
    {"name": "Bathrooms", "sensor_mode": "manual", "sensor_device_id": None, "valve_device_id": "shellyplus1-CCCC", "cool_enabled": False},
]

VALID_EXTRA_SWITCHES = {
    "bypass_valve_device_id": "shellyplus1-DDDD",
    "dehumidifier_device_id": "shellyplus1-EEEE",
}

VALID_EXTRA_SENSORS = {
    "outdoor_sensor_device_id": "shellyht-FFFF",
}


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


# ---------------------------------------------------- extra switches ----
# Two more fixed, standalone Shelly Plus 1 on/off switches every
# "reference model" apartment has (a bathroom bypass valve and a
# dehumidifier) - independent of zones, no name field, no sensor (see
# services/climate_map_store.py's EXTRA_SWITCH_FIELDS/module docstring).

def test_extra_switches_is_empty_when_nothing_saved_yet(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    assert store.get_extra_switches() == {}


def test_extra_switches_round_trip(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_extra_switches(VALID_EXTRA_SWITCHES)
    assert store.get_extra_switches() == VALID_EXTRA_SWITCHES


def test_saving_zones_does_not_wipe_previously_saved_extra_switches(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_extra_switches(VALID_EXTRA_SWITCHES)
    store.set_zones(VALID_ZONES)
    assert store.get_extra_switches() == VALID_EXTRA_SWITCHES
    assert store.get_zones() == VALID_ZONES


def test_saving_extra_switches_does_not_wipe_previously_saved_zones(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_zones(VALID_ZONES)
    store.set_extra_switches(VALID_EXTRA_SWITCHES)
    assert store.get_zones() == VALID_ZONES
    assert store.get_extra_switches() == VALID_EXTRA_SWITCHES


def test_either_extra_switch_field_may_be_left_null(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_extra_switches({"bypass_valve_device_id": "shellyplus1-DDDD", "dehumidifier_device_id": None})
    assert store.get_extra_switches() == {"bypass_valve_device_id": "shellyplus1-DDDD", "dehumidifier_device_id": None}


def test_rejects_a_blank_extra_switch_device_id():
    with pytest.raises(ValueError, match="bypass_valve_device_id"):
        validate_extra_switches({"bypass_valve_device_id": "  ", "dehumidifier_device_id": None})


def test_rejects_duplicate_extra_switch_device_ids():
    switches = {"bypass_valve_device_id": "shellyplus1-DDDD", "dehumidifier_device_id": "shellyplus1-DDDD"}
    with pytest.raises(ValueError, match="duplicate id"):
        validate_extra_switches(switches)


def test_the_duplicate_id_error_names_the_actual_offending_value():
    # Not just "a duplicate was found" - with up to 4 device id fields
    # across zones and extra switches, a technician needs to know
    # exactly which value to go fix (see _check_no_duplicates()).
    switches = {"bypass_valve_device_id": "shellyplus1-DDDD", "dehumidifier_device_id": "shellyplus1-DDDD"}
    with pytest.raises(ValueError, match="shellyplus1-DDDD"):
        validate_extra_switches(switches)


def test_rejects_an_extra_switch_device_id_already_used_by_a_zone():
    switches = {"bypass_valve_device_id": VALID_ZONES[0]["valve_device_id"], "dehumidifier_device_id": None}
    with pytest.raises(ValueError, match="duplicate id"):
        validate_extra_switches(switches, zones=VALID_ZONES)


def test_empty_extra_switches_dict_is_valid():
    validate_extra_switches({})  # must not raise


def test_build_extra_switch_integrations_shape():
    integrations = build_extra_switch_integrations(VALID_EXTRA_SWITCHES, "mqtt_broker.json")
    dehumidifier = next(i for i in integrations if i.options["name"] == "Dehumidifier")

    assert dehumidifier.kind == "switch"
    assert dehumidifier.type == "mqtt_json"
    assert dehumidifier.options["state_topic"] == "shellyplus1-EEEE/status/switch:0"
    assert dehumidifier.options["command_topic"] == "shellyplus1-EEEE/command/switch:0"
    assert dehumidifier.options["payload_on"] is True
    assert dehumidifier.options["payload_off"] is False
    assert dehumidifier.options["availability_topic"] == "shellyplus1-EEEE/online"


def test_build_extra_switch_integrations_skips_an_unset_field():
    integrations = build_extra_switch_integrations({"bypass_valve_device_id": None, "dehumidifier_device_id": None}, "mqtt_broker.json")
    assert integrations == []


# ----------------------------------------------------- extra sensor ----
# One more fixed, standalone Shelly H&T Gen3 sensor every "reference
# model" apartment eventually gets (outdoor temperature/humidity) - not
# part of either zone (no valve, no thermostat decision), no
# simulated/manual stand-in (see services/climate_map_store.py's
# EXTRA_SENSOR_FIELDS/module docstring).

def test_extra_sensors_is_empty_when_nothing_saved_yet(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    assert store.get_extra_sensors() == {}


def test_extra_sensors_round_trip(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_extra_sensors(VALID_EXTRA_SENSORS)
    assert store.get_extra_sensors() == VALID_EXTRA_SENSORS


def test_saving_extra_sensors_does_not_wipe_previously_saved_zones_or_switches(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_zones(VALID_ZONES)
    store.set_extra_switches(VALID_EXTRA_SWITCHES)
    store.set_extra_sensors(VALID_EXTRA_SENSORS)
    assert store.get_zones() == VALID_ZONES
    assert store.get_extra_switches() == VALID_EXTRA_SWITCHES
    assert store.get_extra_sensors() == VALID_EXTRA_SENSORS


def test_rejects_a_blank_extra_sensor_device_id():
    with pytest.raises(ValueError, match="outdoor_sensor_device_id"):
        validate_extra_sensors({"outdoor_sensor_device_id": "  "})


def test_empty_extra_sensors_dict_is_valid():
    validate_extra_sensors({})  # must not raise


def test_rejects_an_extra_sensor_device_id_already_used_by_a_zone():
    sensors = {"outdoor_sensor_device_id": VALID_ZONES[0]["sensor_device_id"]}
    with pytest.raises(ValueError, match="duplicate id"):
        validate_extra_sensors(sensors, zones=VALID_ZONES)


def test_rejects_an_extra_sensor_device_id_already_used_by_an_extra_switch():
    sensors = {"outdoor_sensor_device_id": VALID_EXTRA_SWITCHES["bypass_valve_device_id"]}
    with pytest.raises(ValueError, match="duplicate id"):
        validate_extra_sensors(sensors, extra_switches=VALID_EXTRA_SWITCHES)


def test_rejects_an_extra_switch_device_id_already_used_by_the_extra_sensor():
    # The cross-check works the other direction too - set_extra_switches()
    # passes the saved extra sensor in, not just zones.
    switches = {"bypass_valve_device_id": VALID_EXTRA_SENSORS["outdoor_sensor_device_id"], "dehumidifier_device_id": None}
    with pytest.raises(ValueError, match="duplicate id"):
        validate_extra_switches(switches, extra_sensors=VALID_EXTRA_SENSORS)


def test_saving_a_zone_device_id_that_collides_with_a_saved_extra_sensor_is_rejected(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_extra_sensors(VALID_EXTRA_SENSORS)
    colliding_zones = [
        {**VALID_ZONES[0], "sensor_device_id": VALID_EXTRA_SENSORS["outdoor_sensor_device_id"]},
        VALID_ZONES[1],
    ]
    with pytest.raises(ValueError, match="duplicate id"):
        store.set_zones(colliding_zones)
    assert store.get_zones() is None


def test_build_extra_sensor_integrations_shape():
    integrations = build_extra_sensor_integrations(VALID_EXTRA_SENSORS, "mqtt_broker.json")
    (outdoor,) = integrations

    assert outdoor.kind == "climate"
    assert outdoor.type == "mqtt_json"
    assert outdoor.options["name"] == "Outdoor Sensor"
    assert outdoor.options["state_topic"] == [
        "shellyht-FFFF/status/temperature:0",
        "shellyht-FFFF/status/humidity:0",
        "shellyht-FFFF/status/devicepower:0",
        "shellyht-FFFF/status/wifi",
    ]
    assert outdoor.options["fields"]["wifi_rssi_dbm"] == "rssi"
    # No availability_topic - a battery H&T's own "online" LWT topic
    # reads false for most of its normal working life (it only connects
    # to publish, not continuously) - see _shelly_sensor_options().
    assert "availability_topic" not in outdoor.options


def test_build_extra_sensor_integrations_skips_an_unset_field():
    integrations = build_extra_sensor_integrations({"outdoor_sensor_device_id": None}, "mqtt_broker.json")
    assert integrations == []


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


def test_the_duplicate_zone_name_error_names_the_actual_offending_value():
    zones = [VALID_ZONES[0], dict(VALID_ZONES[1], name=VALID_ZONES[0]["name"])]
    with pytest.raises(ValueError, match=VALID_ZONES[0]["name"]):
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


def test_valve_mode_defaults_to_shelly_when_absent():
    # Every fixture above omits valve_mode entirely - a saved mapping
    # from before this field existed must still validate the same way.
    assert "valve_mode" not in VALID_ZONES[0]
    validate_zones(VALID_ZONES)  # must not raise


def test_rejects_an_unknown_valve_mode():
    zones = [dict(VALID_ZONES[0], valve_mode="cloud"), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="valve_mode"):
        validate_zones(zones)


def test_rejects_a_missing_valve_device_id_in_shelly_mode():
    zones = [dict(VALID_ZONES[0], valve_mode="shelly", valve_device_id=None), VALID_ZONES[1]]
    with pytest.raises(ValueError, match="valve_device_id"):
        validate_zones(zones)


def test_does_not_require_a_valve_device_id_in_simulated_valve_mode():
    zones = [dict(VALID_ZONES[0], valve_mode="simulated", valve_device_id=None), VALID_ZONES[1]]
    validate_zones(zones)  # must not raise


def test_a_fully_hardware_free_zone_is_valid_for_debugging():
    # sensor_mode="manual" + valve_mode="simulated" together need no
    # device id at all - the exact "verify the control logic before any
    # relay is wired" case this pair of modes exists for.
    zones = [
        {"name": "Rooms", "sensor_mode": "manual", "sensor_device_id": None, "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
        VALID_ZONES[1],
    ]
    validate_zones(zones)  # must not raise


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
        "shellyht-AAAA/status/wifi",
    ]
    assert sensor.options["fields"] == {
        "temperature_c": "tC",
        "humidity_pct": "rh",
        "battery_pct": "battery.percent",
        "wifi_rssi_dbm": "rssi",
    }
    assert sensor.options["stale_after_seconds"] == 14400
    # No availability_topic on a battery H&T - see _shelly_sensor_options()'s
    # own comment for why (its "online" LWT topic reads false for most
    # of its normal working life, not just when something's wrong).
    assert "availability_topic" not in sensor.options


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
    assert valve.options["availability_topic"] == "shellyplus1-CCCC/online"


def test_build_climate_integrations_simulated_mode_shape():
    zone = dict(VALID_ZONES[0], sensor_mode="simulated", sensor_device_id=None)
    integrations = build_climate_integrations([zone], "mqtt_broker.json", "manual_override.json")
    sensor = next(i for i in integrations if i.options["name"] == "Rooms Sensor")

    assert sensor.kind == "climate"
    assert sensor.type == "simulated"
    assert sensor.options == {"name": "Rooms Sensor", "dashboard_tab": "climate"}


def test_build_climate_integrations_simulated_valve_shape():
    zone = dict(VALID_ZONES[0], valve_mode="simulated", valve_device_id=None)
    integrations = build_climate_integrations([zone], "mqtt_broker.json", "manual_override.json")
    valve = next(i for i in integrations if i.options["name"] == "Rooms Valve")

    assert valve.kind == "switch"
    assert valve.type == "simulated"
    assert valve.options == {"name": "Rooms Valve", "dashboard_tab": "climate"}


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


# --------------------------------------------- snapshot_from_config ----
# Pre-fills the panel's form from config.yaml's *current* hand-written
# setup (real device ids included) instead of a blank one - read-only,
# never writes back to config.yaml or touches the real device.

def test_snapshot_reads_a_real_shelly_sensor_and_valve():
    zones = [Zone(name="Rooms", sensor="Rooms Sensor", valve="Rooms Valve", cool_enabled=True)]
    integrations = [
        Integration("climate", "mqtt_json", {
            "name": "Rooms Sensor",
            "state_topic": ["shellyht-AAAA/status/temperature:0", "shellyht-AAAA/status/humidity:0"],
        }),
        Integration("switch", "mqtt_json", {"name": "Rooms Valve", "state_topic": "shellyplus1-BBBB/status/switch:0"}),
    ]

    snapshot = snapshot_from_config(zones, integrations)

    assert snapshot[0] == {
        "name": "Rooms", "sensor_mode": "shelly", "sensor_device_id": "shellyht-AAAA",
        "valve_mode": "shelly", "valve_device_id": "shellyplus1-BBBB", "cool_enabled": True,
    }


def test_snapshot_reads_a_simulated_sensor_and_valve():
    zones = [Zone(name="Rooms", sensor="Rooms Sensor", valve="Rooms Valve", cool_enabled=False)]
    integrations = [
        Integration("climate", "simulated", {"name": "Rooms Sensor"}),
        Integration("switch", "simulated", {"name": "Rooms Valve"}),
    ]

    snapshot = snapshot_from_config(zones, integrations)

    assert snapshot[0] == {
        "name": "Rooms", "sensor_mode": "simulated", "sensor_device_id": None,
        "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False,
    }


def test_snapshot_pads_up_to_zone_count_with_blank_entries():
    zones = [Zone(name="Rooms", sensor="Rooms Sensor", valve="Rooms Valve")]

    snapshot = snapshot_from_config(zones, [])

    assert len(snapshot) == 2
    # Simulated: there is no second zone in config.yaml, so there is no
    # real device behind this slot - and a "shelly" slot with no device
    # id is one the panel would refuse to save.
    assert snapshot[1] == {
        "name": "", "sensor_mode": "simulated", "sensor_device_id": None,
        "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True,
    }


def test_snapshot_falls_back_to_an_empty_shelly_slot_for_an_unknown_driver_type():
    zones = [Zone(name="Rooms", sensor="Rooms Sensor", valve="Rooms Valve")]
    integrations = [Integration("climate", "tuya_air_quality", {"name": "Rooms Sensor"})]

    snapshot = snapshot_from_config(zones, integrations)

    assert snapshot[0]["sensor_mode"] == "shelly"
    assert snapshot[0]["sensor_device_id"] is None
    assert snapshot[0]["valve_mode"] == "shelly"
    assert snapshot[0]["valve_device_id"] is None


# ---------------------------------------------------------------- VMC ----
# The ventilation unit's Shelly 0/1-10V dimmer - one device id, two
# integrations (see services/climate_map_store.py's EXTRA_DIMMER_FIELDS).

def test_vmc_builds_a_switch_and_its_paired_speed():
    switch, speed = build_extra_dimmer_integrations({"vmc_device_id": " shelly0110dimg3-AAAA "}, "broker.json")

    assert (switch.kind, switch.type, switch.options["name"]) == ("switch", "shelly_light", "VMC")
    assert (speed.kind, speed.type, speed.options["name"]) == ("number", "shelly_light", "VMC Speed")
    assert speed.options["paired_switch"] == "VMC"
    assert switch.options["device_id"] == speed.options["device_id"] == "shelly0110dimg3-AAAA"
    assert switch.options["broker_file"] == "broker.json"


def test_vmc_not_built_while_blank():
    assert build_extra_dimmer_integrations({"vmc_device_id": None}, "broker.json") == []
    assert build_extra_dimmer_integrations({}, "broker.json") == []


def test_vmc_device_id_may_not_double_up_with_another_role(tmp_path):
    with pytest.raises(ValueError, match="duplicate id"):
        validate_extra_dimmers({"vmc_device_id": "shellyplus1-DDDD"}, extra_switches=VALID_EXTRA_SWITCHES)
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_extra_dimmers({"vmc_device_id": "shelly0110dimg3-AAAA"})
    # ...and the other way round: a later extra switch can't take its id.
    with pytest.raises(ValueError, match="duplicate id"):
        store.set_extra_switches({"bypass_valve_device_id": "shelly0110dimg3-AAAA"})
    assert store.get_extra_dimmers() == {"vmc_device_id": "shelly0110dimg3-AAAA"}


def test_panel_device_replaces_a_hand_written_entry_for_the_same_device(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
integrations:
  - kind: switch
    type: shelly_light
    name: "VMC Dimmer"
    broker_file: "mqtt_broker.json"
    device_id: "shelly0110dimg3-AAAA"
  - kind: number
    type: shelly_light
    name: "VMC Dimmer Speed"
    broker_file: "mqtt_broker.json"
    device_id: "shelly0110dimg3-AAAA"
    paired_switch: "VMC Dimmer"
  - kind: switch
    type: simulated
    name: "Something Else"
""",
        encoding="utf-8",
    )
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_extra_dimmers({"vmc_device_id": "shelly0110dimg3-AAAA"})
    config = load_config(tmp_path / "config.yaml")

    apply_to_config(config)

    names = [i.options["name"] for i in config.integrations]
    assert names == ["Something Else", "VMC", "VMC Speed"]


def test_integration_device_id_reads_rpc_ids_and_topic_prefixes():
    assert integration_device_id(Integration("switch", "shelly_light", {"device_id": "dimmer-1"})) == "dimmer-1"
    assert integration_device_id(Integration("switch", "mqtt_json", {"state_topic": "relay-1/status/switch:0"})) == "relay-1"
    assert integration_device_id(Integration("switch", "simulated", {"name": "x"})) is None


def test_vmc_card_has_modes_and_no_toggle():
    switch, speed = build_extra_dimmer_integrations({"vmc_device_id": "shelly0110dimg3-AAAA"}, "broker.json")

    assert switch.options["hide_toggle"] is True
    assert [(p["key"], p["value"]) for p in speed.options["presets"]] == [("night", 20), ("day", 50), ("boost", 100)]
    assert speed.options["on_with_value"] is True


def test_extra_switches_have_no_fixed_season_of_their_own():
    """Their season rules decide when the dashboard shows them (hidden in
    a season whose rule is "off") - a fixed visible_in_mode would hide a
    dehumidifier someone made usable in winter too."""
    for integration in build_extra_switch_integrations(VALID_EXTRA_SWITCHES, "broker.json"):
        assert "visible_in_mode" not in integration.options



@pytest.mark.parametrize("name, ok", [
    ("Camera dell'ospite", True),          # apostrophes are how Italian names rooms
    ('Bagno "grande"', True),
    ("Più è", True),
    ("Bagno/2", False),                     # a slash splits the zone's URLs
    ("Bagno\\2", False),
    ("Bagno\n2", False),
    ("x" * 41, False),
])
def test_zone_names_people_really_use(name, ok):
    zones = [
        {"name": name, "sensor_mode": "simulated", "valve_mode": "simulated", "cool_enabled": True},
        {"name": "Altro", "sensor_mode": "simulated", "valve_mode": "simulated", "cool_enabled": True},
    ]
    if ok:
        validate_zones(zones)
    else:
        with pytest.raises(ValueError):
            validate_zones(zones)



# ------------------------------------------------------------ season rules --

def test_extra_switch_rules_start_from_the_defaults(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    assert store.get_season_rules() == {
        "bypass_valve_device_id": {"heat": "off", "cool": "on"},
        "dehumidifier_device_id": {"heat": "off", "cool": "manual"},
    }


def test_a_rule_is_saved_per_season_and_checked(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_season_rules({"dehumidifier_device_id": {"heat": "manual", "cool": "on"}})
    assert store.get_season_rules()["dehumidifier_device_id"] == {"heat": "manual", "cool": "on"}
    assert store.get_season_rules()["bypass_valve_device_id"] == {"heat": "off", "cool": "on"}, "the other untouched"
    for bad in ({"dehumidifier_device_id": {"heat": "sometimes", "cool": "on"}},
                {"dehumidifier_device_id": {"heat": "off"}},
                {"lamp": {"heat": "off", "cool": "on"}},
                ["not", "a", "dict"]):
        with pytest.raises(ValueError):
            store.set_season_rules(bad)


def test_only_wired_switches_get_a_panel_rule_and_config_yaml_wins():
    from localhome.services.climate_map_store import panel_season_rules
    rules = {"bypass_valve_device_id": {"heat": "off", "cool": "on"},
             "dehumidifier_device_id": {"heat": "off", "cool": "manual"}}
    wired = {"bypass_valve_device_id": "shellyplus1-aaa", "dehumidifier_device_id": None}
    assert panel_season_rules(wired, rules, {"mode": "heat"}) == [
        {"role": "bypass_valve_device_id", "switch": "Bathroom Bypass Valve", "heat": "off", "cool": "on"}]
    mine = {"mode_switches": [{"switch": "Bathroom Bypass Valve", "active_in_mode": "heat"}]}
    assert panel_season_rules(wired, rules, mine) == [], "config.yaml's own rule for it stays"
    assert panel_season_rules(wired, rules, None) == [], "no thermostat, no season"
