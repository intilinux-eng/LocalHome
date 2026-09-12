# eWeLink (Sonoff POWCT power meter)

Covers the `ewelink_powct` (`power_meter`) driver, for the Sonoff POWCT
power-monitoring relay.

## Why this one isn't local

Every other driver in this project talks to devices on your LAN. This one
doesn't, and it's worth explaining why rather than pretending otherwise:
LAN-only control was tried first (listening for the device's mDNS
broadcasts, the same approach the Tuya covers use), but this device's
updates never arrived reliably after the first one - a known limitation
of the community LAN libraries for this device/firmware. eWeLink's basic
cloud device-list endpoint, on the other hand, is refreshed by the device
every ~20-30 seconds even without a push subscription, so this driver
polls that instead. Only this one sensor depends on eWeLink cloud
availability; the rest of the dashboard is unaffected if it's down.

## 1. Find your device_id

```bash
cp config/secrets_ewelink.example.json config/secrets_ewelink.json
# edit it with your eWeLink app's login email/phone + password + region

python tools/ewelink_discover.py config/secrets_ewelink.json
```

This logs in and prints every device on your account with its
`device_id` and raw params - copy the `device_id` for your POWCT.

`region`/`country_code` should match the account/phone country you
registered the eWeLink app with (e.g. `"eu"`/`"+39"` for an Italian
account, `"us"`/`"+1"` for a US one).

## 2. Configure the device file

```bash
cp config/ewelink_devices.example.json config/ewelink_devices.json
```

```json
{
  "device_id": "<device_id from step 1>",
  "name": "Home Power Meter"
}
```

## 3. Enable the driver

```yaml
integrations:
  - kind: power_meter
    name: "Home Power Meter"   # optional, defaults to the devices_file's "name"
    type: ewelink_powct
    devices_file: "ewelink_devices.json"
    secrets_file: "secrets_ewelink.json"
```

`power_w`, `voltage_v`, `current_a`, `day_kwh`, `month_kwh` and
`yesterday_kwh` are all recorded as history automatically (`power_w`,
`voltage_v`, `current_a` specifically); to wire up the power-budget alert
described in [docs/configuration.md](../configuration.md#energy-power-budget-alerting),
set `energy.sensor` to this integration's `name`.

## Notes

- Polls every 20 seconds by default (`poll_interval_seconds` option).
- Logging in on every process start is normal; the login itself is
  cached for the process's lifetime, not re-done every poll.
- If you see `login failed`, double check `region`/`country_code` first
  - a mismatched country code is the most common cause.
