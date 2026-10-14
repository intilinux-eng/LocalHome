"""Exercise the actual driver logic (JSON parsing, field mapping, command
construction, clamping) against a FakeMqttConnection - no real broker, no
network, but real driver code, including the plain-string-payload path
that a live-broker test on 2026-09-12 caught a genuine bug in (see
services history / THIRD_PARTY_NOTICES for context - non-JSON payloads
used to be silently dropped).
"""
from __future__ import annotations

import json

import pytest

from localhome.drivers.mqtt.cover import MqttCoverDriver
from localhome.drivers.mqtt.number import MqttNumberDriver
from localhome.drivers.mqtt.sensor import MqttSensorDriver
from localhome.drivers.mqtt.switch import MqttSwitchDriver
from tests.mqtt_fakes import FakeMqttConnection

BROKER = {"host": "unused-in-tests"}


@pytest.fixture
def fake_connection(monkeypatch):
    conn = FakeMqttConnection()
    monkeypatch.setattr("localhome.drivers.mqtt.base.get_connection", lambda broker: conn)
    return conn


@pytest.fixture
def fake_clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr("localhome.drivers.mqtt.base.time.time", lambda: now["t"])

    def advance(seconds):
        now["t"] += seconds

    return advance


# --------------------------------------------------------------- sensor ----

def test_sensor_raises_before_any_message_is_received(fake_connection):
    driver = MqttSensorDriver(
        name="Workshop Plug", broker=BROKER, state_topic="tele/plug/SENSOR",
        fields={"power_w": "ENERGY.Power"},
    )
    with pytest.raises(RuntimeError, match="no MQTT message"):
        driver.read()


def test_sensor_extracts_nested_json_fields(fake_connection):
    driver = MqttSensorDriver(
        name="Workshop Plug", broker=BROKER, state_topic="tele/plug/SENSOR",
        fields={"power_w": "ENERGY.Power", "voltage_v": "ENERGY.Voltage"},
    )
    fake_connection.deliver("tele/plug/SENSOR", json.dumps({"ENERGY": {"Power": 42.3, "Voltage": 231.0}}))

    reading = driver.read()

    assert reading == {"ok": True, "name": "Workshop Plug", "power_w": 42.3, "voltage_v": 231.0}


def test_sensor_history_fields_default_to_all_mapped_fields(fake_connection):
    driver = MqttSensorDriver(
        name="s", broker=BROKER, state_topic="t", fields={"power_w": "p", "voltage_v": "v"},
    )
    assert set(driver.history_fields) == {"power_w", "voltage_v"}


# ---------------------------------------------------- multi-topic sensor ----
# A Shelly H&T (and presumably other devices) splits one logical reading's
# fields across separate per-component topics rather than one combined
# payload - state_topic accepts a list for exactly this.

