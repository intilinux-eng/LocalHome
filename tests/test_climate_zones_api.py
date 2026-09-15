"""GET /api/climate/zones forwards a handful of specific reading fields
into each zone's JSON (see web/app.py) - this locks in that battery_pct
(added for a real Shelly H&T's DevicePower component - see
docs/integrations/mqtt.md) is one of them and defaults to None rather
than being silently dropped for a sensor that doesn't report it.
"""
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
