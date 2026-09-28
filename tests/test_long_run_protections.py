"""Running unattended all year: the service watchdog, the board protections
tune-board.sh sets up, the watchdog history and the diagnostics download."""
import io
import json
import zipfile

from localhome.services import diagnostics, system_status
from localhome.services.service_watchdog import ServiceWatchdog
from localhome.web.app import create_app

SYSTEMD = {"NOTIFY_SOCKET": "/run/systemd/notify", "WATCHDOG_USEC": "120000000"}


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
    monkeypatch.setattr(system_status, "ANY_BAND_FLAG", str(tmp_path / "any-band"))
    (tmp_path / "wifi.conf").write_text("[connection]\nwifi.powersave=2\n")
    (tmp_path / "restart.conf").write_text("[Service]\nRestart=always\n")
    network = {"wifi": {"band": "2.4 GHz"}}
    items = system_status.board_protections(env=SYSTEMD, network=network)["items"]
    assert items == {"wifi_always_on": True, "wifi_watchdog": False, "hardware_watchdog": None,
                     "service_watchdog": True, "power_saving_off": False, "services_restart": True,
                     "wifi_24ghz": True}
    items["power_saving_off"] = True
    (tmp_path / "any-band").write_text("")
    assert system_status.board_protections(env=SYSTEMD, network=network)["items"]["wifi_24ghz"] is None
    found = system_status.problems([], [], {}, protections={"items": items})
    assert [(p["code"], p["subject"]) for p in found] == [("protection_missing", "wifi_watchdog")]


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
    status = json.loads(zf.read("system-status.json"))
    assert "problems" in status and "watchdog_history" in status
    backup = zipfile.ZipFile(io.BytesIO(zf.read("config-backup.zip")))
    assert "config.yaml" in backup.namelist()
    assert not any("secrets_" in name for name in backup.namelist())
    assert b"top-secret" not in res.data
