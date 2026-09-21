"""Settings changed from the admin panel override config.yaml's - within
safe ranges, and only once LocalHome restarts."""
import pytest

from localhome.config import load_config
from localhome.services import panel_settings
from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
  language: "en"
integrations:
  - kind: climate
    type: simulated
    name: "Rooms Sensor"
  - kind: switch
    type: simulated
    name: "Rooms Valve"
thermostat:
  hysteresis_c: 0.5
  zones:
    - name: "Rooms"
      sensor: "Rooms Sensor"
      valve: "Rooms Valve"
history:
  retention_days: 30
"""


def _config(tmp_path, text=CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("values, message", [
    ({"hysteresis_c": 5}, "between"),
    ({"history_max_db_mb": 10}, "between"),
    ({"language": "fr"}, "one of"),
    ({"sensor_failsafe_hours": "soon"}, "number"),
    ({"nope": 1}, "unknown"),
])
def test_out_of_range_values_are_refused(values, message):
    with pytest.raises(ValueError, match=message):
        panel_settings.validate(values)


def test_blank_means_not_set_here():
    assert panel_settings.validate({"hysteresis_c": "", "language": None, "history_retention_days": "45"}) == {
        "history_retention_days": 45}


def test_effective_values_say_where_they_come_from(tmp_path):
    config = load_config(_config(tmp_path))
    rows = {r["key"]: r for r in panel_settings.effective(config, {"history_max_db_mb": 500})}
    assert (rows["history_max_db_mb"]["value"], rows["history_max_db_mb"]["source"]) == (500, "panel")
    assert (rows["hysteresis_c"]["value"], rows["hysteresis_c"]["source"]) == (0.5, "config")
    assert (rows["sensor_failsafe_hours"]["value"], rows["sensor_failsafe_hours"]["source"]) == (24, "default")


def test_saved_settings_apply_after_a_restart(tmp_path):
    path = _config(tmp_path)
    client = create_app(str(path)).test_client()

    res = client.post("/api/climate-admin/settings", json={"values": {
        "hysteresis_c": 0.8, "sensor_failsafe_hours": 6, "history_retention_days": 14, "language": "it"}})
    assert res.status_code == 200 and res.get_json()["restart_required"] is True
    assert client.get("/api/climate-admin/settings").get_json()["restart_pending"] is True

    app = create_app(str(path))  # the restart
    thermostat = app.config["THERMOSTAT"]
    assert thermostat.hysteresis_c == 0.8 and thermostat.sensor_failsafe_hours == 6
    assert app.config["HISTORY"] is not None
    assert app.config["LOCALHOME_CONFIG"].web_language == "it"
    assert app.test_client().get("/api/climate-admin/settings").get_json()["restart_pending"] is False


def test_a_bad_value_is_refused_and_nothing_saved(tmp_path):
    path = _config(tmp_path)
    client = create_app(str(path)).test_client()
    res = client.post("/api/climate-admin/settings", json={"values": {"hysteresis_c": 9}})
    assert res.status_code == 400
    assert not (tmp_path / "panel_settings.json").exists()


def test_restore_defaults_removes_the_file(tmp_path):
    path = _config(tmp_path)
    client = create_app(str(path)).test_client()
    client.post("/api/climate-admin/settings", json={"values": {"hysteresis_c": 0.8}})
    client.post("/api/climate-admin/settings", json={"values": {}})
    assert not (tmp_path / "panel_settings.json").exists()


def test_a_thermostat_setting_alone_does_not_create_a_thermostat(tmp_path):
    path = _config(tmp_path, "web:\n  port: 5000\nintegrations: []\n")
    (tmp_path / "panel_settings.json").write_text('{"hysteresis_c": 0.8}', encoding="utf-8")
    assert create_app(str(path)).config["THERMOSTAT"] is None
