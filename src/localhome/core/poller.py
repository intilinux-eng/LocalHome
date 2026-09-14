"""Generic background polling, shared by every PollingDriver so drivers
themselves never hand-roll a thread/lock/loop.

Two flavors:

- `SyncPoller` runs `driver.read()` in a plain background thread. Fits
  any driver that talks to a device with a blocking call (typical LAN
  devices - a socket read that takes a few hundred ms is fine on its own
  thread).
- `AsyncPoller` runs its own asyncio event loop in a background thread:
  `driver.async_setup()` once, then `driver.async_read()` every interval.
  Fits cloud SDKs that need a persistent login/session/MQTT connection
  kept alive across polls (recreating that connection every poll would be
  slow and, for MQTT-based control, would drop the ability to push
  commands between polls).

Both expose the same `.get()` -> dict and `.start()` so the rest of the
app (DeviceManager, the web API) never needs to know which one it has.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any


class ReadingCache:
    """Thread-safe latest-reading store shared by both poller flavors."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest: dict[str, Any] = {"ok": False, "error": "not started yet"}
        self._last_update_ts: float | None = None

    def set(self, reading: dict[str, Any]) -> None:
        with self._lock:
            self._latest = reading
            if reading.get("ok"):
                self._last_update_ts = time.time()

    def merge(self, partial: dict[str, Any]) -> None:
        """Patch a few keys into the latest reading without waiting for the
        next poll - used right after a successful command (turn_on/off,
        set_value) so the dashboard reflects it immediately instead of
        showing a stale cached reading for up to poll_interval_seconds."""
        with self._lock:
            self._latest = {**self._latest, **partial}

    def get(self) -> dict[str, Any]:
        with self._lock:
            reading = dict(self._latest)
            last_update = self._last_update_ts
        reading["seconds_since_update"] = None if last_update is None else round(time.time() - last_update)
        return reading


class SyncPoller:
    def __init__(self, driver, cache: ReadingCache | None = None):
        self.driver = driver
        self.cache = cache or ReadingCache()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            self.poll_once()
            time.sleep(self.driver.poll_interval_seconds)

    def poll_once(self) -> None:
        """One read -> cache cycle, split out of _loop() so a test can
        drive several iterations directly instead of starting a real
        thread and sleeping through real poll intervals to observe them."""
        try:
            self.cache.set(self.driver.read())
        except Exception as exc:
            self.cache.set({"ok": False, "error": str(exc)})

    def get(self) -> dict[str, Any]:
        return self.cache.get()


class AsyncPoller:
    def __init__(self, driver, cache: ReadingCache | None = None):
        self.driver = driver
        self.cache = cache or ReadingCache()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    async def _run(self) -> None:
        # A one-shot setup failure used to kill this poller for the rest of
        # the process's life - fatal if it just races something at boot
        # (e.g. the MQTT broker or the network itself not up yet when
        # LocalHome starts), since nothing ever retried it afterwards.
        # Retrying here, same as a read failure, means a slow-starting
        # dependency delays this poller instead of disabling it forever.
        while not await self.setup_once():
            await asyncio.sleep(self.driver.poll_interval_seconds)
        while True:
            await self.poll_once()
            await asyncio.sleep(self.driver.poll_interval_seconds)

    async def setup_once(self) -> bool:
        """Returns True once driver.async_setup() has succeeded. Split out
        of _run() so a test can drive setup/poll cycles directly instead of
        starting a real asyncio loop and sleeping through poll intervals."""
        try:
            await self.driver.async_setup()
            return True
        except Exception as exc:
            self.cache.set({"ok": False, "error": f"setup failed: {exc}"})
            return False

    async def poll_once(self) -> None:
        try:
            self.cache.set(await self.driver.async_read())
        except Exception as exc:
            self.cache.set({"ok": False, "error": str(exc)})

    def get(self) -> dict[str, Any]:
        return self.cache.get()
