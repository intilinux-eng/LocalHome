# Deployment

`python run.py` is fine for trying LocalHome out. For something that
should survive a reboot and keep running unattended - a Raspberry Pi or
small server next to your router is the typical target - pick one of
these.

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

For running directly on the target device without Docker:

```bash
sudo useradd --system --home /opt/localhome localhome
sudo git clone <this-repo> /opt/localhome
cd /opt/localhome
python3 -m venv .venv
.venv/bin/pip install .

cp config/config.example.yaml config/config.yaml
# ...edit it and the *.example.json files you need

sudo chown -R localhome:localhome /opt/localhome
sudo cp deploy/localhome.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now localhome
sudo journalctl -u localhome -f
```

`deploy/localhome.service` is a starting point, not a one-size-fits-all
unit - adjust `User`/`WorkingDirectory` if your layout differs, and
`ReadWritePaths` if a driver you add needs to write somewhere else.

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
