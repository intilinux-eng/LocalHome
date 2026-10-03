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

## 3. Open the MQTT broker to the LAN

The Shelly devices publish to the broker on this board: in each
device's MQTT settings the server is `carlucci.local:1883` (the board's
own name), with no username or password. Out of the box, though,
mosquitto only accepts connections from the board itself:

```bash
ss -ltn | grep 1883      # fresh install -> 127.0.0.1:1883 and [::1]:1883 only
```

LocalHome, running on the board, would work like that - every Shelly
would be refused. Two lines open it to the LAN:

```bash
printf 'listener 1883\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/localhome.conf
sudo systemctl restart mosquitto
ss -ltn | grep 1883      # -> 0.0.0.0:1883 and [::]:1883
```

- `listener 1883`, with no address, listens on every interface, IPv4
  and IPv6: a PC that resolves `carlucci.local` to an IPv6 address
  connects just as well as a Shelly using IPv4.
- `allow_anonymous true` means no password - the usual choice on a home
  LAN. Anyone on the same Wi-Fi can then read and publish MQTT on this
  board, the valves' commands included. To add a password later: a user
  created with `mosquitto_passwd`, the same credentials in every Shelly
  and in `config/mqtt_broker.json` - see
  [integrations/mqtt.md](integrations/mqtt.md).

Checked on this board: from a PC on the LAN, an MQTT connection with no
credentials is accepted over both IPv4 and IPv6. The admin panel's
"Shellys and MQTT broker" section says "reachable" as well.

Nothing in the installation depends on the board's IP address - the
router's DHCP can move it freely: LocalHome reaches the broker as
`localhost`, the Shellys reach it as `carlucci.local:1883`, browsers
open `http://carlucci.local:5000`. How to set a battery H&T's MQTT
server (it only listens while awake), how to check each device really
connected, and what to do if one can't resolve `.local`: see
[deployment.md, "Addressing: names, not IP addresses"](deployment.md#addressing-names-not-ip-addresses).
Confirmed on the first installation: the Shelly H&T Gen3 sensors and
the 0/1-10V Dimmer Gen3 resolve the board's `.local` name by themselves
and connect with it. `journalctl -u mosquitto | grep "New client
connected"` shows each device as it connects.

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

Or, from the PC, let `python tools/deploy_board.py carlucci` do all of
it - see docs/deployment.md, "Updating LocalHome on the board".

## 5. Python environment

```bash
cd ~/localhome
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e . -c constraints.txt
```

78 MB of virtualenv, 108 MB for the whole checkout. The optional TP-Link
driver is deliberately left out (it is GPL - see
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)); LocalHome logs one
line about it at startup and carries on. Install it with
`.venv/bin/pip install -e .[tplink] -c constraints.txt` if you need Kasa/Tapo devices.

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
file live. It also restarts LocalHome when it hangs rather than crashes:
LocalHome tells systemd it's alive every 40 seconds while its page answers
and its control loops run, and after two minutes of silence systemd
restarts it (on a board installed before this existed, step 8 adds it).

> Over SSH there is no terminal for sudo's prompt. If you are scripting
> this, feed the password to `sudo -S` on stdin and keep `sudo` at the
> start of the command - a `sudo` in the middle of a pipeline prompts
> and hangs.

## 8. Tune the board

Required on every new board, and safe to run again at any time - run it
again after every LocalHome update, too. Two things:

**Years on its eMMC.** It cuts the flash writes that wear the eMMC out
(measured elsewhere: ~7.5 GB/day down to ~0.6 GB/day) by keeping the
system log in RAM, stops the daily background downloads, sends the
broker's log to the journal and turns off the graphical session.

**On Wi-Fi 24/7, with nobody around.** The board depends on Wi-Fi for
everything - Shellys, phones, the clock - so:

- *Wi-Fi never dozes or gives up*: power saving off, and every Wi-Fi
  network retries for ever instead of stopping after a few failures (a
  network added later - the tester's access point - gets the same within
  a minute, from the watchdog below).
- *A Wi-Fi watchdog*, every minute, independent of LocalHome: when the
  router doesn't answer for 3 minutes it reconnects the Wi-Fi, at 10
  restarts the network manager, at 20 reconnects again, at 30 reboots the
  board - at most once every 6 hours, so a router switched off for the
  night doesn't reboot it all night.
- *No power saving anywhere*: the board can't go to sleep, Bluetooth
  (unused, and sharing the radio with the Wi-Fi) is off, USB and the
  eMMC never doze. The CPU still changes speed as needed.
- *The essential services always come back*: the MQTT broker, the name
  service (`<name>.local`) and the network manager are restarted
  whenever they stop, and never given up on; short of memory, LocalHome
  and the broker are the last to go.
- *The hardware watchdog*: if the whole system freezes, the chip resets
  the board by itself (off on a stock UNO Q image).
- *LocalHome restarted if it hangs* - see step 7 - *or if it grows past
  512 MB of memory*: about four times what it uses, so only a leak gets
  there, and LocalHome alone is restarted, not the board.

