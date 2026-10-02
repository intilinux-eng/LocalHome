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
  site_name: "LocalHome"  # optional - the name shown on the dashboard, max 24 characters
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
  max_db_mb: 300                     # default shown - hard ceiling: over it, the oldest days go first
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

Any entry can also carry `enabled: false`: it stays in the file but isn't
loaded at all - for a device that isn't installed yet or is out for
repair. It then raises no error anywhere (the admin panel's system
status, the LED matrix's problem codes), and turning it back on is
deleting that one line and restarting.

```yaml
  - kind: switch
    type: tplink_led_strip
    name: "Divano"
    enabled: false        # not installed yet
```

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

Two more dashboard-only options, for a device meant to keep running at a
chosen level - a VMC, typically:

- `hide_toggle: true` on a `switch` - its card shows whether it's
  running, but offers no on/off control.
- `presets` on a `number` - one-tap buttons above its slider, each
  `{key: <name>, value: <number>}` (the label is the dashboard's
  translation of `vmc_presets.<key>`: `night`, `day`, `boost`) or
  `{label: "<text>", value: <number>}` for your own wording. The button
  matching the current value is highlighted.

With `shelly_light`, add `on_with_value: true` to the number so picking
a value also switches the output on - otherwise a device stopped from
elsewhere (its own app) would have no way back on from the dashboard.
The climate admin panel's VMC gets all three automatically.

```yaml
- kind: switch
  type: shelly_light
  name: "VMC"
  device_id: "shelly0110dimg3-XXXXXXXXXXXX"
  broker_file: "mqtt_broker.json"
  hide_toggle: true
- kind: number
  type: shelly_light
  name: "VMC Speed"
  device_id: "shelly0110dimg3-XXXXXXXXXXXX"
  broker_file: "mqtt_broker.json"
  paired_switch: "VMC"
  on_with_value: true
  presets:
    - {key: night, value: 20}
    - {key: day, value: 50}
    - {key: boost, value: 100}
```

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

**One schedule per season.** Each zone has a separate weekly grid for
heating and for cooling, and the thermostat follows whichever matches
the current mode - a single shared grid meant switching to cooling
applied the winter setpoints as cooling targets. The dashboard paints
each season from its own range (heating 16-24, cooling 20-27) and labels
which grid it's showing. In `schedules.json` the heating grids stay
under the original `zones` key and the cooling ones sit alongside under
`zones_cool`, so a file written before the split needs no migration: it
is the heating schedule, and cooling starts empty - every valve stays
closed while cooling until a summer schedule is painted, and the
dashboard says so on each zone.

**Two zones run the same way?** A zone's schedule pane offers "Copy the
whole schedule to: <zone>" whenever another zone can be scheduled in the
current season - the winter and summer grids stay separate, and only the
one on screen is copied.

**No real hardware yet?** Use [`type: simulated`](integrations/simulated.md)
for both `sensor` and `valve` - the exact same schedule/API/dashboard
works, and the simulated sensor even drifts toward a hotter/colder value
while its paired simulated valve is on, so the whole thing behaves
believably before you've bought anything.

**Run statistics** (the week and month totals under a zone's
Statistics pane - how long the system was on and how many times it
switched on) come from the same history database everything else uses: the
valve switch's `is_on` reading is recorded on
`history.poll_interval_seconds`, and `HistoryStore.on_stats()` counts
the samples that were "on" (times that interval) and the off-to-on
changes. Their accuracy is tied to `history.poll_interval_seconds`, not
`thermostat.poll_interval_seconds` - the two are independent. The list
of runs above a zone's charts (when, how long, temperature and humidity
at start and end) is worked out in the page from the chart's own data.
Both count an "off" shorter than 5 minutes (a restart, one odd reading)
as part of the same run.

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

