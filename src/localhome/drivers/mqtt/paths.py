"""Dot-path lookup into a parsed MQTT payload, e.g. `"ENERGY.Power"` into
`{"ENERGY": {"Power": 12.3}}`. Shared by every generic MQTT driver so
brand-specific payload shapes (Tasmota's nested ENERGY block,
Zigbee2MQTT's flat per-device JSON, Shelly's per-component status
topics, or a plain non-JSON string some devices publish instead) are
handled by configuration, not code.

An empty path ("") means "the whole payload, as received" - the
convention a plain-string (non-JSON) payload needs, since there's no key
to descend into (see e.g. Shelly Gen1's bare "on"/"off").
"""
from __future__ import annotations

from typing import Any


def extract_path(payload: Any, path: str) -> Any:
    if path == "":
        return payload
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value
