"""What every Shelly LocalHome uses says about itself: its Wi-Fi signal,
its own temperature, when it last started, a firmware update waiting -
and for a battery sensor, whether its reports come on time.

Two ways in, one per kind of device:

- A battery sensor (a Shelly H&T Gen3) sleeps, and can't be asked
  anything: what it publishes when it wakes - `status/wifi`,
  `status/devicepower:0`, `status/sys` - is listened to. Each wake is a
  report; a missing one, when its 2-hour heartbeat was due, is a report
  that couldn't get through (see missed_reports()). Its signal is
  already in the history, recorded with its temperature (`wifi_rssi_dbm`
  under the sensor's name) - that's what its day and week are read from.
- A mains Shelly (a relay, the VMC's dimmer) is always connected, but
  doesn't publish its signal or temperature by itself: it's asked, every
  5 minutes, with one Shelly.GetStatus over MQTT. A relay LocalHome
  commands is also asked for its settings (Shelly.GetConfig) - only when
  they can have changed: at its first answer, after it restarts (an
  update can reset them), and when its configuration revision (sys
  cfg_rev, in the status already asked) moves - a setting changed in the
  app. A device that doesn't report the revision is asked again while one
  is wrong, until it's fixed. The ones a valve needs - see
  expected_settings() - and that aren't so are said by name, for whoever
  fixes them in the Shelly app.

The devices come from the integrations themselves (shelly_devices()),
whether written in config.yaml or built by the admin panel - and a
simulated H&T Gen3 (simulated_devices()) is watched the same way, its
reports heard in the simulated world instead of the broker, so a test
bench with no sensor shows this panel as a real one would. What matters
over time goes into the history database, under the device's id: a
mains Shelly's signal and temperature, each battery report, each restart
- so a week's trend survives LocalHome's own restarts. Nothing here ever changes a
device; it only reads.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# A battery H&T Gen3 reports at least every 2 hours (confirmed live: the
# heartbeat lands within a minute or so of exactly 2 h). A gap longer
# than that, less some slack, is a heartbeat that never arrived.
HEARTBEAT_SECONDS = 7200
HEARTBEAT_SLACK = 0.15
# One wake publishes several topics within a second or two: one report.
REPORT_SPAN_SECONDS = 60
# How far a heartbeat may land from its 2-hour slot: they come within a
# minute or so (measured: 121 minutes apart, drifting a minute a cycle).
HEARTBEAT_TOLERANCE_SECONDS = 180
POLL_SECONDS = 300
FIRST_POLL_SECONDS = 20
# A device that didn't answer is asked again this soon, once - often it
# was only restarting (a firmware update: "shutting down in 2267 ms").
RETRY_SECONDS = 30
# A Shelly switches its output off by itself at about 90°C.
HOT_C = 80
MISSED_PER_DAY = 3
# A device's start time, worked out from its uptime and our clock, moves
# by a second or two between polls; a restart moves it by far more.
RESTART_SLACK_SECONDS = 300
# Why it last started (sys.reset_reason, the ESP32's own code): 3 is a
# restart asked for - a firmware update, a settings change, a restart
# from the app or over RPC. Anything else - 1 power on (the power went),
# 4-7 a crash or a watchdog, 9 a brownout - is one nobody asked for.
WANTED_RESET_REASONS = {3}
DAY = 86400
WEEK = 7 * DAY

# Gen2/Gen3 ids end in the device's full MAC; Gen1 ids (6 hex digits)
# don't speak the RPC this relies on.
SHELLY_ID = re.compile(r"^shelly[a-z0-9]*-[0-9a-f]{12}$", re.IGNORECASE)
# The components whose "temperature" is the device's own, not a reading
# (a `temperature:N` component *is* a reading - an H&T's, an add-on's).
POWERED_COMPONENTS = {"switch", "light", "cover", "rgb", "rgbw", "cct", "pm1", "em", "em1"}
# Where the markers live in the history database - see missed_reports().
LISTEN_SENSOR = "shelly_health"
# A simulated sensor's id here: its name, marked - see simulated_devices().
SIMULATED_PREFIX = "simulated:"


def expected_settings(integration) -> dict:
    """What a Shelly relay LocalHome commands has to be set to on the
    device (docs/shelly-gen3.md#plus-1-relay). Any: MQTT commands and
    status on. One of the plant's - a role from the panel (shelly_role) or
    held: its input detached, only LocalHome moves it. Held (a zone
    valve, the dehumidifier - drivers/mqtt/switch.py): its rest state at
    power-on and Auto OFF (or ON) at the hold, which covers a restart of
    the Shelly itself. The bypass: its last state kept through anything
    - restored at power-on, no timer - so summer's closed stays closed.
    Empty for anything that isn't a Shelly relay."""
    options = integration.options or {}
    if getattr(integration, "kind", None) != "switch" \
            or not str(options.get("command_topic") or "").endswith("/command/switch:0"):
        return {}
    expect = {"mqtt_control": True, "mqtt_status": True}
    role = options.get("shelly_role")
    hold = options.get("hold_seconds")
    if role or hold:
        expect["in_mode"] = "detached"
    if hold:
        rest = options.get("rest_state", "off")
        expect["initial_state"] = rest
        expect["auto_on" if rest == "on" else "auto_off"] = int(hold)
    elif role == "bypass":
        expect.update(initial_state="restore_last", auto_off=0, auto_on=0)
    return expect


def settings_wrong(expect: dict, config: dict) -> list[dict]:
    """The expected settings the device has otherwise, from its
    Shelly.GetConfig: [{"key", "value", "expected"}]. A setting the device
    doesn't report (another model, older firmware) isn't one."""
    switch = config.get("switch:0") if isinstance(config.get("switch:0"), dict) else {}
    mqtt = config.get("mqtt") if isinstance(config.get("mqtt"), dict) else {}

    def timer(enabled, delay):
        if enabled is None:
            return None
        return int(_number(delay) or 0) if enabled else 0

    actual = {
        "in_mode": switch.get("in_mode"),
        "initial_state": switch.get("initial_state"),
        "auto_off": timer(switch.get("auto_off"), switch.get("auto_off_delay")),
        "auto_on": timer(switch.get("auto_on"), switch.get("auto_on_delay")),
        "mqtt_control": mqtt.get("enable_control"),
        "mqtt_status": mqtt.get("status_ntf"),
    }
    return [{"key": key, "value": actual[key], "expected": value}
            for key, value in expect.items() if actual.get(key) is not None and actual[key] != value]


def shelly_devices(integrations) -> list[dict]:
    """Every Shelly the integrations use: {"id", "names" (the integrations
    backed by it), "battery", "expect" (expected_settings())}. From an RPC
    `device_id` or the first part of an MQTT topic; battery-powered if a
    topic is its `devicepower`."""
    found: dict[str, dict] = {}
    for integration in integrations:
        options = integration.options or {}
        topics = options.get("state_topic")
        topics = topics if isinstance(topics, list) else [topics]
        topics = [t for t in topics + [options.get("command_topic")] if isinstance(t, str)]
        ids = [t.split("/", 1)[0] for t in topics]
        if isinstance(options.get("device_id"), str):
            ids.append(options["device_id"].strip())
        battery = any("/status/devicepower" in t for t in topics)
        for device_id in dict.fromkeys(ids):
            if not SHELLY_ID.match(device_id):
                continue
            device = found.setdefault(device_id, {"id": device_id, "names": [], "battery": False, "expect": {}})
            device["expect"].update(expected_settings(integration))
            name = options.get("name")
            if name and name not in device["names"]:
                device["names"].append(name)
            device["battery"] = device["battery"] or battery
    return sorted(found.values(), key=lambda d: (not d["battery"], (d["names"][0] if d["names"] else d["id"]).lower()))


def simulated_devices(integrations) -> list[dict]:
    """Every simulated H&T Gen3 (drivers/simulated/climate.py's
    mimic_ht_gen3), as a battery device: its id is its name with
    SIMULATED_PREFIX, the topics it publishes under in the simulated
    world are its name's."""
    found = []
    for integration in integrations:
        options = integration.options or {}
        if (integration.kind, integration.type) == ("climate", "simulated") and options.get("mimic_ht_gen3")                 and options.get("name"):
            found.append({"id": SIMULATED_PREFIX + options["name"], "names": [options["name"]],
                          "battery": True, "simulated": True})
    return sorted(found, key=lambda d: d["names"][0].lower())


def missed_reports(reports: list[float], starts: list[float], since: float, now: float,
                   heartbeat: float = HEARTBEAT_SECONDS) -> int:
    """Heartbeats that never arrived between `since` and `now`: each gap
    between two reports - or from the last one to now - longer than a
    heartbeat (plus slack) counts the heartbeats that fit in it.

    A gap that ends with LocalHome starting isn't counted: it was down,
    or the board was off (unplugged, say), and the reports sent meanwhile
    were lost here, not by the sensor. `starts` are those start times."""
    events = sorted([(t, "report") for t in reports] + [(t, "start") for t in starts]) + [(now, "now")]
    missed = 0
    for (before, _), (at, kind) in zip(events, events[1:]):
        if kind == "start" or at <= since:
            continue
        gap = at - max(before, since)
        missed += max(0, int((gap - HEARTBEAT_SLACK * heartbeat) // heartbeat))
    return missed


def split_reports(reports: list[float], heartbeat: float = HEARTBEAT_SECONDS,
                  tolerance: float = HEARTBEAT_TOLERANCE_SECONDS) -> tuple[list[float], list[float]]:
    """(heartbeats, change reports): which reports were the sensor's
    regular 2-hour heartbeat and which came in between, for a change past
    its thresholds.

    The heartbeat keeps its own clock - a change report doesn't move it
    (measured: an extra report at 12:14, the next heartbeat still at 12:53,
    two hours after 10:52). So each report is set against the last
    heartbeat: a whole number of heartbeats later (give or take a few
    minutes; a lost one in between keeps the rhythm) is a heartbeat, sooner
    is a change. Later and off the rhythm, the rhythm itself has changed -
    a new battery, a restart - and that report starts it again."""
    beats: list[float] = []
    changes: list[float] = []
    for ts in sorted(reports):
        if not beats:
            beats.append(ts)
            continue
        since = ts - beats[-1]
        slots = max(1, round(since / heartbeat))
        if abs(since - slots * heartbeat) <= tolerance or since > heartbeat + tolerance:
            beats.append(ts)
        else:
            changes.append(ts)
    return beats, changes


def next_heartbeat(beats: list[float], now: float, heartbeat: float = HEARTBEAT_SECONDS) -> float | None:
    """When the next heartbeat is due: the first 2-hour slot after `now`
    counted from the last one heard."""
    if not beats:
        return None
    slots = max(1, int((now - beats[-1]) // heartbeat) + 1)
    return beats[-1] + slots * heartbeat


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class ShellyHealth:
    def __init__(self, devices: list[dict], store=None, subscribe: Callable | None = None,
                 rpc_for: Callable[[str], Any] | None = None, clock: Callable[[], float] = time.time,
                 reading_of: Callable[[str], dict] | None = None):
        self.store = store
        self.clock = clock
        # The sensor's own last reading (DeviceManager.sensor_reading): what
        # a battery sensor said before this start, until it wakes again.
        self._reading_of = reading_of
        self._lock = threading.Lock()
        self._rpc_for = rpc_for
        self._rpc: dict[str, Any] = {}
        self._devices: dict[str, dict] = {}
        self._problems: list[dict] = []
        for device in devices:
            state = {
                "id": device["id"], "names": list(device["names"]), "battery": device["battery"],
                "simulated": bool(device.get("simulated")),
                "online": None, "error": None, "polled_at": None,
                "wifi_rssi_dbm": None, "wifi_at": None, "ssid": None,
                "device_temp_c": None, "voltage_v": None, "errors": [],
                "booted_at": None, "update": None, "restart_required": False,
                "model": None, "firmware": None,
                "expect": dict(device.get("expect") or {}), "settings_wrong": [], "settings_read": False,
                "cfg_rev": None,
                "battery_pct": None, "battery_v": None, "external_power": None, "last_report_at": None,
            }
            self._devices[device["id"]] = state
            self._seed_from_history(state)
            if subscribe is not None:
                self._subscribe(subscribe, state)
        self._record(LISTEN_SENSOR, {"start": 1})

    # --------------------------------------------------------- listening --

    def _subscribe(self, subscribe: Callable, state: dict) -> None:
        device_id = state["id"]
        on = lambda handler: (lambda topic, payload: self._on_message(state, handler, payload))
        subscribe(f"{device_id}/status/wifi", on(self._on_wifi))
        subscribe(f"{device_id}/status/sys", on(self._on_sys))
        if state["battery"]:
            subscribe(f"{device_id}/status/devicepower:0", on(self._on_devicepower))
        else:
            # The broker's word for "connected" - a battery sensor's is
            # "false" nearly all the time, asleep, so it's not listened to.
            subscribe(f"{device_id}/online", on(self._on_online))

    def _on_message(self, state: dict, handler: Callable, payload: bytes) -> None:
        try:
            text = payload.decode(errors="replace") if isinstance(payload, bytes) else str(payload)
            try:
                data = json.loads(text)
            except ValueError:
                data = text
            with self._lock:
                handler(state, data)
        except Exception:
            logger.exception("Shelly health: couldn't read a message from %s", state["id"])

    def _on_wifi(self, state: dict, data: Any) -> None:
        rssi = _number(_dig(data, "rssi"))
        if rssi is None:
            return
        state["wifi_rssi_dbm"], state["wifi_at"], state["ssid"] = rssi, self.clock(), _dig(data, "ssid")
        if state["battery"]:
            self._note_report(state)

    def _on_sys(self, state: dict, data: Any) -> None:
        self._read_sys(state, data)
        if state["battery"]:
            self._note_report(state)

    def _on_devicepower(self, state: dict, data: Any) -> None:
        state["battery_pct"] = _number(_dig(data, "battery", "percent"))
        state["battery_v"] = _number(_dig(data, "battery", "V"))
        present = _dig(data, "external", "present")
        state["external_power"] = present if isinstance(present, bool) else None
        self._note_report(state)

    def _on_online(self, state: dict, data: Any) -> None:
        state["online"] = str(data).strip().lower() == "true"

    def _note_report(self, state: dict) -> None:
        now = self.clock()
        if state["last_report_at"] is None or now - state["last_report_at"] > REPORT_SPAN_SECONDS:
            self._record(state["id"], {"report": 1})
        state["last_report_at"] = now

    # ----------------------------------------------------------- asking --

    def poll_once(self) -> bool:
        """Asks every mains Shelly for its status - one RPC each. True when
        one of them didn't answer."""
        failed = False
        for state in list(self._devices.values()):
            if state["battery"] or self._rpc_for is None:
                continue
            try:
                rpc = self._rpc.get(state["id"]) or self._rpc.setdefault(state["id"], self._rpc_for(state["id"]))
                status = rpc.call("Shelly.GetStatus")
            except Exception as exc:
                with self._lock:
                    state["error"] = str(exc)
                failed = True
                continue
            with self._lock:
                self._read_status(state, status)
            # Its model and firmware: at the first answer, and again after a
            # restart (_read_sys() forgets them) - a firmware update is one.
            if state["model"] is None:
                try:
                    info = rpc.call("Shelly.GetDeviceInfo")
                    with self._lock:
                        state["model"], state["firmware"] = info.get("model"), info.get("ver")
                except Exception:
                    pass        # asked again at the next check
            if state["expect"] and (not state["settings_read"]
                                    or (state["settings_wrong"] and state["cfg_rev"] is None)):
                try:
                    config = rpc.call("Shelly.GetConfig")
                except Exception:
                    continue    # asked at the next check; the status above stands
                with self._lock:
                    state["settings_wrong"] = settings_wrong(state["expect"], config)
                    state["settings_read"] = True
        self.refresh_problems()
        return failed

    def _read_status(self, state: dict, status: dict) -> None:
        now = self.clock()
        state["error"], state["polled_at"] = None, now
        rssi = _number(_dig(status, "wifi", "rssi"))
        if rssi is not None:
            state["wifi_rssi_dbm"], state["wifi_at"], state["ssid"] = rssi, now, _dig(status, "wifi", "ssid")
        temps, errors, voltage = [], [], None
        for key, component in status.items():
            if key.split(":")[0] not in POWERED_COMPONENTS or not isinstance(component, dict):
                continue
            temp = _number(_dig(component, "temperature", "tC"))
            if temp is not None:
                temps.append(temp)
            errors += [e for e in component.get("errors") or [] if isinstance(e, str)]
            voltage = voltage if voltage is not None else _number(component.get("voltage"))
        state["device_temp_c"] = max(temps) if temps else None
        state["errors"], state["voltage_v"] = sorted(set(errors)), voltage
        self._read_sys(state, status.get("sys"))
        self._record(state["id"], {k: v for k, v in (("wifi_rssi_dbm", rssi), ("device_temp_c", state["device_temp_c"]))
                                   if v is not None})

    def _read_sys(self, state: dict, sys: Any) -> None:
        if not isinstance(sys, dict):
            return
        update = _dig(sys, "available_updates", "stable", "version")
        state["update"] = update if isinstance(update, str) else None
        # A setting changed (in the app, or anywhere): read them again.
        rev = sys.get("cfg_rev")
        if rev is not None and state["cfg_rev"] is not None and rev != state["cfg_rev"]:
            state["settings_read"] = False
        if rev is not None:
            state["cfg_rev"] = rev
        state["restart_required"] = bool(sys.get("restart_required"))
        uptime = _number(sys.get("uptime"))
        if uptime is None or state["battery"]:
            return          # a sleeping sensor's uptime says nothing about power cuts
        booted = self.clock() - uptime
        known = state["booted_at"]
        if known is None or abs(booted - known) > RESTART_SLACK_SECONDS:
            if known is not None and booted > known:
                # Recorded by kind: only the unexpected ones are a sign of
                # trouble (rows from before reasons were read, plain
                # "restart", are left out of both counts).
                wanted = _number(sys.get("reset_reason")) in WANTED_RESET_REASONS
                self._record(state["id"], {"restart_wanted" if wanted else "restart_unexpected": 1}, ts=booted)
                # A firmware update is one: its version and settings are
                # asked again.
                state["model"] = None
                state["settings_read"] = False
            self._record(state["id"], {"booted_at": booted})
            state["booted_at"] = booted

    # ---------------------------------------------------------- history --

    def _record(self, sensor: str, fields: dict, ts: float | None = None) -> None:
        if self.store is None or not fields:
            return
        try:
            self.store.record(sensor, int(ts if ts is not None else self.clock()), fields)
        except Exception:
            logger.exception("Shelly health: couldn't record %s for %s", list(fields), sensor)

    def _history(self, sensor: str, field: str, since: float) -> list[tuple[int, float]]:
        if self.store is None:
            return []
        try:
            return self.store.values_since(sensor, field, int(since))
        except Exception:
            logger.exception("Shelly health: couldn't read %s of %s", field, sensor)
            return []

    def _seed_from_history(self, state: dict) -> None:
        """What was known before this start: the last report, signal and
        start time - a battery sensor may not speak again for 2 hours."""
        since = self.clock() - WEEK
        for field, key in (("report", "last_report_at"), ("wifi_rssi_dbm", "wifi_at"), ("booted_at", None)):
            if state["battery"] and field != "report":
                continue
            rows = self._history(state["id"], field, since)
            if not rows:
                continue
            ts, value = rows[-1]
            if field == "booted_at":
                state["booted_at"] = value
            else:
                state[key] = float(ts)
                if field == "wifi_rssi_dbm":
                    state["wifi_rssi_dbm"] = value

    def _stats(self, state: dict, now: float) -> dict:
        # A battery sensor's signal is recorded with its readings, under its
        # name; a mains Shelly's by this check, under its id.
        signal_sensor = state["names"][0] if state["battery"] and state["names"] else state["id"]

        def signal(since):
            values = [v for _, v in self._history(signal_sensor, "wifi_rssi_dbm", since)]
            if not values:
                return None
            return {"avg": round(sum(values) / len(values)), "min": round(min(values)), "count": len(values)}

        stats = {"signal_24h": signal(now - DAY), "signal_7d": signal(now - WEEK)}
        if state["battery"]:
            reports = [ts for ts, _ in self._history(state["id"], "report", now - WEEK)]
            starts = [ts for ts, _ in self._history(LISTEN_SENSOR, "start", now - WEEK)]
            stats["reports_24h"] = sum(1 for ts in reports if ts >= now - DAY)
            # Classified over the week, counted over the day: the rhythm is
            # found from reports older than the day, too.
            beats, changes = split_reports(reports)
            stats["heartbeats_24h"] = sum(1 for ts in beats if ts >= now - DAY)
            stats["changes_24h"] = sum(1 for ts in changes if ts >= now - DAY)
            stats["next_heartbeat_at"] = next_heartbeat(beats, now)
            stats["missed_24h"] = missed_reports(reports, starts, now - DAY, now)
            stats["missed_7d"] = missed_reports(reports, starts, now - WEEK, now)
            # Since when it's been counted: a sensor added this week has
            # less than a week of reports behind it.
            stats["counted_since"] = reports[0] if reports else None
        else:
            stats["restarts_7d"] = len(self._history(state["id"], "restart_unexpected", now - WEEK))
            stats["restarts_wanted_7d"] = len(self._history(state["id"], "restart_wanted", now - WEEK))
        return stats

    # ----------------------------------------------------------- output --

    def devices(self) -> list[dict]:
        now = self.clock()
        with self._lock:
            states = [dict(s, errors=list(s["errors"]), settings_wrong=list(s["settings_wrong"]))
                      for s in self._devices.values()]
        for state in states:
            state["stats"] = self._stats(state, now)
            state["name"] = state["names"][0] if state["names"] else state["id"]
            if state["battery"] and self._reading_of is not None:
                self._fill_from_reading(state, now)
        return states

    def _fill_from_reading(self, state: dict, now: float) -> None:
        """Until a battery sensor wakes after this start, what its own
        driver last had (kept across restarts - see drivers/mqtt/state_cache.py)."""
        try:
            reading = self._reading_of(state["name"]) or {}
        except Exception:
            return
        for key, field in (("wifi_rssi_dbm", "wifi_rssi_dbm"), ("battery_pct", "battery_pct")):
            if state[key] is None and _number(reading.get(field)) is not None:
                state[key] = reading[field]
        age = _number(reading.get("seconds_since_update"))
        if age is None:
            age = _number(reading.get("last_value_age_seconds"))
        if state["last_report_at"] is None and age is not None:
            state["last_report_at"] = now - age

    def refresh_problems(self) -> None:
        found = []
        for device in self.devices():
            name, stats = device["name"], device["stats"]
            if device["device_temp_c"] is not None and device["device_temp_c"] >= HOT_C:
                found.append({"code": "device_hot", "subject": name, "detail": f"{device['device_temp_c']:.0f}°C"})
            # A weak signal alone isn't one: a sensor at -88 dBm can get
            # every report through for weeks. Reports that don't arrive are.
            if device["battery"] and stats.get("missed_24h", 0) >= MISSED_PER_DAY:
                found.append({"code": "device_reports_missed", "subject": name, "detail": str(stats["missed_24h"])})
            if device["settings_wrong"]:
                keys = [s["key"] for s in device["settings_wrong"]]
                found.append({"code": "shelly_settings", "subject": name, "detail": ", ".join(keys), "settings": keys})
        with self._lock:
            self._problems = found

    def problems(self) -> list[dict]:
        """The last check's problems - worked out every 5 minutes, so the
        LED matrix and the dashboard's badge can ask for them all day."""
        with self._lock:
            return list(self._problems)

    def start(self, every: float = POLL_SECONDS, first: float = FIRST_POLL_SECONDS,
              retry: float = RETRY_SECONDS) -> None:
        def loop():
            time.sleep(first)
            retried = False
            while True:
                failed = False
                try:
                    failed = self.poll_once()
                except Exception:
                    logger.exception("Shelly health check failed")
                # Once soon after a device didn't answer, then the usual pace.
                retried = failed and not retried
                time.sleep(retry if retried else every)
        threading.Thread(target=loop, daemon=True, name="shelly-health").start()
