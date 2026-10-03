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


GB = 1_000_000_000


@pytest.mark.parametrize("total, free, history, ceiling", [
    (19.2 * GB, 17 * GB, 48_000_000, 1900),   # antonio: 32 GB eMMC, /home/arduino 19 GB
    (4 * GB, 2.5 * GB, 0, 400),               # a small partition: a tenth of it
    (19.2 * GB, 1 * GB, 0, 300),              # little room left: a third of it
    (0.3 * GB, 0.1 * GB, 0, 50),              # never under the setting's minimum
    (500 * GB, 400 * GB, 0, 2000),            # nor over its maximum
    (None, None, 0, 2000),                    # a disk that can't be read
])
def test_the_history_ceiling_stays_well_inside_the_disk(total, free, history, ceiling):
    disk = {"total_bytes": total and int(total), "free_bytes": free and int(free)}
    assert panel_settings.history_ceiling_mb(disk, history) == ceiling


def test_a_history_size_over_the_disks_ceiling_is_refused():
    ceilings = {"history_max_db_mb": 400}
    assert panel_settings.validate({"history_max_db_mb": 400}, ceilings) == {"history_max_db_mb": 400}
    with pytest.raises(ValueError, match="between 50 and 400"):
        panel_settings.validate({"history_max_db_mb": 450}, ceilings)


def test_a_value_saved_under_a_wider_range_is_brought_inside_not_dropped(tmp_path):
    path = tmp_path / "panel_settings.json"
    path.write_text('{"history_max_db_mb": 5000, "site_name": "Casa"}', encoding="utf-8")
    assert panel_settings.PanelSettingsStore(str(path)).get() == {"history_max_db_mb": 2000, "site_name": "Casa"}


def test_the_recorder_and_the_panel_keep_to_what_the_disk_allows(tmp_path, monkeypatch):
    from localhome.services import system_status
    monkeypatch.setattr(system_status, "disk_stats", lambda path: {
        "path": path, "total_bytes": 4 * GB, "free_bytes": 3 * GB, "low": False})
    path = _config(tmp_path, CONFIG + "  max_db_mb: 1500\n")
    app = create_app(str(path))
    assert app.config["RECORDER"].max_db_bytes == 400_000_000
    client = app.test_client()
    body = client.get("/api/climate-admin/settings").get_json()
    row = next(r for r in body["settings"] if r["key"] == "history_max_db_mb")
    assert (row["value"], row["max"], row["capped_to"]) == (1500, 400, 400)
    assert body["disk"]["total_bytes"] == 4 * GB
    # The same disk the recorder watches, to hold the history if it fills.
    assert app.config["RECORDER"].disk_stats()["total_bytes"] == 4 * GB
    assert body["history"]["disk_hold_bytes"] is None
    assert client.post("/api/climate-admin/settings", json={"values": {"history_max_db_mb": 500}}).status_code == 400
    assert client.post("/api/climate-admin/settings", json={"values": {"history_max_db_mb": 350}}).status_code == 200
