"""The outdoor temperature sensor and the VMC: on a climate installation
each starts as its Shelly to configure - nothing built, the home saying
it isn't connected yet - and can be picked simulated, so the outdoor
chart and the VMC's controls exist before the hardware does, or none.

What must never happen: a stand-in replacing or duplicating a real
device wired by hand in config.yaml.
"""
from datetime import datetime

import pytest

from localhome.config import load_config
from localhome.drivers.simulated.climate import SimulatedClimateDriver
from localhome.services.climate_map_store import (
    ClimateMapStore,
    apply_to_config,
    build_extra_dimmer_integrations,
    build_extra_sensor_integrations,
    effective_extra_mode,
    extra_roles,
    open_climate_map,
    validate_extra_dimmers,
    validate_extra_sensors,
)
from localhome.web.app import create_app

ZONES = [
    {"name": "Stanze", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]


def _setup(tmp_path, config_text="web:\n  port: 5000\nintegrations: []\n", zones=True):
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    if zones:
        store.set_zones(ZONES)
    return store


# ------------------------------------------------------ what runs, when ----

@pytest.mark.parametrize("section, default, expected", [
    (None, "shelly", "shelly"),                                      # never saved, climate installation: to configure
    (None, "none", "none"),                                          # never saved, a real one wired by hand
    ({"outdoor_sensor_device_id": "shellyht-A"}, "shelly", "shelly"),  # saved before modes existed
    ({"outdoor_sensor_device_id": None}, "shelly", "none"),          # saved blank before modes: meant "not installed"
    ({"outdoor_sensor_mode": "simulated"}, "none", "simulated"),
    ({"outdoor_sensor_mode": "none", "outdoor_sensor_device_id": "shellyht-A"}, "shelly", "none"),
])
def test_the_effective_source(section, default, expected):
    assert effective_extra_mode(section, "outdoor_sensor_device_id", "outdoor_sensor_mode", default) == expected


def test_a_new_climate_installation_has_both_to_configure(tmp_path):
    store = _setup(tmp_path)
    config = load_config(tmp_path / "config.yaml")

    roles = extra_roles(config, store)
    apply_to_config(config)

    assert roles["outdoor"] == {"mode": "shelly", "device_id": None}
    assert roles["vmc"] == {"mode": "shelly", "device_id": None}
    built = [i.options.get("name") for i in config.integrations]
    assert not {"Outdoor Sensor", "VMC", "VMC Speed"} & set(built), built


OWN_ZONES = """
integrations:
  - kind: climate
    type: simulated
    name: "Rooms Sensor"
  - kind: switch
    type: simulated
    name: "Rooms Valve"
thermostat:
  zones:
    - {name: "Rooms", sensor: "Rooms Sensor", valve: "Rooms Valve"}
"""


def test_a_config_yaml_wiring_its_own_zones_gets_nothing_by_itself(tmp_path):
    _setup(tmp_path, config_text=OWN_ZONES, zones=False)
    config = load_config(tmp_path / "config.yaml")
    store = open_climate_map(config)

    apply_to_config(config)

    assert extra_roles(config, store) == {"outdoor": {"mode": "none", "device_id": None},
                                          "vmc": {"mode": "none", "device_id": None}}
    assert [i.options["name"] for i in config.integrations] == ["Rooms Sensor", "Rooms Valve"]


def test_a_hand_wired_outdoor_sensor_is_never_replaced_by_a_simulated_one(tmp_path):
    store = _setup(tmp_path, config_text="""
integrations:
  - kind: climate
    type: mqtt_json
    name: "Outdoor Sensor"
    broker_file: "mqtt_broker.json"
    state_topic: "shellyht-OUT/status/temperature:0"
""")
    config = load_config(tmp_path / "config.yaml")

    apply_to_config(config)

    assert extra_roles(config, store)["outdoor"]["mode"] == "none"
    outdoor = [i for i in config.integrations if i.options.get("name") == "Outdoor Sensor"]
    assert [i.type for i in outdoor] == ["mqtt_json"], "the real one, alone"


def test_a_hand_wired_dimmer_means_no_simulated_vmc(tmp_path):
    store = _setup(tmp_path, config_text="""
integrations:
  - kind: switch
    type: shelly_light
    name: "VMC Dimmer"
    broker_file: "mqtt_broker.json"
    device_id: "shelly0110dimg3-AAAA"
""")
    config = load_config(tmp_path / "config.yaml")

    assert extra_roles(config, store)["vmc"]["mode"] == "none"


def test_an_id_typed_ahead_while_simulated_changes_nothing_that_runs(tmp_path):
    store = _setup(tmp_path)
    store.set_extra_sensors({"outdoor_sensor_device_id": "shellyht-LATER", "outdoor_sensor_mode": "simulated"})
    config = load_config(tmp_path / "config.yaml")

    assert extra_roles(config, store)["outdoor"] == {"mode": "simulated", "device_id": None}


# ------------------------------------------------------------ what's built --

def test_the_simulated_outdoor_sensor_is_a_climate_tab_sensor():
    (integration,) = build_extra_sensor_integrations({}, "broker.json", "simulated")

    assert (integration.kind, integration.type) == ("climate", "simulated")
    assert integration.options["name"] == "Outdoor Sensor"
    assert integration.options["dashboard_tab"] == "climate"
    assert len(integration.options["monthly_base_c"]) == 12


def test_the_simulated_vmc_has_the_real_ones_card():
    switch, speed = build_extra_dimmer_integrations({}, "broker.json", "simulated")

    assert (switch.kind, switch.type, switch.options["name"]) == ("switch", "simulated", "VMC")
    assert switch.options["hide_toggle"] is True and switch.options["initial_on"] is True
    assert (speed.kind, speed.type, speed.options["name"]) == ("number", "simulated", "VMC Speed")
    assert speed.options["paired_switch"] == "VMC"
    assert [p["key"] for p in speed.options["presets"]] == ["min", "night", "day", "high", "boost"]
    # Like the real dimmer: picking a speed starts it.
    assert speed.options["on_with_value"] is True


def test_none_builds_nothing():
    assert build_extra_sensor_integrations({"outdoor_sensor_device_id": "shellyht-A"}, "b.json", "none") == []
    assert build_extra_dimmer_integrations({"vmc_device_id": "shelly0110dimg3-A"}, "b.json", "none") == []


# --------------------------------------------------------------- checks ----

def test_a_shelly_without_its_id_yet_is_to_configure():
    validate_extra_sensors({"outdoor_sensor_mode": "shelly", "outdoor_sensor_device_id": None})
    validate_extra_dimmers({"vmc_mode": "shelly", "vmc_device_id": None})
    with pytest.raises(ValueError, match="non-empty"):
        validate_extra_dimmers({"vmc_mode": "shelly", "vmc_device_id": "  "})


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError, match="must be one of"):
        validate_extra_sensors({"outdoor_sensor_mode": "fake"})


