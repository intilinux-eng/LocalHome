"""Drivers for TP-Link Kasa/Tapo smart LED strips (and bulbs) via
python-kasa, entirely over the LAN - no cloud dependency. `Discover.
discover_single()` auto-negotiates the right protocol version, so these
drivers cover both older Kasa-branded and newer Tapo-branded devices;
Tapo devices additionally need the TP-Link account username/password at
connect time (checked locally against the device, never sent anywhere
over the internet) - see docs/integrations/tplink.md.

python-kasa is asyncio-native, and these stay plain sync PollingDrivers
wrapping each call in its own asyncio.run(), the way the LAN-only Tuya
drivers do, rather than the AsyncPoller shape the cloud-based Meross
driver needs. Each call therefore opens its own connection - and has to
close it again: a kasa Device holds a live aiohttp session that the
event loop ending does *not* tear down, so every connection must go
through `_device()` below, never `Discover.discover_single()` directly.
(This module used to assume kasa "holds no connection worth keeping
across polls" and simply dropped each Device on the floor. It does hold
one, and that leaked ~17k sessions a day here.)

A strip is addressed by IP, and the router can move it: a restarted
router handed both of this project's strips new addresses, and they sat
"not answering" at the old ones. So the `host` in config.yaml is only
where to look first. Each time a strip answers, its MAC - which doesn't
change - is remembered with the address that worked (TplinkAddressBook,
`tplink_addresses.json` beside the secrets file); when it stops
answering, the LAN is asked where that MAC is now, at most every few
minutes, and the new address is used and remembered. `mac:` in
config.yaml gives the MAC up front, for a strip that has moved before
LocalHome ever reached it.

Two separate driver classes cover one physical strip: `switch` for
on/off and `number` for brightness (see docs/adding-a-driver.md - one
driver implements one interface). Give each its own config.yaml `name`;
`DeviceManager` keys its poller/sensor-kind lookup by name across every
kind, so reusing the switch's name for the brightness entry would make
one of the two invisible on the dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from kasa import Discover, Module

from localhome.core.interfaces import NumberDriver, SwitchDriver
from localhome.core.registry import register_driver
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 10
# Don't scan the LAN for the same strip more often than this: one that's
# simply switched off would otherwise start a scan on every poll.
REDISCOVER_EVERY_SECONDS = 300
SCAN_TIMEOUT_SECONDS = 8


class NoLightModule(RuntimeError):
    """The device answered, but has nothing to dim - not a moved device."""


@asynccontextmanager
async def _device(host: str, username: str | None, password: str | None):
    """Opens a device connection and *always* closes it again.

    The disconnect is the whole point: python-kasa keeps a real
    connection alive behind the Device object (an aiohttp session, for
    the KLAP transport these strips use), and the event loop ending does
    not clean it up. Returning a connected Device and walking away - the
    previous shape here - leaked one aiohttp session, its connector, its
    socket and about one OS thread on every single read. With four
    drivers (two strips x switch+brightness) polling every 20s that was
    ~17k leaked connections a day, and the bulk of a leak that took the
    process past 4.5GB of RAM in nine hours.
    """
    dev = await Discover.discover_single(
        host, username=username, password=password, timeout=COMMAND_TIMEOUT_SECONDS
    )
    try:
        await dev.update()
        yield dev
    finally:
        await dev.disconnect()


def _mac(value) -> str | None:
    """A MAC as 12 upper-case hex digits, whatever the separators."""
    digits = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    return digits if len(digits) == 12 else None


async def _scan(username: str | None, password: str | None) -> dict[str, str]:
    """Every TP-Link device answering the LAN broadcast: {MAC: IP}. Each
    one found is disconnected again - the same leak as _device()."""
    found = await Discover.discover(timeout=SCAN_TIMEOUT_SECONDS, username=username, password=password)
    addresses = {}
    for ip, dev in found.items():
        try:
            mac = _mac(getattr(dev, "mac", None))
            if mac:
                addresses[mac] = ip
        finally:
            try:
                await dev.disconnect()
            except Exception:
                pass
    return addresses


def _scan_sync(username, password) -> dict[str, str]:
    return asyncio.run(_scan(username, password))


class TplinkAddressBook:
    """Each strip's MAC and last address that answered, keyed by the
    `host` config.yaml gives it. Written only when something changes."""

    def __init__(self, cache_path: str | None, scanner=None, retry_after_seconds: float = REDISCOVER_EVERY_SECONDS):
        self.cache_path = cache_path
        self._scanner = scanner or _scan_sync
        self._retry_after = retry_after_seconds
        self._lock = threading.Lock()
        self._last_attempt: dict[str, float] = {}
        self._entries: dict[str, dict] = {}
        if cache_path:
            try:
                self._entries = load_json(cache_path)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self._entries = {}

    def ip_for(self, host: str) -> str:
        return self._entries.get(host, {}).get("ip") or host

    def mac_for(self, host: str, configured: str | None = None) -> str | None:
        return _mac(configured) or self._entries.get(host, {}).get("mac")

    def learn(self, host: str, ip: str, mac) -> None:
        entry = {"ip": ip, "mac": _mac(mac) or self._entries.get(host, {}).get("mac")}
        with self._lock:
            if {k: self._entries.get(host, {}).get(k) for k in ("ip", "mac")} == entry:
                return
            self._entries[host] = dict(entry, found_at=int(time.time()))
            self._save()

    def _save(self) -> None:
        if not self.cache_path:
            return
        try:
            save_json_atomic(self.cache_path, self._entries)
        except OSError as exc:  # a read-only config dir shouldn't stop the driver
            logger.warning("Could not save %s (%s)", self.cache_path, exc)

    def rediscover(self, host: str, username, password, configured_mac: str | None = None,
                   now: float | None = None) -> str | None:
        """Where the strip with this host's MAC is now - only a new address;
        throttled, so a strip that's off doesn't start a scan per poll."""
        mac = self.mac_for(host, configured_mac)
        if not mac:
            return None
        now = time.time() if now is None else now
        if now - self._last_attempt.get(host, 0) < self._retry_after:
            return None
        self._last_attempt[host] = now
        try:
            ip = self._scanner(username, password).get(mac)
        except Exception as exc:
            logger.debug("TP-Link discovery for %s failed: %s", host, exc)
            return None
        if not ip or ip == self.ip_for(host):
            return None
        logger.info("TP-Link device %s (MAC %s) moved to %s - remembering it", host, mac, ip)
        self.learn(host, ip, mac)
        return ip


# One book per file, shared by a strip's switch and brightness drivers.
_BOOKS: dict[str | None, TplinkAddressBook] = {}
_BOOKS_LOCK = threading.Lock()


def address_book_for(cache_path: str | None) -> TplinkAddressBook:
    with _BOOKS_LOCK:
        if cache_path not in _BOOKS:
            _BOOKS[cache_path] = TplinkAddressBook(cache_path)
        return _BOOKS[cache_path]


def _light_module(dev):
    return dev.modules[Module.Light] if Module.Light in dev.modules else None


class _Strip:
    """What both drivers share: finding the strip, and running one call on
    it - at the address that worked last, and if nothing answers there,
    at the address its MAC has moved to."""

    def __init__(self, host: str, username: str | None, password: str | None, mac: str | None = None,
                 addresses: TplinkAddressBook | None = None):
        self._host = host
        self._username = username
        self._password = password
        self._mac = mac
        self._addresses = addresses or TplinkAddressBook(None)

    def _run(self, action):
        async def attempt(ip):
            async with _device(ip, self._username, self._password) as dev:
                result = await action(dev)
                self._addresses.learn(self._host, ip, getattr(dev, "mac", None) or self._mac)
                return result

        try:
            return asyncio.run(attempt(self._addresses.ip_for(self._host)))
        except NoLightModule:
            raise
        except Exception:
            ip = self._addresses.rediscover(self._host, self._username, self._password, self._mac)
            if not ip:
                raise
            return asyncio.run(attempt(ip))


class TplinkLedStripDriver(_Strip, SwitchDriver):
    def __init__(
        self,
        name: str,
        host: str,
        username: str | None = None,
        password: str | None = None,
        poll_interval_seconds: float = 20.0,
        mac: str | None = None,
        addresses: TplinkAddressBook | None = None,
    ):
        _Strip.__init__(self, host, username, password, mac, addresses)
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds

    def read(self) -> dict[str, Any]:
        async def _do(dev) -> dict[str, Any]:
            reading: dict[str, Any] = {"ok": True, "name": self.name, "is_on": dev.is_on}
            light = _light_module(dev)
            if light is not None:
                try:
                    reading["brightness"] = light.brightness
                except Exception:
                    pass
            return reading

        return self._run(_do)

    def turn_on(self) -> None:
        self._set_power(True)

    def turn_off(self) -> None:
        self._set_power(False)

    def _set_power(self, on: bool) -> None:
        async def _do(dev) -> None:
            if on:
                await dev.turn_on()
            else:
                await dev.turn_off()

        self._run(_do)


class TplinkLedStripBrightnessDriver(_Strip, NumberDriver):
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
        mac: str | None = None,
        addresses: TplinkAddressBook | None = None,
    ):
        _Strip.__init__(self, host, username, password, mac, addresses)
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds

    def read(self) -> dict[str, Any]:
        async def _do(dev) -> dict[str, Any]:
            light = _light_module(dev)
            if light is None:
                raise NoLightModule(f"{self._host} has no dimmable light module")
            return {"ok": True, "name": self.name, "value": light.brightness}

        return self._run(_do)

    def set_value(self, value: float) -> None:
        async def _do(dev) -> None:
            light = _light_module(dev)
            if light is None:
                raise NoLightModule(f"{self._host} has no dimmable light module")
            await light.set_brightness(int(value))

        self._run(_do)


def _options_to_kwargs(options: dict) -> dict:
    secrets_file = options.get("secrets_file")
    credentials = load_json(secrets_file) if secrets_file else {}
    # Remembered beside the secrets file - the config dir - when there is one.
    book_path = os.path.join(os.path.dirname(os.path.abspath(secrets_file)), "tplink_addresses.json") \
        if secrets_file else None
    return {
        "host": options["host"],
        "username": credentials.get("username"),
        "password": credentials.get("password"),
        "poll_interval_seconds": options.get("poll_interval_seconds", 20.0),
        "mac": options.get("mac"),
        "addresses": address_book_for(book_path),
    }


@register_driver("switch", "tplink_led_strip")
def _create_switch(options: dict) -> TplinkLedStripDriver:
    return TplinkLedStripDriver(name=options.get("name", "TP-Link LED strip"), **_options_to_kwargs(options))


@register_driver("number", "tplink_led_strip")
def _create_brightness(options: dict) -> TplinkLedStripBrightnessDriver:
    return TplinkLedStripBrightnessDriver(
        name=options.get("name", "TP-Link LED strip brightness"), **_options_to_kwargs(options)
    )
