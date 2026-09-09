"""Background poller + control for a Meross smartplug via the meross-iot cloud
library (HTTP login + MQTT). The manager keeps a persistent MQTT connection
and is asyncio-native, so it runs its own event loop in a background thread;
Flask's sync route handlers submit on/off commands into that loop with
run_coroutine_threadsafe and block for the result.

The device is looked up by its exact name in the Meross app (meross_devices.json),
not by UUID, so setup doesn't require digging the UUID out of the account.
"""
import asyncio
import json
import threading
import time

from meross_iot.controller.mixins.electricity import ElectricityMixin
from meross_iot.http_api import MerossHttpClient
from meross_iot.manager import MerossManager

DEVICES_FILE = "meross_devices.json"
SECRETS_FILE = "secrets_meross.json"
POLL_INTERVAL_SECONDS = 20
COMMAND_TIMEOUT_SECONDS = 10

_started = False
_lock = threading.Lock()
_latest = {"ok": False, "error": "not started yet"}
_last_update_ts = None
_loop = None
_plug = None
_ready = threading.Event()


def start_background():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()


async def _run():
    global _loop, _plug, _latest, _last_update_ts

    _loop = asyncio.get_event_loop()
    config = json.load(open(DEVICES_FILE))
    creds = json.load(open(SECRETS_FILE))

    try:
        http_client = await MerossHttpClient.async_from_user_password(
            api_base_url=creds.get("api_base_url", "https://iotx-eu.meross.com"),
            email=creds["email"],
            password=creds["password"],
        )
        manager = MerossManager(http_client=http_client)
        await manager.async_init()
        await manager.async_device_discovery()

        devices = manager.find_devices(
            device_uuids=[config["uuid"]] if config.get("uuid") else None,
            device_name=config.get("device_name"),
        )
        if not devices:
            with _lock:
                _latest = {"ok": False, "error": f"device '{config.get('device_name', config.get('uuid'))}' not found on Meross account"}
            return
        _plug = devices[0]
    except Exception as exc:
        with _lock:
            _latest = {"ok": False, "error": f"login/discovery failed: {exc}"}
        return
    finally:
        _ready.set()

    while True:
        try:
            await _plug.async_update()
            reading = {
                "ok": True,
                "name": config.get("name", _plug.name),
                "online": _plug.online_status.name,
                "is_on": _plug.is_on(),
            }
            if isinstance(_plug, ElectricityMixin):
                metrics = await _plug.async_get_instant_metrics()
                reading["power_w"] = metrics.power
                reading["voltage_v"] = metrics.voltage
                reading["current_a"] = metrics.current

            with _lock:
                _latest = reading
                _last_update_ts = time.time()
        except Exception as exc:
            with _lock:
                _latest = {"ok": False, "error": str(exc)}

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def get_plug_reading():
    with _lock:
        reading = dict(_latest)
    reading["seconds_since_update"] = None if _last_update_ts is None else round(time.time() - _last_update_ts)
    return reading


def set_power(on: bool):
    if not _ready.wait(timeout=15) or _plug is None or _loop is None:
        raise RuntimeError("Meross plug not ready")
    coro = _plug.async_turn_on() if on else _plug.async_turn_off()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    future.result(timeout=COMMAND_TIMEOUT_SECONDS)
