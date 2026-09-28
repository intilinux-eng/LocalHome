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
    monkeypatch.setattr(system_status, "PROCESS_STARTED_MONO", time.monotonic() - 10)
    assert not system_status.loop_health("thermostat", None, 60, now)["stalled"]
    monkeypatch.setattr(system_status, "PROCESS_STARTED_MONO", time.monotonic() - 600)
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
    # The recorder thread may already have written its first cycle.
    assert isinstance(body["database"]["rows"], int)
    assert body["process"]["uptime_seconds"] >= 0


def _fake_emmc(tmp_path, life_time="0x01 0x01", pre_eol="0x01"):
    card = tmp_path / "sys" / "class" / "mmc_host" / "mmc0" / "mmc0_0001"  # "mmc0:0001" on Linux; NTFS has no colons
    card.mkdir(parents=True)
    (card / "type").write_text("MMC\n")
    (card / "name").write_text("BUTC42\n")
    (card / "life_time").write_text(life_time + "\n")
    (card / "pre_eol_info").write_text(pre_eol + "\n")
    proc = tmp_path / "proc"
    proc.mkdir()
    # 6183775 sectors written, as measured on the real board after ~24.8h
    (proc / "diskstats").write_text(" 179 0 mmcblk0 1 0 2 3 273180 0 6183775 9 0 10 11\n")
    return str(tmp_path / "sys"), str(proc)


def test_storage_health_reads_the_emmc_wear_and_write_rate(tmp_path):
    sys_root, proc_root = _fake_emmc(tmp_path)

    health = system_status.storage_health(sys_root, proc_root, uptime_seconds=89324)

    assert health["name"] == "BUTC42"
    assert health["wear_percent_max"] == 10
    assert health["pre_eol"] == "normal"
    assert health["written_since_boot_bytes"] == 6183775 * 512
    assert round(health["written_per_day_bytes"] / 1e9, 1) == 3.1


def test_storage_health_is_none_without_an_emmc(tmp_path):
    assert system_status.storage_health(str(tmp_path / "nothing"), str(tmp_path)) is None


def test_a_worn_emmc_is_a_problem(tmp_path):
    sys_root, proc_root = _fake_emmc(tmp_path, life_time="0x03 0x09", pre_eol="0x02")
    storage = system_status.storage_health(sys_root, proc_root, uptime_seconds=3600)

    problems = system_status.problems([], [], {"low": False}, [], storage)

    assert storage["wear_percent_max"] == 90
    assert [(p["code"], p["subject"]) for p in problems] == [("storage_wear", "BUTC42")]


def _fake_thermal(tmp_path, zones):
    base = tmp_path / "sys" / "class" / "thermal"
    for i, (kind, millideg) in enumerate(zones):
        zone = base / f"thermal_zone{i}"
        zone.mkdir(parents=True)
        (zone / "type").write_text(kind + "\n")
        (zone / "temp").write_text(f"{millideg}\n")
    return str(tmp_path / "sys")


def test_cpu_temperature_is_the_hottest_cpu_zone(tmp_path):
    # As read on the real UNO Q: the GPU/modem zones don't count.
    sys_root = _fake_thermal(tmp_path, [("mapss-thermal", 41000), ("cpuss0-thermal", 44800),
                                        ("cpuss1-thermal", 46100), ("gpu-thermal", 52000)])
    assert system_status.cpu_temperature_c(sys_root) == 46.1


def test_cpu_temperature_falls_back_to_every_zone(tmp_path):
    sys_root = _fake_thermal(tmp_path, [("soc-thermal", 51000), ("board", 40000)])
    assert system_status.cpu_temperature_c(sys_root) == 51.0


def test_cpu_temperature_is_none_without_thermal_zones(tmp_path):
    assert system_status.cpu_temperature_c(str(tmp_path / "nothing")) is None


def test_a_hot_cpu_is_a_problem():
    problems = system_status.problems([], [], {"low": False}, [], None, 83.4)
    assert [(p["code"], p["subject"]) for p in problems] == [("cpu_hot", "83°C")]
    assert system_status.problems([], [], {"low": False}, [], None, 45.0) == []


def test_nmcli_fields_keep_escaped_colons():
    assert system_status._nmcli_fields(r"GENERAL.HWADDR:14\:B5\:CD\:EF\:1F\:A9") == ["GENERAL.HWADDR", "14:B5:CD:EF:1F:A9"]
    assert system_status._nmcli_fields(r"yes:Casa\:Ospiti:72") == ["yes", "Casa:Ospiti", "72"]


def _fake_nmcli(outputs):
    def run(*args):
        for key, rows in outputs.items():
            if key in args:
                return [system_status._nmcli_fields(line) for line in rows]
        return None
    return run


