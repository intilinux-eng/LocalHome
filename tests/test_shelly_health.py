"""What the Shellys say about themselves (services/shelly_health.py): a
battery H&T's reports and signal as it publishes them, a mains Shelly's
status as it answers, the problems that come of both - with a clock, a
subscribe and an RPC played by the test, and the real history database."""
from __future__ import annotations

import json

from localhome.config import Integration
from localhome.services import shelly_health
from localhome.services.history import HistoryStore
from localhome.services.shelly_health import (
    HEARTBEAT_SECONDS as H, ShellyHealth, missed_reports, next_heartbeat, shelly_devices, split_reports,
)

HT = "shellyhtg3-c0ffee000002"
DIMMER = "shelly0110dimg3-c0ffee000003"

DIMMER_STATUS = {
    "light:0": {"id": 0, "output": True, "brightness": 50, "temperature": {"tC": 57.6, "tF": 135.7},
                "apower": 0.0, "voltage": 225.2},
    "sys": {"uptime": 623172, "restart_required": False, "available_updates": {"stable": {"version": "2.0.1"}}},
    "wifi": {"ssid": "Casa", "rssi": -79, "status": "got ip"},
    "mqtt": {"connected": True},
}


def ht_integration(name="Stanze Sensor", device=HT):
    return Integration("climate", "mqtt_json", {"name": name, "state_topic": [
        f"{device}/status/temperature:0", f"{device}/status/humidity:0",
        f"{device}/status/devicepower:0", f"{device}/status/wifi"]})


def dimmer_integrations():
    return [Integration("switch", "shelly_light", {"name": "VMC", "device_id": DIMMER}),
            Integration("number", "shelly_light", {"name": "VMC Velocità", "device_id": DIMMER})]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Broker:
    def __init__(self):
        self.handlers = {}

    def subscribe(self, topic, callback):
        self.handlers.setdefault(topic, []).append(callback)

    def publish(self, topic, payload):
        for callback in self.handlers.get(topic, []):
            callback(topic, json.dumps(payload).encode() if not isinstance(payload, str) else payload.encode())


class Rpc:
    def __init__(self, status):
        self.status = json.loads(json.dumps(status))
        self.calls = []
        self.fail = None

    def call(self, method, params=None):
        self.calls.append(method)
        if self.fail:
            raise RuntimeError(self.fail)
        if method == "Shelly.GetDeviceInfo":
            return {"model": "S3DM-0010WW", "ver": "2.0.0"}
        return json.loads(json.dumps(self.status))


def health(tmp_path, integrations, rpc=None, clock=None, store=None, reading_of=None):
    clock = clock or Clock()
    store = store or HistoryStore(str(tmp_path / "history.db"))
    broker = Broker()
    watcher = ShellyHealth(shelly_devices(integrations), store, subscribe=broker.subscribe,
                           rpc_for=lambda device_id: rpc, clock=clock, reading_of=reading_of)
    return watcher, broker, clock, store


def ht_wakes(broker, rssi=-70, battery=90, store=None, clock=None):
    broker.publish(f"{HT}/status/devicepower:0", {"id": 0, "battery": {"V": 3.1, "percent": battery},
                                                  "external": {"present": False}})
    broker.publish(f"{HT}/status/wifi", {"ssid": "Casa", "rssi": rssi})
    broker.publish(f"{HT}/status/sys", {"available_updates": {}})
    if store is not None:
        # What HistoryRecorder records from the sensor's own reading.
        store.record("Stanze Sensor", int(clock.t), {"temperature_c": 21.0, "wifi_rssi_dbm": rssi})


def by_id(watcher, device_id):
    return next(d for d in watcher.devices() if d["id"] == device_id)


# --------------------------------------------------------------- devices --

def test_the_shellys_are_found_in_the_integrations_whoever_wrote_them():
    found = shelly_devices([
        ht_integration(),
        *dimmer_integrations(),
        Integration("switch", "mqtt_json", {"name": "Valvola", "state_topic": "shellyplus1-aabbccddeeff/status/switch:0",
                                            "command_topic": "shellyplus1-aabbccddeeff/command/switch:0"}),
        Integration("switch", "mqtt_json", {"name": "Tasmota", "state_topic": "tele/plug/STATE"}),
        Integration("climate", "mqtt_json", {"name": "Gen1", "state_topic": "shellyht-A1B2C3/status"}),
        Integration("climate", "simulated", {"name": "Finto"}),
    ])
    assert [(d["id"], d["names"], d["battery"]) for d in found] == [
        (HT, ["Stanze Sensor"], True),
        ("shellyplus1-aabbccddeeff", ["Valvola"], False),
        (DIMMER, ["VMC", "VMC Velocità"], False),
    ]


