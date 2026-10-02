"""The dehumidifier draws its chilled water through a zone's valve - the
bathrooms' - so while it runs, in summer, that valve is open, even
though the zone itself sits out the cooling; and a bypass valve that is
normally open says "closed" when its Shelly is on (services/interlock.py,
services/climate_map_store.py, the pages' season effects)."""
import json
import time

import pytest

from localhome.config import load_config
from localhome.services.climate_map_store import (
    BYPASS_NORMALLY_OPEN_KEY,
    DEHUMIDIFIER_ZONE_KEY,
    ClimateMapStore,
    apply_to_config,
    build_extra_switch_integrations,
    dehumidifier_water_zone,
)
from localhome.web.app import create_app

ZONES = [
    {"name": "Stanze", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]

# The hand-written form, as on the production board.
CONFIG = """
web:
  port: 5000
integrations:
  - kind: switch
    type: simulated
    name: "Deumidificatore"
  - kind: switch
    type: simulated
    name: "Valvola Bypass Bagni"
    normally_open: true
thermostat:
  mode: "cool"
  schedules_file: "schedules.json"
  poll_interval_seconds: 600
  mode_switches:
    - switch: "Valvola Bypass Bagni"
      active_in_mode: "cool"
  mode_limited_switches:
    - switch: "Deumidificatore"
      only_in_mode: "cool"
  interlocks:
    - leader: "Deumidificatore"
      follower: "Bagni Valve"
      active_in_mode: "cool"
"""


# ------------------------------------------------------------ the panel --

def test_the_water_comes_by_default_from_the_one_zone_kept_out_of_cooling():
    assert dehumidifier_water_zone({}, ZONES) == "Bagni"
    assert dehumidifier_water_zone({DEHUMIDIFIER_ZONE_KEY: None}, ZONES) is None, "saved as none"
    assert dehumidifier_water_zone({DEHUMIDIFIER_ZONE_KEY: "Stanze"}, ZONES) == "Stanze"
    assert dehumidifier_water_zone({}, [dict(z, cool_enabled=True) for z in ZONES]) is None, "no zone kept out"


def test_the_panels_bypass_is_normally_open_unless_said_otherwise():
    wired = {"bypass_valve_device_id": "shellyplus1-aabbccddeeff"}
    (bypass,) = build_extra_switch_integrations(wired, "broker.json")
    assert bypass.options["normally_open"] is True
    (bypass,) = build_extra_switch_integrations({**wired, BYPASS_NORMALLY_OPEN_KEY: False}, "broker.json")
    assert "normally_open" not in bypass.options


def test_the_panels_dehumidifier_holds_its_zones_valve_open_in_summer(tmp_path):
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_zones(ZONES)
    store.set_extra_switches({"dehumidifier_device_id": "shellyplus1-aabbccddeeff"})

    thermostat_cfg = apply_to_config(load_config(tmp_path / "config.yaml"))

    assert thermostat_cfg["interlocks"] == [{"leader": "Dehumidifier", "follower": "Bagni Valve", "active_in_mode": "cool"}]


def test_no_link_when_the_panel_says_none_or_config_yaml_already_links_it(tmp_path):
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_zones(ZONES)
    store.set_extra_switches({"dehumidifier_device_id": "shellyplus1-aabbccddeeff", DEHUMIDIFIER_ZONE_KEY: None})
    assert not apply_to_config(load_config(tmp_path / "config.yaml")).get("interlocks")

    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\nthermostat:\n  interlocks:\n"
                                          "    - {leader: Dehumidifier, follower: Stanze Valve}\n", encoding="utf-8")
    store.set_extra_switches({"dehumidifier_device_id": "shellyplus1-aabbccddeeff"})
    assert apply_to_config(load_config(tmp_path / "config.yaml"))["interlocks"] == [
        {"leader": "Dehumidifier", "follower": "Stanze Valve"}]


