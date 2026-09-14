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

**A device that should follow another one (`interlocks:`) doesn't seem to react**
Check `active_in_mode` on that interlock entry - if it's set (e.g.
`"cool"`), the rule is deliberately a no-op in every other mode, leaving
the follower switch entirely to whatever else normally drives it (see
[docs/configuration.md](configuration.md#interlocks-one-switch-forced-to-follow-another)).
This is the single most common "it's not a bug" surprise with this
feature - a dehumidifier interlock scoped to `cool` genuinely does
nothing while the thermostat is in `heat` mode, on purpose. Check the
current mode (Settings, or `GET /api/climate/zones`) before assuming
something's broken.

**A zone's target/valve status looks stale right after editing the schedule or switching mode**
Should self-correct within one `poll_interval_seconds` at most - both
`/api/climate/zones/<name>/schedule` and `/api/climate/mode` already
trigger an immediate re-evaluation, not just a write to disk. If it's
still stale after that, check the browser console/network tab for a
failed request rather than assuming the control loop itself is wrong.

**I accidentally committed something under `config/`**
```bash
git rm --cached config/the-file-you-committed
git commit -m "Remove accidentally committed local config"
```
Then rotate whatever credential was in it (change the password/token) if
it was a secrets file - a `git rm` only stops *future* commits from
having it; it's still in history until you rewrite history, which is a
much bigger hammer.
