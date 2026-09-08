"""Background poller for the Sonoff POWCT power sensor via the eWeLink cloud.

We first tried listening for the device's LAN mDNS broadcasts (fully local,
like the shutters), but this device's updates never arrived reliably after
the first one — a limitation of the community LAN library for this
device/firmware, not something worth fighting further. Empirically, the
eWeLink cloud's basic device-list endpoint IS still refreshed by the
device every ~20-30s even though real-time push streaming was cut off in
2026, so we poll that instead. This makes the energy widget (only)
depend on eWeLink cloud availability — shutter control is unaffected and
stays fully local via tinytuya.
"""
import asyncio
import json
import threading
import time

from ewelink_cloud import EweLinkCloud

DEVICES_FILE = "ewelink_devices.json"
POLL_INTERVAL_SECONDS = 20

_started = False
_lock = threading.Lock()
_latest = {"ok": False, "error": "not started yet"}
_last_seen_power = None
_last_update_ts = None


def start_background():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=lambda: asyncio.run(_poll_loop()), daemon=True).start()


async def _poll_loop():
    global _latest, _last_seen_power, _last_update_ts

    config = json.load(open(DEVICES_FILE))
    creds = json.load(open("secrets_ewelink.json"))
    cloud = EweLinkCloud()

    try:
        await cloud.login(
            creds["username"], creds["password"],
            region=creds.get("region", "eu"),
            country_code=creds.get("country_code", "+39"),
        )
    except Exception as exc:
        with _lock:
            _latest = {"ok": False, "error": f"login failed: {exc}"}
        return

    while True:
        try:
            devices = await cloud.get_devices()
            device = next((d for d in devices if d["deviceid"] == config["device_id"]), None)
            if device is None:
                raise RuntimeError("device not found in account")

            params = device.get("params", {})
            power = params.get("power", 0)

            if power != _last_seen_power:
                _last_seen_power = power
                _last_update_ts = time.time()

            reading = {
                "ok": True,
                "name": config.get("name", device.get("name")),
                "online": bool(device.get("online")),
                "power_w": power / 100,
                "voltage_v": params.get("voltage", 0) / 100,
                "current_a": params.get("current", 0) / 100,
                "day_kwh": params.get("dayKwh", 0) / 100,
                "month_kwh": params.get("monthKwh", 0) / 100,
                "yesterday_kwh": params.get("yesterdayKwh", 0) / 100,
            }
            with _lock:
                _latest = reading
        except Exception as exc:
            with _lock:
                _latest = {"ok": False, "error": str(exc)}

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def get_power_reading():
    with _lock:
        reading = dict(_latest)
    reading["seconds_since_update"] = None if _last_update_ts is None else round(time.time() - _last_update_ts)
    return reading
