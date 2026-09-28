# TP-Link Kasa/Tapo (LED strips)

Covers the `tplink_led_strip` (`switch` + `number`) drivers, built on
[python-kasa](https://github.com/python-kasa/python-kasa). Controlled and
polled entirely over the LAN - no cloud dependency at runtime, even for
Tapo-branded devices (their local protocol still needs the TP-Link account
password to authenticate the connection, but the traffic itself never
leaves your network).

`python-kasa` auto-detects which of TP-Link's two local protocols a device
speaks, so one driver covers both older Kasa-branded strips/bulbs and
newer Tapo-branded ones.

Unlike this project's other dependencies, `python-kasa` is
GPL-3.0-or-later, not a permissive license - which is why, unlike
every other driver's SDK, it's **not** installed by default: it lives
in its own `tplink` extra (`pip install -e ".[tplink]" -c constraints.txt`), so nothing
GPL-licensed lands in your environment unless you actually use this
driver. See
[THIRD_PARTY_NOTICES.md#a-note-on-python-kasa-specifically](../../THIRD_PARTY_NOTICES.md#a-note-on-python-kasa-specifically)
for what that license means in practice before opting in.

## 0. Tapo devices on recent firmware: enable "Third-Party Compatibility" first

Tapo devices updated to a recent firmware (confirmed on an L900 light
strip) default to a newer local-auth scheme (`TPAP`) that **no version of
python-kasa supports yet** ([tracking issue](https://github.com/python-kasa/python-kasa/issues/1590)).
Symptom: `tools/tplink_discover.py` finds the device's IP, but logs it as
an "unsupported device" instead of showing its alias/model.

Fix: in the Tapo app, tap **Me** (the account tab - bottom-right or
top-left icon depending on app version) → **Servizi di terze parti /
Third-Party Services** → enable **Compatibilità con dispositivi di terze
parti / Third-Party Compatibility**. It's an account-level setting, not
buried in each device's own settings page, which is easy to miss - if you
don't see it at all, update the Tapo app first, it's a fairly recent
addition. This drops the device back to the (supported) `KLAP` scheme;
re-run `tools/tplink_discover.py` to confirm it now shows a real
alias/model instead of "unsupported".

Kasa-branded devices and older Tapo firmware don't need this step.

## 1. Find each strip's IP

```bash
pip install -e ".[tplink]" -c constraints.txt   # pulls in python-kasa - see the license note above
python tools/tplink_discover.py
```

This broadcasts on the LAN and lists every TP-Link device found with its
IP/alias/model. If a strip needs Tapo credentials to fully identify itself
(see step 2), pass them: `python tools/tplink_discover.py --username ... --password ...`.

If nothing shows up (Wi-Fi client isolation or AP/VLAN separation can
block LAN broadcasts), open the Kasa or Tapo app, tap the device, and read
its IP from the device info screen instead - or check your router's DHCP
client list for its hostname/MAC.

A static IP or DHCP reservation for each strip is worth setting up, the
same way you'd want for any device this driver polls by IP.

## 2. Configure credentials (Tapo devices only)

Kasa-branded devices (older TP-Link app, no per-device login) work with
just a `host` - skip this step. Tapo-branded devices (newer app) need the
TP-Link account they're registered to:

```bash
cp config/secrets_tplink.example.json config/secrets_tplink.json
```

```json
{
  "username": "your-tplink-account@email.com",
  "password": "YOUR_TAPO_PASSWORD"
}
```

One secrets file can be shared by both strips if they're on the same
account - point both integration entries at it.

## 3. Enable the driver

One `switch` entry per strip for on/off. Optionally add a `number` entry
too for a brightness slider on the dashboard - **give it a different
`name`** than the switch entry for the same strip: `DeviceManager` keys
its poller/sensor-kind lookup by name across every kind (switch, number,
cover, ...), so two entries sharing a name silently overwrite each
other's dashboard card, no matter their `kind`. A startup log warning
flags this if it happens.

```yaml
integrations:
  - kind: switch
    name: "Striscia LED Salotto"
    type: tplink_led_strip
    host: "192.168.1.60"
    secrets_file: "secrets_tplink.json"   # omit for Kasa-branded devices

  - kind: number
    name: "Striscia LED Salotto Luminosità"   # must differ from the switch's name above
    type: tplink_led_strip
    host: "192.168.1.60"
    secrets_file: "secrets_tplink.json"

  - kind: switch
    name: "Striscia LED Camera"
    type: tplink_led_strip
    host: "192.168.1.61"
    secrets_file: "secrets_tplink.json"
```

Without the `number` entry, current brightness still shows up read-only
as an extra field on the switch's card.

## When the router gives a strip a new address

You don't need a fixed IP (a DHCP reservation) for the strips. `host` is
only where LocalHome looks first: every time a strip answers, its MAC -
which never changes - is remembered with the address that worked
(`config/tplink_addresses.json`). When it stops answering there - a
restarted router hands out new addresses - LocalHome asks the LAN where
that MAC is now (at most every 5 minutes, so a strip that's switched off
doesn't cause a scan per poll), uses the new address and remembers it
across restarts. `config.yaml` itself is never rewritten.

That needs the strip to have answered once. For a strip that may move
before LocalHome first reaches it, give its MAC too (on the box, or in
the Tapo app under Device info):

```yaml
    host: "192.168.1.60"
    mac: "78:20:51:53:B5:CD"
```

## Notes

- Each read/command is a fresh short LAN request (no persistent
  connection to keep alive), so a strip being briefly offline just shows
  as an error on that one poll, not a driver crash.
- Polls every 20 seconds by default (`poll_interval_seconds` option); a
  toggle/slider action updates the dashboard's cached reading immediately
  on success rather than waiting for the next poll, so the switch/slider
  position doesn't visually snap back to a stale state in the meantime.
