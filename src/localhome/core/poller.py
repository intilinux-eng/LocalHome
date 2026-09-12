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
            try:
                self.cache.set(self.driver.read())
            except Exception as exc:
                self.cache.set({"ok": False, "error": str(exc)})
            time.sleep(self.driver.poll_interval_seconds)

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
        try:
            await self.driver.async_setup()
        except Exception as exc:
            self.cache.set({"ok": False, "error": f"setup failed: {exc}"})
            return
        while True:
            try:
                self.cache.set(await self.driver.async_read())
            except Exception as exc:
                self.cache.set({"ok": False, "error": str(exc)})
            await asyncio.sleep(self.driver.poll_interval_seconds)

    def get(self) -> dict[str, Any]:
        return self.cache.get()
