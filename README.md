# LocalHome

A small, local-first framework for automating a home with cheap smart
devices - roller shutters, power meters, climate/air-quality sensors,
smart plugs - **without a hub, without a subscription, and without your
data leaving your network unless one specific device forces it.**

Most drivers talk to devices directly over your LAN. A couple of
device families only expose their data through their vendor's cloud API
(there's no usable local protocol) - those are clearly called out, and
everything else keeps working if they're down.

This isn't a hardware-agnostic platform like Home Assistant; it's a much
smaller, hackable base meant to be read end to end in one sitting and
extended with exactly the drivers your home needs. If you outgrow it,
you've outgrown it - that's a fine outcome for a project this size.

## Screenshots

All three captured against the `simulated` driver and example device
ids - no real hardware needed to see this for yourself.

| Home | Heating & Cooling |
|---|---|
| ![Home tab: sensor cards and covers](docs/screenshots/home.png) | ![Heating & Cooling tab: per-zone schedule, on-time history and chart](docs/screenshots/climate.png) |

![Climate admin panel: mapping zones onto Shelly device ids, MQTT broker settings, and live per-zone status](docs/screenshots/climate-admin.png)
*Climate admin panel - see [below](#the-shelly-climate-admin-panel).*

## Install on an Arduino UNO Q, step by step

The reference installation: an Arduino UNO Q running one apartment's
heating/cooling with Shelly devices, managed entirely from the web admin
panel. The board must already be on your Wi-Fi (first setup with Arduino
App Lab); then, over SSH (`ssh arduino@<board-ip>`) or `adb shell`:

```bash
# 1. Time zone - the board ships in UTC, and the schedules run on its clock
sudo timedatectl set-timezone Europe/Rome

# 2. The board's name - devices and browsers reach it as <name>.local, never
#    by IP. Choose it now: renaming later means reconfiguring every device.
sudo hostnamectl set-hostname localhome
sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tlocalhome/" /etc/hosts

# 3. System packages
sudo apt update
sudo apt install -y git mosquitto avahi-daemon libnss-mdns python3.13-venv

# 4. MQTT broker, reachable by the Shelly devices on the LAN
printf 'listener 1883 0.0.0.0\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/localhome.conf
sudo systemctl restart mosquitto avahi-daemon

# 5. LocalHome
git clone <this-repo> ~/localhome && cd ~/localhome
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# 6. Configuration - climate only, no devices in the file (they're added
#    from the admin panel)
cp config/config.climate.example.yaml config/config.yaml
cp config/mqtt_broker.example.json config/mqtt_broker.json
.venv/bin/localhome-cli doctor

# 7. Start it at every boot
sed "s/YOUR_USER/$USER/g" deploy/localhome.service | sudo tee /etc/systemd/system/localhome.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now localhome

# 8. Tune the board for years of unattended running on its eMMC - REQUIRED
#    on every new board: system log in RAM, no background update downloads,
#    broker log in RAM, no desktop. Add --lean to also remove Arduino App
#    Lab and Docker (recommended on the 2 GB model). Safe to run again.
sudo sh deploy/tune-board.sh
```

9. **Open the admin panel** at `http://localhome.local:5000/climate-admin`
   and follow its four steps: choose the season, enter each Shelly's id
   (zones, bypass, dehumidifier, outdoor sensor, VMC), restart, check.
   On each Shelly, set the MQTT server to `localhome.local:1883` with
   "Generic status update over MQTT" on.

Everything after this - devices, schedules, settings, backups, reset -
is done from the admin panel. The full guide, with what each step does,
the limits and troubleshooting: [docs/arduino-uno-q.md](docs/arduino-uno-q.md).

## Try it on a PC

The generic steps, to try LocalHome on any computer with the example
devices:

```bash
git clone <this-repo> && cd <the-cloned-directory>
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

cp config/config.example.yaml config/config.yaml
# ...and the *.example.json files you need - see docs/configuration.md
# and docs/integrations/ for each device's own ids/keys/credentials.

.venv/bin/localhome-cli doctor   # sanity-checks config.yaml
.venv/bin/localhome
# -> dashboard at http://localhost:5000
```

For any other Linux board (a Raspberry Pi, ...) see
[docs/deployment.md](docs/deployment.md) for the full walkthrough,
including mosquitto, systemd (so it survives a reboot), and a Docker
alternative. Hit `externally-managed-environment` or "ensurepip is not
available"? See [docs/troubleshooting.md](docs/troubleshooting.md) -
both are one-line fixes.

**Using Shelly devices?** Skip editing `config.yaml` by hand and use
`/climate-admin` from the dashboard instead - see
[docs/configuration.md#the-shelly-climate-admin-panel](docs/configuration.md#the-shelly-climate-admin-panel).

Nothing under `config/` other than the `*.example.*` files is ever
meant to be committed - see [.gitignore](.gitignore). That's what keeps
your own device inventory, API keys and account passwords out of git
while this framework itself stays fully public.

## What's included

| Kind          | Driver                | Talks to                          | Local? |
|---------------|------------------------|------------------------------------|--------|
| `cover`       | `tuya_curtain_switch`  | Tuya-based curtain switches (e.g. LoraTap WiFi roller shutter modules) | Yes (LAN) |
| `climate`     | `tuya_air_quality`     | Tuya 9-in-1/12-in-1 air-quality sensors | Yes (LAN) |
| `power_meter` | `ewelink_powct`        | Sonoff POWCT power-monitoring relay | No - eWeLink cloud only, see [docs/integrations/ewelink.md](docs/integrations/ewelink.md) |
| `switch`      | `meross_plug`          | Meross smartplugs                  | No - Meross cloud (MQTT), see [docs/integrations/meross.md](docs/integrations/meross.md) |
| `switch` / `number` | `tplink_led_strip` | TP-Link Kasa/Tapo LED strips (on/off + brightness) | Yes (LAN) - see [docs/integrations/tplink.md](docs/integrations/tplink.md) |
| `cover` / `climate` / `power_meter` / `switch` / `number` | `mqtt_json` | **Generic** - any device that publishes JSON (or plain-string) state over MQTT: Shelly, Tasmota, Zigbee2MQTT, ESPHome, DIY sketches... | Yes (LAN, to your own broker) - see [docs/integrations/mqtt.md](docs/integrations/mqtt.md) |
| `climate` / `switch` | `simulated`       | **No hardware required** - a fake sensor/switch for trying out the dashboard (or a whole thermostat setup) before you own the real thing | N/A - see [docs/integrations/simulated.md](docs/integrations/simulated.md) |
| `notifier`    | `telegram`             | Telegram bot API                   | Cloud by nature (push notifications) |

`number` is a controllable analog value - a dimmer, a fan speed, a 0-10V
output - for anything set to a level rather than toggled or driven
open/closed.

The dashboard has two tabs: **Home** (your enabled devices, estimated
cover position, sqlite-backed history/charts, an optional power-budget
alert) and **Heating & Cooling** (a multi-zone valve thermostat - a
click-and-drag weekly schedule, run statistics, a one-tap away mode, a
zone that can opt out of one mode entirely, and switch-follows-switch
interlocks - see
[docs/configuration.md#thermostat-heatingcooling-zones](docs/configuration.md#thermostat-heatingcooling-zones)).
Both support optional HTTP Basic Auth and a switchable UI language
(English and Italian ship today - see
[docs/configuration.md#language](docs/configuration.md#language)).

Don't own any of the devices above? Adding your own driver is a
self-contained, half-hour change - see
[docs/adding-a-driver.md](docs/adding-a-driver.md).

## The Shelly climate admin panel

A dedicated `/climate-admin` page for redeploying the same two-zone
thermostat to a new home that standardizes on the
[Shelly](https://www.shelly.com/) device family - map each zone's
sensor/valve, the MQTT broker, and an outdoor sensor to that home's own
device ids from the browser, no `config.yaml` editing. Simulated/manual
test modes let the whole control loop be verified before any relay is
wired, and a live status view gives plain-language guidance instead of
raw error codes. See
[docs/configuration.md#the-shelly-climate-admin-panel](docs/configuration.md#the-shelly-climate-admin-panel)
for the full picture.

## Tested hardware

Driver *code* should work with any device of the right product
category/protocol version, but here's specifically what this project
runs against day to day:

| Device | Driver | Details |
|--------|--------|---------|
| LoraTap WiFi Roller Shutter Switch | `tuya_curtain_switch` | Tuya "Curtain switch" product (`product_id nnfrmb3val45av1n`, model `qcsc500wv2n2`), category `clkg`, tinytuya protocol v3.4. Several units running continuously. |
| A Tuya 9-in-1 air-quality/climate sensor | `tuya_air_quality` | Product family "9in1/12in1/14in1" (model `QTF-A+C`, `product_id b7alv1ety7u19i9d`), category `hjjcy`, tinytuya protocol v3.5. |
| Sonoff POWCT power-monitoring relay | `ewelink_powct` | Via the eWeLink cloud v2 API - see [docs/integrations/ewelink.md](docs/integrations/ewelink.md) for why this one isn't local. |
| Meross MSS310 smartplug (energy monitoring) | `meross_plug` | Via the official `meross-iot` library (HTTP login + MQTT), using its `ElectricityMixin` for live power/voltage/current. |
| Shelly H&T Gen3 (battery temperature/humidity sensor) | `mqtt_json` | Two topics (`status/temperature:0`, `status/humidity:0`) merged into one reading via a list `state_topic`, plus its `status/devicepower:0` (battery %) and `status/wifi` (signal strength) components - see [docs/integrations/mqtt.md](docs/integrations/mqtt.md). |
| Shelly Plus 1 (relay, mains-powered) | `mqtt_json` | The generic Gen2+ JSON-boolean shape (`status/switch:0`/`command/switch:0`) - used for both zone valves and the climate admin panel's two extra switches. |
| Shelly 0/1-10V Dimmer Gen3 | `shelly_light` | Doesn't publish a retained status topic like the devices above - actively polled via RPC (`Light.GetStatus`/`Light.Set`) instead; see "Shelly components without a retained status topic" in [docs/integrations/mqtt.md](docs/integrations/mqtt.md). |

If you run a driver against different hardware in the same product
family and it works (or doesn't), a PR adding a row here - or opening an
issue with the model/product_id - is genuinely useful to the next
person.

## How it's organized

```
config/            Your device inventories, secrets and config.yaml (gitignored, except *.example.*)
src/localhome/
  core/            Driver interfaces, the plugin registry, background pollers
  drivers/         One subpackage per brand/protocol (tuya, ewelink, meross, mqtt, simulated, notifiers, ...)
  services/        Cross-driver logic: cover position estimation, sqlite history + alerting, the heating/cooling thermostat, and its switch-follows-switch interlock
  web/             Flask app (+ optional Basic Auth), dashboard templates/static files, and locales/ (UI language files, see docs/configuration.md#language)
  config.py        Loads config.yaml and resolves its relative paths
  cli.py           Command-line cover control
docs/              Full documentation (see below)
tests/             Hardware-independent unit tests
deploy/            systemd unit example; see also Dockerfile/docker-compose.yml at the repo root
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how these pieces fit together
and why they're split this way.

## Documentation

- [docs/arduino-uno-q.md](docs/arduino-uno-q.md) - the reference installation: Arduino UNO Q, Shelly-only climate control, limits, tuning, updating
- [ARCHITECTURE.md](ARCHITECTURE.md) - the driver/registry/manager design and why it looks like this
- [docs/configuration.md](docs/configuration.md) - full config.yaml reference, including optional web auth
- [docs/adding-a-driver.md](docs/adding-a-driver.md) - how to add a new brand or device kind
- [docs/integrations/](docs/integrations/) - per-brand setup guides (Tuya, eWeLink, Meross, MQTT, Telegram, Simulated)
- [docs/deployment.md](docs/deployment.md) - Docker / systemd for running this unattended
- [docs/troubleshooting.md](docs/troubleshooting.md) - known error messages and fixes
- [CONTRIBUTING.md](CONTRIBUTING.md) - how to propose changes, coding conventions
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) - dependency licenses and a note on the eWeLink driver

## Why this exists

Cloud-dependent smart-home apps break when the vendor's server has a bad
day, get discontinued when the vendor loses interest, and generally cost
more (subscriptions, hub hardware) than the gadgets they control. Most
cheap WiFi devices already speak a local protocol under the hood -
projects like [tinytuya](https://github.com/jasonacox/tinytuya) simply
expose it. LocalHome is the smallest structure that lets several such
devices, from different brands, show up on one dashboard and share one
history/alerting story, while staying easy enough to read that adding
"one more sensor" stays a small change instead of a rewrite.

## Legal & licensing

LocalHome is [MIT licensed](LICENSE). Its dependencies are permissive
(MIT/BSD/Apache) except `python-kasa` (GPL-3.0-or-later) - it's only
pulled in if you opt into the `tplink_led_strip` driver's own
`[tplink]` extra, never by a plain install - see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md#a-note-on-python-kasa-specifically)
for what that means in practice. The `ewelink_powct` driver separately
relies on unofficial, reverse-engineered eWeLink app credentials (the
same pattern [AlexxIT/SonoffLAN](https://github.com/AlexxIT/SonoffLAN)
uses) - not sanctioned by Coolkit/eWeLink; skip that one integration if
that's not a tradeoff you want. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md#a-note-on-the-ewelink-driver-specifically)
for the full explanation either way.

This project is not affiliated with, endorsed by, or sponsored by Tuya,
LoraTap, Sonoff, Coolkit/eWeLink, Meross, or Telegram. All trademarks
belong to their respective owners.
