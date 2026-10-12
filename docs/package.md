# The .deb package (compiled with Nuitka)

A board can run LocalHome two ways:

| | From git (development boards) | From the .deb |
|---|---|---|
| What's on the board | the source, a venv, git | a compiled program, no Python sources, no pip |
| Install / update | [arduino-uno-q.md](arduino-uno-q.md#install), `tools/deploy_board.py <board>` | `tools/deploy_board.py <board> --deb latest` (or `sudo apt install ./localhome_<version>_arm64.deb`) |
| Configuration and history | `~/localhome/config` | `/var/lib/localhome` |
| A change tried on the board | seconds | a new build: minutes to an hour |

Develop as always - on the PC, and on a development board updated from
git. Build the package for a board that just has to run.

## Build it

```bash
python tools/build_deb.py            # the current commit -> dist/localhome_<version>_arm64.deb
python tools/build_deb.py --check    # what it would build
```

It needs Docker: Docker Desktop, or Docker inside WSL2 (`sudo apt install
docker.io qemu-user-static binfmt-support` in the WSL distribution, your
user in the `docker` group) - `build_deb.py` finds either. What it does:

1. exports the commit with `git archive` - committed work only;
2. builds the build machine once (`packaging/Dockerfile`): arm64 Debian
   trixie, the UNO Q's own system, run by QEMU, with LocalHome's
   dependencies at `constraints.txt`'s versions and Nuitka;
3. compiles LocalHome with Nuitka, standalone - Python and every
   dependency inside (`packaging/build.sh`);
4. **smoke test**: starts the program with the factory configuration and
   asks its pages; a missing module or file fails the build here, not on
   the board;
5. lays out the package (`packaging/stage.py`) and packs it.

The version is `0.1.0+git<commit time>.<hash>`: every later commit is a
higher version, so apt sees an update. Under emulation the first build is
long (an hour or more); Nuitka's and ccache's caches stay in Docker's
`localhome-deb-cache` volume, so later builds compile only what changed.

## What it installs

| Where | What |
|---|---|
| `/opt/localhome/` | the program and everything it needs |
| `/var/lib/localhome/` | `config.yaml`, the admin panel's files, `data/history.db`, backups - kept when the package is removed, even purged |
| `localhome.service` | runs as `arduino`; restarted if it hangs (watchdog) or passes 512 MB |
| `/usr/lib/localhome/` | `tune-board.sh` with its watchdogs, the factory configuration |
| `/etc/default/localhome` | `TUNE_BOARD=1`, `TUNE_LEAN=0` - edit, then reinstall, to change |
| `/etc/mosquitto/conf.d/localhome.conf` | the broker on every interface, no password |

It depends on `mosquitto`, `avahi-daemon` and `libnss-mdns`, which apt
installs along. The board still needs its time zone and name set first
([arduino-uno-q.md](arduino-uno-q.md#install), steps 1-2).

## Install, update, go back

```bash
python tools/deploy_board.py <board> --deb latest     # copies the newest package over and installs it
python tools/deploy_board.py <board> --rollback       # the package installed before
```

Installing is the same for a new board and an update. On every install
the package tunes the board (`tune-board.sh`, `--lean` only with
`TUNE_LEAN=1`) and restarts LocalHome. The board keeps the last three
packages in `/var/cache/localhome/debs` for `--rollback`.

**From git to the package**: the first install finds
`~/localhome/config`, stops the old service, copies the configuration and
history to `/var/lib/localhome` (the original stays where it was) and sets
the old service aside as `/etc/systemd/system/localhome.service.pre-deb`.
From then on, `deploy_board.py` refuses a git update on that board.

**From the package back to git**, by hand: `sudo apt remove localhome`,
copy `/var/lib/localhome` back to `~/localhome/config`, and move the
`.pre-deb` service back.

## Good to know

- **The sources don't travel, the pages do.** A compiled program can be
  taken apart with effort, but not read; the web pages (HTML, JavaScript,
  CSS, translations) are sent to every browser and stay readable.
- **One system**: built against Debian trixie's C library, the UNO Q's.
- **Bugs only the compiled program has** - a module imported by name, a
  data file - are what the smoke test is for. The driver modules are
  listed in `core/registry.py` (a compiled program has no folders to
  walk); `tests/test_registry_modules.py` keeps the list complete.
