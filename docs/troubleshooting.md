# Troubleshooting

Per-brand connection issues (Tuya cloud project errors, eWeLink login
failures, Meross region mismatches) are covered in their own
[docs/integrations/](integrations/) pages. This page covers the rest.

**`error: externally-managed-environment`, or `python3 -m venv` fails with "ensurepip is not available"**
Two separate speed bumps on modern Debian/Ubuntu (hit setting this project
up on its own Arduino UNO Q instance, Debian trixie) that look unrelated
but share one fix. The distro blocks installing Python packages
system-wide, and often doesn't ship the venv module by default - install
that first (the exact package name varies; try the generic one, then the
version-suffixed one your own error message names):
```bash
sudo apt install -y python3-venv || sudo apt install -y python3.13-venv
```
Then always install into a virtual environment, never with a bare `pip
install` against the system Python:
```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]" -c constraints.txt     # or: pip install -r requirements.txt
```
A venv's own `pip` is never "externally managed", so this sidesteps the
restriction entirely instead of fighting it with `--break-system-packages`
(which the error message itself offers, but risks whatever else the
system's own Python is used for). See
[docs/deployment.md](deployment.md#bare-metal-systemd) for the systemd
unit that keeps this running after you close the SSH session, instead of
dying with it.

**Every MQTT device stopped reporting at once, e.g. after a router reboot**
The board probably got a new IP address from the router's DHCP, and the
devices (or `config/mqtt_broker.json`) still point at the old one.
Check the board's current address with `hostname -I`, then move every
link to names so it can't happen again: `"host": "localhost"` in
`mqtt_broker.json`, and `<name>.local:1883` as each device's MQTT
server - see
[docs/deployment.md#addressing-names-not-ip-addresses](deployment.md#addressing-names-not-ip-addresses).
If the devices already use the name, check the name is still
announced: `systemctl status avahi-daemon` and `journalctl -u
avahi-daemon | grep -iE "host name is|conflict"` on the board, then
`ping <name>.local` from a PC (not from the board itself - its own
resolver may not look up its own `.local` name).

**`FileNotFoundError: Config file not found: .../config/config.yaml`**
You haven't created your own config yet:
```bash
cp config/config.example.yaml config/config.yaml
```
Then edit it and add the `devices_file`/`secrets_file` JSON files it
points at - see [docs/configuration.md](configuration.md).

**A driver fails to start but the rest of the dashboard works**
By design - `DeviceManager` logs the exception and skips that one
integration rather than crashing the whole app. Check the console output
for a traceback naming the failing `kind`/`type`, then check that
integration's `docs/integrations/*.md` page.

**`ValueError: No driver registered for kind=... type=...`**
Typo in `config.yaml`'s `kind`/`type`, or you're trying to use a driver
that doesn't exist in this checkout. List what's actually registered:
```bash
python -c "from localhome.core.registry import known_drivers; print(known_drivers())"
```

**Port 5000 already in use**
Set `web.port` in `config.yaml` to something else, or stop whatever else
is using it (5000 is also macOS's AirPlay Receiver port on some
versions).

**A cover's position estimate is wrong after a power cut / manual pull**
Expected - position is a time-based estimate, not a real sensor reading
(see [ARCHITECTURE.md](../ARCHITECTURE.md)). Move it fully open or fully
closed once via the dashboard/CLI to recalibrate; that's an exact
0%/100% regardless of what the estimate said before.

**A device that should follow another one (`interlocks:`) doesn't seem to react**
Check `active_in_mode` on that interlock entry - if it's set (e.g.
`"cool"`), the rule is deliberately a no-op in every other mode, leaving
the follower switch entirely to whatever else normally drives it (see
[docs/configuration.md](configuration.md#interlocks-one-switch-forced-to-follow-another)).
This is the single most common "it's not a bug" surprise with this
feature - a dehumidifier interlock scoped to `cool` genuinely does
nothing while the thermostat is in `heat` mode, on purpose. Check the
current mode (Settings, or `GET /api/climate/zones`) before assuming
something's broken.

**A zone's target/valve status looks stale right after editing the schedule or switching mode**
Should self-correct within one `poll_interval_seconds` at most - both
`/api/climate/zones/<name>/schedule` and `/api/climate/mode` already
trigger an immediate re-evaluation, not just a write to disk. If it's
still stale after that, check the browser console/network tab for a
failed request rather than assuming the control loop itself is wrong.

**I accidentally committed something under `config/`**
```bash
git rm --cached config/the-file-you-committed
git commit -m "Remove accidentally committed local config"
```
Then rotate whatever credential was in it (change the password/token) if
it was a secrets file - a `git rm` only stops *future* commits from
having it; it's still in history until you rewrite history, which is a
much bigger hammer.
