"""GET /api/climate/zones forwards a handful of specific reading fields
into each zone's JSON (see web/app.py) - this locks in that battery_pct
(added for a real Shelly H&T's DevicePower component - see
docs/integrations/mqtt.md) is one of them and defaults to None rather
than being silently dropped for a sensor that doesn't report it - and
that dew_point_c (services/dewpoint.py) gets computed from that same
zone's own temperature/humidity, not left for the frontend to derive.
"""
import time

import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000

integrations:
  - kind: climate
    type: simulated
    name: "Room Sensor"
  - kind: switch
    type: simulated
    name: "Room Valve"

thermostat:
  mode: "heat"
  schedules_file: "schedules.json"
  zones:
    - name: "Rooms"
      sensor: "Room Sensor"
      valve: "Room Valve"
"""


def test_battery_pct_key_is_present_and_defaults_to_none(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()

    body = client.get("/api/climate/zones").get_json()

    zone = next(z for z in body["zones"] if z["name"] == "Rooms")
    assert "battery_pct" in zone
    # The simulated driver doesn't report a battery at all - must not
    # crash, and must not be silently omitted from the response either.
    assert zone["battery_pct"] is None


def test_wifi_rssi_dbm_key_is_present_and_defaults_to_none(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()

    body = client.get("/api/climate/zones").get_json()

    zone = next(z for z in body["zones"] if z["name"] == "Rooms")
    assert "wifi_rssi_dbm" in zone
    assert zone["wifi_rssi_dbm"] is None


def test_dew_point_c_is_computed_from_the_zones_own_reading(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    # A known, exact reading instead of the simulated driver's own read()
    # (which jitters temperature/humidity by a small random amount every
    # call - see drivers/simulated/climate.py - fine for a believable
    # dashboard, not for a deterministic assertion here).
    app.config["MANAGER"].pollers["Room Sensor"].cache.set(
        {"ok": True, "name": "Room Sensor", "temperature_c": 20.0, "humidity_pct": 50.0}
    )
    client = app.test_client()

    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Rooms")

    # Same reference value test_dewpoint.py checks the underlying formula against.
    assert zone["dew_point_c"] == pytest.approx(9.3, abs=0.1)


def test_dew_point_c_is_none_without_a_usable_reading(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    # Force a broken reading deterministically (poll_interval_seconds
    # defaults to 10s for the simulated driver, so the background poller
    # thread won't overwrite this again during the test) instead of
    # racing SyncPoller.start()'s background thread for its first read.
    app.config["MANAGER"].pollers["Room Sensor"].cache.set({"ok": False, "error": "sensor unreachable"})
    client = app.test_client()

    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Rooms")

    assert zone["dew_point_c"] is None


# ------------------------------------------------------- diagnostic mode -----

def test_diagnostic_mode_defaults_to_false_in_the_zones_response(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()

    body = client.get("/api/climate/zones").get_json()

    assert body["diagnostic_mode"] is False
    zone = next(z for z in body["zones"] if z["name"] == "Rooms")
    assert zone["sensor_failsafe_active"] is False
    assert zone["sensor_unreachable_hours"] is None
    assert zone["sensor_failsafe_hours"] == 24.0


def test_enabling_diagnostic_mode_via_the_api_is_reflected_back(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()

    res = client.post("/api/climate/diagnostic-mode", json={"enabled": True})

    assert res.get_json() == {"ok": True, "enabled": True}
    assert client.get("/api/climate/zones").get_json()["diagnostic_mode"] is True


def test_diagnostic_mode_rejects_a_non_boolean_value(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()

    res = client.post("/api/climate/diagnostic-mode", json={"enabled": "yes"})

    assert res.status_code == 400


def test_disabling_diagnostic_mode_resumes_control_immediately(tmp_path):
    # No waiting for the next scheduled tick - the same idiom as every
    # other setter on this thermostat (mode/away/schedule). Manually
    # force the valve open while diagnostic mode is on (as an installer
    # would from the panel), then confirm turning diagnostic mode back
    # off snaps it shut again right away, since this zone has no
    # schedule target set at all - decide() always returns False without
    # one, regardless of the valve's previous state.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()
    client.post("/api/climate/diagnostic-mode", json={"enabled": True})
    client.post("/api/sensors/Room Valve/on")
    assert client.get("/api/climate/zones").get_json()["zones"][0]["valve_on"] is True

    client.post("/api/climate/diagnostic-mode", json={"enabled": False})

    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Rooms")
    assert zone["valve_on"] is False


def test_disabling_diagnostic_mode_closes_a_zone_whose_sensor_is_unreachable(tmp_path):
    # The real bug reported from live testing against the two actual
    # battery H&T sensors (currently offline in the real apartment):
    # with the sensor unreachable, tick()'s own per-zone loop skips
    # that zone entirely (see services/thermostat.py) - forget_valve_state()
    # alone wasn't enough, since tick() never even reaches decide()/
    # _apply() for this zone to be affected by it. The valve stayed
    # open after leaving diagnostic mode even though the temperature
    # (unknown - the sensor is down) could never have justified it.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()
    app.config["MANAGER"].pollers["Room Sensor"].cache.set({"ok": False, "error": "sensor unreachable"})
    client.post("/api/climate/diagnostic-mode", json={"enabled": True})
    client.post("/api/sensors/Room Valve/on")
    assert client.get("/api/climate/zones").get_json()["zones"][0]["valve_on"] is True

    client.post("/api/climate/diagnostic-mode", json={"enabled": False})

    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Rooms")
    assert zone["valve_on"] is False


# ------------------------------------------------- per-season schedules -----

def _week(value):
    return {day: [value] * 24 for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def test_schedule_api_keeps_heating_and_cooling_apart(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    client = create_app(str(config_path)).test_client()

    assert client.post("/api/climate/zones/Rooms/schedule", json={"week": _week(20), "mode": "heat"}).status_code == 200
    assert client.post("/api/climate/zones/Rooms/schedule", json={"week": _week(26), "mode": "cool"}).status_code == 200

    heat = client.get("/api/climate/zones/Rooms/schedule?mode=heat").get_json()
    cool = client.get("/api/climate/zones/Rooms/schedule?mode=cool").get_json()
    assert (heat["mode"], heat["week"]["mon"][0]) == ("heat", 20)
    assert (cool["mode"], cool["week"]["mon"][0]) == ("cool", 26)


def test_schedule_api_defaults_to_the_current_season(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    client = create_app(str(config_path)).test_client()
    client.post("/api/climate/zones/Rooms/schedule", json={"week": _week(20), "mode": "heat"})
    client.post("/api/climate/mode", json={"mode": "cool"})

    body = client.get("/api/climate/zones/Rooms/schedule").get_json()

    # Cooling has nothing painted yet: off, not the winter grid.
    assert body["mode"] == "cool"
    assert all(v is None for v in body["week"]["mon"])
    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Rooms")
    assert zone["target_c"] is None


def test_schedule_api_rejects_an_unknown_season(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    client = create_app(str(config_path)).test_client()

    assert client.get("/api/climate/zones/Rooms/schedule?mode=spring").status_code == 400
    assert client.post("/api/climate/zones/Rooms/schedule", json={"week": _week(20), "mode": "spring"}).status_code == 400


def test_zones_api_names_the_interlock_driving_an_excluded_valve(tmp_path):
    # Cool mode: the bathroom valve is excluded from cooling but fed to a
    # dehumidifier by an interlock - the dashboard needs to know why it
    # might be open.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
web:
  port: 5000
integrations:
  - kind: climate
    type: simulated
    name: "Bath Sensor"
  - kind: switch
    type: simulated
    name: "Bath Valve"
  - kind: switch
    type: simulated
    name: "Dehumidifier"
thermostat:
  mode: cool
  zones:
    - name: "Bathrooms"
      sensor: "Bath Sensor"
      valve: "Bath Valve"
      cool_enabled: false
  interlocks:
    - leader: "Dehumidifier"
      follower: "Bath Valve"
      active_in_mode: "cool"
""",
        encoding="utf-8",
    )
    app = create_app(str(config_path))

    (zone,) = app.test_client().get("/api/climate/zones").get_json()["zones"]

    assert zone["valve_driver"]["name"] == "Dehumidifier"
    assert zone["valve_driver"]["on"] is False
    assert app.config["THERMOSTAT"].interlock_owned_valves == {"Bath Valve": "cool"}


