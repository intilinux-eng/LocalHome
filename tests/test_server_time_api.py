"""The clock the web UI shows is the *server's*, because that's the one the
thermostat picks each hour's target with. The first real deployment ran
on a board left at its factory UTC default: 08:00 in the house, the 06:00
slot being applied, and a browser-side clock showing the right time the
whole while. These lock in that the server reports its own local time and
offset honestly, so the UI can show it and flag the mismatch.
"""
import time
from datetime import datetime, timedelta, timezone

from localhome.services import system_clock
from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations: []
"""


def test_server_time_reports_a_fixed_offset_exactly():
    rome_summer = datetime(2026, 9, 21, 8, 17, tzinfo=timezone(timedelta(hours=2)))
    result = system_clock.server_time(rome_summer)

    assert result["utc_offset_minutes"] == 120
    assert result["epoch_ms"] == int(rome_summer.timestamp() * 1000)


def test_server_time_reports_utc_as_zero_offset():
    # The misconfigured-board case the UI has to be able to flag.
    utc = datetime(2026, 9, 21, 6, 17, tzinfo=timezone.utc)
    assert system_clock.server_time(utc)["utc_offset_minutes"] == 0


def test_server_time_defaults_to_the_systems_own_local_time():
    result = system_clock.server_time()
    expected_offset = datetime.now().astimezone().utcoffset().total_seconds() // 60

    assert abs(result["epoch_ms"] / 1000 - time.time()) < 5
    assert result["utc_offset_minutes"] == expected_offset


def test_timezone_name_is_read_from_the_zoneinfo_link(monkeypatch):
    monkeypatch.setattr(system_clock.os.path, "realpath", lambda p: "/usr/share/zoneinfo/Europe/Rome")
    assert system_clock.system_timezone_name() == "Europe/Rome"


def test_timezone_name_reports_a_factory_utc_board_as_such(monkeypatch):
    monkeypatch.setattr(system_clock.os.path, "realpath", lambda p: "/usr/share/zoneinfo/Etc/UTC")
    assert system_clock.system_timezone_name() == "Etc/UTC"


def test_timezone_name_is_none_when_nothing_can_be_read(monkeypatch):
    # e.g. Windows: no /etc/localtime link, no /etc/timezone. The offset
    # and abbreviation still work; the name just isn't known.
    monkeypatch.setattr(system_clock.os.path, "realpath", lambda p: r"C:\etc\localtime")

    def _missing(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(system_clock, "open", _missing, raising=False)
    assert system_clock.system_timezone_name() is None


def test_server_time_route_returns_every_field(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    client = create_app(str(config_path)).test_client()

    body = client.get("/api/server-time").get_json()

    assert body["ok"] is True
    for key in ("epoch_ms", "utc_offset_minutes", "tz_abbrev", "tz_name"):
        assert key in body
    assert abs(body["epoch_ms"] / 1000 - time.time()) < 5
