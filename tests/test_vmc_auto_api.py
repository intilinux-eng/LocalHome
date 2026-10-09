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