While `thermostat.mode` is `"cool"`, this zone's valve is **held
closed**: never opened for the zone's temperature, and checked against
its real state on every tick - a valve left open from the heating
season, or opened by hand, closes again at the next tick. The one
exception is a valve an [interlock](#interlocks-one-switch-forced-to-follow-another)
owns in cool mode: that interlock decides instead (see the next
section). It behaves completely normally in `"heat"` mode.

While cooling, this zone has no detail card on the dashboard: its tile
at the top of the Climate tab shows its temperature and its valve's real
state - "Valve closed ✓" (left out of cooling to avoid condensation),
"Valve open for <dehumidifier>" when an interlock opened it, "Valve
closing", or "Valve: state unknown" if the valve isn't answering. The
card comes back in heat mode.

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

A leader that isn't answering can't be confirmed running, so its
follower is switched **off** - a dehumidifier gone offline doesn't leave
the valve feeding it open indefinitely.

Leave this interlock out if that zone's valve must stay closed in cool
mode no matter what: without it, a `cool_enabled: false` zone's valve is
simply held closed all summer (see above).

Nothing about `interlocks` is dehumidifier-specific - any "switch A on
requires switch B on too" relationship fits.

### `season_rules`: what a switch does in each season

The general form of the two sections below. For each season, a switch
is one of:

- `on` - LocalHome keeps it on;
- `off` - LocalHome keeps it off (and the dashboard hides its card);
- `manual` - left to whoever uses it, from the dashboard.

```yaml
thermostat:
  ...
  season_rules:
    - switch: "Bathroom Bypass Valve"   # a `switch` integration's name
      heat: "off"
      cool: "on"
    - switch: "Dehumidifier"
      heat: "off"
      cool: "manual"
```

A season left out is `manual`. While a season forces a switch on or off,
the dashboard shows its state without a control and a manual command is
refused - it would only be undone on the next control cycle (every
`poll_interval_seconds`). Diagnostic mode suspends every rule.

The admin panel's two extra switches (bypass valve, dehumidifier) have
the same rules without any of this: **Extra switches → Behaviour per
season** in `/climate-admin`, starting from the ones above, applied as
soon as they're saved - no restart. A switch config.yaml already gives
a rule (here, or in the two sections below, or as an interlock follower)
keeps that one, and the panel shows it read-only.

### `mode_limited_switches`: a switch usable in one mode only

For a switch you control by hand, but that only makes sense in one mode -
a dehumidifier, used only while cooling. Outside that mode it's forced
off; inside it, it's left entirely to you. Its card is hidden in the
season that forces it off, and says under its name what it does in each
season ("Winter: always off · Summer: your choice"):

```yaml
thermostat:
  ...
  mode_limited_switches:
    - switch: "Dehumidifier"      # a `switch` integration's name
      only_in_mode: "cool"
```

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
another switch, that's `interlocks`, not this. On the dashboard its card
says so ("Winter: always off · Summer: always on") and offers no switch
while its season drives it.

### The ventilation unit's weekly program

Mechanical ventilation runs all the time; what changes is how fast. The
VMC's own card on the home page has a program for it, as the controllers
sold for these units do: a week of hours, each with one of the unit's
speeds - off, night, day or boost - painted on a grid like a zone's.
Ready-made weeks fill it in one tap and can be reshaped afterwards:
*day and night* (day 7-22), *out at work* (night speed 8-17 on
weekdays), *busy mornings and evenings* (boost 7-8 and 19-20), *always
day*, *always quiet*. It's saved in `config/vmc_schedule.json`:

```json
{
  "enabled": true,
  "target": "VMC Speed",
  "week": {"mon": [20, 20, 20, 20, 20, 20, 20, 50, ...24 speeds], "tue": [...], ...},
  "override": null
}
```

`target` is the device whose card the program was switched on from, so
a VMC declared in config.yaml is driven just as well as one mapped in
the admin panel. A speed of 0 switches the unit off; the next speed
switches it back on. The speed is written **once, when the program
changes it**: a speed picked by hand therefore stays until the next
change, instead of being corrected away a minute later. Every change on
the card saves by itself, and the control loop
(`services/vmc_schedule.py`, every 60s) re-reads the file, so it takes
effect without a restart.

The older form - `"day": {"at": "07:00", "value": 50}, "night": {"at":
"22:00", "value": 20}` in place of `week` - is still read, as the week
it stands for, and is what the classic page (`/classic`) edits; a week
painted otherwise is only switched on or off there.

**A timed speed** ("Boost for 30 min / 1 h / 2 h" on the card) is the
`override`: that speed until its end, then back to the program - or,
with the program off, to the speed and on/off state from before. A
speed picked by hand meanwhile ends it.

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

## The Shelly climate admin panel

`/climate-admin` is a second, separate page from the main dashboard -
built for one specific, common case: the exact same two-zone
heating/cooling setup described above, installed across multiple homes
that all standardize on the [Shelly](https://www.shelly.com/) device
family (Gen3 H&T sensors, Plus 1 relays). Redeploying to a new home
should only ever mean typing in *that* home's own device ids - never
touching code or `config.yaml` by hand.

When nothing's been saved here yet, the page pre-fills itself from
`config.yaml`'s own current, hand-written setup (real device ids
included) rather than a blank form - purely read-only, and never
consulted again once something's saved. Saving writes to
`config/climate_map.json` instead (additive, gitignored) - see
`services/climate_map_store.py`'s module docstring for the exact
on-disk shape and precedence rules. **Changes here need a LocalHome
restart to take effect** (`DeviceManager` only builds its drivers once,
at process start) - the page has a "Restart LocalHome" button for this
(re-execs the process in place - reliable on Linux, the deployment
target; on Windows, the C runtime's `execv` emulation reliably leaves
two processes fighting over one port instead of actually restarting,
confirmed empirically, so the button refuses there instead of leaving
you with a page that never comes back - `services/self_restart.py`'s
`is_supported()` is the check. Developing on Windows: stop the process
yourself (Ctrl+C) and start it again instead).

The page opens on an **overview**: one tile each for the system status,
the season, the zones, the MQTT broker, the board (uptime, clock) and
the LED matrix, coloured by how they're doing and each a link to its
section. Below it the sections come in four groups - *Plant* (season,
zones), *Devices* (broker, extra switches, outdoor sensor, VMC), *Board*
(system status, LED matrix, settings) and *Tools* (diagnostic mode,
maintenance, how it works) - each a card whose header says its state
even when closed. A sticky bar of links follows the scroll. Two notices
sit above everything when they apply: saved changes waiting for a
restart (naming which, with the restart button), and diagnostic mode
being on. Which sections are open is remembered for the tab, so the
reload after a restart lands back where you were.

Per zone:

- **Sensor source**: a real Shelly device (needs a device id), a
  simulated one (`drivers/simulated/climate.py` - no hardware
  installed yet), or a manually-typed debug value
  (`drivers/manual/climate.py`) held fixed until changed, so the
  thermostat's on/off decision can be exercised at a chosen
  temperature without waiting for a real event.
- **Valve source**: a real Shelly relay, or simulated
  (`drivers/simulated/switch.py`) - fully controllable through the
  same generic on/off route the panel's own manual test buttons use,
  so the whole control loop (sensor → thermostat → valve) can be
  verified before any relay is actually wired.

The MQTT broker connection (`config/mqtt_broker.json`) is also editable
from this page - host, port, username, and password (never read back
out once saved), with a "test connection" check against whatever's
currently typed in, before saving it. And two more fixed, standalone
Shelly Plus 1 switches with no sensor or schedule of their own - a
bathroom bypass valve and a dehumidifier - are mappable the same way;
if `thermostat.interlocks`/`mode_switches` above already control one of
these by a different name, update that entry to say "Bathroom Bypass
Valve"/"Dehumidifier" instead, since this panel always uses those exact
names.

One more fixed, standalone sensor - the outdoor temperature/humidity,
a Shelly H&T Gen3 - has its own "Outdoor temperature sensor" section.
It's not part of either zone (no valve, no thermostat decision); it
feeds the outdoor chip at the top of the Climate tab and its own chart.
The panel always builds it as "Outdoor Sensor".

Both it and the VMC below have a **source**, like a zone's sensor:

- **Shelly** - the real device; its id is required.
- **Simulated** - no hardware behind it. The outdoor sensor follows the
  month's average temperature with a 5 degree swing over the day
  (warmest at 15:00, coldest at 03:00), so its chart looks like
  weather; the VMC is the same card, speeds and day/night program as the
  real one. Everything simulated is marked "simulated" on the
  dashboard, so it never passes for the real apartment.
- **None** - not built at all.

On a climate installation (the panel manages the zones), a section that
has never been saved starts **simulated**, so a new board shows the
outdoor chart and the VMC's controls before the hardware exists. Never
over a real device, though: not if config.yaml already wires one by hand
(an integration named "Outdoor Sensor" / "VMC", or any `shelly_light`
dimmer), and not if the section was saved before sources existed - there
a device id still means the real device and a blank still means none.
A LocalHome that isn't doing climate at all gets neither.

The VMC (mechanical ventilation) has its own section too, with the
same Shelly / simulated / none source: one device id
of the Shelly 0/1-10V Dimmer Gen3 driving its speed builds both an on/off
switch named "VMC" and its speed as "VMC Speed", paired into one
dashboard card. The card has no on/off toggle - residential mechanical
ventilation is meant to run continuously - and three speed modes:
Night 20%, Day 50%, Boost 100%, plus the slider for anything in between.
Picking a mode also starts the VMC if it was stopped from elsewhere. If
config.yaml already has a `shelly_light` entry for a dimmer, the section
offers its device id pre-filled.

A device id the panel manages replaces any config.yaml integration for
the same device (matched by its `device_id` or its MQTT topic prefix):
that entry is skipped with a warning in the log, instead of the same
hardware being polled and commanded twice under two names. Removing the
old entry from config.yaml silences the warning. History recorded under
the old name stays under the old name.

The page's live status section reflects whichever mapping is *actually
running* (since the last restart) - joined against the saved mapping to
show a mode/source pill (e.g. `SIMULATED`) next to each zone. A
dedicated broker-reachability check (a raw TCP probe, not a full MQTT
handshake) is kept separate from each zone's own status, so a broker
that's simply not running isn't mistaken for six devices failing at
once. Guidance text is keyed off the specific error a device's driver
reports: a battery sensor that hasn't reported *yet* (it only wakes on
its own schedule, or a button press) reads differently from one that
*was* reporting and has now gone quiet.

