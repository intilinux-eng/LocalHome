# Meross (smartplug)

Covers the `meross_plug` (`switch`) driver, built on the
[meross-iot](https://github.com/albertogeniola/MerossIot) library
(HTTP login + a persistent MQTT connection). Tested against a Meross
MSS310 (energy-monitoring plug); any device meross-iot supports should
work, with power/voltage/current only showing up for models that expose
`ElectricityMixin`.

## Why this one isn't local

Meross plugs are controlled and read through Meross's cloud (HTTP login,
then MQTT for both status push and on/off commands). There's no
supported local protocol for the current generation of devices, so this
driver depends on Meross cloud availability for both reading and
control - unlike the Tuya covers, there's no LAN fallback to fall back
to here.

## 1. Configure the device file

```bash
cp config/meross_devices.example.json config/meross_devices.json
```

```json
{
  "name": "Desk Plug",
  "device_name": "Desk Plug"
}
```

`device_name` must match the device's exact name in the Meross app -
that's how it's looked up, so you don't need to dig a UUID out of the
account. If you'd rather pin it by UUID instead (e.g. because you rename
devices often), add `"uuid": "<device uuid>"` and drop `device_name`.

## 2. Configure credentials

```bash
cp config/secrets_meross.example.json config/secrets_meross.json
```

```json
{
  "email": "your-meross-account@email.com",
  "password": "YOUR_MEROSS_PASSWORD",
  "api_base_url": "https://iotx-eu.meross.com"
}
```

`api_base_url` depends on which regional Meross cloud your account was
created against - `iotx-eu`/`iotx-us`/`iotx-ap` are the current ones;
check the Meross app's account region if login fails.

## 3. Enable the driver

```yaml
integrations:
  - kind: switch
    name: "Desk Plug"   # optional, defaults to the devices_file's "name"
    type: meross_plug
    devices_file: "meross_devices.json"
    secrets_file: "secrets_meross.json"
```

If the plug reports live power (most Meross plugs with metering do),
`power_w`/`voltage_v`/`current_a` are recorded as history automatically
and shown on its dashboard card; plugs without metering just show on/off.

## Notes

- Polls every 20 seconds by default; on/off commands are submitted into
  the same persistent MQTT connection the poller uses, so they go out
  immediately rather than waiting for the next poll.
- The dashboard confirms before turning a plug off, since it's easy to
  forget what a "smart plug" is actually powering.
- Polling faster than every 20 seconds buys little: each read is two
  messages through Meross's cloud, which limits accounts that ask too
  often, and the home page shows the devices every 20 seconds anyway.

## Past days from the plug itself

A plug that counts its consumption (an MSS310: `ConsumptionXMixin`)
keeps about a month of daily kWh by itself - what the Meross app shows.
LocalHome reads them once a day (the first time as soon as the plug is
connected after a start) and writes each past day as the plug counted it,
over a day it had worked out from the power or missing: the days lost to
*Cancella storico*, or from before LocalHome followed the plug, come back
by themselves. Today stays LocalHome's own, from its samples.

Not to wait for the daily read after clearing the history by hand:

```bash
python tools/import_meter_history.py antonio      # or host:port
```

It asks the board (`POST /api/energy/import`), which reads with its own
credentials - nothing of them on the PC - and prints each meter's days
written and its week and month.
