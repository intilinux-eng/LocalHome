# Quick installation: Arduino UNO Q

From a board fresh out of its box to an empty LocalHome running as a
service - started at boot, reachable by name, and usable end to end with
no devices installed yet. About 15 minutes, most of it waiting for
`apt` and `pip`.

This is the short, ordered path, written down as it was actually run on
a second board (Debian 13 trixie, aarch64, Python 3.13.5, 2 GB UNO Q)
and with the numbers that board reported at each step. The longer
explanations - what the board can take, how it behaves over the years,
how to configure each Shelly - are in
[arduino-uno-q.md](arduino-uno-q.md); the generic Linux/Docker guide is
[deployment.md](deployment.md).

**What you end up with**: an installation with no configuration at all -
no devices, no schedules, no history - that still starts, serves the
dashboard and can run a full two-zone thermostat on simulated sensors.
That makes it a usable test bench on day one, and the real apartment's
setup is then typed into the admin panel, never into a file.

## Before you start

- The board on your Wi-Fi, with its permanent name already set (Arduino
  App Lab does this at first boot). Everything below reaches it by that
  name - `carlucci` in the examples - never by its IP, because the
  router's DHCP will change the IP sooner or later.
- SSH into it: `ssh arduino@carlucci`, with the board's own password.
- A PC with this repository checked out. The board needs no GitHub
  account and no access to your repo host: the code travels as a single
  git bundle file (step 4).

Check the name resolves before anything else - if this fails, nothing
below will work:

```bash
ping carlucci          # answers with the board's current IP
ping carlucci.local    # the mDNS name, from a phone or another PC
```

## 1. Time zone

The board ships in UTC, and every schedule slot is picked from its
clock: an hour or two off, silently, for the whole heating season.

```bash
sudo timedatectl set-timezone Europe/Rome    # yours: timedatectl list-timezones
timedatectl                                  # -> "Time zone: Europe/Rome (CEST, +0200)"
```

The dashboard header shows the board's own clock and warns when it
disagrees with the phone looking at it, so a forgotten step here is
visible rather than silent.

## 2. Packages

```bash
sudo apt-get update
sudo apt-get install -y mosquitto python3.13-venv libnss-mdns
```

- `mosquitto` - the MQTT broker the Shelly devices will publish to. Install
  it now even if no device exists yet; it costs nothing idle.
- `python3.13-venv` - **needed**: the stock image's Python has no
  `ensurepip`, so `python3 -m venv` fails without it. (On another Debian
  release the package is named after that release's Python: apt's own
  error names the right one.)
- `libnss-mdns` - lets the board resolve other `.local` names.

`git`, `avahi-daemon` and `docker` are already on the UNO Q image;
avahi is what answers `carlucci.local`, and it is already running.

## 3. The MQTT broker's listener

Out of the box mosquitto listens on loopback only:

```bash
ss -ltn | grep 1883      # -> 127.0.0.1:1883 and [::1]:1883
```

That is enough for LocalHome itself, which connects to `localhost` - the
dashboard, the thermostat, the history and every simulated device work
fully with the broker exactly as installed. It is **not** enough for
real Shelly devices, which publish from elsewhere on the LAN and need
the broker to accept connections on the network interface.

Decide this consciously, because it is the one step here that exposes
something:

- **The reference installation's setting** - open to the LAN, no
  credentials. Simple, and what
  [arduino-uno-q.md](arduino-uno-q.md) documents; it means anyone on
  your Wi-Fi can read and publish MQTT on this board:

  ```bash
  printf 'listener 1883 0.0.0.0\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/localhome.conf
  sudo systemctl restart mosquitto
  ```

- **With a password** - the same listener, plus a user each Shelly is
  configured with (`mosquitto_passwd`), and the same credentials in
  `config/mqtt_broker.json` or in the admin panel's MQTT section. More
  typing at install time, one less open service on the LAN.

Leave it on loopback while there is no hardware: an empty installation
and a simulated one need nothing here.

## 4. The code

The repository travels as one file - no GitHub account on the board, no
network access to your repo host, and the board ends up with a real git
checkout it can be updated from later.

On the PC:

```bash
git bundle create localhome.bundle main        # ~6 MB, whole history
```

Copy it to the board (`scp localhome.bundle arduino@carlucci:~/`), then
on the board:

```bash
git clone --branch main ~/localhome.bundle ~/localhome
cd ~/localhome && git log --oneline -1         # the same commit as the PC
```

> The UNO Q's SFTP subsystem refuses writes (`ENOENT` on any path), so
> `scp`/`sftp` may fail on this board. Sending the file base64-encoded
> over a plain SSH exec channel works and is what these steps were run
> with: `base64 < localhome.bundle | ssh arduino@carlucci "base64 -d > ~/localhome.bundle"`,
> then compare `sha256sum` on both sides.

**To update later**: rebuild the bundle on the PC, overwrite the same
file on the board, and

```bash
cd ~/localhome && git pull --ff-only && sudo systemctl restart localhome
```

`git clone` from a bundle leaves that file as `origin`, so `git pull`
just works - no remote to configure.

## 5. Python environment

