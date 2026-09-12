# MQTT (generic - Shelly, Tasmota, Zigbee2MQTT, ESPHome, ...)

Covers the `mqtt_json` driver, registered under all four device kinds
(`cover`, `climate`, `power_meter`, `switch`) plus `number` (a
controllable analog value - a dimmer, a fan speed, a 0-10V output). One
driver, one JSON-path-mapping convention, no brand-specific code: if a
device publishes its state as JSON over MQTT and accepts commands the
same way - which covers most of the DIY/self-hosted smart-home world
(Shelly, Tasmota, Zigbee2MQTT, ESPHome, a handful of lines of a
microcontroller sketch) - it plugs in through config alone.

## How it works

Every `mqtt_json` entry:
1. Subscribes to `state_topic` and keeps the latest JSON payload received.
2. Maps whatever fields you list in `fields:` (or `value_field:` for a
   `number`) from that payload into a reading, using a dot path for
   nested JSON (`"ENERGY.Power"` reaches into `{"ENERGY": {"Power": ...}}`).
3. For controllable kinds (`switch`, `number`, `cover`), publishes to
   `command_topic` on request - either the raw value, or a small JSON
   object if `command_payload_field` is set (some ecosystems, notably
   Zigbee2MQTT, expect `{"state": "ON"}` rather than a bare `"ON"`).

Multiple `mqtt_json` entries pointed at the same `broker_file` share one
real MQTT connection - see `drivers/mqtt/client.py`.

## 1. Configure the broker connection

```bash
cp config/mqtt_broker.example.json config/mqtt_broker.json
```

```json
{
  "host": "192.168.1.5",
  "port": 1883,
  "username": "mqtt_user",
  "password": "mqtt_password"
}
```

Omit `username`/`password` for an anonymous-access broker. Every
`mqtt_json` integration below references this same file via
`broker_file`.

## 2. Examples

### A Tasmota smartplug (`power_meter`)

Tasmota's `SENSOR` telemetry nests everything under `"ENERGY"`:

```yaml
- kind: power_meter
  type: mqtt_json
  name: "Workshop Plug"
  broker_file: "mqtt_broker.json"
  state_topic: "tele/tasmota_workshop/SENSOR"
  fields:
    power_w: "ENERGY.Power"
    voltage_v: "ENERGY.Voltage"
    current_a: "ENERGY.Current"
```

(Tasmota's on/off relay state is a separate topic/driver instance if you
also want it as a `switch` - see below; a plug that's always-on and only
metered doesn't need one.)

### A Zigbee2MQTT device - climate sensor

Zigbee2MQTT publishes one flat JSON payload per device on
`zigbee2mqtt/<friendly_name>`:

```yaml
- kind: climate
  type: mqtt_json
  name: "Shed Sensor"
  broker_file: "mqtt_broker.json"
  state_topic: "zigbee2mqtt/Shed Sensor"
  fields:
    temperature_c: "temperature"
    humidity_pct: "humidity"
    battery_pct: "battery"
```

### A Zigbee2MQTT smartplug (`switch`)

Commands go to `<topic>/set` as a small JSON object:

```yaml
- kind: switch
  type: mqtt_json
  name: "Living Room Lamp"
  broker_file: "mqtt_broker.json"
  state_topic: "zigbee2mqtt/Living Room Lamp"
  command_topic: "zigbee2mqtt/Living Room Lamp/set"
  command_payload_field: "state"    # sends {"state": "ON"/"OFF"}
  fields:
    power_w: "power"
```

### A Shelly plug, native MQTT status/command topics

Shelly's exact topic layout and payload format differs by generation and
firmware version - always confirm yours with the Shelly app/web UI or a
`mosquitto_sub -t '#' -v` while you toggle it by hand, rather than
trusting a snippet verbatim. Two shapes you'll likely see:

**JSON status, boolean field** (a common Gen2+ shape) - note
`payload_on`/`payload_off` are written *unquoted* below so PyYAML parses
them as real booleans, matching a JSON `true`/`false` in the payload
rather than the string `"true"`:

```yaml
- kind: switch
  type: mqtt_json
  name: "Desk Plug"
  broker_file: "mqtt_broker.json"
  state_topic: "shellyplus1pm-XXXXXX/status/switch:0"
  command_topic: "shellyplus1pm-XXXXXX/command/switch:0"
  state_field: "output"
  payload_on: true
  payload_off: false
  fields:
    power_w: "apower"
```

**Plain string payload, no JSON** (Gen1's `shellies/<id>/relay/0`
scheme): the payload isn't JSON at all, so `state_field: ""` (an empty
path means "the raw payload, as-is" - see `drivers/mqtt/paths.py`) reads
the bare `"on"`/`"off"` string directly, and `fields: {}` means there's
nothing else to extract from it:

