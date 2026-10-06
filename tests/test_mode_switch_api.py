"""Exercises the web layer's treatment of a mode_switch (services/interlock.py):
it has no owner of its own - only the thermostat's current mode drives it -
so the dashboard must show its state without offering a manual toggle that
would just get overridden on the controller's next tick, and the API must
reject a manual on/off call the same way even if something bypasses the UI -
*unless* diagnostic mode is on, in which case InterlockController itself
backs off (services/thermostat.py's ScheduleStore.get_diagnostic_mode()) and
the same manual call must be let through instead.
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


def test_manually_toggling_a_mode_switch_is_allowed_in_diagnostic_mode(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    app.config["THERMOSTAT"].schedule_store.set_diagnostic_mode(True)

    res = client.post("/api/sensors/Bypass Valve/on")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_who_set_the_season_and_when_is_kept(tmp_path, caplog):
    """On the production board the season changed twice with nobody able
    to say who had done it: now the admin's season card says when, from
    which device, and from what - and so does the log."""
    import logging

    client = _app(tmp_path).test_client()
    assert client.get("/api/climate/zones").get_json()["mode_changed"] is None
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15"
    with caplog.at_level(logging.INFO, logger="localhome.web.app"):
        assert client.post("/api/climate/mode", json={"mode": "heat"}, headers={"User-Agent": iphone}).get_json()["ok"]
    changed = client.get("/api/climate/zones").get_json()["mode_changed"]
    assert changed["from"] == "cool" and changed["by"] == "iPhone (127.0.0.1)" and changed["at"] > 0
    assert "Season set to heat (was cool) from iPhone (127.0.0.1)" in caplog.text
    # Set again to the same: nothing changed, the record stays.
    client.post("/api/climate/mode", json={"mode": "heat"}, headers={"User-Agent": "curl/8"})
    assert client.get("/api/climate/zones").get_json()["mode_changed"] == changed
