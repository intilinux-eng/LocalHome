"""Shared helpers for Tuya LAN drivers (tinytuya-based).

Every Tuya driver reads the same devices.json produced by
`python -m tinytuya wizard` (optionally merged with IPs found by
`python -m tinytuya scan`), and filters it by product category - see
docs/integrations/tuya.md.
"""
from __future__ import annotations

import json

import tinytuya


def load_devices(devices_file: str, category: str | None = None) -> list[dict]:
    with open(devices_file, encoding="utf-8") as f:
        devices = json.load(f)
    if category is not None:
        devices = [d for d in devices if d.get("category") == category]
    return [d for d in devices if d.get("ip")]


def make_device(entry: dict, version: float = 3.4, socket_timeout: float = 5.0) -> tinytuya.Device:
    device = tinytuya.Device(entry["id"], entry["ip"], entry["key"], version=version)
    device.set_socketTimeout(socket_timeout)
    return device
