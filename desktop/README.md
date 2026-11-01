# LocalHome for the Windows desktop

A small window and a tray icon for a LocalHome board: the zones with their
comfort, outside, the house's power, the VMC's levels and timed speed, the
shutters; a Windows notice when a zone turns on, when there's something to
check, when the board can't be reached.

**It's a separate app: nothing on the board changes.** No new endpoint, no
setting in the admin panel, nothing deployed. It talks to the API the board
already has, at the address it's given (IP and port). If a board's API changes,
this app is what has to follow.

## Using it

1. Run `LocalHome-Setup.exe`. It installs for this user only, with no
   administrator rights. Windows may warn about it the first time, as it's not
   signed: *More info*, *Run anyway*.
2. Give it the board's address, for example `192.168.1.20:5000`. It's tried
   before it's saved.

Closing the window leaves it in the tray, because the notices are its point. A
left click on the icon brings it back; a right click opens the menu:

| In the menu | What it does |
|---|---|
| Always on top | The window stays over the others |
| Tell me when a zone turns on | Those notices on or off; the others always come |
| Start with Windows | On after installing; off and it only runs when opened |
| Board address… | Another IP or port |
| Exit | Closes it until it's opened again |

To remove it: Windows' *Installed apps*, LocalHome, Uninstall. This takes away
the program and its settings, and touches nothing on the board.

The icon's dot says how things are: green all fine, orange a zone on, amber
something to check, grey the board out of reach. A zone opens the full page in
the browser.

## What it can and can't do

The program only passes on the requests the window needs, listed in
`MainForm.cs` (`Reads`, `Commands`) and logged when refused: reading the zones,
problems, devices, readings, VMC program, shutters and clock; setting the VMC's
speed, turning it on, its timed speed; moving a shutter. So it can't switch off
the VMC, change a schedule or a season, or reach the admin panel. The other
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
  the board. `texts.js` is Italian and English, the problems' words copied from
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

`--tray` starts it hidden, as Windows does. `--data-dir` keeps a trial's settings
apart from the installed copy's, and lets both run at once.
