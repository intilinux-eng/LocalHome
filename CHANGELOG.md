# Changelog

Notable changes to LocalHome. Dates are when the change landed on `main`;
this project doesn't cut versioned releases, so entries are grouped by
date rather than by tag.

## Unreleased

### Removed
- **TP-Link Kasa/Tapo LED strip support.** The `tplink_led_strip`
  drivers, the `[tplink]` extra (`python-kasa`), `tools/tplink_discover.py`
  and `docs/integrations/tplink.md` are gone. The GPL SDK was the only
  copyleft runtime dependency; dropping the driver leaves every remaining
  one permissive.

### Fixed
- **The VMC's program applied at startup, not a minute later.** The
  loop's first check asked for the speeds before the app was ready, failed
  and logged it, and the program's speed waited for the next check.
- **Save, pressed right after typing a Shelly's id, saves.** Leaving the
  field starts the id's check, and the line it wrote under the field
  moved the button from under the pointer between press and release -
  the first press often did nothing. The line now waits for the release.
- **The sensor failsafe now counts silence LocalHome actually watched**,
  kept in `config/sensor_silence.json` across restarts. It used to count
  from when the process first noticed, so a board that restarts every
  day - an update, a power cut - restarted the countdown with it and a
  genuinely dead sensor never reached the threshold. Counting the age of
  the last reading instead would have been wrong the other way: a board
  switched off for three days came back convinced every sensor had died,
  when nobody had been listening. Time between ticks is capped at a
  couple of cycles, so a gap costs nothing.
- **The board's start time is part of the state now.** A reading older
  than the boot means nobody was there to hear it: the dashboard says
  "waiting for its first reading (board up 12 min)" instead of "no data
  for 53h", and the admin panel's problems list separates "not heard yet
  since the board came up" from "offline" for the first couple of hours -
  a battery sensor's own heartbeat window.
- **A cover that isn't answering is no longer asked every 8 seconds.**
  Every open dashboard polls each cover, and a device that has stopped
  answering costs a socket timeout per poll - with five dead blinds the
  board spent most of its time waiting on them. A failing cover is
  retried after 30s, then 60, 120, up to 5 minutes; the failure itself
  is still shown immediately, and a button press is always sent (and
  puts it back in the normal rhythm if it works).
- **A Tuya device that isn't working now says so.** tinytuya reports a
  failure as a payload (`{"Error": "Check device key or version"}`)
  rather than raising, and the cover driver read `dps` straight out of
  it - so five covers whose local keys had gone stale sat on the
  dashboard showing "unknown", looking like devices that simply didn't
  know their position. Their cards now read "key needs refreshing" or
  "not answering", with the full reason on hover, and covers appear in
  the admin panel's System status like every other device (nothing polls
  them in the background, so they were missing from it entirely).

### Added
- **How long each zone ran, and the plant - beside the heat meter.** Each
  zone's card has a "Running history": the last 24 hours on one line,
  and opened the last 7 or 30 days as bars, a tap naming the day's hours
  and starts. The admin panel's "Hours and consumption" shows a year
  month by month: the plant's hours - any zone open, counted once - and
  each zone's, beside the heat meter's kWh and kWh per hour, each column
  saying what it holds. The meter's month-end readings are typed a year
  at a time, as its display keeps them, and under each the month's usage
  shows as it's typed - the reading less the month before's - so a
  mistake shows before saving. Each month sits beside the same month the
  year before, with the outdoor sensor's mean and degree days and heat
  kWh per degree day, to compare winters however cold; the current month
  gets an estimate from its hours so far. Six years are kept.
- **The VMC's card, redone, with five levels and a fine slider.** Minimum
  (10%) and High (75%) join Night, Day and Boost - set in the admin panel
  with the others, worked out from them for a device or a file with only
  three. A slider for any speed between the minimum and 100%; on and off
  is a switch of its own. Under its name the card says why it runs at that
  speed (program, hand pick until the next change, timed, automatic,
  away). "Guests": high for 3 hours, beside "Boost for". The program is a
  line that opens. Away, the VMC can also be held at its minimum.
- **The VMC filters' reminder**: how often and when last cleaned, set in
  the admin panel; the home card warns two weeks before and once due, and
  "cleaned today" starts it over. Nothing recorded, no reminder.
- **Winter's dry-air automatic** (off by default, with the others): the
  reference room under 35% humidity, the VMC at its night speed until it's
  3 points over again.
- **Away from home, the VMC at its night speed or off** - chosen in the
  away sheet (night unless chosen otherwise; the last choice is kept).
  It wins over the program, the automatics and a timed boost; the VMC's
  card says what it's held at and offers nothing to press, a change from
  the Shelly app is put back within a minute, and on return it goes back
  to its program, or as it was before. `tools/live_walk.py` asks for the
  VMC to be left alone, so its walks don't touch it.
