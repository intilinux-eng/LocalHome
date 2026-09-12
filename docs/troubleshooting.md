# Troubleshooting

Per-brand connection issues (Tuya cloud project errors, eWeLink login
failures, Meross region mismatches) are covered in their own
[docs/integrations/](integrations/) pages. This page covers the rest.

**`FileNotFoundError: Config file not found: .../config/config.yaml`**
You haven't created your own config yet:
```bash
cp config/config.example.yaml config/config.yaml
```
Then edit it and add the `devices_file`/`secrets_file` JSON files it
points at - see [docs/configuration.md](configuration.md).

**A driver fails to start but the rest of the dashboard works**
By design - `DeviceManager` logs the exception and skips that one
integration rather than crashing the whole app. Check the console output
for a traceback naming the failing `kind`/`type`, then check that
integration's `docs/integrations/*.md` page.

**`ValueError: No driver registered for kind=... type=...`**
Typo in `config.yaml`'s `kind`/`type`, or you're trying to use a driver
that doesn't exist in this checkout. List what's actually registered:
```bash
python -c "from localhome.core.registry import known_drivers; print(known_drivers())"
```

**Port 5000 already in use**
Set `web.port` in `config.yaml` to something else, or stop whatever else
is using it (5000 is also macOS's AirPlay Receiver port on some
versions).

**A cover's position estimate is wrong after a power cut / manual pull**
Expected - position is a time-based estimate, not a real sensor reading
(see [ARCHITECTURE.md](../ARCHITECTURE.md)). Move it fully open or fully
closed once via the dashboard/CLI to recalibrate; that's an exact
0%/100% regardless of what the estimate said before.

**I accidentally committed something under `config/`**
```bash
git rm --cached config/the-file-you-committed
git commit -m "Remove accidentally committed local config"
```
Then rotate whatever credential was in it (change the password/token) if
it was a secrets file - a `git rm` only stops *future* commits from
having it; it's still in history until you rewrite history, which is a
much bigger hammer.
