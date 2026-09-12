"""Abstract interfaces every driver implements.

A "driver" wraps one physical device, or one device model queried through
a brand's API, and exposes it through one of the small interfaces below.
The rest of the application - the web dashboard, the CLI, the history
service - only ever talks to these interfaces, never to a specific
brand's SDK. That is what makes adding support for a new device a
self-contained change: see docs/adding-a-driver.md.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CoverDriver(ABC):
    """A motorized cover: roller shutter, blind, awning, garage door..."""

    id: str
    name: str

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """Return at least {"state": "open"|"close"|"stop"|"unknown"}.

        Values follow the raw motor-command vocabulary most cheap curtain
        switches use, since that is the lowest common denominator; a
        driver for a device that reports a real position can still emit
        these three plus "unknown" for anything else.
        """

    @abstractmethod
    def send(self, action: str) -> None:
        """Send one of "open", "close", "stop" to the device."""

    def read_travel_time(self, default: float = 15.0) -> float:
        """Seconds for a full open<->close traversal, used by
        services.position_control to estimate intermediate positions on
        devices that don't report one. Override if the device knows its
        own travel time (most Tuya curtain switches do)."""
        return default


class PollingDriver(ABC):
    """Base for anything read on a fixed interval rather than commanded:
    power meters, climate/air-quality sensors, and (via SwitchDriver)
    smart plugs that also report their own power draw.

    Implement exactly one of the two read paths:

    - `read()` for anything synchronous (most LAN devices: a blocking
      socket call is fine on its own background thread).
    - `is_async = True` plus `async_setup()` / `async_read()` for cloud
      SDKs that need a persistent login/session/MQTT connection kept
      alive across polls inside one asyncio event loop.

    Optionally set `history_fields` to the numeric keys of your read()
    dict that are worth recording as a time series (e.g. `("power_w",)`).
    """

    name: str
    poll_interval_seconds: float = 20.0
    is_async: bool = False
    history_fields: tuple[str, ...] = ()

    def read(self) -> dict[str, Any]:
        raise NotImplementedError

    async def async_setup(self) -> None:
        return None

    async def async_read(self) -> dict[str, Any]:
        raise NotImplementedError


class SwitchDriver(PollingDriver):
    """A controllable on/off device that also reports its own state."""

    @abstractmethod
    def turn_on(self) -> None: ...

    @abstractmethod
    def turn_off(self) -> None: ...


class NumberDriver(PollingDriver):
    """A controllable analog value that also reports its own state: a
    dimmer, a fan speed, a thermostat setpoint - anything set to a number
    within a range rather than toggled or driven open/closed. `read()`/
    `async_read()` must include a numeric `"value"` key.

    This is the interface a future Shelly (or any other brand's) 0-10V
    dimmer driver would implement - see drivers/mqtt/number.py for a
    ready-to-use MQTT-backed one."""

    min_value: float = 0.0
    max_value: float = 100.0
    unit: str = "%"

    @abstractmethod
    def set_value(self, value: float) -> None: ...


class Notifier(ABC):
    """A push-notification channel used for alerts (see services/history.py)."""

    @abstractmethod
    def send(self, text: str) -> bool: ...
