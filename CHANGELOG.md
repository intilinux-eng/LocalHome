# Changelog

Notable changes to LocalHome. Dates are when the change landed on `main`;
this project doesn't cut versioned releases, so entries are grouped by
date rather than by tag.

## Unreleased

### Changed
- `/climate-admin`'s "Reporting" status now shows a battery sensor's
  reading age too, not just the value - and past 30 minutes, frames it
  explicitly as normal ("30m ago - normal, this sensor reports on a
  change or roughly every 2h") rather than leaving a technician to
  guess whether an aging timestamp means trouble. Confirmed against an
  overnight watch of the two real H&T Gen3 sensors governing this
  apartment's climate: every heartbeat landed within about a minute of
  exactly 2h, all night, regardless of individual firmware version or
  WiFi signal - the panel was previously the one making a perfectly
  healthy sensor look like it might be dying.
- The battery sensor `stale_after_seconds` default dropped from 6h to
  4h (just under double the confirmed 2h heartbeat) - a genuinely dead
  sensor is now caught after missing two heartbeats instead of three,
  with the same one-missed-heartbeat tolerance as before.

### Fixed
- The "Xs/Xm/Xh ago" freshness shown for every `mqtt_json` sensor,
  switch and number was computed from whenever `SyncPoller` last
  *polled* (`poll_interval_seconds`, typically 20s), not from when the
  underlying MQTT message actually arrived - since MQTT is push-based,
  re-reading the same cached payload every poll made an untouched
  reading look freshly updated every single cycle, even hours after a
  battery sensor's last real report. The dashboard could show "5s ago"
  on a value that was actually hours old (confirmed live: a real
  Shelly H&T's reported freshness went from always ~5-20s to the
  genuine ~4-5 minutes since its last report once fixed). `core/poller.py`
  now asks the driver for its own `last_update_ts` when it defines one
  (the three generic MQTT drivers do, from the cached message's actual
  arrival time - see `drivers/mqtt/base.py`'s `get_received_at()`) and
  only falls back to poll-time otherwise, so every non-MQTT driver
  (Tuya, Meross, simulated, ...) is unaffected.

### Added
- `availability_topic`/`available_payload` on the generic `mqtt_json`
  sensor/switch/number drivers - points at a device's own MQTT Last
  Will and Testament (most Shelly Gen2/Gen3 devices publish `<id>/online`)
  for immediate, precise offline detection instead of waiting for
  `stale_after_seconds` to infer it from silence. Overrides even a
  still-fresh regular reading; a switch that only publishes on state
  change (which may have no sensible `stale_after_seconds` to set at
  all) can now still be detected as offline. Optional, off by default -
  every existing config.yaml keeps behaving exactly as before. See
  [docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline](docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline).
- Every *valve/switch* the `/climate-admin` panel builds (both zones'
  valves, the two extra switches) now gets the `availability_topic`
  above for free, derived automatically as `<device id>/online` - no
  field to fill in, since every Shelly publishes this itself. See
  `services/climate_map_store.py`. Deliberately **not** wired up for
  sensors (the zones' own H&T Gen3s, the outdoor sensor) - tried it
  live against the two real ones governing this apartment's climate:
  in battery mode, an H&T only actually connects to the broker to
  publish a threshold/heartbeat report, so its "online" topic reads
  false almost the entire time it's working perfectly normally, not
  just when something's wrong. A mains-powered relay has no such
  legitimate "disconnected on purpose" state, so it stays a
  meaningful signal there.
- A **sensor failsafe** on `ThermostatController`: a zone whose sensor
  stays actually unreachable (not just quiet - past
  `stale_after_seconds`) for longer than `sensor_failsafe_hours`
  (default 24) now has its valve forced closed automatically instead of
  staying frozen in whatever it was last doing indefinitely - clears
  itself the moment the sensor reports again. Surfaced on
  `/climate-admin`'s zone cards as a countdown/warning, not a push
  notification.
- **Diagnostic mode**: a new system-wide override, toggled from its own
  `/climate-admin` section, that takes every valve (both zones' + the
  bathroom bypass + the dehumidifier) out of automatic control entirely
  - `ThermostatController` and `InterlockController` both go hands-off
  on every tick while it's on - and exposes each one as a direct manual
  on/off switch (reusing the existing generic switch route, including
  for a mode_switch like the bypass valve, normally blocked from manual
  control). For on-site verification (does this relay actually switch?
  which physical valve is which name?) without the thermostat undoing a
  manual test moments later. Turning it back off resumes normal,
  sensor-driven control immediately, no restart needed - including
  force-closing any zone whose sensor is currently unreachable
  (`ThermostatController.resync_after_diagnostic_mode()`), since
  `tick()` would otherwise just skip such a zone and leave it exactly
  where a manual test left it. The panel's manual quick-set value box
  (for a zone in "manual" sensor mode) now also rejects a value outright
  while diagnostic mode is on, rather than accepting one that would sit
  there with no visible effect - the thermostat's `tick()` is a no-op
  the whole time diagnostic mode is active. See
  [docs/configuration.md#diagnostic-mode](docs/configuration.md#diagnostic-mode).
- The `/climate-admin` panel gained a "How this works" reference
  section explaining every battery-sensor edge case in place (a stable
  reading's 2h heartbeat, weak WiFi, low battery, the failsafe above) -
  replacing an earlier plan to push these as Telegram notifications, so
  the explanation lives next to the state it's explaining instead of a
  separate channel that needs its own bot token.

### Fixed
- `PositionStore.set()` (a cover's persisted 0-100% position) read-
  modified-wrote `positions.json` without the lock the rest of the
  class already uses - two covers finishing their moves close enough
  together (each on its own timer) could race, and the slower one's
  save silently overwrote the faster one's just-written position (on
  Windows, `os.replace()`'s stricter concurrent-rename semantics could
  even raise instead of just losing data). Confirmed with 20 covers
  finishing at the same instant losing positions before the fix, none
  after.
- `MerossPlugDriver.async_setup()` leaked the HTTP session and
  persistent MQTT connection meross-iot opens whenever the account
  login succeeded but the configured `device_name`/`uuid` didn't match
  a real device (or `async_device_discovery()` itself failed) -
  `AsyncPoller` retries `async_setup()` every `poll_interval_seconds`
  until it succeeds, so a permanently-wrong config leaked one of each,
  forever. Now closed on any failure after creation.
- `ClimateMapStore.set_zones()`/`set_extra_switches()`/`set_extra_sensors()`
  validated cross-references (duplicate device ids across zones/extra
  switches/the extra sensor) against a snapshot read *outside* the
  lock, then wrote inside it - two near-simultaneous saves (e.g. two
  browser tabs on `/climate-admin`) could validate against an
  already-stale snapshot. Now read, validated, and written against the
  same snapshot in one lock acquisition.
- Removed `EweLinkCloud.get_hours_kwh()` and `WS_DISPATCH_HOSTS` (an
  undocumented websocket-based 31-day hourly-kWh history) - unused
  anywhere in this project since it was added.
- The climate admin panel's fixed outdoor-sensor role built its
  Integration as `"Temperatura Esterna"` - the only non-English fixed
  name in an otherwise English-named system (`EXTRA_SWITCH_FIELDS` uses
  "Bathroom Bypass Valve"/"Dehumidifier"). Renamed to `"Outdoor Sensor"`
  for consistency.
- A `mqtt_json` climate/power_meter sensor whose topics all went past
  `stale_after_seconds` (most commonly a battery sensor like a Shelly
  H&T between wake cycles, not actually broken hardware) used to
  discard its last known reading entirely and show a blank
  "unreachable" card. It now keeps showing the last known value,
  labeled "last known value - no update in Xh", on the main dashboard,
  a zone's card, and the climate admin panel's live status alike -
  still `"ok": False` under the hood, so the thermostat/interlock still
  correctly never acts on it as if it were fresh. See
  `drivers/mqtt/sensor.py`'s `read()` and
  [docs/integrations/mqtt.md](docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline).

### Added
- A third fixed, standalone role in the climate admin panel: an
  outdoor temperature/humidity sensor, always a real Shelly H&T Gen3
  (no simulated/manual stand-in, unlike a zone's own sensor - there's
  no thermostat decision riding on this one). Same "leave blank if not
  wired yet" shape as the two extra switches, with its own live status
  readout. See
  [docs/configuration.md#the-shelly-climate-admin-panel](docs/configuration.md#the-shelly-climate-admin-panel).

### Fixed
- The main dashboard's per-zone chart could silently keep polling a
  sensor/valve name that stopped existing - `climateZones` (which the
  chart's own refresh loop reads `.sensor`/`.valve` from) was only ever
  set once, at page load; a climate-admin save+restart that renamed a
  zone's underlying driver (e.g. switching its sensor to/from "manual")
  while the dashboard tab was already open left that tab's chart stuck
  querying the old name until a full page reload. `refreshClimateZonesLive()`
  (already polling every 5s for the live numbers) now keeps this array
  in sync too.

### Changed
- Redesigned the climate admin panel (`/climate-admin`): each zone's
  config form and its live status (sensor reading, valve state, manual
  test buttons) now live in one merged card instead of two separate
  page sections - swapping a device id and checking whether it's
  actually reporting no longer means scrolling back and forth. The
  heat/cool mode banner moved to the top of the page (it decides which
  direction every valve's "open"/"closed" below actually means) and now
  spells out the formula explicitly ("valves open below target, close
  above it"), not just the mode name. A zone with `cool_enabled: false`
  now gets an explicit "excluded from cooling" notice while the
  thermostat is in cool mode, instead of showing a bare, unexplained
  valve state. The MQTT broker connection and the two extra switches -
  one-time setup, not part of the day-to-day "swap a sensor" loop - are
  now collapsed behind native `<details>` disclosures instead of
  competing for attention with the zone cards.

### Fixed
- The climate admin panel's "Restart LocalHome" button now refuses
  cleanly on Windows (`services/self_restart.py`'s new `is_supported()`)
  instead of attempting a restart that reliably leaves two processes
  fighting over the same port there - confirmed happening live during
  development. The button's success path also now polls for the server
  actually coming back instead of blindly reloading after a fixed 4
  seconds, which previously looked identical to "stuck" whenever a
  restart took a little longer.
- `python-kasa` (the `tplink_led_strip` driver's SDK) is
  GPL-3.0-or-later, unlike every other dependency here, but was sitting
  in `pyproject.toml`'s main `dependencies` - so every install pulled
  in GPL-licensed code whether or not you own any TP-Link devices, and
  `core/registry.py`'s driver loader had no fallback if it were ever
  missing, so the whole app would fail to start rather than just that
  one driver. Moved to its own `[tplink]` extra
  (`pip install -e ".[tplink]"`), and `load_builtin_drivers()` now logs
  and skips a driver module whose own package isn't installed instead
  of aborting startup. See
  [THIRD_PARTY_NOTICES.md#a-note-on-python-kasa-specifically](THIRD_PARTY_NOTICES.md#a-note-on-python-kasa-specifically).
- Every route now falls back to a clean `{"ok": false, "error": ...}`
  JSON response (logged server-side) for any exception nobody
  anticipated, instead of Flask's default HTML error page - which broke
  every frontend `fetch().then(r => r.json())` call with a JSON parse
  error instead of a clear message. A normal 404/405 is unaffected.
- `meross_plug`'s setup/read calls are now wrapped in an explicit
  timeout - meross-iot's own HTTP client doesn't document one, so
  during an internet outage a hung request (rather than one that
  fails/times out on its own) could have stuck that poller's
  retry-forever loop waiting indefinitely instead of actually retrying
  every interval. Tuya (LAN), eWeLink and Telegram already bounded
  every network call the same way.

## 2026-09-16

### Added
- The Shelly climate admin panel (`/climate-admin`) - maps the
  thermostat's two zones onto a home's own Shelly device ids from the
  browser, instead of hand-editing `config.yaml`, for redeploying the
  exact same setup across multiple homes that standardize on the
  Shelly family. Per zone: a sensor source (a real Shelly H&T, a
  simulated one, or a manually-typed debug value) and a valve source
  (real or simulated) - simulated + manual together let the whole
  sensor-to-valve control loop be verified, including manual open/close
  test buttons, before any relay is wired. Also edits the MQTT broker
  connection and two more fixed Shelly Plus 1 switches (a bathroom
  bypass valve, a dehumidifier), and shows live per-zone status with
  plain-language guidance (a battery sensor that hasn't reported *yet*
  reads differently from one that's gone offline; a dead broker is
  never mistaken for every device failing at once) and a "Restart
  LocalHome" button. See
  [docs/configuration.md#the-shelly-climate-admin-panel](docs/configuration.md#the-shelly-climate-admin-panel).
- WiFi signal strength (`wifi_rssi_dbm`) on a zone's sensor card,
  alongside the existing battery indicator - useful for judging a
  battery sensor's placement.
- A zone's dew point while cooling (condensation risk context) and a
  low-battery popup (once per session) when a sensor's battery drops
  below 10%.
- A season indicator (☀/❄) replacing the static "Heating & Cooling"
  heading.
- The Arduino UNO Q, this project's own day-to-day deployment target,
  noted alongside the existing Raspberry Pi/generic-Linux-board
  mentions (README, `docs/deployment.md`).

### Fixed
- `localhome-cli doctor` now cross-checks the climate admin panel's own
  saved zones/extra switches too, via the same merge `web/app.py`'s
  `create_app()` applies - previously an apartment set up entirely
  through the panel had no `thermostat:` block in `config.yaml` at
  all, so `doctor` silently checked nothing about it.
- A failed MQTT publish (most concretely: sent before the client
  finished its initial connection) no longer looks identical to a
  successful one - every switch/number/cover command already had
  error handling ready for this at every layer (the thermostat and
  interlock control loops, the `/api/sensors/...` routes), it just
  never fired because `publish()` never raised.
- The climate admin panel's manual-value debug route checked the
  *saved* zone mapping instead of what the running process actually
  loaded - a save that hadn't taken effect yet (no restart) could
  apply a fabricated reading to whichever real driver still owned that
  zone's name.
- `python3 -m venv`/a bare `pip install` now gets a virtual environment
  first in Quick start and the bare-metal deployment guide - modern
  Debian/Ubuntu actively refuses the latter
  (`externally-managed-environment`) and often doesn't ship the venv
  module by default either, confirmed setting this project up for real
  on its own Arduino UNO Q instance (Debian trixie). See
  [docs/troubleshooting.md](docs/troubleshooting.md).

## 2026-09-14

### Added
- `stale_after_seconds` on the generic MQTT driver (`sensor`/`switch`/
  `number`) - since MQTT only pushes when a device decides to, without
  this a dead device's last message was cached and returned as a fine,
  current reading forever. Opt-in (default: never expires), since a
  switch that only publishes on state change can legitimately go quiet
  for a long time without being offline - see
  [docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline](docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline).
- A "last updated" age (e.g. "5m ago") on every sensor card, formatted
  as a human-friendly duration instead of raw seconds, and now also
  shown on each thermostat zone card (previously only on the generic
  Home-tab cards) alongside the sensor's own error message when it's
  unreachable.
- README screenshots (`docs/screenshots/`), captured against the
  `simulated` driver.
- A heating/cooling thermostat: a multi-zone valve control loop for a
  shared-plant radiant-panel setup (or anything similar) - a
  click-and-drag weekly setpoint schedule per zone, a shared heat/cool
  mode (behind a settings menu, persisted so it survives a restart), a
  live clock and "current hour" highlight on the schedule grid, and
  on-hours history (today/week/month) per zone. See
  [docs/configuration.md#thermostat-heatingcooling-zones](docs/configuration.md#thermostat-heatingcooling-zones).
- `Zone.cool_enabled` - a zone can opt out of one mode entirely (e.g. a
  bathroom whose radiant ceiling panel can't be cooled safely without
  risking condensation).
- `services/interlock.py` - a generic "switch A forces switch B to
  follow it" rule, for a device (e.g. a water-based dehumidifier) whose
  own operation depends on a specific valve regardless of that zone's
  own temperature control.
- Away mode: a one-tap "close every valve for N days" override, separate
  from the weekly schedule so nothing needs to remember to undo it - it
  resumes the normal schedule on its own once the period ends.
- `climate`/`switch` `simulated` driver - a fake sensor/switch (with a
  simple closed-loop model: an open valve nudges its paired sensor's
  reading toward a target) for trying out the dashboard, or building a
  whole thermostat setup, before owning any real hardware. See
  [docs/integrations/simulated.md](docs/integrations/simulated.md).
- `localhome-cli doctor` - cross-checks every zone/switch/sensor name
  `config.yaml` references against what's actually configured, catching
  typos that previously failed completely silently. See
  [docs/configuration.md#checking-a-new-setup-with-doctor](docs/configuration.md#checking-a-new-setup-with-doctor).
- `history.retention_days` config option (default 30) - rows older than
  this are pruned once a day, so the sqlite history database doesn't
  grow forever just because the process stays up for a long time.
- TP-Link Kasa/Tapo LED strip support (`tplink_led_strip`, `switch` +
  `number`), entirely over the LAN via
  [python-kasa](https://github.com/python-kasa/python-kasa) - on/off,
  read-only brightness on the switch card, and an optional brightness
  slider that renders inside the switch's own card via a new
  `paired_switch` config option. See
  [docs/integrations/tplink.md](docs/integrations/tplink.md) (includes
  the "Third-Party Compatibility" step some Tapo firmware needs, since
  python-kasa doesn't yet support its newer `TPAP` local-auth scheme).
- `tools/tplink_discover.py` to find TP-Link devices' IPs on the LAN.
- A startup log warning when two integration entries share a `name`
  (previously a silent card-swallowing bug, since `DeviceManager` keys
  pollers/sensor kinds by name across every kind).

### Fixed
- A switch/number command (turn on/off, set a slider value) now updates
  the dashboard's cached reading immediately on success, instead of
  waiting up to `poll_interval_seconds` for the next background poll.
  Previously the toggle/slider could visually snap back to its
  pre-command state for several seconds after a successful command.
- Schedule/mode persistence (`schedules.json`, `positions.json`) now
  writes atomically (temp file + fsync + rename) and falls back to
  defaults on a corrupt read instead of crashing on the next boot - a
  power loss mid-write could previously corrupt one of these files.
- `AsyncPoller` retries a failed `async_setup()` on the same interval
  instead of disabling that poller for the rest of the process's life -
  e.g. the MQTT broker or the network itself not being up yet when
  LocalHome starts used to be a permanent failure, not a transient one.
- `HistoryRecorder`'s background loop no longer dies silently on one
  failed database write (e.g. a transient SQLite lock) - caught and
  retried next interval, the same self-healing shape the thermostat and
  interlock loops already had.
- Editing a zone's schedule or the heat/cool mode now re-evaluates the
  affected valve(s) immediately instead of waiting up to
  `poll_interval_seconds` - previously the dashboard could briefly show
  a stale "target reached" status right after a change that should have
  opened the valve.
- The schedule grid's hour axis now lines up with the grid's actual
  columns (it previously used `justify-content: space-between` on a
  handful of labels, which visually looked like the schedule stopped at
  18:00 instead of running to 24:00).
- `tuya_air_quality` treats a status snapshot reporting both
  `temperature_c` and `humidity_pct` as exactly zero as a failed read
  instead of recording it - a rare but real stale/incomplete response
  from the device (indoor air can't physically be 0C and 0% RH at once),
  found auditing real collected history data. Previously showed up as a
  brief zero spike on the chart.
- The thermostat schedule now rejects a non-numeric hour value on save
  instead of persisting it - it would otherwise crash the entire
  `/api/climate/zones` endpoint (not just one zone) the moment that hour
  was reached. Also sanitizes any such value already on disk back to
  "no target" on read, so one bad legacy cell can't permanently block
  every future save of that zone (schedules always round-trip the whole
  week, not a diff). Found via automated UI/API stress-testing.

### Changed
- Each zone's temperature/humidity chart now overlays when its valve
  was on (a translucent band, plus the state in the hover tooltip),
  reusing the on/off history already recorded for the on-hours metric.
- The heat/cool mode indicator in the Climate tab is now a plain,
  non-interactive label next to the heading instead of a second button
  that duplicated the settings gear icon's job - the gear icon is the
  only place that actually changes it now.
- Dropped the covers "N of M open" header summary - redundant with each
  cover card's own state, and out of place while on the Climate tab.

## 2026-09-12
- Restructured the project into LocalHome, a generic, extensible
  local-first smart-home framework (plugin drivers, `core/interfaces.py`,
  `config/` split into real vs. example files, MIT license).
- Added English and Italian dashboard translations, switchable via
  `web.language` in config.yaml.

## 2026-09-09
- Added Meross smartplug control, Telegram power alerts, kWh history and
  shutter external-move tracking.
- Added the home panel to control LoraTap shutters and monitor power via
  a Sonoff POWCT relay.
