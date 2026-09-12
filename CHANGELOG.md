# Changelog

Notable changes to LocalHome. Dates are when the change landed on `main`;
this project doesn't cut versioned releases, so entries are grouped by
date rather than by tag.

## Unreleased

### Added
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