def test_sensor_merges_fields_from_multiple_topics(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_connection.deliver("shelly/status/humidity:0", json.dumps({"rh": 47.0}))

    reading = driver.read()

    assert reading == {"ok": True, "name": "Bathroom Sensor", "temperature_c": 21.5, "humidity_pct": 47.0}


def test_sensor_merges_a_third_topic_with_a_nested_field_path(fake_connection):
    # The real-world shape: a Shelly H&T's DevicePower component nests
    # battery percentage under "battery.percent", on its own topic
    # alongside temperature/humidity - see drivers/mqtt/sensor.py's
    # module docstring and docs/integrations/mqtt.md.
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=[
            "shelly/status/temperature:0", "shelly/status/humidity:0", "shelly/status/devicepower:0",
        ],
        fields={"temperature_c": "tC", "humidity_pct": "rh", "battery_pct": "battery.percent"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_connection.deliver("shelly/status/humidity:0", json.dumps({"rh": 47.0}))
    fake_connection.deliver("shelly/status/devicepower:0", json.dumps({"battery": {"percent": 78}}))

    reading = driver.read()

    assert reading == {
        "ok": True, "name": "Bathroom Sensor", "temperature_c": 21.5, "humidity_pct": 47.0, "battery_pct": 78,
    }


def test_sensor_gives_a_partial_reading_when_only_some_topics_have_reported(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    # humidity topic never delivered anything yet

    reading = driver.read()

    assert reading == {"ok": True, "name": "Bathroom Sensor", "temperature_c": 21.5}


def test_sensor_raises_only_when_every_topic_has_nothing(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
    )
    with pytest.raises(RuntimeError, match="no MQTT message"):
        driver.read()


# A battery sensor (Shelly H&T) between wake cycles crosses
# stale_after_seconds constantly - that's not the same as genuinely
# offline hardware, so read() must keep surfacing the last known value
# (ok: False, but with the data) instead of discarding it the way
# raising here always used to.

def test_sensor_keeps_the_last_known_value_once_every_topic_goes_stale(fake_connection, fake_clock):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
        stale_after_seconds=300,
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_connection.deliver("shelly/status/humidity:0", json.dumps({"rh": 47.0}))
    fake_clock(301)

    reading = driver.read()

    assert reading["ok"] is False
    assert "no MQTT message in" in reading["error"]
    assert reading["temperature_c"] == 21.5
    assert reading["humidity_pct"] == 47.0
    assert reading["last_value_age_seconds"] == pytest.approx(301)


def test_sensor_falls_back_only_for_the_topic_that_ever_had_a_value(fake_connection, fake_clock):
    # Mixed case: one topic went stale (has a last-known value to fall
    # back to), the other never reported at all (nothing to fall back
    # to) - each topic's own history decides whether its field shows up
    # in the fallback reading, not an all-or-nothing choice.
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
        stale_after_seconds=300,
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_clock(301)
    # humidity topic never delivered anything, ever

    reading = driver.read()

    assert reading["ok"] is False
    assert reading["temperature_c"] == 21.5
    assert "humidity_pct" not in reading
    assert reading["last_value_age_seconds"] == pytest.approx(301)


# --------------------------------------------------------------- switch ----

def test_switch_reads_state_and_extra_fields(fake_connection):
    driver = MqttSwitchDriver(
        name="Living Room Lamp", broker=BROKER,
        state_topic="zigbee2mqtt/lamp", command_topic="zigbee2mqtt/lamp/set",
        fields={"power_w": "power"}, command_payload_field="state",
    )
    fake_connection.deliver("zigbee2mqtt/lamp", json.dumps({"state": "ON", "power": 8.5}))

    reading = driver.read()

    assert reading["is_on"] is True
    assert reading["power_w"] == 8.5


def test_switch_commands_use_the_json_payload_field_when_configured(fake_connection):
    driver = MqttSwitchDriver(
        name="Living Room Lamp", broker=BROKER,
        state_topic="zigbee2mqtt/lamp", command_topic="zigbee2mqtt/lamp/set",
        command_payload_field="state",
    )

    driver.turn_off()
    driver.turn_on()

    assert fake_connection.published == [
        ("zigbee2mqtt/lamp/set", json.dumps({"state": "OFF"})),
        ("zigbee2mqtt/lamp/set", json.dumps({"state": "ON"})),
    ]


def test_switch_commands_send_the_raw_payload_by_default(fake_connection):
    driver = MqttSwitchDriver(
        name="Tasmota Plug", broker=BROKER,
        state_topic="tele/plug/STATE", command_topic="cmnd/plug/POWER",
    )

    driver.turn_on()

    assert fake_connection.published == [("cmnd/plug/POWER", "ON")]


def test_switch_handles_a_plain_non_json_payload(fake_connection):
    # Regression test: Shelly Gen1-style relays publish a bare "on"/"off"
    # string, not JSON - state_field="" means "the whole payload, as-is".
    driver = MqttSwitchDriver(
        name="Gen1 Relay", broker=BROKER,
        state_topic="shellies/relay0/relay/0", command_topic="shellies/relay0/relay/0/command",
        state_field="", payload_on="on", payload_off="off", fields={},
    )
    fake_connection.deliver("shellies/relay0/relay/0", "on")

    reading = driver.read()

    assert reading["is_on"] is True


# --------------------------------------------------------------- number ----

def test_number_reads_the_current_value(fake_connection):
    driver = MqttNumberDriver(
        name="VMC Dimmer", broker=BROKER,
        state_topic="shellies/vmc-dimmer/status", command_topic="shellies/vmc-dimmer/set",
        value_field="brightness",
    )
    fake_connection.deliver("shellies/vmc-dimmer/status", json.dumps({"brightness": 55}))

    assert driver.read()["value"] == 55.0


def test_number_clamps_set_value_to_the_configured_range(fake_connection):
    driver = MqttNumberDriver(
        name="VMC Dimmer", broker=BROKER,
        state_topic="shellies/vmc-dimmer/status", command_topic="shellies/vmc-dimmer/set",
        value_field="brightness", command_payload_field="brightness",
        min_value=0, max_value=100,
    )

    driver.set_value(150)
    driver.set_value(-10)

    assert fake_connection.published == [
        ("shellies/vmc-dimmer/set", json.dumps({"brightness": 100.0})),
        ("shellies/vmc-dimmer/set", json.dumps({"brightness": 0.0})),
    ]


# ---------------------------------------------------------------- cover ----

def test_cover_reports_unknown_state_before_any_message(fake_connection):
    driver = MqttCoverDriver(
        name="MQTT Shutter", broker=BROKER,
        state_topic="shellies/shutter1/status", command_topic="shellies/shutter1/set",
    )
    assert driver.status()["state"] == "unknown"


def test_cover_maps_state_and_defaults_id_to_name(fake_connection):
    driver = MqttCoverDriver(
        name="MQTT Shutter", broker=BROKER,
        state_topic="shellies/shutter1/status", command_topic="shellies/shutter1/set",
    )
    fake_connection.deliver("shellies/shutter1/status", json.dumps({"state": "open"}))

    assert driver.status()["state"] == "open"
    assert driver.id == "MQTT Shutter"


def test_cover_send_publishes_the_configured_action_payload(fake_connection):
    driver = MqttCoverDriver(
        name="MQTT Shutter", broker=BROKER,
        state_topic="shellies/shutter1/status", command_topic="shellies/shutter1/set",
    )

    driver.send("stop")

    assert fake_connection.published == [("shellies/shutter1/set", "stop")]


# --------------------------------------------------- state persistence ----
# cache_path lets a driver survive a restart with its last known payload
# intact - see drivers/mqtt/state_cache.py for why this matters (a
# battery sensor's next report can be hours away, and the thermostat
# would otherwise leave a valve alone for that whole window on every
# restart rather than trusting a recent-enough last reading).

def test_sensor_seeds_its_reading_from_a_persisted_cache(fake_connection, tmp_path):
    cache_path = str(tmp_path / "mqtt_state_cache.json")
    first_run = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER, state_topic="shelly/status/temperature:0",
        fields={"temperature_c": "tC"}, cache_path=cache_path,
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    assert first_run.read()["temperature_c"] == 21.5  # sanity check, and what triggers the save

    # A brand new driver instance, standing in for the app after a
    # restart - never received a live message of its own.
    second_run = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER, state_topic="shelly/status/temperature:0",
        fields={"temperature_c": "tC"}, cache_path=cache_path,
    )

    assert second_run.read()["temperature_c"] == 21.5


def test_sensor_without_a_cache_path_does_not_touch_disk(fake_connection, tmp_path):
    # No cache_path at all (e.g. an inline `broker: {...}` entry with no
    # file to anchor next to) - must behave exactly as before, no crash.
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER, state_topic="shelly/status/temperature:0",
        fields={"temperature_c": "tC"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))

    assert driver.read()["temperature_c"] == 21.5
    assert list(tmp_path.iterdir()) == []


def test_a_shelly_relay_is_commanded_with_on_and_off_not_its_status_values(fake_connection):
    """Its status says `"output": true`, its command topic takes "on"/"off":
    "True"/"False" are ignored without a word - the valve never moved."""
    driver = MqttSwitchDriver(
        name="Rooms Valve", broker=BROKER,
        state_topic="shellyplus1-XXXX/status/switch:0", command_topic="shellyplus1-XXXX/command/switch:0",
        state_field="output", payload_on=True, payload_off=False, command_on="on", command_off="off",
    )
    driver.turn_on()
    driver.turn_off()
    assert fake_connection.published == [("shellyplus1-XXXX/command/switch:0", "on"),
                                         ("shellyplus1-XXXX/command/switch:0", "off")]
    fake_connection.deliver("shellyplus1-XXXX/status/switch:0", json.dumps({"output": True}))
    assert driver.read()["is_on"] is True


def test_switch_seeds_its_on_off_state_from_a_persisted_cache(fake_connection, tmp_path):
    cache_path = str(tmp_path / "mqtt_state_cache.json")
    first_run = MqttSwitchDriver(
        name="Bypass Valve", broker=BROKER,
        state_topic="shellyplus1-XXXX/status/switch:0", command_topic="shellyplus1-XXXX/command/switch:0",
        state_field="output", payload_on=True, payload_off=False, cache_path=cache_path,
    )
    fake_connection.deliver("shellyplus1-XXXX/status/switch:0", json.dumps({"output": True}))
    assert first_run.read()["is_on"] is True

    second_run = MqttSwitchDriver(
        name="Bypass Valve", broker=BROKER,
        state_topic="shellyplus1-XXXX/status/switch:0", command_topic="shellyplus1-XXXX/command/switch:0",
        state_field="output", payload_on=True, payload_off=False, cache_path=cache_path,
    )

    assert second_run.read()["is_on"] is True


def test_a_shelly_valve_built_by_the_panel_has_its_on_off_recorded(fake_connection, tmp_path):
    """Found cooling for a while on antonio with the zone's hours on at 0:
    the valve driver recorded only its extra fields, and a Shelly valve has
    none - its on/off never reached the history. The simulated switch
    records it, so no test saw it: this one builds the valve as the panel
    does and goes through the recorder."""
    from localhome.core import registry
    from localhome.services.climate_map_store import valve_integration_options
    from localhome.services.history import HistoryRecorder, HistoryStore

    broker_file = tmp_path / "mqtt_broker.json"
    broker_file.write_text(json.dumps(BROKER), encoding="utf-8")
    options = valve_integration_options({"valve_device_id": "shellyplus1-XXXX"}, "Stanze Valve", str(broker_file))
    [driver] = registry.create("switch", "mqtt_json", options)
    assert "is_on" in driver.history_fields
    fake_connection.deliver("shellyplus1-XXXX/status/switch:0", json.dumps({"output": True}))

    class Poller:
        def __init__(self):
            self.driver = driver

        def get(self):
            return driver.read()

    class Manager:
        pollers = {"Stanze Valve": Poller()}

    store = HistoryStore(str(tmp_path / "history.db"))
    HistoryRecorder(Manager(), store).poll_once()
    assert [point["value"] for point in store.query("Stanze Valve", "is_on", "24h")] == [1]


def test_every_role_built_for_real_shellys_records_what_the_pages_read(tmp_path):
    """The same gap anywhere else: the house's real configuration - each
    zone's H&T and valve, the outdoor H&T, bypass, dehumidifier, the VMC's
    dimmer - built as the panel builds it, each driver's history against
    what's read from it: a room's and the outdoor temperature (the day's
    strip, lows and highs, degree days), a valve's on/off (hours on), the
    VMC's speed. Tests run on simulated devices: this is the real ones'."""
    from localhome.core import registry
    from localhome.services import climate_map_store as panel

    broker = tmp_path / "mqtt_broker.json"
    broker.write_text(json.dumps(BROKER), encoding="utf-8")
    zones = [{"name": name, "sensor_mode": "shelly", "sensor_device_id": f"shellyhtg3-{name}", "valve_mode": "shelly",
              "valve_device_id": f"shelly1g3-{name}", "cool_enabled": name == "Stanze"} for name in ("Stanze", "Bagni")]
    integrations = [
        *panel.build_climate_integrations(zones, str(broker), str(tmp_path / "manual.json")),
        *panel.build_extra_sensor_integrations({"outdoor_sensor_device_id": "shellyhtg3-out"}, str(broker), "shelly"),
        *panel.build_extra_switch_integrations({"dehumidifier_device_id": "shelly1g3-d",
                                                "bypass_valve_device_id": "shelly1g3-bp"}, str(broker)),
        *panel.build_extra_dimmer_integrations({"vmc_device_id": "shelly0110dimg3-x"}, str(broker), "shelly"),
    ]
    recorded = {driver.name: set(driver.history_fields)
                for i in integrations for driver in registry.create(i.kind, i.type, i.options)}
    wanted = {"Stanze Sensor": "temperature_c", "Bagni Sensor": "temperature_c", "Outdoor Sensor": "temperature_c",
              "Stanze Valve": "is_on", "Bagni Valve": "is_on", "Bathroom Bypass Valve": "is_on",
              "Dehumidifier": "is_on", "VMC Speed": "value"}
    assert {name: field for name, field in wanted.items() if field not in recorded.get(name, set())} == {}


# ------------------------------------------------------------ availability ----
# availability_topic (see drivers/mqtt/base.py's Availability) overrides
# even a still-fresh regular reading - a definitive "the broker says this
# device disconnected" signal beats an inferred one.

def test_sensor_reports_offline_immediately_when_availability_says_so(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER, state_topic="shelly/status/temperature:0",
        fields={"temperature_c": "tC"}, availability_topic="shelly/online",
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_connection.deliver("shelly/online", "false")

    reading = driver.read()

    # Still surfaces the last known value (same fallback as a
    # stale_after_seconds timeout) - just correctly not ok.
    assert reading["ok"] is False
    assert reading["temperature_c"] == 21.5
    assert "offline" in reading["error"]


def test_sensor_is_unaffected_by_availability_when_it_reports_online(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER, state_topic="shelly/status/temperature:0",
        fields={"temperature_c": "tC"}, availability_topic="shelly/online",
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_connection.deliver("shelly/online", "true")

    reading = driver.read()

    assert reading == {"ok": True, "name": "Bathroom Sensor", "temperature_c": 21.5}


def test_switch_raises_immediately_when_availability_says_offline(fake_connection):
    # A relay that only publishes on state change has no natural
    # stale_after_seconds to set - availability_topic may be the *only*
    # way this ever notices the device went offline.
    driver = MqttSwitchDriver(
        name="Bypass Valve", broker=BROKER,
        state_topic="shellyplus1-XXXX/status/switch:0", command_topic="shellyplus1-XXXX/command/switch:0",
        state_field="output", payload_on=True, payload_off=False,
        availability_topic="shellyplus1-XXXX/online",
    )
    fake_connection.deliver("shellyplus1-XXXX/status/switch:0", json.dumps({"output": True}))
    fake_connection.deliver("shellyplus1-XXXX/online", "false")

    with pytest.raises(RuntimeError, match="offline"):
        driver.read()


def test_an_unplugged_relay_reads_offline_as_the_broker_says_it(fake_connection):
    """Measured on the production board: the dehumidifier's reading went
    offline 15-17 s after its online "false" - the 20 s poll. Its poller
    reads it again on the message itself, once per change."""
    from localhome.core.manager import DeviceManager
    from localhome.core.poller import SyncPoller
    driver = MqttSwitchDriver(
        name="Dehumidifier", broker=BROKER,
        state_topic="shelly1g3-dehum/status/switch:0", command_topic="shelly1g3-dehum/command/switch:0",
        state_field="output", payload_on=True, payload_off=False,
        availability_topic="shelly1g3-dehum/online",
    )
    manager = DeviceManager.__new__(DeviceManager)
    manager.covers, manager.switches, manager.numbers, manager.pollers, manager.sensor_kinds = {}, {}, {}, {}, {}
    manager._register("switch", driver)
    poller = manager.pollers["Dehumidifier"]
    assert isinstance(poller, SyncPoller) and driver.wake_poller == poller.wake
    reads = []
    driver.wake_poller = lambda: (reads.append(1), poller.poll_once())   # the thread's next read, now

    fake_connection.deliver("shelly1g3-dehum/status/switch:0", json.dumps({"output": False}))
    fake_connection.deliver("shelly1g3-dehum/online", "true")
    assert poller.get()["ok"] is True
    fake_connection.deliver("shelly1g3-dehum/online", "false")
    assert poller.get()["ok"] is False, "no poll in between"
    fake_connection.deliver("shelly1g3-dehum/online", "false")
    assert len(reads) == 2, "said again: nothing new to read"


def test_number_raises_immediately_when_availability_says_offline(fake_connection):
    driver = MqttNumberDriver(
        name="VMC Dimmer", broker=BROKER,
        state_topic="shellies/vmc-dimmer/status", command_topic="shellies/vmc-dimmer/set",
        value_field="brightness", availability_topic="shellies/vmc-dimmer/online",
    )
    fake_connection.deliver("shellies/vmc-dimmer/status", json.dumps({"brightness": 42}))
    fake_connection.deliver("shellies/vmc-dimmer/online", "false")

    with pytest.raises(RuntimeError, match="offline"):
        driver.read()


def test_availability_topic_left_unset_behaves_exactly_as_before(fake_connection):
    # No availability_topic configured (the default) - existing
    # config.yaml entries must keep working unchanged.
    driver = MqttSwitchDriver(
        name="Bypass Valve", broker=BROKER,
        state_topic="shellyplus1-XXXX/status/switch:0", command_topic="shellyplus1-XXXX/command/switch:0",
        state_field="output", payload_on=True, payload_off=False,
    )
    fake_connection.deliver("shellyplus1-XXXX/status/switch:0", json.dumps({"output": True}))

    assert driver.read() == {"ok": True, "name": "Bypass Valve", "is_on": True}


# ------------------------------------------------------------ last_update_ts --
# core/poller.py's SyncPoller reports freshness from this instead of
# "whenever poll_once() last ran", when a driver defines it - see
# test_poller.py for why re-polling a push-based MQTT cache on a timer
# otherwise makes a stale reading look freshly updated every cycle.

def test_sensor_last_update_ts_is_the_mqtt_messages_own_arrival_time(fake_connection, fake_clock):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER, state_topic="shelly/status/temperature:0",
        fields={"temperature_c": "tC"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    message_ts = driver.last_update_ts
    fake_clock(300)  # time passes with no new message - the poller might still poll_once() every cycle

    assert driver.last_update_ts == message_ts  # unchanged - no new message arrived


def test_sensor_last_update_ts_is_none_before_any_message(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER, state_topic="shelly/status/temperature:0",
        fields={"temperature_c": "tC"},
    )
    assert driver.last_update_ts is None


def test_sensor_last_update_ts_is_the_newest_of_several_topics(fake_connection, fake_clock):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_clock(60)
    fake_connection.deliver("shelly/status/humidity:0", json.dumps({"rh": 47.0}))  # arrives 60s later

    assert driver.last_update_ts == pytest.approx(driver._states[1].get_received_at())


def test_switch_last_update_ts_is_the_mqtt_messages_own_arrival_time(fake_connection, fake_clock):
    driver = MqttSwitchDriver(
        name="Bypass Valve", broker=BROKER,
        state_topic="shellyplus1-XXXX/status/switch:0", command_topic="shellyplus1-XXXX/command/switch:0",
        state_field="output", payload_on=True, payload_off=False,
    )
    assert driver.last_update_ts is None
    fake_connection.deliver("shellyplus1-XXXX/status/switch:0", json.dumps({"output": True}))
    message_ts = driver.last_update_ts
    fake_clock(300)

    assert driver.last_update_ts == message_ts


def test_number_last_update_ts_is_the_mqtt_messages_own_arrival_time(fake_connection, fake_clock):
    driver = MqttNumberDriver(
        name="VMC Dimmer", broker=BROKER,
        state_topic="shellies/vmc-dimmer/status", command_topic="shellies/vmc-dimmer/set",
        value_field="brightness",
    )
    assert driver.last_update_ts is None
    fake_connection.deliver("shellies/vmc-dimmer/status", json.dumps({"brightness": 42}))
    message_ts = driver.last_update_ts
    fake_clock(300)

    assert driver.last_update_ts == message_ts



# ------------------------------------------------------------------ hold ----
# A Shelly relay out of its rest state is held for hold_seconds and the
# hold renewed while LocalHome runs - measured on a real Plus 1: each
# "on,N" restarts the timer and the output never drops (docs/shelly-gen3.md).

@pytest.fixture
def monotonic(monkeypatch):
    now = {"t": 5000.0}
    monkeypatch.setattr("localhome.drivers.mqtt.switch.time.monotonic", lambda: now["t"])
    return now


def _valve(**options):
    return MqttSwitchDriver(
        name="Rooms Valve", broker=BROKER,
        state_topic="shellyplus1-XXXX/status/switch:0", command_topic="shellyplus1-XXXX/command/switch:0",
        state_field="output", payload_on=True, payload_off=False, command_on="on", command_off="off", **options)


def _status(fake_connection, on):
    fake_connection.deliver("shellyplus1-XXXX/status/switch:0", json.dumps({"output": on}))


def test_a_held_relay_is_switched_on_for_a_while_and_off_for_good(fake_connection, monotonic):
    valve = _valve(hold_seconds=900)
    valve.turn_on()
    valve.turn_off()
    assert fake_connection.published == [("shellyplus1-XXXX/command/switch:0", "on,900"),
                                         ("shellyplus1-XXXX/command/switch:0", "off")]


def test_while_on_the_hold_is_renewed_every_five_minutes(fake_connection, monotonic):
    """Only as often as the hold needs: two renewals can go missing and
    the relay still doesn't drop - no more traffic than that, for years."""
    valve = _valve(hold_seconds=900)
    valve.turn_on()
    _status(fake_connection, True)
    for _ in range(45):                 # readings every 20 s for 15 minutes
        monotonic["t"] += 20
        assert valve.read()["is_on"] is True
    sent = [p for _, p in fake_connection.published]
    assert sent == ["on,900"] * 4, "the command, then a renewal every 5 minutes"


def test_a_relay_found_on_is_held_at_once_after_a_restart(fake_connection, monotonic):
    """LocalHome restarted: the relay is still on, its timer running -
    renewed at the first reading, before it runs out."""
    valve = _valve(hold_seconds=900)
    _status(fake_connection, True)      # its first status since the restart
    valve.read()
    assert fake_connection.published == [("shellyplus1-XXXX/command/switch:0", "on,900")]


def test_at_rest_nothing_is_sent(fake_connection, monotonic):
    valve = _valve(hold_seconds=900)
    _status(fake_connection, False)
    for _ in range(10):
        monotonic["t"] += 60
        valve.read()
    assert fake_connection.published == []


def test_a_relay_resting_on_is_held_off(fake_connection, monotonic):
    """Held off as RPC Switch.Set on its prefix: a Shelly 1 Gen3 (fw 2.0.1)
    ignores "off,N" on its command topic - measured - and both
    generations take the RPC."""
    valve = _valve(hold_seconds=600, rest_state="on")
    valve.turn_off()
    _status(fake_connection, False)
    monotonic["t"] += 200
    valve.read()                                   # off: held off, renewed
    valve.turn_on()
    valve.read()                                   # still says off: not renewed
    topics = [t for t, _ in fake_connection.published]
    assert topics == ["shellyplus1-XXXX/rpc", "shellyplus1-XXXX/rpc", "shellyplus1-XXXX/command/switch:0"]
    held = json.loads(fake_connection.published[0][1])
    assert held["method"] == "Switch.Set" and held["params"] == {"id": 0, "on": False, "toggle_after": 600}
    assert fake_connection.published[2][1] == "on"


def test_a_renewal_that_fails_doesnt_hide_the_reading(fake_connection, monotonic, caplog):
    valve = _valve(hold_seconds=900)
    _status(fake_connection, True)

    def broken(topic, payload):
        raise ConnectionError("broker down")
    valve._state.publish = broken
    for _ in range(3):
        monotonic["t"] += 300
        assert valve.read()["is_on"] is True
    assert sum("couldn't renew" in r.getMessage() for r in caplog.records) == 1, "said once"


def test_without_a_hold_the_commands_are_plain(fake_connection, monotonic):
    valve = _valve()
    valve.turn_on()
    _status(fake_connection, True)
    monotonic["t"] += 120
    valve.read()
    assert [p for _, p in fake_connection.published] == ["on"]



def test_switched_off_it_isnt_turned_back_on_by_a_reading_from_before(fake_connection, monotonic):
    """Seen on the production board in diagnostic mode: "Close", then the
    valve open again. The device is re-read right after the command, before
    the relay has said it's off: that reading still says on, and renewing
    it sent "on,900" straight back."""
    valve = _valve(hold_seconds=900)
    valve.turn_on()
    _status(fake_connection, True)
    valve.read()
    valve.turn_off()
    valve.read()                                   # the relay hasn't confirmed yet
    monotonic["t"] += 600
    valve.read()
    assert [p for _, p in fake_connection.published] == ["on,900", "off"]
    _status(fake_connection, False)                # now it has
    assert valve.read()["is_on"] is False
    assert [p for _, p in fake_connection.published] == ["on,900", "off"]


def test_switched_on_again_after_off_it_is_held_again(fake_connection, monotonic):
    valve = _valve(hold_seconds=900)
    valve.turn_on()
    valve.turn_off()
    _status(fake_connection, False)
    valve.turn_on()
    _status(fake_connection, True)
    monotonic["t"] += 300
    valve.read()
    assert [p for _, p in fake_connection.published] == ["on,900", "off", "on,900", "on,900"]



def test_just_commanded_it_reads_as_commanded_until_the_device_says_otherwise(fake_connection, monkeypatch):
    """Seen in diagnostic mode: the relay clicked at once, the page said so
    only at the next poll - the re-read right after the command had found
    the state from before it. Until the device speaks, or for
    CONFIRM_SECONDS if it doesn't, a reading says what was commanded."""
    import localhome.drivers.mqtt.switch as switch_module
    now = {"t": 2_000_000_000.0}
    monkeypatch.setattr(switch_module.time, "time", lambda: now["t"])
    monkeypatch.setattr("localhome.drivers.mqtt.base.time.time", lambda: now["t"])
    valve = _valve(hold_seconds=900)
    _status(fake_connection, False)
    now["t"] += 1
    valve.turn_on()
    assert valve.read()["is_on"] is True, "commanded, not confirmed yet"
    now["t"] += 1
    _status(fake_connection, False)                # the device says it didn't
    assert valve.read()["is_on"] is False
    valve.turn_on()
    now["t"] += switch_module.CONFIRM_SECONDS + 1  # and never answers
    assert valve.read()["is_on"] is False, "past the wait, what the device last said"


# ------------------------------------------------- asked, and its timer ----
# A Shelly relay is asked for its state (status_command) when what's known
# of it may be from before: at start, back online, or held and silent for
# longer than its hold. And a held relay put back at rest by its own timer
# is noted - on the message that says so, at the time it happened.

TOPIC = "shellyplus1-XXXX/status/switch:0"


@pytest.fixture
def clock(monkeypatch):
    """One wall clock for the driver and the messages' arrival times; the
    wait for an answer that doesn't come, none."""
    import localhome.drivers.mqtt.switch as switch_module
    now = {"t": 2_000_000_000.0}
    monkeypatch.setattr(switch_module.time, "time", lambda: now["t"])
    monkeypatch.setattr("localhome.drivers.mqtt.base.time.time", lambda: now["t"])
    monkeypatch.setattr(switch_module, "ANSWER_WAIT_SECONDS", 0)
    return now


def _say(fake_connection, on, source="mqtt", timer=None, started=1_791_000_000.0):
    """By default its hold started at a time far from the tests' clock - a
    Shelly whose own clock isn't set."""
    payload = {"id": 0, "source": source, "output": on}
    if timer:
        payload.update(timer_started_at=started, timer_duration=float(timer))
    fake_connection.deliver(TOPIC, json.dumps(payload))


def _answers(fake_connection, on, source="mqtt", timer=None):
    """The Shelly answers "status_update" at once, as a Plus 1 does."""
    publish = fake_connection.publish

    def answering(topic, payload, qos=0, retain=False):
        publish(topic, payload, qos, retain)
        if payload == "status_update":
            _say(fake_connection, on, source, timer)
    fake_connection.publish = answering


def _sent(fake_connection):
    return [payload for _, payload in fake_connection.published]


def _shelly_valve(tmp_path, **options):
    return _valve(hold_seconds=900, status_command="status_update", event_log=str(tmp_path / "watchdog.log"),
                  cache_path=str(tmp_path / "mqtt_state_cache.json"), **options)


def _logged(tmp_path):
    path = tmp_path / "watchdog.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_back_at_rest_by_its_own_timer_is_noted_at_the_time_it_happened(fake_connection, clock, tmp_path):
    """Its hold ran out - nobody renewed it - and the Shelly says
    "source": "timer". The time is the last hold heard plus its length, by
    LocalHome's clock; kept after LocalHome commands it again, and after a
    restart of LocalHome - in the event log, where the watchdogs write."""
    valve = _shelly_valve(tmp_path)
    t0 = clock["t"]
    _say(fake_connection, True, timer=900)
    assert "safety_rest_at" not in valve.read()
    clock["t"] += 1000                               # heard only now - LocalHome was away
    _say(fake_connection, False, source="timer")
    assert valve.read()["safety_rest_at"] == t0 + 900
    _say(fake_connection, True, timer=900)           # on again
    assert valve.read()["safety_rest_at"] == t0 + 900, "it happened, whatever came next"
    assert len(_logged(tmp_path)) == 1 and _logged(tmp_path)[0].endswith(" safety_timer Rooms Valve")
    again = _shelly_valve(tmp_path)                  # LocalHome restarted
    assert again._safety_rest_at == t0 + 900


def test_a_relay_that_ran_out_while_localhome_was_stopped_is_found_out_at_start(fake_connection, clock, tmp_path):
    """LocalHome stopped for an hour, its broker on: the relay went back to
    rest by its timer and LocalHome missed it. At start, what's on disk
    says on - asked first, it isn't held from that, and what happened is
    noted, at the time it did."""
    before = _shelly_valve(tmp_path)
    t0 = clock["t"]
    _say(fake_connection, True, timer=900)
    before.read()
    fake_connection.subscribers.clear()
    fake_connection.published.clear()
    clock["t"] += 3600
    _answers(fake_connection, False, source="timer")
    valve = _shelly_valve(tmp_path)
    reading = valve.read()
    assert _sent(fake_connection) == ["status_update"], "asked, and not turned back on from the disk"
    assert reading["is_on"] is False and reading["safety_rest_at"] == t0 + 900


def test_a_relay_still_on_after_a_restart_is_asked_then_held(fake_connection, clock, tmp_path):
    before = _shelly_valve(tmp_path)
    _say(fake_connection, True, timer=900)
    before.read()
    fake_connection.subscribers.clear()
    fake_connection.published.clear()
    clock["t"] += 120
    _answers(fake_connection, True, timer=900)
    valve = _shelly_valve(tmp_path)
    assert valve.read()["is_on"] is True
    assert _sent(fake_connection) == ["status_update", "on,900"]
    assert "safety_rest_at" not in valve.read() and _logged(tmp_path) == []


def test_the_same_rest_heard_again_isnt_noted_twice(fake_connection, clock, tmp_path):
    valve = _shelly_valve(tmp_path)
    _say(fake_connection, True, timer=900)
    clock["t"] += 900
    _say(fake_connection, False, source="timer")
    clock["t"] += 300
    _say(fake_connection, False, source="timer")     # its answer to a status_update
    assert len(_logged(tmp_path)) == 1


def test_only_a_held_relay_has_a_safety_timer(fake_connection, clock, tmp_path):
    bypass = _valve(status_command="status_update", event_log=str(tmp_path / "watchdog.log"))
    _say(fake_connection, True)
    _say(fake_connection, False, source="timer")
    assert "safety_rest_at" not in bypass.read() and _logged(tmp_path) == []


def test_unanswered_it_is_asked_less_and_less_often_and_at_once_when_back(fake_connection, clock, monotonic, tmp_path):
    """Right after the board itself started, the Shelly may not be
    connected yet: asked again after 1, 2, 4 minutes, then every 5 - and
    at once when it says it's online."""
    valve = _shelly_valve(tmp_path, availability_topic="shellyplus1-XXXX/online")
    asked_at = []
    for second in range(0, 900, 20):
        monotonic["t"] = 5000.0 + second
        before = len(fake_connection.published)
        with pytest.raises(RuntimeError, match="no MQTT message"):
            valve.read()
        if len(fake_connection.published) > before:
            asked_at.append(second)
    assert asked_at == [0, 60, 180, 420, 720]
    clock["t"] += 1
    fake_connection.deliver("shellyplus1-XXXX/online", "true")
    _answers(fake_connection, False, source="init")
    monotonic["t"] += 20
    assert valve.read()["is_on"] is False
    assert _sent(fake_connection) == ["status_update"] * 6
    for _ in range(10):                              # answered: not asked again
        monotonic["t"] += 300
        valve.read()
    assert _sent(fake_connection) == ["status_update"] * 6


def test_back_online_it_is_asked_before_its_hold_is_renewed(fake_connection, clock, monotonic, tmp_path):
    """Its Wi-Fi dropped for longer than the hold: back online, it's asked
    what it is before anything is renewed - it's off by its timer, so
    that's noted, and nothing turns it back on from what it said before."""
    valve = _shelly_valve(tmp_path, availability_topic="shellyplus1-XXXX/online")
    fake_connection.deliver("shellyplus1-XXXX/online", "true")
    valve.turn_on()
    t0 = clock["t"]
    _say(fake_connection, True, timer=900)
    valve.read()
    fake_connection.deliver("shellyplus1-XXXX/online", "false")
    with pytest.raises(RuntimeError, match="offline"):
        valve.read()
    clock["t"] += 1500
    monotonic["t"] += 1500
    fake_connection.deliver("shellyplus1-XXXX/online", "true")
    _answers(fake_connection, False, source="timer")
    fake_connection.published.clear()
    reading = valve.read()
    assert _sent(fake_connection) == ["status_update"]
    assert reading["is_on"] is False and reading["safety_rest_at"] == t0 + 900


def test_held_and_silent_for_longer_than_its_hold_it_is_asked(fake_connection, clock, monotonic, tmp_path):
    """LocalHome's own connection to the broker gone for a while: what it
    knows says on, but nothing has come for longer than the hold - asked
    first, then renewed only if it's still on."""
    valve = _shelly_valve(tmp_path)
    valve.turn_on()
    _say(fake_connection, True, timer=900)
    valve.read()
    fake_connection.published.clear()
    clock["t"] += 1000
    monotonic["t"] += 1000
    _answers(fake_connection, True, timer=900)       # someone else held it
    valve.read()
    assert _sent(fake_connection) == ["status_update", "on,900"]


def test_a_command_before_it_has_been_heard_asks_first(fake_connection, clock, tmp_path):
    """Seen on the production board: LocalHome back after the board had
    stopped, the thermostat sent "on,900" blind - the valve had no reading
    yet - before the driver asked. Had the Shelly been connected, that
    would have wiped out its timer's "source": "timer". Asked first, in
    the same breath: its answer comes before the command takes effect."""
    before = _shelly_valve(tmp_path)
    t0 = clock["t"]
    _say(fake_connection, True, timer=900)
    before.read()
    fake_connection.subscribers.clear()
    fake_connection.published.clear()
    clock["t"] += 3600
    _answers(fake_connection, False, source="timer")
    valve = _shelly_valve(tmp_path)
    valve.turn_on()
    assert _sent(fake_connection) == ["status_update", "on,900"]
    assert valve.read()["safety_rest_at"] == t0 + 900
    _say(fake_connection, True, timer=900)
    valve.turn_off()
    assert _sent(fake_connection)[-1] == "off", "heard since: just the command"
    assert _sent(fake_connection).count("status_update") == 1


def test_back_online_too_quickly_to_be_seen_offline_it_is_asked(fake_connection, clock, monotonic, tmp_path):
    """Seen on the production board: 5 s without power. The broker said
    "offline" and "online" 50 ms apart - no reading saw it offline - and
    the Shelly came back at rest ("source": "init"), LocalHome sure it
    was on until the next renewal. Online since its last state: asked."""
    valve = _shelly_valve(tmp_path, availability_topic="shellyplus1-XXXX/online")
    fake_connection.deliver("shellyplus1-XXXX/online", "true")
    valve.turn_on()
    _say(fake_connection, True, timer=900)
    valve.read()
    fake_connection.published.clear()
    clock["t"] += 60
    monotonic["t"] += 60
    valve.read()
    assert _sent(fake_connection) == [], "nothing new: nothing asked"
    clock["t"] += 5
    fake_connection.deliver("shellyplus1-XXXX/online", "false")
    fake_connection.deliver("shellyplus1-XXXX/online", "true")
    _answers(fake_connection, False, source="init")
    clock["t"] += 15
    monotonic["t"] += 15
    reading = valve.read()
    assert _sent(fake_connection) == ["status_update"]
    assert reading["is_on"] is False and "safety_rest_at" not in reading, "a restart, not its timer"
    monotonic["t"] += 20
    valve.read()
    assert _sent(fake_connection) == ["status_update"], "answered: not asked again"


def test_the_time_is_the_devices_own_start_of_the_hold_when_its_clock_agrees(fake_connection, clock, tmp_path):
    """Seen on the production board: the last message heard was an answer
    20 s into the hold, not the renewal that started it - "heard + hold"
    noted it 20 s late. The Shelly's own start of the hold, when its clock
    agrees with the board's, is right."""
    valve = _shelly_valve(tmp_path)
    t0 = clock["t"]
    _say(fake_connection, True, timer=900, started=t0 - 20)
    clock["t"] += 2000
    _say(fake_connection, False, source="timer")
    assert valve.read()["safety_rest_at"] == t0 - 20 + 900


def test_a_renewal_that_couldnt_reach_it_is_sent_again_as_soon_as_it_answers(fake_connection, clock, monotonic, tmp_path):
    """Seen on the production board after a power cut: LocalHome started
    before the Shelly was back on the broker - asked, no answer, renewed
    from what it knew, and that renewal reached nobody. The Shelly back,
    asked and answering, its hold is renewed at once, not 5 minutes after
    the one that was lost - which could be after the hold ran out."""
    before = _shelly_valve(tmp_path)
    _say(fake_connection, True, timer=900)
    before.read()
    fake_connection.subscribers.clear()
    fake_connection.published.clear()
    clock["t"] += 600
    valve = _shelly_valve(tmp_path, availability_topic="shellyplus1-XXXX/online")
    assert valve.read()["is_on"] is True
    assert _sent(fake_connection) == ["status_update", "on,900"], "asked, then held from what it knew"
    clock["t"] += 70
    monotonic["t"] += 70
    fake_connection.deliver("shellyplus1-XXXX/online", "true")
    _answers(fake_connection, True, timer=900)
    clock["t"] += 20
    monotonic["t"] += 20
    valve.read()
    assert _sent(fake_connection) == ["status_update", "on,900", "status_update", "on,900"]
    monotonic["t"] += 20
    valve.read()
    assert len(_sent(fake_connection)) == 4, "then at the usual pace"


def test_a_command_isnt_shown_as_obeyed_when_the_one_before_went_unanswered(fake_connection, clock, tmp_path):
    """Seen on the production board, a Shelly 1 Gen3 just unplugged: the
    broker didn't know yet, the thermostat sent "on,900" again every tick,
    and each was shown as obeyed for 15 s - the page flipping between open
    and closed. One unanswered command is shown; the next isn't, until the
    device speaks again."""
    import localhome.drivers.mqtt.switch as switch_module
    valve = _shelly_valve(tmp_path)
    _say(fake_connection, False)
    clock["t"] += 1
    valve.turn_on()
    assert valve.read()["is_on"] is True, "just commanded: shown as commanded"
    clock["t"] += switch_module.CONFIRM_SECONDS + 5
    assert valve.read()["is_on"] is False, "no answer: what it last said"
    valve.turn_on()                                  # sent again - nobody answers
    assert valve.read()["is_on"] is False, "two unanswered: likely gone, not shown as obeyed"
    clock["t"] += 1
    _say(fake_connection, True, timer=900)           # it's back and answers
    valve.turn_off()
    assert valve.read()["is_on"] is False


def test_a_status_queued_by_the_device_doesnt_move_the_time_off(fake_connection, clock, tmp_path):
    """Seen on the production board with a Shelly 1 Gen3: the board
    stopped, the hold ran out; when the board was back the Gen3 first sent
    a status queued from before - on, its hold started 16 minutes earlier -
    then "off, by its timer". Noted at the end of that hold, not when heard."""
    valve = _shelly_valve(tmp_path)
    t0 = clock["t"]
    _say(fake_connection, True, timer=900, started=t0)
    clock["t"] += 1000
    _say(fake_connection, True, timer=900, started=t0)        # the queued one, heard only now
    _say(fake_connection, False, source="timer")
    assert valve.read()["safety_rest_at"] == t0 + 900


# ----------------------------------------------------------- an H&T Gen3 ----
# The two real sensors on the production board, one on its batteries and
# one on USB-C: every wake publishes every component (NotifyFullStatus),
# then "sleep" - on USB-C too, with its batteries still in. Their payloads
# as captured, and the driver as the climate admin panel builds it.

def _ht(**extra):
    options = dict(
        name="Bagni Sensor", broker=BROKER,
        state_topic=["ht/status/temperature:0", "ht/status/humidity:0", "ht/status/devicepower:0", "ht/status/wifi"],
        fields={"temperature_c": "tC", "humidity_pct": "rh", "battery_pct": "battery.percent",
                "wifi_rssi_dbm": "rssi", "external_power": "external.present"},
        valid_ranges={"temperature_c": [-30, 70], "humidity_pct": [1, 100], "battery_pct": [0, 100]},
        required_fields=["temperature_c"], stale_after_seconds=14400)
    options.update(extra)
    return MqttSensorDriver(**options)


def _wake(conn, temperature=25.7, humidity=54.9, external=True, percent=87):
    conn.deliver("ht/status/devicepower:0", json.dumps(
        {"id": 0, "battery": {"V": 5.76, "percent": percent}, "external": {"present": external}}))
    conn.deliver("ht/status/humidity:0", json.dumps({"id": 0, "rh": humidity}))
    conn.deliver("ht/status/temperature:0", json.dumps({"id": 0, "tC": temperature, "tF": 78.3}))
    conn.deliver("ht/status/wifi", json.dumps({"sta_ip": "192.168.1.19", "status": "got ip", "rssi": -72}))


def test_an_h_and_t_reads_the_same_on_usb_c_and_on_its_batteries(fake_connection):
    driver = _ht()
    _wake(fake_connection, external=True)
    assert driver.read() == {"ok": True, "name": "Bagni Sensor", "temperature_c": 25.7, "humidity_pct": 54.9,
                             "battery_pct": 87, "wifi_rssi_dbm": -72, "external_power": True}
    _wake(fake_connection, external=False, percent=75)
    reading = driver.read()
    assert reading["ok"] and reading["external_power"] is False and reading["battery_pct"] == 75


def test_a_temperature_that_cant_be_one_isnt_a_reading(fake_connection):
    """A failing element sends -127 as readily as a real value: left out,
    and without a temperature the sensor isn't "ok" - the thermostat
    treats it as unreachable, its own safe path, instead of acting on it."""
    driver = _ht()
    _wake(fake_connection, temperature=-127.0)
    reading = driver.read()
    assert reading["ok"] is False and "temperature_c" not in reading
    assert reading["rejected"] == ["temperature_c -127.0 out of -30..70"]
    assert "out of -30..70" in reading["error"]
    _wake(fake_connection, temperature=25.5)
    reading = driver.read()
    assert reading["ok"] is True and "rejected" not in reading


def test_a_humidity_that_cant_be_one_is_left_out_and_the_rest_stands(fake_connection):
    driver = _ht()
    _wake(fake_connection, humidity=0)
    reading = driver.read()
    assert reading["ok"] is True and reading["temperature_c"] == 25.7 and "humidity_pct" not in reading
    assert reading["rejected"] == ["humidity_pct 0 out of 1..100"]


def test_a_value_that_isnt_a_number_is_left_out(fake_connection):
    driver = _ht()
    _wake(fake_connection, temperature="25.7")
    assert driver.read()["ok"] is False


def test_a_temperature_gone_quiet_while_the_rest_reports_is_not_trusted(fake_connection, fake_clock):
    """Every wake republishes every component: a temperature that stops
    while the rest goes on is a failing element, not a quiet room."""
    driver = _ht()
    _wake(fake_connection)
    fake_clock(14401)
    fake_connection.deliver("ht/status/humidity:0", json.dumps({"id": 0, "rh": 55.0}))
    reading = driver.read()
    assert reading["ok"] is False and "temperature_c" not in reading and reading["humidity_pct"] == 55.0


def test_the_battery_sensor_between_wakes_keeps_its_last_value(fake_connection, fake_clock):
    driver = _ht()
    _wake(fake_connection, external=False)
    fake_clock(14401)                                    # past its 4 h: every topic stale
    reading = driver.read()
    assert reading["ok"] is False and reading["temperature_c"] == 25.7
    assert reading["last_value_age_seconds"] == pytest.approx(14401)
