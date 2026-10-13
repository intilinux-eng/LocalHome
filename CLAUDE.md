# LocalHome - notes for Claude

Home automation on a small board in the house (an Arduino UNO Q running
Debian): Flask + Waitress, plain-JS pages, MQTT/Shelly devices, SQLite
history. Its focus is a real home's climate - two zones (rooms,
bathrooms) with valves, an outdoor sensor, a dehumidifier, a VMC - with a
home page for the residents and an admin panel for the installer. How the
pieces fit: [ARCHITECTURE.md](ARCHITECTURE.md); working on it:
[CONTRIBUTING.md](CONTRIBUTING.md).

## Spend context on the change, not on finding it

- **Find, then read.** Search for the function, the route or the text key
  and read that part (offset/limit), not whole files. Every file opens
  with a comment saying what it holds; `static/home/*.js` and the route
  modules are split by area for this.
- **Reuse before writing anything new:**
  - a use case of the home page → `tests/ui/test_end_user.py` (the `walk`
    fixture, `w.check(...)` after each step). Add the case there as a test;
    never write a one-off script to walk the page;
  - a look at the page → `python tools/screenshot_home.py OUT --state all`
    (widths, `--dark`); one-off screenshot scripts only for a state it
    doesn't have - and then add the state to the tool;
  - a server with simulated devices → `tests/ui/conftest.py` (`_launch`,
    `CONFIG`).
- **Tests: only what's needed, when it's needed.** Run the tests the
  change touches (`-k`), the full suites once before a commit, and a run
  only when the user asks for one. Don't repeat suites to chase a known
  Windows-only flake (a `PermissionError` replacing `schedules.json` while
  it's read - the board runs Linux).
- **One topic per session.** A long session costs more than any file;
  what's worth keeping goes in the memory or here.

## Where things are

| For | Look in |
|---|---|
| The home page (residents) | `src/localhome/web/templates/home.html`, `static/home.css`, `static/home/*.js` |
| ... its script, in load order | `core` (API, texts, clock, weeks) · `zones` (a zone's card, modes) · `zone-details` (table, hours, lows and highs) · `hero` (the top, its sheets) · `plant` (dehumidifier, VMC) · `house` (lights, air, energy) · `start` (refreshes, listeners, start) |
| The admin panel (installer) | `templates/climate_admin.html`, `static/climate_admin.js`, `static/admin.css` |
| The weekly table (both pages) | `static/schedule_grid.js` |
| Redrawing without losing taps/scroll | `static/morph.js` (`morph()`, `steady()`) |
| Building the app, starting it | `src/localhome/web/app.py` (`create_app()`, `main()`) |
| The endpoints, by area | `web/routes/`: `pages`, `system` (health, settings, restart, maintenance), `devices`, `plant` (VMC, dehumidifier), `climate` (zones, modes, schedules, away), `admin` (the panel), `common` (request helpers) |
| Caching / gzip of pages | `web/transfer.py` |
| Texts, both languages | `web/locales/it.json`, `en.json` - same keys in both, always |
| Thermostat, schedules, overrides | `services/thermostat.py` |
| History, daily summaries | `services/history.py`, `services/run_hours.py`, `services/day_range.py` |
| Energy meters | `services/energy.py` |
| VMC | `services/vmc_*.py` |
| Board health, boots, diagnostics | `services/system_status.py`, `services/diagnostics.py`, `services/maintenance.py` |
| Devices | `drivers/<brand>/` - registered with `@register_driver`, see `core/registry.py` |
| Board setup and deploy | `deploy/` (service, `tune-board.sh`, watchdogs), `tools/deploy_board.py` |
| The installer's guide | `static/guide/shelly-plus1-valvole.html` |

## Commands

```bash
python run.py                                  # the dashboard locally (config/config.yaml)
pytest -n auto                                 # unit tests, ~15 s
pytest -m ui -n 6                              # browser tests (Playwright), ~1 min
pytest -m enduser -n 6                         # the end-user suite alone, ~20 s
python tools/screenshot_home.py OUT --state all  # the home page in each state
python -m ruff check --select F <files>        # undefined names, unused imports
```

## Conventions

- Comments say what a piece does for the person using it, and why, in
  plain sentences - match the surrounding style.
- Every user-visible text goes through the locale files, in both languages.
- A user-visible change gets an entry under `## Unreleased` in CHANGELOG.md.
- A use case found in real use becomes a test in `test_end_user.py`.
- Files use CRLF line endings on this machine; edit with tools that keep them.
- Small, single-purpose commits.

## Care

- `config/` holds the real installation: `config.yaml`, `secrets_*.json`,
  device files with keys. Never print their secrets, never commit them -
  only the `*.example.*` files are tracked.
- Real boards run real devices (the VMC is in daily use): don't drive a
  device on a board, restart it or change its settings unless asked. A
  board's SSH password comes only from the `LH_SSH_PW` environment
  variable, never written to a file.
- A diagnostics zip contains a config backup: keep it out of the repo.
