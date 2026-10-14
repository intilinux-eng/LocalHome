/* The redesigned home page - see templates/home.html.

   Three questions, in the order people ask them:
   1. Glance - is the house comfortable, is it doing anything, does
      anything need me? A sentence at the top, then one card per zone.
   2. Tweak - warmer or off for a while, from the card itself.
   3. Plan - the week, in time bands instead of 168 cells, behind a
      button in a sheet.

   Everything is timed by the board's clock, not the phone's: schedules
   run on the board, so "until 20:30" must mean the board's 20:30.

   Its script in parts (static/home/), loaded in this order: core - this
   one: the board's API, the texts, the board's clock, numbers, a zone's
   week and its next change - then zones, zone-details, hero, plant,
   house, start. Plain scripts sharing one scope: a part uses what the
   others declare once the page is running; listening and refreshing
   start in start.js, loaded last. */
"use strict";

// --------------------------------------------------------------- basics --

function tr(path, vars) {
  const value = path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), window.I18N);
  let str = value != null ? value : path;
  if (vars) for (const k in vars) str = str.split(`{${k}}`).join(vars[k]);
  return str;
}

const LOCALE = (window.I18N && window.I18N.locale) || undefined;
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const REFRESH_MS = 20000;
const OFFLINE_RETRY_MS = 5000;   // the board not answering: asked again this often
// The history read every 5 minutes, as often as the board writes it: each
// read makes it write what's waiting, and its flash wears by the writes.
// A valve opening or closing has it read again sooner - once the board has
// a sample of it (one every 30 s) - and meanwhile the page counts an open
// valve's minutes itself (zones.js's liveRun()).
const HISTORY_MS = 5 * 60000;
const HISTORY_AFTER_CHANGE_MS = 45000;
const PROBLEMS_MS = 60000;
const DEFAULT_TARGET = { heat: 21, cool: 25 };
const TARGET_RANGE = { min: 5, max: 35 };
// A slow radiant ceiling does little in an hour: longer ones, and then
// until the schedule's next change, or until it's changed back by hand.
const DURATIONS = [2, 4, 8, 24];
const AWAY_OPTIONS = [24, 72, 168];
const AWAY_BACK_TIME = "18:00";   // the day back: from when, unless changed
// Which zones' details are open, kept in this browser: a convenience - a
// private window, or storage blocked, just starts as a first visit.
const MORE_OPEN_KEY = "localhome.zoneDetails";
function storedMoreOpen() {
  try {
    const kept = JSON.parse(localStorage.getItem(MORE_OPEN_KEY) || "{}");
    return kept && typeof kept === "object" ? kept : {};
  } catch (e) {
    return {};
  }
}

// The fixed roles' names, in the page's language.
const DISPLAY_NAMES = {
  "Outdoor Sensor": "climate.outdoor_name",
  "Bathroom Bypass Valve": "climate.bypass_name",
  "Dehumidifier": "climate.dehumidifier",
  "VMC": "home.vmc_name",
};

const state = {
  climate: null,
  devices: null,
  readings: {},
  weeks: {},              // zone -> {heat: week, cool: week}
  history: {},            // zone -> {temps: [...], runs: [...]}
  airHistory: {},         // air sensor -> its CO2 over the last 24 h
  energy: {},             // meter or plug -> its days, week, month, year (/api/energy)
  energyRange: {},        // meter or plug -> the period its bars show
  energyPick: {},         // meter or plug -> the bar tapped, to read its numbers
  // When the history was last read (the board's epoch, ms), and each
  // valve's opening and closing seen since: [from, to], `to` null while
  // it's open (start.js's noteValveChanges()).
  historyAt: 0,
  liveRuns: {},
  historyTimer: null,     // the read after a valve's change
  // Each zone's valve hours - the last 24 hours, the last 30 days - and
  // the plant's (services/run_hours.py); which zones show theirs open,
  // over how many days, and the bar picked.
  runHours: null,
  runsOpen: {},
  // Which zones show what their quick schedule's modes do.
  quickInfo: {},
  // Which zones show their details - remembered on this phone.
  moreOpen: storedMoreOpen(),
  // Each zone's lowest and highest of the last 7 days, and when
  // (/api/climate/temps) - today's live in dayRanges; which zones show
  // theirs open, and the bar picked.
  temps: null,
  tempsOpen: {},
  tempsPick: {},
  runsRange: {},
  runsPick: {},
  vmc: null,
  problems: null,
  pending: {},            // zone -> {target, hours | "next"}
  failures: 0,
  // Since when the board hasn't answered (null: it has), and the next try.
  offlineSince: null,
  retryTimer: null,
  sheet: null,            // what the sheet shows
};

