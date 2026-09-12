"""Builds the running set of drivers described by config.yaml and starts
their background polling. This is the one place that turns "a list of
integration entries" into "live driver objects the web app and CLI can
use" - see docs/adding-a-driver.md for how a new driver plugs into it.
"""
from __future__ import annotations

import logging

from localhome.config import LocalHomeConfig
from localhome.core.interfaces import CoverDriver, NumberDriver, Notifier, SwitchDriver
from localhome.core.poller import AsyncPoller, SyncPoller
from localhome.core.registry import create

logger = logging.getLogger(__name__)

# Option keys that hold a path relative to config.yaml's own directory,
# resolved before being handed to a driver factory. `broker_file` is the
# MQTT drivers' shared-connection-details file (see drivers/mqtt/client.py).
_PATH_OPTION_KEYS = ("devices_file", "secrets_file", "broker_file")


def _resolve_paths(config: LocalHomeConfig, options: dict) -> dict:
    resolved = dict(options)
    for key in _PATH_OPTION_KEYS:
        if key in resolved:
            resolved[key] = config.resolve(resolved[key])
    return resolved


class DeviceManager:
    def __init__(self, config: LocalHomeConfig):
        self.config = config
        self.covers: dict[str, CoverDriver] = {}
        self.switches: dict[str, SwitchDriver] = {}
        self.numbers: dict[str, NumberDriver] = {}
        self.pollers: dict[str, "SyncPoller | AsyncPoller"] = {}
        self.sensor_kinds: dict[str, str] = {}
        # number-sensor name -> switch name, for a `number` integration entry
        # that sets `paired_switch` - lets the dashboard show its slider
        # inside that switch's own card instead of as a separate one.
        self.paired_switch: dict[str, str] = {}
        self.notifier: Notifier | None = None
        self._build()

    def _build(self) -> None:
        for entry in self.config.integrations:
            options = _resolve_paths(self.config, entry.options)
            try:
                drivers = create(entry.kind, entry.type, options)
            except Exception:
                logger.exception("Failed to create %s/%s driver, skipping it", entry.kind, entry.type)
                continue
            for driver in drivers:
                self._register(entry.kind, driver)
                if entry.kind == "number" and options.get("paired_switch"):
                    self.paired_switch[driver.name] = options["paired_switch"]

        if self.config.notifier:
            options = _resolve_paths(self.config, self.config.notifier.options)
            try:
                (self.notifier,) = create("notifier", self.config.notifier.type, options)
            except Exception:
                logger.exception("Failed to create notifier, alerts will be silently dropped")

    def _register(self, kind: str, driver) -> None:
        if driver.name in self.sensor_kinds or driver.name in self.covers:
            logger.warning(
                "Integration name %r is used by more than one entry - only the last one "
                "registered will be reachable (pollers/sensor_kinds are keyed by name across "
                "every kind). Give each entry its own name.",
                driver.name,
            )
        if kind == "cover":
            self.covers[driver.name] = driver
            return
        if kind == "switch":
            self.switches[driver.name] = driver
        elif kind == "number":
            self.numbers[driver.name] = driver
        poller = AsyncPoller(driver) if getattr(driver, "is_async", False) else SyncPoller(driver)
        self.pollers[driver.name] = poller
        self.sensor_kinds[driver.name] = kind

    def start(self) -> None:
        for poller in self.pollers.values():
            poller.start()

    def sensor_reading(self, name: str) -> dict:
        poller = self.pollers.get(name)
        if poller is None:
            return {"ok": False, "error": f"unknown sensor '{name}'"}
        return poller.get()

    def notify(self, text: str) -> bool:
        if self.notifier is None:
            return False
        return self.notifier.send(text)
