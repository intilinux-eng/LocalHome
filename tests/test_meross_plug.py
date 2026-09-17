"""async_setup()'s cleanup-on-failure path - see drivers/meross/plug.py.
Everything else in this driver is a thin pass-through to meross-iot
(no logic worth unit-testing against a real account/device), but this
one path is worth locking down: AsyncPoller retries async_setup() every
poll_interval_seconds until it succeeds, so a leaked HTTP session/MQTT
connection on every failed attempt (a wrong device_name, a transient
async_init()/async_device_discovery() failure) would accumulate
forever - see core/poller.py.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from localhome.drivers.meross.plug import MerossPlugDriver


def _make_driver() -> MerossPlugDriver:
    return MerossPlugDriver(
        name="Desk Plug",
        device_name="Desk Plug",
        uuid=None,
        credentials={"email": "a@b.com", "password": "secret"},
    )


def _patch_meross(monkeypatch, manager: MagicMock, http_client: MagicMock):
    monkeypatch.setattr(
        "localhome.drivers.meross.plug.MerossHttpClient.async_from_user_password",
        AsyncMock(return_value=http_client),
    )
    monkeypatch.setattr("localhome.drivers.meross.plug.MerossManager", MagicMock(return_value=manager))


def test_async_setup_closes_the_connection_when_the_device_is_not_found(monkeypatch):
    manager = MagicMock()
    manager.async_init = AsyncMock()
    manager.async_device_discovery = AsyncMock()
    manager.find_devices = MagicMock(return_value=[])  # not found
    manager.close = MagicMock()
    http_client = MagicMock()
    http_client.async_logout = AsyncMock()
    _patch_meross(monkeypatch, manager, http_client)

    driver = _make_driver()
    with pytest.raises(RuntimeError, match="not found on Meross account"):
        asyncio.run(driver.async_setup())

    manager.close.assert_called_once()
    http_client.async_logout.assert_awaited_once()


def test_async_setup_closes_the_connection_when_discovery_itself_fails(monkeypatch):
    manager = MagicMock()
    manager.async_init = AsyncMock()
    manager.async_device_discovery = AsyncMock(side_effect=RuntimeError("network error"))
    manager.close = MagicMock()
    http_client = MagicMock()
    http_client.async_logout = AsyncMock()
    _patch_meross(monkeypatch, manager, http_client)

    driver = _make_driver()
    with pytest.raises(RuntimeError, match="network error"):
        asyncio.run(driver.async_setup())

    manager.close.assert_called_once()
    http_client.async_logout.assert_awaited_once()


def test_async_setup_does_not_close_the_connection_on_success(monkeypatch):
    plug = MagicMock()
    manager = MagicMock()
    manager.async_init = AsyncMock()
    manager.async_device_discovery = AsyncMock()
    manager.find_devices = MagicMock(return_value=[plug])
    manager.close = MagicMock()
    http_client = MagicMock()
    http_client.async_logout = AsyncMock()
    _patch_meross(monkeypatch, manager, http_client)

    driver = _make_driver()
    asyncio.run(driver.async_setup())

    assert driver._plug is plug
    manager.close.assert_not_called()
    http_client.async_logout.assert_not_awaited()
