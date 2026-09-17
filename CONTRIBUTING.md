# Contributing

This project stays useful only if it stays small and readable, so the bar
for a change is less "is it correct" and more "does it still fit in one
sitting after this."

## Adding a driver

By far the most valuable contribution. Read
[docs/adding-a-driver.md](docs/adding-a-driver.md) first - it walks
through the exact interface to implement. In short:

1. New module under `src/localhome/drivers/<your_brand>/`.
2. Implement `CoverDriver`, `PollingDriver`, `SwitchDriver` or `Notifier`
   from `core/interfaces.py`.
3. Register a factory with `@register_driver(kind, "your_type_name")`.
4. Add a `docs/integrations/<your_brand>.md` explaining how to get the
   device's IDs/keys/credentials - this is usually the part that took
   you the longest, and the part most worth writing down.
5. If it's a `power_meter`, `climate` or `switch` that reports fields the
   dashboard doesn't already know how to label, add them to `FIELD_META`
   in `web/static/app.js`, and add the label under `fields` in
   `web/locales/en.json` (and any other locale file you can translate it
   into - see docs/configuration.md#language) rather than hardcoding the
   English text in `FIELD_META` itself.
6. A test under `tests/` for anything that isn't a thin wrapper around a
   vendor SDK call (position math, parsing, registry wiring - not "does
   the real device respond").

## Ground rules

- **No cloud dependency unless the device genuinely has no local
  protocol.** If you're adding a driver for a device with a documented
  local API, use it, even if the cloud API is easier - that's the entire
  point of this project. If the device truly requires the cloud, say so
  explicitly in its integration doc, the way `docs/integrations/ewelink.md`
  and `docs/integrations/meross.md` do.
- **No new required dependencies for existing drivers.** A new driver
  can add its own dependency, but it goes in its own
  `[project.optional-dependencies]` extra in `pyproject.toml`, not the
  main `dependencies` list - `core/registry.py`'s driver loader already
  tolerates that extra not being installed (logs it, skips that one
  driver, starts everything else normally), so nothing every user needs
  is pip-installed automatically for a device only some users own.
- **Check a new dependency's license before adding it**, the same way
  you'd check a device's local-vs-cloud protocol. This project stays
  under permissive licenses (see
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)) on purpose - a
  copyleft one (GPL, AGPL, ...) isn't automatically disqualifying, but
  it needs its own extra (per the rule above, doubly so here) and an
  explicit note in THIRD_PARTY_NOTICES.md, not a silent addition to the
  table as if it were just another MIT/BSD package. `python-kasa`
  (GPL-3.0-or-later) is the one existing example of this - see
  [THIRD_PARTY_NOTICES.md#a-note-on-python-kasa-specifically](THIRD_PARTY_NOTICES.md#a-note-on-python-kasa-specifically).
- **Never commit real device data.** `devices.json`, `secrets*.json`,
  `config.yaml` and anything else under `config/` other than the
  `*.example.*` files are gitignored on purpose - keep it that way, and
  double-check `git status`/`git diff` before committing anything that
  touches `config/`.
- **Keep comments to the "why", not the "what".** Several existing
  drivers carry comments explaining a specific vendor quirk that took
  real debugging to discover (see e.g. `drivers/ewelink/power.py`) -
  that kind of comment is exactly the point; a comment restating what the
  next line of code already says is not.

## Developing without the real hardware

Don't own the device you're building a feature or driver against yet (or
building something, like the thermostat, that spans several devices)?
Point it at the `climate`/`switch` `simulated` driver instead - see
[docs/integrations/simulated.md](docs/integrations/simulated.md) - and
`localhome-cli doctor` to catch a typo'd zone/sensor/switch name in
config.yaml before it fails silently. Both are exactly how the thermostat
and its scheduling UI were built and tested end to end before any real
valve/sensor was involved.

## Running tests

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

(Same venv from the [README](README.md)'s Quick start works fine here
too - just add `[dev]` to the `pip install -e .` you already ran.)

Tests must not require real hardware or real cloud credentials - mock or
construct fake driver instances instead (see `tests/` for examples).
