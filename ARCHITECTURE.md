# Architecture

The goal is that adding a new device - a new brand of power meter, a
different curtain-motor protocol, a whole new kind of sensor - touches as
few files as possible, and ideally none outside `drivers/`. This document
explains the pieces that make that true.

## The device kinds

Everything the app knows about a physical device is expressed through one
of the small interfaces in [`core/interfaces.py`](src/localhome/core/interfaces.py):

- **`CoverDriver`** - something you open/close/stop and optionally poll for
  state. Roller shutters, blinds, garage doors.
- **`PollingDriver`** - something read on an interval: power meters,
  climate/air-quality sensors. The base for the other two polling kinds:
- **`SwitchDriver`** - a `PollingDriver` that's also commandable on/off
  (a smart plug is a switch that happens to also report its power draw).
- **`NumberDriver`** - a `PollingDriver` that's commandable to a value in
  a range instead of on/off: a dimmer, a fan speed, a 0-10V output.
- **`Notifier`** - a push-notification channel.

The web dashboard, the CLI and the history service only ever call methods
on these interfaces. None of them import `tinytuya`, `meross_iot`, or any
other brand SDK directly - that's confined to `drivers/`.

## Registry: config.yaml entries become driver objects

Each driver module registers a factory function against a `(kind, type)`
key with `@register_driver(...)` (see [`core/registry.py`](src/localhome/core/registry.py)):

```python
@register_driver("cover", "tuya_curtain_switch")
def _create(options: dict) -> list[TuyaCoverDriver]:
    ...
```

`core/registry.load_builtin_drivers()` walks the `drivers/` package with
`pkgutil` and imports every module it finds, so those decorators run
without anything needing to maintain a central import list. Drop a new
module under `drivers/`, decorate a factory, reference its `type` string
from `config.yaml` - that's the entire integration point.

A factory can return one driver or a list. Tuya's cover/climate factories
return a list because one `devices.json` (a tinytuya wizard dump) commonly
holds several physical devices of the same kind; cloud-backed drivers
(eWeLink, Meross) return a single instance per config entry since each one
names one specific device by ID/name.

## Manager: wiring config to running drivers

[`core/manager.py`](src/localhome/core/manager.py)'s `DeviceManager` reads
`LocalHomeConfig.integrations`, asks the registry to build each entry's
driver(s), and sorts the results into `covers` / `switches` / `numbers` /
`pollers` dicts keyed by device name. A driver that fails to construct
(bad credentials, missing file) is logged and skipped rather than
crashing the whole app - one broken integration shouldn't take down your
shutters.

## Polling: one generic background thread pattern, not three

The original version of this project (and most small hobby integrations)
end up hand-rolling the same "background thread + lock + latest-reading
cache" pattern once per device type. [`core/poller.py`](src/localhome/core/poller.py)
factors that out into two reusable classes:

- **`SyncPoller`** - runs `driver.read()` on a plain background thread.
  Fits any driver with a blocking call (a LAN socket read is fine on its
  own thread).
- **`AsyncPoller`** - runs its own asyncio event loop in a background
  thread: `driver.async_setup()` once, then `driver.async_read()` every
  interval. Fits cloud SDKs that need a persistent login/session/MQTT
  connection kept alive across polls (Meross in particular needs that
  same connection to *send* on/off commands between polls).

`DeviceManager` picks the right one per driver based on its `is_async`
class attribute. Either way, the rest of the app calls `.get()` and gets
back a plain dict with an `"ok"` key plus `seconds_since_update` - it
never needs to know which flavor of poller is underneath.

## A driver family that shares a resource: MQTT

Most drivers own their connection outright (one Tuya device, one eWeLink
login). MQTT is different: several devices commonly share one broker, and
opening a separate TCP/MQTT session per device would be wasteful and
would make every driver re-implement reconnect/resubscribe logic.
[`drivers/mqtt/client.py`](src/localhome/drivers/mqtt/client.py) solves
this with a small connection pool - `get_connection(broker)` returns the
same `MqttConnection` for an identical (host, port, username), so N
`mqtt_json` integrations pointed at the same `broker_file` share one real
connection; [`drivers/mqtt/base.py`](src/localhome/drivers/mqtt/base.py)'s
`MqttJsonState` then gives each driver instance its own
subscribe-and-cache-the-latest-payload view over that shared connection.
The four `mqtt_json` driver modules (`sensor.py`/`switch.py`/`number.py`/
`cover.py`) are thin: each just picks which interface to implement and
which JSON path(s) matter, all four via the same `extract_path()`
dot-path convention (see [`drivers/mqtt/paths.py`](src/localhome/drivers/mqtt/paths.py)
and [docs/integrations/mqtt.md](docs/integrations/mqtt.md)) - which is
also why *one* driver covers Shelly, Tasmota, Zigbee2MQTT and anything
else that speaks MQTT+JSON, instead of one driver per brand.

## Services: logic that's generic across drivers

- **`services/position_control.py`** estimates a 0-100% position for any
  `CoverDriver` whose device only reports a raw motor command, by timing
  how long the motor has run relative to its own reported travel time.
  This isn't Tuya-specific - any future cover driver with the same
  limitation reuses it for free.
- **`services/history.py`** records whatever numeric fields a
  `PollingDriver` declares in `history_fields` into one sqlite table
  (`sensor`, `field`, `ts`, `value`), so a new kind of sensor never needs
  a schema migration - it just starts producing new rows. It also runs
  the optional power-budget alert (a three-zone tolerance model: safe
  indefinitely / tolerated briefly / trips soon) against one named
  `power_meter` sensor.

## Web layer

[`web/app.py`](src/localhome/web/app.py) exposes a small, kind-generic
REST API (`/api/covers/...`, `/api/sensors/...`) and never imports a
specific driver. The frontend ([`web/static/app.js`](src/localhome/web/static/app.js))
asks `/api/devices` what exists and renders one card per sensor based on
its `kind` (`power_meter` / `climate` / `switch` / `number`) - so a
second power meter from a different brand just shows up as another card,
with zero frontend changes, as long as it reports fields the existing
card renderer already understands (`power_w`, `voltage_v`, ...). A
genuinely new *kind* of device needs a new card renderer in `app.js` -
see [docs/adding-a-driver.md](docs/adding-a-driver.md).

Authentication ([`web/auth.py`](src/localhome/web/auth.py)) is a single
`before_request` hook wired in by `create_app()` only if `web.auth` is
set in config.yaml - off by default, so it adds no code path at all for
the common "trusted LAN" case.

UI text is never hardcoded in English in the templates or in `app.js`:
[`web/i18n.py`](src/localhome/web/i18n.py) loads one JSON file from
`web/locales/<web.language>.json` and `create_app()` hands it to
`index.html` as `t`, which both renders it server-side (`{{ t.covers.heading }}`)
and dumps it into `window.I18N` for `app.js`'s `tr("some.key", vars)`
helper to read client-side. Adding a language is only ever "add a JSON
file with the same keys" - no template or JS change - see
[docs/configuration.md#language](docs/configuration.md#language).

## What this isn't

There's no dependency injection framework, no plugin manifest format, no
dynamic pip-installable driver packages. Drivers are plain Python modules
in-tree, discovered by walking one package. That's a deliberate ceiling:
this project is meant to stay small enough to read in one sitting. If you
need hot-loading third-party driver packages, a rules engine, or a mobile
app, you want a bigger platform than this one.