# --------------------------------------------------------- missed reports --

def test_reports_every_two_hours_miss_nothing():
    reports = [i * H + 30 for i in range(12)]
    assert missed_reports(reports, [], since=0, now=12 * H) == 0


def test_a_heartbeat_that_never_came_is_counted_and_so_is_the_silence_since():
    reports = [0, H, 3 * H, 4 * H]           # the one at 2 h is missing
    assert missed_reports(reports, [], since=0, now=4 * H + 60) == 1
    assert missed_reports(reports, [], since=0, now=6.5 * H) == 3, "nothing since 4 h: 5 h's and 6 h's too"


def test_reports_lost_while_localhome_was_off_arent_the_sensors_fault():
    """The board unplugged from 2 h to 9 h: the sensor kept reporting to a
    broker that wasn't there. Only from LocalHome's start on is it judged."""
    reports = [0, H, 2 * H, 9.5 * H, 10.5 * H]
    assert missed_reports(reports, starts=[9 * H], since=0, now=11 * H) == 0


def test_a_threshold_report_between_heartbeats_is_simply_one_more():
    assert missed_reports([0, 0.4 * H, H, 2 * H], [], since=0, now=2.5 * H) == 0


# ------------------------------------------------ heartbeats and changes --

def _clock(hhmmss):
    h, m, s = (int(v) for v in hhmmss.split(":"))
    return h * 3600 + m * 60 + s


# The rooms' sensor over a real day (30 Sep 2026): twelve heartbeats 121
# minutes apart and three reports for a humidity change - one of them four
# minutes before a heartbeat.
ROOMS_DAY = ["18:45:00", "20:46:00", "22:41:59", "22:46:03", "24:47:00", "26:48:00", "28:49:01",
             "30:50:01", "32:51:01", "34:52:01", "36:14:01", "36:53:03", "38:23:02", "38:54:01", "40:54:59"]


def test_a_real_day_splits_into_heartbeats_and_change_reports():
    beats, changes = split_reports([_clock(t) for t in ROOMS_DAY])
    assert [t for t in ROOMS_DAY if _clock(t) in changes] == ["22:41:59", "36:14:01", "38:23:02"]
    assert len(beats) == 12


def test_a_change_report_doesnt_move_the_heartbeat_and_a_lost_one_keeps_the_rhythm():
    beats, changes = split_reports([0, 0.4 * H, H + 60, 3 * H + 120])      # the one at 2 H is lost
    assert (beats, changes) == ([0, H + 60, 3 * H + 120], [0.4 * H])


def test_a_new_rhythm_after_a_battery_change_starts_from_its_first_report():
    beats, changes = split_reports([0, H, 2 * H, 3.5 * H, 4.5 * H, 5.5 * H])
    assert changes == [] and beats[-3:] == [3.5 * H, 4.5 * H, 5.5 * H]


def test_the_next_heartbeat_is_the_next_slot_after_now():
    assert next_heartbeat([0, H], now=H + 600) == 2 * H
    assert next_heartbeat([0, H], now=3.5 * H) == 4 * H, "overdue: the next slot, lost ones are counted apart"
    assert next_heartbeat([], now=0) is None


# ---------------------------------------------------------------- battery --

