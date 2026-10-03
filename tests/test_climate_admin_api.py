"""Flask-level tests for the climate admin panel's routes (web/app.py) -
mirrors tests/test_mode_switch_api.py's exact create_app()+tmp_path
pattern. Covers: the panel working alongside an existing config.yaml
thermostat block, the from-scratch case (no thermostat: key at all,
climate_map.json alone stands up the whole thermostat), the manual
debug-value route applying immediately with no restart, and the
validation/rename edge cases services/climate_map_store.py documents.
"""
import json
import socket

import pytest

from localhome.services.climate_map_store import ClimateMapStore
from localhome.services.thermostat import ScheduleStore
from localhome.web.app import create_app
from tests.mqtt_fakes import FakeMqttConnection

CONFIG_WITH_THERMOSTAT = """
web:
  port: 5000

integrations:
  - kind: climate
    type: simulated
    name: "Existing Sensor"
  - kind: switch
    type: simulated
    name: "Existing Valve"

thermostat:
  mode: "heat"
  schedules_file: "schedules.json"
  zones:
    - name: "Existing Zone"
      sensor: "Existing Sensor"
      valve: "Existing Valve"
"""

CONFIG_NO_THERMOSTAT = """
web:
  port: 5000

integrations: []
"""

VALID_ZONES = [
    {"name": "Rooms", "sensor_mode": "simulated", "sensor_device_id": None, "valve_device_id": "shellyplus1-AAAA", "cool_enabled": True},
    {"name": "Bathrooms", "sensor_mode": "manual", "sensor_device_id": None, "valve_device_id": "shellyplus1-BBBB", "cool_enabled": False},
]


@pytest.fixture(autouse=True)
def fake_mqtt(tmp_path, monkeypatch):
    # Every zone's valve is always a real Shelly relay (mqtt_json) even
    # when its sensor is "simulated"/"manual" - see
    # services/climate_map_store.py - so every test in this file needs a
    # broker to resolve, real or fake. Applies to all tests automatically
    # rather than threading a fixture through each one.
    conn = FakeMqttConnection()
    monkeypatch.setattr("localhome.drivers.mqtt.base.get_connection", lambda broker: conn)
    (tmp_path / "mqtt_broker.json").write_text(json.dumps({"host": "test-broker", "port": 1883}), encoding="utf-8")
    return conn


def _app(tmp_path, config_text=CONFIG_WITH_THERMOSTAT):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    return create_app(str(config_path))


# --------------------------------------------------------- broker status ----
# A real loopback socket, not tests/mqtt_fakes.py's FakeMqttConnection -
# that fixture only fakes the MQTT protocol layer, not whether anything
# is actually listening on the TCP port, which is exactly what this
# route checks (see services/mqtt_broker_status.py).

def test_broker_status_route_reports_reachable_when_something_listens(tmp_path):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    (tmp_path / "mqtt_broker.json").write_text(json.dumps({"host": host, "port": port}), encoding="utf-8")
    app = _app(tmp_path)
    client = app.test_client()

    try:
        body = client.get("/api/climate-admin/broker-status").get_json()
    finally:
        server.close()

    assert body["ok"] is True
    assert body["reachable"] is True
    assert body["host"] == host
    assert body["port"] == port


def test_broker_status_route_reports_unreachable_when_nothing_listens(tmp_path):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    _, port = probe.getsockname()
    probe.close()
    (tmp_path / "mqtt_broker.json").write_text(json.dumps({"host": "127.0.0.1", "port": port}), encoding="utf-8")
    app = _app(tmp_path)
    client = app.test_client()

    body = client.get("/api/climate-admin/broker-status").get_json()

    assert body["ok"] is True
    assert body["reachable"] is False
    assert body["host"] == "127.0.0.1"
    assert body["port"] == port


# ------------------------------------------------------------------ GET ----

