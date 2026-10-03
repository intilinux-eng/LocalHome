# Shelly Gen3 over local MQTT: field notes

What running Shelly Gen3 devices on a local MQTT broker actually looks
like, day and night, in a lived-in apartment: two **H&T Gen3** sensors on
batteries and a **Dimmer 0/1-10V PM Gen3** driving a ventilation unit,
all talking to a Mosquitto broker on the same board as LocalHome - no
Shelly cloud, no hub, no Home Assistant in between.

Everything below was either measured on those devices or read from
their own replies; where a number comes from Shelly's documentation
instead, it says so. If you run the same devices, this is meant to save
you the weeks it took to find out. Corrections and additions from other
installations are very welcome - open an issue with your model and
firmware.

- [The devices](#the-devices)
- [MQTT settings that work](#mqtt-settings-that-work)
- [H&T Gen3 on batteries](#ht-gen3-on-batteries)
- [Dimmer 0/1-10V PM Gen3](#dimmer-01-10v-pm-gen3)
- [Plus 1 relay](#plus-1-relay)
- [The network under them](#the-network-under-them)
- [Where this lives in the code](#where-this-lives-in-the-code)

## The devices

| Device | Id on MQTT | Powered | Status here |
|---|---|---|---|
| **Shelly H&T Gen3** - temperature and humidity | `shellyhtg3-<mac>` | 4 × AA batteries | Two units, in daily use since September 2026 |
| **Shelly Dimmer 0/1-10V PM Gen3** (`S3DM-0010WW`) - a 0-10 V control output | `shelly0110dimg3-<mac>` | mains | One unit driving a VMC's speed input, in daily use; firmware 2.0.0 |
| **Shelly Plus 1** (Gen2) - relay | `shellyplus1-<mac>` | mains | One unit as the Rooms valve since October 2026 (no load yet); firmware 1.3.3 |

A Gen2/Gen3 id is the model prefix plus the device's full MAC address,
lowercase. The same id is the MQTT topic prefix, the RPC address and the
device's mDNS name (`http://<id>.local` opens its web page).

## MQTT settings that work

On each device, in its web page or the Shelly app, **Settings → MQTT**:

| Setting | Value | Why |
|---|---|---|
| Enable | on | |
| Server | `<board>.local:1883` | The board's **name**, not its IP: Gen3 resolves `.local` names, so the router is free to re-address the board. |
| Client ID, topic prefix | the device id (the default) | Every topic below starts with it. |
| Generic status update over MQTT | **on** | This is what publishes `<id>/status/<component>` - without it a sensor says nothing. |
| RPC over MQTT | **on** | Needed to ask a mains device for its status (the dimmer, the health check). |
| TLS, user, password | off / empty | A broker on the LAN, anonymous - see [deployment.md](deployment.md). |

Check it from the board while pressing the device's button:

```bash
mosquitto_sub -v -t 'shellyhtg3-xxxxxxxxxxxx/#'
```

## H&T Gen3 on batteries

### When it talks

It sleeps nearly all the time. It connects to the Wi-Fi and the broker
only to send a report, and it sends one:

- **at once, when temperature or humidity has moved** by the thresholds
  set in the Shelly app since the last report - 0.5 °C and 1% are the
  tightest it offers;
- **every 2 hours regardless**, even in a room where nothing changes.

The 2-hour heartbeat is precise: over several nights on two sensors,
with different firmware and very different Wi-Fi signals, every
heartbeat arrived within about a minute of exactly 2 hours.

**A change report doesn't move the heartbeat.** The heartbeat keeps its
own clock: after an extra report at 12:14, the next heartbeat still came
at 12:53 - two hours after the 10:52 one, not after 12:14. And the
thresholds are measured from the **last value sent**, heartbeat or not.

A real day, two sensors with the thresholds at 0.5 °C and 1%, a stable
apartment at the end of September:

| | Reports in 24 h | Heartbeats | Change reports | Temperature range |
|---|---|---|---|---|
| Rooms | 15 | 12 | 3 - all humidity (-1.1%, -1.0%, -1.1%) | 25.6-26.0 °C |
| Bathrooms | 14 | 12 | 2 - both humidity (+1.0%, -1.0%) | 24.9-25.2 °C |

The temperature never moved 0.5 °C away from the last value sent, so it
never triggered a report on its own: in a steady house the sensor speaks
on its heartbeat, and now and then for humidity. Each heartbeat still
carries the exact temperature, so the chart follows every 0.1 °C.

Each report carries the exact reading of that moment, to 0.1 °C - the
thresholds decide *when* it speaks, not how precisely. That's why a
night chart falls in 0.1 °C steps about 2 hours apart, and why
"updated 1h 50m ago" is perfectly normal.

Tighter thresholds mean more reports and shorter battery life,
especially on a weak signal, where every wake costs more.

### What one wake publishes

Several topics within a second or two - none of them **retained**:

```text
shellyhtg3-xxxxxxxxxxxx/status/temperature:0   {"id":0,"tC":21.3,"tF":70.3}
shellyhtg3-xxxxxxxxxxxx/status/humidity:0      {"id":0,"rh":54.8}
shellyhtg3-xxxxxxxxxxxx/status/devicepower:0   {"id":0,"battery":{"V":5.66,"percent":82},"external":{"present":false}}
shellyhtg3-xxxxxxxxxxxx/status/wifi            {"sta_ip":"192.168.1.40","status":"got ip","ssid":"...","rssi":-80}
```

(Payloads trimmed to the fields that matter.) Three consequences:

- **A subscriber that starts after a wake hears nothing until the next
  one** - up to 2 hours. Keep the last payload yourself, with the time
  it arrived, across restarts. LocalHome does
  ([state_cache.py](../src/localhome/drivers/mqtt/state_cache.py)), so a
  restart doesn't blank every sensor for two hours.
- **Merge the topics into one reading**, each field from whichever topic
  last carried it: a missing humidity message shouldn't throw away a
  fresh temperature.
- **One wake is one report**: count the burst of topics (within a
  minute) as one, if you count reports at all.

### Is it alive?

- **Don't use its `online` topic.** `<id>/online` is the broker's Last
  Will for the device - and a sleeping H&T is disconnected on purpose, so
  it reads `false` for nearly the whole life of a perfectly healthy
  sensor. It means something only for a mains device.
- **Judge it by its heartbeat.** A sensor silent for more than two
  heartbeats (4 hours) is really gone; one late report isn't. LocalHome
  marks a reading stale after 4 h and, separately, counts heartbeats
  that never arrived: a gap between two reports longer than 2 hours
  (plus slack) holds that many lost ones.
- **Don't blame the sensor for your own downtime.** If the broker or the
  board was off (unplugged, rebooting), the sensor kept reporting to
  nobody. Only count the silence you were there to hear -
  [shelly_health.py](../src/localhome/services/shelly_health.py)'s
  `missed_reports()` skips any gap that ends with LocalHome starting.

### Battery

`status/devicepower:0` carries both the percentage and the voltage of the
4 AA cells: 96% read 5.93 V and 82% read 5.66 V on the two units here.
The percentage comes with every report, so a low battery shows up by
itself - LocalHome highlights it at 10%.

### Signal

The sensor reports its own Wi-Fi signal (`status/wifi`, `rssi` in dBm)
with every wake.

| dBm | |
|---|---|
| above -65 | excellent |
| -65 to -75 | good |
| -75 to -85 | weak but usable |
| below -85 | critical |

In practice the bands are softer than they look: one of the sensors here
sits at -85 dBm on average (-90 at worst) and has kept reporting on
schedule for weeks. What a weak signal costs is **battery** - each wake
takes longer to connect - so lost reports, not the dBm figure, are the
thing to act on. LocalHome shows the signal and never raises it as a
fault on its own.

### The small things

- **Its web page is reachable only while it's awake.** Press the button:
  it wakes, sends a report at once, and stays connected for a few
  minutes - long enough to open `http://<id>.local` or run the
  `mosquitto_sub` above. Also the quickest way to test a new install.
- **After the board or broker restarts**, a zone waits for its sensor's
  next report - at most 2 hours. The button skips the wait.
- **On USB power** instead of batteries, according to Shelly it reports
  every 5 minutes and stays connected, and its `online` topic becomes
  meaningful. Not tried here: these units run on batteries.

## Dimmer 0/1-10V PM Gen3

Used here to set a ventilation unit's speed through its 0-10 V input.

- **Ask it, don't wait for it.** Its `light:0` status is published only
  when something changes, and never retained, so a freshly started
  LocalHome has nothing to read. Being mains-powered and always
  connected, it can simply be asked: `Light.GetStatus` and `Light.Set`
  over **RPC on MQTT** - publish `{"id", "src", "method", "params"}` to
  `<id>/rpc`, the reply comes back on `<src>/rpc`. Use a reply address
  unique to each process: two programs sharing one (a PC and the board,
  say) receive each other's replies.
- **Brightness must be a whole number.** Firmware 2.0.0 silently ignores
  `"brightness": 30.0` - no change, no proper reply - and applies `30` at
  once.
- **It's a control signal, not a load**: `apower` is always 0 W. It does
  report the mains voltage (`voltage`, 225-227 V here) - a free supply
  monitor.
- **It runs warm.** 57-58 °C internal (`light:0.temperature.tC`) in a
  closed wall box, with the output on or off: its own power supply and
  radio. Shelly devices switch their output off by themselves at about
  90 °C; LocalHome flags one above 80 °C.
- **Leave Eco mode off.** It lowers the CPU clock and puts the Wi-Fi
  radio to sleep between packets: a fraction of a watt saved, paid for
  in latency and dropped requests - worse on a weak signal (this one is
  at -78 dBm).
- **`Shelly.GetStatus` tells you the rest**, in one call every few
  minutes: `wifi.rssi`, `sys.uptime` (a reset uptime is a power cut or
  a crash), `sys.available_updates.stable.version` (a firmware update
  waiting), `sys.restart_required`, and each component's `errors`
  (`overtemp`, `overpower`, ...).

## Plus 1 relay

A zone valve's (or the dehumidifier's) on/off relay. One unit on the
production board since 3 October 2026, firmware 1.3.3, measured with no
load on it.

### Settings for a valve

In the Shelly app, besides the [MQTT settings](#mqtt-settings-that-work):

| Setting | Value | Why |
|---|---|---|
| Input/output → relay type | **Detached** | Only LocalHome moves the valve: whatever ends up on SW (a wire, a disturbance) can't. The default, Toggle, follows the input. |
| Relay state at power-on | **Off** | After a power cut the valve starts at rest: closed. The default follows the input. |
| Auto OFF | **on, 900 s** | The backstop: any switch-on - from LocalHome, the app, the button - ends within 15 minutes unless renewed, a restart of the Shelly included (see below). |
| App-only settings (input state shown on the button, notifications, the card) | the defaults | They change the app, not the device. |

LocalHome reads these back from every relay it commands, every 5
minutes (`Shelly.GetConfig` over MQTT), together with MQTT control and
"Generic status update" being on. Anything set otherwise shows in the
admin panel - the device's row in *Devices* says which setting, what it
is and what it should be, in the app's own words, and *System status*
lists it as a problem. Nothing is ever changed on the device.

### On only while LocalHome says so

If the board stops, a relay keeps its last state: a valve left open stays
open. So LocalHome never sends a plain `on` to a valve: it sends `on,900`
on the command topic - on for 15 minutes, then off by itself - and renews
it every minute while the valve should be open
([drivers/mqtt/switch.py](../src/localhome/drivers/mqtt/switch.py),
`hold_seconds`). Measured on the Plus 1:

| Test | Result |
|---|---|
| `on,60` renewed every 20 s for 100 s | stays on, **never drops** (no `output: false` in its MQTT status, no extra click); each renewal restarts the timer (`timer_started_at` moves) |
| renewals stop | off by itself 59 s after the last one, `source: timer` |
| `on,900` | accepted |
| `off,20` renewed - a relay whose rest is on | stays off, then back on by itself |
| a plain `on` while a timer runs | **cancels the timer** - stays on for good. Hence never a plain `on` |
| restart of the Shelly while on, no Auto OFF | **comes back on with no timer** (`source: init`) |
| the same with Auto OFF set | comes back on with Auto OFF's timer, then off ✓ |
| `on,60` renewals with Auto OFF at 30 s | the command's timer wins, on past the 30 s, no drop ✓ |

Hence both: `on,900` renewed by LocalHome, and Auto OFF at 900 s in the
device for whatever LocalHome doesn't cover. The command topic takes
`on`, `off`, `toggle`, `on,<seconds>`, `off,<seconds>` - anything else,
`true` or `True` included, is ignored without a word.

## The network under them

The devices were the easy part; the network is where the surprises
were.

- **Wi-Fi clients must be able to reach each other.** The board, the
  broker and the Shellys are all Wi-Fi clients of the same router. An
  access point with client isolation - or, as happened here, a router
  that silently stopped forwarding between clients on the same radio
  until it was restarted - leaves everything "connected" and nothing
  working: the board still answers ping, while no MQTT or web
  connection gets through. If every Shelly goes quiet at once while
  the board is on the Wi-Fi, suspect the router first. LocalHome
  detects it - no mains Shelly connected to the broker for 10 minutes,
  or every battery sensor well past its heartbeat - and shows `RT` on
  the LED matrix.
- **Gen3 is 2.4 GHz only.** With band steering (one network name for
  both bands) the Shellys land on 2.4 GHz and the board on 5 GHz; that
  works, as long as the router forwards between the two.
- **Keep 5 GHz on a fixed channel from 36 to 48, 80 MHz wide.** "Auto"
  and 160 MHz both reach the DFS (radar) channels, which the router
  must leave - dropping every 5 GHz client - whenever it detects, or
  thinks it detects, a radar.
- **No fixed IP addresses needed.** Every device finds the broker by
  the board's name, and LocalHome never talks to a Shelly's IP. After a
  router change re-addressed every device here, everything reconnected
  within a minute without a single edit.
- **After changing router settings**, a Windows PC may keep a failed
  `.local` lookup cached for a while: `ipconfig /flushdns`.

## Where this lives in the code

- [drivers/mqtt/sensor.py](../src/localhome/drivers/mqtt/sensor.py) - the H&T: several topics merged into one reading, stale after 4 hours
- [drivers/mqtt/state_cache.py](../src/localhome/drivers/mqtt/state_cache.py) - the last payload of each topic, kept across restarts
- [drivers/mqtt/rpc.py](../src/localhome/drivers/mqtt/rpc.py) and [shelly_light.py](../src/localhome/drivers/mqtt/shelly_light.py) - RPC over MQTT, and the dimmer
- [services/shelly_health.py](../src/localhome/services/shelly_health.py) - each device's signal, temperature, restarts, updates and lost reports
- [services/climate_map_store.py](../src/localhome/services/climate_map_store.py) - the exact topics and fields built for each Shelly role from the admin panel
- [integrations/mqtt.md](integrations/mqtt.md) - every `mqtt_json` option, for Shellys and anything else on MQTT
