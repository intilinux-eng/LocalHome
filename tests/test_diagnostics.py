"""services/diagnostics.py, read by hand: the summary page in the board's
language, and a network log that isn't the Wi-Fi watchdog's every-minute
lines. Seen in a board's download: 1999 of network.log's 2000 lines were
systemd noting the watchdog's runs, and the story of the board - how long
it had been on, when it was unplugged, what the watchdogs did - had to be
pieced together from four files."""
import io
import zipfile
from datetime import datetime

from localhome.services import diagnostics
from localhome.web.i18n import load_translations

NOW = datetime(2026, 10, 7, 22, 32).timestamp()

STATUS = {
    "problems": [{"code": "device_offline", "subject": "Bagni Sensor"}],
    "boots": [{"booted_at": NOW - 3 * 86400, "ended": "running"},
              {"booted_at": NOW - 3 * 86400 - 600, "ended": "crash"}],
    "network": {"hostname": "board", "mdns_name": "board.local",
                "wifi": {"ip": "192.168.1.27", "ssid": "Home", "band": "5 GHz", "channel": 64,
                         "signal_percent": 100}},
    "devices": [{"name": "Stanze Sensor", "ok": True, "error": None, "seconds_since_update": 720},
                {"name": "Bagni Sensor", "ok": False, "error": "probably offline", "seconds_since_update": 30000}],
    "process": {"uptime_seconds": 26 * 3600, "rss_bytes": 95_000_000},
    "host": {"uptime_seconds": 3 * 86400 + 9000, "mem_total_bytes": 1_822_000_000,
             "mem_available_bytes": 1_400_000_000, "cpu_temp_c": 40.9, "hardware": {"wifi_errors": 0}},
    "storage": {"written_per_day_bytes": 415_000_000, "wear_percent_max": 10},
}

# Two times on, 1 h 36 min off between them; a line cut short by the power
# going (NUL bytes), as the real log had.
RESOURCE = ("time,uptime_s,lh_rss_kb\n"
            "2026-10-03T09:30:00,60,1\n"
            "2026-10-03T09:40:00,660,1\n"
            "2026-10-03T20:50:00,40860,1\n"
            "\x00\x00\x002026-10-03T22:30:00,240,1\n"
            "2026-10-03T22:40:00,840,1\n")

# What the watchdogs logged, newest first - as system_status.watchdog_history gives them.
EVENTS = [{"when": "2026-10-03 07:40:00", "source": "wifi", "code": "lan_back", "values": "27955"},
          {"when": "2026-10-03 00:24:50", "source": "wifi", "code": "lan_silent", "values": "30"}]


def _summary(language="it", status=STATUS):
    return diagnostics.summary(status, load_translations(language), "abc123", RESOURCE, EVENTS, now=NOW)


def test_the_summary_tells_the_boards_story_in_its_language():
    text = _summary()
    assert "Accesa da 3 g 2 h senza interruzioni" in text
    assert "LocalHome in funzione da 1 g 2 h" in text
    assert "interrotto" in text and "in corso" in text, "how each session ended"
    assert "2026-10-03 09:29 -> 2026-10-03 20:50  accesa per 11 h 21 min" in text
    assert "spenta per circa 1 h 36 min" in text
    assert "3 byte nulli" in text, "a power cut mid-write"
    assert "Stanze Sensor: ok, ultimo dato 12 min fa" in text
    assert "Bagni Sensor: NON RISPONDE - probably offline" in text
    assert "Bagni Sensor: non risponde" in text, "the problem, as the admin panel says it"
    assert "LocalHome usa 95 MB" in text and "415 MB scritti al giorno" in text


def test_the_watchdogs_actions_are_in_words_oldest_first():
    text = _summary()
    silent = text.index("2026-10-03 00:24  Nessuno Shelly collegato da 30 min")
    back = text.index("2026-10-03 07:40  Gli Shelly raggiungono di nuovo la scheda (dopo 7 h 45 min)")
    assert silent < back


def test_a_radar_channel_is_pointed_out():
    assert "canale radar (DFS)" in _summary()
    for band, channel in (("5 GHz", 36), ("2.4 GHz", 13)):
        quiet = dict(STATUS, network={"wifi": dict(STATUS["network"]["wifi"], band=band, channel=channel)})
        assert "DFS" not in _summary(status=quiet)


def test_the_summary_follows_the_boards_language():
    text = _summary("en")
    assert "On for 3 d 2 h without a break" in text and "radar (DFS)" in text
    assert "Accesa" not in text


def test_the_summary_is_first_in_the_download_and_listed_in_its_readme(tmp_path):
    (tmp_path / "resource.csv").write_text(RESOURCE, encoding="utf-8")
    data = diagnostics.build_zip(STATUS, b"PK", str(tmp_path / "none"), str(tmp_path / "none"),
                                 journal=lambda *units, lines=0: None, resource_log=str(tmp_path / "resource.csv"),
                                 translations=load_translations("it"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.namelist()[:2] == ["README.txt", "summary.txt"]
    assert "Accesa da" in zf.read("summary.txt").decode("utf-8")
    assert "summary.txt" in zf.read("README.txt").decode("utf-8")


def test_a_summary_that_fails_doesnt_fail_the_download(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "summary", lambda *a, **k: 1 / 0)
    data = diagnostics.build_zip(STATUS, b"PK", str(tmp_path / "none"), str(tmp_path / "none"),
                                 journal=lambda *units, lines=0: None, resource_log=str(tmp_path / "none"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert "summary.txt" not in zf.namelist() and "summary.txt" in zf.read("README.txt").decode()


def test_the_network_log_leaves_out_the_watchdogs_every_minute_lines(tmp_path):
    noise = "".join(
        f"2026-10-07T10:{m:02d}:37+02:00 b systemd[1]: Starting localhome-wifi-watchdog.service - LocalHome Wi-Fi watchdog...\n"
        f"2026-10-07T10:{m:02d}:38+02:00 b systemd[1]: localhome-wifi-watchdog.service: Deactivated successfully.\n"
        f"2026-10-07T10:{m:02d}:38+02:00 b systemd[1]: Finished localhome-wifi-watchdog.service - LocalHome Wi-Fi watchdog.\n"
        for m in range(60))
    real = "2026-10-07T10:30:00+02:00 b NetworkManager[700]: <info>  device (wlan0): state change: activated -> disconnected\n"
    own = "2026-10-07T10:31:00+02:00 b localhome-wifi-watchdog.sh[900]: reconnect 3 wlan0\n"
    broker = "2026-10-07T10:32:00+02:00 b mosquitto[600]: New client connected from 192.168.1.34:51234 as shelly1g3-stanze\n"
    asked = []

    def journal(*units, lines=0):
        asked.append(units)
        if "NetworkManager" in units:
            return noise + real + own
        return broker if units == ("mosquitto",) else None

    data = diagnostics.build_zip({"problems": []}, b"PK", str(tmp_path / "none"), str(tmp_path / "none"),
                                 journal=journal, resource_log=str(tmp_path / "none"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.read("network.log").decode() == real + own
    assert zf.read("mosquitto.log").decode() == broker, "each Shelly connecting to the broker, and leaving"
    assert ("NetworkManager", "wpa_supplicant", "localhome-wifi-watchdog") in asked