def test_get_map_reports_not_configured_with_no_saved_mapping(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    body = client.get("/api/climate-admin/map").get_json()

    assert body["ok"] is True
    assert body["configured"] is False
    # A snapshot of config.yaml's own current setup, not a blank form -
    # see services/climate_map_store.py's snapshot_from_config(). The
    # second slot is a blank placeholder since CONFIG_WITH_THERMOSTAT
    # only defines one zone.
    assert body["zones"][0] == {
        "name": "Existing Zone", "sensor_mode": "simulated", "sensor_device_id": None,
        "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True,
    }
    assert body["zones"][1]["name"] == ""


def test_get_map_reports_the_saved_mapping(tmp_path):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path)
    client = app.test_client()

    body = client.get("/api/climate-admin/map").get_json()

    assert body["configured"] is True
    assert body["zones"] == VALID_ZONES
    # Saved before this process started - already what's running.
    assert body["restart_pending"] is False


def test_get_map_reports_restart_pending_after_a_save_the_running_process_hasnt_loaded(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    assert client.get("/api/climate-admin/map").get_json()["restart_pending"] is False

    client.post("/api/climate-admin/map", json={"zones": VALID_ZONES})

    # The save succeeded and is on disk, but *this* running process
    # built its DeviceManager before it existed - exactly the "changed
    # shelly to simulated but the panel still shows the old driver"
    # confusion this field exists to make impossible to miss.
    assert client.get("/api/climate-admin/map").get_json()["restart_pending"] is True


def test_get_extra_switches_reports_restart_pending_after_an_unloaded_save(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    assert client.get("/api/climate-admin/extra-switches").get_json()["restart_pending"] is False

    client.post("/api/climate-admin/extra-switches", json={"dehumidifier_device_id": "shellyplus1-FFFF"})

    assert client.get("/api/climate-admin/extra-switches").get_json()["restart_pending"] is True


# ----------------------------------------------------------------- POST ----

def test_post_map_saves_a_valid_mapping(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/climate-admin/map", json={"zones": VALID_ZONES})

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["restart_required"] is True
    assert body["warnings"] == []
    assert client.get("/api/climate-admin/map").get_json()["zones"] == VALID_ZONES


def test_post_map_rejects_the_wrong_zone_count(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/climate-admin/map", json={"zones": VALID_ZONES[:1]})

    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_post_map_rejects_a_duplicate_device_id(tmp_path):
    zones = [dict(VALID_ZONES[0]), dict(VALID_ZONES[1])]
    zones[1]["valve_device_id"] = zones[0]["valve_device_id"]
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/climate-admin/map", json={"zones": zones})

    assert res.status_code == 400
    assert "duplicate id" in res.get_json()["error"]


def test_post_map_warns_when_a_zone_is_renamed(tmp_path):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path)
    client = app.test_client()

    renamed = [dict(VALID_ZONES[0], name="Renamed Rooms"), VALID_ZONES[1]]
    res = client.post("/api/climate-admin/map", json={"zones": renamed})

    assert res.status_code == 200
    warnings = res.get_json()["warnings"]
    assert any("Renamed Rooms" in w for w in warnings)


def test_renaming_a_zone_orphans_its_existing_schedule_but_fails_safe(tmp_path):
    # Documents the accepted limitation from services/climate_map_store.py:
    # no automatic schedule migration - the old data just sits unused.
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    schedule_store = ScheduleStore(str(tmp_path / "schedules.json"))
    week = ScheduleStore.empty_week()
    week["mon"][8] = 21.0
    schedule_store.set_week("Rooms", week)

    app = _app(tmp_path)
    client = app.test_client()
    renamed = [dict(VALID_ZONES[0], name="Renamed Rooms"), VALID_ZONES[1]]
    client.post("/api/climate-admin/map", json={"zones": renamed})

    app2 = _app(tmp_path)
    thermostat = app2.config["THERMOSTAT"]
    assert thermostat.schedule_store.get_week("Renamed Rooms") == ScheduleStore.empty_week()


# ------------------------------------------------------- from-scratch app ----

def test_climate_map_alone_stands_up_a_thermostat_with_no_thermostat_key_at_all(tmp_path):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)

    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)

    thermostat = app.config["THERMOSTAT"]
    assert thermostat is not None
    names = sorted(z.name for z in thermostat.zones)
    assert names == ["Bathrooms", "Rooms"]
    manager = app.config["MANAGER"]
    assert "Rooms Sensor" in manager.pollers
    assert "Rooms Valve" in manager.switches
    assert "Bathrooms Sensor" in manager.pollers
    assert "Bathrooms Valve" in manager.switches


def test_shelly_mode_zone_builds_a_real_mqtt_json_driver(tmp_path, fake_mqtt):
    conn = fake_mqtt
    zones = [
        {"name": "Rooms", "sensor_mode": "shelly", "sensor_device_id": "shellyht-CCCC",
         "valve_device_id": "shellyplus1-DDDD", "cool_enabled": True},
        VALID_ZONES[1],
    ]
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(zones)

    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)

    manager = app.config["MANAGER"]
    assert "Rooms Sensor" in manager.pollers
    assert "Rooms Valve" in manager.switches
    conn.deliver("shellyht-CCCC/status/temperature:0", json.dumps({"tC": 21.5}))
    # Force one synchronous read instead of racing the background poller
    # thread manager.start() already spawned - see core/poller.py.
    manager.pollers["Rooms Sensor"].poll_once()
    reading = manager.sensor_reading("Rooms Sensor")
    assert reading["ok"] is True
    assert reading["temperature_c"] == 21.5