Every *valve/switch* this panel builds (both zones' valves, the two
extra switches) also gets an `availability_topic` of `<device id>/online`
for free, with no field to fill in - every Shelly publishes this itself
(MQTT LWT, retained, updated by the broker the instant the device
disconnects), so "was reporting, now unreachable" is detected
immediately rather than only after `stale_after_seconds` (see
`services/climate_map_store.py`'s `_valve_integration_options()`). This
is deliberately **not** wired up for sensors (the zones' own H&T
sensors, the outdoor sensor): tried it live against the two real H&T
Gen3 devices governing this apartment's climate and had to back it out
- see "Sensor edge cases" just below for why a battery H&T's own
"online" topic isn't a trustworthy signal at all.

### Sensor edge cases, explained in-panel

The page's own "How this works" section (collapsed by default, above
the zone cards) spells out, for the installer, exactly what a battery
H&T Gen3's reporting behavior means for each status pill - deliberately
kept there instead of a push notification, so it's always visible
alongside the state it's explaining rather than depending on a
Telegram bot token or a phone being nearby:

- **A rock-stable reading isn't stale.** The sensor sends at once on a
  change past the thresholds set in the Shelly app (0.5°C and 1% are the
  tightest it offers), and unconditionally every 2 hours regardless -
  each report carrying the exact value, to 0.1°C - so a
  reading's age creeping toward 2h is normal, not a fault, and
  `stale_after_seconds` (4h - just under double the heartbeat) only
  trips on an actually-missed device (two heartbeats in a row), not a
  merely-quiet room. An overnight watch of two real H&T Gen3 sensors
  confirmed the 2h promise holds precisely - every heartbeat landed
  within about a minute of exactly 2h, whatever their individual
  firmware version or WiFi signal.
