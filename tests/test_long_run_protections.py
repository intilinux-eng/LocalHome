"""Running unattended all year: the service watchdog, the board protections
tune-board.sh sets up, the watchdog history and the diagnostics download."""
import io
import json
import zipfile

from localhome.services import diagnostics, system_status
from localhome.services.service_watchdog import ServiceWatchdog
from localhome.web.app import create_app

SYSTEMD = {"NOTIFY_SOCKET": "/run/systemd/notify", "WATCHDOG_USEC": "120000000", "INVOCATION_ID": "abc"}


def _watchdog(tmp_path, answers=True, stalled=(), env=SYSTEMD):
    sent = []

    def fetch(url):
        if not answers_now["ok"]:
            raise OSError("connection refused")

    answers_now = {"ok": answers}
    loops = [{"name": name, "stalled": True} for name in stalled]
    wd = ServiceWatchdog("http://127.0.0.1:5000/api/server-time", lambda: loops, env=env,
                         sender=lambda msg, env: sent.append(msg), fetch=fetch,
                         event_log=str(tmp_path / "watchdog.log"))
    return wd, sent, answers_now, loops


def test_ready_once_the_page_answers_then_alive_every_third_of_the_timeout(tmp_path):
    wd, sent, _, _ = _watchdog(tmp_path)
    assert wd.interval == 40
    wd.step()
    wd.step()
    assert sent == ["READY=1", "WATCHDOG=1"]


def test_not_ready_while_starting_and_no_history_for_it(tmp_path):
    wd, sent, answers, _ = _watchdog(tmp_path, answers=False)
    wd.step()
    assert sent == [] and not (tmp_path / "watchdog.log").exists(), "starting up isn't a problem"
    answers["ok"] = True
    wd.step()
    assert sent == ["READY=1"]


def test_a_hang_stops_the_pings_and_goes_in_the_history(tmp_path):
    wd, sent, answers, loops = _watchdog(tmp_path)
    wd.step()
    loops.append({"name": "thermostat", "stalled": True})
    wd.step()
    wd.step()
    assert sent == ["READY=1"], "no WATCHDOG=1 while the thermostat loop is stuck"
    events = system_status.read_events(str(tmp_path / "watchdog.log"), "localhome")
    assert [e["code"] for e in events] == ["unhealthy"], "once, not every step"
    assert "thermostat" in events[0]["values"]
    loops.clear()
    wd.step()
    assert sent[-1] == "WATCHDOG=1"
    assert [e["code"] for e in system_status.read_events(str(tmp_path / "watchdog.log"), "localhome")] == [
        "unhealthy", "healthy"]


def test_a_restart_after_a_hang_is_said(tmp_path):
    system_status.append_event(str(tmp_path / "watchdog.log"), "unhealthy", "its own web page doesn't answer")
    wd, _, _, _ = _watchdog(tmp_path)
    wd.note_start()
    assert system_status.read_events(str(tmp_path / "watchdog.log"), "localhome")[-1]["code"] == "restarted"


def test_nothing_at_all_where_systemd_did_not_ask(tmp_path):
    wd, sent, _, _ = _watchdog(tmp_path, env={})
    assert not wd.wanted and wd.interval is None
    wd.start()   # no thread, no file
    assert not (tmp_path / "watchdog.log").exists()


def test_the_history_keeps_the_last_lines_and_merges_both_watchdogs_newest_first(tmp_path):
    mine = str(tmp_path / "watchdog.log")
    for i in range(system_status.EVENT_LOG_KEEP + 5):
        system_status.append_event(mine, "healthy")
    assert len(open(mine, encoding="utf-8").read().splitlines()) == system_status.EVENT_LOG_KEEP
    wifi = tmp_path / "wifi.log"
    wifi.write_text("2099-01-01 03:00:00 reconnect 3 wlan0\n2000-01-01 00:00:00 router_down 192.168.1.1\n",
                    encoding="utf-8")
    history = system_status.watchdog_history(mine, str(wifi), str(tmp_path / "none.log"), limit=3)
    assert history[0] == {"when": "2099-01-01 03:00:00", "source": "wifi", "code": "reconnect", "values": "3 wlan0"}
    assert [e["source"] for e in history[1:]] == ["localhome", "localhome"]


def test_protections_are_read_from_what_tune_board_writes(tmp_path, monkeypatch):
    nm = tmp_path / "NetworkManager"
    nm.mkdir()
    monkeypatch.setattr(system_status, "NETWORKMANAGER_DIR", str(nm))
    monkeypatch.setattr(system_status, "WIFI_CONF", str(tmp_path / "wifi.conf"))
    monkeypatch.setattr(system_status, "WIFI_WATCHDOG_ENABLED", str(tmp_path / "timer"))
    monkeypatch.setattr(system_status, "HARDWARE_WATCHDOG_CONF", str(tmp_path / "hw.conf"))
    monkeypatch.setattr(system_status, "HARDWARE_WATCHDOG_DEVICE", str(tmp_path / "no-watchdog-here"))
    monkeypatch.setattr(system_status, "POWER_CONF", str(tmp_path / "power.conf"))
    monkeypatch.setattr(system_status, "RESTART_DROPINS", [str(tmp_path / "restart.conf")])
    (tmp_path / "wifi.conf").write_text("[connection]\nwifi.powersave=2\n")
    (tmp_path / "restart.conf").write_text("[Service]\nRestart=always\n")
    _fake_cgroup(tmp_path, monkeypatch, "536870912")
    items = system_status.board_protections(env=SYSTEMD)["items"]
    assert items == {"wifi_always_on": True, "wifi_watchdog": False, "hardware_watchdog": None,
                     "service_watchdog": True, "power_saving_off": False, "services_restart": True,
                     "memory_limit": True}
    items["power_saving_off"] = True
    found = system_status.problems([], [], {}, protections={"items": items})
    assert [(p["code"], p["subject"]) for p in found] == [("protection_missing", "wifi_watchdog")]