Then reboot once (`sudo systemctl reboot`): Wi-Fi power saving applies
from the next connection.

```bash
sudo sh /home/arduino/localhome/deploy/tune-board.sh
```

The admin panel's **System status** shows whether each of these is on
("Protections for running all year" - a missing one is also listed as a
problem) and the **Watchdog history**: every reconnect, restart and
reboot they did, with the time - it survives reboots.

**The router.** One thing the board can't fix: a router that answers it
but stops passing it the other devices' connections - pings get through,
the page, SSH and the Shellys' MQTT don't. It happened on the home router
(5 GHz on radar channel 100 at the time), between devices on the same
radio, in both bands, until the router was restarted. So:

- The watchdog re-activates the Wi-Fi once two minutes after every boot,
  and whenever the Shellys that stay connected to the broker all drop
  off while the router still answers (at 10 and 30 minutes). LocalHome
  then shows **RT** on the LED matrix and a problem in System status:
  restart the router.
- Put the board and the Shellys on the **main network**, never the guest
  one (guest networks keep devices apart on purpose).
- If the router allows it, 5 GHz on a **fixed channel between 36 and
  48**: channels 52-144 are radar channels the router may have to leave
  by itself.
- A **fixed IP** for the board (a DHCP reservation on its MAC address) is
  worth having; everything uses the board's name, so it isn't required.
- Leave the Wi-Fi band alone: the board joins whichever band the router
  offers. Pinning it to 2.4 GHz cut the Shellys off on that router.

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

And check the protections from step 8: **System status → Protections for
running all year** must show them all on (or "not available" for a
hardware watchdog on a board without one).

To see the Wi-Fi watchdog act without touching the router, point it at
an address that doesn't answer for one run - it logs "the router isn't
answering"; four more minutes of that and it reconnects the Wi-Fi:

```bash
sudo LOCALHOME_WIFI_WATCHDOG_TARGET=192.0.2.1 sh /usr/local/lib/localhome/localhome-wifi-watchdog.sh
cat /var/lib/localhome/wifi-watchdog.log
```

The next regular run, a minute later, finds the real router answering
and logs "network back".

## 10. First run in the panel

- **Page name** (Settings) - what this installation calls itself, up to
  24 characters. It replaces "LocalHome" in the header, the browser tab
  and the name under the icon when the dashboard is added to a phone's
  home screen, and takes effect on the next page load.
- **Outdoor temperature sensor and VMC** - on a fresh installation
  both start **simulated** too, so the outdoor chart and the VMC's
  speeds and day/night program can be shown and tried before the
  hardware exists. Everything simulated is marked "simulated" on the
  dashboard. Switch each to "Shelly" with its device id once it's
  installed, or to "None" if the apartment doesn't have one.
- **Zones** - exactly two, and on a fresh installation both start
  **simulated** on purpose: there is no hardware yet, a real device id
  is required to save a "Shelly" zone, and a zone pointed at a device
  that doesn't exist reports errors forever. Name them, save, restart
  from the panel, and the thermostat, the weekly schedule, the history
  and the statistics all work against simulated sensors - the whole
  system, driveable on a bench.
