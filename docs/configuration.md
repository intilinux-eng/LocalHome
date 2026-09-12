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
| `notifier`    | `telegram`            | [integrations/telegram.md](integrations/telegram.md) |

`number` is a controllable analog value (a dimmer, a fan speed, a 0-10V
output) - see `core/interfaces.py`'s `NumberDriver`. It's currently only
implemented by the generic MQTT driver; nothing stops a future
brand-specific one from implementing it too.

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

## Adding your own config keys

If your driver needs an option `config.yaml` doesn't have a name for
yet, just add it under that integration's entry - every key besides
`kind` and `type` is forwarded verbatim to the driver's factory as a
plain dict. Nothing needs to be declared elsewhere.
