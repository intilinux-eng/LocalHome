# LocalHome's Debian package

One file that installs LocalHome on a board, or updates it: the program, its
service, the broker's configuration, the board's tuning. LocalHome's own
modules travel compiled (Cython): the package carries none of its Python
source. The pages, scripts and texts can't be hidden, since a browser reads
them.

## Building it

On a Windows PC with WSL2 and Docker in it (no pipeline, nothing else):

```bash
python tools/build_deb.py                      # arm64, for a board: dist/localhome_<version>_arm64.deb
python tools/build_deb.py --check              # then installed, started, updated, removed in a clean Debian
python tools/build_deb.py --arch amd64 --test  # this PC's processor: the unit tests against the compiled modules
```

It takes the files git knows, committed or not; a package built from changes
not committed has `.dirty` in its version. The build runs in `debian:trixie`,
the board's own Debian (Python 3.13), for the board's processor through QEMU:
what's compiled there runs on the board. The first arm64 build registers QEMU
with WSL's kernel, again after WSL restarts.

A module Cython can't compile goes as bytecode instead, still without its
source; the build lists them (today `services/run_hours.py`, for one starred
expression Cython doesn't take).

## Installing it on a board

```bash
scp dist/localhome_<version>_arm64.deb arduino@<board>:
ssh arduino@<board> sudo apt install -y ./localhome_<version>_arm64.deb
```

The same command updates it. What it does:

| Where | What |
|---|---|
| `/opt/localhome` | the program, in its own Python environment |
| `/var/lib/localhome` | its data: configuration, history, backups - never removed with the package |
| `localhome.service` | started at boot as `arduino`, restarted if it hangs or outgrows 512 MB |
| `/etc/mosquitto/conf.d/localhome.conf` | the broker open to the home network, for the Shellys |
| `/usr/lib/localhome/deploy/tune-board.sh` | run at each install and update (`/etc/default/localhome`: `TUNE_BOARD=0` to skip it) |
| security updates | Debian's security fixes, and nothing else, install at 03:30, never a reboot (`SECURITY_UPDATES=0` turns it off); `unattended-upgrades` comes as a dependency |

A first install starts from the factory configuration, or - on a board
installed by hand with git - copies its configuration, history and backups
from `~/localhome/config` (the old folder stays) and sets the old service
aside. Removing the package (`apt purge localhome`) leaves the data.

A board installed this way isn't updated with `tools/deploy_board.py`: that's
for the boards that run a git checkout. Updating it: `python tools/install_deb.py
<board>` (`--usb` over the cable), the newest package in `dist/`.

Not in the package yet: the LED matrix's sketch for the microcontroller
(docs/quick-installation-arduino-uno-q.md, step 11), nor the time zone and
the board's name (docs/arduino-uno-q.md, steps 1-2).
