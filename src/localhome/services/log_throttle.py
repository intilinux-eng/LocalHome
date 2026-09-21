"""Keeps a device that stays unreachable from flooding the log.

The control loops retry a command every tick for as long as it fails - a
relay switched off, or the MQTT broker down - which is right for the
device, but logging a full traceback every tick buried everything else
(28 in 90 seconds with the broker down, in a test). This logs the first
failure in full, then one line at most every `interval` seconds while
it keeps failing, and says once when it works again.
"""
from __future__ import annotations

import logging
import time


class FailureLog:
    def __init__(self, logger: logging.Logger, interval: float = 600):
        self.logger = logger
        self.interval = interval
        self._last_logged: dict[str, float] = {}

    def failed(self, key: str, message: str, *args) -> None:
        """Call from inside the `except` block, so the first report keeps
        the traceback."""
        now = time.monotonic()
        last = self._last_logged.get(key)
        if last is None:
            self.logger.exception(message, *args)
        elif now - last >= self.interval:
            self.logger.warning(message + " - still failing", *args)
        else:
            return
        self._last_logged[key] = now

    def succeeded(self, key: str) -> None:
        if self._last_logged.pop(key, None) is not None:
            self.logger.info("%s is working again", key)
