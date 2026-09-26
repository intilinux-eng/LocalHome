# Deployment

`python run.py` is fine for trying LocalHome out. For something that
should survive a reboot and keep running unattended - an
[Arduino UNO Q](https://www.arduino.cc/), a Raspberry Pi, or any other
small Linux board next to your router is the typical target - pick one
of these. The Arduino UNO Q runs a real Linux userspace (SSH, bash,
systemd - not a microcontroller-only board), so everything below -
Docker and bare-metal/systemd alike - applies to it exactly as
written; no board-specific steps.

> **Arduino UNO Q, climate only with Shelly devices?** Follow
> [arduino-uno-q.md](arduino-uno-q.md) instead: the same steps, in the
> order that board needs, with its limits and tuning explained.
>
> **In a hurry, on a brand-new UNO Q?**
> [quick-installation-arduino-uno-q.md](quick-installation-arduino-uno-q.md)
> is the same path with nothing but the commands, ending at an empty
> installation that already runs on simulated devices.

## Quick start: a fresh Debian/Ubuntu box, start to finish

The single ordered path from a brand-new box to a running,
survives-a-reboot dashboard, MQTT broker included. This is plain
Debian/Ubuntu, whatever board it's running on - an
[Arduino UNO Q](https://www.arduino.cc/)'s Linux userspace *is* Debian
trixie, a Raspberry Pi's is Raspberry Pi OS (Debian-based), and so on,
so there's nothing board-specific below. This is what to follow the
first time, skipping straight to [Docker](#docker) or the more
hardened [dedicated-user bare-metal setup](#bare-metal-systemd) below
only if you already know you want one of those instead. Confirmed
against a real Arduino UNO Q (Debian trixie).

```bash
# 1. On the device itself (SSH in first if it's headless). mosquitto
#    is only needed if any of your devices speak MQTT (Shelly, Tasmota,
#    Zigbee2MQTT, ...) - include it now if so, `sudo apt remove
#    mosquitto` later if you decide you don't need it after all:
sudo apt update
sudo apt install -y git mosquitto python3-venv || sudo apt install -y git mosquitto python3.13-venv
# (the exact venv package name varies by distro version - if the first
#  one 404s, apt's own error names the right one; see below)

#    Set the home's timezone. Fresh images - the Arduino UNO Q's
#    included - default to UTC, and the thermostat picks each hour's
#    target from the system clock, so a UTC board runs every schedule
#    slot 1-2h off (confirmed on a real UNO Q). The dashboard's header
#    clock shows the system's time and warns when it disagrees with
#    your phone/PC, so a missed step here is visible, not silent:
sudo timedatectl set-timezone Europe/Rome   # your own zone - `timedatectl list-timezones`
timedatectl                                  # -> "Time zone: Europe/Rome", "synchronized: yes"

#    Give the board its permanent name now, before any device is
#    pointed at it: from here on your browser and every device reach it
#    by this name, never by its IP, so the router's DHCP can change the
#    IP freely. Choose it once - renaming later means reconfiguring
#    every device. Read "Addressing: names, not IP addresses" below:
sudo hostnamectl set-hostname localhome            # your choice; unique on the LAN
sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tlocalhome/" /etc/hosts
sudo apt install -y avahi-daemon libnss-mdns       # announces it as localhome.local
sudo systemctl restart avahi-daemon
journalctl -u avahi-daemon | grep "Host name is" | tail -1   # -> "Host name is localhome.local"
#    ...and from your PC/phone: ping localhome.local -> answers with the
#    board's current IP. (Don't test the .local name from the board
#    itself: its own resolver may not look up its own .local name, while
#    every other device does. LocalHome doesn't need it - it uses
#    localhost.)

# 2. Get the code and its dependencies - a normal user account is fine,
#    no dedicated system user needed for a single-purpose device:
git clone <this-repo> ~/localhome
cd ~/localhome
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
#    Some drivers need an optional extra on top - only if you use them,
#    e.g. TP-Link Kasa devices: .venv/bin/pip install -e .[tplink]
#    (GPL-licensed, hence opt-in - see THIRD_PARTY_NOTICES.md). Step 5's
#    doctor names any extra a configured device is missing.

# 3. Using MQTT devices? Point mosquitto at your LAN instead of just
#    localhost (apt's package already enabled its systemd unit, so it
#    already survives a reboot - this just gives it LocalHome's own
#    settings instead of the untouched default, which only listens on
#    localhost and refuses every device on your LAN):
printf 'listener 1883\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/localhome.conf
sudo systemctl restart mosquitto
# (no address after the port: every interface, IPv4 and IPv6)
# -> confirm it's listening and accepting messages:
#    mosquitto_sub -h localhost -t '#' -v      (Ctrl+C to stop)
# `allow_anonymous true` is fine for a broker only reachable on your
# own trusted LAN with nothing else exposed - see
# docs/integrations/mqtt.md#1-configure-the-broker-connection for a
# username/password setup instead.

# 4. Configure LocalHome itself - either by hand:
cp config/config.example.yaml config/config.yaml
#    (Heating/cooling with Shelly devices only? Copy
#    config/config.climate.example.yaml instead - see "A new apartment
#    (climate only)" below.)
#    ...and the *.example.json files you need; see docs/configuration.md
#    and docs/integrations/ - or, if every device is Shelly, skip this
#    and use the climate admin panel instead (step 6).
#    MQTT: config/mqtt_broker.json's "host" is "localhost" - mosquitto
#    runs on this same board (step 3), so LocalHome reaches it with no
#    IP address at all. Devices use the board's name instead, see
#    "Addressing: names, not IP addresses" below.

# 5. Sanity-check before trusting it to run unattended:
.venv/bin/localhome-cli doctor

# 6. Make LocalHome itself survive a reboot and closing the SSH session
#    - see deploy/localhome.service (copy it, then edit
#    User/WorkingDirectory/ExecStart to match the path you cloned into
#    above):
sudo cp deploy/localhome.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now localhome
sudo systemctl status localhome
# -> dashboard at http://localhome.local:5000 (the name from step 1)
#    logs: sudo journalctl -u localhome -f

# 7. Tune the board for years of unattended running - on its eMMC
#    (system log in RAM, no background update downloads, no desktop) and
#    on Wi-Fi (never dozing or giving up, a Wi-Fi watchdog, the hardware
#    watchdog, LocalHome restarted if it hangs). Don't skip it on an
#    Arduino UNO Q, and run it again after updates: see "Tune the board"
#    below and docs/quick-installation-arduino-uno-q.md step 8.
sudo sh deploy/tune-board.sh
```

If step 1's `python3-venv` package isn't found, or step 2's `python3 -m
venv .venv` itself fails with "ensurepip is not available", your
distro named the package differently - install the exact one apt's own
error message names (Debian trixie wants `python3.13-venv`) and retry.
See [docs/troubleshooting.md](troubleshooting.md) if anything else about
steps 1-5 goes sideways.

**Using Shelly devices** (the climate admin panel, `/climate-admin`)?
Map your zones onto this home's actual Shelly device ids from
`/climate-admin` once the dashboard is up (step 6's URL) - see
[docs/configuration.md#the-shelly-climate-admin-panel](configuration.md#the-shelly-climate-admin-panel).
Remember it needs a restart (`sudo systemctl restart localhome`) after
every save before a change actually takes effect.

## A new apartment (climate only)

When LocalHome only runs the heating/cooling with Shelly devices - the
usual case for a new apartment - start from the climate-only example
instead of `config.example.yaml` in quick start step 4:

```bash
cp config/config.climate.example.yaml config/config.yaml
cp config/mqtt_broker.example.json config/mqtt_broker.json   # "host": "localhost"
```

It has no devices at all: no Tuya, Meross, eWeLink or TP-Link entries to
remove, nothing that fails to load. Every device is added from
`/climate-admin` by its Shelly id - the two zones (H&T sensor + relay
valve), the bathroom bypass valve, the dehumidifier, the outdoor sensor
and the VMC - and each Shelly's MQTT server is set to
`<board-name>.local:1883` (see
[Addressing](#addressing-names-not-ip-addresses)).

**A device that's missing, switched off or unreachable never stops the
rest.** Tested with every device of the configuration unreachable, and
with the MQTT broker itself down:

- its card and the admin panel's system status show it as not
  responding; nothing else changes;
- the thermostat leaves a zone without a sensor reading alone, and after
  `sensor_failsafe_hours` (24 by default) closes its valve;
- a zone with no schedule for the current hour keeps its valve closed;
- a device named in a season rule (`season_rules`, `mode_switches`,
  `mode_limited_switches`) that the
  apartment doesn't have (no bypass valve wired, say) is noted once in
  the log and skipped;
- memory, threads and the control loops stay flat - see the admin
  panel's system status on the board.

## Updating LocalHome on the board

However the new code gets there - `git pull`, or a copy of the repo from
your PC - always reinstall and restart afterwards:

```bash
cd ~/localhome
.venv/bin/pip install -e .           # picks up new or changed dependencies
.venv/bin/localhome-cli doctor       # optional sanity check
sudo systemctl restart localhome
```

Skipping the `pip install` works until an update adds a dependency - then
the service fails at startup with a `ModuleNotFoundError` (e.g.
`No module named 'waitress'`) and systemd keeps restarting it. Your
`config/` files are never touched by an update.

The dashboard runs on [Waitress](https://github.com/Pylons/waitress), a
production web server: one process with several threads, so the
thermostat and device polling run exactly once. Flask's own `app.run()`
development server isn't used.

## Tune the board for long unattended running

**Do this on every new board** (quick start step 7). An Arduino UNO Q
stores everything on a soldered-in 32 GB eMMC - flash that wears out with
writes and can't be swapped like an SD card. Measured on a real UNO Q
with LocalHome running:

| | eMMC writes |
|---|---|
| stock image | ~7.5 GB/day |
| after `deploy/tune-board.sh` | ~0.6 GB/day |

Almost all of it wasn't data at all but write amplification: journald
dirties several 4 KB pages for every log line. What the script does,
each step reversible (it prints how):

1. **System log in RAM** (`Storage=volatile`, 30 MB): no flash writes
   from logging. The log is lost at reboot - the admin panel's system
   status (`/climate-admin`) keeps working regardless.
2. **No background update downloads** (`apt-daily`, `apt-daily-upgrade`,
   `fwupd-refresh` timers off). A stock image installs nothing by itself
   anyway; these only download package and firmware lists every day.
   Update by hand when you choose - `sudo apt update && sudo apt upgrade`
   every few months - and check the dashboard afterwards.
3. **MQTT broker log into the journal**: mosquitto ships logging every
   device connection to a file on the eMMC; it goes to the journal
   instead - `journalctl -u mosquitto` - so into RAM like the rest.
4. **No graphical desktop** (`lightdm` off, boot to text mode): nothing
   is attached to a monitor, and it's ~80 MB of RAM plus a steady stream
   of log lines.

Left as they are, on purpose: Arduino App Lab and its services
(`arduino-app-cli`, `arduino-router`, Docker) - turn them off only if
you'll never use App Lab on that board.

The admin panel's system status shows the eMMC's own wear estimate (in
10% steps, as the chip reports it) and its writes per day, and raises a
problem when the chip nears the end of its life. At ~0.6 GB/day a 32 GB
eMMC lasts well over a decade.

## Addressing: names, not IP addresses

**Read this before pointing any device at the board.** Most home
routers hand out IP addresses by DHCP, and nothing guarantees the board
keeps the same one: a router reboot, a lease expiring while the board
was off, or a router replacement can all move it. Anything configured
with the board's IP address then silently stops working - the Shelly
devices stop reporting, a bookmarked dashboard stops loading.

The fix doesn't need any change on the router: every link to the board
uses a **name** instead of an address.

| Who connects | To what | Use |
|---|---|---|
| LocalHome | the MQTT broker (mosquitto, on the same board) | `"host": "localhost"` in `config/mqtt_broker.json` |
| Every Shelly / MQTT device | the MQTT broker | `<name>.local:1883` as the device's MQTT server |
| You (browser, phone) | the dashboard | `http://<name>.local:5000` |

`<name>` is the board's hostname (step 1 of the quick start above). The
board announces it on the LAN over mDNS (`avahi-daemon`), so
`<name>.local` always resolves to whatever IP it has right now.
Windows 10+, macOS, iOS and Android 12+ resolve `.local` names with no
setup.

The result: **the installation no longer depends on any IP address -
it depends on the board's name.** Treat that name accordingly:

1. **Choose it once, before configuring any device.** Every device
   stores the name in its own settings. Renaming the board later means
   changing it on every single device again, battery ones included.
2. **Keep it unique on the LAN.** If a second machine answers to the
   same name (a spare board being tested, a cloned SD card/image),
   `avahi-daemon` silently renames one of them to `<name>-2.local` and
   devices may end up talking to the wrong one. `journalctl -u
   avahi-daemon | grep -i conflict` shows it happening.
3. **Keep `avahi-daemon` running and enabled** (`systemctl is-enabled
   avahi-daemon`). It is what answers `<name>.local` - without it the
   name stops resolving even though the board is up. Check the name it
   announces with `journalctl -u avahi-daemon | grep "Host name is"`,
   and test it from another machine (`ping <name>.local`) - not from the
   board itself, whose own resolver may not look up its own `.local`
   name even while every other device does.
4. **mDNS doesn't cross network segments.** Devices on a guest WiFi, a
   separate VLAN, or an access point with client isolation turned on
   can't see `.local` names. Keep the board and its devices on the same
   network.

### Setting a Shelly's MQTT server to the board's name

In the Shelly app or the device's own web page: **Settings -> MQTT**,
set the server to `<name>.local:1883`, save. The device reboots and
reconnects by name.

- **Battery devices (H&T sensors)** are asleep most of the time and only
  accept a settings change while awake: press the device's button, then
  change and save within the next minute or so. The new server is used
  from its next wake-up (a report, at the latest after its ~2h
  heartbeat).
- **Check it worked** from the board itself - each device shows up by
  its id as it connects:
  ```bash
  journalctl -u mosquitto | grep "New client connected" | tail
  ```
  or in `/climate-admin`, where the device's status turns to reporting.
- Confirmed on a real install: the Shelly H&T Gen3 sensors and the
  0/1-10V Dimmer Gen3 resolve the board's `.local` name by themselves and
  connect with it.

### If a device can't resolve `.local`

- **The router's own DNS name.** Most home routers also register each
  DHCP client's hostname in their DNS, under a suffix that depends on
  the router (`.home`, `.lan`, `.fritz.box`, ...). Find it with `ping
  <name>` from a PC on the LAN - the reply shows the full name, e.g.
  `<name>.home`. That's ordinary DNS, which any device using the router
  as its DNS server can resolve.
- **Last resort: a static IP set on the board itself**, outside the
  router's DHCP range (read the range from the router's settings page -
  nothing needs changing there). With NetworkManager:
  `sudo nmcli con mod <connection> ipv4.method manual ipv4.addresses
  192.168.1.250/24 ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1`, then
  `sudo nmcli con up <connection>`. Pick an address outside the DHCP
  range, or the router may hand the same address to another device.

## Docker

Two ways to run this in Docker - same result, pick whichever fits how
you deploy. Either way you need a local `config/` directory (never
baked into the image - see `.dockerignore`), so start by cloning the
repo regardless of which path you take below:

```bash
git clone <this-repo>
cd <the-cloned-directory>
cp config/config.example.yaml config/config.yaml
# ...and the *.example.json files you need - see docs/configuration.md
# and docs/integrations/ for each device's own ids/keys/credentials.
```

### A. Build the image yourself (the default, works right away)

```bash
docker compose up -d --build
```

`docker-compose.yml` bind-mounts `./config` into the container at
runtime. Rebuild (`docker compose up -d --build`) after pulling code
changes; no rebuild is needed after editing anything under `config/`,
just restart (`docker compose restart`).

### B. Pull a pre-built image instead of building locally

Every push to `main` (and every version tag) publishes a multi-arch
(amd64 + arm64 - covers Raspberry Pi/Arduino UNO Q-class boards) image
to GitHub Container Registry via `.github/workflows/docker.yml`, using
the repo's automatic `GITHUB_TOKEN` - no publishing step or secrets to
set up on your end. This project's own image (public, no `docker
login` needed) lives at:

```
ghcr.io/intilinux-eng/localhome:latest
```

(Forked or renamed the repo? Yours publishes to
`ghcr.io/<your-github-username-or-org>/<your-repo-name>` instead - all
lowercase, matching how GHCR normalizes the name - once you've pushed
to `main` at least once.)

Skip the build step entirely - handy on a low-power board where
compiling anything is slower than just downloading a finished image -
either by editing `docker-compose.yml` to use it:

```yaml
services:
  localhome:
    image: ghcr.io/intilinux-eng/localhome:latest   # instead of `build: .`
    ports:
      - "5000:5000"
    volumes:
      - ./config:/app/config
    restart: unless-stopped
```

```bash
docker compose pull && docker compose up -d
```

or with a plain `docker run`, no compose file needed:

```bash
docker pull ghcr.io/intilinux-eng/localhome:latest
docker run -d --name localhome \
  -p 5000:5000 \
  -v "$(pwd)/config:/app/config" \
  --restart unless-stopped \
  ghcr.io/intilinux-eng/localhome:latest
```

Either form: `docker compose pull`/`docker pull` again followed by
`docker compose up -d`/`docker restart localhome` picks up a newer
published image; editing anything under `config/` still only needs a
restart, no pull.

## Bare metal (systemd)

The steps above under "Quick start" *are* the bare-metal/systemd path -
this section is only for the one variant they skip: a dedicated,
more locked-down system user instead of your own account, for a box
that does other things besides run LocalHome.

```bash
sudo useradd --system --home /opt/localhome localhome
sudo git clone <this-repo> /opt/localhome
cd /opt/localhome
python3 -m venv .venv && .venv/bin/pip install -e .

cp config/config.example.yaml config/config.yaml
# ...edit it and the *.example.json files you need

sudo chown -R localhome:localhome /opt/localhome
```

Then edit `deploy/localhome.service`'s `YOUR_USER`/path placeholders to
`localhome`/`/opt/localhome` instead of your own account before
copying it and continuing from `systemctl daemon-reload` in the Quick
start above - same everything else, including the venv troubleshooting
note if `python3 -m venv` itself fails.

## A few things worth knowing before you expose this beyond your LAN

- **The dashboard has no authentication by default.** If you want it,
  see [docs/configuration.md](configuration.md#web-auth-optional) - it's
  a few lines of config, off by default because this project assumes a
  trusted LAN.
- **Flask's built-in server (what `localhome`/`run.py` starts) isn't
  meant to be internet-facing.** For a household dashboard on your own
  LAN it's genuinely fine - that's what it's designed for and what this
  project ships. If you do want to reach it from outside your LAN,
  don't port-forward it directly: put it behind a reverse proxy with TLS
  (Caddy or nginx are both a few lines of config for this), or better,
  reach it over a private network (Tailscale/WireGuard) instead of
  exposing it publicly at all.
- Both of the above matter more if you enable the Meross/eWeLink/MQTT
  switches - an unauthenticated, internet-reachable "turn things on and
  off" endpoint is a very different risk than a read-only status page.