def _fake_cgroup(tmp_path, monkeypatch, memory_max):
    """/proc/self/cgroup and the service's memory.max, as systemd sets them."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    own = tmp_path / "self-cgroup"
    own.write_text("0::/system.slice/localhome.service\n")
    unit = tmp_path / "cgroup" / "system.slice" / "localhome.service"
    unit.mkdir(parents=True)
    (unit / "memory.max").write_text(memory_max + "\n")
    monkeypatch.setattr(system_status, "OWN_CGROUP", str(own))
    monkeypatch.setattr(system_status, "CGROUP_ROOT", str(tmp_path / "cgroup"))


def test_the_memory_limit_is_read_from_the_services_own_cgroup(tmp_path, monkeypatch):
    _fake_cgroup(tmp_path, monkeypatch, "536870912")
    assert system_status.memory_limit_bytes(system_status.OWN_CGROUP, system_status.CGROUP_ROOT) == 512 * 1024 ** 2
    _fake_cgroup(tmp_path / "none", monkeypatch, "max")
    assert system_status.memory_limit_bytes(system_status.OWN_CGROUP, system_status.CGROUP_ROOT) == 0
    assert system_status.memory_limit_bytes(str(tmp_path / "missing"), str(tmp_path)) is None


def test_the_memory_limit_counts_only_for_the_service(tmp_path, monkeypatch):
    monkeypatch.setattr(system_status, "NETWORKMANAGER_DIR", str(tmp_path))
    _fake_cgroup(tmp_path, monkeypatch, "max")
    assert system_status.board_protections(env=SYSTEMD)["items"]["memory_limit"] is False
    assert system_status.board_protections(env={})["items"]["memory_limit"] is None, "run by hand"


def test_no_protections_off_a_board(monkeypatch, tmp_path):
    monkeypatch.setattr(system_status, "NETWORKMANAGER_DIR", str(tmp_path / "none"))
    assert system_status.board_protections() is None


def test_the_diagnostics_download_has_everything_it_can_and_says_what_it_cannot(tmp_path):
    (tmp_path / "watchdog.log").write_text("2026-09-27 10:00:00 restarted\n", encoding="utf-8")
    data = diagnostics.build_zip({"ok": True, "problems": []}, b"PK-backup", str(tmp_path / "watchdog.log"),
                                 str(tmp_path / "no-wifi.log"), version="abc123",
                                 journal=lambda *units, lines=0: None, resource_log=str(tmp_path / "none.csv"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert {"README.txt", "system-status.json", "watchdog-localhome.log", "config-backup.zip"} <= names
    assert "localhome.log" not in names
    readme = zf.read("README.txt").decode()
    assert "abc123" in readme and "localhome.log" in readme and "watchdog-wifi.log" in readme


def test_the_diagnostics_route_sends_a_zip_without_secrets(tmp_path):
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    (tmp_path / "secrets_tuya.json").write_text('{"key": "top-secret"}', encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        res = app.test_client().get("/api/climate-admin/diagnostics.zip")
    finally:
        app.config["HISTORY"].close()
    assert res.status_code == 200 and res.mimetype == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(res.data))
    assert "summary.txt" in zf.namelist()
    status = json.loads(zf.read("system-status.json"))
    assert "problems" in status and "watchdog_history" in status
    backup = zipfile.ZipFile(io.BytesIO(zf.read("config-backup.zip")))
    assert "config.yaml" in backup.namelist()
    assert not any("secrets_" in name for name in backup.namelist())
    assert b"top-secret" not in res.data


# ------------------------------------------ the devices reach the board --

TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:075B 00000000:0000 0A 00000000:00000000 00:00000000 00000000   107        0 1 1
   1: 1C01A8C0:075B 0B01A8C0:C0FF 01 00000000:00000000 02:00000000 00000000   107        0 2 1
   2: 0100007F:075B 0100007F:BF93 01 00000000:00000000 02:00000000 00000000   107        0 3 1
   3: 1C01A8C0:1388 1501A8C0:E9FA 01 00000000:00000000 02:00000000 00000000  1000        0 4 1
"""