def test_network_status_reads_the_active_wifi():
    # Shaped like the real UNO Q's nmcli output.
    run = _fake_nmcli({
        "DEVICE,TYPE,STATE": ["wlan0:wifi:connected", "docker0:bridge:connected (externally)", "lo:loopback:connected"],
        "ACTIVE,SSID,SIGNAL,FREQ,CHAN,SECURITY": ["no:Neighbour:40:2437 MHz:6:WPA2", "yes:Casa-WiFi:89:5500 MHz:100:WPA2"],
        "IP4.ADDRESS,IP4.GATEWAY,GENERAL.HWADDR": ["IP4.ADDRESS[1]:192.168.1.28/24", "IP4.GATEWAY:192.168.1.1",
                                                  r"GENERAL.HWADDR:14\:B5\:CD\:EF\:1F\:A9"],
    })

    wifi = system_status.network_status(run)["wifi"]

    assert wifi == {"device": "wlan0", "ip": "192.168.1.28", "gateway": "192.168.1.1", "mac": "14:B5:CD:EF:1F:A9",
                    "ssid": "Casa-WiFi", "signal_percent": 89, "band": "5 GHz", "channel": 100, "security": "WPA2"}


def test_network_status_without_networkmanager():
    status = system_status.network_status(lambda *args: None)
    assert status["wifi"] is None and status["mdns_name"].endswith(".local")


def test_a_weak_wifi_signal_is_a_problem():
    network = {"wifi": {"ssid": "Casa", "signal_percent": 22}}
    problems = system_status.problems([], [], {"low": False}, [], None, None, network)
    assert [(p["code"], p["subject"]) for p in problems] == [("wifi_weak", "Casa")]


class _Out:
    def __init__(self, stdout):
        self.stdout = stdout


def test_boot_history_reads_how_each_session_ended():
    # `last -x -F -n 5 reboot` as printed on the real UNO Q
    output = (
        "reboot   system boot  7.0.0-g122c2c22d Sun Sep 20 18:33:51 2026   still running\n".replace("   still", " - still")
        + "reboot   system boot  7.0.0-g122c2c22d Sun Sep 20 18:28:40 2026 - crash                    (00:04)\n"
        + "reboot   system boot  7.0.0-g122c2c22d Thu May 28 13:23:02 2026 - Thu May 28 14:00:10 2026  (00:37)\n"
        + "\nwtmp begins Thu May 28 13:23:02 2026\n"
    )

    boots = system_status.boot_history(run=lambda *a, **k: _Out(output))

    assert [b["ended"] for b in boots] == ["running", "crash", "clean"]
    assert boots[0]["booted_at"] > boots[1]["booted_at"] > boots[2]["booted_at"]


def test_boot_history_is_empty_without_last():
    def missing(*args, **kwargs):
        raise OSError("no last here")
    assert system_status.boot_history(run=missing) == []


def test_an_unsynchronized_clock_is_a_problem():
    problems = system_status.problems([], [], {"low": False}, [], None, None, None, {"clock_synced": False})
    assert [p["code"] for p in problems] == ["clock_unsynced"]
    assert system_status.problems([], [], {"low": False}, [], None, None, None, {"clock_synced": None}) == []


def test_problem_details_drop_the_list_of_every_driver():
    error = ("ValueError: No driver registered for kind='switch' type='tplink_led_strip'. Driver modules skipped "
             "for a missing package: localhome.drivers.tplink.led_strip (needs: pip install -e .[tplink]). "
             "Available kind/type combinations: ['climate/simulated', 'switch/simulated']")
    (problem,) = system_status.problems([], [{"kind": "switch", "type": "tplink_led_strip", "name": "Divano",
                                              "error": error}], {"low": False})
    assert problem["detail"].endswith("(needs: pip install -e .[tplink]).")
    assert "Available kind/type" not in problem["detail"]


def test_cover_health_reports_what_the_last_read_said():
    now = 1_000_000
    devices = system_status.cover_health({
        "Kitchen Blind": {"ok": True, "at": now - 8},
        "Bedroom Blind": {"ok": False, "error": "Bedroom Blind: Check device key or version", "at": now - 20},
    }, now=now)

    assert [d["name"] for d in devices] == ["Bedroom Blind", "Kitchen Blind"]
    assert devices[0] == {"name": "Bedroom Blind", "kind": "cover", "ok": False,
                          "error": "Bedroom Blind: Check device key or version", "seconds_since_update": 20}
    assert devices[1]["ok"] is True and devices[1]["error"] is None


def test_an_unreachable_cover_becomes_a_problem():
    devices = system_status.cover_health({"Kitchen Blind": {"ok": False, "error": "no answer", "at": 1}})

    found = system_status.problems(devices, [], {"low": False, "path": "/"})

    assert [p["code"] for p in found] == ["device_offline"]
    assert found[0]["subject"] == "Kitchen Blind"


