# LocalHome for the Windows desktop

A small window and a tray icon for a LocalHome board: the zones with their
comfort and a temperature for a while, outside, the house's power, the VMC's
levels and timed speed, the shutters, "away", the dehumidifier in summer; a
Windows notice when a zone turns on, when there's something to
check, when the board can't be reached.

A click on a zone, on "Fuori" or on "Consumo" opens its charts:
- the temperature and the humidity for 6 hours, 24 hours or 7 days, with the
  valve's openings under them;
- the outside temperature beside a zone's, when you ask for it;
- the lows and highs of each day, and the hours of heating of each day;
- the power, and the energy of each day.

Put the pointer on a chart to see its values at that time.

The second tab, **Impianto** (Ctrl+2; Ctrl+1 goes back to the house), is for
whoever installed the system. It only reads: to change something, its
"Pannello completo" button opens the board's own admin panel in the browser.
- At the top: a verdict ("Tutto in ordine", or how many things to fix) and the
  list of what to do, from the board's own problems.
- Six tiles: the devices, the MQTT broker, the board, the history, the
  season, the last backup.
- The devices by their place: each zone's sensor and valve, the plant, outside,
  energy, shutters. A device that does not answer or needs attention says why
  in a word. A click opens its details: model, firmware, signal with its 24-hour
  and 7-day means, battery, reports, restarts. A battery sensor also shows its
  last 7 days of battery and signal.
- The settings in cards: season and rules, zones and devices, thermostat, VMC,
  dehumidifier, board and system. A value shows where it comes from (the
  panel, config.yaml, the default) or that it differs from the factory value.

The tab never shows a device id (it holds the device's MAC), the Wi-Fi's name,
an address, the board's serial, the broker's user or the raw text of an
error. It reads the heavy endpoints rarely: the board's status every 2 minutes,
the Shellys' health only when it changes, the settings every 10 minutes, and
nothing while the window is in the tray.

Each zone's card shows its last 24 hours as a small line, with the valve's
openings under it. An arrow shows when the temperature went up or down by 0.2°
or more in the last hour. A zone that heats has the colour of the season.

On Windows 11 (22H2 and later), the window has Mica: the desktop's colour
shows through it, as in Windows' own Settings. The title bar follows Windows'
light or dark mode, and the icons are Windows' own (Segoe Fluent Icons).

**It's a separate app: nothing on the board changes.** No new endpoint, no
setting in the admin panel, nothing deployed. It talks to the API the board
already has, at the address it's given (IP and port). If a board's API changes,
this app is what has to follow.

## Using it

1. Run `LocalHome-Setup.exe`. It installs for this user only, with no
   administrator rights. Windows may warn about it the first time, as it's not
   signed: *More info*, *Run anyway*.
2. Give it the board's address: IP and port (`192.168.1.20:5000`) or its
   network name (`board.local`), with 5000 if no port is given. It's tried before
   it's saved. A name is optional: it's what the window and its notices call the
   board, otherwise LocalHome.

Closing the window leaves it in the tray, because the notices are its point. A
left click on the icon brings it back; a right click opens the menu:

| In the menu | What it does |
|---|---|
| In the icon | The temperature that the icon shows: a zone, outside, or only the colour |
| Always on top | The window stays over the others |
| Tell me when a zone turns on | Those notices on or off; the others always come |
| Start with Windows | On after installing; off and it only runs when opened |
| Board settings… | The board's name and address, also from the gear in the window |
| Exit | Closes it until it's opened again |

To remove it: Windows' *Installed apps*, LocalHome, Uninstall. This takes away
the program and its settings, and touches nothing on the board.

The icon's colour shows the state: green all fine, orange a zone on, amber
something to check, grey the board out of reach. By default the icon shows the
temperature of the first zone as a number, in a tile of that colour. When the
board is out of reach, the icon shows no number, because an old value is not
the value now.

In a narrow window, the charts replace the house; the arrow goes back. Make
the window wider (the button beside the gear, or drag its edge) and the house
and the charts are side by side. The house keeps its width. The link at the
bottom opens the full page in the browser.

## What it can and can't do

The program only passes on the requests the window needs, listed in
`MainForm.cs` (`Reads`, `Commands`) and logged when refused: reading the zones,
problems, devices, readings, VMC program, shutters and clock; for the charts,
reading a sensor's history, the zones' hours and lows and highs, the outdoor
days and the energy; for the Impianto tab, reading the board's status, the
Shellys' health, the broker, the zone map, the extra devices, the settings,
the VMC's speeds and automatic adjustments, the dehumidifier's settings, the
LED matrix, the backups and the house record (never the zips, which hold
secrets, nor the reads that contact a device or write on the board); setting the VMC's
speed, turning it on, its timed speed; moving a shutter; a zone's temperature
for a while, and back to its program; away and back; the dehumidifier on and off.
So it can't switch off the VMC, change a schedule or a season, or reach the admin
panel. A zone's temperature and "away" take two taps: the first only chooses,
nothing is sent until the time is picked. The other
switches of the board aren't shown at all. Its lists don't tell one device from
another: `sensors/<name>/on` and `set-value` are limited to the VMC by the page,
which has no button for anything else.

## How it's built

- `Program.cs`: alone (the file you download), the program is its own installer
  (`Setup.cs`); with its libraries beside it, it's the window (`App`, `MainForm.cs`).
- `MainForm.cs`: the window, the tray, the notices. The page lives at an address
  that exists only inside the program; what it asks under `/api/` is passed on to
  the board from here, so the board doesn't have to allow calls from elsewhere.
- `web/`: the page. Its own script and words, on purpose: it loads nothing from
  the board. `charts.js` draws the charts in plain SVG, with no library;
  `plant.js` is the Impianto tab. `texts.js` is Italian and English, the problems' words copied from
  LocalHome's locale files.
- It keeps its settings, its log (`app.log`) and WebView2's folder in
  `%LOCALAPPDATA%\LocalHome Desktop`. The log says what was sent, refused and
  notified, which is the place to look when a notice didn't come.

.NET Framework 4.8 and the WebView2 runtime are part of Windows 11, which is why
the installer is one file of about 1 MB.

## Building and testing

```bash
cd desktop
dotnet build -c Release      # writes dist/LocalHome-Setup.exe
pytest tests/ui/test_desktop_page.py -m ui -n 6     # the page, on simulated devices
```

To run the build without installing it:

```bash
bin/Release/net48/LocalHome.exe --data-dir <a folder> --address <ip:port>
```

`--tray` starts it hidden, as Windows does; `--name` sets the board's name. `--data-dir` keeps a trial's settings
apart from the installed copy's, and lets both run at once.
