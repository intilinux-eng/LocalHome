# Configuration reference

LocalHome reads one file, `config/config.yaml`, plus whatever
`devices_file`/`secrets_file` paths it points at. Start from
`config/config.example.yaml`:

```bash
cp config/config.example.yaml config/config.yaml
```

`config.yaml` and everything else you add under `config/` except the
`*.example.*` files are gitignored - see [.gitignore](../.gitignore).

## Path resolution

**Every relative path in config.yaml (`devices_file`, `secrets_file`,
`broker_file`, `positions_file`, `history.db_file`, `web.auth.secrets_file`)
is resolved against config.yaml's own directory** - i.e. against
`config/` itself, not the repo root and not your current working
directory. If `config/devices.json` sits right next to
`config/config.yaml`, write `devices_file: "devices.json"`, not
`"config/devices.json"`.

This means the app behaves identically whether you start it as
`python run.py` from the repo root, as `localhome` from anywhere after an
editable install, or from a systemd unit with a different working
directory.

Point at a config file somewhere else entirely (a second home, a test
fixture) with the `LOCALHOME_CONFIG` environment variable, which takes
precedence over `config/config.yaml` for both the web dashboard and
`localhome-cli`. `localhome-cli` additionally accepts a `--config` flag
for the same purpose (`web/app.py`'s entry point doesn't parse any CLI
arguments, so `--config` only works for the CLI, not `python run.py` /
`localhome`).

## Top-level keys

```yaml
web:
  host: "0.0.0.0"    # default: 0.0.0.0
  port: 5000          # default: 5000
  language: "en"       # optional, default: "en" - see "Language" below
  auth:                # optional, off by default - see "Web auth" below
    secrets_file: "secrets_web.json"

integrations: [...]   # see below

notifications:        # optional
  type: telegram
  secrets_file: "secrets_telegram.json"

energy:                # optional, power-budget alerting
  sensor: "Home Power Meter"   # must match an integration's `name`
  contract_limit_w: 3000

positions_file: "positions.json"   # default shown
history:
  db_file: "data/history.db"        # default shown
  poll_interval_seconds: 30          # default shown
  retention_days: 30                 # default shown - rows older than this are pruned once a day
```

## `integrations`

A list of driver instances to start. Every entry needs `kind` and `type`;
everything else is passed straight to that driver's factory function, so
the accepted keys depend on the driver - see
[docs/integrations/](integrations/) for each one's specifics. Two keys
are common across most of them:

- `name` - how the device shows up in the dashboard/API/CLI. Cloud-backed
  drivers (eWeLink, Meross) default to the name in their `devices_file`
  if you omit it; Tuya drivers always use the `name` field already
  present in each `devices.json` entry (one config entry fans out into
  several named covers/sensors).
- `devices_file` / `secrets_file` - paths to that driver's device
  inventory and/or credentials, resolved as described above.

Known `kind`/`type` combinations (run `python -c "from localhome.core.registry import known_drivers; print(known_drivers())"`
after installing to list what your checkout actually has registered):

| kind          | type                  | Docs |
|---------------|------------------------|------|
| `cover`       | `tuya_curtain_switch` | [integrations/tuya.md](integrations/tuya.md) |
| `climate`     | `tuya_air_quality`    | [integrations/tuya.md](integrations/tuya.md) |
| `power_meter` | `ewelink_powct`       | [integrations/ewelink.md](integrations/ewelink.md) |
| `switch`      | `meross_plug`         | [integrations/meross.md](integrations/meross.md) |
| `switch`/`number` | `tplink_led_strip`| [integrations/tplink.md](integrations/tplink.md) |
| `cover`/`climate`/`power_meter`/`switch`/`number` | `mqtt_json` | [integrations/mqtt.md](integrations/mqtt.md) - generic, covers Shelly/Tasmota/Zigbee2MQTT/ESPHome/etc. |
| `climate`/`switch` | `simulated`      | [integrations/simulated.md](integrations/simulated.md) - no hardware required |
| `notifier`    | `telegram`            | [integrations/telegram.md](integrations/telegram.md) |

`number` is a controllable analog value (a dimmer, a fan speed, a 0-10V
output) - see `core/interfaces.py`'s `NumberDriver`. It's currently only
implemented by the generic MQTT driver; nothing stops a future
brand-specific one from implementing it too.

Any integration entry can also set `dashboard_tab: "climate"` to move it
off the generic Home grid and into the dashboard's Heating & Cooling tab
instead (alongside any thermostat zone cards) - useful for something like
a dehumidifier switch that conceptually belongs with climate control but
isn't itself part of a zone. Defaults to the Home tab if omitted.

Combine that with `visible_in_mode: "heat"` or `"cool"` to also hide its
card whenever the thermostat isn't in that mode - e.g. a dehumidifier
that only makes sense while cooling. This only affects visibility, not
behavior: the device keeps running (and recording history) regardless,
it just doesn't clutter the tab in the season it isn't relevant.

## `energy` (power-budget alerting)