def test_a_device_not_heard_yet_after_a_boot_is_not_called_offline():
    """A board that came back five minutes ago hasn't heard from its
    battery sensors yet - they speak every couple of hours. Calling that
    "offline" after every power cut is how a problems list gets ignored."""
    devices = [{"name": "Rooms Sensor", "kind": "climate", "ok": False,
                "error": "no MQTT message received yet", "seconds_since_update": None}]

    fresh = system_status.problems(devices, [], {"low": False, "path": "/"}, host={"uptime_seconds": 300})
    running_for_days = system_status.problems(devices, [], {"low": False, "path": "/"}, host={"uptime_seconds": 300000})

    assert [p["code"] for p in fresh] == ["device_not_heard_yet"]
    assert [p["code"] for p in running_for_days] == ["device_offline"]


def test_a_device_that_reported_then_went_quiet_is_offline_even_after_a_boot():
    devices = [{"name": "Plug", "kind": "switch", "ok": False, "error": "timeout", "seconds_since_update": 120}]

    found = system_status.problems(devices, [], {"low": False, "path": "/"}, host={"uptime_seconds": 300})

    assert [p["code"] for p in found] == ["device_offline"]


def test_boot_times_follow_a_clock_corrected_after_startup(monkeypatch):
    """The UNO Q boots with yesterday's time and NTP fixes it moments
    later. Boot and start times must come out right on the corrected
    clock - not frozen at whatever the clock said when LocalHome started."""
    monkeypatch.setattr(system_status, "_uptime_seconds", lambda: 780)             # up 13 minutes
    monkeypatch.setattr(system_status, "PROCESS_STARTED_MONO", time.monotonic() - 760)
    corrected_now = time.time()

    times = system_status.boot_times()

    assert abs(times["booted_at"] - (corrected_now - 780)) <= 2
    assert abs(times["started_at"] - (corrected_now - 760)) <= 2
    assert abs(system_status.process_stats()["uptime_seconds"] - 760) <= 2


def test_boot_times_are_reported_for_the_pages():
    times = system_status.boot_times()

    assert set(times) == {"booted_at", "started_at"}
    assert times["started_at"] <= time.time()
    # booted_at is None where the board can't tell us (no /proc/uptime).
    assert times["booted_at"] is None or times["booted_at"] <= times["started_at"]


def test_the_page_gets_the_boot_time_on_the_corrected_clock(tmp_path, monkeypatch):
    """/api/server-time used to hand out a boot time computed once, when
    LocalHome started - before NTP had fixed the board's clock."""
    from localhome.web.app import create_app

    path = tmp_path / "config.yaml"
    path.write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(path))
    try:
        monkeypatch.setattr(system_status, "_uptime_seconds", lambda: 780)
        data = app.test_client().get("/api/server-time").get_json()
        assert abs(data["booted_at"] - (time.time() - 780)) <= 2
    finally:
        app.config["HISTORY"].close()


def test_the_write_rate_is_the_last_hours_not_since_boot():
    clock = {"t": 0.0}
    written = {"b": 0}
    rate = system_status.WriteRate(read=lambda: written["b"], clock=lambda: clock["t"])
    rate.sample()
    assert rate.per_day() is None
    written["b"], clock["t"] = 500_000_000, 60        # the start-up burst
    rate.sample()
    assert rate.per_day() is None, "less than 10 minutes measured"
    for minute in range(5, 125, 5):                    # then 1 MB every 5 min
        clock["t"] = 60 + minute * 60
        written["b"] += 1_000_000
        rate.sample()
    per_day, minutes = rate.per_day()
    assert minutes == 60 and 250_000_000 < per_day < 300_000_000, "the burst is out of the window"
    written["b"] = 10                                   # a reboot resets the counter
    rate.sample()
    assert rate.per_day() is None


def test_a_device_answering_with_an_error_after_a_boot_is_offline_not_waiting():
    host = {"uptime_seconds": 60}
    waiting = {"name": "Stanze Sensor", "ok": False, "seconds_since_update": None,
               "error": "no MQTT message received yet on shellyhtg3-x/status/temperature:0"}
    failing = {"name": "Divano", "ok": False, "seconds_since_update": None,
               "error": "Timed out getting discovery response for 192.168.1.3"}
    found = {p["subject"]: p["code"] for p in system_status.problems([waiting, failing], [], {}, host=host)}
    assert found == {"Stanze Sensor": "device_not_heard_yet", "Divano": "device_offline"}


def test_the_dashboards_problem_count_leaves_out_devices_not_heard_yet(tmp_path, monkeypatch):
    from localhome.web.app import create_app
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        monkeypatch.setattr(system_status, "problems", lambda *a, **k: [
            {"code": "device_offline", "subject": "Stanze Valve", "detail": None},
            {"code": "device_not_heard_yet", "subject": "Bagni Sensor", "detail": None},
        ])
        body = app.test_client().get("/api/problems").get_json()
    finally:
        app.config["HISTORY"].close()
    assert body == {"ok": True, "problems": [{"code": "device_offline", "subject": "Stanze Valve", "detail": None}]}
