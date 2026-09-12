"""Driver for a Meross smartplug via the meross-iot cloud library (HTTP
login + MQTT). The manager keeps a persistent MQTT connection and is
asyncio-native, so `async_setup`/`async_read` run inside the AsyncPoller's
own event loop; `turn_on`/`turn_off` are called from Flask's sync request
thread and submitted into that loop with `run_coroutine_threadsafe`.

The device is looked up by its exact name in the Meross app by default,
so setup doesn't require digging a UUID out of the account - set `uuid`
in devices_file instead if you'd rather pin it that way.
"""
from __future__ import annotations

import asyncio
from typing import Any

from meross_iot.controller.mixins.electricity import ElectricityMixin
from meross_iot.http_api import MerossHttpClient
from meross_iot.manager import MerossManager

from localhome.core.interfaces import SwitchDriver
from localhome.core.registry import register_driver
from localhome.util.jsonfile import load_json

COMMAND_TIMEOUT_SECONDS = 10


class MerossPlugDriver(SwitchDriver):
    is_async = True

    def __init__(
        self,
        name: str,
        device_name: str | None,
        uuid: str | None,
        credentials: dict,
        poll_interval_seconds: float = 20.0,
    ):
        self.name = name
        self._device_name = device_name
        self._uuid = uuid
        self._credentials = credentials
        self.poll_interval_seconds = poll_interval_seconds
        self._plug = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def history_fields(self) -> tuple[str, ...]:
        return ("power_w", "voltage_v", "current_a") if isinstance(self._plug, ElectricityMixin) else ()

    async def async_setup(self) -> None:
        self._loop = asyncio.get_running_loop()
        http_client = await MerossHttpClient.async_from_user_password(
            api_base_url=self._credentials.get("api_base_url", "https://iotx-eu.meross.com"),
            email=self._credentials["email"],
            password=self._credentials["password"],
        )
        manager = MerossManager(http_client=http_client)
        await manager.async_init()
        await manager.async_device_discovery()

        devices = manager.find_devices(
            device_uuids=[self._uuid] if self._uuid else None,
            device_name=self._device_name,
        )
        if not devices:
            raise RuntimeError(f"device '{self._device_name or self._uuid}' not found on Meross account")
        self._plug = devices[0]

    async def async_read(self) -> dict[str, Any]:
        await self._plug.async_update()
        reading: dict[str, Any] = {
            "ok": True,
            "name": self.name,
            "online": self._plug.online_status.name,
            "is_on": self._plug.is_on(),
        }
        if isinstance(self._plug, ElectricityMixin):
            metrics = await self._plug.async_get_instant_metrics()
            reading["power_w"] = metrics.power
            reading["voltage_v"] = metrics.voltage
            reading["current_a"] = metrics.current
        return reading

    def turn_on(self) -> None:
        self._submit(self._plug.async_turn_on())

    def turn_off(self) -> None:
        self._submit(self._plug.async_turn_off())

    def _submit(self, coro) -> None:
        if self._plug is None or self._loop is None:
            raise RuntimeError("Meross plug not ready yet")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.result(timeout=COMMAND_TIMEOUT_SECONDS)


@register_driver("switch", "meross_plug")
def _create(options: dict) -> MerossPlugDriver:
    device_config = load_json(options["devices_file"])
    credentials = load_json(options["secrets_file"])
    return MerossPlugDriver(
        name=options.get("name", device_config.get("name", "Meross plug")),
        device_name=device_config.get("device_name"),
        uuid=device_config.get("uuid"),
        credentials=credentials,
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
    )
