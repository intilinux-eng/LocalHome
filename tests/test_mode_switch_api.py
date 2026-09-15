"""Exercises the web layer's treatment of a mode_switch (services/interlock.py):
it has no owner of its own - only the thermostat's current mode drives it -
so the dashboard must show its state without offering a manual toggle that
would just get overridden on the controller's next tick, and the API must
reject a manual on/off call the same way even if something bypasses the UI.
"""
from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000

integrations:
  - kind: switch
    type: simulated
    name: "Bypass Valve"
  - kind: switch
    type: simulated
    name: "Regular Valve"

thermostat:
  mode: "cool"
  schedules_file: "schedules.json"
  mode_switches:
    - switch: "Bypass Valve"
      active_in_mode: "cool"
"""


def _app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    return create_app(str(config_path))


def test_a_mode_switch_is_reported_as_not_controllable(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    body = client.get("/api/devices").get_json()

    entries = {s["name"]: s for s in body["sensors"]}
    assert entries["Bypass Valve"]["controllable"] is False
    assert entries["Regular Valve"]["controllable"] is True


def test_manually_toggling_a_mode_switch_is_rejected(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/sensors/Bypass Valve/on")

    assert res.status_code == 409
    body = res.get_json()
    assert body["ok"] is False


def test_a_regular_switch_can_still_be_toggled(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/sensors/Regular Valve/on")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
