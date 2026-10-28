"""The VMC automatics' routes in the admin panel: read with their
defaults (all off), changed a setting at a time, refused with a reason,
put back to the recommended values."""
import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations: []
"""


@pytest.fixture
def client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    yield app.test_client(), tmp_path
    app.config["HISTORY"].close()


def test_all_off_until_switched_on(client):
    c, _ = client
    data = c.get("/api/climate-admin/vmc-auto").get_json()
    assert data["ok"] and not any(data["settings"][k] for k in ("shower", "humid", "cooling"))
    assert data["settings"] == data["defaults"] and data["ranges"]["shower_rise"] == [3, 30]
    # A new board: no VMC, no outdoor sensor, no zone with a sensor yet.
    assert (data["vmc"], data["outdoor"], data["zones"], data["under_way"]) == (False, False, [], None)


def test_a_setting_at_a_time_kept_on_disk_and_put_back(client):
    c, tmp_path = client
    data = c.post("/api/climate-admin/vmc-auto", json={"shower": True, "shower_rise": 10}).get_json()
    assert data["ok"] and data["settings"]["shower"] and data["settings"]["shower_rise"] == 10
    assert (tmp_path / "vmc_auto.json").exists()
    data = c.post("/api/climate-admin/vmc-auto", json={"reset": True}).get_json()
    assert data["settings"] == data["defaults"] and not (tmp_path / "vmc_auto.json").exists()


@pytest.mark.parametrize("body,code", [
    ({"shower_rise": 99}, "setting_range"),
    ({"quiet_from_hour": 3, "quiet_to_hour": 3}, "vmc_auto_quiet_hours"),
    ({"bathroom_zone": "Cantina"}, "vmc_auto_zone_unknown"),
])
def test_refused_with_a_reason(client, body, code):
    c, _ = client
    res = c.post("/api/climate-admin/vmc-auto", json=body)
    assert res.status_code == 400 and res.get_json()["code"] == code


def _app_with_outdoor(tmp_path, mode):
    """A board whose outdoor sensor is `mode` ("simulated", "none") - set
    in the admin panel, as climate_map.json keeps it."""
    import json
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps({
        "zones": [], "extra_sensors": {"outdoor_sensor_mode": mode, "outdoor_sensor_device_id": None}}), encoding="utf-8")
    return create_app(str(tmp_path / "config.yaml"))


def test_a_simulated_outdoor_sensor_never_moves_the_real_vmc(tmp_path, monkeypatch):
    """Asked: does a simulated outdoor sensor change anything? Its made-up
    air is on the page (marked so), but the automatics that go by the
    outdoor air don't take it - nor do the degree days in Consumi."""
    from localhome.services import run_hours
    app = _app_with_outdoor(tmp_path, "simulated")
    try:
        assert "Outdoor Sensor" in app.config["MANAGER"].pollers, "it's there, on the page"
        assert app.config["VMC_SCHEDULE"].auto.outdoor_sensor is None
        data = app.test_client().get("/api/climate-admin/vmc-auto").get_json()
        assert (data["outdoor"], data["outdoor_simulated"]) == (False, True)
        seen = []
        real = run_hours.year_report
        monkeypatch.setattr(run_hours, "year_report", lambda *a, **k: (seen.append(k.get("outdoor")), real(*a, **k))[1])
        assert app.test_client().get("/api/climate-admin/usage").status_code == 200
        assert seen and all(outdoor is None for outdoor in seen), "no degree days from made-up air"
    finally:
        app.config["HISTORY"].close()


def test_without_an_outdoor_sensor_nothing_is_said_simulated(tmp_path):
    app = _app_with_outdoor(tmp_path, "none")
    try:
        data = app.test_client().get("/api/climate-admin/vmc-auto").get_json()
        assert (data["outdoor"], data["outdoor_simulated"]) == (False, False)
    finally:
        app.config["HISTORY"].close()


def test_the_measured_outdoor_sensor_is_a_real_one():
    from localhome.services.climate_map_store import measured_outdoor

    class Manager:
        def __init__(self, pollers, types):
            self.pollers, self.driver_types = pollers, types
    assert measured_outdoor(Manager({"Outdoor Sensor": 1}, {"Outdoor Sensor": "mqtt_json"})) == "Outdoor Sensor"
    assert measured_outdoor(Manager({"Outdoor Sensor": 1}, {"Outdoor Sensor": "simulated"})) is None
    assert measured_outdoor(Manager({}, {})) is None
