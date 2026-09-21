"""The admin panel's maintenance actions: backups, clearing the history or
the schedules, and the factory reset - each destructive one backed up
first, and the reset removing only what LocalHome itself created."""
import io
import json
import os
import time
import zipfile

import pytest

from localhome.services.history import HistoryStore
from localhome.services.maintenance import KEEP_CONFIG_BACKUPS, Maintenance
from localhome.services.thermostat import ScheduleStore
from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations:
  - kind: climate
    type: simulated
    name: "Rooms Sensor"
  - kind: switch
    type: simulated
    name: "Rooms Valve"
thermostat:
  zones:
    - name: "Rooms"
      sensor: "Rooms Sensor"
      valve: "Rooms Valve"
"""


def _write(path, data):
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write(cfg / "config.yaml", CONFIG)
    _write(cfg / "mqtt_broker.json", {"host": "localhost", "port": 1883, "username": "u", "password": "s3cret"})
    _write(cfg / "secrets_meross.json", {"password": "x"})
    _write(cfg / "devices.json", [{"local_key": "k"}])
    _write(cfg / "config.example.yaml", "example: true")
    _write(cfg / "climate_map.json", {"zones": []})
    _write(cfg / "manual_override.json", {})
    _write(cfg / "mqtt_state_cache.json", {})
    _write(cfg / "positions.json", {})
    store = HistoryStore(str(cfg / "data" / "history.db"))
    store.record_many(int(time.time()), {"Rooms Sensor": {"temperature_c": 21.0}})
    schedules = ScheduleStore(str(cfg / "schedules.json"))
    schedules.set_mode("cool")
    week = ScheduleStore.empty_week()
    week["mon"][8] = 24.0
    schedules.set_week("Rooms", week)
    schedules.set_diagnostic_mode(True)
    runtime = [str(cfg / n) for n in ("climate_map.json", "manual_override.json", "mqtt_state_cache.json",
                                      "positions.json", "schedules.json")]
    maintenance = Maintenance(str(cfg), store, schedules, runtime)
    yield cfg, maintenance, store, schedules
    store.close()


def _names(zip_bytes):
    return set(zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist())


def test_download_backup_leaves_credentials_out(setup):
    cfg, maintenance, _, _ = setup

    data = maintenance.backup_zip(include_credentials=False)

    names = _names(data)
    assert {"config.yaml", "climate_map.json", "schedules.json", "mqtt_broker.json"} <= names
    assert not names & {"secrets_meross.json", "devices.json", "config.example.yaml"}
    broker = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("mqtt_broker.json"))
    assert broker["password"] == "" and broker["username"] == "u"


def test_board_backup_keeps_everything(setup):
    cfg, maintenance, _, _ = setup

    name = maintenance.save_backup("test")

    names = _names((cfg / "backups" / name).read_bytes())
    assert {"secrets_meross.json", "devices.json", "mqtt_broker.json"} <= names
    broker = json.loads(zipfile.ZipFile(cfg / "backups" / name).read("mqtt_broker.json"))
    assert broker["password"] == "s3cret"


def test_old_board_backups_are_pruned(setup, monkeypatch):
    cfg, maintenance, _, _ = setup
    for i in range(KEEP_CONFIG_BACKUPS + 3):
        monkeypatch.setattr("localhome.services.maintenance.time.strftime", lambda fmt, i=i: f"2026010{i:02d}")
        maintenance.save_backup("n")

    zips = [n for n in os.listdir(cfg / "backups") if n.endswith(".zip")]
    assert len(zips) == KEEP_CONFIG_BACKUPS
    assert "backup-202601000-n.zip" not in zips  # the oldest went first


def test_clear_history_backs_up_then_empties(setup):
    cfg, maintenance, store, _ = setup

    result = maintenance.clear_history()

    assert result["rows_removed"] == 1
    assert store.stats()["rows"] == 0
    assert (cfg / "backups" / result["backup"]).stat().st_size > 0


def test_clear_schedules_keeps_the_season_only(setup):
    cfg, maintenance, _, schedules = setup

    maintenance.clear_schedules()

    assert json.loads((cfg / "schedules.json").read_text(encoding="utf-8")) == {"mode": "cool"}
    assert schedules.get_diagnostic_mode() is False
    assert all(v is None for day in schedules.get_week("Rooms").values() for v in day)


def test_factory_reset_removes_only_what_localhome_created(setup):
    cfg, maintenance, store, _ = setup

    result = maintenance.factory_reset()

    for name in ("climate_map.json", "manual_override.json", "mqtt_state_cache.json", "positions.json", "schedules.json"):
        assert not (cfg / name).exists(), name
    for name in ("config.yaml", "mqtt_broker.json", "secrets_meross.json", "devices.json", "config.example.yaml"):
        assert (cfg / name).exists(), name
    assert store.stats()["rows"] == 0
    backup = _names((cfg / "backups" / result["backup"]).read_bytes())
    assert "climate_map.json" in backup and "schedules.json" in backup


# ------------------------------------------------------------------ API ----

@pytest.fixture
def client(tmp_path, monkeypatch):
    restarts = []
    monkeypatch.setattr("localhome.web.app.schedule_restart", lambda: restarts.append(True))
    monkeypatch.setattr("localhome.web.app.is_supported", lambda: True)
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write(cfg / "config.yaml", CONFIG)
    app = create_app(str(cfg / "config.yaml"))
    yield app.test_client(), cfg, restarts
    app.config["HISTORY"].close()


def test_destructive_routes_need_the_word_reset(client):
    c, _, restarts = client
    for action in ("clear-history", "clear-schedules", "factory-reset"):
        res = c.post(f"/api/climate-admin/maintenance/{action}", json={"confirm": "reset"})
        assert res.status_code == 400, action
    assert restarts == []


def test_factory_reset_route_restarts(client):
    c, cfg, restarts = client
    ScheduleStore(str(cfg / "schedules.json")).set_week("Rooms", ScheduleStore.empty_week())

    body = c.post("/api/climate-admin/maintenance/factory-reset", json={"confirm": "RESET"}).get_json()

    assert body["ok"] is True and body["restarting"] is True
    assert "schedules.json" in body["removed"]
    assert restarts == [True]


def test_backup_download_is_a_zip(client):
    c, _, _ = client

    res = c.get("/api/climate-admin/maintenance/backup.zip")

    assert res.status_code == 200
    assert res.mimetype == "application/zip"
    assert "config.yaml" in _names(res.data)
    info = c.get("/api/climate-admin/maintenance").get_json()
    assert info["ok"] is True and info["has_thermostat"] is True


def test_history_backup_is_skipped_when_the_disk_is_nearly_full(setup, monkeypatch):
    cfg, maintenance, store, _ = setup

    class Usage:
        free = 100 * 1024 * 1024

    monkeypatch.setattr("localhome.services.maintenance.shutil.disk_usage", lambda p: Usage)

    result = maintenance.clear_history()

    assert result["backup"] is None
    assert store.stats()["rows"] == 0  # the action itself still ran


def test_only_one_history_backup_is_kept(setup, monkeypatch):
    cfg, maintenance, _, _ = setup
    names = iter(["history-1.db", "history-2.db"])
    monkeypatch.setattr("localhome.services.maintenance.time.strftime", lambda fmt: next(names).split("-")[1].split(".")[0])
    maintenance.save_history_backup()
    maintenance.save_history_backup()
    assert sorted(n for n in os.listdir(cfg / "backups") if n.startswith("history-")) == ["history-2.db"]
