"""What the Shellys say about themselves (services/shelly_health.py): a
battery H&T's reports and signal as it publishes them, a mains Shelly's
status as it answers, the problems that come of both - with a clock, a
subscribe and an RPC played by the test, and the real history database."""
from __future__ import annotations

import json
from datetime import datetime

from localhome.config import Integration
from localhome.services import shelly_health
from localhome.services.history import HistoryStore
from localhome.services.shelly_health import (
    HEARTBEAT_SECONDS as H, ShellyHealth, expected_settings, missed_reports, next_heartbeat, settings_wrong,
    shelly_devices, split_reports,
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

def test_a_shelly_whose_mqtt_prefix_was_changed_is_found_by_its_topics():
    """Seen on the production board: "shelly1g3-stanze", set in the Shelly
    app - left out, and with it the check of its settings. A Gen2/Gen3 is
    known by its topics' shape; another brand's isn't taken for one."""
    found = shelly_devices([
        Integration("switch", "mqtt_json", {"name": "Stanze Valve", "state_topic": "shelly1g3-stanze/status/switch:0",
                                            "command_topic": "shelly1g3-stanze/command/switch:0", "hold_seconds": 900}),
        Integration("switch", "mqtt_json", {"name": "Tasmota", "state_topic": "stat/plug/POWER",
                                            "command_topic": "cmnd/plug/POWER"}),
        Integration("switch", "mqtt_json", {"name": "Z2M", "state_topic": "zigbee2mqtt/Lamp",
                                            "command_topic": "zigbee2mqtt/Lamp/set"}),
        Integration("climate", "tuya_air", {"name": "Aria", "device_id": "bf1234567890abcdef"}),
    ])
    assert [(d["id"], d["names"]) for d in found] == [("shelly1g3-stanze", ["Stanze Valve"])]
    assert found[0]["expect"]["auto_off"] == 900


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
    rpc.status["sys"].update(uptime=120, reset_reason=1)  # a power cut, 2 minutes ago
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


# ---------------------------------------------------- a simulated H&T --

def test_a_simulated_ht_is_watched_like_a_real_one(tmp_path):
    """No sensor on a test bench: the simulated H&T's reports reach this
    panel through the simulated world, on the real one's topics."""
    from localhome.drivers.simulated import world
    from localhome.drivers.simulated.climate import SimulatedClimateDriver
    from localhome.services.shelly_health import SIMULATED_PREFIX, simulated_devices

    integrations = [Integration("climate", "simulated", {"name": "Banco Sensor", "mimic_ht_gen3": True}),
                    Integration("climate", "simulated", {"name": "Outdoor Sensor"})]
    devices = simulated_devices(integrations)
    assert devices == [{"id": "simulated:Banco Sensor", "names": ["Banco Sensor"], "battery": True, "simulated": True}]
    clock = Clock()
    watcher = ShellyHealth(devices, HistoryStore(str(tmp_path / "history.db")), clock=clock,
                           subscribe=lambda topic, handler: world.subscribe(topic[len(SIMULATED_PREFIX):], handler))
    reading = SimulatedClimateDriver(name="Banco Sensor", mimic_ht_gen3=True).read()
    device = by_id(watcher, "simulated:Banco Sensor")
    assert device["simulated"] and device["last_report_at"] == clock.t
    assert (device["battery_pct"], device["wifi_rssi_dbm"]) == (reading["battery_pct"], reading["wifi_rssi_dbm"])
    assert device["stats"]["reports_24h"] == 1 and device["stats"]["missed_24h"] == 0


def test_the_admin_api_lists_a_bench_with_simulated_sensors_and_no_broker(tmp_path):
    from localhome.web.app import create_app
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations:\n"
                                          "  - kind: climate\n    type: simulated\n    name: Banco Sensor\n"
                                          "    mimic_ht_gen3: true\n", encoding="utf-8")
    client = create_app(str(tmp_path / "config.yaml")).test_client()
    data = client.get("/api/climate-admin/shelly-health").get_json()
    assert [(d["id"], d["simulated"], d["battery"]) for d in data["devices"]] == [("simulated:Banco Sensor", True, True)]
    # Listening before the sensor starts: its first report, on its first
    # read, is counted - it used to go unheard until the heartbeat.
    import time
    deadline = time.time() + 10
    while not client.get("/api/sensors/Banco%20Sensor").get_json().get("ok") and time.time() < deadline:
        time.sleep(0.1)
    device = client.get("/api/climate-admin/shelly-health").get_json()["devices"][0]
    assert device["stats"]["reports_24h"] == 1



