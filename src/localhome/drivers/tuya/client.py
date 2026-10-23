"""Shared helpers for Tuya LAN drivers (tinytuya-based).

Every Tuya driver reads the same devices.json produced by
`python -m tinytuya wizard` (optionally merged with IPs found by
`python -m tinytuya scan`), and filters it by product category - see
docs/integrations/tuya.md.

Two things every driver here needs that tinytuya doesn't do by itself:

1. **Failures come back as a payload, not an exception.** `status()`
   returns `{"Error": "Check device key or version", "Err": "914"}` and a
   driver that just reads `dps` out of it reports an empty reading - which
   is how five covers sat on a dashboard for days showing "unknown"
   instead of "not reachable". `require_dps()` turns those payloads into
   errors that say what to do about them.

2. **A Tuya device is addressed by IP, and DHCP can move it.** The
   address in devices.json is only a starting point: when a device stops
   answering there, its id is looked up on the LAN - Tuya devices
   announce themselves by id on a UDP broadcast - and the new address is
   remembered in `tuya_addresses.json` next to devices.json, so the next
   restart starts from the address that worked last.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import tinytuya

from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

# tinytuya's own error codes (see its error_json()). These mean "nothing
# answered at this address", which is what a changed IP looks like.
UNREACHABLE_CODES = {"901", "902", "905"}
# What to do about the ones an installation actually hits.
ERROR_HINTS = {
    "905": "nothing answered at this address - the device is off, or its IP changed",
    "914": "the device's local key (or protocol version) no longer matches - "
           "refresh config/devices.json with the tinytuya wizard, see docs/integrations/tuya.md",
}
# Don't scan the network for the same device more often than this: a
# device that's simply switched off would otherwise trigger a scan on
# every poll.
REDISCOVER_EVERY_SECONDS = 300


class TuyaDeviceError(RuntimeError):
    """A Tuya device answered with an error payload, or didn't answer."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code

    @property
    def unreachable(self) -> bool:
        return self.code in UNREACHABLE_CODES


def load_devices(devices_file: str, category: str | None = None) -> list[dict]:
    with open(devices_file, encoding="utf-8") as f:
        devices = json.load(f)
    if category is not None:
        devices = [d for d in devices if d.get("category") == category]
    named = [d for d in devices if d.get("ip")]
    for entry in devices:
        if not entry.get("ip"):
            # Without an address there's nothing to talk to - say so once
            # instead of silently leaving the device out of the dashboard.
            logger.warning(
                "Tuya device %r in %s has no 'ip' - run `python -m tinytuya scan` and merge it in, "
                "or remove the entry", entry.get("name", entry.get("id", "?")), devices_file,
            )
    return named


def require_dps(raw: dict, name: str) -> dict:
    """The `dps` values out of a tinytuya response, or a TuyaDeviceError
    explaining what went wrong."""
    dps = (raw or {}).get("dps")
    if dps:
        return dps
    code = str((raw or {}).get("Err") or "")
    message = (raw or {}).get("Error") or (f"unexpected response: {raw}" if raw else "no response")
    hint = ERROR_HINTS.get(code)
    raise TuyaDeviceError(f"{name}: {message}" + (f" - {hint}" if hint else ""), code=code)


def _find_on_lan(device_id: str, timeout: float = 12.0) -> str | None:
    """The device's current address, from its own broadcast."""
    found = tinytuya.find_device(dev_id=device_id) or {}
    return found.get("ip")


class TuyaAddressBook:
    """Each device's last known good address, remembered across restarts."""

    def __init__(self, cache_path: str, finder=None, retry_after_seconds: float = REDISCOVER_EVERY_SECONDS):
        self.cache_path = cache_path
        self._finder = finder or _find_on_lan
        self._retry_after = retry_after_seconds
        self._lock = threading.Lock()
        self._last_attempt: dict[str, float] = {}
        try:
            self._addresses = load_json(cache_path)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._addresses = {}

    def ip_for(self, entry: dict) -> str:
        return self._addresses.get(entry["id"], {}).get("ip") or entry.get("ip")

    def remember(self, device_id: str, ip: str) -> None:
        with self._lock:
            self._addresses[device_id] = {"ip": ip, "found_at": int(time.time())}
            try:
                save_json_atomic(self.cache_path, self._addresses)
            except OSError as exc:  # a read-only config dir shouldn't stop the driver
                logger.warning("Could not save %s (%s)", self.cache_path, exc)

    def rediscover(self, entry: dict, now: float | None = None) -> str | None:
        """Ask the network where this device is now. Returns its address
        only if it's a new one; throttled, so a device that's simply off
        doesn't start a scan on every poll."""
        now = time.time() if now is None else now
        device_id = entry["id"]
        if now - self._last_attempt.get(device_id, 0) < self._retry_after:
            return None
        self._last_attempt[device_id] = now
        try:
            ip = self._finder(device_id)
        except Exception as exc:
            logger.debug("Tuya discovery for %s failed: %s", device_id, exc)
            return None
        if not ip or ip == self.ip_for(entry):
            return None
        logger.info("Tuya device %r moved to %s - remembering it", entry.get("name", device_id), ip)
        self.remember(device_id, ip)
        return ip


class TuyaConnection:
    """One device on the LAN, addressed by id: the IP is only where we
    look first. Every call raises TuyaDeviceError instead of returning
    tinytuya's error payload, and a device that stopped answering is
    looked up once before the call is given up on."""

    def __init__(self, entry: dict, version: float = 3.4, addresses: TuyaAddressBook | None = None,
                 socket_timeout: float = 5.0):
        self.entry = entry
        self.name = entry.get("name", entry["id"])
        self.version = version
        self.addresses = addresses
        self.socket_timeout = socket_timeout

    def _ip(self) -> str:
        return self.addresses.ip_for(self.entry) if self.addresses else self.entry.get("ip")

    def _device(self, ip: str):
        device = tinytuya.Device(self.entry["id"], ip, self.entry["key"], version=self.version)
        device.set_socketTimeout(self.socket_timeout)
        return device

    def _call(self, call):
        try:
            return call(self._device(self._ip()))
        except TuyaDeviceError as first:
            if not (first.unreachable and self.addresses):
                raise
            ip = self.addresses.rediscover(self.entry)
            if not ip:
                raise
            return call(self._device(ip))

    def status(self) -> dict:
        """The device's `dps` values."""
        return self._call(lambda device: require_dps(device.status(), self.name))

    def set_value(self, dp: int | str, value) -> None:
        self._call(lambda device: require_dps(device.set_value(dp, value), self.name))

    def open_persistent(self, listen_timeout: float = 1.0):
        """A connection kept open, to hear what the device says by itself:
        (device, its dps now). Reached like every call - one look for a new
        address if nothing answers. Reads on it wait `listen_timeout`."""
        def connect(device):
            device.set_socketPersistent(True)
            try:
                dps = require_dps(device.status(), self.name)
            except Exception:
                device.close()
                raise
            device.set_socketTimeout(listen_timeout)
            return device, dps
        return self._call(connect)


def address_book_for(devices_file: str, finder=None) -> TuyaAddressBook:
    """One address book per devices.json, stored beside it."""
    directory = os.path.dirname(os.path.abspath(devices_file))
    return TuyaAddressBook(os.path.join(directory, "tuya_addresses.json"), finder=finder)


def make_device(entry: dict, version: float = 3.4, socket_timeout: float = 5.0) -> tinytuya.Device:
    device = tinytuya.Device(entry["id"], entry["ip"], entry["key"], version=version)
    device.set_socketTimeout(socket_timeout)
    return device
