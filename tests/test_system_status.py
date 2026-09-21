"""The admin panel's system status and `localhome-cli doctor` both exist to
make an installation's own faults visible: before them, a driver whose
optional package wasn't installed (the real case: python-kasa missing on
the board) only left one warning line in the startup log, and doctor
still said "No configuration problems found".
"""
import time

from localhome.cli import _run_doctor
from localhome.config import load_config
from localhome.core import registry
from localhome.core.manager import DeviceManager
from localhome.services import system_status
from localhome.services.climate_map_store import apply_to_config
from localhome.services.history import HistoryStore
from localhome.services.thermostat import ScheduleStore
from localhome.web.app import create_app

BROKEN_CONFIG = """
web:
  port: 5000
integrations:
  - kind: switch
    type: no_such_driver
    name: "Ghost Plug"
  - kind: climate
    type: simulated
    name: "Rooms Sensor"
"""


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_manager_keeps_the_reason_an_integration_failed(tmp_path):
    manager = DeviceManager(load_config(_write(tmp_path, BROKEN_CONFIG)))

    assert "Rooms Sensor" in manager.pollers  # the rest still starts
    (failure,) = manager.failed_integrations
    assert failure["kind"] == "switch"
    assert failure["type"] == "no_such_driver"
    assert failure["name"] == "Ghost Plug"
    assert "No driver registered" in failure["error"]


def test_unknown_driver_error_names_the_install_command_for_a_skipped_module(monkeypatch):
    monkeypatch.setitem(registry._SKIPPED, "localhome.drivers.tplink.led_strip", "kasa")

    try:
        registry.create("light", "tplink_led_strip", {})
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected a ValueError")

    assert "localhome.drivers.tplink.led_strip" in message
    assert "pip install -e .[tplink]" in message


def test_install_hint_falls_back_to_the_bare_package_name():
    assert registry.install_hint("kasa") == "pip install -e .[tplink]"
    assert registry.install_hint("somepkg") == "pip install somepkg"


def test_skipped_driver_modules_lists_each_with_its_remedy(monkeypatch):
    monkeypatch.setitem(registry._SKIPPED, "localhome.drivers.tplink.led_strip", "kasa")

    listed = {item["module"]: item for item in registry.skipped_driver_modules()}

    assert listed["localhome.drivers.tplink.led_strip"] == {
        "module": "localhome.drivers.tplink.led_strip",
        "missing_package": "kasa",
        "install": "pip install -e .[tplink]",
    }


def test_doctor_reports_a_failed_integration_as_a_problem(tmp_path, capsys):
    config = load_config(_write(tmp_path, BROKEN_CONFIG))
    manager = DeviceManager(config)

    exit_code = _run_doctor(config, manager, apply_to_config(config))

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "switch/no_such_driver 'Ghost Plug' failed to load" in out


def test_doctor_mentions_a_skipped_optional_driver_without_failing(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(registry._SKIPPED, "localhome.drivers.tplink.led_strip", "kasa")
    config = load_config(_write(tmp_path, "integrations: []\n"))

    exit_code = _run_doctor(config, DeviceManager(config), apply_to_config(config))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Optional driver not installed: localhome.drivers.tplink.led_strip" in out
    assert "pip install -e .[tplink]" in out


def test_doctor_shows_the_season_actually_in_force_not_the_config_default(tmp_path, capsys):
    # config.yaml says heat (the fresh-install default); the dashboard has
    # since switched to cool, which lives in schedules.json.
    config = load_config(
        _write(
            tmp_path,
            """
integrations: []
thermostat:
  mode: heat
  zones: []
""",
        )
    )
    ScheduleStore(str(tmp_path / "schedules.json")).set_mode("cool")

    _run_doctor(config, DeviceManager(config), apply_to_config(config))

    assert "(mode: cool)" in capsys.readouterr().out


def test_doctor_flags_diagnostic_mode(tmp_path, capsys):
    config = load_config(_write(tmp_path, "integrations: []\nthermostat:\n  zones: []\n"))
    ScheduleStore(str(tmp_path / "schedules.json")).set_diagnostic_mode(True)

    _run_doctor(config, DeviceManager(config), apply_to_config(config))

    assert "DIAGNOSTIC MODE ON" in capsys.readouterr().out


def test_loop_is_stalled_after_three_missed_intervals():
    now = time.time()
    assert not system_status.loop_health("history", now - 80, 30, now)["stalled"]
    assert system_status.loop_health("history", now - 100, 30, now)["stalled"]


def test_loop_that_never_finished_is_only_stalled_once_past_the_startup_grace(monkeypatch):
    now = time.time()
    monkeypatch.setattr(system_status, "PROCESS_STARTED_TS", now - 10)
    assert not system_status.loop_health("thermostat", None, 60, now)["stalled"]
    monkeypatch.setattr(system_status, "PROCESS_STARTED_TS", now - 600)
    assert system_status.loop_health("thermostat", None, 60, now)["stalled"]


def test_disk_is_low_below_ten_percent_free(monkeypatch):
    class Usage:
        total = 16 * 1024**3
        free = 1 * 1024**3

    monkeypatch.setattr(system_status.shutil, "disk_usage", lambda path: Usage)
    assert system_status.disk_stats("/data")["low"] is True


def test_problems_lists_each_kind_of_fault():
    problems = system_status.problems(
        devices=[{"name": "VMC", "ok": False, "error": "timed out"}, {"name": "Rooms Sensor", "ok": True, "error": None}],
        failed=[{"kind": "switch", "type": "no_such_driver", "name": None, "error": "ValueError: nope"}],
        disk={"path": "/data", "low": True},
        loops=[{"name": "thermostat", "stalled": True}, {"name": "history", "stalled": False}],
    )

    assert [(p["code"], p["subject"]) for p in problems] == [
        ("integration_failed", "switch/no_such_driver"),
        ("loop_stalled", "thermostat"),
        ("disk_low", "/data"),
        ("device_offline", "VMC"),
    ]


def test_history_stats_counts_rows_and_growth(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    now = int(time.time())
    store.record_many(now - 3 * 86400, {"Rooms Sensor": {"temperature_c": 20.0}})
    store.record_many(now - 60, {"Rooms Sensor": {"temperature_c": 21.0, "humidity_pct": 50.0}})

    stats = store.stats(now)

    assert stats["rows"] == 3
    assert stats["rows_24h"] == 2
    assert stats["oldest_ts"] == now - 3 * 86400
    assert stats["file_bytes"] > 0


def test_system_status_route_reports_the_failed_integration(tmp_path):
    client = create_app(str(_write(tmp_path, BROKEN_CONFIG))).test_client()

    body = client.get("/api/system-status").get_json()

    assert body["ok"] is True
    assert body["failed_integrations"][0]["name"] == "Ghost Plug"
    assert ("integration_failed", "Ghost Plug") in [(p["code"], p["subject"]) for p in body["problems"]]
    assert [d["name"] for d in body["devices"]] == ["Rooms Sensor"]
    for key in ("process", "host", "disk", "database", "loops"):
        assert key in body
    assert body["database"]["rows"] == 0
    assert body["process"]["uptime_seconds"] >= 0