# ---------------------------------------------------------- relay settings --
# A relay LocalHome commands has to be set as a valve needs it - read back
# from the device, said by name when it isn't (docs/shelly-gen3.md).

RELAY = "shellyplus1-c0ffee000001"


def valve_integration(**extra):
    return Integration("switch", "mqtt_json", {
        "name": "Stanze Valve", "state_topic": f"{RELAY}/status/switch:0", "command_topic": f"{RELAY}/command/switch:0",
        "hold_seconds": 900, **extra})


# As read from the production board's Plus 1 on 3 October 2026, after a
# first try in the app: the power-on state right, the relay type not, no Auto OFF.
RELAY_CONFIG = {
    "switch:0": {"id": 0, "in_mode": "activate", "initial_state": "off", "auto_on": False, "auto_on_delay": 60.0,
                 "auto_off": False, "auto_off_delay": 60.0},
    "mqtt": {"enable": True, "server": "antonio.local:1883", "rpc_ntf": True, "status_ntf": True,
             "enable_rpc": True, "enable_control": True},
}


def test_what_a_held_valve_relay_must_be_set_to():
    assert expected_settings(valve_integration()) == {
        "in_mode": "detached", "mqtt_control": True, "mqtt_status": True, "initial_state": "off", "auto_off": 900}
    assert expected_settings(valve_integration(rest_state="on"))["auto_on"] == 900
    assert expected_settings(ht_integration()) == {} and expected_settings(dimmer_integrations()[0]) == {}


def test_the_bypass_keeps_its_last_state_and_any_other_relay_only_needs_mqtt():
    """The bypass, normally open, closed by LocalHome all summer: if the
    board stops it stays as it was - restored at power-on, no timer. A
    Shelly relay that isn't the plant's (a lamp with its wall switch on
    SW) isn't told to detach its input."""
    bypass = expected_settings(valve_integration(hold_seconds=None, shelly_role="bypass"))
    assert bypass == {"in_mode": "detached", "mqtt_control": True, "mqtt_status": True,
                      "initial_state": "restore_last", "auto_off": 0, "auto_on": 0}
    assert expected_settings(valve_integration(hold_seconds=None)) == {"mqtt_control": True, "mqtt_status": True}


def test_the_settings_set_otherwise_are_said_by_name():
    wrong = settings_wrong(expected_settings(valve_integration()), RELAY_CONFIG)
    assert wrong == [{"key": "in_mode", "value": "activate", "expected": "detached"},
                     {"key": "auto_off", "value": 0, "expected": 900}]
    right = json.loads(json.dumps(RELAY_CONFIG))
    right["switch:0"].update(in_mode="detached", auto_off=True, auto_off_delay=900.0)
    assert settings_wrong(expected_settings(valve_integration()), right) == []
    assert settings_wrong(expected_settings(valve_integration()), {}) == [], "nothing reported, nothing said"


class RelayRpc(Rpc):
    def __init__(self, status, config):
        super().__init__(status)
        self.config = json.loads(json.dumps(config))

    def call(self, method, params=None):
        if method == "Shelly.GetConfig":
            self.calls.append(method)
            return json.loads(json.dumps(self.config))
        return super().call(method, params)


