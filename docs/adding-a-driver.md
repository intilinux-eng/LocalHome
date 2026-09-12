# Adding a driver

A driver is a plain Python module that wraps one device family behind one
of the interfaces in
[`core/interfaces.py`](../src/localhome/core/interfaces.py). This walks
through adding a new `switch` driver end to end - the same shape applies
to `cover`, `climate`/`power_meter` (a `PollingDriver` like `switch`,
minus the on/off methods), `number` and `notifier`.

Before writing a brand-specific driver, check whether
[docs/integrations/mqtt.md](integrations/mqtt.md) already covers your
device - if it publishes JSON (or even a plain string) over MQTT, the
generic `mqtt_json` driver probably needs zero new code, just a
config.yaml entry.

## 1. Pick the interface

| You're adding...                                   | Implement       |
|------------------------------------------------------|-----------------|
| A motorized cover (shutter, blind, garage door)       | `CoverDriver`   |
| A sensor read on an interval (power, climate, ...)    | `PollingDriver` |
| An on/off device that also reports its state          | `SwitchDriver`  |
| A dimmer/fan-speed/setpoint-style analog value         | `NumberDriver`  |
| A push-notification channel                           | `Notifier`      |

## 2. Decide sync or async

- If talking to the device is a simple blocking call (almost anything on
  your LAN), implement `read()` and leave `is_async` at its default
  (`False`). `core/poller.SyncPoller` will call it on a background
  thread every `poll_interval_seconds`.
- If it's a cloud SDK that needs a persistent login/session/connection
  kept alive across polls (and possibly used to *send* commands between
  polls, like MQTT-based control), set `is_async = True` and implement
  `async_setup()` (once) and `async_read()` (every interval) instead.
  `core/poller.AsyncPoller` runs both inside one asyncio event loop on a
  background thread.

## 3. Write the module

```python
# src/localhome/drivers/acme/plug.py
"""Driver for an Acme SmartPlug via its (hypothetical) local HTTP API."""
from __future__ import annotations

from typing import Any

import requests

from localhome.core.interfaces import SwitchDriver
from localhome.core.registry import register_driver


class AcmePlugDriver(SwitchDriver):
    history_fields = ("power_w",)  # recorded to the history db automatically

    def __init__(self, name: str, host: str, poll_interval_seconds: float = 20.0):
        self.name = name
        self._host = host
        self.poll_interval_seconds = poll_interval_seconds

    def read(self) -> dict[str, Any]:
        resp = requests.get(f"http://{self._host}/status", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {"ok": True, "name": self.name, "is_on": data["on"], "power_w": data["watts"]}

    def turn_on(self) -> None:
        requests.post(f"http://{self._host}/on", timeout=5).raise_for_status()

    def turn_off(self) -> None:
        requests.post(f"http://{self._host}/off", timeout=5).raise_for_status()


@register_driver("switch", "acme_plug")
def _create(options: dict) -> AcmePlugDriver:
    return AcmePlugDriver(
        name=options.get("name", "Acme Plug"),
        host=options["host"],
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
    )
```

A few things to notice:

- **Raising is enough.** `read()`/`async_read()` can just raise on
  failure - `SyncPoller`/`AsyncPoller` catch it and turn it into
  `{"ok": False, "error": str(exc)}` for you. Don't wrap every call in
  try/except yourself.
- **`history_fields` is opt-in.** Only list keys that are numeric and
  worth graphing; the history service records exactly those, nothing
  else, with no schema change required elsewhere.
- **The factory reads `options`, a plain dict from that config.yaml
  entry** (everything except `kind`/`type`). `devices_file`/
  `secrets_file`, if you use those names, get resolved to absolute paths
  by `DeviceManager` before your factory sees them - see
  `core/manager.py`'s `_PATH_OPTION_KEYS` if you need a third path-like
  option name added there.
- You never touch `core/registry.py`, `core/manager.py` or `web/app.py`.
  `register_driver` plus `load_builtin_drivers()`'s package walk is the
  entire integration point.

## 4. Reference it from config.yaml

```yaml
integrations:
  - kind: switch
    type: acme_plug
    name: "Garage Plug"
    host: "192.0.2.50"
```

## 5. Make it show up correctly on the dashboard

The frontend (`web/static/app.js`) renders a card per `kind`
(`power_meter` / `climate` / `switch` / `number`) using a fixed set of
known field names (`FIELD_META`). If your driver's `read()` dict uses
fields the existing card already understands (`power_w`, `voltage_v`,
`is_on`, ...), you get a working card for free. A field it doesn't
recognize still shows up, just with its raw key as the label - add it to
`FIELD_META` for a nicer label/unit/precision.

Adding a genuinely new *kind* (not cover/power_meter/climate/switch/number)
- say, a binary door sensor - needs: a new value flowing through
`DeviceManager.sensor_kinds`, a small render branch in
`updateSensorCard()` in `app.js`, and probably a new interface in
`core/interfaces.py` if the existing ones don't fit. That's a bigger
change; open an issue to discuss the shape first if you're not sure.

## If several of your driver's devices share one connection

Most drivers own their connection outright. If yours doesn't - several
devices talking through one hub, gateway, or broker, the way every MQTT
device shares one broker connection - don't open one connection per
driver instance. Follow `drivers/mqtt/client.py`'s pattern instead: a
small module-level pool keyed by whatever identifies the shared resource
(host+port+account, a hub's serial number, ...), handed out by a
`get_connection(...)`-style function so multiple driver instances
transparently reuse one real connection.

## 6. Write the setup doc

The hardest part of most of these drivers isn't the code, it's finding
the device's ID/key/credentials in the first place. Add
`docs/integrations/acme.md` covering that - see the existing ones for the
level of detail expected (`docs/integrations/tuya.md` in particular,
which documents a genuinely fiddly cloud-project setup step by step).

## 7. Add a test if there's non-trivial logic

If your driver is a thin pass-through to a vendor SDK, there's nothing
to unit test beyond "does it construct" - skip it. If it has any actual
logic (parsing, unit conversion, state machines like
`services/position_control.py`), add a test that exercises it against a
fake/stub, not the real device. See `tests/` for the existing pattern.