- **The device's own "online" status isn't a useful signal for a
  battery sensor, on purpose.** In battery mode, an H&T wakes locally
  about once a minute just to measure, but only actually connects to
  publish when a threshold trips or its 2h heartbeat is due - the rest
  of the time it isn't connected to the broker *at all*, to save power.
  That means its MQTT availability topic (LWT-backed `<id>/online`)
  reads "false" for most of a perfectly healthy sensor's normal working
  life, not just when something's actually wrong - confirmed live
  against this apartment's own two H&T Gen3 sensors, which is why this
  panel doesn't wire `availability_topic` up for sensors at all (see
  just above) and relies on `stale_after_seconds` alone instead. A
  mains-powered device (a valve/relay) has no such legitimate
  "disconnected on purpose" state, so its own availability topic stays
  meaningful. Powering an H&T over USB instead of battery changes this
  trade-off entirely - it then wakes and reports unconditionally every
  5 minutes rather than only on a threshold, which would make its
  online status a trustworthy signal too - but that's a different,
  more power-hungry device configuration, not something this panel
  assumes.
- **Weak WiFi** shows as the existing signal-strength figure next to
  each reading - worth fixing physically (move the sensor, add a
  repeater), since it drains the battery faster than normal use.
- **Low battery** is highlighted in the same card, at the same 10%
  threshold the main dashboard's own popup uses - no separate channel
  needed to notice it.
