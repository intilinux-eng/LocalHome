"""Driver for the Sonoff POWCT power-monitoring relay via the eWeLink
cloud (v2 API).

LAN-only control was tried first (mDNS broadcasts, the same way the Tuya
covers work), but this device's updates never arrived reliably after the
first one - a limitation of the community LAN library for this
device/firmware, not something worth fighting further. Empirically,
eWeLink's basic device-list endpoint is still refreshed by the device
every ~20-30s even without a push subscription, so this driver polls
that instead. This makes this one sensor (only) depend on eWeLink cloud
availability; every other driver in this project stays fully local.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.drivers.ewelink.cloud import EweLinkCloud
from localhome.util.jsonfile import load_json


class EweLinkPowerDriver(PollingDriver):
    is_async = True
    history_fields = ("power_w", "voltage_v", "current_a")

    def __init__(self, device_id: str, name: str, credentials: dict, poll_interval_seconds: float = 20.0):
        self.name = name
        self._device_id = device_id
        self._credentials = credentials
        self.poll_interval_seconds = poll_interval_seconds
        self._cloud = EweLinkCloud()

    async def async_setup(self) -> None:
        await self._cloud.login(
            self._credentials["username"],
            self._credentials["password"],
            region=self._credentials.get("region", "eu"),
            country_code=self._credentials.get("country_code", "+1"),
        )

    async def async_read(self) -> dict[str, Any]:
        devices = await self._cloud.get_devices()
        device = next((d for d in devices if d["deviceid"] == self._device_id), None)
        if device is None:
            raise RuntimeError("device not found in eWeLink account")

        params = device.get("params", {})
        power = params.get("power", 0)
        return {
            "ok": True,
            "name": self.name,
            "online": bool(device.get("online")),
            "power_w": power / 100,
            "voltage_v": params.get("voltage", 0) / 100,
            "current_a": params.get("current", 0) / 100,
            "day_kwh": params.get("dayKwh", 0) / 100,
            "month_kwh": params.get("monthKwh", 0) / 100,
            "yesterday_kwh": params.get("yesterdayKwh", 0) / 100,
        }


@register_driver("power_meter", "ewelink_powct")
def _create(options: dict) -> EweLinkPowerDriver:
    device_config = load_json(options["devices_file"])
    credentials = load_json(options["secrets_file"])
    name = options.get("name", device_config.get("name", "eWeLink power meter"))
    return EweLinkPowerDriver(
        device_id=device_config["device_id"],
        name=name,
        credentials=credentials,
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
    )