```bash
cd ~/localhome
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

78 MB of virtualenv, 108 MB for the whole checkout. The optional TP-Link
driver is deliberately left out (it is GPL - see
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)); LocalHome logs one
line about it at startup and carries on. Install it with
`.venv/bin/pip install -e .[tplink]` if you need Kasa/Tapo devices.

## 6. An empty configuration

The point of this installation is that it holds nothing from anywhere
else - no device ids, no keys, no schedules, no history. Only
`*.example` files are tracked in git, so the clone brings none of that
with it; these two files are all you create:

```bash
cd ~/localhome
cat > config/config.yaml <<'YAML'
web:
  host: "0.0.0.0"
  port: 5000
  language: "it"

integrations: []
YAML
printf '{\n  "host": "localhost",\n  "port": 1883\n}\n' > config/mqtt_broker.json
```

That is the whole file: no devices, no thermostat. Everything else is
added from the admin panel, which writes its own files next to these
(`climate_map.json`, `schedules.json`, `panel_settings.json`, ...).

Check it before starting anything:

```bash
.venv/bin/localhome-cli doctor    # -> "No configuration problems found."
```

## 7. The service

```bash
cd ~/localhome
sed "s/YOUR_USER/$USER/g" deploy/localhome.service > /tmp/localhome.service
sudo install -m 644 -o root -g root /tmp/localhome.service /etc/systemd/system/localhome.service
sudo systemctl daemon-reload
sudo systemctl enable --now localhome
systemctl is-active localhome     # -> active
```

The unit runs as the ordinary `arduino` user, restarts on failure, and
is hardened with `ProtectSystem=strict`: the only writable path is
`config/`, which is exactly where the history database and every runtime
file live.

> Over SSH there is no terminal for sudo's prompt. If you are scripting
> this, feed the password to `sudo -S` on stdin and keep `sudo` at the
> start of the command - a `sudo` in the middle of a pipeline prompts
> and hangs.

## 8. Tune the board

Required on every new board, and safe to run again at any time: it cuts
the flash writes that wear the eMMC out (measured elsewhere: ~7.5 GB/day
down to ~0.6 GB/day) by keeping the system log in RAM, stops the daily
background downloads, sends the broker's log to the journal and turns
off the graphical session.

```bash
sudo sh /home/arduino/localhome/deploy/tune-board.sh
```

Every step prints how to undo it. Add `--lean` to also turn off Arduino
App Lab, its Docker runtime and the firmware updater: ~270 MB of RAM and
~2.3 GB of disk back, worth it on the 2 GB board - but App Lab can no
longer be used on the board until they are turned back on.

## 9. Check it, including a reboot

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://carlucci.local:5000/    # 200, from your PC
```

- Dashboard: `http://carlucci.local:5000` - it opens on "no devices
  configured", which is correct for a fresh install.
- Admin panel: `http://carlucci.local:5000/climate-admin`.

Then prove the part that only matters later:

```bash
sudo systemctl reboot
```

The dashboard answered again 40 seconds after the reboot on this board,
with no intervention.

## 10. First run in the panel

- **Page name** (Settings) - what this installation calls itself, up to
  24 characters. It replaces "LocalHome" in the header, the browser tab
  and the name under the icon when the dashboard is added to a phone's
  home screen, and takes effect on the next page load.
- **Zones** - exactly two, and on a fresh installation both start
  **simulated** on purpose: there is no hardware yet, a real device id
  is required to save a "Shelly" zone, and a zone pointed at a device
  that doesn't exist reports errors forever. Name them, save, restart
  from the panel, and the thermostat, the weekly schedule, the history
  and the statistics all work against simulated sensors - the whole
  system, driveable on a bench.
- **Shelly devices** - switch a zone's sensor or valve to "Shelly" and
  paste the device id from the Shelly app, once the device is installed
  and reporting. The panel's per-zone status says whether it is actually
  being heard, and distinguishes "not reachable yet" (a battery sensor
  that hasn't woken up) from "was reporting, now unreachable".

## What it costs on the board

Measured on the 2 GB UNO Q right after this install:

| | |
|---|---|
| Checkout + virtualenv | 108 MB (78 MB of it the venv) |
| LocalHome, resident | 42 MB |
| Disk free afterwards | 2.7 GB of 9.8 GB |
| Reboot to dashboard | 40 s |
| History database, empty install | 416 KB |

## If something goes wrong

| What you see | What it is |
|---|---|
| `python3 -m venv` fails, `No module named 'ensurepip'` | `python3.13-venv` isn't installed - step 2 |
| `scp`/`sftp` fails with `ENOENT` on any path | the board's SFTP subsystem refuses writes; copy over the exec channel instead - step 4 |
| `sudo: a terminal is required to read the password` | a `sudo` in the middle of a piped command over SSH; put `sudo` first and use `sudo -S` |
| The clock in the dashboard header is wrong | step 1, or `timedatectl` says "synchronized: no" |
| A zone says "not reachable yet" forever | the broker is on loopback (step 3) or the device id is wrong - the panel's MQTT section says which |
| `localhome-cli doctor` lists a skipped driver | the optional TP-Link driver, not installed on purpose - step 5 |