- **Shelly devices** - two settings, one on each side, and they don't
  depend on each other:
  1. **In this admin panel**, the device's **id** (from the Shelly app,
     e.g. `shellyhtg3-c0ffee000002`): switch the zone's sensor or valve
     to "Shelly" and paste it. It tells LocalHome which messages are
     that device's - each Shelly prefixes its own with its id - so no
     name or address is involved.
  2. **On the Shelly itself** (Shelly app -> device -> Settings ->
     MQTT), where the broker is: **Enable** on, **Server**
     `carlucci.local:1883`, **Generic status update over MQTT** on
     (it's what publishes the readings), **SSL** off, no username or
     password. A battery H&T only applies new settings when it wakes:
     press its button once after saving.

  The panel's per-zone status then says whether the device is really
  being heard, and tells "not reachable yet" (a battery sensor that
  hasn't woken up) from "was reporting, now unreachable". A zone that
  stays "not reachable yet" after the H&T's button press almost always
  means one of the settings above: the port (1883, not 1833), or
  "Generic status update over MQTT" left off.

## 11. The LED matrix

The UNO Q's 13x8 LED matrix shows LocalHome's health to whoever is in
front of the box, without a phone - above all when the network or
LocalHome itself is down, exactly when the dashboard can't help. The
admin panel's **LED matrix** section has a live mirror of it, a legend
of every picture and what each problem code means.

The picture is decided by LocalHome; the microcontroller only draws it,
and shows two things on its own: a dot running along the bottom until
LocalHome's first frame, and a blinking X if frames stop for 20 seconds.
Its sketch has to be built and loaded once, from the board itself, with
`arduino-cli` - three steps, **the first two only if they aren't done
already**:

**a. The toolchain.** `arduino-cli` and the UNO Q core ship with the
board's image (both test boards: arduino-cli 1.5.1, core
`arduino:zephyr` 1.0.0). Check, and install the core only if missing:

```bash
arduino-cli version
arduino-cli core list              # must list arduino:zephyr
arduino-cli core install arduino:zephyr    # only if it doesn't
```

**b. The libraries - required, and not always there.** The sketch
talks to Linux through the `Arduino_RouterBridge` library and its
dependencies. They are present only if Arduino App Lab has already
installed them (on one test board they were, on the other not at all).
Check:

```bash
arduino-cli lib list
```

If `Arduino_RouterBridge` isn't listed, install it - its dependencies
(`Arduino_RPClite`, `MsgPack`, `ArxContainer`, `ArxTypeTraits`,
`DebugLog`) come with it:

```bash
arduino-cli lib install "Arduino_RouterBridge@0.4.3"
arduino-cli lib list               # Arduino_RouterBridge 0.4.3 and its dependencies
```

Verified with `Arduino_RouterBridge` 0.4.3 (with `Arduino_RPClite`
0.3.0 on one board; a fresh install today brings 0.3.1).

**c. Build and load the sketch** - one build at a time: two
`arduino-cli compile` runs at once share the same build cache and can
leave a broken core archive behind (see the troubleshooting table):

```bash
cd ~/localhome
arduino-cli compile --upload -b arduino:zephyr:unoq deploy/led-matrix/localhome_matrix
```

About two minutes, most of it compiling; a successful run ends with
`Sketch uses ... bytes` and openocd's `shutdown command invoked`. No
LocalHome restart is needed: the matrix lights up within 30 seconds, and
the admin panel's LED matrix section turns "active". Loading it
replaces whatever sketch was on the microcontroller (an Arduino App Lab
app included). Without it, LocalHome runs exactly the same - the admin
panel just says the sketch isn't loaded.

It's built to cost nothing that matters: one frame a second over a
single connection to arduino-router, and its state is read from memory
every 30 seconds - no network probes, no subprocesses. Where there's no
matrix (a PC, a Raspberry Pi) it does nothing at all.

## 12. A resource log, for boards left in the field

A board handed over for weeks should come back with the answer to "does
anything slowly grow?". A systemd timer appends one line every 10
minutes to `~/localhome-resource-log.csv`: LocalHome's memory and peak,
threads, open files and CPU time, arduino-router's and mosquitto's
memory, the board's available memory, eMMC writes and the history
database's size. About 17 KB a day, capped at a year of lines; it runs
outside LocalHome, so it can't affect it and keeps logging if LocalHome
stops.

```bash
cd ~/localhome
sudo install -m 755 deploy/resource-log/localhome-resource-log.sh /usr/local/bin/
sudo install -m 644 deploy/resource-log/localhome-resource-log.service deploy/resource-log/localhome-resource-log.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now localhome-resource-log.timer
```

Reading it: `column -t -s, ~/localhome-resource-log.csv | less`. Memory
is in kB. What healthy looks like, measured on the test board over its
first half hour: LocalHome's memory steps up by a few hundred kB the
first time each page or feature is used, then stays flat (about 73 MB);
threads and open files don't move; arduino-router stays flat. A steady
climb over days, rather than steps that stop, is what to look for. To
stop it: `sudo systemctl disable --now localhome-resource-log.timer`.

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
| A Shelly never connects, a zone says "not reachable yet" forever | the broker still listens on the board only (step 3: `ss -ltn \| grep 1883` must show `0.0.0.0:1883`), or the device's MQTT server / id is wrong |
| `localhome-cli doctor` lists a skipped driver | the optional TP-Link driver, not installed on purpose - step 5 |
| Building the LED sketch: `Arduino_RouterBridge.h: No such file or directory` | the libraries aren't installed - step 11b |
| Building the LED sketch: dozens of `undefined reference to 'arduino::String...'` / `'arduino::Stream...'`, even in the sketch's own lines | a broken core archive in arduino-cli's cache - seen after two compiles ran at the same time (its `core.a` held 16 of the core's 28 objects, none of `String`/`Stream`). Clear it and build once: `rm -rf ~/.cache/arduino/cores/arduino_zephyr_unoq_*`, then step 11c with `--clean` added |
| The admin panel says "sketch not loaded" | step 11 hasn't been done on this board, or the upload failed - rerun 11c and read its last lines |
| System status lists "Protection not on" | step 8 hasn't been run since the board was installed or updated - run it again |
| The page doesn't open, but the LED matrix is drawing normally (top-right pixel blinking) | LocalHome is fine: the Wi-Fi or the router is the problem. Wait a few minutes for the watchdog, or switch the board off and on; the watchdog history says afterwards what happened. If it keeps happening, give the board a fixed IP in the router (step 8) |
| Someone far away needs to see what happened | System status → **Download diagnostics**: one file with the status, this boot's logs, the watchdog history, the resource log and the configuration without passwords |