function esc(value) {
  return String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function displayName(name) {
  return DISPLAY_NAMES[name] ? tr(DISPLAY_NAMES[name]) : name;
}

// A request has a time limit: one the board never answers (stuck, or
// gone without a word) would leave its button waiting for ever - and the
// browser's few connections to the board taken, every later request
// queued behind them. An action may wait on a Shelly a little longer.
const READ_TIMEOUT_MS = 10000;
const ACTION_TIMEOUT_MS = 15000;

async function api(path, body) {
  const options = body === undefined ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), body === undefined ? READ_TIMEOUT_MS : ACTION_TIMEOUT_MS);
  options.signal = controller.signal;
  let res, data;
  try {
    res = await fetch(path, options);
    data = await res.json().catch(() => ({ ok: false }));
  } catch (e) {
    // Said in the page's language, not the browser's "Failed to fetch".
    throw new Error(tr(controller.signal.aborted ? "home.no_answer" : "home.unreachable"));
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok || data.ok === false) {
    // A proxy's error page has no JSON: the status, not "Bad Gateway".
    const error = new Error(data.error || `${tr("home.request_failed")} (${res.status})`);
    error.data = data;
    throw error;
  }
  return data;
}

function errorText(error) {
  return apiError(error.data) || error.message || tr("home.request_failed");
}

// ------------------------------------------- what the shared grid needs --
// static/schedule_grid.js - the weekly grid - relies on these.

let climateZones = [];
let climateMode = "heat";

function slug(name) {
  return name.replace(/[^a-z0-9]/gi, "-").toLowerCase();
}

function escapeAttr(text) {
  return String(text).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function escapeHtml(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function jsArg(value) {
  return escapeAttr(JSON.stringify(String(value)));
}

// The server's refusal in this page's language.
function apiError(data) {
  if (!data) return "";
  const params = Object.assign({}, data.params || {});
  if (params.key) {
    const label = tr(`climate_admin.setting_${params.key}`);
    params.label = label.startsWith("climate_admin.") ? params.key : label;
  }
  if (params.period) {
    const period = tr(`errors.period_${params.period}`);
    if (!period.startsWith("errors.")) params.period = period;
  }
  if (typeof data.code === "string") {
    const text = tr(`errors.${data.code}`, params);
    if (text !== `errors.${data.code}`) return text;
  }
  return data.error || "";
}

function zoneExcluded(zone) {
  return climateMode === "cool" && zone.cool_enabled === false;
}

// Its Shellys configured: a sensor to go by, a valve to drive. Until then
// the zone says what isn't connected yet - the admin panel's to do, not a
// fault (the board keeps its valve closed without its sensor).
function zoneReady(zone) {
  return zone.sensor_configured !== false && zone.valve_configured !== false;
}

// "...sensor", "...valve" or "...both": the text for what's missing.
function setupKey(zone, prefix) {
  if (zone.sensor_configured === false && zone.valve_configured === false) return `${prefix}_both`;
  return `${prefix}_${zone.sensor_configured === false ? "sensor" : "valve"}`;
}

// A painted hour changes what the card says comes next: the card reads
// the grid being edited.
function updateZoneNext(zone) {
  const week = scheduleWeeks[zone.name];
  const mode = scheduleWeekModes[zone.name];
  if (week && mode) state.weeks[zone.name] = Object.assign({}, state.weeks[zone.name], { [mode]: week });
  const card = document.getElementById(zoneId(zone.name));
  const current = state.climate && state.climate.zones.find(z => z.name === zone.name);
  // Painted a moment ago: the grid stays under the finger. Loaded on its
  // own (opening the page): nothing was touched - holding the grid then
  // opened the page scrolled half-way down.
  if (card && current) steady(() => fillZone(card, current, state.climate), planTouchedRecently() ? card.querySelector(".zone-plan") : null);
}

// A week saved: the board ticked on it - the card's target, and the way
// back's, from it now rather than at the next round.
function onScheduleSaved() {
  refreshClimate();
}

function updateNowHighlight() {
  const { dayKey, hour } = scheduleNow();
  document.querySelectorAll(".sched-cell.now, .sched-day-label.now, .sched-hour-axis span.now").forEach(c => c.classList.remove("now"));
  document.querySelectorAll(".sched-day-label").forEach((label, i) => {
    if (DAY_KEYS[i % 7] === dayKey) label.classList.add("now");
  });
  document.querySelectorAll(`.sched-cell[data-day="${dayKey}"][data-hour="${hour}"]`).forEach(c => c.classList.add("now"));
  document.querySelectorAll(`.sched-hour-axis span[data-hour="${hour}"]`).forEach(c => c.classList.add("now"));
}

// ----------------------------------------------------------- the clock --
// The board's wall clock as a Date read with getUTC*(): its local time,
// whatever this phone's own time zone - from static/server_clock.js, the
// same clock the top bar shows (this device's own until its first sync).

function boardOffsetMin() {
  const offset = window.ServerClock && window.ServerClock.offsetMinutes();
  return offset == null ? -new Date().getTimezoneOffset() : offset;
}

// The offset the board had at a moment: the clocks change twice a year,
// and a time from before the change keeps the offset it had (this
// device's own rules before the first sync).
function boardOffsetAt(epochMs) {
  const offset = window.ServerClock && window.ServerClock.offsetAt && window.ServerClock.offsetAt(epochMs);
  return offset == null ? -new Date(epochMs).getTimezoneOffset() : offset;
}

function boardNow() {
  return (window.ServerClock && window.ServerClock.now()) || new Date(Date.now() + boardOffsetMin() * 60000);
}

// Now on the board's clock, as an instant (ms) - what the history's times are.
function boardEpochMs() {
  return wallToEpoch(boardNow());
}

function wall(epochMs) {
  return new Date(epochMs + boardOffsetAt(epochMs) * 60000);
}

// "2026-09-30T21:30:00" from the server is the board's own local time.
function boardDate(iso) {
  return /[zZ]|[+-]\d\d:\d\d$/.test(iso) ? wall(Date.parse(iso)) : new Date(Date.parse(iso + "Z"));
}

// A wall time back to its moment: the offset there, found from today's -
// in the hour October repeats, the one of the offset in force now.
function wallToEpoch(wallDate) {
  const ms = wallDate.getTime();
  let epoch = ms - boardOffsetMin() * 60000;
  for (let i = 0; i < 2; i++) epoch = ms - boardOffsetAt(epoch) * 60000;
  return epoch;
}

// False for a wall time the clock skips: 2:xx on the last Sunday of March.
function wallExists(wallDate) {
  return wall(wallToEpoch(wallDate)).getTime() === wallDate.getTime();
}

function hhmm(date) {
  return `${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}`;
}

function dayIndex(date) {
  return (date.getUTCDay() + 6) % 7;
}

function whenText(date) {
  const now = boardNow();
  const days = Math.round((Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate())
    - Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())) / 86400000);
  if (days === 0) return tr("home.at_time", { time: hhmm(date) });
  if (days === 1) return tr("home.tomorrow_at", { time: hhmm(date) });
  return tr("home.day_at", { day: tr(`home.day_long.${DAYS[dayIndex(date)]}`), time: hhmm(date) });
}