- **A sensor that's actually unreachable for a long time** - not just
  quiet, no availability/heartbeat at all past `stale_after_seconds` -
  eventually trips `ThermostatController`'s **sensor failsafe**. What
  counts toward it is silence LocalHome was there for: the total is kept
  in `config/sensor_silence.json` so restarts don't restart the
  countdown, and time the board spent switched off doesn't count at all
  (the sensors were talking; nobody was listening). It is
  configurable per-thermostat as `sensor_failsafe_hours` (default 24),
  it forces that zone's valve closed rather than leaving it frozen in
  whatever it was last doing indefinitely, and clears itself the moment
  the sensor reports again. The zone's card shows the countdown
  (`sensor_unreachable_hours` vs. the threshold) and whether it's
  already tripped.

### Diagnostic mode

A system-wide override, toggled from its own section on `/climate-admin`
(under *Tools*; while it's on, a banner at the top of the page says so
and the section opens by itself)
(`POST /api/climate/diagnostic-mode`, persisted as `diagnostic_mode` in
`schedules.json` via `ScheduleStore`) - while on, `ThermostatController`
and `InterlockController` both skip every zone/interlock/mode_switch
entirely on every tick, the same "hands off, not even a forced close"
stance `cool_enabled: false` already takes for one zone. Every valve
this project knows about (both zones' valves, plus the bathroom bypass
and dehumidifier if wired) becomes directly, manually switchable from
that same section - reusing the generic `/api/sensors/<name>/on|off`
route every controllable device already has, including a mode_switch
like the bypass valve, which that route normally refuses to touch
manually (only while diagnostic mode is on).

Meant for on-site verification - does this relay actually switch? which
physical valve is which name? - without the thermostat undoing a manual
test on its next cycle a few seconds later, and as a way to hold any
valve open (or closed) for as long as needed while riding out a sensor
failsafe or replacing a battery. Turning it back off resumes normal,
sensor-driven control immediately (no restart, and no waiting for the
next scheduled tick) - `ThermostatController.forget_valve_state()` is
called first, so a manual change made during diagnostic mode is
actually re-asserted on that first tick back, not silently skipped
because the controller's own bookkeeping of "what I last commanded"
still happens to agree with the newly-computed state.

