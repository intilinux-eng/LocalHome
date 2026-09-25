# LocalHome on the Arduino UNO Q

The reference installation: an [Arduino UNO Q](https://www.arduino.cc/)
running LocalHome as the heating/cooling controller of one apartment,
with Shelly devices only - two radiant-panel zones, a bathroom bypass
valve, a dehumidifier, an outdoor sensor and a VMC - all managed from
the web admin panel, with no source code or config file to edit.

This page is the whole path, from a new board to a running
installation, and what to expect of it over the years. For just the
commands, in order, ending at an empty installation with no devices
configured - and the board's LED matrix showing its health - see
[quick-installation-arduino-uno-q.md](quick-installation-arduino-uno-q.md).
The generic guide for any Linux board is
[deployment.md](deployment.md); the full configuration reference is
[configuration.md](configuration.md).

## Which board

Both UNO Q versions run LocalHome with a wide margin. Measured on a
4 GB board running LocalHome and the Arduino services:

| | Measured | On a 2 GB / 16 GB UNO Q |
|---|---|---|
| LocalHome, RAM | ~75 MB, flat over days | same |
| Whole board, RAM | ~545 MB used | ~1.4 GB left free |
| LocalHome, CPU | ~1.3% of one core (0.3% of the board) | same |
| eMMC writes (after step 8 below) | ~0.6 GB/day | same |
| eMMC life at that rate | well over a decade | still over a decade (half the cells) |

On the 2 GB board, `deploy/tune-board.sh --lean` (step 8) is worth it:
it frees ~270 MB of RAM and ~2.3 GB of disk that Arduino App Lab uses.

## Install

You need the board on your Wi-Fi and a way into it: SSH (`ssh
arduino@<board-ip>`, the board's own password) or `adb shell` over USB.
The first boot and the Wi-Fi setup are done with Arduino App Lab, as for
any UNO Q.

```bash
# 1. Time zone - the board ships in UTC, and schedules run on its clock.
sudo timedatectl set-timezone Europe/Rome
timedatectl                          # -> "Time zone: Europe/Rome", "synchronized: yes"

# 2. The board's permanent name - devices and browsers reach it by name,
#    never by IP (the router may change the IP). Choose it before
#    configuring any device; renaming later means updating every device.
sudo hostnamectl set-hostname localhome
sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tlocalhome/" /etc/hosts
sudo apt update
sudo apt install -y git mosquitto avahi-daemon libnss-mdns python3.13-venv
sudo systemctl restart avahi-daemon

# 3. The MQTT broker, reachable from the Shellys on the LAN.
printf 'listener 1883 0.0.0.0\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/localhome.conf
sudo systemctl restart mosquitto

# 4. LocalHome itself.
git clone <this-repo> ~/localhome      # or copy the repository over
cd ~/localhome
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# 5. Configuration: climate only, no devices in the file.
cp config/config.climate.example.yaml config/config.yaml
cp config/mqtt_broker.example.json config/mqtt_broker.json     # "host": "localhost"
.venv/bin/localhome-cli doctor

# 6. Run it as a service - started at every boot, restarted if it fails.
#    (Assumes the code is in ~/localhome; otherwise edit the paths.)
sed "s/YOUR_USER/$USER/g" deploy/localhome.service | sudo tee /etc/systemd/system/localhome.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now localhome

# 7. Check: from a phone or PC on the same Wi-Fi.
#    http://localhome.local:5000            dashboard
#    http://localhome.local:5000/climate-admin   admin panel

# 8. REQUIRED on every new board: tune it for years of unattended running
#    (see "Tune the board" below). Safe to run again at any time.
sudo sh deploy/tune-board.sh          # add --lean to also remove App Lab & co.
```

## Configure it - from the admin panel only

Everything from here is done in `/climate-admin`; nothing needs SSH
again. The panel opens with a four-step guide; in short:

1. **Season** - heating or cooling. It applies to the whole plant, it
   changes twice a year, and switching asks you to type the season's
   name. Each season has its own weekly schedule, painted from the
   dashboard.
2. **Devices** - enter each Shelly's id (shown in the Shelly app, e.g.
   `shellyhtg3-c0ffee000002`): each zone's H&T sensor and relay valve,
   the bathroom bypass valve, the dehumidifier, the outdoor sensor, the
   VMC dimmer. Save.
3. **Restart** - device and settings changes apply after "Restart
   LocalHome"; the button appears after every save.
4. **Check** - each card says whether its device answers; "System
   status" at the bottom lists any problem.

**On each Shelly** (Shelly app → device → Settings → MQTT):

- **Enable** on;
- **Server** `localhome.local:1883` - the board's name from step 2, and
  port **1883** (a typo like 1833 fails silently: the device just never
  appears);
- **Generic status update over MQTT** on - it's what publishes the
  temperature and humidity;
- **SSL** off.

Battery sensors (H&T) only apply new settings when they wake up: press
the device's button once after saving.

**Settings** (admin panel → Settings) replace editing config.yaml:
hysteresis, how often the thermostat checks, after how many hours a
silent sensor's valve is closed, how many days and MB of history to
keep, and the page language. Each has a safe range; "Restore defaults"
goes back to config.yaml's values.

## What it does on its own

- **Heating** opens a zone's valve below target − hysteresis and closes
  it above target + hysteresis; **cooling** the opposite. An hour with
  no schedule keeps the valve closed. Every cycle compares the valve's
  real state: one opened or closed by hand is corrected.
- **Bathrooms in summer**: excluded to avoid condensation - valve held
  closed. The **bypass valve** follows the season (on in cooling, off
  in heating); the **dehumidifier** is switched by hand, cooling only,
  and turned off when heating starts.
- **VMC**: always on, three speed modes (Night 20%, Day 50%, Boost
  100%) plus a slider on the dashboard.
- **A device that's off or missing never stops the rest**: its card and
  the system status say so, it writes no history, the log notes it once;
  a zone whose sensor stays silent for the safety-close time gets its
  valve closed.

## Limits: nothing grows without bound

Checked on the real board; every item is capped, in RAM or on the eMMC.

| What | Where | Limit |
|---|---|---|
| System log (journald: LocalHome, services, kernel) | RAM | 30 MB, lost at reboot |
| MQTT broker log | RAM (in the system log) | as above |
| History database (charts, statistics) | eMMC | 30 days **and** 300 MB, oldest first - both in Settings |
| A sensor that's off | - | writes nothing |
| Automatic backups before a reset | eMMC | last 10 configurations (a few KB each) + 1 history copy, skipped if disk space would get low |
| MQTT last-reading cache | eMMC | one entry per device topic |
| mosquitto persistence | eMMC | a few hundred bytes |
| LocalHome memory | RAM | ~75 MB, flat |
| A device that doesn't answer | CPU, network | MQTT devices cost nothing; the VMC is asked less often (up to every 2 min) until it answers |

The admin panel's **System status** shows these live: RAM, CPU and
temperature, disk space, the eMMC's own wear estimate and its writes
per day, database size and growth, Wi-Fi signal, clock sync, uptime,
recent boots (and whether each ended properly or with a power cut), and
the installed version.

## Tune the board: what `tune-board.sh` removes

`sudo sh deploy/tune-board.sh` - do it on every board:

| Step | Why | Undo |
|---|---|---|
| System log into RAM (30 MB) | journald's write amplification was most of the eMMC writes (~5 GB/day) | `rm /etc/systemd/journald.conf.d/localhome-volatile.conf && systemctl restart systemd-journald` |
| No background update downloads (`apt-daily`, `fwupd-refresh`) | nothing installs by itself anyway; they only download lists daily | `systemctl enable --now apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer` |
| mosquitto log into the system log | the last log still written to the eMMC | set `log_dest file /var/log/mosquitto/mosquitto.log` in `/etc/mosquitto/mosquitto.conf` |
| No graphical desktop | no monitor attached; ~80 MB of RAM | `systemctl set-default graphical.target && systemctl enable --now lightdm` |

`sudo sh deploy/tune-board.sh --lean` - also removes what LocalHome
doesn't use, recommended on the 2 GB board:

| Removed | Frees | Undo |
|---|---|---|
| Arduino App Lab (`arduino-app-cli`, `arduino-cloud-connector`) and Docker (`docker`, `containerd`) | ~200 MB RAM | `systemctl enable --now arduino-app-cli arduino-cloud-connector docker containerd` |
| App Lab's Docker images | ~2.3 GB disk | re-downloaded by App Lab when turned back on |
| Firmware updater (`fwupd`) | ~70 MB RAM | `systemctl unmask fwupd` |
| Package download cache | ~100 MB disk | - |

Kept on purpose: `adbd` (USB access - your way in if the Wi-Fi is ever
lost) and `arduino-router` (the Linux-microcontroller bridge).

## Updating

Copy the new code to the board (or `git pull`), then:

```bash
cd ~/localhome
.venv/bin/pip install -e .       # always - an update may add a dependency
sudo systemctl restart localhome
```

The version is shown in System status. Pages left open during an update
show "LocalHome was updated - Reload". `config/` is never touched.

## Backup, reset, a new apartment

Admin panel → Maintenance:

- **Download backup** - the configuration as a `.zip` (credentials left
  out), to keep on a PC.
- **Clear history**, **Clear schedules**, **Factory reset** - each needs
  `RESET` typed in and saves a full backup on the board first
  (`config/backups/`). The reset removes what LocalHome created - device
  mapping, schedules, history, settings - and keeps config.yaml, the
  broker and credentials: the board comes back as a fresh installation.

For another apartment: same board image and steps; choose a hostname
for it (step 2), point its Shellys at it, and map them in the panel.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A Shelly never appears | its MQTT server/port (`localhome.local:1883`), "Generic status update" off, or - battery sensor - not woken up since saving (press its button). `journalctl -u mosquitto \| grep "New client"` shows who connects. |
| Every device stopped at once | the board's name doesn't resolve - `systemctl status avahi-daemon`; `ping localhome.local` from a PC |
| A panel change has no effect | not restarted yet - "Restart LocalHome" |
| A new field/section isn't visible | the page is from before an update - reload |
| Schedules trigger at the wrong hour | time zone (step 1) or clock sync - System status shows both |
| "Recent boots" shows interrupted sessions | power cuts or hangs - the board's power supply is the first suspect |

More in [troubleshooting.md](troubleshooting.md).