def test_an_unused_id_cannot_collide():
    zones = [{**ZONES[0], "sensor_mode": "shelly", "sensor_device_id": "shellyht-SAME"}, ZONES[1]]
    validate_extra_sensors({"outdoor_sensor_mode": "simulated", "outdoor_sensor_device_id": "shellyht-SAME"}, zones)
    with pytest.raises(ValueError, match="duplicate"):
        validate_extra_sensors({"outdoor_sensor_mode": "shelly", "outdoor_sensor_device_id": "shellyht-SAME"}, zones)


# --------------------------------------------------- the whole app, and API --

@pytest.fixture
def app(tmp_path):
    _setup(tmp_path)
    app = create_app(str(tmp_path / "config.yaml"))
    yield app
    app.config["HISTORY"].close()


def test_a_new_climate_board_builds_neither_until_configured(app):
    devices = {s["name"]: s for s in app.test_client().get("/api/devices").get_json()["sensors"]}

    assert not {"Outdoor Sensor", "VMC", "VMC Speed"} & set(devices), "the home says they aren't connected yet"
    zones = app.test_client().get("/api/climate/zones").get_json()["zones"]
    assert all(z["sensor_simulated"] for z in zones)


def test_a_simulated_outdoor_sensor_is_there_once_picked(tmp_path):
    store = _setup(tmp_path)
    store.set_extra_sensors({"outdoor_sensor_mode": "simulated", "outdoor_sensor_device_id": None})
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        devices = {s["name"]: s for s in app.test_client().get("/api/devices").get_json()["sensors"]}
    finally:
        app.config["HISTORY"].close()

    assert devices["Outdoor Sensor"]["simulated"] is True
    assert devices["Outdoor Sensor"]["tab"] == "climate"


def test_the_panel_reads_and_saves_the_source(app):
    client = app.test_client()
    assert client.get("/api/climate-admin/extra-sensors").get_json()["outdoor_sensor_mode"] == "shelly"
    assert client.get("/api/climate-admin/extra-dimmers").get_json()["vmc_mode"] == "shelly"
    assert client.get("/api/climate-admin/extra-sensors").get_json()["restart_pending"] is False

    res = client.post("/api/climate-admin/extra-sensors", json={"outdoor_sensor_mode": "simulated"})
    assert res.status_code == 200
    data = client.get("/api/climate-admin/extra-sensors").get_json()
    assert data["outdoor_sensor_mode"] == "simulated" and data["restart_pending"] is True

    # Still no id: to configure, nothing to restart for.
    res = client.post("/api/climate-admin/extra-dimmers", json={"vmc_mode": "shelly", "vmc_device_id": None})
    assert res.status_code == 200
    assert client.get("/api/climate-admin/extra-dimmers").get_json()["restart_pending"] is False


def test_saving_the_same_source_again_is_not_a_pending_restart(app):
    client = app.test_client()
    client.post("/api/climate-admin/extra-sensors", json={"outdoor_sensor_mode": "none",
                                                          "outdoor_sensor_device_id": "shellyht-LATER"})
    assert client.get("/api/climate-admin/extra-sensors").get_json()["restart_pending"] is False


# ------------------------------------------------ a believable outdoors -----

def test_the_simulated_outdoors_follows_the_month_and_the_hour():
    sensor = SimulatedClimateDriver("Outdoor Sensor", monthly_base_c=[8, 9, 11, 14, 18, 23, 26, 26, 22, 18, 13, 9],
                                    daily_swing_c=5.0)
    january_3pm = sensor._resting(datetime(2026, 1, 15, 15, 0))
    january_3am = sensor._resting(datetime(2026, 1, 15, 3, 0))
    july_3pm = sensor._resting(datetime(2026, 7, 15, 15, 0))

    assert (january_3pm, january_3am, july_3pm) == pytest.approx((13.0, 3.0, 31.0))


def test_a_plain_simulated_sensor_is_unchanged():
    sensor = SimulatedClimateDriver("Rooms Sensor", base_temperature_c=20.0)
    assert sensor._resting(datetime(2026, 1, 15, 3, 0)) == 20.0