def test_a_water_zone_that_isnt_a_zone_is_refused(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_zones(ZONES)
    with pytest.raises(ValueError) as error:
        store.set_extra_switches({"dehumidifier_device_id": "shellyplus1-aabbccddeeff", DEHUMIDIFIER_ZONE_KEY: "Cucina"})
    assert error.value.code == "dehumidifier_zone_invalid"


# ---------------------------------------------------- the running board --

@pytest.fixture
def board(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(ZONES)
    app = create_app(str(tmp_path / "config.yaml"))
    yield app, app.test_client()
    app.config["HISTORY"].close()


def _valve(client, name="Bagni Valve"):
    return client.get(f"/api/sensors/{name.replace(' ', '%20')}").get_json().get("is_on")


def _settle(app):
    """One thermostat and one interlock cycle, as the loops would run them."""
    app.config["THERMOSTAT"].tick()
    app.config["INTERLOCK"].tick()
    app.config["THERMOSTAT"].tick()
    time.sleep(0.05)


def test_dehumidifier_on_opens_the_bathrooms_valve_and_off_closes_it(board):
    app, client = board
    _settle(app)
    assert _valve(client) is False, "a zone kept out of cooling stays closed"

    client.post("/api/sensors/Deumidificatore/on")
    _settle(app)
    assert _valve(client) is True, "the thermostat doesn't close it: the dehumidifier needs its water"

    client.post("/api/sensors/Deumidificatore/off")
    _settle(app)
    assert _valve(client) is False


def test_in_winter_the_link_lets_the_thermostat_have_the_valve(board):
    app, client = board
    app.config["THERMOSTAT"].schedule_store.set_mode("heat")
    client.post("/api/sensors/Deumidificatore/on")      # forced off in winter anyway
    _settle(app)
    zones = {z["name"]: z for z in client.get("/api/climate/zones").get_json()["zones"]}
    assert zones["Bagni"]["valve_driver"] is None


def test_the_pages_know_the_bypass_is_normally_open_and_what_the_dehumidifier_opens(board):
    app, client = board
    client.post("/api/sensors/Deumidificatore/on")
    _settle(app)
    body = client.get("/api/climate/zones").get_json()
    rules = {r["switch"]: r for r in body["season_effects"]["rules"]}
    assert rules["Valvola Bypass Bagni"]["normally_open"] is True and rules["Deumidificatore"]["normally_open"] is False
    assert body["season_effects"]["links"] == [
        {"leader": "Deumidificatore", "follower": "Bagni Valve", "zone": "Bagni", "mode": "cool"}]
    bagni = next(z for z in body["zones"] if z["name"] == "Bagni")
    assert bagni["valve_driver"] == {"name": "Deumidificatore", "ok": True, "on": True}
    devices = {d["name"]: d for d in client.get("/api/devices").get_json()["sensors"]}
    assert devices["Valvola Bypass Bagni"]["normally_open"] is True
    assert devices["Deumidificatore"]["linked_valve"] == "Bagni Valve"
    # In summer its Shelly is on: the valve closed.
    assert _valve(client, "Valvola Bypass Bagni") is True


def test_the_valve_opens_as_soon_as_the_dehumidifier_is_switched_on(board):
    """Not at the next interlock cycle, a minute later."""
    app, client = board
    _settle(app)
    client.post("/api/sensors/Deumidificatore/on")
    assert _valve(client) is True


# ------------------------------------------------- summer, winter, summer --

def _plant(client):
    on = lambda name: client.get(f"/api/sensors/{name.replace(' ', '%20')}").get_json().get("is_on")
    return {"bypass": on("Valvola Bypass Bagni"), "dehumidifier": on("Deumidificatore"),
            "stanze": on("Stanze Valve"), "bagni": on("Bagni Valve")}


def _room(client, zone, temperature):
    assert client.post(f"/api/climate-admin/zones/{zone}/manual-value", json={"temperature_c": temperature}).get_json()["ok"]


def test_every_season_switch_applies_the_whole_plant_at_once(tmp_path):
    """Summer: the bypass powered (closed), the bathrooms' valve powered
    if and only if the dehumidifier is on, the rooms' valve by temperature.
    Winter: only the two zone valves, by temperature - the bypass and the
    dehumidifier off. Checked right after each switch, no loop waited for."""
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones([dict(z, sensor_mode="manual") for z in ZONES])
    app = create_app(str(tmp_path / "config.yaml"))
    client = app.test_client()
    store = app.config["THERMOSTAT"].schedule_store
    every_hour = lambda target: {d: [target] * 24 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    for zone in ("Stanze", "Bagni"):
        store.set_week(zone, every_hour(21.0), mode="heat")
    store.set_week("Stanze", every_hour(24.0), mode="cool")
    try:
        # Summer, warm rooms: the rooms cool, the bathrooms don't.
        client.post("/api/climate/mode", json={"mode": "cool"})
        _room(client, "Stanze", 27.0)
        _room(client, "Bagni", 27.0)
        assert _plant(client) == {"bypass": True, "dehumidifier": False, "stanze": True, "bagni": False}
        client.post("/api/sensors/Deumidificatore/on")
        assert _plant(client) == {"bypass": True, "dehumidifier": True, "stanze": True, "bagni": True}

        # Winter, the rooms cold and the bathrooms warm: by temperature only.
        _room(client, "Stanze", 18.0)
        _room(client, "Bagni", 23.0)
        client.post("/api/climate/mode", json={"mode": "heat"})
        assert _plant(client) == {"bypass": False, "dehumidifier": False, "stanze": True, "bagni": False}
        _room(client, "Bagni", 18.0)
        assert _plant(client)["bagni"] is True, "in winter the bathrooms heat like any zone"

        # Summer again: the dehumidifier stays off until someone turns it
        # on, so the bathrooms' valve closes; the cold rooms need no cooling.
        client.post("/api/climate/mode", json={"mode": "cool"})
        assert _plant(client) == {"bypass": True, "dehumidifier": False, "stanze": False, "bagni": False}
    finally:
        app.config["HISTORY"].close()