def test_one_wake_is_one_report_with_its_signal_and_battery(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    ht_wakes(broker, rssi=-76, battery=88, store=store, clock=clock)
    device = by_id(watcher, HT)
    assert device["battery"] and device["name"] == "Stanze Sensor"
    assert (device["wifi_rssi_dbm"], device["battery_pct"], device["battery_v"]) == (-76, 88, 3.1)
    assert device["last_report_at"] == clock.t
    assert [v for _, v in store.values_since(HT, "report", 0)] == [1], "three topics, one wake"
    assert device["stats"]["reports_24h"] == 1
    assert device["stats"]["signal_24h"] == {"avg": -76, "min": -76, "count": 1}


def test_a_restart_of_localhome_remembers_the_last_report(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    ht_wakes(broker, rssi=-80)
    clock.t += 600
    readings = {"Stanze Sensor": {"ok": True, "wifi_rssi_dbm": -80, "battery_pct": 91, "seconds_since_update": 600}}
    again, _, _, _ = health(tmp_path, [ht_integration()], clock=clock, store=store, reading_of=readings.get)
    device = by_id(again, HT)
    assert device["last_report_at"] == clock.t - 600
    assert (device["wifi_rssi_dbm"], device["battery_pct"]) == (-80, 91), "the sensor's own last reading"


def test_before_any_report_the_sensors_own_reading_says_what_it_last_knew(tmp_path):
    readings = {"Stanze Sensor": {"ok": False, "wifi_rssi_dbm": -64, "last_value_age_seconds": 3000}}
    watcher, broker, clock, store = health(tmp_path, [ht_integration()], reading_of=readings.get)
    device = by_id(watcher, HT)
    assert device["wifi_rssi_dbm"] == -64 and device["last_report_at"] == clock.t - 3000


def test_reports_that_dont_get_through_become_a_problem(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    ht_wakes(broker)
    clock.t += 4.5 * H                        # four heartbeats, none arrived
    watcher.refresh_problems()
    assert watcher.problems() == [{"code": "device_reports_missed", "subject": "Stanze Sensor", "detail": "4"}]
    assert by_id(watcher, HT)["stats"]["missed_24h"] == 4


def test_a_weak_signal_isnt_a_problem_as_long_as_the_reports_arrive(tmp_path):
    """The bathroom sensor sits at -88/-90 dBm and has got every report
    through for weeks: its signal is shown, never counted as a fault."""
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    for rssi in (-70, -71, -95):
        ht_wakes(broker, rssi=rssi, store=store, clock=clock)
        clock.t += H
    watcher.refresh_problems()
    assert watcher.problems() == []
    for rssi in (-88, -90, -89, -91):
        ht_wakes(broker, rssi=rssi, store=store, clock=clock)
        clock.t += H
    watcher.refresh_problems()
    assert watcher.problems() == []
    assert by_id(watcher, HT)["stats"]["signal_24h"]["avg"] <= -85


# ------------------------------------------------------------------ mains --

def test_a_mains_shelly_is_asked_for_its_temperature_signal_and_updates(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    broker.publish(f"{DIMMER}/online", "true")
    watcher.poll_once()
    device = by_id(watcher, DIMMER)
    assert device["online"] is True and device["error"] is None
    assert (device["device_temp_c"], device["wifi_rssi_dbm"], device["voltage_v"]) == (57.6, -79, 225.2)
    assert (device["model"], device["firmware"], device["update"]) == ("S3DM-0010WW", "2.0.0", "2.0.1")
    assert device["booted_at"] == clock.t - 623172
    assert [v for _, v in store.values_since(DIMMER, "device_temp_c", 0)] == [57.6]
    watcher.poll_once()
    assert rpc.calls.count("Shelly.GetDeviceInfo") == 1, "the model is asked once"


def test_a_mains_shelly_that_restarted_is_counted(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    watcher.poll_once()
    clock.t += 300
    rpc.status["sys"]["uptime"] = 623172 + 300            # still the same start
    watcher.poll_once()
    assert by_id(watcher, DIMMER)["stats"]["restarts_7d"] == 0
    clock.t += 3600
    rpc.status["sys"]["uptime"] = 120                     # a power cut, 2 minutes ago
    watcher.poll_once()
    assert by_id(watcher, DIMMER)["stats"]["restarts_7d"] == 1
    assert by_id(watcher, DIMMER)["booted_at"] == clock.t - 120


def test_a_hot_shelly_is_a_problem(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    rpc.status["light:0"]["temperature"]["tC"] = 83.4
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    watcher.poll_once()
    assert watcher.problems() == [{"code": "device_hot", "subject": "VMC", "detail": "83°C"}]


def test_a_mains_shelly_not_answering_says_why_and_keeps_what_it_knew(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    watcher.poll_once()
    rpc.fail = "no RPC reply - device is probably offline"
    watcher.poll_once()
    device = by_id(watcher, DIMMER)
    assert device["error"] == "no RPC reply - device is probably offline"
    assert device["device_temp_c"] == 57.6


def test_the_admin_api_lists_nothing_without_a_shelly(tmp_path):
    from localhome.web.app import create_app
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    client = create_app(str(tmp_path / "config.yaml")).test_client()
    data = client.get("/api/climate-admin/shelly-health").get_json()
    assert data["ok"] and data["devices"] == [] and data["hot_c"] == shelly_health.HOT_C