def test_mode_limited_switches_are_read_from_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
web:
  port: 5000
integrations:
  - kind: switch
    type: simulated
    name: "Dehumidifier"
thermostat:
  zones: []
  mode_limited_switches:
    - switch: "Dehumidifier"
      only_in_mode: "cool"
""",
        encoding="utf-8",
    )
    interlock = create_app(str(config_path)).config["INTERLOCK"]

    assert [(m.switch, m.only_in_mode) for m in interlock.mode_limited] == [("Dehumidifier", "cool")]


def test_on_hours_reports_hours_and_starts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    now = int(time.time())
    for i in range(2):
        app.config["HISTORY"].record("Room Valve", now - 1 + i, {"is_on": True})

    body = app.test_client().get("/api/climate/zones/Rooms/on-hours").get_json()

    interval = app.config["HISTORY_POLL_INTERVAL"]
    assert body["ok"] is True
    assert body["today"] == pytest.approx(round(2 * interval / 3600, 2))
    assert body["starts"]["today"] == 1
    assert set(body["starts"]) == {"today", "week", "month"}


# ------------------------------------------------ temporary targets ----------

def test_a_temporary_target_from_the_dashboard(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    client = app.test_client()
    name = app.config["THERMOSTAT"].zones[0].name
    res = client.post(f"/api/climate/zones/{name}/override", json={"target": 22.5, "hours": 2}).get_json()
    assert res["ok"] and res["override"]["target"] == 22.5
    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == name)
    assert zone["target_c"] == 22.5 and zone["override"]["target"] == 22.5
    assert "scheduled_target_c" in zone
    res = client.post(f"/api/climate/zones/{name}/override", json={"target": None, "until": "next_change"}).get_json()
    assert res["ok"] and res["override"]["target"] is None
    assert client.post(f"/api/climate/zones/{name}/override/cancel").get_json() == {"ok": True}
    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == name)
    assert zone["override"] is None
    for body, code in (({"target": 90, "hours": 1}, "override_target_range"),
                       ({"target": True, "hours": 1}, "override_target_range"),
                       ({"target": 21, "hours": 48}, "override_hours_invalid"),
                       ({"target": 21}, "override_hours_invalid")):
        assert client.post(f"/api/climate/zones/{name}/override", json=body).get_json()["code"] == code
    assert client.post("/api/climate/zones/Nowhere/override", json={"target": 21, "hours": 1}).status_code == 404


def test_an_h_and_t_on_usb_c_shows_no_battery(tmp_path):
    """Its devicepower says it's on external power: its battery isn't what
    keeps it going, and isn't shown."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    cache = app.config["MANAGER"].pollers["Room Sensor"].cache
    client = app.test_client()
    cache.set({"ok": True, "name": "Room Sensor", "temperature_c": 21.0, "battery_pct": 89, "external_power": False})
    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Rooms")
    assert zone["battery_pct"] == 89
    cache.set({"ok": True, "name": "Room Sensor", "temperature_c": 21.0, "battery_pct": 89, "external_power": True})
    zone = next(z for z in client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Rooms")
    assert zone["battery_pct"] is None