- **VMC automatics, off by default**: set in the admin panel under Plant
  tuning, each switched on by itself, in use at once. *Shower*: the
  bathroom's humidity rising 8 points within 10 minutes runs the VMC at
  boost (day speed in the quiet hours, 22-7) until it's back within 5
  points, for 15 to 60 minutes. *Humid outside* (summer): the outdoor
  dew point 1 °C above the reference room's - the night speed. *Night
  cooling* (summer): the house from 24 °C, the outdoor air 3 °C cooler,
  not under 16 °C (the unit's summer bypass) and no more humid - the day
  speed. On top of the program and of a speed picked by hand: never
  switching the VMC on, never over a timed boost, and a speed picked by
  hand makes them step aside until their reason has passed. The home
  page's VMC card says which one is under way and why.
- **`tools/live_walk.py`: a walk on the real board**, to run before
  wiring the valves and after a deploy. The home page used as a resident
  would (manual changes of every kind, back to the week, away and back),
  the season changed in every direction, diagnostic mode from the admin
  page - and each time what the Shelly relays themselves say on the
  broker. Prints the plan without `--yes`; puts back the season, the
  dehumidifier's threshold and diagnostic mode as it found them; stops if
  someone is using the house. `--parts restart` and `--parts reboot`, only
  when named: LocalHome restarted, or the board rebooted or unplugged by
  hand, while away - still away, the valves closed, settings and weeks as
  they were, every device back, no new problem.
- **A demo that runs in a minute**: `docker compose -f
  docker-compose.demo.yml up --build` starts LocalHome with every device
  simulated - two zones with a sample week, the outdoor sensor, the VMC,
  the bypass valve and the dehumidifier - and no hardware or broker. CI
  builds and starts it on every push.
- **Field notes for Shelly Gen3 over local MQTT**
  ([docs/shelly-gen3.md](docs/shelly-gen3.md)): how an H&T Gen3 on
  batteries really reports, telling a sleeping sensor from a dead one,
  the Dimmer 0/1-10V PM Gen3's quirks, and what the network under them
  needs.
- **Shelly device health in the admin panel.** Each Shelly's own Wi-Fi
  signal, now and over a day and a week; for a battery H&T, its battery
  and its reports lost (reports sent while LocalHome was off aren't held
  against it); for a mains Shelly, asked every 5 minutes, its internal
  temperature, mains voltage, since when it's been on, its restarts and
  any firmware update waiting. A Shelly too hot, or losing reports,
  reaches the problems badge; a weak signal alone doesn't - a sensor at
  -88 dBm can get every report through.
- **More of the board's hardware in the system status**: which board it
  is, the Wi-Fi chip's, GPU's and modem's temperatures, the CPU's speed
  and whether heat has capped it, the zram swap, and the Wi-Fi's errors
  since boot.
- **Every dependency pinned** to the versions the production board runs
  (`constraints.txt`), used by every documented install, the CI and the
  Docker image; `pyproject.toml` stops each library short of its next
  major version.
- **Copy a whole weekly schedule onto another zone.** Two zones usually
  run the same way in winter, and painting 168 cells twice is work the
  dashboard should take off you: under "Copy Monday to" there's now
  "Copy the whole schedule to: <zone>". It asks first (it replaces
  what's there), copies only the season on screen, and shows up only
  where there is another zone to fill - in cooling a bathroom is out of
  the season entirely, so it isn't offered.
- **A Tuya device that changed address finds its own way back.** The IP
  in devices.json is now only a starting point: when a device stops
  answering there, it's looked up by its device id on the LAN and the
  new address is remembered in `config/tuya_addresses.json`, so a
  router's DHCP shuffle heals itself instead of needing a re-scan. The
  lookup is throttled per device, and a wrong-key error never triggers
  one (the device is answering; scanning would only slow every poll).
- **"The board isn't answering"**: when the dashboard stops getting
  replies it says so at the top, with the time of the last update, and
  dims the values instead of showing stale ones as if they were live. It
  clears itself the moment the board answers again, and needs two failed
  refreshes in a row, so a restart after an update doesn't flash it.
- An **app icon, name and manifest**: added to a phone's home screen,
  LocalHome now has its own icon and opens without the browser's bars.
- **First-run screens**: an install with no devices says so and offers a
  button to the configuration panel, on both tabs, instead of an empty
  page or a link to a documentation file. The covers section hides
  itself when there are no covers.
- A **day/night program for the ventilation unit**: two periods, each a
  start time and a speed (day 07:00 at 50%, night 22:00 at 20% to begin
  with), on the VMC's own card next to its speed buttons - a daily
  control, not installation config. Every change saves by itself and is
  applied straight away, with no restart, because the control loop
  re-reads the schedule every tick.
  The speed is written when a period begins and then left alone, so a
  speed picked by hand on the dashboard stays until the next change; the
  card says which period is running ("Automatic program: day 50% until
  22:00"). It drives any settable number, so a VMC wired in config.yaml
  can be picked as well as one mapped in the panel. See
  `services/vmc_schedule.py` and `/api/climate-admin/vmc-schedule`.
- `type: simulated` for a `number` (a dimmer or fan speed), so a VMC and
  its program can be tried on a PC with no hardware - the counterpart of
  the simulated climate sensor and switch.
- The README opens with the Arduino UNO Q installation step by step,
  ready to copy - including `deploy/tune-board.sh` as a required step and
  a one-line command that fills in the systemd unit's user and paths.
- `docs/arduino-uno-q.md`: the reference installation end to end - the
  board versions and measured resource use, install steps, configuring
  from the admin panel only (and each Shelly's MQTT settings), what runs
  on its own, a table of every limit, what `tune-board.sh` and
  `--lean` remove (with undo), updating, backup/reset, troubleshooting.
  Linked from the README and the deployment guide.
- A **Settings** section in `/climate-admin`: hysteresis, thermostat
  check interval, safety-close hours, history days and size cap, page
  language - changed there instead of in config.yaml, within safe
  ranges, each showing where its current value comes from. Saved in
  `config/panel_settings.json`, applied after a restart; "Restore
  defaults" goes back to config.yaml's.
- The admin panel opens with a four-step "How to use this page" guide
  and links to every section (a link or bookmark opens a collapsed
  section), and "How it works" became a full summary: seasons and
  valves, bathrooms/bypass/dehumidifier, VMC, battery sensors, missing
  devices, data and limits, diagnostics.
- `deploy/tune-board.sh --lean` also turns off Arduino App Lab and its
  Docker runtime and the firmware updater, removes App Lab's unused
  Docker images and empties the package cache (~270 MB RAM, ~2.3 GB disk).
- `history.max_db_mb` (default 300): a hard ceiling on the history
  database on top of `retention_days` - checked hourly, the oldest days
  go first, the last day is always kept. A sensor that's off never adds
  rows (confirmed: 0 rows from 9 offline devices in a 16-minute test).
- A device queried over the network on every read (the Shelly dimmer
  via RPC) is asked less and less often while it doesn't answer - 20s,
  40s, 80s, then every 2 minutes - and back to normal on its first reply,
  or at once when a command to it goes through (it and its paired
  switch/number are re-read immediately).
- `config/config.climate.example.yaml`: a climate-only, Shelly-only
  starting point for a new apartment - no devices in the file at all,
  every one added from the admin panel - and a deployment-guide section,
  "A new apartment (climate only)". Soak-tested with every device
  unreachable and with the MQTT broker down: memory, threads and the
  control loops stayed flat, and the sensor failsafe closed the valves.
- The system status shows when LocalHome and the board last started (and
  for how long), the installed version (git commit and date), the
  operating system, whether the clock is NTP-synchronized (a problem if
  not), and the board's recent boots with how each session ended -
  properly shut down, or interrupted (a power cut or a hang). The
  problems box moved from the top of the page into the system status
  section; its count stays visible in the section header.
- Pages open during an update now say so ("LocalHome was updated -
  Reload") instead of silently running the old code, and every static
  URL carries a version so no browser keeps a stale copy. Found when a
  new admin-panel status group didn't show until a reload.
- The system status shows the board's network: Wi-Fi name, signal (with
  a problem raised below 30%), band and channel, IP address and its
  `.local` name - read from NetworkManager, never changed.
- A **Maintenance** section in `/climate-admin`: download a
  configuration backup (credentials left out), clear the history, clear
  every schedule, or factory-reset to a fresh installation (only what
  LocalHome created at runtime; config.yaml, credentials and the MQTT
  broker stay) followed by a restart. Each destructive action needs
  `RESET` typed in and first saves a full backup on the board, in
  `config/backups/` (gitignored).
- `deploy/tune-board.sh` also moves mosquitto's log from a file on the
  eMMC into the journal (RAM) - the last log still written to flash.
- `deploy/tune-board.sh` and a deployment-guide section, "Tune the board
  for long unattended running" (quick start step 7): system log in RAM,
  no background update downloads, no graphical desktop. On a real UNO Q
  it took eMMC writes from ~7.5 to ~0.6 GB/day.
- The admin panel's system status shows the CPU temperature (the
  hottest CPU thermal zone) and flags it from 80°C.
- The admin panel's system status shows the board's eMMC wear (as the
  chip reports it, in 10% steps) and its writes per day, and flags worn
  storage as a problem.
- Cooling palette in blues - the deeper the blue, the cooler the target -
  and a muted green for economy; previously the higher cooling setpoints
  came out orange and red, reading as "hot". Both seasons' palettes are
  now split into two boxes, **Comfort** and **Economy** (heating: economy
  below 20°C; cooling: economy from 26°C), each with its label and icon
  on the box's edge, and each zone's current target says which one it
  is.
- While cooling, a zone excluded from it (the bathrooms) shows a panel
  with its valve's real state instead of just being hidden: "closed ✓",
  "open: feeding <dehumidifier>", "still open: closing" or "state
  unknown". `GET /api/climate/zones` reports which interlock drives a
  valve (`valve_driver`).
- VMC speed modes: the VMC card has no on/off toggle anymore
  (mechanical ventilation runs continuously) and offers three one-tap
  modes - Night 20%, Day 50%, Boost 100% - above the slider, with the
  current one highlighted. Picking a mode also starts the VMC if it was
  stopped from elsewhere. Built from three new generic options:
  `hide_toggle` (switch), `presets` (number) and `on_with_value`
  (`shelly_light` number); the admin panel's VMC gets them by default.
- Deployment guide: **"Addressing: names, not IP addresses"** - how to
  keep an install working when the router's DHCP changes the board's
  IP, with no router changes: LocalHome reaches mosquitto at
  `localhost`, every device reaches the broker at `<name>.local:1883`,
  people reach the dashboard at `http://<name>.local:5000`. Covers what
  that makes the board's hostname responsible for (choose it before
  configuring devices, keep it unique, keep avahi running, same network
  segment), setting a Shelly's MQTT server including battery devices,
  and fallbacks (the router's DNS name, a static IP set on the board).
  The quick start now sets the hostname and installs avahi in step 1;
  `mqtt_broker.example.json` and the admin panel default to `localhost`.
- The VMC is configurable from `/climate-admin` like the other Shelly
  devices: one device id of its Shelly 0/1-10V Dimmer Gen3 builds the
  on/off switch "VMC" and its "VMC Speed" slider, with a live
  reachable/on/speed status. A dimmer already hand-wired in config.yaml
  is offered pre-filled. New `GET/POST /api/climate-admin/extra-dimmers`.
- A device the climate admin panel manages now replaces a config.yaml
  integration for the same device id, with a warning, instead of both
  polling and commanding the same hardware under two names.
- A **System status** section on `/climate-admin`, plus a problems box
  at the top of the page whenever something is wrong: integrations that
  failed to load (with the reason and, for a missing optional package,
  the install command), devices not responding with how long since
  their last update, a thermostat or history loop that stopped
  completing its cycles, and low disk space. It also shows the process's
  uptime, memory (current and peak), CPU and threads, the board's
  memory/load/uptime, and the history database's size and growth - the
  numbers that tell a slow leak or a filling disk apart from normal.
  New `GET /api/system-status`; see `services/system_status.py`.
- A clock in the header of both the dashboard and `/climate-admin`
  showing the **server's** date and time - not the browser's - plus a
  prominent warning when the server's timezone or clock disagrees with
  the device viewing it. The thermostat picks each hour's target from
  the server's clock, so a board left on its factory UTC default runs
  every schedule slot hours off, while a browser-side clock keeps
  showing the right time and hides it. That was live on the first real
  deployment (an Arduino UNO Q): 08:00 in the house, the 06:00 slot
  being applied. The warning names both zones, the resulting shift, and
  the exact `timedatectl` command to fix it. The schedule grid's "now"
  highlight follows the server clock too, so the lit-up cell is always
  the one actually being applied. New `GET /api/server-time`; see
  `services/system_clock.py`. The deployment guide now sets the
  timezone as part of the first-time setup.
- The zone chart now shows **why** a valve moved. It's redrawn as three
  panels on one time axis: temperature with the target the thermostat
  actually drove the valve against (a solid line inside its hysteresis
  band) and a dot wherever the valve opened; a strip showing exactly
  when the valve was open; and humidity in its own smaller panel. A
  readout above the chart gives the latest values, and follows the
  pointer (or a finger) across the chart instead of a tooltip covering
  the lines. Only a target that was really in control is recorded -
  away, diagnostic mode, an off hour, a cooling-excluded zone or an
  unreachable sensor leave a gap, and the chart says when no target was
  active in the whole range. Target recording starts from this version
  on; earlier history has no target to show.
- **Separate heating and cooling schedules.** Each zone now has one
  weekly grid per season, and the thermostat follows the one matching
  the current mode. With a single shared grid, switching the real
  deployment to cooling applied the winter setpoints as cooling
  targets - it was cooling a 26.5C room toward 19C. Stored additively:
  heating stays under `schedules.json`'s original `zones` key, cooling
  goes under `zones_cool`, so existing files need no migration and
  cooling simply starts empty (valves closed, with a notice on each
  zone card) until a summer schedule is painted. The dashboard labels
  which season's grid it's showing, and an edit saved in the same
  instant as a season switch still lands in the season it was painted
  for. `GET/POST /api/climate/zones/<name>/schedule` take an optional
  `mode`, defaulting to the current season.
- `ScheduleStore` now serializes its read-modify-write saves with a
  lock - two saves landing together (both zones' grids, or a grid and
  a season switch) could previously each read the old file and the
  second write would silently drop the first.
- The weekly schedule palette now depends on the season: heating paints
  from 16/18/**19**/20/21/22/24 (19 fills the 18-20 gap), cooling from
  its own 20-27 range, each with its own color scale - on the heating
  scale 24-27 would all have come out the same red. The weekly grid is
  still shared by both seasons; only the brush and colors change. A
  season switched from another device now re-draws an already-open
  dashboard's palette and grid within one refresh, instead of leaving
  the previous season's palette in place until a reload.

### Changed
- **The tests in parallel, and the slowest without real waits.**
  `pytest -n auto` (pytest-xdist, a development dependency) runs the
  suite in about 15 s instead of 50, `pytest -m ui -n 6` the browser
  tests in about a minute instead of five; CI does the same. The three
  browser tests that waited out the pages' 20 s refreshes and 15 s time
  limit run the page's clock forward instead - the same checks, a few
  seconds each instead of 97.
- **The administrator's guide explains the whole system; the admin page
  is shorter.** The guide (linked at the top of the admin page, printable
  to PDF) now has three parts with a table of contents: how the software
  and the plant work - seasons, the bathrooms, the VMC, the sensors,
  safety, restarts and power cuts as measured on the board, Wi-Fi, data
  and maintenance, diagnostic mode - then installing the Shellys, then
  what to do when something is missing. The admin page's "How it works"
  section is gone, its content now in the guide, and diagnostic mode and
  maintenance are one section.
- **The safety close comes 3 hours after a zone's sensor is unreachable,
  not 24.** An H&T is unreachable after 4 hours without a report, so its
  valve now closes 7 hours after the last one instead of 28 - a radiant
  panel left open blind for a day was too long. Still set in the admin
  panel (Plant tuning), from 1 hour up.
- **A new board is the apartment's climate, every Shelly to configure.**
  With nothing saved in the admin panel and no zones of its own in
  config.yaml, a board starts with Stanze and Bagni (Bagni out of
  cooling, the dehumidifier drawing its water through its valve), each
  with its H&T sensor and valve, plus the bypass valve, the
  dehumidifier, the outdoor sensor and the VMC - all on Shelly, none
  with an id yet, nothing simulated (it used to start with two
  simulated zones and a simulated VMC). A Shelly without an id saves,
  one at a time, builds nothing and isn't a fault: the panel lists it
  as *to configure* - by zone, with the model to look for - the
  dashboard says what isn't connected yet, a zone without its sensor
  keeps its valve closed, and `localhome-cli doctor` lists what's
  missing instead of failing. Simulated stays one choice away.
- Made for a phone in the hand: nothing overflows at 320px any more,
  every field is 16px so iOS stops zooming the page when one is tapped,
  and the buttons that were 28-38px tall (tabs, season chip, away,
  ranges, palette, panes) are at least 36-44px.
- The Climate tab, redesigned for everyday use:
  - At the top, the season ("Summer · Cooling", a link to where it's
    changed) and the outdoor temperature, next to "Away".
  - A tile per zone - temperature, humidity and what it's doing ("On ·
    heading for 25°", "Off · 27° reached"). A zone left out of cooling
    is a tile too, with its valve's real state ("Valve closed ✓") - it
    replaces the separate panel.
  - The zone picked there in detail: "System on/off" and why, the next
    scheduled change ("Next change at 17:00: 25° Comfort"), the sensor's
    freshness, battery, Wi-Fi and dew point on one line; then two panes,
    **Schedule** and **Statistics**, instead of one very long card. The
    zone and the pane are remembered.
  - The palette labels "Off" like Comfort and Economy, and fits on one
    row on a phone; copying Monday reads "Copy Monday to: Mon-Fri /
    Whole week".
  - "Other devices": switches first, cards as tall as their content, the
    admin panel's English device names translated, and a line under the
    devices the season drives ("Automatic: on in summer, off in winter",
    "Can only be switched on in summer").
  - Charts are drawn in real pixels and redrawn whenever their width
    changes, so they stay sharp and readable at any size - on a phone,
    full width on a desktop, or when a card grows.

- A zone's valve statistics became **run statistics**, worded for anyone
  ("system on/off" instead of "valve open/closed", on the whole
  dashboard), and pared down to what reads at a glance. One period
  (6 hours / 24 hours / 7 days) drives the whole pane: first the list of
  runs - time on and share of the period, how many times it switched on,
  then each run: when, how long, and what temperature and humidity did
  meanwhile (e.g. 25.9° -> 25.5°, -0.4°; one line per run on a wide
  screen); then the temperature and humidity charts in one box, the
  stretches the system was on shading both, hovering a run outlining it
  there; then one line with this week's and this month's totals. Gone:
  the today/week/month tiles, the separate "system" strip and switch-on
  dots under the chart, the hysteresis in the readout. The time axis
  labels as many round hours as fit, the temperature axis has a line per
  degree, and the target band is drawn without hairline gaps.
  `/api/climate/zones/<name>/on-hours` also returns `starts`
  (`HistoryStore.on_stats`, replacing `sum_on_hours`/`sum_on_hours_range`).
  The days before today are counted once a day (again after a clear or
  prune), so the 30s refresh reads only today's samples - ~15 times
  less work than the three month-long counts it replaces.
- Temperature and humidity are two separate charts now, each with its
  own title and time axis ("Temperature and valve" above, "Humidity"
  below) - one crowded plot mixing degrees and percent was hard to read.
  Hovering either marks the same moment on both; a sensor without
  humidity shows no empty chart.
- Automatic backups keep one history copy (was two), and skip it when it
  would leave less than 500 MB free on the disk.
- A device that stays unreachable is logged in full once, then with one
  line every 10 minutes while it keeps failing, and once more when it
  works again - with the MQTT broker down, the control loops used to log
  a traceback per device on every tick (hundreds of lines a minute). A
  switch named in the thermostat config that doesn't exist is reported
  once, not every tick.
- The admin panel's bathroom bypass valve and dehumidifier are shown
  only while cooling, like hand-written ones.
- The heating/cooling season moved from the dashboard's settings gear to
  a **Season** section at the top of `/climate-admin`: two cards, the
  active one marked, each listing what that season does on this
  installation (zones, valves held closed, bypass, dehumidifier).
  Switching needs the new season's name typed (ESTATE / INVERNO), and
  now updates the bypass and dehumidifier at once too. The dashboard
  shows the season read-only, linking there.
- The dashboard/API now runs on **Waitress**, a production WSGI server,
  instead of Flask's own development server (werkzeug's `app.run()`,
  which Flask itself says not to use in production). Still one
  multi-threaded process, so the thermostat and pollers run exactly
  once; pure Python, so it installs the same on the board and a PC. New
  dependency: `waitress` (ZPL-2.1). Reinstall after updating:
  `.venv/bin/pip install -e .`
- New `thermostat.mode_limited_switches`: a switch controlled by hand but
  usable in one mode only, forced off in any other - a dehumidifier used
  only while cooling no longer stays on, hidden, after switching to
  heating.
- A zone excluded from cooling (`cool_enabled: false`) now has its valve
  **held closed** in cool mode, checked against its real state every
  tick - it used to be left untouched, so a valve left open from the
  heating season (or opened by hand) stayed open all summer. A valve an
  interlock owns in cool mode is still left to that interlock.
- An interlock whose leader stops answering now switches its follower
  off instead of leaving it as it was.

### Fixed
- In the admin panel, the automatic-backup list and (new) settings only
  loaded after saving the VMC, not when the page opened.
- After the admin panel's "Restart LocalHome", the page could reload too
  early - on the old process, just before it turned over. It now waits
  2.5s before checking for the restarted server.
- The history database opened a new connection for every write and
  every chart read; the last one closing folded the whole WAL back into
  the database and deleted the -wal/-shm files, recreated on the next
  30s cycle - on the board, ~1.3 GB a day of eMMC writes for a few KB of
  samples. One connection is now held open and shared under a lock.
- The thermostat compared each command with the last one it sent, not
  with the valve's real state: a relay that rebooted into "off", or was
  switched from its own app, stayed wrong until the decision itself
  changed - a heating zone could keep cooling down with its valve shut.
  It now checks the real state whenever the valve is answering (and
  logs the correction), falling back to its last command only while the
  valve is offline.
- The admin panel's "Restart LocalHome" failed on the board with
  "Address already in use" (systemd brought it back 5s later): werkzeug
  marks its listening socket inheritable, so it survived the re-exec.
  It's now released just before.
- Shelly RPC replies went to a fixed `localhome/rpc` address: two
  LocalHome instances on one broker (a dev PC and the board) could take
  each other's replies - ids start at 1 in both - as their own. The
  reply address is now unique per process.
- Changing the broker's address (an IP to `localhost` or a `.local`
  name) dropped every MQTT device's saved last reading, so battery
  sensors read as unreachable until they next woke up (up to 2h). The
  same topic under the old address is used now.
- One log line per HTTP request made up ~98% of LocalHome's log
  (~33k lines a day with a dashboard open), all written to the board's
  eMMC by journald. Request logging is off; errors still show.
- Translated texts with the same placeholder twice (e.g. `{driver}`)
  only filled in the first one.
- The VMC speed set from the dashboard was never applied: LocalHome
  sent the brightness as `30.0`, which the Shelly 0/1-10V Dimmer Gen3
  silently ignores, while the dashboard showed the new value as if it
  had worked. It's now sent as a whole number (confirmed live: applied
  within the dimmer's 3s transition), and the card refreshes right
  after a change.
- `/climate-admin` kept saying "saved, but not running yet" after an
  extra device's id was cleared again without a restart - a cleared
  field and nothing saved build the same drivers, and are now treated
  the same.
- `localhome-cli doctor` said "No configuration problems found" even
  when a configured driver had failed to load (the real case: python-kasa
  not installed on the board, so the TP-Link devices silently never
  appeared). It now lists every integration that failed with its error,
  and names the pip extra to install when that's the cause. It also
  reported `config.yaml`'s default season instead of the one actually in
  force; it now reads `schedules.json` and flags diagnostic/away mode.
- The TP-Link LED strip drivers opened a python-kasa connection on
  every read and command and never closed it, leaking an aiohttp
  session, its socket and about one OS thread per call - with four
  drivers polling every 20s, ~17k a day, taking the process past 4.5GB
  of RAM in nine hours. Every connection now closes in a `finally`,
  including on a failed read. On the target Arduino UNO Q the process
  now holds steady at ~90MB (peak 91MB over 12h), 19 threads, 0.6% CPU.

### Changed
- `/climate-admin`'s "Reporting" status now shows a battery sensor's
  reading age too, not just the value - and past 30 minutes, frames it
  explicitly as normal ("30m ago - normal, this sensor reports on a
  change or roughly every 2h") rather than leaving a technician to
  guess whether an aging timestamp means trouble. Confirmed against an
  overnight watch of the two real H&T Gen3 sensors governing this
  apartment's climate: every heartbeat landed within about a minute of
  exactly 2h, all night, regardless of individual firmware version or
  WiFi signal - the panel was previously the one making a perfectly
  healthy sensor look like it might be dying.
- The battery sensor `stale_after_seconds` default dropped from 6h to
  4h (just under double the confirmed 2h heartbeat) - a genuinely dead
  sensor is now caught after missing two heartbeats instead of three,
  with the same one-missed-heartbeat tolerance as before.

### Fixed
- The "Xs/Xm/Xh ago" freshness shown for every `mqtt_json` sensor,
  switch and number was computed from whenever `SyncPoller` last
  *polled* (`poll_interval_seconds`, typically 20s), not from when the
  underlying MQTT message actually arrived - since MQTT is push-based,
  re-reading the same cached payload every poll made an untouched
  reading look freshly updated every single cycle, even hours after a
  battery sensor's last real report. The dashboard could show "5s ago"
  on a value that was actually hours old (confirmed live: a real
  Shelly H&T's reported freshness went from always ~5-20s to the
  genuine ~4-5 minutes since its last report once fixed). `core/poller.py`
  now asks the driver for its own `last_update_ts` when it defines one
  (the three generic MQTT drivers do, from the cached message's actual
  arrival time - see `drivers/mqtt/base.py`'s `get_received_at()`) and
  only falls back to poll-time otherwise, so every non-MQTT driver
  (Tuya, Meross, simulated, ...) is unaffected.

### Added
- `availability_topic`/`available_payload` on the generic `mqtt_json`
  sensor/switch/number drivers - points at a device's own MQTT Last
  Will and Testament (most Shelly Gen2/Gen3 devices publish `<id>/online`)
  for immediate, precise offline detection instead of waiting for
  `stale_after_seconds` to infer it from silence. Overrides even a
  still-fresh regular reading; a switch that only publishes on state
  change (which may have no sensible `stale_after_seconds` to set at
  all) can now still be detected as offline. Optional, off by default -
  every existing config.yaml keeps behaving exactly as before. See
  [docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline](docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline).
- Every *valve/switch* the `/climate-admin` panel builds (both zones'
  valves, the two extra switches) now gets the `availability_topic`
  above for free, derived automatically as `<device id>/online` - no
  field to fill in, since every Shelly publishes this itself. See
  `services/climate_map_store.py`. Deliberately **not** wired up for
  sensors (the zones' own H&T Gen3s, the outdoor sensor) - tried it
  live against the two real ones governing this apartment's climate:
  in battery mode, an H&T only actually connects to the broker to
  publish a threshold/heartbeat report, so its "online" topic reads
  false almost the entire time it's working perfectly normally, not
  just when something's wrong. A mains-powered relay has no such
  legitimate "disconnected on purpose" state, so it stays a
  meaningful signal there.
- A **sensor failsafe** on `ThermostatController`: a zone whose sensor
  stays actually unreachable (not just quiet - past
  `stale_after_seconds`) for longer than `sensor_failsafe_hours`
  (default 24) now has its valve forced closed automatically instead of
  staying frozen in whatever it was last doing indefinitely - clears
  itself the moment the sensor reports again. Surfaced on
  `/climate-admin`'s zone cards as a countdown/warning, not a push
  notification.
- **Diagnostic mode**: a new system-wide override, toggled from its own
  `/climate-admin` section, that takes every valve (both zones' + the
  bathroom bypass + the dehumidifier) out of automatic control entirely
  - `ThermostatController` and `InterlockController` both go hands-off
  on every tick while it's on - and exposes each one as a direct manual
  on/off switch (reusing the existing generic switch route, including
  for a mode_switch like the bypass valve, normally blocked from manual
  control). For on-site verification (does this relay actually switch?
  which physical valve is which name?) without the thermostat undoing a
  manual test moments later. Turning it back off resumes normal,
  sensor-driven control immediately, no restart needed - including
  force-closing any zone whose sensor is currently unreachable
  (`ThermostatController.resync_after_diagnostic_mode()`), since
  `tick()` would otherwise just skip such a zone and leave it exactly
  where a manual test left it. The panel's manual quick-set value box
  (for a zone in "manual" sensor mode) now also rejects a value outright
  while diagnostic mode is on, rather than accepting one that would sit
  there with no visible effect - the thermostat's `tick()` is a no-op
  the whole time diagnostic mode is active. See
  [docs/configuration.md#diagnostic-mode](docs/configuration.md#diagnostic-mode).
- The `/climate-admin` panel gained a "How this works" reference
  section explaining every battery-sensor edge case in place (a stable
  reading's 2h heartbeat, weak WiFi, low battery, the failsafe above) -
  replacing an earlier plan to push these as Telegram notifications, so
  the explanation lives next to the state it's explaining instead of a
  separate channel that needs its own bot token.

### Fixed
- `PositionStore.set()` (a cover's persisted 0-100% position) read-
  modified-wrote `positions.json` without the lock the rest of the
  class already uses - two covers finishing their moves close enough
  together (each on its own timer) could race, and the slower one's
  save silently overwrote the faster one's just-written position (on
  Windows, `os.replace()`'s stricter concurrent-rename semantics could
  even raise instead of just losing data). Confirmed with 20 covers
  finishing at the same instant losing positions before the fix, none
  after.
- `MerossPlugDriver.async_setup()` leaked the HTTP session and
  persistent MQTT connection meross-iot opens whenever the account
  login succeeded but the configured `device_name`/`uuid` didn't match
  a real device (or `async_device_discovery()` itself failed) -
  `AsyncPoller` retries `async_setup()` every `poll_interval_seconds`
  until it succeeds, so a permanently-wrong config leaked one of each,
  forever. Now closed on any failure after creation.
- `ClimateMapStore.set_zones()`/`set_extra_switches()`/`set_extra_sensors()`
  validated cross-references (duplicate device ids across zones/extra
  switches/the extra sensor) against a snapshot read *outside* the
  lock, then wrote inside it - two near-simultaneous saves (e.g. two
  browser tabs on `/climate-admin`) could validate against an
  already-stale snapshot. Now read, validated, and written against the
  same snapshot in one lock acquisition.
- Removed `EweLinkCloud.get_hours_kwh()` and `WS_DISPATCH_HOSTS` (an
  undocumented websocket-based 31-day hourly-kWh history) - unused
  anywhere in this project since it was added.
- The climate admin panel's fixed outdoor-sensor role built its
  Integration as `"Temperatura Esterna"` - the only non-English fixed
  name in an otherwise English-named system (`EXTRA_SWITCH_FIELDS` uses
  "Bathroom Bypass Valve"/"Dehumidifier"). Renamed to `"Outdoor Sensor"`
  for consistency.
- A `mqtt_json` climate/power_meter sensor whose topics all went past
  `stale_after_seconds` (most commonly a battery sensor like a Shelly
  H&T between wake cycles, not actually broken hardware) used to
  discard its last known reading entirely and show a blank
  "unreachable" card. It now keeps showing the last known value,
  labeled "last known value - no update in Xh", on the main dashboard,
  a zone's card, and the climate admin panel's live status alike -
  still `"ok": False` under the hood, so the thermostat/interlock still
  correctly never acts on it as if it were fresh. See
  `drivers/mqtt/sensor.py`'s `read()` and
  [docs/integrations/mqtt.md](docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline).

### Added
- A third fixed, standalone role in the climate admin panel: an
  outdoor temperature/humidity sensor, always a real Shelly H&T Gen3
  (no simulated/manual stand-in, unlike a zone's own sensor - there's
  no thermostat decision riding on this one). Same "leave blank if not
  wired yet" shape as the two extra switches, with its own live status
  readout. See
  [docs/configuration.md#the-shelly-climate-admin-panel](docs/configuration.md#the-shelly-climate-admin-panel).

### Fixed
- The main dashboard's per-zone chart could silently keep polling a
  sensor/valve name that stopped existing - `climateZones` (which the
  chart's own refresh loop reads `.sensor`/`.valve` from) was only ever
  set once, at page load; a climate-admin save+restart that renamed a
  zone's underlying driver (e.g. switching its sensor to/from "manual")
  while the dashboard tab was already open left that tab's chart stuck
  querying the old name until a full page reload. `refreshClimateZonesLive()`
  (already polling every 5s for the live numbers) now keeps this array
  in sync too.

### Changed
- Redesigned the climate admin panel (`/climate-admin`): each zone's
  config form and its live status (sensor reading, valve state, manual
  test buttons) now live in one merged card instead of two separate
  page sections - swapping a device id and checking whether it's
  actually reporting no longer means scrolling back and forth. The
  heat/cool mode banner moved to the top of the page (it decides which
  direction every valve's "open"/"closed" below actually means) and now
  spells out the formula explicitly ("valves open below target, close
  above it"), not just the mode name. A zone with `cool_enabled: false`
  now gets an explicit "excluded from cooling" notice while the
  thermostat is in cool mode, instead of showing a bare, unexplained
  valve state. The MQTT broker connection and the two extra switches -
  one-time setup, not part of the day-to-day "swap a sensor" loop - are
  now collapsed behind native `<details>` disclosures instead of
  competing for attention with the zone cards.

### Fixed
- The climate admin panel's "Restart LocalHome" button now refuses
  cleanly on Windows (`services/self_restart.py`'s new `is_supported()`)
  instead of attempting a restart that reliably leaves two processes
  fighting over the same port there - confirmed happening live during
  development. The button's success path also now polls for the server
  actually coming back instead of blindly reloading after a fixed 4
  seconds, which previously looked identical to "stuck" whenever a
  restart took a little longer.
- `python-kasa` (the `tplink_led_strip` driver's SDK) is
  GPL-3.0-or-later, unlike every other dependency here, but was sitting
  in `pyproject.toml`'s main `dependencies` - so every install pulled
  in GPL-licensed code whether or not you own any TP-Link devices, and
  `core/registry.py`'s driver loader had no fallback if it were ever
  missing, so the whole app would fail to start rather than just that
  one driver. Moved to its own `[tplink]` extra
  (`pip install -e ".[tplink]"`), and `load_builtin_drivers()` now logs
  and skips a driver module whose own package isn't installed instead
  of aborting startup. (The note on it in THIRD_PARTY_NOTICES.md went
  with the driver - see *Removed* above.)
- Every route now falls back to a clean `{"ok": false, "error": ...}`
  JSON response (logged server-side) for any exception nobody
  anticipated, instead of Flask's default HTML error page - which broke
  every frontend `fetch().then(r => r.json())` call with a JSON parse
  error instead of a clear message. A normal 404/405 is unaffected.
- `meross_plug`'s setup/read calls are now wrapped in an explicit
  timeout - meross-iot's own HTTP client doesn't document one, so
  during an internet outage a hung request (rather than one that
  fails/times out on its own) could have stuck that poller's
  retry-forever loop waiting indefinitely instead of actually retrying
  every interval. Tuya (LAN), eWeLink and Telegram already bounded
  every network call the same way.

## 2026-09-16

### Added
- The Shelly climate admin panel (`/climate-admin`) - maps the
  thermostat's two zones onto a home's own Shelly device ids from the
  browser, instead of hand-editing `config.yaml`, for redeploying the
  exact same setup across multiple homes that standardize on the
  Shelly family. Per zone: a sensor source (a real Shelly H&T, a
  simulated one, or a manually-typed debug value) and a valve source
  (real or simulated) - simulated + manual together let the whole
  sensor-to-valve control loop be verified, including manual open/close
  test buttons, before any relay is wired. Also edits the MQTT broker
  connection and two more fixed Shelly Plus 1 switches (a bathroom
  bypass valve, a dehumidifier), and shows live per-zone status with
  plain-language guidance (a battery sensor that hasn't reported *yet*
  reads differently from one that's gone offline; a dead broker is
  never mistaken for every device failing at once) and a "Restart
  LocalHome" button. See
  [docs/configuration.md#the-shelly-climate-admin-panel](docs/configuration.md#the-shelly-climate-admin-panel).
- WiFi signal strength (`wifi_rssi_dbm`) on a zone's sensor card,
  alongside the existing battery indicator - useful for judging a
  battery sensor's placement.
- A zone's dew point while cooling (condensation risk context) and a
  low-battery popup (once per session) when a sensor's battery drops
  below 10%.
- A season indicator (☀/❄) replacing the static "Heating & Cooling"
  heading.
- The Arduino UNO Q, this project's own day-to-day deployment target,
  noted alongside the existing Raspberry Pi/generic-Linux-board
  mentions (README, `docs/deployment.md`).

### Fixed
- `localhome-cli doctor` now cross-checks the climate admin panel's own
  saved zones/extra switches too, via the same merge `web/app.py`'s
  `create_app()` applies - previously an apartment set up entirely
  through the panel had no `thermostat:` block in `config.yaml` at
  all, so `doctor` silently checked nothing about it.
- A failed MQTT publish (most concretely: sent before the client
  finished its initial connection) no longer looks identical to a
  successful one - every switch/number/cover command already had
  error handling ready for this at every layer (the thermostat and
  interlock control loops, the `/api/sensors/...` routes), it just
  never fired because `publish()` never raised.
- The climate admin panel's manual-value debug route checked the
  *saved* zone mapping instead of what the running process actually
  loaded - a save that hadn't taken effect yet (no restart) could
  apply a fabricated reading to whichever real driver still owned that
  zone's name.
- `python3 -m venv`/a bare `pip install` now gets a virtual environment
  first in Quick start and the bare-metal deployment guide - modern
  Debian/Ubuntu actively refuses the latter
  (`externally-managed-environment`) and often doesn't ship the venv
  module by default either, confirmed setting this project up for real
  on its own Arduino UNO Q instance (Debian trixie). See
  [docs/troubleshooting.md](docs/troubleshooting.md).

## 2026-09-14

### Added
- `stale_after_seconds` on the generic MQTT driver (`sensor`/`switch`/
  `number`) - since MQTT only pushes when a device decides to, without
  this a dead device's last message was cached and returned as a fine,
  current reading forever. Opt-in (default: never expires), since a
  switch that only publishes on state change can legitimately go quiet
  for a long time without being offline - see
  [docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline](docs/integrations/mqtt.md#dhcp-devices-and-detecting-one-thats-gone-offline).
- A "last updated" age (e.g. "5m ago") on every sensor card, formatted
  as a human-friendly duration instead of raw seconds, and now also
  shown on each thermostat zone card (previously only on the generic
  Home-tab cards) alongside the sensor's own error message when it's
  unreachable.
- README screenshots (`docs/screenshots/`), captured against the
  `simulated` driver.
- A heating/cooling thermostat: a multi-zone valve control loop for a
  shared-plant radiant-panel setup (or anything similar) - a
  click-and-drag weekly setpoint schedule per zone, a shared heat/cool
  mode (behind a settings menu, persisted so it survives a restart), a
  live clock and "current hour" highlight on the schedule grid, and
  on-hours history (today/week/month) per zone. See
  [docs/configuration.md#thermostat-heatingcooling-zones](docs/configuration.md#thermostat-heatingcooling-zones).
- `Zone.cool_enabled` - a zone can opt out of one mode entirely (e.g. a
  bathroom whose radiant ceiling panel can't be cooled safely without
  risking condensation).
- `services/interlock.py` - a generic "switch A forces switch B to
  follow it" rule, for a device (e.g. a water-based dehumidifier) whose
  own operation depends on a specific valve regardless of that zone's
  own temperature control.
- Away mode: a one-tap "close every valve for N days" override, separate
  from the weekly schedule so nothing needs to remember to undo it - it
  resumes the normal schedule on its own once the period ends.
- `climate`/`switch` `simulated` driver - a fake sensor/switch (with a
  simple closed-loop model: an open valve nudges its paired sensor's
  reading toward a target) for trying out the dashboard, or building a
  whole thermostat setup, before owning any real hardware. See
  [docs/integrations/simulated.md](docs/integrations/simulated.md).
- `localhome-cli doctor` - cross-checks every zone/switch/sensor name
  `config.yaml` references against what's actually configured, catching
  typos that previously failed completely silently. See
  [docs/configuration.md#checking-a-new-setup-with-doctor](docs/configuration.md#checking-a-new-setup-with-doctor).
- `history.retention_days` config option (default 30) - rows older than
  this are pruned once a day, so the sqlite history database doesn't
  grow forever just because the process stays up for a long time.
- TP-Link Kasa/Tapo LED strip support (`tplink_led_strip`, `switch` +
  `number`), entirely over the LAN via
  [python-kasa](https://github.com/python-kasa/python-kasa) - on/off,
  read-only brightness on the switch card, and an optional brightness
  slider that renders inside the switch's own card via a new
  `paired_switch` config option. Its guide, `docs/integrations/tplink.md`
  (with the "Third-Party Compatibility" step some Tapo firmware needs,
  since python-kasa doesn't yet support its newer `TPAP` local-auth
  scheme), went with the driver - see *Removed* above.
- `tools/tplink_discover.py` to find TP-Link devices' IPs on the LAN.
- A startup log warning when two integration entries share a `name`
  (previously a silent card-swallowing bug, since `DeviceManager` keys
  pollers/sensor kinds by name across every kind).

### Fixed
- A switch/number command (turn on/off, set a slider value) now updates
  the dashboard's cached reading immediately on success, instead of
  waiting up to `poll_interval_seconds` for the next background poll.
  Previously the toggle/slider could visually snap back to its
  pre-command state for several seconds after a successful command.
- Schedule/mode persistence (`schedules.json`, `positions.json`) now
  writes atomically (temp file + fsync + rename) and falls back to
  defaults on a corrupt read instead of crashing on the next boot - a
  power loss mid-write could previously corrupt one of these files.
- `AsyncPoller` retries a failed `async_setup()` on the same interval
  instead of disabling that poller for the rest of the process's life -
  e.g. the MQTT broker or the network itself not being up yet when
  LocalHome starts used to be a permanent failure, not a transient one.
- `HistoryRecorder`'s background loop no longer dies silently on one
  failed database write (e.g. a transient SQLite lock) - caught and
  retried next interval, the same self-healing shape the thermostat and
  interlock loops already had.
- Editing a zone's schedule or the heat/cool mode now re-evaluates the
  affected valve(s) immediately instead of waiting up to
  `poll_interval_seconds` - previously the dashboard could briefly show
  a stale "target reached" status right after a change that should have
  opened the valve.
- The schedule grid's hour axis now lines up with the grid's actual
  columns (it previously used `justify-content: space-between` on a
  handful of labels, which visually looked like the schedule stopped at
  18:00 instead of running to 24:00).
- `tuya_air_quality` treats a status snapshot reporting both
  `temperature_c` and `humidity_pct` as exactly zero as a failed read
  instead of recording it - a rare but real stale/incomplete response
  from the device (indoor air can't physically be 0C and 0% RH at once),
  found auditing real collected history data. Previously showed up as a
  brief zero spike on the chart.
- The thermostat schedule now rejects a non-numeric hour value on save
  instead of persisting it - it would otherwise crash the entire
  `/api/climate/zones` endpoint (not just one zone) the moment that hour
  was reached. Also sanitizes any such value already on disk back to
  "no target" on read, so one bad legacy cell can't permanently block
  every future save of that zone (schedules always round-trip the whole
  week, not a diff). Found via automated UI/API stress-testing.

### Changed
- Each zone's temperature/humidity chart now overlays when its valve
  was on (a translucent band, plus the state in the hover tooltip),
  reusing the on/off history already recorded for the on-hours metric.
- The heat/cool mode indicator in the Climate tab is now a plain,
  non-interactive label next to the heading instead of a second button
  that duplicated the settings gear icon's job - the gear icon is the
  only place that actually changes it now.
- Dropped the covers "N of M open" header summary - redundant with each
  cover card's own state, and out of place while on the Climate tab.

## 2026-09-12
- Restructured the project into LocalHome, a generic, extensible
  local-first smart-home framework (plugin drivers, `core/interfaces.py`,
  `config/` split into real vs. example files, MIT license).
- Added English and Italian dashboard translations, switchable via
  `web.language` in config.yaml.

## 2026-09-09
- Added Meross smartplug control, Telegram power alerts, kWh history and
  shutter external-move tracking.
- Added the home panel to control LoraTap shutters and monitor power via
  a Sonoff POWCT relay.
