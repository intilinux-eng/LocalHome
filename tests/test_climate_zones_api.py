"""GET /api/climate/zones forwards a handful of specific reading fields
into each zone's JSON (see web/app.py) - this locks in that battery_pct
(added for a real Shelly H&T's DevicePower component - see
docs/integrations/mqtt.md) is one of them and defaults to None rather
than being silently dropped for a sensor that doesn't report it - and
that dew_point_c (services/dewpoint.py) gets computed from that same
zone's own temperature/humidity, not left for the frontend to derive.
"""
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
