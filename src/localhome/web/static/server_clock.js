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
// and fires a "serverclock:minute" event on document whenever the
// displayed minute changes, so a page can re-highlight "now" on time.
(function () {
  const RESYNC_MS = 60 * 1000;
  // Generous on purpose: this is about a board that's minutes or hours
  // off (no NTP), not about network jitter.
  const SKEW_WARN_MS = 2 * 60 * 1000;

  let state = null;       // { skewMs, offsetMin, tzName, tzAbbrev }
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

  function serverWallClock() {
    if (!state) return null;
    return new Date(Date.now() + state.skewMs + state.offsetMin * 60000);
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
    if (Math.abs(state.skewMs) > SKEW_WARN_MS) {
      messages.push(tr("clock.warning_skew", { minutes: esc(Math.round(Math.abs(state.skewMs) / 60000)) }));
    }

    banner.hidden = messages.length === 0;
    banner.innerHTML = messages.map(m => `<p>${m}</p>`).join("");
    const clock = document.getElementById("server-clock");
    if (clock) clock.classList.toggle("is-warning", messages.length > 0);
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
      };
      render();
      renderWarning();
    } catch (e) {
      // Keep ticking from the last known sync - a transient network blip
      // shouldn't blank the clock or raise a false alarm.
    }
  }

  window.ServerClock = { now: serverWallClock };

  sync();
  setInterval(sync, RESYNC_MS);
  setInterval(render, 1000);
})();
