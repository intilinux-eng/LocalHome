"""The board's clock sync, read from systemd-timesyncd's flag file - no
subprocess - and handed to the pages and the LED matrix."""
from localhome.services import system_status
from localhome.web.app import create_app


def test_synced_once_the_flag_file_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(system_status, "TIMESYNC_DIR", str(tmp_path))
    assert system_status.clock_synced_fast() is False
    (tmp_path / "synchronized").write_text("", encoding="utf-8")
    assert system_status.clock_synced_fast() is True


def test_nothing_is_claimed_where_timesyncd_isnt_running(tmp_path, monkeypatch):
    monkeypatch.setattr(system_status, "TIMESYNC_DIR", str(tmp_path / "absent"))
    assert system_status.clock_synced_fast() is None


def test_the_pages_are_told(tmp_path, monkeypatch):
    monkeypatch.setattr(system_status, "TIMESYNC_DIR", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(path))
    try:
        assert app.test_client().get("/api/server-time").get_json()["clock_synced"] is False
    finally:
        app.config["HISTORY"].close()


def test_the_system_status_uses_the_same_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(system_status, "TIMESYNC_DIR", str(tmp_path))
    found = system_status.problems([], [], {"low": False, "path": "/"},
                                   host={"uptime_seconds": 60, "clock_synced": system_status.clock_synced_fast()})
    assert [p["code"] for p in found] == ["clock_unsynced"]
