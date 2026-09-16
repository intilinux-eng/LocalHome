# Deployment

`python run.py` is fine for trying LocalHome out. For something that
should survive a reboot and keep running unattended - an
[Arduino UNO Q](https://www.arduino.cc/), a Raspberry Pi, or any other
small Linux board next to your router is the typical target - pick one
of these. The Arduino UNO Q runs a real Linux userspace (SSH, bash,
systemd - not a microcontroller-only board), so everything below -
Docker and bare-metal/systemd alike - applies to it exactly as
written; no board-specific steps.

## Quick start: a fresh device, start to finish

The single ordered path from a brand-new board to a running,
survives-a-reboot dashboard - this is what to follow the first time,
skipping straight to [Docker](#docker) or the more hardened
[dedicated-user bare-metal setup](#bare-metal-systemd) below only if
you already know you want one of those instead. Confirmed against a
real Arduino UNO Q (Debian trixie).

```bash
# 1. On the device itself (SSH in first if it's headless):
sudo apt update
sudo apt install -y git python3-venv || sudo apt install -y python3.13-venv
# (the exact venv package name varies by distro version - if the first
#  one 404s, apt's own error names the right one; see below)

# 2. Get the code and its dependencies - a normal user account is fine,
#    no dedicated system user needed for a single-purpose device:
git clone <this-repo> ~/localhome
cd ~/localhome
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# 3. Configure it - either by hand:
cp config/config.example.yaml config/config.yaml
#    ...and the *.example.json files you need; see docs/configuration.md
#    and docs/integrations/ - or, if every device is Shelly, skip this
#    and use the climate admin panel instead (step 5).

# 4. Sanity-check before trusting it to run unattended:
.venv/bin/localhome-cli doctor

# 5. Make it survive a reboot and closing the SSH session - see
#    deploy/localhome.service (copy it, then edit User/WorkingDirectory/
#    ExecStart to match the path you cloned into above):
sudo cp deploy/localhome.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now localhome
sudo systemctl status localhome
# -> dashboard at http://<device-ip>:5000 ; logs: sudo journalctl -u localhome -f
```

If step 1's `python3-venv` package isn't found, or step 2's `python3 -m
venv .venv` itself fails with "ensurepip is not available", your
distro named the package differently - install the exact one apt's own
error message names (Debian trixie wants `python3.13-venv`) and retry.
See [docs/troubleshooting.md](troubleshooting.md) if anything else about
steps 1-4 goes sideways.

**Using Shelly devices** (the climate admin panel, `/climate-admin`)?
Two more things belong in this same "before you walk away" checklist:

- The MQTT broker (mosquitto) needs to survive a reboot exactly like
  LocalHome itself does above - `sudo apt install mosquitto` already
  auto-starts it via its own systemd unit, you just need your settings
  in `/etc/mosquitto/conf.d/` instead of the package default. See
  [docs/integrations/mqtt.md#2-keep-the-broker-itself-running](integrations/mqtt.md#2-keep-the-broker-itself-running).
- Map your zones onto this home's actual Shelly device ids from
  `/climate-admin` once the dashboard is up (step 5's URL) - see
  [docs/configuration.md#the-shelly-climate-admin-panel](configuration.md#the-shelly-climate-admin-panel).
  Remember it needs a restart (`sudo systemctl restart localhome`)
  after every save before a change actually takes effect.

## Docker

```bash
cp config/config.example.yaml config/config.yaml
# ...and the *.example.json files you need, same as any other setup -
# see docs/configuration.md.

docker compose up -d --build
```

`docker-compose.yml` bind-mounts `./config` into the container; nothing
under `config/` is ever baked into the image (see `.dockerignore`), so
the image itself stays generic and shareable while your real setup stays
on the host. Rebuild (`docker compose up -d --build`) after pulling code
changes; no rebuild is needed after editing anything under `config/`,
just restart (`docker compose restart`).

### Using the published image instead of building locally

`.github/workflows/docker.yml` builds a multi-arch (amd64 + arm64 -
covers Raspberry Pi/similar boards) image and pushes it to GitHub
Container Registry on every push to `main` and on version tags, once
this repo lives on GitHub - no manual publishing step, no secrets to set
up (it uses the repo's automatic `GITHUB_TOKEN`). Once that's run at
least once, pull it instead of building:

```bash
docker pull ghcr.io/<owner>/<repo>:latest
```

and swap `build: .` for `image: ghcr.io/<owner>/<repo>:latest` in
`docker-compose.yml` (or use `docker run` directly with the same
`-p 5000:5000 -v ./config:/app/config` shape as the compose file). This
skips the local build step entirely - handy on a low-power device like a
Raspberry Pi, where compiling anything is slower than just downloading a
finished image. Building locally (the default above) still works exactly
the same either way; use whichever fits how you deploy.

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