If set, `energy.sensor` must name a `power_meter` integration by its
`name`. Whenever that sensor's `power_w` reading crosses
`contract_limit_w * 1.10` ("safe indefinitely" limit), a notification is
sent through whatever `notifications` driver is configured (silently
skipped if none is). It re-notifies at most every 10 minutes while the
condition persists, and once more when it clears. See
`services/history.py`'s `PowerBudget` for the full three-zone model and
how to adjust its ratios - the 1.10/1.33 defaults approximate a typical
European residential contract's tolerance, not a universal constant;
check your own meter/breaker/utility documentation.

## `thermostat` (heating/cooling zones)

Optional. Turns on the dashboard's "Heating & Cooling" tab: per-zone
weekly setpoint schedules, editable from the dashboard, driving a
background control loop that opens/closes each zone's valve based on its
own current temperature vs. that schedule. Built for a shared-plant
radiant-panel setup (one central boiler/chiller, one valve per zone) but
nothing about it is radiant-panel-specific - any setup where "open a
valve/relay when a zone is too cold/hot" is the whole job fits.

```yaml
thermostat:
  mode: "heat"                # "heat" or "cool" - shared by every zone; also changeable from the dashboard
  hysteresis_c: 0.3            # dead band around the setpoint before the valve flips again - default shown
  schedules_file: "schedules.json"   # where the weekly grids + mode are persisted - default shown
  poll_interval_seconds: 60    # how often the control loop re-evaluates - default shown
  zones:
    - name: "Rooms"            # shows up as this name in the dashboard/API
      sensor: "Rooms Temperature"   # must match a `climate` integration's name above
      valve: "Rooms Valve"           # must match a `switch` integration's name above
    - name: "Bathrooms"
      sensor: "Bathrooms Temperature"
      valve: "Bathrooms Valve"
```

Each zone's `sensor` and `valve` are ordinary `climate`/`switch`
integrations declared like any other - the thermostat doesn't need its
own device protocol, it just reads one and commands the other. Give them
`dashboard_tab: "climate"` (see above) so they render as part of their
zone's card instead of also showing up as generic ones.

