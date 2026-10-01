# Simulated (no hardware required)

Covers `type: simulated`, registered for `climate`, `switch` and
`number` (a dimmer or fan speed: `initial_value`, `min`, `max`, `unit`
- enough to try a VMC and its day/night program with no hardware; with
`paired_switch` and `on_with_value: true`, picking a speed also switches
the paired simulated switch on, as the real VMC dimmer does). It
fakes a device with nothing real behind it - for trying out the
dashboard, or a whole feature like the [thermostat](../configuration.md#thermostat-heatingcooling-zones),
before you own the actual hardware. Also handy for demoing LocalHome to
someone else without giving them access to your real devices.

## Why bother simulating instead of just hardcoding numbers in the UI

Because the *rest* of the app doesn't know the difference. A simulated
driver implements the exact same `PollingDriver`/`SwitchDriver` interface
a real one does (see [docs/adding-a-driver.md](../adding-a-driver.md)),
so the dashboard, the history database, and - for a thermostat setup -
the actual control loop all run for real against it. When the real
hardware arrives, you change `type: simulated` to the real driver's type
and nothing else: not the dashboard, not the schedule you already set
up, not the zone names the thermostat's control loop refers to.

## The climate driver reacts to a paired switch

`influenced_by` names a `simulated` switch: while it's on, the
temperature drifts toward `influence_target_c`; while it's off, it
drifts back toward `base_temperature_c`. This is what makes a simulated
thermostat setup feel real instead of static - open the valve in the
dashboard and watch the temperature actually follow over the next few
reads, the same way flipping a real valve would (just faster, since
there's no real water/air mass to heat).

## A simulated Shelly H&T Gen3

With `mimic_ht_gen3: true` the climate driver stands in for a Shelly
H&T Gen3 on batteries - what the admin panel builds for a zone whose
sensor is set to *Simulated*, so a test bench or a demo runs like the
real installation:

- **The same fields**: temperature and humidity to a tenth, `battery_pct`
  and `wifi_rssi_dbm` (each sensor its own, steady from one start to the
  next), all recorded in the history like a real sensor's.
- **The same rhythm**: a report when the temperature moves 0.5° or the
  humidity 1% since the last one, and a heartbeat every 2 hours on its
  own clock - so the dashboard says "updated 40 min ago", as it does for
  the real ones. See [shelly-gen3.md](../shelly-gen3.md).
- **A room that behaves**: with the zone's valve (`influenced_by`) open it
  warms in winter or cools in summer at about 0.6° an hour, like a
  radiant floor - heating never cools a room, cooling never warms it -
  and with the valve closed it drifts back toward the season's resting
  temperature (19.5° in winter, 27° in summer). The season is read from
  `season_file`, the thermostat's `schedules.json`. Its humidity wanders
  like a lived-in room's - about two or three 1% reports a day on top of
  the 12 heartbeats, as the real ones send, however often it's polled.
- **The same health panel**: each report is also published, on the real
  one's topics (`status/devicepower:0`, `status/wifi`), in the simulated
  world instead of the broker - so the admin panel's *Shelly health*
  lists it, marked simulated, with its reports, heartbeats, signal and
  battery like a real one.
- **The same after a restart**: with a `state_file` (the admin panel uses
  `config/simulated_sensors.json`) it carries on where it was - the room
  as warm as it had got, the last report shown with its real age, the
  heartbeat on its own clock - instead of starting over. It's written at
  each report only: a few times an hour at most.

## Options

**`climate`**

| Option | Default | Meaning |
|---|---|---|
| `name` | "Simulated sensor" | dashboard/API/CLI name |
| `base_temperature_c` | 20.0 | resting temperature when not influenced |
| `base_humidity_pct` | 50.0 | starting humidity (random-walks slowly) |
| `influenced_by` | none | name of a `simulated` switch to react to |
| `influence_target_c` | 26.0 | where it drifts to while that switch is on |
| `drift_rate_c_per_min` | 0.15 | how fast it moves toward whichever target applies |
| `poll_interval_seconds` | 10.0 | how often a new reading is generated |
| `mimic_ht_gen3` | false | behave like a Shelly H&T Gen3 - see above (the other drift options are then unused) |
| `season_file` | none | with `mimic_ht_gen3`: the `schedules.json` whose `mode` says the season |
| `state_file` | none | with `mimic_ht_gen3`: a JSON file where it keeps its room and last report across restarts (one file can hold several sensors) |

**`switch`**

| Option | Default | Meaning |
|---|---|---|
| `name` | "Simulated switch" | dashboard/API/CLI name |
| `initial_on` | false | starting state |
| `poll_interval_seconds` | 10.0 | how often it's "polled" (it's in-memory, so this mostly just controls history-recording cadence) |

## Example - a fake power meter with no thermostat involved

```yaml
- kind: switch
  type: simulated
  name: "Demo Plug"
```

No `influenced_by` needed if you just want something to click on/off and
see recorded in history - only pair it with a climate sensor if you're
building out a scenario like the thermostat's.
