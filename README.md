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

## What's included

| Kind          | Driver                | Talks to                          | Local? |
|---------------|------------------------|------------------------------------|--------|
| `cover`       | `tuya_curtain_switch`  | Tuya-based curtain switches (e.g. LoraTap WiFi roller shutter modules) | Yes (LAN) |
| `climate`     | `tuya_air_quality`     | Tuya 9-in-1/12-in-1 air-quality sensors | Yes (LAN) |
| `power_meter` | `ewelink_powct`        | Sonoff POWCT power-monitoring relay | No - eWeLink cloud only, see [docs/integrations/ewelink.md](docs/integrations/ewelink.md) |
| `switch`      | `meross_plug`          | Meross smartplugs                  | No - Meross cloud (MQTT), see [docs/integrations/meross.md](docs/integrations/meross.md) |
| `cover` / `climate` / `power_meter` / `switch` / `number` | `mqtt_json` | **Generic** - any device that publishes JSON (or plain-string) state over MQTT: Shelly, Tasmota, Zigbee2MQTT, ESPHome, DIY sketches... | Yes (LAN, to your own broker) - see [docs/integrations/mqtt.md](docs/integrations/mqtt.md) |
| `notifier`    | `telegram`             | Telegram bot API                   | Cloud by nature (push notifications) |

`number` is a controllable analog value - a dimmer, a fan speed, a 0-10V
output - for anything set to a level rather than toggled or driven
open/closed.

The web dashboard renders whatever combination of these you have
enabled, plus estimated 0-100% position for covers that only report a
raw open/close/stop motor command, plus a sqlite-backed history/chart for
any numeric field a driver exposes, plus an optional power-budget alert,
plus optional HTTP Basic Auth if you want it (off by default).

Don't own any of the devices above? The point of this project is that
adding your own driver is a self-contained, half-hour change - see
[docs/adding-a-driver.md](docs/adding-a-driver.md).

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

If you run a driver against different hardware in the same product
family and it works (or doesn't), a PR adding a row here - or opening an
issue with the model/product_id - is genuinely useful to the next
person.

## Quick start

```bash
git clone <this-repo>
cd <the-cloned-directory>
pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml
# also copy the *.example.json files you need next to it and fill them in -
# see docs/configuration.md and docs/integrations/ for how to get each
# device's IDs/keys/credentials.

python run.py
# -> dashboard at http://localhost:5000
```

Nothing under `config/` other than the `*.example.*` files is ever meant
to be committed - see [.gitignore](.gitignore). That's what keeps your
own device inventory, API keys and account passwords out of git while
this framework itself stays fully public.

Prefer an editable install instead of `run.py`? `pip install -e .` gives
you the `localhome` and `localhome-cli` commands.

Want this running unattended on a dedicated device (a Raspberry Pi or
similar next to your router) instead of a dev machine? See
[docs/deployment.md](docs/deployment.md) for Docker and systemd options.

## How it's organized

```
config/            Your device inventories, secrets and config.yaml (gitignored, except *.example.*)
src/localhome/
  core/            Driver interfaces, the plugin registry, background pollers, config loading
  drivers/         One subpackage per brand/protocol (tuya, ewelink, meross, mqtt, notifiers, ...)
  services/        Cross-driver logic: cover position estimation, sqlite history + alerting
  web/             Flask app (+ optional Basic Auth) and the dashboard's templates/static files
  cli.py           Command-line cover control
docs/              Full documentation (see below)
tests/             Hardware-independent unit tests
deploy/            systemd unit example; see also Dockerfile/docker-compose.yml at the repo root
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how these pieces fit together
and why they're split this way.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) - the driver/registry/manager design and why it looks like this
- [docs/configuration.md](docs/configuration.md) - full config.yaml reference, including optional web auth
- [docs/adding-a-driver.md](docs/adding-a-driver.md) - how to add a new brand or device kind
- [docs/integrations/](docs/integrations/) - per-brand setup guides (Tuya, eWeLink, Meross, MQTT, Telegram)
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

LocalHome is [MIT licensed](LICENSE). All of its dependencies (tinytuya,
Flask, aiohttp, meross-iot, requests, PyYAML, paho-mqtt) are under
permissive licenses with no copyleft obligations - see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the full list.

One exception worth knowing about explicitly: the `ewelink_powct` driver
talks to eWeLink's cloud using unofficial, reverse-engineered app
credentials (the same pattern the well-known
[AlexxIT/SonoffLAN](https://github.com/AlexxIT/SonoffLAN) project uses) -
not something Coolkit/eWeLink issued to this project or sanctioned. It's
common practice in this space, but it's not a license grant from them,
and skipping that one integration keeps everything else on unambiguous
footing (your own Tuya/Meross developer accounts, standard SDK usage).
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md#a-note-on-the-ewelink-driver-specifically)
for the full explanation.

This project is not affiliated with, endorsed by, or sponsored by Tuya,
LoraTap, Sonoff, Coolkit/eWeLink, Meross, or Telegram. All trademarks
belong to their respective owners.
