"use strict";

// The server's date and time, not the browser's - loaded by both the
// dashboard and /climate-admin, standalone on purpose (no dependency on
// either page's own bundle).
//
// Why the server's: the thermostat picks each hour's target from the
// server's own clock and timezone (services/thermostat.py). A board left
// on its factory UTC default applies every schedule slot hours off, while
// a clock drawn from `new Date()` keeps showing the right time and hides
// it - exactly what happened on the first real deployment. Showing the
// server's time, and saying so loudly when it disagrees with this
// device, turns a silent 2h shift into something an installer can't miss.
//
// Exposes window.ServerClock:
//   now()   -> a Date whose *UTC* fields are the server's local wall-clock
//              (read it with getUTCHours()/getUTCDay() only), or null
//              before the first sync
//   offsetMinutes() -> the server's offset from UTC, or null before then
//   offsetAt(epochMs) -> the offset the server's clock had at that moment
//              (the clocks change twice a year), or null before then
// and fires a "serverclock:minute" event on document whenever the
// displayed minute changes, so a page can re-highlight "now" on time.
(function () {
  const RESYNC_MS = 60 * 1000;
  // Generous on purpose: this is about a board that's minutes or hours
  // off (no NTP), not about network jitter.
  const SKEW_WARN_MS = 2 * 60 * 1000;

  let state = null;       // { skewMs, offsetMin, tzName, tzAbbrev }
  let boot = {};          // { bootedAt, startedAt } - epoch seconds, from the server
  let appVersion = null;  // the dashboard's files as first loaded - see showUpdateBanner()
  let lastMinuteKey = null;

  function tr(path, vars) {
    const value = path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), window.I18N);
    let str = value != null ? value : path;
    if (vars) for (const k in vars) str = str.split(`{${k}}`).join(vars[k]);
    return str;
  }

  // The warning templates carry their own markup (a <code> for the fix
  // command), so every *value* spliced into them is escaped instead.
  function esc(value) {
    return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  function locale() {
    return (window.I18N && window.I18N.locale) || undefined;
  }

  function formatOffset(minutes) {
    const sign = minutes < 0 ? "-" : "+";
    const abs = Math.abs(minutes);
    const h = Math.floor(abs / 60);
    const m = abs % 60;
    return `UTC${sign}${h}${m ? ":" + String(m).padStart(2, "0") : ""}`;
  }

  function formatShift(minutes) {
    const abs = Math.abs(minutes);
    const h = Math.floor(abs / 60);
    const m = abs % 60;
    if (!m) return tr("clock.hours", { n: h });
    if (!h) return tr("clock.minutes", { n: m });
    return `${tr("clock.hours", { n: h })} ${tr("clock.minutes", { n: m })}`;
  }

  // Linux reports a real abbreviation ("CEST"); Windows reports a long
  // localized name ("ora legale Europa occidentale") that wraps the header
  // clock onto three lines on a phone. Only use it when it's short enough
  // to be an actual abbreviation, and fall back to "UTC+2" otherwise.
  function shortZone() {
    const abbrev = state.tzAbbrev || "";
    return abbrev && abbrev.length <= 6 && !/\s/.test(abbrev) ? abbrev : formatOffset(state.offsetMin);
  }

  // The board's offset at any moment, by its time zone's own rules where
  // this browser knows the zone: the clocks change twice a year, and a
  // time from before the change - last night's lowest, a day in last
  // week's table - is told with the offset it had then, not today's. The
  // last sync's offset where the browser doesn't know the zone, or its
  // rules disagree with what the board says (a name not to trust).
  let zoneFormat = null;  // { zone, format }

  function zoneOffsetAt(epochMs) {
    if (!state || !state.tzName) return null;
    try {
      if (!zoneFormat || zoneFormat.zone !== state.tzName) {
        zoneFormat = { zone: state.tzName, format: new Intl.DateTimeFormat("en-US", {
          timeZone: state.tzName, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric",
          hour: "numeric", minute: "numeric", second: "numeric" }) };
      }
      const p = {};
      zoneFormat.format.formatToParts(new Date(epochMs)).forEach(({ type, value }) => { p[type] = Number(value); });
      const wallMs = Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second);
      return Math.round((wallMs - Math.floor(epochMs / 1000) * 1000) / 60000);
    } catch (e) {
      return null;        // a zone this browser doesn't know
    }
  }

  function offsetAt(epochMs) {
    if (!state) return null;
    const zoned = state.zoneAgrees ? zoneOffsetAt(epochMs) : null;
    return zoned == null ? state.offsetMin : zoned;
  }

  function serverWallClock() {
    if (!state) return null;
    const now = Date.now() + state.skewMs;
    return new Date(now + offsetAt(now) * 60000);
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el && el.textContent !== text) el.textContent = text;
  }

  function render() {
    const wall = serverWallClock();
    if (!wall) return;

    // timeZone: "UTC" because `wall` already carries the server's offset -
    // formatting it as UTC prints the server's wall-clock verbatim,
    // whatever timezone this browser happens to be in.
    const opts = { timeZone: "UTC" };
    setText("server-clock-time", wall.toLocaleTimeString(locale(), { ...opts, hour: "2-digit", minute: "2-digit" }));
    setText("server-clock-date", wall.toLocaleDateString(locale(), { ...opts, weekday: "long", day: "numeric", month: "long", year: "numeric" }));
    setText("server-clock-date-short", wall.toLocaleDateString(locale(), { ...opts, weekday: "short", day: "numeric", month: "short" }));
    const clock = document.getElementById("server-clock");
    const fullZone = clock && clock.dataset.zone === "full";
    setText("server-clock-zone", fullZone
      ? `${state.tzName || shortZone()} · ${formatOffset(state.offsetMin)}`
      : shortZone());

    if (clock) {
      clock.title = tr("clock.tooltip", {
        zone: state.tzName || shortZone(),
        offset: formatOffset(state.offsetMin),
      });
    }

    const minuteKey = `${wall.getUTCDate()}-${wall.getUTCHours()}-${wall.getUTCMinutes()}`;
    if (minuteKey !== lastMinuteKey) {
      lastMinuteKey = minuteKey;
      document.dispatchEvent(new CustomEvent("serverclock:minute"));
    }
  }

  function renderWarning() {
    const banner = document.getElementById("server-clock-warning");
    if (!banner || !state) return;

    // getTimezoneOffset() is minutes *behind* UTC, hence the sign flip.
    const deviceOffset = -new Date().getTimezoneOffset();
    const messages = [];

    if (state.offsetMin !== deviceOffset) {
      const deviceZone = (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || "Europe/Rome";
      messages.push(tr("clock.warning_timezone", {
        server: esc(`${state.tzName || shortZone()} (${formatOffset(state.offsetMin)})`),
        device: esc(formatOffset(deviceOffset)),
        shift: esc(formatShift(deviceOffset - state.offsetMin)),
        zone: esc(deviceZone),
      }));
    }
    // Not synchronized over NTP yet (right after a power-on, or no internet
    // at all): the board runs on the time it last saved, and so do the
    // schedules. That's the cause, so it's what's said - with how far off
    // it is from this device when that's noticeable.
    if (state.clockSynced === false) {
      messages.push(tr("clock.warning_unsynced"));
      if (Math.abs(state.skewMs) > SKEW_WARN_MS) {
        messages.push(tr("clock.warning_skew", { minutes: esc(Math.round(Math.abs(state.skewMs) / 60000)) }));
      }
    } else if (Math.abs(state.skewMs) > SKEW_WARN_MS) {
      messages.push(tr("clock.warning_skew", { minutes: esc(Math.round(Math.abs(state.skewMs) / 60000)) }));
    }

    banner.hidden = messages.length === 0;
    banner.innerHTML = messages.map(m => `<p>${m}</p>`).join("");
    const clock = document.getElementById("server-clock");
    if (clock) clock.classList.toggle("is-warning", messages.length > 0);
  }

  // LocalHome was updated while this page stayed open: it's still running
  // the old code, which may not know about new data (it once hid a whole
  // new status group). Offer a reload instead of forcing one mid-edit.
  function showUpdateBanner() {
    if (document.getElementById("update-banner")) return;
    const banner = document.createElement("div");
    banner.id = "update-banner";
    banner.className = "update-banner";
    banner.innerHTML = `<span>${esc(tr("clock.update_available"))}</span>`
      + `<button type="button">${esc(tr("clock.update_reload"))}</button>`;
    banner.querySelector("button").addEventListener("click", () => location.reload());
    document.body.prepend(banner);
  }

  async function sync() {
    try {
      const sent = Date.now();
      const res = await fetch("/api/server-time", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const received = Date.now();
      // Compare against the request's midpoint, so a slow round trip on a
      // busy board isn't mistaken for the server's clock being off.
      state = {
        skewMs: data.epoch_ms - (sent + received) / 2,
        offsetMin: data.utc_offset_minutes,
        tzName: data.tz_name,
        tzAbbrev: data.tz_abbrev,
        clockSynced: data.clock_synced,
      };
      state.zoneAgrees = zoneOffsetAt(data.epoch_ms) === data.utc_offset_minutes;
      boot = { bootedAt: data.booted_at, startedAt: data.started_at };
      render();
      renderWarning();
      if (data.app_version) {
        if (appVersion === null) appVersion = data.app_version;
        else if (data.app_version !== appVersion) showUpdateBanner();
      }
    } catch (e) {
      // Keep ticking from the last known sync - a transient network blip
      // shouldn't blank the clock or raise a false alarm.
    }
  }

  // How long the board (and LocalHome on it) has been up, in seconds -
  // null until the first sync, or where the board can't tell us.
  function upFor() {
    // "Now" on the board's own clock, like bootedAt/startedAt: right after
    // a power-on the board runs on yesterday's time until NTP corrects it
    // (8 minutes, measured), and mixing the phone's clock with the board's
    // turned the difference into "on for 17h".
    const now = (Date.now() + (state ? state.skewMs : 0)) / 1000;
    return {
      board: boot.bootedAt ? now - boot.bootedAt : null,
      process: boot.startedAt ? now - boot.startedAt : null,
    };
  }

  window.ServerClock = { now: serverWallClock, upFor, offsetAt, offsetMinutes: () => (state ? state.offsetMin : null) };

  sync();
  setInterval(sync, RESYNC_MS);
  setInterval(render, 1000);
})();