def test_a_relay_set_otherwise_shows_in_devices_and_as_a_problem(tmp_path):
    status = {"switch:0": {"id": 0, "output": False, "temperature": {"tC": 52.4}},
              "sys": {"uptime": 3000}, "wifi": {"rssi": -57}}
    rpc = RelayRpc(status, RELAY_CONFIG)
    watcher, broker, clock, store = health(tmp_path, [valve_integration()], rpc=rpc)
    watcher.poll_once()
    assert [s["key"] for s in by_id(watcher, RELAY)["settings_wrong"]] == ["in_mode", "auto_off"]
    assert watcher.problems() == [{"code": "shelly_settings", "subject": "Stanze Valve",
                                   "detail": "in_mode, auto_off", "settings": ["in_mode", "auto_off"]}]
    rpc.config["switch:0"].update(in_mode="detached", auto_off=True, auto_off_delay=900.0)   # fixed in the app
    watcher.poll_once()
    assert by_id(watcher, RELAY)["settings_wrong"] == [] and watcher.problems() == []


def test_a_dimmer_isnt_asked_for_its_settings(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    watcher.poll_once()
    assert "Shelly.GetConfig" not in rpc.calls



def test_a_device_restarting_for_an_update_is_asked_again_and_its_new_firmware_read(tmp_path):
    """Seen on the production board: asked in the middle of a firmware
    update, the Plus 1 said "shutting down in 2267 ms" - an error worth
    a quick second ask, and a version worth reading again."""
    status = {"switch:0": {"id": 0, "output": False}, "sys": {"uptime": 50000}, "wifi": {"rssi": -57}}
    rpc = RelayRpc(status, RELAY_CONFIG)
    watcher, broker, clock, store = health(tmp_path, [valve_integration()], rpc=rpc)
    assert watcher.poll_once() is False
    rpc.fail = "RPC error: {'code': -109, 'message': 'shutting down in 2267 ms'}"
    assert watcher.poll_once() is True, "worth asking again soon"
    rpc.fail = None
    clock.t += 120
    rpc.status["sys"]["uptime"] = 60                      # back up, on the new firmware
    asked = rpc.calls.count("Shelly.GetDeviceInfo")
    assert watcher.poll_once() is False
    assert by_id(watcher, RELAY)["error"] is None
    assert rpc.calls.count("Shelly.GetDeviceInfo") == asked + 1, "the version is read again after a restart"



def test_settings_are_read_only_when_they_can_have_changed(tmp_path):
    """First answer, then not again while all is right - only while one is
    wrong (so a fix shows), and after a restart of the Shelly. (A device
    that doesn't report its configuration revision.)"""
    status = {"switch:0": {"id": 0, "output": False}, "sys": {"uptime": 50000}, "wifi": {"rssi": -57}}
    right = json.loads(json.dumps(RELAY_CONFIG))
    right["switch:0"].update(in_mode="detached", auto_off=True, auto_off_delay=900.0)
    rpc = RelayRpc(status, right)
    watcher, broker, clock, store = health(tmp_path, [valve_integration()], rpc=rpc)
    for _ in range(4):
        clock.t += 300
        rpc.status["sys"]["uptime"] += 300
        watcher.poll_once()
    assert rpc.calls.count("Shelly.GetConfig") == 1, "all right: read once"
    clock.t += 300
    rpc.status["sys"]["uptime"] = 40                      # restarted (an update)
    rpc.config = json.loads(json.dumps(RELAY_CONFIG))       # ...which reset a setting
    watcher.poll_once()
    assert rpc.calls.count("Shelly.GetConfig") == 2
    assert [s["key"] for s in by_id(watcher, RELAY)["settings_wrong"]] == ["in_mode", "auto_off"]
    rpc.config = right                                      # fixed in the app
    clock.t += 300
    rpc.status["sys"]["uptime"] += 300
    watcher.poll_once()
    assert by_id(watcher, RELAY)["settings_wrong"] == [] and rpc.calls.count("Shelly.GetConfig") == 3



def test_a_restart_somebody_asked_for_isnt_a_problem(tmp_path):
    """Seen on the production board: an update, settings changed in the
    app and test restarts made a healthy Plus 1 "to check" for a week. The
    Shelly says why it started - 3, a restart asked for - and only the
    others (a power cut, a crash) count as unexpected."""
    status = {"switch:0": {"id": 0, "output": False}, "sys": {"uptime": 50000}, "wifi": {"rssi": -57}}
    rpc = RelayRpc(status, RELAY_CONFIG)
    watcher, broker, clock, store = health(tmp_path, [valve_integration()], rpc=rpc)
    watcher.poll_once()
    for reason in (3, 3, 1):                              # update, settings change, power cut
        clock.t += 600
        rpc.status["sys"].update(uptime=60, reset_reason=reason)
        watcher.poll_once()
    stats = by_id(watcher, RELAY)["stats"]
    assert (stats["restarts_7d"], stats["restarts_wanted_7d"]) == (1, 2)



def test_a_setting_changed_in_the_app_is_read_again_when_the_revision_moves(tmp_path):
    """The status asked every 5 minutes carries the configuration revision:
    settings are read again when it moves, and only then - wrong or not."""
    status = {"switch:0": {"id": 0, "output": False}, "sys": {"uptime": 50000, "cfg_rev": 21}, "wifi": {"rssi": -57}}
    rpc = RelayRpc(status, RELAY_CONFIG)
    watcher, broker, clock, store = health(tmp_path, [valve_integration()], rpc=rpc)
    watcher.poll_once()
    assert [s["key"] for s in by_id(watcher, RELAY)["settings_wrong"]] == ["in_mode", "auto_off"]
    for _ in range(3):                                    # wrong, but nothing changed: not asked again
        clock.t += 300
        rpc.status["sys"]["uptime"] += 300
        watcher.poll_once()
    assert rpc.calls.count("Shelly.GetConfig") == 1
    rpc.config["switch:0"].update(in_mode="detached", auto_off=True, auto_off_delay=900.0)   # fixed in the app
    rpc.status["sys"]["cfg_rev"] = 23
    clock.t += 300
    rpc.status["sys"]["uptime"] += 300
    watcher.poll_once()
    assert rpc.calls.count("Shelly.GetConfig") == 2
    assert by_id(watcher, RELAY)["settings_wrong"] == [] and watcher.problems() == []


def test_a_mains_shelly_back_online_is_asked_at_once(tmp_path, monkeypatch):
    """Seen on the production board: two power cuts 5 minutes apart, one
    restart counted - no check came between them. Back online after
    being offline (however briefly: "offline" and "online" 50 ms apart),
    it's asked at once, not at the next 5-minute check."""
    import threading
    import time as _time
    import localhome.services.shelly_health as module
    monkeypatch.setattr(module, "BACK_ONLINE_SETTLE_SECONDS", 0)
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    broker.publish(f"{DIMMER}/online", "true")             # LocalHome starting: just its state
    assert not watcher._wake.is_set()
    polled = threading.Event()
    original = watcher.poll_once
    watcher.poll_once = lambda: (original(), polled.set())[0]
    watcher.start(every=3600, first=0, retry=3600)
    assert polled.wait(5)
    polled.clear()
    clock.t += 3600
    rpc.status["sys"].update(uptime=8, reset_reason=1)     # 5 s without power
    broker.publish(f"{DIMMER}/online", "false")
    broker.publish(f"{DIMMER}/online", "true")
    assert polled.wait(5), "asked at once, not in an hour"
    deadline = _time.time() + 5
    while by_id(watcher, DIMMER)["stats"]["restarts_7d"] != 1 and _time.time() < deadline:
        _time.sleep(0.05)
    assert by_id(watcher, DIMMER)["stats"]["restarts_7d"] == 1


def test_a_mains_shelly_that_didnt_answer_is_asked_again_as_soon_as_it_reports(tmp_path, monkeypatch):
    """Seen on the production board after a boot without network and a
    router restart: both mains Shellys back and reporting for minutes,
    the admin page still saying "not answering" until the next 5-minute
    check. Heard from again, one that didn't answer is asked at once - at
    most once a minute, for one whose RPC really is off."""
    import localhome.services.shelly_health as module
    rpc = Rpc(DIMMER_STATUS)
    rpc.fail = "no RPC reply - device is probably offline"
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    watcher.poll_once()
    assert by_id(watcher, DIMMER)["error"]
    broker.publish(f"{DIMMER}/status/sys", {"uptime": 120})
    assert watcher._wake.is_set(), "heard from: asked again"
    watcher._wake.clear()
    broker.publish(f"{DIMMER}/status/wifi", {"rssi": -70, "ssid": "Casa"})
    assert not watcher._wake.is_set(), "not again within the minute"
    monkeypatch.setattr(module, "ALIVE_RECHECK_SECONDS", 0)
    rpc.fail = None
    watcher.poll_once()
    broker.publish(f"{DIMMER}/status/sys", {"uptime": 180})
    assert not watcher._wake.is_set(), "answering: nothing to ask again"


def test_started_a_battery_report_is_written_off_the_mqtt_thread(tmp_path):
    """The MQTT thread hands what it hears to a writer thread: a slow
    disk (an SQLite commit on the board's eMMC) never holds it."""
    import threading
    import time as _time
    store = HistoryStore(str(tmp_path / "history.db"))
    real_record = store.record
    written = threading.Event()

    def slow_record(sensor, ts, fields):
        _time.sleep(1.0)
        real_record(sensor, ts, fields)
        if "report" in fields:
            written.set()
    store.record = slow_record
    watcher, broker, clock, _ = health(tmp_path, [ht_integration()], store=store)
    watcher.start(every=3600, first=3600, retry=3600)
    started = _time.monotonic()
    ht_wakes(broker)
    assert _time.monotonic() - started < 0.5, "the MQTT thread didn't wait for the disk"
    assert written.wait(5)
    assert [v for _, v in store.values_since(HT, "report", 0)] == [1]


def test_a_gen3_relays_own_counters_give_its_switch_ons_and_on_time(tmp_path):
    """A Shelly 1 Gen3 counts its relay (switch:0.counts, kept across its
    restarts): its switch-ons in the last 24 hours and its on time today,
    from the differences - a reset counted again from 0. A Plus 1 has no
    counts: nothing said."""
    status = {"switch:0": {"id": 0, "output": True, "counts": {"switch_on": 100, "on_time": 5000}},
              "sys": {"uptime": 50000, "cfg_rev": 1}, "wifi": {"rssi": -50}}
    rpc = RelayRpc(status, RELAY_CONFIG)
    clock = Clock(datetime(2026, 10, 5, 8, 0).timestamp())
    watcher, broker, clock, store = health(tmp_path, [valve_integration()], rpc=rpc, clock=clock)
    watcher.poll_once()
    for switch_ons, on_time in ((103, 5600), (2, 400), (5, 1000)):   # the second: counters reset
        clock.t += 3600
        rpc.status["sys"]["uptime"] += 3600
        rpc.status["switch:0"]["counts"].update(switch_on=switch_ons, on_time=on_time)
        watcher.poll_once()
    stats = by_id(watcher, RELAY)["stats"]
    assert stats["switch_ons_24h"] == 3 + 2 + 3
    assert stats["on_seconds_today"] == 600 + 400 + 600
    assert watcher.problems() == [] or all(p["code"] != "relay_short_cycling" for p in watcher.problems())
    plus1 = {"switch:0": {"id": 0, "output": False}, "sys": {"uptime": 50000}, "wifi": {"rssi": -50}}
    other, *_ = health(tmp_path / "p", [valve_integration()], rpc=RelayRpc(plus1, RELAY_CONFIG))
    other.poll_once()
    assert by_id(other, RELAY)["stats"]["switch_ons_24h"] is None and by_id(other, RELAY)["stats"]["on_seconds_today"] is None


def test_too_many_switch_ons_in_a_day_is_short_cycling(tmp_path):
    status = {"switch:0": {"id": 0, "output": True, "counts": {"switch_on": 10, "on_time": 0}},
              "sys": {"uptime": 50000, "cfg_rev": 1}, "wifi": {"rssi": -50}}
    rpc = RelayRpc(status, RELAY_CONFIG)
    watcher, broker, clock, store = health(tmp_path, [valve_integration()], rpc=rpc)
    watcher.poll_once()
    for _ in range(4):
        clock.t += 3600
        rpc.status["sys"]["uptime"] += 3600
        rpc.status["switch:0"]["counts"]["switch_on"] += 15
        watcher.poll_once()
    found = [p for p in watcher.problems() if p["code"] == "relay_short_cycling"]
    assert found == [{"code": "relay_short_cycling", "subject": "Stanze Valve", "detail": "60"}]



# ------------------------------------------------------------ verdict --
# One verdict per Shelly - ok, attention (a reason to act on, said) or
# offline - for the admin page and the system's problems alike. Seen on
# the production board: a dimmer whose power was cut three times in a
# test, and a firmware update, made a device that worked "to check" for a
# week, while the system status listed no problem for it.

def test_power_cuts_and_an_update_are_notes_not_trouble(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    watcher.poll_once()
    for reason in (1, 1, 1, 3):                          # three power cuts, then an update
        clock.t += 600
        rpc.status["sys"].update(uptime=60, reset_reason=reason)
        watcher.poll_once()
    device = by_id(watcher, DIMMER)
    stats = device["stats"]
    assert (stats["restarts_power_7d"], stats["restarts_wanted_7d"], stats["restarts_7d"]) == (3, 1, 3)
    verdict = device["assessment"]
    assert verdict["state"] == "ok" and verdict["reasons"] == []
    assert [n["code"] for n in verdict["notes"]] == ["power_cuts", "restarts_wanted", "weak_signal"]
    assert verdict["notes"][0]["count"] == 3 and verdict["notes"][0]["at"] == clock.t - 600 - 60
    watcher.refresh_problems()
    assert watcher.problems() == []


def test_a_crash_or_a_brownout_is_a_reason_to_act(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    watcher.poll_once()
    for reason in (4, 9):                                # a crash, then a brownout
        clock.t += 600
        rpc.status["sys"].update(uptime=60, reset_reason=reason)
        watcher.poll_once()
    verdict = by_id(watcher, DIMMER)["assessment"]
    assert verdict["state"] == "attention"
    assert [(r["code"], r["detail"]) for r in verdict["reasons"]] == [("crashes", 1), ("power_unstable", 1)]
    watcher.refresh_problems()
    assert [(p["code"], p["detail"]) for p in watcher.problems()] == [("device_crashes", "1"), ("device_power_unstable", "1")]


def test_restarts_counted_before_their_reason_was_read_are_only_a_note(tmp_path):
    rpc = Rpc(DIMMER_STATUS)
    watcher, broker, clock, store = health(tmp_path, dimmer_integrations(), rpc=rpc)
    store.record(DIMMER, int(clock.t - 3 * 86400), {"restart_unexpected": 1})
    watcher.poll_once()
    verdict = by_id(watcher, DIMMER)["assessment"]
    assert verdict["state"] == "ok" and verdict["notes"][0] == {"code": "restarts_unknown", "count": 1}


def _ht(**extra):
    device = {"battery": True, "last_report_at": 1000.0, "battery_pct": 80, "external_power": False,
              "settings_wrong": [], "restart_required": False, "errors": [], "device_temp_c": None,
              "stats": {"missed_24h": 0, "signal_24h": {"avg": -60}}}
    device.update(extra)
    return device


def test_a_battery_sensor_low_late_or_losing_reports_says_which():
    now = 1000.0 + 600
    assert shelly_health.assess(_ht(), now) == {"state": "ok", "reasons": [], "notes": []}
    assert shelly_health.assess(_ht(battery_pct=8), now)["reasons"] == [{"code": "battery_low", "detail": 8}]
    assert shelly_health.assess(_ht(battery_pct=8, external_power=True), now)["state"] == "ok", "on USB-C"
    late = shelly_health.assess(_ht(), 1000.0 + 2 * H + 60)
    assert late["state"] == "attention" and late["reasons"] == [{"code": "report_late", "detail": 2 * H + 60, "period": None, "early": False}]
    missed = shelly_health.assess(_ht(stats={"missed_24h": 3, "signal_24h": {"avg": -86}}), now)
    assert missed["reasons"] == [{"code": "reports_missed", "detail": 3}]
    assert missed["notes"] == [], "the lost reports say it - not a weak signal on top"
    weak = shelly_health.assess(_ht(stats={"missed_24h": 0, "signal_24h": {"avg": -86}}), now)
    assert weak == {"state": "ok", "reasons": [], "notes": [{"code": "weak_signal", "avg": -86}]}


def test_a_mains_shelly_that_doesnt_answer_is_offline_whatever_else():
    device = {"battery": False, "online": False, "error": None, "settings_wrong": [], "restart_required": True,
              "errors": [], "device_temp_c": None, "stats": {}}
    verdict = shelly_health.assess(device, 0)
    assert verdict["state"] == "offline" and verdict["reasons"] == [{"code": "restart_required"}]


def test_each_restart_is_counted_under_its_reason():
    assert [shelly_health.restart_field(r) for r in (3, 1, 4, 7, 9, 0, None, "x")] == [
        "restart_wanted", "restart_power", "restart_crash", "restart_crash", "restart_brownout",
        "restart_unexpected", "restart_unexpected", "restart_unexpected"]


# ------------------------------------------------------ USB-C or batteries --
# An H&T Gen3 runs on its batteries or on USB-C (its batteries still in):
# its devicepower says which ("external": {"present": true}). Seen on the
# production board: one sensor on each.

def _power(broker, present, percent=87, device=HT):
    broker.publish(f"{device}/status/devicepower:0",
                   {"id": 0, "battery": {"V": 5.76, "percent": percent}, "external": {"present": present}})


def test_an_h_and_t_on_usb_c_is_known_as_such_and_its_battery_isnt_a_worry(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _power(broker, True, percent=5)
    device = by_id(watcher, HT)
    assert device["external_power"] is True
    assert device["assessment"]["state"] == "ok", "a low battery on USB-C isn't what keeps it going"
    assert [v for _, v in store.values_since(HT, "external_power", 0)] == [1]


def test_going_from_usb_c_to_its_batteries_is_said_not_a_fault(tmp_path):
    """Either is as good: the batteries are there to do it."""
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _power(broker, True)
    clock.t += 3600
    _power(broker, False, percent=86)                     # unplugged: on its batteries now
    verdict = by_id(watcher, HT)["assessment"]
    assert verdict["state"] == "ok" and verdict["reasons"] == []
    assert {"code": "power_switched", "at": clock.t} in verdict["notes"]
    watcher.refresh_problems()
    assert watcher.problems() == []
    clock.t += 600
    _power(broker, True)                                  # plugged back
    assert not any(n["code"] == "power_switched" for n in by_id(watcher, HT)["assessment"]["notes"])
    assert [v for _, v in store.values_since(HT, "external_power", 0)] == [1, 0, 1]


def test_a_sensor_always_on_its_batteries_records_nothing_about_power(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    for _ in range(3):
        clock.t += 7200
        _power(broker, False)
    assert store.values_since(HT, "external_power", 0) == []
    assert by_id(watcher, HT)["assessment"]["state"] == "ok"


def test_its_power_source_is_known_again_after_a_restart(tmp_path):
    """Until it reports again - up to 2 hours - the admin page kept saying
    "battery" for a sensor on USB-C after every restart."""
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _power(broker, True)
    clock.t += 600
    restarted, _, _, _ = health(tmp_path, [ht_integration()], clock=clock, store=store)
    assert by_id(restarted, HT)["external_power"] is True
    _power(broker, False)                                 # the first watcher hears it unplugged...
    clock.t += 600
    again, _, _, _ = health(tmp_path, [ht_integration()], clock=clock, store=store)
    assert any(n["code"] == "power_switched" for n in by_id(again, HT)["assessment"]["notes"]), "...and the next start knows"


# Its data are asynchronous - each message is the latest value, whenever
# it comes. Only "late" needs to know when the next one is due, and a
# sleeping H&T says it: its status's wakeup_period. Seen on the production
# board: 7200 on its batteries, 600 on USB-C, 7200 just after power-on.
# No firmware's cycle in the code.

def _wake(broker, period, device=HT, present=True):
    broker.publish(f"{device}/status/sys", {"uptime": 4, "wakeup_period": period,
                                            "wakeup_reason": {"boot": "deepsleep_wake", "cause": "status_update"}})
    _power(broker, present, device=device)


def test_on_usb_c_a_silence_past_the_period_it_declares_is_late_and_a_problem(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _wake(broker, 600)
    clock.t += 29 * 60
    assert by_id(watcher, HT)["assessment"]["state"] == "ok", "within 3 of its 10-minute periods"
    clock.t += 2 * 60
    reasons = by_id(watcher, HT)["assessment"]["reasons"]
    assert reasons == [{"code": "report_late", "detail": 31 * 60, "period": 600, "early": True}]
    watcher.refresh_problems()
    assert watcher.problems() == [{"code": "device_report_late", "subject": "Stanze Sensor", "detail": "31 min"}],         "its reading stays good for hours - said in minutes, not seconds"


def test_a_firmware_that_declares_another_period_is_taken_at_its_word(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _wake(broker, 1800)                                   # every 30 minutes instead
    clock.t += 80 * 60
    assert by_id(watcher, HT)["assessment"]["state"] == "ok"
    clock.t += 20 * 60
    assert by_id(watcher, HT)["assessment"]["reasons"][0]["code"] == "report_late"


def test_on_its_batteries_its_2_hour_period_keeps_the_4_hour_bound(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _wake(broker, 7200, present=False)
    clock.t += 4 * 3600 - 60
    assert by_id(watcher, HT)["assessment"]["state"] == "ok"
    clock.t += 120
    reasons = by_id(watcher, HT)["assessment"]["reasons"]
    assert reasons[0]["code"] == "report_late" and reasons[0]["early"] is False
    watcher.refresh_problems()
    assert watcher.problems() == [], "its stale reading says it already"


def test_a_sensor_that_declares_nothing_gets_the_heartbeat_bound(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _power(broker, True)                                  # no sys, no period
    clock.t += 3 * 3600
    assert by_id(watcher, HT)["assessment"]["state"] == "ok"
    assert shelly_health.late_after(None) == 2 * H and shelly_health.late_after(60) == 1200


def test_the_period_it_declared_is_known_again_after_a_restart(tmp_path):
    """Seen on the production board: its USB-C unplugged, the sensor off -
    LocalHome restarted then would never hear its period again, and
    waited 4 hours instead of 30 minutes to say it."""
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _wake(broker, 600)
    _wake(broker, 600)                                    # unchanged: recorded once
    assert [v for _, v in store.values_since(HT, "wakeup_period", 0)] == [600]
    clock.t += 10 * 60
    restarted, _, _, _ = health(tmp_path, [ht_integration()], clock=clock, store=store)
    clock.t += 25 * 60
    assert by_id(restarted, HT)["assessment"]["reasons"][0]["code"] == "report_late"


def test_a_late_sensor_that_reports_again_leaves_the_problems_at_once(tmp_path):
    watcher, broker, clock, store = health(tmp_path, [ht_integration()])
    _wake(broker, 600)
    clock.t += 31 * 60
    watcher.refresh_problems()
    assert [p["code"] for p in watcher.problems()] == ["device_report_late"]
    clock.t += 60
    _wake(broker, 7200, present=False)                    # back, on its batteries
    assert watcher.problems() == [], "not at the next check, minutes later"