def test_the_shellys_connected_to_the_broker_are_counted_from_proc(tmp_path):
    (tmp_path / "tcp").write_text(TCP)
    # the listening socket, the board's own client and a web visitor don't count
    assert system_status.lan_mqtt_clients(paths=[str(tmp_path / "tcp")]) == 1
    assert system_status.lan_mqtt_clients(paths=[str(tmp_path / "none")]) is None


def test_the_connections_to_the_broker_are_told_apart(tmp_path):
    (tmp_path / "tcp").write_text(TCP)
    assert system_status.lan_mqtt_peers(paths=[str(tmp_path / "tcp")]) == {"0B01A8C0:C0FF"}
    assert system_status.lan_mqtt_peers(paths=[str(tmp_path / "none")]) is None


def test_silence_counts_only_after_a_device_stayed_connected():
    watch = system_status.LanWatch()
    assert watch.update(set(), now=0) is None, "nobody connected yet"
    assert watch.update({"A", "B"}, now=10) is None
    assert watch.update({"A", "B"}, now=80) is None, "connected 70 s: devices that stay"
    assert watch.update(set(), now=90) == 0
    assert watch.update(set(), now=690) == 600
    assert watch.update({"C"}, now=700) == 610, "a new connection isn't back until it stays"
    assert watch.update({"C"}, now=760) is None
    assert watch.update(None, now=770) is None, "no /proc: nothing claimed"


def test_a_battery_sensor_waking_is_never_a_silence():
    """Seen in a board's watchdog log: an H&T caught while it reported,
    then asleep - "no Shelly connected", every 2 hours, on a network that
    was fine. Asked every 5 s for 6 hours, it connects 10 s every 2 h."""
    watch = system_status.LanWatch()
    for t in range(0, 6 * 3600, 5):
        peers = {f"HT:{t // 7200}"} if t % 7200 < 10 else set()
        assert watch.update(peers, now=t) is None


def test_an_h_and_t_on_usb_c_counts_like_a_mains_shelly():
    """On USB-C it stays connected: gone for 10 minutes, it's the network."""
    watch = system_status.LanWatch()
    for t in range(0, 300, 5):
        watch.update({"HT-usb"}, now=t)
    assert watch.update(set(), now=300) == 0
    assert watch.update({"HT-battery"}, now=600) == 300, "another sensor waking doesn't end it"
    assert watch.update(set(), now=900) == 600


def test_every_battery_sensor_silent_at_once_is_the_network():
    names = {"Stanze Sensor", "Bagni Sensor"}
    late = [{"name": "Stanze Sensor", "seconds_since_update": 9000}, {"name": "Bagni Sensor", "seconds_since_update": 8200}]
    assert system_status.battery_sensors_silent(late, names, uptime_seconds=20000, listening_seconds=20000)
    one_fine = [late[0], {"name": "Bagni Sensor", "seconds_since_update": 600}]
    assert not system_status.battery_sensors_silent(one_fine, names, uptime_seconds=20000, listening_seconds=20000)
    assert not system_status.battery_sensors_silent(late, names, uptime_seconds=3000), "just booted"
    assert not system_status.battery_sensors_silent(late, set(), uptime_seconds=20000, listening_seconds=20000)
    assert not system_status.battery_sensors_silent(late, names, uptime_seconds=20000, listening_seconds=1000), \
        "LocalHome just started: what came while it was stopped, it couldn't hear"
    assert not system_status.battery_sensors_silent(late, names, uptime_seconds=20000), "its own age by default"


def test_devices_not_reaching_the_board_is_a_problem_and_rt_on_the_matrix():
    from localhome.services import led_matrix
    found = system_status.problems([], [], {}, lan_silent_seconds=900)
    assert [(p["code"], p["subject"]) for p in found] == [("lan_unreachable", "15 min")]
    assert system_status.problems([], [], {}, lan_silent_seconds=300) == [], "10 minutes' grace"
    assert led_matrix.problem_codes(found, [])[0]["code"] == "RT"
    assert all(c in led_matrix.FONT for c in "RT")


def test_the_admin_page_lists_the_last_ten_watchdog_actions(tmp_path):
    """Newest first, ten at most - the whole logs go in the diagnostics zip."""
    log = tmp_path / "watchdog.log"
    log.write_text("".join(f"2026-10-{day:02d} 12:00:00 restarted\n" for day in range(1, 16)), encoding="utf-8")
    history = system_status.watchdog_history(str(log), str(tmp_path / "none.log"), str(tmp_path / "none2.log"))
    assert len(history) == 10
    assert history[0]["when"] == "2026-10-15 12:00:00" and history[-1]["when"] == "2026-10-06 12:00:00"


def test_a_shellys_safety_timer_is_in_the_history_as_the_shellys(tmp_path):
    mine = str(tmp_path / "watchdog.log")
    system_status.append_event(mine, "healthy")
    system_status.append_event(mine, "safety_timer", "Stanze Valve", at=946_684_800.0)
    history = system_status.watchdog_history(mine, str(tmp_path / "none.log"), str(tmp_path / "none2.log"))
    timer = next(e for e in history if e["code"] == "safety_timer")
    assert timer["source"] == "shelly" and timer["values"] == "Stanze Valve"
    assert history[-1] is timer, "dated when it happened, not when it was written"