def test_a_fully_hardware_free_zone_saves_and_builds_simulated_manual_drivers(tmp_path):
    # sensor_mode="manual" + valve_mode="simulated", no device id at all -
    # the "verify the control logic before any relay is wired" case
    # services/climate_map_store.py's module docstring describes.
    zones = [
        {"name": "Rooms", "sensor_mode": "manual", "sensor_device_id": None,
         "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
        VALID_ZONES[1],
    ]
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/climate-admin/map", json={"zones": zones})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    app2 = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    manager = app2.config["MANAGER"]
    assert "Rooms Valve" in manager.switches

    # The manual sensor value applies immediately...
    client2 = app2.test_client()
    res = client2.post("/api/climate-admin/zones/Rooms/manual-value", json={"temperature_c": 30.0})
    assert res.get_json()["ok"] is True

    # ...and the simulated valve is directly testable through the exact
    # same generic route the live status page's manual test buttons use.
    res = client2.post("/api/sensors/Rooms Valve/on")
    assert res.get_json()["ok"] is True
    assert manager.sensor_reading("Rooms Valve")["is_on"] is True


# ------------------------------------------------------------ manual value ----

def test_manual_value_route_applies_immediately_with_no_restart(tmp_path):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()

    res = client.post("/api/climate-admin/zones/Bathrooms/manual-value", json={"temperature_c": 26.5, "humidity_pct": 60.0})

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    reading = app.config["MANAGER"].sensor_reading("Bathrooms Sensor")
    assert reading["ok"] is True
    assert reading["temperature_c"] == 26.5
    assert reading["humidity_pct"] == 60.0
    assert reading["manual_override"] is True


def test_manual_value_route_rejects_a_value_while_diagnostic_mode_is_active(tmp_path):
    # Found during live end-to-end testing of the manual quick-set box:
    # with diagnostic mode on, ThermostatController.tick() is a no-op
    # (see ScheduleStore.get_diagnostic_mode()), so the value applied
    # here would sit there with no visible effect and no explanation -
    # exactly the "why isn't the valve reacting" confusion this route
    # exists to let an installer rule out. Reject instead of silently
    # accepting a value that can't do anything yet.
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()
    app.config["THERMOSTAT"].schedule_store.set_diagnostic_mode(True)

    res = client.post("/api/climate-admin/zones/Bathrooms/manual-value", json={"temperature_c": 26.5})

    assert res.status_code == 409
    assert res.get_json()["ok"] is False
    # Nothing was applied - the sensor still has no reading at all.
    reading = app.config["MANAGER"].sensor_reading("Bathrooms Sensor")
    assert reading["ok"] is False


def test_manual_value_route_rejects_a_zone_not_in_manual_mode(tmp_path):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()

    res = client.post("/api/climate-admin/zones/Rooms/manual-value", json={"temperature_c": 21.0})

    assert res.status_code == 409
    assert res.get_json()["ok"] is False


def test_the_valve_follows_a_manual_value_at_once(tmp_path):
    """What manual mode is for: a temperature written by hand, and the
    thermostat deciding on it straight away - open below the target,
    closed above it - with the zone marked manual for the dashboard."""
    zones = [dict(VALID_ZONES[0], valve_mode="simulated", valve_device_id=None),
             dict(VALID_ZONES[1], valve_mode="simulated", valve_device_id=None)]
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(zones)
    schedules = ScheduleStore(str(tmp_path / "schedules.json"))
    schedules.set_mode("heat")
    schedules.set_week("Bathrooms", {d: [21.0] * 24 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}, mode="heat")
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()
    manager = app.config["MANAGER"]

    client.post("/api/climate-admin/zones/Bathrooms/manual-value", json={"temperature_c": 18.0})
    assert manager.sensor_reading("Bathrooms Valve")["is_on"] is True
    client.post("/api/climate-admin/zones/Bathrooms/manual-value", json={"temperature_c": 23.0})
    assert manager.sensor_reading("Bathrooms Valve")["is_on"] is False

    flags = {z["name"]: z["sensor_manual"] for z in client.get("/api/climate/zones").get_json()["zones"]}
    assert flags == {"Rooms": False, "Bathrooms": True}


def test_manual_value_route_404s_when_no_mapping_is_saved_at_all(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/climate-admin/zones/Rooms/manual-value", json={"temperature_c": 21.0})

    assert res.status_code == 409


def test_manual_value_route_rejects_a_save_not_yet_loaded_by_this_process(tmp_path):
    # This process started with no saved mapping at all - "Rooms" is
    # whatever CONFIG_WITH_THERMOSTAT's own real driver is, not a
    # manual_override one - even though a save moments later claims
    # otherwise. Same "active, not saved" rule every sibling route
    # follows (ACTIVE_CLIMATE_ZONES) - a save always needs a restart
    # before this route (or any other) can act on it.
    app = _app(tmp_path)
    client = app.test_client()
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)

    res = client.post("/api/climate-admin/zones/Rooms/manual-value", json={"temperature_c": 21.0})

    assert res.status_code == 409


# ------------------------------------------------------------ broker config ----
# What the panel shows of config/mqtt_broker.json (written by hand) - see
# services/mqtt_broker_config.py. fake_mqtt's own broker file has host
# "test-broker".

def test_get_broker_reflects_the_current_file(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    body = client.get("/api/climate-admin/broker").get_json()

    assert body["ok"] is True
    assert body["host"] == "test-broker"
    assert body["port"] == 1883
    assert body["has_password"] is False


def test_the_broker_is_not_changed_from_the_web(tmp_path):
    client = _app(tmp_path).test_client()
    assert client.post("/api/climate-admin/broker", json={"host": "x", "port": 1883}).status_code == 405
    assert client.post("/api/climate-admin/broker/test", json={"host": "x", "port": 1883}).status_code in (404, 405)


# ------------------------------------------------------- extra switches ----
# Two more fixed, standalone Shelly Plus 1 on/off switches (bathroom
# bypass valve, dehumidifier), independent of the zone mapping - see
# services/climate_map_store.py's EXTRA_SWITCH_FIELDS.

def test_get_extra_switches_is_null_when_nothing_saved(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    body = client.get("/api/climate-admin/extra-switches").get_json()

    assert body["ok"] is True
    assert body["bypass_valve_device_id"] is None
    assert body["dehumidifier_device_id"] is None


def test_post_extra_switches_saves_and_builds_a_real_driver_on_the_next_restart(tmp_path):
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()

    res = client.post("/api/climate-admin/extra-switches", json={
        "dehumidifier_device_id": "shellyplus1-FFFF",
    })

    assert res.status_code == 200
    assert res.get_json()["restart_required"] is True
    # Like a zone, a saved extra switch only becomes a real driver on the
    # *next* create_app() (a restart) - see web/app.py's create_app().
    app2 = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    assert "Dehumidifier" in app2.config["MANAGER"].switches
    assert "Bathroom Bypass Valve" not in app2.config["MANAGER"].switches


def test_post_extra_switches_rejects_a_duplicate_device_id_with_a_zone(tmp_path):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()

    res = client.post("/api/climate-admin/extra-switches", json={
        "dehumidifier_device_id": VALID_ZONES[0]["valve_device_id"],
    })

    assert res.status_code == 400
    assert "duplicate id" in res.get_json()["error"]


# -------------------------------------------------------- extra sensor ----
# One more fixed, standalone Shelly H&T Gen3 sensor (outdoor
# temperature/humidity), independent of the zone mapping and the extra
# switches above - see services/climate_map_store.py's EXTRA_SENSOR_FIELDS.

def test_get_extra_sensors_is_null_when_nothing_saved(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    body = client.get("/api/climate-admin/extra-sensors").get_json()

    assert body["ok"] is True
    assert body["outdoor_sensor_device_id"] is None


def test_get_extra_sensors_reports_restart_pending_after_an_unloaded_save(tmp_path):
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()

    assert client.get("/api/climate-admin/extra-sensors").get_json()["restart_pending"] is False
    client.post("/api/climate-admin/extra-sensors", json={"outdoor_sensor_device_id": "shellyht-FFFF"})
    assert client.get("/api/climate-admin/extra-sensors").get_json()["restart_pending"] is True


def test_post_extra_sensors_saves_and_builds_a_real_driver_on_the_next_restart(tmp_path):
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()

    res = client.post("/api/climate-admin/extra-sensors", json={
        "outdoor_sensor_device_id": "shellyht-FFFF",
    })

    assert res.status_code == 200
    assert res.get_json()["restart_required"] is True
    # Like a zone or an extra switch, a saved extra sensor only becomes a
    # real driver on the *next* create_app() (a restart).
    app2 = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    assert "Outdoor Sensor" in app2.config["MANAGER"].pollers


def test_post_extra_sensors_rejects_a_duplicate_device_id_with_a_zone(tmp_path):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)
    client = app.test_client()

    res = client.post("/api/climate-admin/extra-sensors", json={
        "outdoor_sensor_device_id": VALID_ZONES[0]["valve_device_id"],
    })

    assert res.status_code == 400
    assert "duplicate id" in res.get_json()["error"]


# --------------------------------------------------------------- restart ----

def test_restart_route_schedules_a_restart_without_actually_restarting(tmp_path, monkeypatch):
    # Never let the real thing run here - os.execv would replace the
    # pytest process itself (see services/self_restart.py).
    called = []
    monkeypatch.setattr("localhome.web.app.schedule_restart", lambda: called.append(True))
    monkeypatch.setattr("localhome.web.app.is_supported", lambda: True)
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/climate-admin/restart")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert called == [True]


def test_restart_route_refuses_cleanly_when_the_platform_cant_restart_safely(tmp_path, monkeypatch):
    # The real is_supported() is False on Windows - see
    # services/self_restart.py's module docstring for why os.execv
    # there reliably leaves two processes bound to the same port
    # instead of actually restarting. The route must refuse before
    # ever calling schedule_restart(), not attempt it and fail loudly.
    called = []
    monkeypatch.setattr("localhome.web.app.schedule_restart", lambda: called.append(True))
    monkeypatch.setattr("localhome.web.app.is_supported", lambda: False)
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/climate-admin/restart")

    assert res.status_code == 501
    body = res.get_json()
    assert body["ok"] is False
    assert body["platform_unsupported"] is True
    assert called == []


# ---------------------------------------------------------------- VMC ----
# The ventilation unit's Shelly 0/1-10V dimmer - see
# services/climate_map_store.py's EXTRA_DIMMER_FIELDS.

CONFIG_WITH_HAND_WIRED_VMC = """
web:
  port: 5000

integrations:
  - kind: switch
    type: shelly_light
    name: "VMC Dimmer"
    broker_file: "mqtt_broker.json"
    device_id: "shelly0110dimg3-AAAA"
"""


@pytest.fixture
def fake_rpc(fake_mqtt, monkeypatch):
    monkeypatch.setattr("localhome.drivers.mqtt.rpc.get_connection", lambda broker: fake_mqtt)
    return fake_mqtt


def test_get_extra_dimmers_suggests_a_hand_wired_vmc(tmp_path, fake_rpc):
    body = _app(tmp_path, CONFIG_WITH_HAND_WIRED_VMC).test_client().get("/api/climate-admin/extra-dimmers").get_json()

    assert body["vmc_device_id"] is None
    assert body["suggested_vmc"] == {"device_id": "shelly0110dimg3-AAAA", "name": "VMC Dimmer"}


def test_post_extra_dimmers_takes_over_the_hand_wired_vmc_on_restart(tmp_path, fake_rpc):
    client = _app(tmp_path, CONFIG_WITH_HAND_WIRED_VMC).test_client()

    res = client.post("/api/climate-admin/extra-dimmers", json={"vmc_device_id": "shelly0110dimg3-AAAA"})

    assert res.status_code == 200
    assert client.get("/api/climate-admin/extra-dimmers").get_json()["restart_pending"] is True
    manager = _app(tmp_path, CONFIG_WITH_HAND_WIRED_VMC).config["MANAGER"]
    assert "VMC" in manager.switches and "VMC Speed" in manager.numbers
    assert manager.paired_switch["VMC Speed"] == "VMC"
    assert "VMC Dimmer" not in manager.switches  # not polled twice under two names


def test_post_extra_dimmers_rejects_a_duplicate_device_id_with_a_zone(tmp_path, fake_rpc):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    client = _app(tmp_path, CONFIG_NO_THERMOSTAT).test_client()

    res = client.post("/api/climate-admin/extra-dimmers", json={"vmc_device_id": VALID_ZONES[0]["valve_device_id"]})

    assert res.status_code == 400
    assert "duplicate id" in res.get_json()["error"]


def test_clearing_an_unloaded_extra_device_is_not_a_pending_restart(tmp_path, fake_rpc):
    client = _app(tmp_path, CONFIG_NO_THERMOSTAT).test_client()

    client.post("/api/climate-admin/extra-dimmers", json={"vmc_device_id": "shelly0110dimg3-AAAA"})
    client.post("/api/climate-admin/extra-dimmers", json={"vmc_device_id": None})
    client.post("/api/climate-admin/extra-sensors", json={"outdoor_sensor_device_id": None})

    assert client.get("/api/climate-admin/extra-dimmers").get_json()["restart_pending"] is False
    assert client.get("/api/climate-admin/extra-sensors").get_json()["restart_pending"] is False


def test_devices_api_exposes_the_vmc_modes_and_hidden_toggle(tmp_path, fake_rpc):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_extra_dimmers({"vmc_device_id": "shelly0110dimg3-AAAA"})
    body = _app(tmp_path, CONFIG_NO_THERMOSTAT).test_client().get("/api/devices").get_json()

    by_name = {s["name"]: s for s in body["sensors"]}
    assert by_name["VMC"]["hide_toggle"] is True
    assert [p["value"] for p in by_name["VMC Speed"]["presets"]] == [25, 50, 100]
    assert "presets" not in by_name["VMC"]


def test_a_command_re_reads_the_device_and_its_paired_partner():
    from localhome.web.app import _wake_poller

    woken = []

    class Poller:
        def __init__(self, name):
            self.name = name

        def wake(self):
            woken.append(self.name)

    class Manager:
        paired_switch = {"VMC Speed": "VMC"}
        pollers = {n: Poller(n) for n in ("VMC", "VMC Speed", "Other")}

    _wake_poller(Manager, "VMC")
    assert sorted(woken) == ["VMC", "VMC Speed"]
    woken.clear()
    _wake_poller(Manager, "VMC Speed")
    assert sorted(woken) == ["VMC", "VMC Speed"]
    woken.clear()
    _wake_poller(Manager, "Other")
    assert woken == ["Other"]



@pytest.mark.parametrize("body", [
    {"temperature_c": 215},                      # a typo for 21.5
    {"temperature_c": -100},
    {"temperature_c": "nan"},                    # float() takes it; JSON can't carry it
    {"temperature_c": "inf"},
    {"temperature_c": 21, "humidity_pct": 140},
])
def test_manual_value_route_refuses_readings_no_room_has(tmp_path, body):
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones(VALID_ZONES)
    app = _app(tmp_path, CONFIG_NO_THERMOSTAT)

    res = app.test_client().post("/api/climate-admin/zones/Bathrooms/manual-value", json=body)

    assert res.status_code == 400
    assert app.config["MANAGER"].sensor_reading("Bathrooms Sensor").get("temperature_c") is None



# ------------------------------------------------------------ season rules --

def _panel_with_extra_switches(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    store.set_zones(VALID_ZONES)
    store.set_extra_switches({"bypass_valve_device_id": "shellyplus1-EEEE", "dehumidifier_device_id": "shellyplus1-FFFF"})
    return _app(tmp_path, CONFIG_NO_THERMOSTAT)


def test_a_bypass_entered_only_in_the_panel_follows_the_season(tmp_path, fake_mqtt):
    """No thermostat block in config.yaml at all: the rule for the panel's
    bypass valve and dehumidifier comes from the panel's defaults."""
    app = _panel_with_extra_switches(tmp_path)
    client = app.test_client()
    rules = client.get("/api/climate/zones").get_json()["season_effects"]["rules"]
    assert [(r["switch"], r["heat"], r["cool"]) for r in rules] == [
        ("Bathroom Bypass Valve", "off", "on"), ("Dehumidifier", "off", "manual")]

    app.config["INTERLOCK"].tick()   # heating: both forced off
    assert ("shellyplus1-EEEE/command/switch:0", "false") in [(t, str(p).lower()) for t, p in fake_mqtt.published]
    devices = {d["name"]: d for d in client.get("/api/devices").get_json()["sensors"]}
    assert devices["Bathroom Bypass Valve"]["controllable"] is False
    assert client.post("/api/sensors/Dehumidifier/on").get_json()["code"] == "switch_mode_controlled"


def test_a_rule_changed_in_the_panel_applies_without_a_restart(tmp_path, fake_mqtt):
    app = _panel_with_extra_switches(tmp_path)
    client = app.test_client()
    res = client.post("/api/climate-admin/season-rules",
                      json={"rules": {"dehumidifier_device_id": {"heat": "manual", "cool": "manual"}}})
    assert res.get_json() == {"ok": True}
    devices = {d["name"]: d for d in client.get("/api/devices").get_json()["sensors"]}
    assert devices["Dehumidifier"]["controllable"] is True, "manual in winter now: the dashboard may switch it"
    roles = {r["role"]: r for r in client.get("/api/climate-admin/season-rules").get_json()["roles"]}
    assert roles["dehumidifier_device_id"]["heat"] == "manual" and roles["dehumidifier_device_id"]["wired"] is True
    res = client.post("/api/climate-admin/season-rules",
                      json={"rules": {"dehumidifier_device_id": {"heat": "maybe", "cool": "on"}}})
    assert res.status_code == 400 and res.get_json()["code"] == "season_rule_invalid"