// "until 20:30", "until tomorrow at 06:00", "until Thursday at 18:00".
// Until when a zone's change by hand holds: a time, or until it's changed back.
function overrideUntil(override) {
  return override.until ? untilText(boardDate(override.until)) : tr("home.until_changed");
}

function untilText(date) {
  const now = boardNow();
  const days = Math.round((Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate())
    - Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())) / 86400000);
  if (days === 0) return tr("home.until_at", { time: hhmm(date) });
  if (days === 1) return tr("home.until_tomorrow", { time: hhmm(date) });
  return tr("home.until_day", { day: tr(`home.day_long.${DAYS[dayIndex(date)]}`), time: hhmm(date) });
}

// --------------------------------------------------------------- numbers --

function fmt(value, digits = 1) {
  return Number(value).toLocaleString(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtTarget(value) {
  return value == null ? tr("home.off") : `${fmt(value, Number.isInteger(value) ? 0 : 1)}°`;
}

function fmtDuration(hours) {
  const total = Math.round(hours * 60);
  const h = Math.floor(total / 60), m = total % 60;
  if (!h) return tr("home.minutes", { m });
  return m ? tr("home.hours_minutes", { h, m }) : tr("home.hours", { h });
}

function fmtAge(seconds) {
  if (seconds == null) return null;
  if (seconds < 90) return tr("home.just_now");
  if (seconds < 3600) return tr("home.min_ago", { m: Math.round(seconds / 60) });
  return tr("home.h_ago", { h: fmt(seconds / 3600, seconds < 36000 ? 1 : 0) });
}

// The same bands as humidityWord(), for the colour of its label.
function rhLevel(rh) {
  return rh < 30 ? "dry" : rh > 60 ? "humid" : "ok";
}

function humidityWord(rh) {
  if (rh < 30) return tr("home.rh_dry");
  if (rh > 60) return tr("home.rh_humid");
  return tr("home.rh_ok");
}

// ------------------------------------------------------------ schedules --

function nextChange(week, now) {
  if (!week) return null;
  const current = week[DAYS[dayIndex(now)]][now.getUTCHours()];
  const start = new Date(now.getTime());
  start.setUTCMinutes(0, 0, 0);
  for (let step = 1; step <= 7 * 24; step++) {
    const at = new Date(start.getTime() + step * 3600000);
    const value = week[DAYS[dayIndex(at)]][at.getUTCHours()];
    if (value !== current && wallExists(at)) return { at, target: value };   // services/thermostat.py's next_change()
  }
  return null;
}

function bands(day) {
  const out = [];
  day.forEach((value, hour) => {
    const last = out[out.length - 1];
    if (last && last.value === value) last.end = hour + 1;
    else out.push({ start: hour, end: hour + 1, value });
  });
  return out;
}
