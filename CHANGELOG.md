# Changelog

Notable changes to LocalHome. Dates are when the change landed on `main`;
this project doesn't cut versioned releases, so entries are grouped by
date rather than by tag.

## Unreleased

### Added
- `stale_after_seconds` on the generic MQTT driver (`sensor`/`switch`/
  `number`) - since MQTT only pushes when a device decides to, without
  this a dead device's last message was cached and returned as a fine,
  current reading forever. Opt-in (default: never expires), since a
  switch that only publishes on state change can legitimately go quiet
  for a long time without being offline - see
  [docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline](docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline).

## 2026-09-14

### Added
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