### Settings: tuning without editing config.yaml

The admin panel's **Settings** section changes the operating parameters
without touching this file: `thermostat.hysteresis_c`,
`thermostat.poll_interval_seconds`, `thermostat.sensor_failsafe_hours`,
`history.retention_days`, `history.max_db_mb`, `web.language` and
`web.site_name`. Each has a safe range (a value outside it is refused),
and each field shows whether its value comes from the panel, from
config.yaml or from the built-in default. Saved in
`config/panel_settings.json`, they take precedence over config.yaml after
a restart; "Restore defaults" removes them. A factory reset removes them
too.

**Page name** (`web.site_name`) is the exception that needs no restart:
it's read on every page render, so it's there on the next reload. It's
the heading at the top of the dashboard, the browser tab's title and the
name under the icon when the dashboard is added to a phone's home screen
(`/site.webmanifest` is built from it). At most 24 characters, so it
still fits a phone's header; line breaks and repeated spaces are
flattened, control characters are refused, and a blank field puts
"LocalHome" back.

### Maintenance: backup, clear, factory reset

A collapsed **Maintenance** section under *Tools* in `/climate-admin`:

- **Download backup** - config.yaml, the panel's zones and devices,
  schedules and manual values as a `.zip`. Credentials are left out
  (`secrets_*.json`, `devices.json`, `tuya_cloud.json`, and the MQTT
  broker's password is blanked) - they never leave the board.
- **Clear history** - empties the charts and the run statistics.
- **Clear schedules** - empties every zone's grid for both seasons and
  cancels away and diagnostic mode; the season stays. Valves close until
  a new schedule is painted.
- **Factory reset** - removes everything LocalHome created at runtime
  (the panel's zones/VMC/extra devices, schedules, manual values, cached
  readings, shutter positions, history), then restarts. config.yaml,
  credential files and the MQTT broker settings stay, so the board comes
  back as a fresh installation on the same network, ready to be mapped
  again from the panel.

Each destructive action asks for `RESET` to be typed, and first saves a
full backup on the board in `config/backups/` - the last 10
configuration backups (credentials included) and the last 2 history
databases. To undo, stop LocalHome and copy the files back from the zip
into `config/`. `config/backups/` is gitignored.

Anyone who can open the admin panel can use these: turn on
[web auth](#web-auth-optional) if others share your network.

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

- A driver that fails to **load** - a missing `devices_file`, bad
  credentials, or a driver whose optional package isn't installed (e.g.
  `tplink_led_strip` without `pip install -e .[tplink]`) - is listed as
  a problem with its error, which names the install command where one
  applies. Optional drivers that aren't installed but aren't used either
  are mentioned as a note, not a problem.
- A **name that doesn't match anything**, which otherwise fails silently:
  a typo in `thermostat.zones[].sensor`/`valve`, a `paired_switch`
  pointing at the wrong switch, an `energy.sensor` that doesn't exist.
  These don't crash anything - the thermostat just quietly never acts on
  that zone, the number just never gets embedded in its switch's card -
  which is exactly why they're worth a dedicated check on a new install
  rather than discovering them by noticing something never turns on.

It also prints the thermostat's current season as stored in
`schedules.json` (the one switched from the dashboard - `thermostat.mode`
in `config.yaml` is only the default for a fresh install), and whether
diagnostic mode or away mode is on.

Exits `0` with "No configuration problems found" when everything checks
out, `1` otherwise - safe to drop into a setup script or CI.

On a running install the same faults - plus devices that stopped
responding, a stalled control or recording loop, low disk space and the
process's memory/CPU - are shown live in the **System status** section
of `/climate-admin` (`GET /api/system-status`).

## Adding your own config keys

If your driver needs an option `config.yaml` doesn't have a name for
yet, just add it under that integration's entry - every key besides
`kind` and `type` is forwarded verbatim to the driver's factory as a
plain dict. Nothing needs to be declared elsewhere.