**Control logic** (`services/thermostat.py`): every `poll_interval_seconds`,
for each zone, the current hour's schedule slot gives a target
temperature (or nothing - the valve stays closed for hours with no
target set). In `heat` mode the valve opens once the zone is more than
`hysteresis_c` below the target and closes once it's that far above it,
staying in whatever state it was in while inside that band (so it
doesn't chatter open/close right at the setpoint); `cool` mode is the
mirror image. `mode` is shared across every zone, since a shared-plant
system's water is either hot or cold for everyone at once - the
dashboard's mode toggle changes it seasonally without editing
config.yaml.

**No real hardware yet?** Use [`type: simulated`](integrations/simulated.md)
for both `sensor` and `valve` - the exact same schedule/API/dashboard
works, and the simulated sensor even drifts toward a hotter/colder value
while its paired simulated valve is on, so the whole thing behaves
believably before you've bought anything.

**Valve on-hours** (the dashboard's "today/this week/this month" stat per
zone) is derived from the same history database everything else uses:
the valve switch's `is_on` reading is recorded on `history.poll_interval_seconds`,
and `HistoryStore.sum_on_hours_range()` counts how many of those samples
were "on" and multiplies by that interval. This means the on-hours stat's
accuracy is tied to `history.poll_interval_seconds`, not
`thermostat.poll_interval_seconds` - the two are independent.

### A zone that can't be cooled: `cool_enabled`

A room a radiant-panel system physically can't cool safely - the classic
case is a bathroom, where everyday humidity (showers) condenses on a
cold ceiling panel regardless of the room's own temperature - gets
`cool_enabled: false`:

```yaml
    - name: "Bathrooms"
      sensor: "Bathrooms Temperature"
      valve: "Bathrooms Valve"
      cool_enabled: false   # default: true
```

While `thermostat.mode` is `"cool"`, the control loop never touches this
zone's valve for temperature reasons at all - not even a forced "closed"
command, it's simply skipped (see the next section for why that
distinction matters). It behaves completely normally in `"heat"` mode.
The dashboard hides a `cool_enabled: false` zone's card entirely while
cooling (nothing to schedule - the thermostat won't act on it), and
shows it again in heat mode.

### `interlocks`: one switch forced to follow another

For a device whose own operation depends on a *specific* valve being
open for reasons that have nothing to do with any zone's temperature -
the motivating case is a water-based dehumidifier that draws chilled
water through a particular zone's circuit to condense moisture out of
the air, so that valve must open whenever the dehumidifier runs, even if
the zone itself is `cool_enabled: false`:

```yaml
thermostat:
  ...
  interlocks:
    - leader: "Dehumidifier"       # a `switch` integration's name
      follower: "Bathrooms Valve"   # a `switch` integration's name, forced to mirror the leader
      active_in_mode: "cool"        # optional - omit to apply regardless of thermostat mode
```

`services/interlock.py`'s `InterlockController` runs its own loop
(same `poll_interval_seconds` as the thermostat) that copies the
leader's on/off state onto the follower. When `active_in_mode` doesn't
match the current mode, the interlock does nothing at all for that
link - leaving the follower switch entirely to whatever else normally
controls it (typically the thermostat, in the mode the interlock isn't
active in), so the two never fight over the same switch. The dashboard
shows this relationship on the leader's own switch card ("Also has
Bathrooms Valve open right now") whenever it's actually in effect, since
otherwise a valve opening for a reason unrelated to its own zone's
temperature would be confusing to see.

Nothing about `interlocks` is dehumidifier-specific - any "switch A on
requires switch B on too" relationship fits.

### `mode_switches`: one switch forced to follow the thermostat's mode

For a switch that has no owner of its own at all - nothing schedules it,
no zone valve logic ever touches it - and should simply be on while the
thermostat is in one mode and off otherwise. The motivating case: a
cooling-only bypass valve that reroutes water around a zone's radiant
panel (so that zone's own valve can stay shut, avoiding condensation,
while the circuit still carries chilled water elsewhere) whenever the
system is in "cool", full stop - not tied to any other device's state.

```yaml
thermostat:
  ...
  mode_switches:
    - switch: "Cooling Bypass Valve"   # a `switch` integration's name
      active_in_mode: "cool"            # required - the mode this switch mirrors
```

Same `InterlockController`/loop as `interlocks` above, but with a
different rule: since nothing else owns this switch, there's no "leave
it alone" case to protect - outside `active_in_mode` it's actively
forced *off*, not left in whatever state it was. If you need a switch
that's sometimes owned by something else and sometimes forced to follow
another switch, that's `interlocks`, not this.

### Away mode

There's no config key for this - it's a runtime toggle, from the
Climate tab's "Away" button, for "I'm leaving for a day/few days, turn
every zone's valve off". The alternative would be painting every hour
of the schedule off for that period, but that's tedious, easy to
forget to undo, and destroys the schedule the person actually wants
back when they return. Away mode instead just remembers a deadline
(`away_until` in `schedules.json`, alongside the mode and each zone's
week) - `ThermostatController.tick()` holds every valve closed while
that deadline is in the future, and clears it and resumes the normal
per-zone schedule logic on its own on the first tick after it passes,
with no need for anything to remember to turn it back off.

## Language

The dashboard's UI text (labels, buttons, alerts) comes from a JSON file
under `src/localhome/web/locales/`, picked by `web.language`. Ships with
`en` (default) and `it`:

```yaml
web:
  language: "it"
```

An unknown code falls back to `en` with a warning logged at startup.

To add another language, copy `src/localhome/web/locales/en.json` to
`<code>.json` in that same directory and translate the values - keep
every `{placeholder}` (e.g. `{percent}`, `{error}`) exactly as it is,
since app.js substitutes those at render time. No other file needs to
change; `web.language: "<code>"` picks it up.

## Web auth (optional)

Off by default - this project assumes you're running it on a trusted
LAN. To turn on HTTP Basic Auth for the whole dashboard/API:

```bash
python tools/hash_password.py    # prompts for a password, prints a hash
cp config/secrets_web.example.json config/secrets_web.json
# paste the hash in as "password_hash", set "username"
```

```yaml
web:
  auth:
    secrets_file: "secrets_web.json"
```

`secrets_web.json` needs `username` plus either `password_hash`
(preferred - generated above, never stored as plain text) or `password`
(plain text, simpler if you're not worried about who can read that one
file). Every route, including the static JS/CSS, requires a valid
`Authorization: Basic ...` header once this is set.

**This is better than nothing, not a substitute for TLS.** HTTP Basic
Auth sends credentials base64-encoded (not encrypted) on every request -
fine to deter casual access on your own LAN, not safe if this is ever
reachable from an untrusted network. See
[docs/deployment.md](deployment.md#a-few-things-worth-knowing-before-you-expose-this-beyond-your-lan)
before exposing this beyond your LAN at all.

## Checking a new setup with `doctor`

Useful right after copying `config.example.yaml` on a new install (a
different home, a different machine): `localhome-cli doctor` builds
everything exactly like the web app does and reports what it found,
without starting a web server or polling anything on a schedule.

```bash
localhome-cli doctor
```

It catches two different kinds of problem:

- A driver that fails to **construct** - a missing `devices_file`, bad
  credentials - is logged loudly as it happens (`DeviceManager` already
  does this; `doctor` doesn't repeat it).
- A **name that doesn't match anything**, which otherwise fails silently:
  a typo in `thermostat.zones[].sensor`/`valve`, a `paired_switch`
  pointing at the wrong switch, an `energy.sensor` that doesn't exist.
  These don't crash anything - the thermostat just quietly never acts on
  that zone, the number just never gets embedded in its switch's card -
  which is exactly why they're worth a dedicated check on a new install
  rather than discovering them by noticing something never turns on.

Exits `0` with "No configuration problems found" when everything checks
out, `1` otherwise - safe to drop into a setup script or CI.

## Adding your own config keys

If your driver needs an option `config.yaml` doesn't have a name for
yet, just add it under that integration's entry - every key besides
`kind` and `type` is forwarded verbatim to the driver's factory as a
plain dict. Nothing needs to be declared elsewhere.
