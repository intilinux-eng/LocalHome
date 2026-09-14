# Simulated (no hardware required)

Covers `type: simulated`, registered for both `climate` and `switch`. It
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
