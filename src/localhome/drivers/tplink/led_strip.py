"""Drivers for TP-Link Kasa/Tapo smart LED strips (and bulbs) via
python-kasa, entirely over the LAN - no cloud dependency. `Discover.
discover_single()` auto-negotiates the right protocol version, so these
drivers cover both older Kasa-branded and newer Tapo-branded devices;
Tapo devices additionally need the TP-Link account username/password at
connect time (checked locally against the device, never sent anywhere
over the internet) - see docs/integrations/tplink.md.

python-kasa is asyncio-native but holds no connection worth keeping
across polls - every call below is its own short-lived request - so
these stay plain sync PollingDrivers wrapping each call in its own
asyncio.run(), the way the LAN-only Tuya drivers do, rather than the
AsyncPoller shape the cloud-based Meross driver needs.

Two separate driver classes cover one physical strip: `switch` for
on/off and `number` for brightness (see docs/adding-a-driver.md - one
driver implements one interface). Give each its own config.yaml `name`;
`DeviceManager` keys its poller/sensor-kind lookup by name across every
kind, so reusing the switch's name for the brightness entry would make
one of the two invisible on the dashboard.
"""
from __future__ import annotations

import asyncio
from typing import Any

from kasa import Discover, Module

from localhome.core.interfaces import NumberDriver, SwitchDriver
from localhome.core.registry import register_driver
from localhome.util.jsonfile import load_json

COMMAND_TIMEOUT_SECONDS = 10


async def _connect(host: str, username: str | None, password: str | None):
    dev = await Discover.discover_single(
        host, username=username, password=password, timeout=COMMAND_TIMEOUT_SECONDS
    )
    await dev.update()
    return dev


def _light_module(dev):
    return dev.modules[Module.Light] if Module.Light in dev.modules else None


class TplinkLedStripDriver(SwitchDriver):
    def __init__(
        self,
        name: str,
        host: str,
        username: str | None = None,
        password: str | None = None,
        poll_interval_seconds: float = 20.0,
    ):
        self.name = name
        self._host = host
        self._username = username
        self._password = password
        self.poll_interval_seconds = poll_interval_seconds

    def read(self) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            dev = await _connect(self._host, self._username, self._password)
            reading: dict[str, Any] = {"ok": True, "name": self.name, "is_on": dev.is_on}
            light = _light_module(dev)
            if light is not None:
                try:
                    reading["brightness"] = light.brightness
                except Exception:
                    pass
            return reading

        return asyncio.run(_do())

    def turn_on(self) -> None:
        self._set_power(True)

    def turn_off(self) -> None:
        self._set_power(False)

    def _set_power(self, on: bool) -> None:
        async def _do() -> None:
            dev = await _connect(self._host, self._username, self._password)
            if on:
                await dev.turn_on()
            else:
                await dev.turn_off()

        asyncio.run(_do())


class TplinkLedStripBrightnessDriver(NumberDriver):
    min_value = 1.0
    max_value = 100.0
    unit = "%"

    def __init__(
        self,
        name: str,
        host: str,
        username: str | None = None,
        password: str | None = None,
        poll_interval_seconds: float = 20.0,
    ):
        self.name = name
        self._host = host
        self._username = username
        self._password = password
        self.poll_interval_seconds = poll_interval_seconds

    def read(self) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            dev = await _connect(self._host, self._username, self._password)
            light = _light_module(dev)
            if light is None:
                raise RuntimeError(f"{self._host} has no dimmable light module")
            return {"ok": True, "name": self.name, "value": light.brightness}

        return asyncio.run(_do())

    def set_value(self, value: float) -> None:
        async def _do() -> None:
            dev = await _connect(self._host, self._username, self._password)
            light = _light_module(dev)
            if light is None:
                raise RuntimeError(f"{self._host} has no dimmable light module")
            await light.set_brightness(int(value))

        asyncio.run(_do())


def _options_to_kwargs(options: dict) -> dict:
    credentials = load_json(options["secrets_file"]) if options.get("secrets_file") else {}
    return {
        "host": options["host"],
        "username": credentials.get("username"),
        "password": credentials.get("password"),
        "poll_interval_seconds": options.get("poll_interval_seconds", 20.0),
    }


@register_driver("switch", "tplink_led_strip")
def _create_switch(options: dict) -> TplinkLedStripDriver:
    return TplinkLedStripDriver(name=options.get("name", "TP-Link LED strip"), **_options_to_kwargs(options))


@register_driver("number", "tplink_led_strip")
def _create_brightness(options: dict) -> TplinkLedStripBrightnessDriver:
    return TplinkLedStripBrightnessDriver(
        name=options.get("name", "TP-Link LED strip brightness"), **_options_to_kwargs(options)
    )