```yaml
- kind: switch
  type: mqtt_json
  name: "Desk Plug"
  broker_file: "mqtt_broker.json"
  state_topic: "shellies/desk-plug/relay/0"
  command_topic: "shellies/desk-plug/relay/0/command"
  state_field: ""
  payload_on: "on"
  payload_off: "off"
  fields: {}
```

### A dimmer / analog output (`number`)

This is the shape a future 0-10V dimmer (e.g. bridged through a Shelly
add-on) would use - see `core/interfaces.py`'s `NumberDriver`:

```yaml
- kind: number
  type: mqtt_json
  name: "VMC Dimmer"
  broker_file: "mqtt_broker.json"
  state_topic: "shellies/vmc-dimmer/status"
  command_topic: "shellies/vmc-dimmer/set"
  value_field: "brightness"
  command_payload_field: "brightness"
  min_value: 0
  max_value: 100
  unit: "%"
```

The dashboard renders this as a slider card; dragging it calls
`POST /api/sensors/<name>/set-value`, clamped to `min_value`/`max_value`
before anything is published.

### A generic MQTT cover

```yaml
- kind: cover
  type: mqtt_json
  name: "MQTT Shutter"
  broker_file: "mqtt_broker.json"
  state_topic: "shellies/shutter1/status"
  command_topic: "shellies/shutter1/set"
  # payload_open/payload_close/payload_stop default to "open"/"close"/"stop"
```

Like the Tuya covers, this only reports/sends the raw motor command -
`services/position_control.py` estimates 0-100% position the same way
for any brand.

## Reference: all `mqtt_json` options

| Option | Applies to | Meaning |
|---|---|---|
| `broker_file` / `broker` | all | connection details (file path or inline dict) |
| `name` | all | required - dashboard/API/CLI name |
| `state_topic` | all | topic to subscribe to |
| `fields` | cover excluded | `{reading_field: "dotted.path"}` |
| `command_topic` | switch, number, cover | topic commands are published to |
| `command_payload_field` | switch, number, cover | wrap the command value as `{field: value}` JSON instead of sending it raw |
| `state_field` | switch, cover | dotted path to the on/off or open/close/stop state (default `"state"`); use `""` if the payload isn't JSON at all - see the Shelly Gen1 example above |
| `payload_on` / `payload_off` | switch | values meaning on/off (default `"ON"`/`"OFF"`) - write these unquoted (`true`/`false`) in config.yaml if the device reports a real JSON boolean rather than a string |
| `payload_open` / `payload_close` / `payload_stop` | cover | values meaning each action (default `"open"`/`"close"`/`"stop"`) |
| `value_field` | number | dotted path to the current value (default `"value"`); also supports `""` for a non-JSON payload |
| `min_value` / `max_value` / `unit` | number | range shown on the dashboard slider and clamped against |
| `id` | cover | stable id for position tracking (default: `name`) |
| `history_fields` | sensor kinds | subset of `fields` worth recording (default: all of them) |
| `poll_interval_seconds` | all | how often the cached MQTT value is snapshotted into a reading/history row (default 20s - this doesn't trigger new network traffic, MQTT already pushes) |
