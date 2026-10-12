/* The redesigned home page - see templates/home.html.

   Three questions, in the order people ask them:
   1. Glance - is the house comfortable, is it doing anything, does
      anything need me? A sentence at the top, then one card per zone.
   2. Tweak - warmer or off for a while, from the card itself.
   3. Plan - the week, in time bands instead of 168 cells, behind a
      button in a sheet.

   Everything is timed by the board's clock, not the phone's: schedules
   run on the board, so "until 20:30" must mean the board's 20:30. */
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
const HISTORY_MS = 5 * 60000;
const PROBLEMS_MS = 60000;
const DEFAULT_TARGET = { heat: 21, cool: 25 };
const TARGET_RANGE = { min: 5, max: 35 };
// A slow radiant ceiling does little in an hour: longer ones, and then
// until the schedule's next change, or until it's changed back by hand.
const DURATIONS = [2, 4, 8, 24];
const AWAY_OPTIONS = [24, 72, 168];
const AWAY_BACK_TIME = "18:00";   // the day back: from when, unless changed

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
  onHours: {},
  vmc: null,
  problems: null,
  pending: {},            // zone -> {target, hours | "next"}
  failures: 0,
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

function boardNow() {
  return (window.ServerClock && window.ServerClock.now()) || new Date(Date.now() + boardOffsetMin() * 60000);
}

function wall(epochMs) {
  return new Date(epochMs + boardOffsetMin() * 60000);
}

// "2026-09-30T21:30:00" from the server is the board's own local time.
function boardDate(iso) {
  return /[zZ]|[+-]\d\d:\d\d$/.test(iso) ? wall(Date.parse(iso)) : new Date(Date.parse(iso + "Z"));
}

function wallToEpoch(wallDate) {
  return wallDate.getTime() - boardOffsetMin() * 60000;
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
    if (value !== current) return { at, target: value };
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

// ------------------------------------------------ what the valve does --
// No forecasts: the sensors report every couple of hours, too seldom to
// tell when a room will get there. What is certain is the thermostat's
// own rule (services/thermostat.py's decide()): while heating it opens
// below target - hysteresis and closes above target + hysteresis, and in
// between keeps doing what it was doing; cooling is the mirror image.

// The thermostat's own two numbers for this target (services/
// thermostat.py's thresholds()): it stops at the target, and opens again
// half a degree away - an economy target further on.
function valveRule(zone, climate) {
  // A valve that doesn't answer won't do what the rule says.
  if (zone.target_c == null || zone.temperature_c == null || zone.valve_opens_at == null
      || !zone.sensor_ok || !zone.valve_ok) return null;
  const cool = climate.mode === "cool";
  if (zone.valve_on) return tr(cool ? "home.runs_until_cool" : "home.runs_until_heat", { value: fmt(zone.valve_stops_at) });
  return tr("home.restarts_at", { value: fmt(zone.valve_opens_at) });
}

// Comfort or economy, as the palette calls it - with the same icon.
function tierChip(tier, mode) {
  if (!tier || typeof TIER_ICONS === "undefined") return "";
  return `<span class="tier-chip" data-tier="${tier}">${TIER_ICONS[tier === "comfort" ? mode : "economy"]}${esc(tr(`climate.tier_${tier}`))}</span>`;
}

// Under the palette: what the two levels do, in this season, with the
// thermostat's numbers.
function tierNoteHtml() {
  const climate = state.climate || {};
  const h = climate.hysteresis_c, e = climate.economy_tolerance_c;
  if (h == null) return "";
  return `<p class="sched-tier-note">${esc(tr(climateMode === "cool" ? "climate.tier_note_cool" : "climate.tier_note_heat",
    { comfort: fmt(h), economy: fmt(h + (e || 0)) }))}</p>`;
}

// "0.8° below target", "at target" - to the tenth the sensor reports.
function gapText(zone) {
  if (zone.target_c == null || zone.temperature_c == null) return "";
  const gap = Math.round((zone.temperature_c - zone.target_c) * 10) / 10;
  if (gap === 0) return tr("home.at_target");
  return tr(gap < 0 ? "home.gap_below" : "home.gap_above", { value: fmt(Math.abs(gap)) });
}

// Its target reached: on the side the season wants it, within the
// hysteresis. Cooling, at or below the target - 24.3° with a 24° target is
// above it, even if the valve waits for 24.5° to open again (seen: it said
// "just right"); heating, at or above. To the tenth the sensor reports.
function targetReached(zone, climate) {
  if (zone.target_c == null || zone.temperature_c == null) return false;
  const h = climate.hysteresis_c || 0.5;
  const gap = Math.round((zone.temperature_c - zone.target_c) * 10) / 10;
  return climate.mode === "cool" ? gap <= 0 && gap >= -h : gap >= 0 && gap <= h;
}

// ----------------------------------------------------------- the zones --

function zoneStatus(zone, climate) {
  const mode = climate.mode;
  if (!zoneReady(zone)) return { tone: "off", text: tr(setupKey(zone, "home.st_setup")) };
  // Held open by another device (a dehumidifier drawing its water
  // through this valve) - even in a zone that sits out the cooling.
  if (mode === "cool" && !zone.cool_enabled) return { tone: "off", text: tr("home.st_excluded") };
  // Its Shelly not answering - unplugged, out of the Wi-Fi: whatever the
  // thermostat decides, the valve won't follow. Seen: a cooling target
  // below the room's, the valve offline, and the card said "resting".
  if (zone.valve_ok === false) return { tone: "warn", text: tr("home.st_valve_offline") };
  if (zone.sensor_failsafe_active) return { tone: "warn", text: tr("home.st_failsafe") };
  if (zone.sensor_manual && !zone.sensor_ok) return { tone: "warn", text: tr("home.st_manual_waiting") };
  // The board just came up: the sensor hasn't reported since - not a fault.
  if (zone.sensor_waiting) return { tone: "off", text: tr("home.st_first_report") };
  if (!zone.sensor_ok && zone.temperature_c == null) return { tone: "warn", text: tr("home.st_waiting") };
  if (!zone.sensor_ok) return { tone: "warn", text: tr("home.st_stale") };
  if (climate.away_until) return { tone: "off", text: tr("home.st_away") };
  if (zone.target_c == null) return { tone: "off", text: tr("home.st_off") };
  if (zone.valve_on) return { tone: "active", text: tr(mode === "cool" ? "home.st_cooling" : "home.st_heating"), active: true };
  const reached = targetReached(zone, climate);
  return { tone: "ok", text: tr(reached ? "home.st_reached" : "home.st_idle"), reached };
}

function zoneId(name) {
  return "zone-" + name.replace(/[^a-z0-9]/gi, "-").toLowerCase();
}

function renderZones() {
  const climate = state.climate;
  const host = document.getElementById("zones");
  if (!climate || !climate.zones.length) {
    morph(host, `<div class="tile tile-wide"><div class="tile-name">${esc(tr("home.no_zones"))}</div>
      <div class="tile-note">${esc(tr("home.no_zones_note"))}</div><a class="link-btn" href="/climate-admin">${esc(tr("home.admin_link"))}</a></div>`);
    return;
  }
  // Cards are kept and refilled: a card being used keeps its place.
  const seen = new Set();
  climate.zones.forEach(zone => {
    const id = zoneId(zone.name);
    seen.add(id);
    let card = document.getElementById(id);
    if (!card) {
      card = document.createElement("article");
      card.className = "zone";
      card.id = id;
      card.innerHTML = `<div class="zone-live"></div><section class="zone-plan"></section>`;
      host.appendChild(card);
    }
    fillZone(card, zone, climate);
    fillPlan(card, zone);
  });
  [...host.children].forEach(child => { if (!seen.has(child.id)) child.remove(); });
}

function fillZone(card, zone, climate) {
  // Not under someone typing a manual temperature: the refresh would
  // wipe what they're writing.
  const typing = document.activeElement;
  if (typing && typing.matches(".manual-input") && card.contains(typing)) return;
  const mode = climate.mode;
  const status = zoneStatus(zone, climate);
  const week = (state.weeks[zone.name] || {})[mode];
  const excludedNow = climate.mode === "cool" && !zone.cool_enabled;
  // Out of the season, its valve only opens for another device (the
  // dehumidifier's water): not this zone running - no runs, no hours.
  const seen = state.history[zone.name] || { temps: [], runs: [] };
  const history = excludedNow ? { temps: seen.temps, runs: [] } : seen;
  const now = boardNow();
  const change = nextChange(week, now);
  card.dataset.active = status.active ? "true" : "false";
  // The card's colour (CSS): running, at its target, a problem - resting
  // inside its band and off stay plain.
  card.dataset.tone = status.tone === "ok" && !status.reached ? "rest" : status.tone;
  card.dataset.zone = zone.name;

  // The two readings side by side, large: the temperature, and the
  // humidity with what it means.
  const temp = zone.temperature_c != null
    ? `<span class="now-value">${fmt(zone.temperature_c)}</span><span class="now-unit">°</span>`
    : `<span class="now-missing">--</span>`;
  const readings = `<div class="readings">
      <div class="reading"><div class="reading-value">${temp}</div><div class="reading-label">${esc(tr("home.reading_temp"))}</div></div>
      ${zone.humidity_pct != null ? `<div class="reading reading-rh" data-rh="${rhLevel(zone.humidity_pct)}">
        <div class="reading-value"><span class="now-value">${fmt(zone.humidity_pct, 0)}</span><span class="now-unit">%</span></div>
        <div class="reading-label">${ICONS.drop}${esc(humidityWord(zone.humidity_pct))}</div></div>` : ""}
    </div>`;
  // The extras, one a row with its icon: on the right of the readings, or
  // under them in two columns on a narrow card.
  const fact = (icon, html, extra = "") => `<div class="fact${extra}"><span class="fact-icon">${icon}</span><span class="fact-text">${html}</span></div>`;
  const facts = [];
  const excluded = mode === "cool" && !zone.cool_enabled;
  // Away: nothing to reach - every zone off until the way back.
  if (zone.target_c != null && !excluded && !climate.away_until) {
    const gap = zone.sensor_ok ? gapText(zone) : "";
    const chip = tierChip(zone.tier, mode);
    const sub = chip || gap ? `<span class="fact-sub">${chip}${gap ? `<span class="trend">${esc(gap)}</span>` : ""}</span>` : "";
    facts.push(fact(ICONS.target, `<span>${esc(tr("home.target"))} <b>${fmtTarget(zone.target_c)}</b></span>${sub}`, " fact-target"));
    const rule = valveRule(zone, climate);
    if (rule) facts.push(fact(ICONS.flow, esc(rule), " fact-rule"));
  }
  if (change && !zone.override && !climate.away_until && !excluded) {
    facts.push(fact(ICONS.clock, esc(tr("home.next_change", { when: whenText(change.at), target: fmtTarget(change.target) }))));
  }
  if (mode === "cool" && zone.humidity_pct != null && zone.dew_point_c != null) {
    facts.push(fact(ICONS.drop, esc(tr("home.dew_point_long", { value: fmt(zone.dew_point_c) }))));
  }

  const foot = [];
  // A typed value isn't a reading: no "updated a moment ago" under it.
  const age = zone.sensor_manual ? "" : fmtAge(zone.sensor_seconds_since_update ?? zone.sensor_last_value_age_seconds);
  // The sensor's, said as such - the hours run today after it are the
  // valve's. A value it hasn't confirmed since: the last one saved, and
  // how old. Its power in plain words: on battery (how full), or powered
  // (an H&T on USB-C - the server leaves its battery out).
  const sensorBits = [];
  if (age) sensorBits.push(tr(zone.sensor_ok ? "home.updated" : "home.last_saved", { age }));
  if (zone.external_power) sensorBits.push(tr("home.powered"));
  else if (zone.battery_pct != null) sensorBits.push(tr("home.on_battery", { pct: Math.round(zone.battery_pct) }));
  if (sensorBits.length) foot.push(esc(tr("home.sensor_foot", { what: sensorBits.join(", ") })));
  const hoursToday = excludedNow ? 0 : (state.onHours[zone.name] || {}).today;
  if (hoursToday) foot.push(esc(tr(mode === "cool" ? "home.ran_today_cool" : "home.ran_today", { time: fmtDuration(hoursToday) })));

  morph(card.querySelector(".zone-live"), `
    <div class="zone-head">
      <h2 class="zone-name">${esc(zone.name)}${zone.sensor_simulated ? ` <span class="sim">${esc(tr("home.simulated"))}</span>` : ""}${zone.sensor_manual ? ` <span class="sim">${esc(tr("home.manual"))}</span>` : ""}</h2>
      <span class="state" data-tone="${status.tone}">${esc(status.text)}</span>
    </div>
    ${manualBannerHtml(zone, climate)}
    <div class="zone-main">
      ${readings}
      ${facts.length ? `<div class="zone-facts">${facts.join("")}</div>` : ""}
    </div>
    ${sensorNoteHtml(zone)}
    ${excludedNow ? "" : `<div class="ribbon">
      ${ribbonSvg(zone, week, history, mode)}
      <div class="ribbon-caption"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
      <div class="ribbon-readout">${legendHtml()}</div>
    </div>`}
    <div class="quick">${quickHtml(zone, climate, change)}</div>
    ${zone.sensor_manual ? manualHtml(zone) : ""}
    <div class="zone-foot">${foot.join(" · ")}</div>`);
  wireRibbon(card, week, history, mode);
}

// Its sensor's batteries, said where they're seen: to change soon, or now
// (the admin panel's "battery low" too). On battery or powered is only
// said under the card: either is as good.
const BATTERY_SOON_PCT = 20;
const BATTERY_NOW_PCT = 10;
function sensorNoteHtml(zone) {
  if (zone.external_power || zone.battery_pct == null || zone.battery_pct > BATTERY_SOON_PCT) return "";
  const level = zone.battery_pct <= BATTERY_NOW_PCT ? "now" : "soon";
  const text = tr(level === "now" ? "home.battery_now" : "home.battery_soon", { pct: Math.round(zone.battery_pct) });
  return `<div class="sensor-note" data-level="${level}">${ICONS.battery}<span>${esc(text)}</span></div>`;
}

// A zone whose sensor is set to "manual" in the admin panel: the
// temperature it reads is the one written here, to try the thermostat
// without a sensor - the valve reacts at once.
function manualHtml(zone) {
  const id = `manual-${zoneId(zone.name)}`;
  const value = zone.sensor_ok && zone.temperature_c != null ? fmt(zone.temperature_c) : "";
  return `<form class="manual-set" data-zone="${esc(zone.name)}" novalidate>
    <label for="${id}">${esc(tr("home.manual_label"))}</label>
    <div class="manual-row">
      <input id="${id}" class="manual-input" type="text" inputmode="decimal" autocomplete="off" value="${esc(value)}" placeholder="20,0">
      <span class="manual-unit">°C</span>
      <button class="btn btn-primary" type="submit">${esc(tr("home.manual_set"))}</button>
    </div>
    <p class="manual-note">${esc(tr("home.manual_note"))}</p>
  </form>`;
}

async function onManualSubmit(event) {
  const form = event.target.closest("form.manual-set");
  if (!form) return;
  event.preventDefault();
  const input = form.querySelector(".manual-input");
  const temperature = Number(input.value.trim().replace(",", "."));
  if (input.value.trim() === "" || !Number.isFinite(temperature) || temperature < -30 || temperature > 60) {
    notify(tr("home.manual_invalid"));
    input.focus();
    return;
  }
  const zone = (state.climate ? state.climate.zones : []).find(z => z.name === form.dataset.zone);
  // The humidity it had stays: only the temperature is written here.
  const body = { temperature_c: temperature };
  if (zone && zone.sensor_ok && zone.humidity_pct != null) body.humidity_pct = zone.humidity_pct;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    await api(`/api/climate-admin/zones/${encodeURIComponent(form.dataset.zone)}/manual-value`, body);
    input.blur();
    await refreshClimate(form);
  } catch (e) {
    notify(errorText(e));
  } finally {
    button.disabled = false;
  }
}

// ------------------------------------------------------- today's strip --
// One strip, the day's whole story: the plan as coloured bands, the
// target as a dashed line, the room's temperature as a solid one, the
// stretches the system ran as a bar underneath, and now.

const RB = { w: 1000, top: 6, bottom: 66, runY: 74, runH: 8 };

function dayStartEpoch() {
  const now = boardNow();
  return wallToEpoch(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))) / 1000;
}

// Where the valve opens again for this target - the far edge of its
// tolerance band (services/thermostat.py's thresholds(): the hysteresis,
// and the economy tolerance for an economy target).
function bandEdge(target, mode) {
  const climate = state.climate || {};
  const tier = typeof TIER_OF !== "undefined" && TIER_OF[mode] ? TIER_OF[mode](target) : null;
  const spread = (climate.hysteresis_c != null ? climate.hysteresis_c : 0.5)
    + (tier === "economy" ? climate.economy_tolerance_c || 0 : 0);
  return mode === "cool" ? target + spread : target - spread;
}

function ribbonScale(zone, week, history, mode) {
  const today = week ? week[DAYS[dayIndex(boardNow())]] : [];
  const targets = today.filter(v => v != null);
  const values = [...history.temps.map(p => p.value), ...targets, ...targets.map(v => bandEdge(v, mode))];
  if (zone.temperature_c != null) values.push(zone.temperature_c);
  if (zone.override && zone.override.target != null) values.push(zone.override.target, bandEdge(zone.override.target, mode));
  if (!values.length) return null;
  let lo = Math.min(...values) - 0.4, hi = Math.max(...values) + 0.4;
  if (hi - lo < 2) { const mid = (hi + lo) / 2; lo = mid - 1; hi = mid + 1; }
  return { lo, hi, y: v => RB.bottom - (v - lo) / (hi - lo) * (RB.bottom - RB.top), today };
}

function ribbonSvg(zone, week, history, mode) {
  const scale = ribbonScale(zone, week, history, mode);
  const x = hour => (hour / 24) * RB.w;
  const start = dayStartEpoch();
  const now = boardNow();
  const nowHour = now.getUTCHours() + now.getUTCMinutes() / 60;
  const parts = [];
  for (const h of [6, 12, 18]) parts.push(`<line class="rb-grid" x1="${x(h)}" x2="${x(h)}" y1="0" y2="${RB.bottom}" vector-effect="non-scaling-stroke"/>`);
  if (scale) {
    bands(scale.today).forEach(b => {
      const color = b.value == null ? null : tempColor(b.value);   // the grid's colours
      if (color) parts.push(`<rect class="rb-block" x="${x(b.start)}" y="0" width="${x(b.end) - x(b.start)}" height="${RB.bottom}" fill="${color}"/>`);
    });
    // Each hour's tolerance band, under its target line.
    const band = (from, to, target) => {
      const edge = bandEdge(target, mode);
      const top = scale.y(Math.max(target, edge)), bottom = scale.y(Math.min(target, edge));
      const tier = TIER_OF[mode] ? TIER_OF[mode](target) : "comfort";
      parts.push(`<rect class="rb-band" data-tier="${tier}" x="${x(from)}" y="${top.toFixed(1)}" width="${(x(to) - x(from)).toFixed(1)}" height="${(bottom - top).toFixed(1)}"/>`);
    };
    bands(scale.today).forEach(b => { if (b.value != null) band(b.start, b.end, b.value); });
    let d = "";
    scale.today.forEach((v, h) => {
      if (v == null) return;
      const prev = h > 0 ? scale.today[h - 1] : null;
      d += `${prev == null ? "M" : "L"}${x(h)},${scale.y(v)} L${x(h + 1)},${scale.y(v)} `;
    });
    if (d) parts.push(`<path class="rb-target" d="${d}" vector-effect="non-scaling-stroke"/>`);
    if (zone.override && zone.override.target != null) {
      const until = zone.override.until ? boardDate(zone.override.until) : null;
      const endHour = until ? Math.min(24, (until - new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))) / 3600000) : 24;
      const y = scale.y(zone.override.target);
      if (endHour > nowHour) band(nowHour, endHour, zone.override.target);
      parts.push(`<path class="rb-override" d="M${x(nowHour)},${y} L${x(endHour)},${y}" vector-effect="non-scaling-stroke"/>`);
    }
    const points = history.temps.filter(p => p.ts >= start).map(p => `${x((p.ts - start) / 3600).toFixed(1)},${scale.y(p.value).toFixed(1)}`);
    if (zone.temperature_c != null && points.length) points.push(`${x(nowHour).toFixed(1)},${scale.y(zone.temperature_c).toFixed(1)}`);
    if (points.length > 1) parts.push(`<polyline class="rb-temp" points="${points.join(" ")}" vector-effect="non-scaling-stroke"/>`);
  }
  runSpans(history.runs, start).forEach(([from, to]) => {
    parts.push(`<rect class="rb-run" x="${x(from)}" y="${RB.runY}" width="${Math.max(2, x(to) - x(from))}" height="${RB.runH}"/>`);
  });
  parts.push(`<line class="rb-now" x1="${x(nowHour)}" x2="${x(nowHour)}" y1="0" y2="${RB.runY + RB.runH}" vector-effect="non-scaling-stroke"/>`);
  parts.push(`<line class="rb-cursor" x1="0" x2="0" y1="0" y2="${RB.runY + RB.runH}" vector-effect="non-scaling-stroke" visibility="hidden"/>`);
  const label = tr("home.ribbon_label", { name: zone.name });
  return `<svg viewBox="0 0 ${RB.w} ${RB.runY + RB.runH}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">${parts.join("")}</svg>`;
}

// The stretches the system ran today, as [from, to] hours: 5-minute
// samples joined into runs, a gap of one sample bridged.
function runSpans(runs, start) {
  const spans = [];
  runs.filter(p => p.ts >= start && p.value >= 0.5).forEach(p => {
    const h = (p.ts - start) / 3600;
    const last = spans[spans.length - 1];
    if (last && h - last[1] <= 0.17) last[1] = h + 5 / 60;
    else spans.push([h, h + 5 / 60]);
  });
  return spans;
}

function legendHtml() {
  return `<span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><path d="M1 6 L6 3 L11 5 L17 2" class="lg-temp"/></svg>${esc(tr("home.lg_temp"))}</span>
    <span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><path d="M1 4 H17" class="lg-target"/></svg>${esc(tr("home.lg_target"))}</span>
    <span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><rect x="1" y="1" width="16" height="6" rx="1" class="lg-band"/></svg>${esc(tr("home.lg_band"))}</span>
    <span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><rect x="1" y="2" width="16" height="4" rx="1" class="lg-run"/></svg>${esc(tr("home.lg_run"))}</span>`;
}

// Touch or hover the strip: what happened (or is planned) at that time.
// Wired once - the strip is updated in place, not rebuilt - reading the
// day the card was last filled with.
function wireRibbon(card, week, history, mode) {
  card.ribbonDay = { week, history, mode };
  const svg = card.querySelector(".ribbon svg");
  if (!svg || svg.ribbonWired) return;    // none out of the season
  svg.ribbonWired = true;
  let hideTimer = null;
  const show = event => {
    clearTimeout(hideTimer);
    const { week, history, mode } = card.ribbonDay;
    const readout = card.querySelector(".ribbon-readout");
    const cursor = svg.querySelector(".rb-cursor");
    const start = dayStartEpoch();
    const today = week ? week[DAYS[dayIndex(boardNow())]] : null;
    const box = svg.getBoundingClientRect();
    const k = Math.max(0, Math.min(0.9999, (event.clientX - box.left) / box.width));
    const hour = k * 24;
    cursor.setAttribute("x1", k * RB.w);
    cursor.setAttribute("x2", k * RB.w);
    cursor.setAttribute("visibility", "visible");
    const ts = start + hour * 3600;
    const time = hhmm(wall(ts * 1000));
    const bits = [time];
    const nowTs = wallToEpoch(boardNow()) / 1000;
    if (ts <= nowTs) {
      const near = history.temps.reduce((best, p) => (!best || Math.abs(p.ts - ts) < Math.abs(best.ts - ts) ? p : best), null);
      if (near && Math.abs(near.ts - ts) < 1800) bits.push(`${fmt(near.value)}°`);
      const run = history.runs.find(p => Math.abs(p.ts - ts) <= 180);
      if (run && run.value >= 0.5) bits.push(tr(mode === "cool" ? "home.was_cooling" : "home.was_heating"));
    }
    if (today) bits.push(tr("home.planned", { target: fmtTarget(today[Math.floor(hour)]) }));
    readout.textContent = bits.join(" · ");
  };
  // A finger lifted leaves the strip at once (pointerleave follows
  // pointerup on a touch): what it touched stays a moment to be read -
  // a tap used to show nothing at all.
  const hide = event => {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      svg.querySelector(".rb-cursor").setAttribute("visibility", "hidden");
      morph(card.querySelector(".ribbon-readout"), legendHtml());
    }, event && event.pointerType === "touch" ? 4000 : 0);
  };
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
}

// -------------------------------------------------------- quick change --

// A zone changed by hand, said at the top of its card - not only at the
// bottom, where it was easy to miss: what it holds, until when, and the
// way back to its schedule.
function manualBannerHtml(zone, climate) {
  if (!zone.override || climate.away_until || (climate.mode === "cool" && !zone.cool_enabled)) return "";
  const until = overrideUntil(zone.override);
  const text = zone.override.target == null
    ? tr("home.manual_off", { until })
    : tr("home.manual_on", { target: fmtTarget(zone.override.target), until });
  // What the week has for this hour: what "back" means, said before it's tapped.
  const back = zone.scheduled_target_c === undefined ? tr("home.resume")
    : zone.scheduled_target_c === null ? tr("home.resume_to_off")
    : tr("home.resume_to", { target: fmtTarget(zone.scheduled_target_c) });
  return `<div class="manual-banner">${ICONS.hand}<span>${esc(text)}</span>
    <button class="btn" type="button" data-action="resume">${esc(back)}</button></div>`;
}

function quickHtml(zone, climate, change) {
  const mode = climate.mode;
  if (!zoneReady(zone)) {
    delete state.pending[zone.name];
    return `<div class="excluded-note setup-note">${ICONS.link}<span>${esc(tr(setupKey(zone, "home.setup_note")))}</span>
      <a class="btn" href="/climate-admin">${esc(tr("home.setup_link"))}</a></div>`;
  }
  if (mode === "cool" && !zone.cool_enabled) {
    return `<div class="excluded-note">${ICONS.snow}<span>${esc(tr("home.excluded_note"))}</span></div>`;
  }
  // Away: off until the way back, and nothing to change meanwhile - the
  // board refuses it too. The way back right here.
  if (climate.away_until) {
    delete state.pending[zone.name];
    return `<div class="excluded-note away-note">${ICONS.suitcase}<span>${esc(tr("home.away_zone_note"))}</span>
      <button class="btn" type="button" data-action="back">${esc(tr("home.away_back"))}</button></div>`;
  }
  const pending = state.pending[zone.name];
  if (pending) {
    const options = DURATIONS.map(h => `<button type="button" data-action="duration" data-hours="${h}" aria-pressed="${pending.hours === h}">${esc(fmtDuration(h))}</button>`);
    if (change) {
      options.push(`<button type="button" data-action="duration" data-hours="next" aria-pressed="${pending.hours === "next"}">${esc(untilText(change.at))}</button>`);
    }
    options.push(`<button type="button" data-action="duration" data-hours="manual" aria-pressed="${pending.hours === "manual"}">${esc(tr("home.until_changed_button"))}</button>`);
    // A temperature, or off: both a manual change, for the time chosen.
    const off = pending.target == null;
    return `
      <div class="pending">
        <div class="pending-title">${esc(tr("home.pending_title"))}</div>
        <div class="segmented" role="group" aria-label="${esc(tr("home.pending_title"))}">
          <button type="button" data-action="pending-on" aria-pressed="${!off}">${esc(tr("home.pending_on"))}</button>
          <button type="button" data-action="pending-off" aria-pressed="${off}">${esc(tr("home.pending_off"))}</button>
        </div>
        <div class="stepper" ${off ? "hidden" : ""}>
          <button type="button" data-action="step" data-delta="-0.5" aria-label="${esc(tr("home.colder"))}">−</button>
          <span class="stepper-value">${fmtTarget(pending.target)}</span>
          <button type="button" data-action="step" data-delta="0.5" aria-label="${esc(tr("home.warmer"))}">+</button>
        </div>
        <div class="pending-label">${esc(tr("home.for_how_long"))}</div>
        <div class="segmented" role="group" aria-label="${esc(tr("home.for_how_long"))}">${options.join("")}</div>
        <div class="pending-actions">
          <button class="btn" type="button" data-action="cancel-pending">${esc(tr("home.cancel"))}</button>
          <button class="btn btn-primary" type="button" data-action="apply">${esc(tr("home.apply"))}</button>
        </div>
      </div>`;
  }
  // Its controls say what they are: a title, and what a tap does.
  const shown = zone.target_c != null ? zone.target_c : null;
  return `
    <div class="quick-title">${esc(tr(zone.override ? "home.quick_title_again" : "home.quick_title"))}</div>
    <div class="stepper">
      <button type="button" data-action="start" data-delta="-0.5" aria-label="${esc(tr("home.colder"))}">−</button>
      <span class="stepper-value">${shown == null ? esc(tr("home.st_off")) : fmtTarget(shown)}</span>
      <button type="button" data-action="start" data-delta="0.5" aria-label="${esc(tr("home.warmer"))}">+</button>
    </div>
    <p class="quick-hint">${esc(tr("home.quick_hint"))}</p>`;
}

function lastTarget(zone) {
  if (zone.target_c != null) return zone.target_c;
  const week = (state.weeks[zone.name] || {})[state.climate.mode];
  const values = week ? Object.values(week).flat().filter(v => v != null) : [];
  return values.length ? Math.max(...values) : DEFAULT_TARGET[state.climate.mode];
}

function clampTarget(value) {
  return Math.max(TARGET_RANGE.min, Math.min(TARGET_RANGE.max, Math.round(value * 2) / 2));
}

async function onZoneClick(event) {
  const button = event.target.closest("button[data-action]");
  const card = event.target.closest(".zone");
  if (!button || !card) return;
  const zone = state.climate.zones.find(z => z.name === card.dataset.zone);
  if (!zone) return;
  const action = button.dataset.action;
  if (action === "toggle-plan") {
    const plan = card.querySelector(".zone-plan");
    const open = plan.dataset.open !== "true";
    plan.dataset.open = String(open);
    button.setAttribute("aria-expanded", String(open));
    return;
  }
  const pending = state.pending[zone.name];
  // Until the schedule's next change, as most thermostats do - or 4 hours
  // without one.
  const firstHours = () => nextChange((state.weeks[zone.name] || {})[state.climate.mode], boardNow()) ? "next" : 4;
  if (action === "start") {
    // Off: the first tap turns it on at its usual temperature, - or +.
    const step = zone.target_c == null ? 0 : Number(button.dataset.delta);
    state.pending[zone.name] = { target: clampTarget(lastTarget(zone) + step), hours: firstHours() };
  } else if (action === "pending-off" && pending && pending.target != null) {
    pending.last = pending.target;
    pending.target = null;
  } else if (action === "pending-on" && pending && pending.target == null) {
    pending.target = pending.last ?? clampTarget(lastTarget(zone));
  } else if (action === "step" && pending) {
    pending.target = clampTarget(pending.target + Number(button.dataset.delta));
  } else if (action === "duration" && pending) {
    pending.hours = ["next", "manual"].includes(button.dataset.hours) ? button.dataset.hours : Number(button.dataset.hours);
  } else if (action === "cancel-pending") {
    delete state.pending[zone.name];
  } else if (action === "apply" && pending) {
    const body = pending.hours === "next" ? { target: pending.target, until: "next_change" }
      : pending.hours === "manual" ? { target: pending.target, until: "manual" }
      : { target: pending.target, hours: pending.hours };
    asked.climate++;
    const done = await act(button, () => api(`/api/climate/zones/${encodeURIComponent(zone.name)}/override`, body));
    const quick = card.querySelector(".quick");
    if (done) {
      delete state.pending[zone.name];
      // The card says it at once; the valve's answer follows.
      zone.override = done.override;
      zone.target_c = done.override.target;
      steady(() => { renderHero(); fillZone(card, zone, state.climate); }, quick);
    }
    refreshClimate(quick);
    return;
  } else if (action === "resume") {
    asked.climate++;
    await act(button, () => api(`/api/climate/zones/${encodeURIComponent(zone.name)}/override/cancel`, {}));
    refreshClimate(card.querySelector(".quick"));
    return;
  } else if (action === "back") {
    await comeBack(button);
    return;
  } else {
    return;
  }
  steady(() => fillZone(card, zone, state.climate), button);
}

// An action: its button waits for the board, and is itself again before
// anything is redrawn. Refused, or not answered: said, and null - the
// caller then shows what the board says (unanswered isn't undone: it may
// have been done all the same).
async function act(button, request) {
  busy(button, true);
  try {
    return await request();
  } catch (e) {
    notify(errorText(e));
    return null;
  } finally {
    busy(button, false);
  }
}

// A button waiting for the board: dimmed, not pressed twice.
function busy(button, on) {
  button.disabled = on;
  if (on) button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
}

// ---------------------------------------------------------------- hero --

function renderHero() {
  const climate = state.climate;
  if (!climate) return;
  const mode = climate.mode || "heat";
  document.body.dataset.season = mode;
  setText(document.getElementById("hero-season"), tr(mode === "cool" ? "home.season_cool" : "home.season_heat"));
  const season = document.getElementById("season-chip");
  morph(season, `${mode === "cool" ? ICONS.snow : ICONS.sun}<span>${esc(tr(mode === "cool" ? "home.summer" : "home.winter"))}</span>`);

  // The title says what the plant is doing as a whole, the line under it
  // where each zone stands - nothing a reading can't back up.
  const zones = climate.zones || [];
  const cool = mode === "cool";
  const ready = zones.filter(zoneReady);
  const running = ready.filter(z => !zoneExcluded(z));
  const active = running.filter(z => zoneStatus(z, climate).active);
  const off = running.filter(z => z.target_c == null);
  document.body.dataset.running = String(active.length > 0);   // the top brighter while it runs
  let title;
  if (!zones.length) title = tr("home.no_zones");
  else if (!ready.length) title = tr("home.hero_setup");
  else if (climate.away_until) title = tr("home.hero_away", { until: awayUntilText(boardDate(climate.away_until)) });
  else if (active.length) title = tr(cool ? "home.hero_cooling" : "home.hero_heating", { zones: listNames(active.map(z => z.name)) });
  // All off: "none planned now" only if it's the week's doing - one off
  // by hand is planned, and the line under it says so.
  else if (running.length && off.length === running.length) {
    title = tr(`home.${off.some(z => z.override) ? "hero_off_now" : "hero_all_off"}_${cool ? "cool" : "heat"}`);
  }
  else if (running.length && !off.length && running.every(z => zoneStatus(z, climate).reached)) title = tr("home.hero_all_ok");
  else title = tr(cool ? "home.hero_idle_cool" : "home.hero_idle_heat");
  const devices = awayDevices();
  // The VMC, away: off with the rest, or at its night speed.
  const vmcAway = climate.away_until && state.vmc && state.vmc.status && state.vmc.status.away;
  const vmcDevice = vmcAway && deviceByName(vmcAway.device);
  const vmcName = vmcDevice && vmcDevice.paired_switch ? displayName(vmcDevice.paired_switch) : null;
  if (vmcName && vmcAway.mode === "off") devices.push(vmcName);
  const detail = climate.away_until
    ? tr(devices.length ? "home.hero_away_detail_devices" : "home.hero_away_detail", { devices: listNames(devices) })
      + (vmcName && vmcAway.mode !== "off" ? ` ${tr("home.hero_away_vmc_level", { name: vmcName, level: tr(`vmc_presets.${vmcAway.mode}`) })}` : "")
    : zones.map(z => zoneLine(z, climate)).concat(dehumidifierLine(climate)).filter(Boolean).join(" · ");
  setText(document.getElementById("hero-title"), title);
  setText(document.getElementById("hero-detail"), detail);

  const away = document.getElementById("away-chip");
  away.setAttribute("aria-pressed", climate.away_until ? "true" : "false");
  morph(away, `${ICONS.suitcase}<span>${esc(climate.away_until ? tr("home.away_on") : tr("home.away_off"))}</span>`);
}

// One zone's state in a few words, for the line under the title.
function zoneLine(zone, climate) {
  const temp = zone.temperature_c != null ? `${fmt(zone.temperature_c)}°` : "";
  if (!zoneReady(zone)) return tr(setupKey(zone, "home.line_setup"), { zone: zone.name });
  if (zoneExcluded(zone)) return tr("home.line_excluded", { zone: zone.name });
  if (zone.valve_ok === false) return tr("home.line_valve_offline", { zone: zone.name });
  if (zone.sensor_waiting) return tr("home.line_first_report", { zone: zone.name });
  if (!zone.sensor_ok && zone.temperature_c == null) return tr("home.line_waiting", { zone: zone.name });
  if (zone.target_c == null) {
    if (zone.override) return tr("home.line_off_manual", { zone: zone.name, until: overrideUntil(zone.override) });
    const change = nextChange((state.weeks[zone.name] || {})[climate.mode], boardNow());
    return change ? tr("home.line_off_until", { zone: zone.name, until: untilText(change.at) }) : tr("home.line_off", { zone: zone.name });
  }
  // Held by hand: said after the rest of its line.
  const manual = zone.override ? ` · ${tr("home.line_manual", { until: overrideUntil(zone.override) })}` : "";
  return zoneLineFor(zone, climate, temp) + manual;
}

function zoneLineFor(zone, climate, temp) {
  if (zoneStatus(zone, climate).active) return tr("home.line_on", { zone: zone.name, temp, target: fmtTarget(zone.target_c) });
  // Resting: at its target on the season's side (targetReached()), or
  // past it - 26° with a 20° heating target isn't "at temperature", and
  // neither is 24.3° with a 24° cooling one.
  if (targetReached(zone, climate)) return tr("home.line_ok", { zone: zone.name, temp });
  const diff = zone.temperature_c != null ? zone.temperature_c - zone.target_c : 0;
  return tr(diff > 0 ? "home.line_above" : "home.line_below", { zone: zone.name, temp, target: fmtTarget(zone.target_c) });
}

function listNames(names) {
  if (names.length < 2) return names.join("");
  return `${names.slice(0, -1).join(", ")} ${tr("home.and")} ${names[names.length - 1]}`;
}

function renderOutdoor() {
  const chip = document.getElementById("outdoor-chip");
  const sensors = (state.devices && state.devices.sensors) || [];
  const device = deviceByName("Outdoor Sensor");
  const reading = state.readings["Outdoor Sensor"];
  // A simulated one says so, as a zone's sensor does: an invented outdoor
  // temperature isn't read as the real one.
  const label = `<span class="outdoor-label">${ICONS.leaf}${esc(tr("home.outside"))}${device && device.simulated
    ? ` <span class="sim">${esc(tr("home.simulated"))}</span>` : ""}</span>`;
  if (!reading || reading.temperature_c == null) {
    // Not wired yet, or wired and not heard from yet: said, not left out -
    // in a home with rooms to heat or cool (a sensor of theirs is here).
    // Shown in a climate home - and on a new board, whose outdoor sensor
    // is still to configure before any other sensor reports.
    const todo = ((state.climate && state.climate.to_configure) || []).some(item => item.role === "outdoor");
    chip.hidden = !device && !todo && !sensors.some(d => d.kind === "climate");
    // On a wide screen the value's place is kept, with a dash: the tile
    // has the shape it will have once there's a reading.
    morph(chip, `${label}<span class="outdoor-value outdoor-value-none" aria-hidden="true"><span class="dash">−−</span><small>°</small></span>
      <span class="outdoor-none">${esc(tr(device ? "home.outdoor_waiting" : "home.outdoor_none"))}</span>`);
    return;
  }
  chip.hidden = false;
  const rh = reading.humidity_pct != null
    ? `<span class="outdoor-rh">${ICONS.drop}${fmt(reading.humidity_pct, 0)}% ${esc(tr("home.humidity").toLowerCase())}</span>` : "";
  // Today's lowest and highest so far (the board's, kept as it records) -
  // the reading itself counted too: it may be newer than the last sample.
  const today = ((state.dayRanges || {})["Outdoor Sensor"] || {}).temperature_c;
  let range = "";
  const low = today ? fmt(Math.min(today.min, reading.temperature_c)) : "";
  const high = today ? fmt(Math.max(today.max, reading.temperature_c)) : "";
  // "min 19,4° · max 19,4°" says nothing the reading doesn't.
  if (today && low !== high) {
    // Short, and broken only between the two on a narrow phone.
    range = `<span class="outdoor-range" aria-label="${esc(tr("home.outdoor_range", { min: low, max: high }))}">
      <span>${esc(tr("home.outdoor_min", { value: low }))}</span> · <span>${esc(tr("home.outdoor_max", { value: high }))}</span></span>`;
  }
  morph(chip, `${label}
    <span class="outdoor-value">${fmt(reading.temperature_c)}<small>°</small></span>${range}${rh}`);
}

const ICONS = {
  sun: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`,
  snow: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v20M4 6l16 12M20 6L4 18M9 3l3 3 3-3M9 21l3-3 3 3"/></svg>`,
  suitcase: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>`,
  drop: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3s6 6.6 6 11a6 6 0 0 1-12 0c0-4.4 6-11 6-11z"/></svg>`,
  target: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/></svg>`,
  flow: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h12M12 6l6 6-6 6"/></svg>`,
  link: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>`,
  leaf: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20c0-9 6-15 16-16-1 10-7 16-16 16z"/><path d="M4 20l8-8"/></svg>`,
  moon: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z"/></svg>`,
  wind: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 8h10.5a3 3 0 1 0-3-3"/><path d="M3 12h15.5a3 3 0 1 1-3 3"/><path d="M3 16h8"/></svg>`,
  breeze: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10h12.5a3 3 0 1 0-3-3"/><path d="M3 15h9"/></svg>`,
  filter: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5h18l-7 8v5l-4 2v-7z"/></svg>`,
  clock: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  chevron: `<svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`,
  hand: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V11m0-6.5a1.5 1.5 0 0 1 3 0V11m0-4.5a1.5 1.5 0 0 1 3 0V13c0 4-2.5 7-6.5 7S5 17 5 13v-2a1.5 1.5 0 0 1 3 0"/></svg>`,
  battery: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="7" width="16" height="10" rx="2"/><path d="M21 10v4M6.5 10.5v3"/></svg>`,
  power: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v8"/><path d="M6.6 6.6a7.5 7.5 0 1 0 10.8 0"/></svg>`,
  fan: `<svg viewBox="0 0 24 24" aria-hidden="true"><g class="fan-blades">${[0, 120, 240].map(a => `<path transform="rotate(${a} 12 12)" d="M12 12C9.6 9.6 9.4 4.9 11.6 3.3c2.4-1.7 5.6.4 4.5 3.6-.8 2.2-2.4 3.7-4.1 5.1z"/>`).join("")}<circle cx="12" cy="12" r="1.9"/></g></svg>`,
};

// ------------------------------------------- season and health sheets --
// What the top's two chips mean, in plain words - the admin page, with
// its technical details, one link away.

function dateText(date) {
  const months = tr("home.months_long");
  return tr("home.date_at", { day: tr(`home.day_long.${DAYS[dayIndex(date)]}`), date: date.getUTCDate(),
    month: Array.isArray(months) ? months[date.getUTCMonth()] : "", year: date.getUTCFullYear(), time: hhmm(date) });
}

// Away can last weeks: "until Thursday" doesn't say which. Today, the
// time; any other day, the whole date.
function awayUntilText(date) {
  const now = boardNow();
  const today = date.getUTCFullYear() === now.getUTCFullYear() && date.getUTCMonth() === now.getUTCMonth()
    && date.getUTCDate() === now.getUTCDate();
  return today ? untilText(date) : tr("home.until_when", { when: dateText(date) });
}

// What the season holds a switch at, in words: a normally open valve's
// "on" is the valve closed.
function seasonState(rule, mode) {
  return tr(`home.season_state_${rule[mode]}${rule.normally_open ? "_no" : ""}`);
}

// A device's name inside a sentence: "col deumidificatore acceso".
function inSentence(name) {
  return name.charAt(0).toLowerCase() + name.slice(1);
}

function openSeason() {
  const climate = state.climate;
  if (!climate) return;
  const mode = climate.mode;
  const cool = mode === "cool";
  const zones = climate.zones || [];
  const effects = climate.season_effects || {};
  const names = list => listNames(list.map(z => z.name));
  // The zones by name: which this season schedules - in winter all of
  // them, and the one left out in summer said so; in summer the one left
  // out, why, and what its valve does meanwhile.
  const keptOut = zones.filter(z => z.cool_enabled === false);
  const scheduled = cool ? zones.filter(z => z.cool_enabled !== false) : zones;
  const points = [];
  if (scheduled.length) {
    const which = scheduled.length === zones.length ? "all" : scheduled.length === 1 ? "one" : "some";
    points.push(tr(`home.season_sheet_zones_${which}_${mode}`, { zones: names(scheduled) }));
  }
  if (cool) {
    keptOut.forEach(z => {
      const link = (effects.links || []).find(l => l.zone === z.name && (!l.mode || l.mode === mode) && deviceByName(l.leader));
      points.push(link ? tr("home.season_sheet_kept_linked", { zone: z.name, leader: inSentence(displayName(link.leader)) })
        : tr("home.season_sheet_kept_closed", { zone: z.name }));
    });
  } else if (keptOut.length) {
    points.push(tr(keptOut.length === 1 ? "home.season_sheet_summer_one" : "home.season_sheet_summer_many", { zones: names(keptOut) }));
  }
  // What the season does to the plant's switches: one way for all of it -
  // one it powers (the bypass, in summer) whatever the schedule, away too -
  // or left to whoever lives there.
  const rules = (effects.rules || []).filter(r => deviceByName(r.switch));
  rules.filter(r => r[mode] === "on" || r[mode] === "off").forEach(r => points.push(tr(
    r[mode] === "on" ? "home.season_sheet_forced_on" : "home.season_sheet_forced",
    { device: displayName(r.switch), state: seasonState(r, mode) })));
  const byHand = rules.filter(r => r[mode] === "manual").map(r => displayName(r.switch));
  if (byHand.length) points.push(tr("home.season_sheet_by_hand", { devices: listNames(byHand) }));
  points.push(tr("home.season_sheet_who"));
  const changed = climate.mode_changed && climate.mode_changed.at
    ? `<p class="sheet-note">${esc(tr("home.season_sheet_changed", { when: dateText(wall(climate.mode_changed.at * 1000)) }))}</p>` : "";
  openSheet(tr("home.season_sheet_title"), `
    <p class="sheet-lead">${cool ? ICONS.snow : ICONS.sun}<b>${esc(tr(cool ? "home.season_sheet_cool" : "home.season_sheet_heat"))}</b></p>
    <ul class="sheet-points">${points.map(point => `<li>${esc(point)}</li>`).join("")}</ul>
    ${changed}
    <p><a class="sheet-link" href="/climate-admin#season">${esc(tr("home.season_sheet_admin"))}</a></p>`);
}

// A device as a resident calls it: a zone's sensor or valve by its zone.
function subjectName(subject) {
  const zones = ((state.climate && state.climate.zones) || []).map(z => z.name);
  for (const [suffix, key] of [[" Sensor", "home.subject_sensor"], [" Valve", "home.subject_valve"]]) {
    if (subject && subject.endsWith(suffix) && zones.includes(subject.slice(0, -suffix.length))) {
      return tr(key, { zone: subject.slice(0, -suffix.length) });
    }
  }
  return displayName(subject || "");
}

// What a resident can make of each problem: a device that doesn't answer,
// a battery, the internet, the router - or something for whoever looks
// after the system, said as the admin page says it.
const RESIDENT_PROBLEMS = {
  device_offline: "home.problem_offline", integration_failed: "home.problem_offline",
  device_report_late: "home.problem_offline", device_battery_low: "home.problem_battery",
  clock_unsynced: "home.problem_internet", lan_unreachable: "home.problem_router",
};

function problemText(problem) {
  const subject = subjectName(problem.subject);
  const key = RESIDENT_PROBLEMS[problem.code];
  if (key) return tr(key, { subject });
  const technical = tr(`climate_admin.system_problem_${problem.code}`, { subject });
  return tr("home.problem_technical", { what: technical.startsWith("climate_admin.") ? problem.code : technical });
}

// A device that doesn't answer is one line: its reports missed, its
// lateness, follow from it.
const FOLLOWS_OFFLINE = new Set(["device_reports_missed", "device_report_late"]);

function residentProblems(problems) {
  const offline = new Set(problems.filter(p => RESIDENT_PROBLEMS[p.code] === "home.problem_offline").map(p => p.subject));
  return problems.filter(p => !(FOLLOWS_OFFLINE.has(p.code) && offline.has(p.subject)));
}

function openHealth() {
  const problems = residentProblems(state.problems || []);
  const zones = (state.climate && state.climate.zones) || [];
  const todo = (state.climate && state.climate.to_configure) || [];
  let body;
  if (problems.length) {
    body = `<p class="sheet-lead warn"><b>${esc(tr("home.health_problems", { count: problems.length }))}</b></p>
      <ul class="sheet-points">${problems.map(p => `<li>${esc(problemText(p))}</li>`).join("")}</ul>`;
  } else if (!todo.length) {
    body = `<p class="sheet-lead ok"><b>${esc(tr("home.health_sheet_ok"))}</b></p>
      <ul class="sheet-points"><li>${esc(tr("home.health_sheet_answering"))}</li></ul>`;
  } else {
    body = `<p class="sheet-lead ok"><b>${esc(tr("home.health_sheet_nothing_wrong"))}</b></p>`;
  }
  // Not connected yet: not a problem, but the plant isn't whole until they are.
  if (todo.length) {
    body += `<div class="sheet-label">${esc(tr("home.health_sheet_setup"))}</div>
      <ul class="sheet-points">${todo.map(item => `<li>${esc(setupName(item))}</li>`).join("")}</ul>
      <p><a class="sheet-link" href="/climate-admin">${esc(tr("home.health_sheet_setup_admin"))}</a></p>`;
  }
  // Each zone's sensor's power, whatever else is going on: what a
  // resident looks after.
  const power = zones.filter(z => z.external_power || z.battery_pct != null).map(z => `<li>${esc(z.external_power
    ? tr("home.health_sheet_powered", { zone: z.name })
    : tr("home.health_sheet_battery", { zone: z.name, pct: Math.round(z.battery_pct) }))}</li>`);
  openSheet(tr("home.health_sheet_title"), `${body}
    ${power.length ? `<div class="sheet-label">${esc(tr("home.health_sheet_sensors"))}</div><ul class="sheet-points">${power.join("")}</ul>` : ""}
    <p><a class="sheet-link" href="/climate-admin#system-status">${esc(tr("home.health_sheet_admin"))}</a></p>`);
}

// ---------------------------------------------------------- away sheet --

// What else away switches off: what this season leaves to be switched by
// hand (the dehumidifier, in summer) - services/interlock.py.
function awayDevices() {
  const climate = state.climate;
  const rules = (climate && climate.season_effects && climate.season_effects.rules) || [];
  return rules.filter(rule => rule[climate.mode] === "manual" && deviceByName(rule.switch)).map(rule => displayName(rule.switch));
}

// A day as a date field wants it: yyyy-mm-dd, of a board-time date (its
// UTC fields are the board's wall clock - boardNow(), boardDate()).
function dayValue(date) {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
}

function openAway() {
  const away = state.climate && state.climate.away_until;
  const options = AWAY_OPTIONS.map(h => `<button class="btn" type="button" data-away="${h}">${esc(tr(`home.away_${h}`))}</button>`).join("");
  // Said before it's chosen: what goes off, and that nothing comes back on
  // until "I'm back".
  const devices = awayDevices();
  // What the season powers stays so - the bypass, in summer: the plant's,
  // not a comfort (services/interlock.py).
  const mode = state.climate && state.climate.mode;
  const kept = ((state.climate && state.climate.season_effects && state.climate.season_effects.rules) || [])
    .filter(r => r[mode] === "on" && deviceByName(r.switch))
    .map(r => tr("home.away_point_kept", { device: displayName(r.switch), state: seasonState(r, mode) }));
  const points = [tr("home.away_point_zones"),
    ...(devices.length ? [tr("home.away_point_devices", { devices: listNames(devices) })] : []),
    ...kept, tr("home.away_point_locked"), tr("home.away_point_plan")];
  // Or the day back, picked in the phone's own calendar - the board's
  // days: today at the earliest ("until tonight at 22"), a year at most
  // (the board's limit). Away already: the day back it has, to change.
  const day = 24 * 3600 * 1000;
  const now = boardNow();
  const back = away ? boardDate(away) : null;
  const shownDay = back ? dayValue(back) : dayValue(new Date(now.getTime() + day));
  const shownTime = back ? hhmm(back) : AWAY_BACK_TIME;
  // What the VMC does meanwhile (services/vmc_schedule.py): its minimum
  // or night speed, or off - the last choice, or what it's doing if away
  // already. Picked before the length, which is what goes away.
  const vmc = vmcSpeedDevice();
  const held = state.vmc && state.vmc.status && state.vmc.status.away;
  let vmcChoice = held ? held.mode : ((state.vmc && state.vmc.schedule && state.vmc.schedule.away_choice) || "night");
  const awayLevel = key => {
    const preset = vmc && vmc.presets.find(p => p.key === key);
    return preset ? `<button class="btn" type="button" role="radio" data-away-vmc="${key}" aria-checked="${vmcChoice === key}">${esc(`${tr(`vmc_presets.${key}`)} ${fmt(preset.value, 0)}%`)}</button>` : "";
  };
  const vmcRow = vmc ? `<div class="sheet-label">${esc(tr("home.away_vmc"))}</div>
    <div class="sheet-actions away-vmc" role="radiogroup" aria-label="${esc(tr("home.away_vmc"))}">
      ${awayLevel("min")}${awayLevel("night")}
      <button class="btn" type="button" role="radio" data-away-vmc="off" aria-checked="${vmcChoice === "off"}">${esc(tr("home.vmc_off"))}</button>
    </div>
    <p class="sheet-note">${esc(tr("home.away_vmc_note"))}</p>` : "";
  openSheet(tr("home.away_title"), `
    <ul class="sheet-points">${points.map(point => `<li>${esc(point)}</li>`).join("")}</ul>
    ${away ? `<p><b>${esc(tr("home.hero_away", { until: awayUntilText(boardDate(away)) }))}</b></p>` : ""}
    ${vmcRow}
    <div class="sheet-label">${esc(tr(away ? "home.away_change" : "home.away_for"))}</div>
    <div class="sheet-actions">${options}</div>
    <div class="sheet-label">${esc(tr("home.away_back_on"))}</div>
    <div class="away-date">
      <input type="date" id="away-day" value="${shownDay}" min="${dayValue(now)}" max="${dayValue(new Date(now.getTime() + 365 * day))}" aria-label="${esc(tr("home.away_day"))}">
      <input type="time" id="away-time" value="${shownTime}" aria-label="${esc(tr("home.away_time"))}">
      <button class="btn" type="button" data-away="day">${esc(tr("home.away_set"))}</button>
    </div>
    ${away ? `<div class="sheet-actions"><button class="btn btn-primary" type="button" data-away="back">${esc(tr("home.away_back"))}</button></div>` : ""}`,
    async event => {
      const pick = event.target.closest("button[data-away-vmc]");
      if (pick) {
        vmcChoice = pick.dataset.awayVmc;
        pick.parentElement.querySelectorAll("[data-away-vmc]").forEach(b => b.setAttribute("aria-checked", String(b === pick)));
        // Away already: the new choice now, the day back as it is.
        if (away) {
          asked.devices++;
          await act(pick, () => api("/api/climate/away", { until: away.slice(0, 16), vmc: vmcChoice }));
          refreshDevices();
        }
        return;
      }
      const button = event.target.closest("button[data-away]");
      if (!button) return;
      const choice = button.dataset.away;
      if (choice === "back") {
        if (await comeBack(button)) closeSheet();
        return;
      }
      let body = { hours: Number(choice) };
      if (choice === "day") {
        const dayField = document.getElementById("away-day"), timeField = document.getElementById("away-time");
        if (!dayField.value) { dayField.focus(); return; }
        body = { until: `${dayField.value}T${timeField.value || AWAY_BACK_TIME}` };
      }
      if (vmc) body.vmc = vmcChoice;
      asked.climate++;
      const done = await act(button, () => api("/api/climate/away", body));
      if (done) {
        closeSheet();
        state.climate.away_until = done.away_until || null;
        steady(() => { renderHero(); renderZones(); renderSeasonDevices(); });
      }
      asked.devices++;
      refreshClimate();
      refreshDevices();
    });
}

// "I'm back", from a card or the away sheet.
async function comeBack(button) {
  asked.climate++;
  const done = await act(button, () => api("/api/climate/away/cancel", {}));
  if (done) {
    state.climate.away_until = null;
    steady(() => { renderHero(); renderZones(); renderSeasonDevices(); });
  }
  refreshClimate();
  refreshDevices();
  return done;
}

// -------------------------------------------------------------- sheet --

function openSheet(title, html, onClick) {
  state.sheet = { onClick };
  document.getElementById("sheet-title").textContent = title;
  document.getElementById("sheet-body").innerHTML = html;
  document.getElementById("sheet").hidden = false;
  document.getElementById("sheet-backdrop").hidden = false;
  document.body.style.overflow = "hidden";
  document.getElementById("sheet-close").focus();
}

function closeSheet() {
  flushPendingScheduleSaves();
  state.sheet = null;
  document.getElementById("sheet").hidden = true;
  document.getElementById("sheet-backdrop").hidden = true;
  document.body.style.overflow = "";
}

// ---------------------------------------------------------- the plan --
// Each card's week, right under its day: the weekly grid
// (static/schedule_grid.js) - painted with a drag, copied to the week or
// to another zone, saved as it's painted, only the hours that changed.
// Built once per season and left alone: the card's live part above it is
// refilled every refresh, the grid never is, so a stroke is never cut.

function fillPlan(card, zone) {
  const plan = card.querySelector(".zone-plan");
  // Away: nothing changes until "I'm back" - the week neither (the board
  // refuses it too). Hidden, kept: back, it's there as it was.
  const hidden = zoneExcluded(zone) || !!(state.climate && state.climate.away_until);
  plan.hidden = hidden;
  // No week to plan this season: the card's live part takes the full width.
  card.dataset.plan = hidden ? "none" : "week";
  if (hidden) return;
  if (plan.dataset.mode !== climateMode) buildPlan(plan, zone);
  // Held by hand: the week is what comes back - shown, not changed until
  // then (the board refuses it too); the way back right above it.
  const locked = !!zone.override;
  const note = plan.querySelector(".plan-manual");
  if (note) note.hidden = !locked;
  const editor = plan.querySelector(".plan-edit");
  if (editor) {
    editor.inert = locked;
    editor.classList.toggle("is-locked", locked);
  }
}

function buildPlan(plan, zone) {
  plan.dataset.mode = climateMode;
  // On a phone the week waits behind its button (CSS): it's two screens
  // of hours, between this zone and everything after it. Wider, it's
  // beside the card, always open.
  plan.innerHTML = `<button class="plan-toggle" type="button" data-action="toggle-plan" aria-expanded="${plan.dataset.open === "true"}">
      <span class="plan-toggle-label">${esc(tr(`climate.schedule_heading_${climateMode}`))}</span>
      <span class="plan-toggle-hint">${esc(tr("home.plan_edit"))}</span>${ICONS.chevron}</button>
    <div class="plan-body">
      <div class="plan-manual" hidden>${ICONS.hand}<span>${esc(tr("home.plan_manual_note"))}</span>
        <button class="btn" type="button" data-action="resume">${esc(tr("home.resume"))}</button></div>
      <div class="plan-edit">${schedulePaneHtml(zone, slug(zone.name), tierNoteHtml())}</div>
    </div>`;
  // Not loaded (the board didn't answer): the next refresh tries again.
  loadZoneSchedule(zone.name).then(loaded => { if (!loaded) delete plan.dataset.mode; });
}

// --------------------------------------------------------- the plant --
// What the season runs besides the zones - ventilation, the bypass
// valve, the dehumidifier - with what each does in this season.

function deviceByName(name) {
  return (state.devices ? state.devices.sensors : []).find(d => d.name === name);
}

// The VMC's speed: the level paired with a switch that has speed presets.
function vmcSpeedDevice() {
  return (state.devices ? state.devices.sensors : []).find(d => d.kind === "number" && d.paired_switch && (d.presets || []).length);
}

function renderPlant() {
  const tiles = [];
  const vmcSpeed = vmcSpeedDevice();
  if (vmcSpeed) tiles.push(vmcTile(vmcSpeed));
  const body = document.getElementById("plant-body");
  body.hidden = !tiles.length;
  morph(body, tiles.join(""));
  renderSeasonDevices();
}

function showPlant() {
  document.getElementById("plant").hidden = document.getElementById("plant-body").hidden
    && document.getElementById("season-devices").hidden;
}

// What this season has to be switched by hand (the dehumidifier, in
// summer): first in the plant's section, a switch each - always there, on
// or off; the top of the page says it when it runs. How the plant follows them - a valve
// opened for its water, a bypass moving with the season - is the admin
// panel's: nothing a home needs to see.
function renderSeasonDevices() {
  const host = document.getElementById("season-devices");
  const climate = state.climate;
  const rules = (climate && climate.season_effects && climate.season_effects.rules) || [];
  const cards = rules.filter(rule => rule[climate.mode] === "manual").map(rule => {
    const device = deviceByName(rule.switch);
    if (!device || !device.controllable) return "";
    if (climate.away_until) return awayDeviceCard(rule.switch);
    if (climate.dehumidifier && climate.dehumidifier.switch === rule.switch) return dehumidifierCard(rule.switch, climate.dehumidifier);
    const on = !!(state.readings[rule.switch] || {}).is_on;
    return `<div class="season-device" data-on="${on}">
        <span class="season-device-icon">${ICONS.drop}</span>
        <span class="season-device-text"><b>${esc(displayName(rule.switch))}</b><span>${esc(tr(on ? "home.on" : "home.off_state"))}</span></span>
        <button class="switch" type="button" role="switch" data-switch="${esc(rule.switch)}" aria-checked="${on}" aria-label="${esc(displayName(rule.switch))}"></button>
      </div>`;
  }).filter(Boolean);
  host.hidden = !cards.length;
  morph(host, cards.join(""));
  showPlant();
}

// The dehumidifier running, said at the top: its card is down in the plant.
function dehumidifierLine(climate) {
  const d = climate.dehumidifier;
  if (!d || d.on !== true) return "";
  return d.until_at ? tr("home.line_dehum_until", { until: untilText(boardDate(d.until_at)) }) : tr("home.line_dehum");
}

// Away: off until the way back - no switch, no durations (the way back
// is on the zones and the away chip).
function awayDeviceCard(name) {
  return `<div class="season-device" data-on="false" data-state="away">
      <span class="season-device-icon">${ICONS.drop}</span>
      <span class="season-device-text"><b>${esc(displayName(name))}</b><span>${esc(tr("home.away_device_off"))}</span></span>
    </div>`;
}

// The dehumidifier: on for a while or until the room is dry enough (the
// admin panel's thresholds and choices - services/dehumidifier.py); by
// itself only if the panel says so. With the air dry enough it can't be
// switched on, and says so: "off", the humidity, from when it would.
function dehumidifierCard(name, d) {
  const on = d.on === true;
  const dry = !on && d.humidity != null && d.humidity < d.start_from;
  const auto = d.auto || {};
  const until = d.until_at ? untilText(boardDate(d.until_at)) : "";
  const stop = Math.round(d.stop_below), start = Math.round(d.start_from);
  let note;
  if (on) {
    note = d.mode === "auto" ? tr("home.dehum_on_auto", { stop, until })
      : d.mode === "target" ? tr("home.dehum_on_target", { stop, until })
      : until ? tr("home.dehum_on_until", { until }) : tr("home.on");
  } else if (dry) {
    note = tr(auto.enabled ? "home.dehum_disabled_auto" : "home.dehum_disabled", { rh: d.humidity, start });
  } else {
    note = tr("home.off_state");
  }
  const humidity = d.humidity != null && !dry ? `<span class="season-device-rh">${ICONS.drop}${esc(tr("home.dehum_rh", { rh: d.humidity }))}</span>` : "";
  const label = h => (h < 1 ? tr("home.vmc_minutes", { m: Math.round(h * 60) }) : tr("home.vmc_hours", { h }));
  let controls = "";
  if (on) {
    controls = `<button class="chip" type="button" data-dehum-off>${esc(tr("home.dehum_off"))}</button>`;
  } else if (!dry) {
    controls = `<span class="season-device-for">${esc(tr("home.dehum_for"))}</span>`
      + d.durations.map(h => `<button class="chip" type="button" data-dehum-hours="${h}">${esc(label(h))}</button>`).join("")
      + (d.until_target ? `<button class="chip" type="button" data-dehum-target>${esc(tr("home.dehum_until_target", { stop }))}</button>` : "");
  }
  // On by itself: when, or why not now.
  let autoNote = "";
  if (auto.enabled && !on) {
    const window = auto.from_hour === 0 && auto.to_hour === 24 ? tr("home.dehum_auto_all_day")
      : tr("home.dehum_auto_window", { from: String(auto.from_hour).padStart(2, "0"), to: String(auto.to_hour).padStart(2, "0") });
    autoNote = auto.paused_until_at
      ? tr("home.dehum_auto_paused", { until: untilText(boardDate(auto.paused_until_at)) })
      : auto.used_today_seconds >= auto.max_hours_day * 3600 - 60 ? tr("home.dehum_auto_done", { h: auto.max_hours_day })
      : tr("home.dehum_auto_on", { start, window });
  }
  return `<div class="season-device season-device-dehum" data-on="${on}" data-state="${on ? "on" : dry ? "disabled" : "off"}">
      <span class="season-device-icon">${ICONS.drop}</span>
      <span class="season-device-text"><b>${esc(displayName(name))}</b><span>${esc(note)}</span>${humidity}</span>
      ${controls ? `<span class="season-device-controls">${controls}</span>` : ""}
      ${autoNote ? `<span class="season-device-auto">${ICONS.clock}<span>${esc(autoNote)}</span></span>` : ""}
    </div>`;
}

async function onSeasonDeviceClick(event) {
  const dehum = event.target.closest("button[data-dehum-hours], button[data-dehum-target], button[data-dehum-off]");
  if (dehum) {
    const host = dehum.closest(".season-device");
    asked.climate++;
    const done = await act(dehum, () => (dehum.hasAttribute("data-dehum-off") ? api("/api/dehumidifier/off", {})
      : dehum.hasAttribute("data-dehum-target") ? api("/api/dehumidifier/on", { until: "target" })
      : api("/api/dehumidifier/on", { hours: Number(dehum.dataset.dehumHours) })));
    if (done && state.climate) {
      state.climate.dehumidifier = done.dehumidifier;
      steady(renderSeasonDevices, host);
    }
    refreshClimate(host);
    refreshDevices(host);
    return;
  }
  const toggle = event.target.closest("button[data-switch]");
  if (toggle) await flipSwitch(toggle);
}

// A switch slides at once, and back if the board refuses. Not dimmed
// while it's asked - a second tap meanwhile is just ignored.
const switching = new Set();

async function flipSwitch(toggle) {
  const name = toggle.dataset.switch;
  if (switching.has(name)) return;
  const on = toggle.getAttribute("aria-checked") !== "true";
  const before = state.readings[name];
  toggle.setAttribute("aria-checked", String(on));
  state.readings[name] = Object.assign({}, before, { is_on: on });
  switching.add(name);
  asked.devices++;
  try {
    await api(`/api/sensors/${encodeURIComponent(name)}/${on ? "on" : "off"}`, {});
  } catch (e) {
    notify(errorText(e));
    state.readings[name] = before;
    toggle.setAttribute("aria-checked", String(!on));
  } finally {
    switching.delete(name);
  }
  await refreshDevices(toggle);
}

// ------------------------------------------------------------- the VMC --
// Its switch, five levels and a fine slider, a speed for a while (boost,
// guests), the filters' reminder, and one choice for the rest of the
// time: manual (the speed stays what you pick) or a ready-made program
// (services/vmc_schedule.py runs it as a week of hours).

const VMC_ICONS = { min: "leaf", night: "moon", day: "sun", high: "breeze", boost: "wind" };
const VMC_BOOST_MINUTES = [30, 60, 120];
const VMC_GUESTS_MINUTES = 180;   // high, for guests: a dinner's length
// Ready-made programs in the unit's own speeds: [from hour, speed] per day.
const VMC_PROGRAMS = [
  { key: "day_night", all: [[0, "night"], [7, "day"], [22, "night"]] },
  { key: "work", weekdays: [[0, "night"], [6, "day"], [8, "night"], [17, "day"], [23, "night"]],
    weekend: [[0, "night"], [8, "day"], [23, "night"]] },
  { key: "peaks", all: [[0, "night"], [7, "boost"], [8, "day"], [19, "boost"], [20, "day"], [22, "night"]] },
];

function vmcLevelName(speed, value) {
  if (value === 0) return tr("home.vmc_off");
  const preset = speed.presets.find(p => p.value === value);
  return preset ? tr(`vmc_presets.${preset.key}`) : `${fmt(value, 0)}%`;
}

function vmcProgramWeek(program, speed) {
  const value = key => (speed.presets.find(p => p.key === key) || speed.presets.find(p => p.key === "day") || { value: 50 }).value;
  const day = steps => {
    const hours = new Array(24);
    steps.forEach(([from, key], i) => {
      const to = i + 1 < steps.length ? steps[i + 1][0] : 24;
      for (let h = from; h < to; h++) hours[h] = value(key);
    });
    return hours;
  };
  const week = {};
  DAYS.forEach((d, i) => { week[d] = day(program.all || (i < 5 ? program.weekdays : program.weekend)); });
  return week;
}

function sameWeek(a, b) {
  return !!a && !!b && DAYS.every(d => a[d].every((v, h) => v === b[d][h]));
}

function vmcRunning(speed) {
  const program = state.vmc && state.vmc.schedule;
  const programOn = !!(program && program.enabled && program.target === speed.name);
  const status = state.vmc && state.vmc.status;
  return { program, programOn, running: programOn && status && status.enabled ? status : null,
           boost: status && status.override && status.override.device === speed.name ? status.override : null };
}

// Which choice the board is on: "manual", a ready-made program, or
// "custom" - a week saved otherwise (by the API, or an older file).
function vmcChoice(speed) {
  const { program, programOn } = vmcRunning(speed);
  if (!programOn) return "manual";
  const match = VMC_PROGRAMS.find(p => sameWeek(program.week, vmcProgramWeek(p, speed)));
  return match ? match.key : "custom";
}

function vmcTile(speed) {
  const reading = state.readings[speed.name] || {};
  const value = reading.value;
  const on = (state.readings[speed.paired_switch] || {}).is_on !== false;
  const { program, running, boost } = vmcRunning(speed);
  // Away: held at its night speed or off until "I'm back" - nothing to press.
  const held = state.vmc && state.vmc.status && state.vmc.status.away;
  if (held && held.device === speed.name) {
    const level = vmcLevelName(speed, held.value) + (held.value ? ` (${fmt(held.value, 0)}%)` : "");
    const turning = on && value > 0 ? Math.max(0.8, 90 / value) : 0;
    const spinning = turning ? ` style="--turn: ${turning.toFixed(2)}s; --phase: -${((Date.now() / 1000) % turning).toFixed(2)}s"` : "";
    return `<div class="tile tile-wide vmc vmc-held" id="vmc-card" data-device="${esc(speed.name)}" data-on="${on}">
      <div class="vmc-live">
        <div class="vmc-head">
          <span class="vmc-fan" data-spin="${!!turning}"${spinning}>${ICONS.fan}</span>
          <span class="vmc-title"><span class="tile-name">${esc(displayName(speed.paired_switch))}</span>
            <span class="tile-note">${esc(tr("home.st_away"))}</span></span>
          <span class="tile-value">${!on ? "" : value != null ? `${fmt(value, 0)}<small>%</small>` : "--"}</span>
        </div>
        <p class="vmc-auto-note">${ICONS.suitcase}<span>${esc(tr(held.mode === "off" ? "home.vmc_away_off" : "home.vmc_away_level", { level }))}</span></p>
      </div>
    </div>`;
  }
  const until = at => (at ? untilText(boardDate(at)) : "");
  // A speed by its level's name, or "Custom 62%" between levels.
  const levelName = v => (speed.presets.some(p => p.value === v) ? vmcLevelName(speed, v) : tr("home.vmc_custom_level", { pct: fmt(v, 0) }));
  const auto = !boost && state.vmc && state.vmc.status && state.vmc.status.auto;

  // Off and on: a switch of its own - the levels are speeds, and any of
  // them (or the slider) switches it back on.
  const power = deviceByName(speed.paired_switch);
  const powerSwitch = power && power.controllable
    ? `<button class="switch vmc-power" type="button" role="switch" data-vmc-power data-device="${esc(speed.paired_switch)}" aria-checked="${on}" aria-label="${esc(tr(on ? "home.vmc_power_off" : "home.vmc_power_on"))}"></button>`
    : "";
  // Each button names the device it sets: a light with its own level (a
  // Shelly dimmer on the Home tab) is another level paired with a switch.
  const buttons = speed.presets.map(p => `<button class="vmc-preset" type="button" data-vmc="${p.value}" data-device="${esc(speed.name)}" aria-pressed="${on && value === p.value}">
      ${ICONS[VMC_ICONS[p.key]] || ""}<span class="vmc-preset-name">${esc(tr(`vmc_presets.${p.key}`))}</span><span class="vmc-preset-value">${fmt(p.value, 0)}%</span></button>`).join("");
  // The fine slider: from the slowest level - the minimum set in the
  // admin panel - to 100%; 0 is the switch's. Sent when let go.
  const lowest = Math.min(...speed.presets.map(p => p.value));
  const shown = value != null ? Math.min(100, Math.max(lowest, Math.round(value))) : lowest;
  const fine = `<div class="vmc-fine">
      <input type="range" min="${lowest}" max="100" step="1" value="${shown}" list="vmc-levels" data-vmc-fine data-device="${esc(speed.name)}" aria-label="${esc(tr("home.vmc_fine"))}" aria-valuetext="${fmt(shown, 0)}%">
      <datalist id="vmc-levels">${speed.presets.map(p => `<option value="${p.value}"></option>`).join("")}</datalist>
      <div class="vmc-fine-scale" aria-hidden="true"><span>${fmt(lowest, 0)}%</span><span>100%</span></div>
    </div>`;

  // Why it runs at this speed, always under its name: the program, a hand
  // pick until the program's next change, a timed speed, an automatic.
  let why = "";
  if (!on) {
    why = running ? (running.value === 0 ? tr("home.vmc_off_program") : tr("home.vmc_off_until", { until: running.until || "" }))
      : tr("home.vmc_off");
  } else if (boost) {
    why = tr("home.vmc_why_timed", { level: levelName(boost.value), until: until(boost.until) });
  } else if (value != null) {
    const level = levelName(Math.round(value));
    if (auto) why = tr("home.vmc_why_auto", { level });
    else if (running && running.value === Math.round(value)) {
      why = running.until_at ? tr("home.vmc_why_program", { level, until: until(running.until_at) })
        : tr("home.vmc_why_program_always", { level });
    } else if (running && running.until_at) why = tr("home.vmc_why_hand_until", { level, until: until(running.until_at) });
    else why = tr("home.vmc_why_hand", { level });
  }

  // For a while: the maximum (a shower, cooking), or high for guests -
  // then back to the program, or to before.
  const top = Math.max(...speed.presets.map(p => p.value));
  const high = (speed.presets.find(p => p.key === "high") || {}).value;
  const timedRow = (icon, text, minutes, level) => `<div class="vmc-boost">${icon}<span class="vmc-boost-text">${esc(text)}</span>
      <span class="vmc-boost-times">${minutes.map(m => `<button class="chip" type="button" data-vmc-boost="${m}" data-value="${level}" data-device="${esc(speed.name)}">${esc(m < 60 ? tr("home.vmc_minutes", { m }) : tr("home.vmc_hours", { h: m / 60 }))}</button>`).join("")}</span></div>`;
  const timed = boost
    ? `<div class="vmc-boost" data-active="true">${ICONS.wind}<span class="vmc-boost-text">${esc(tr("home.vmc_boost_running", { level: levelName(boost.value), until: until(boost.until) }))}</span>
        <button class="chip" type="button" data-vmc-boost-cancel>${esc(tr("home.vmc_boost_cancel"))}</button></div>`
    : timedRow(ICONS.wind, tr("home.vmc_boost_for", { level: vmcLevelName(speed, top) }), VMC_BOOST_MINUTES, top)
      + (high ? timedRow(ICONS.breeze, tr("home.vmc_guests", { level: vmcLevelName(speed, high) }), [VMC_GUESTS_MINUTES], high) : "");

  // An automatic change under way (services/vmc_auto.py): its reason.
  const autoNote = auto
    ? `<p class="vmc-auto-note">${auto.reason === "cooling" ? ICONS.moon : ICONS.drop}<span>${esc(tr(`home.vmc_auto_${auto.reason}`, {
        zone: displayName(auto.zone || ""), rh: fmt(auto.humidity, 0), level: vmcLevelName(speed, auto.value).toLowerCase(),
        out: fmt(auto.reason === "cooling" ? auto.outdoor : auto.outdoor_dew), in: fmt(auto.reason === "cooling" ? auto.indoor : auto.indoor_dew) }))}</span></p>`
    : "";

  // The filters, from two weeks before they're due (services/vmc_filters.py).
  const filters = state.vmc && state.vmc.filters;
  const filtersRow = filters && (filters.state === "soon" || filters.state === "due")
    ? `<div class="vmc-filters" data-state="${filters.state}">${ICONS.filter}<span>${esc(filters.days_left > 0
        ? tr("home.vmc_filters_soon", { n: filters.days_left })
        : filters.days_left === 0 ? tr("home.vmc_filters_today") : tr("home.vmc_filters_due", { n: -filters.days_left }))}</span>
        <button class="chip" type="button" data-vmc-filters-cleaned>${esc(tr("home.vmc_filters_cleaned"))}</button></div>`
    : "";

  // The rest of the time: one choice, manual or a program, behind one
  // line that opens. A week set elsewhere shows as "custom", only while
  // it's the one running.
  const choice = vmcChoice(speed);
  const choices = [
    { key: "manual", note: tr("home.vmc_tpl_manual_note") },
    ...VMC_PROGRAMS.map(p => ({ key: p.key, note: tr(`home.vmc_tpl_${p.key}_note`) })),
    ...(choice === "custom" ? [{ key: "custom", note: tr("home.vmc_tpl_custom_note") }] : []),
  ];
  const programNote = running
    ? (running.until_at
        ? tr("home.vmc_program_now", { level: vmcLevelName(speed, running.value), until: until(running.until_at),
            next: vmcLevelName(speed, running.next_value).toLowerCase() })
        : tr("home.vmc_program_always", { level: vmcLevelName(speed, running.value) }))
    : tr("home.vmc_program_manual");
  const programs = program ? `<details class="vmc-plan" id="vmc-plan"${state.vmcPlanOpen ? " open" : ""}>
      <summary><span class="sched-season-label">${esc(tr("home.vmc_program"))}</span>
        <span class="vmc-plan-name">${esc(tr(`home.vmc_tpl_${choice}`))}</span>${ICONS.chevron}</summary>
      <div class="vmc-programs" role="radiogroup" aria-label="${esc(tr("home.vmc_program"))}">
        ${choices.map(c => `<button class="vmc-template" type="button" role="radio" aria-checked="${c.key === choice}"
            data-vmc-choice="${c.key}" data-device="${esc(speed.name)}">
          <span class="vmc-template-name">${esc(tr(`home.vmc_tpl_${c.key}`))}</span>
          <span class="vmc-template-note">${esc(c.note)}</span></button>`).join("")}
      </div>
      <p class="vmc-program-note">${ICONS.clock}<span>${esc(programNote)}</span></p>
    </details>` : "";

  // The fan turns faster as the speed goes up; its phase comes from the
  // clock, so a refresh that redraws the card doesn't make it jump.
  const turn = on && value > 0 ? Math.max(0.8, 90 / value) : 0;
  const spin = turn ? ` style="--turn: ${turn.toFixed(2)}s; --phase: -${((Date.now() / 1000) % turn).toFixed(2)}s"` : "";
  return `<div class="tile tile-wide vmc" id="vmc-card" data-device="${esc(speed.name)}" data-on="${on}">
    <div class="vmc-live">
      <div class="vmc-head">
        <span class="vmc-fan" data-spin="${!!turn}"${spin}>${ICONS.fan}</span>
        <span class="vmc-title"><span class="tile-name">${esc(displayName(speed.paired_switch))}</span>
          <span class="tile-note">${esc(why)}</span></span>
        <span class="tile-value">${!on ? "" : value != null ? `${fmt(value, 0)}<small>%</small>` : "--"}</span>
        ${powerSwitch}
      </div>
      <div class="vmc-controls">
        <div class="vmc-presets" data-count="${speed.presets.length}" role="group" aria-label="${esc(tr("home.vmc_speed"))}">${buttons}</div>
        ${fine}
      </div>
      <div class="vmc-side">${autoNote}${timed}${filtersRow}</div>
    </div>
    ${programs}
  </div>`;
}

async function onPlantClick(event) {
  const target = event.target.closest("button");
  if (!target) return;
  const card = target.closest("#vmc-card");
  const speed = card && deviceByName(card.dataset.device);
  if (target.dataset.vmcChoice && target.getAttribute("aria-checked") === "true") return;
  if (target.dataset.switch) { await flipSwitch(target); return; }
  pressNow(target);
  busy(target, true);
  asked.devices++;
  try {
    const boosting = speed && vmcRunning(speed).boost;
    if (target.hasAttribute("data-vmc-power") && target.dataset.device) {
      // Off ends a timed speed, so it isn't switched back on later.
      const turnOn = target.getAttribute("aria-checked") !== "true";
      if (!turnOn && boosting) await api("/api/vmc-schedule/boost/cancel", {});
      await api(`/api/sensors/${encodeURIComponent(target.dataset.device)}/${turnOn ? "on" : "off"}`, {});
    } else if (target.dataset.vmcBoost && target.dataset.device) {
      await api("/api/vmc-schedule/boost", { device: target.dataset.device, value: Number(target.dataset.value),
                                             minutes: Number(target.dataset.vmcBoost) });
    } else if (target.hasAttribute("data-vmc-boost-cancel")) {
      await api("/api/vmc-schedule/boost/cancel", {});
    } else if (target.hasAttribute("data-vmc-filters-cleaned")) {
      const done = await api("/api/vmc-filters/cleaned", {});
      if (state.vmc && done) state.vmc.filters = done.filters;
    } else if (target.dataset.vmc && target.dataset.device) {
      // A speed picked by hand ends a timed one, so it isn't undone later.
      if (boosting) await api("/api/vmc-schedule/boost/cancel", {});
      await api(`/api/sensors/${encodeURIComponent(target.dataset.device)}/set-value`, { value: Number(target.dataset.vmc) });
    } else if (target.dataset.vmcChoice === "manual") {
      // No program: the week stays saved, unused.
      await api("/api/vmc-schedule", { enabled: false });
    } else if (target.dataset.vmcChoice && speed) {
      // A program is its week, run on this VMC - picking one is what
      // switches it on and names the device it drives.
      const program = VMC_PROGRAMS.find(p => p.key === target.dataset.vmcChoice);
      await api("/api/vmc-schedule", { enabled: true, target: speed.name, week: vmcProgramWeek(program, speed) });
    } else {
      return;
    }
  } catch (e) {
    notify(errorText(e));
  } finally {
    busy(target, false);
  }
  await refreshDevices(target);
}

// The fine slider: its number and the level it's on follow the finger;
// the speed is sent when it's let go.
function onVmcSlide(event) {
  const fine = event.target.closest("[data-vmc-fine]");
  if (!fine) return;
  const card = fine.closest("#vmc-card");
  const v = Number(fine.value);
  fine.setAttribute("aria-valuetext", `${fmt(v, 0)}%`);
  const shown = card.querySelector(".vmc-head .tile-value");
  if (shown) shown.innerHTML = `${fmt(v, 0)}<small>%</small>`;
  card.querySelectorAll(".vmc-preset[data-vmc]").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.vmc) === v)));
}

async function onVmcSlideEnd(event) {
  const fine = event.target.closest("[data-vmc-fine]");
  if (!fine) return;
  const speed = deviceByName(fine.dataset.device);
  asked.devices++;
  try {
    if (speed && vmcRunning(speed).boost) await api("/api/vmc-schedule/boost/cancel", {});
    await api(`/api/sensors/${encodeURIComponent(fine.dataset.device)}/set-value`, { value: Number(fine.value) });
  } catch (e) {
    notify(errorText(e));
  }
  fine.blur();     // the board's value shows again from the next refresh
  await refreshDevices(fine);
}

// A choice in a group lights up as it's tapped, the others go out - the
// board's answer (refreshDevices) confirms it or puts it back.
function pressNow(button) {
  const group = button.closest("[role=group], [role=radiogroup]");
  const attr = button.hasAttribute("aria-checked") ? "aria-checked" : button.hasAttribute("aria-pressed") ? "aria-pressed" : null;
  if (!group || !attr) return;
  group.querySelectorAll(`[${attr}]`).forEach(b => b.setAttribute(attr, String(b === button)));
}

// ---------------------------------------------------------- the house --
// Everything else on the Home tab, as plain tiles: a switch, a level,
// a reading, a cover.

function houseDevices() {
  if (!state.devices) return [];
  const climateNames = new Set();
  (state.climate ? state.climate.zones : []).forEach(z => { climateNames.add(z.sensor); climateNames.add(z.valve); });
  const vmc = state.devices.sensors.find(d => d.kind === "number" && d.paired_switch && (d.presets || []).length);
  return state.devices.sensors.filter(d => d.tab !== "climate" && !climateNames.has(d.name)
    && !(vmc && (d.name === vmc.name || d.name === vmc.paired_switch)));
}

function renderHouse() {
  const tiles = [];
  const devices = houseDevices();
  const numbersByPair = Object.fromEntries(devices.filter(d => d.kind === "number" && d.paired_switch).map(d => [d.paired_switch, d]));
  devices.forEach(d => {
    if (d.kind === "number" && d.paired_switch && devices.some(o => o.name === d.paired_switch)) return;
    const r = state.readings[d.name] || {};
    const name = esc(displayName(d.name));
    if (d.kind === "switch" && r.power_w != null && state.energy[d.name]) {
      // A plug that measures its power: its days, with its switch.
      const control = d.controllable && !d.hide_toggle
        ? `<button class="switch" type="button" role="switch" data-switch="${esc(d.name)}" aria-checked="${!!r.is_on}" aria-label="${name}"></button>` : "";
      tiles.push(energyTile(d, r, control));
    } else if (d.kind === "switch") {
      const level = numbersByPair[d.name];
      const slider = level ? rangeHtml(level, state.readings[level.name] || {}) : "";
      tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${name}</span>
        ${d.controllable && !d.hide_toggle ? `<button class="switch" type="button" role="switch" data-switch="${esc(d.name)}" aria-checked="${!!r.is_on}" aria-label="${name}"></button>`
          : `<span class="dot-state" data-on="${!!r.is_on}">${esc(tr(r.is_on ? "home.on" : "home.off_state"))}</span>`}</div>
        ${r.power_w != null ? `<div class="tile-note">${fmt(r.power_w, 0)} W</div>` : ""}${slider}${readingNote(r)}</div>`);
    } else if (d.kind === "number") {
      tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${name}</span></div>${rangeHtml(d, r)}${readingNote(r)}</div>`);
    } else if (d.kind === "power_meter") {
      tiles.push(energyTile(d, r));
    } else if (d.kind === "climate" && isAirReading(r)) {
      tiles.push(airTile(d, r));
    } else if (d.kind === "climate") {
      tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${name}</span></div>
        <div class="tile-value">${r.temperature_c != null ? `${fmt(r.temperature_c)}<small>°</small>` : "--"}${r.humidity_pct != null ? ` <small>${fmt(r.humidity_pct, 0)}%</small>` : ""}</div>${readingNote(r)}</div>`);
    }
  });
  (state.devices ? state.devices.covers : []).forEach(name => {
    tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${esc(name)}</span></div>
      <div class="segmented">
        <button type="button" data-cover="${esc(name)}" data-cover-action="open">${esc(tr("covers.btn_open"))}</button>
        <button type="button" data-cover="${esc(name)}" data-cover-action="stop">${esc(tr("covers.btn_stop"))}</button>
        <button type="button" data-cover="${esc(name)}" data-cover-action="close">${esc(tr("covers.btn_close"))}</button>
      </div></div>`);
  });
  // The wide ones (a meter's days, the air's day) first, the small ones
  // after: two wide ones fill a row, and the small ones the gaps (CSS
  // dense) - a wide one after a small one left a hole beside it.
  const wide = html => /^<div class="tile (energy|air)\b/.test(html);
  tiles.sort((a, b) => wide(b) - wide(a));
  document.getElementById("house").hidden = !tiles.length;
  morph(document.getElementById("house-body"), tiles.join(""));
}

// --------------------------------------------------------- the air --
// An air-quality sensor: its CO2 large, with the last 24 hours under it
// (a night with the door shut reads as a curve), and the particles and
// organic compounds each against the usual limit. `AIR_LIMITS`: up to
// the first value it's fine, up to the second it's worth airing, past it
// it's poor - CO2 by the usual indoor bands, PM2.5/PM10 by the WHO's 24 h
// guidelines (15/45), total VOCs under 0.3 mg/m3, formaldehyde under
// the WHO's 0.1 mg/m3.

const AIR_LIMITS = {
  co2_ppm: [800, 1200],
  pm25_ugm3: [15, 35],
  pm10_ugm3: [45, 100],
  voc_mgm3: [0.3, 1],
  ch2o_mgm3: [0.06, 0.1],
};
const AIR_FACTS = [
  { field: "pm25_ugm3", unit: "µg/m³", digits: 0 },
  { field: "pm10_ugm3", unit: "µg/m³", digits: 0 },
  { field: "voc_mgm3", unit: "mg/m³", digits: 2 },
  { field: "ch2o_mgm3", unit: "mg/m³", digits: 3 },
];
const AIR_TONE = { ok: 0, warn: 1, bad: 2 };

function isAirReading(reading) {
  return Object.keys(AIR_LIMITS).some(field => reading[field] != null);
}

function airLevel(field, value) {
  const [fine, poor] = AIR_LIMITS[field];
  return value <= fine ? "ok" : value <= poor ? "warn" : "bad";
}

function airTile(device, r) {
  const measured = Object.keys(AIR_LIMITS).filter(f => r[f] != null).map(f => ({ field: f, level: airLevel(f, r[f]) }));
  const worst = measured.reduce((a, b) => (AIR_TONE[b.level] > AIR_TONE[a.level] ? b : a), measured[0]);
  const summary = worst.level === "ok" ? tr("home.air_good")
    : tr(worst.level === "bad" ? "home.air_very_high" : "home.air_high", { what: tr(`home.air_${worst.field}`) });
  const facts = AIR_FACTS.filter(f => r[f.field] != null).map(f => `<span class="air-fact" data-level="${airLevel(f.field, r[f.field])}">
      <span class="air-dot"></span>${esc(tr(`home.air_${f.field}`))} <b>${fmt(r[f.field], f.digits)}</b> <small>${f.unit}</small></span>`).join("");
  const extra = [r.temperature_c != null ? `${fmt(r.temperature_c)}°` : "", r.humidity_pct != null ? `${fmt(r.humidity_pct, 0)}%` : "",
    r.battery_pct != null ? tr("home.battery", { pct: Math.round(r.battery_pct) }) : ""].filter(Boolean).join(" · ");
  return `<div class="tile air" data-sensor="${esc(device.name)}" data-level="${worst.level}">
    <div class="tile-head"><span class="tile-name">${esc(displayName(device.name))}</span>
      <span class="state" data-tone="${{ ok: "ok", warn: "warn", bad: "bad" }[worst.level]}">${esc(summary)}</span></div>
    ${r.co2_ppm != null ? `<div class="air-co2" data-level="${airLevel("co2_ppm", r.co2_ppm)}">
      <span class="tile-value">${fmt(r.co2_ppm, 0)}<small>ppm</small></span><span class="air-co2-name">CO₂</span></div>
      ${airSparkHtml(device.name)}` : ""}
    ${facts ? `<div class="air-facts">${facts}</div>` : ""}
    ${extra ? `<div class="tile-note">${esc(extra)}</div>` : ""}${readingNote(r)}
  </div>`;
}

// The CO2 of the last 24 hours as a line, the "air it" and "poor" limits
// dashed, and the day's peak said in words.
function airSparkHtml(name) {
  const points = state.airHistory[name] || [];
  if (points.length < 2) return "";
  const w = 300, h = 54;
  const start = Date.now() / 1000 - 86400;
  const values = points.map(p => p.value);
  const lo = Math.min(400, ...values), hi = Math.max(1300, ...values);
  const x = ts => ((ts - start) / 86400) * w;
  const y = v => h - 2 - ((v - lo) / (hi - lo)) * (h - 4);
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.ts).toFixed(1)} ${y(p.value).toFixed(1)}`).join(" ");
  const limits = AIR_LIMITS.co2_ppm.map(v => `<line class="air-limit" x1="0" x2="${w}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" vector-effect="non-scaling-stroke"/>`).join("");
  const peak = points.reduce((a, b) => (b.value > a.value ? b : a));
  return `<svg class="air-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(tr("home.air_24h"))}">
      ${limits}<path class="air-line" d="${line}" vector-effect="non-scaling-stroke"/></svg>
    <div class="air-spark-note"><span>${esc(tr("home.air_24h"))}</span><span>${esc(tr("home.air_peak", { value: fmt(peak.value, 0), when: whenText(wall(peak.ts * 1000)) }))}</span></div>`;
}

async function refreshAirHistory() {
  const sensors = houseDevices().filter(d => d.kind === "climate" && isAirReading(state.readings[d.name] || {}));
  await Promise.all(sensors.map(async d => {
    try {
      const data = await api(`/api/sensors/${encodeURIComponent(d.name)}/history?field=co2_ppm&range=24h`);
      state.airHistory[d.name] = data.series.co2_ppm || [];
    } catch (e) { /* keep what we had */ }
  }));
  if (sensors.length) steady(renderHouse);
}

// ------------------------------------------------------ the energy --
// A power meter: what it draws now, today's peak with when, and the
// week (Monday on), the month or the year by month as bars - tap one for
// its kWh and peak. Days worked out from the recorded power, before the
// meter's own counter was kept, are drawn lighter and said to be estimates.

const ENERGY_RANGES = ["week", "month", "year"];

function fmtPower(w) {
  return w >= 1000 ? `${fmt(w / 1000, 1)} kW` : `${fmt(w, 0)} W`;
}

// Against the contract: amber past what it bears for good, red past what
// it bears for a moment (the power budget, when one is configured).
function powerLevel(device, w) {
  const b = device.power_budget;
  if (!b || w == null) return "ok";
  return w >= b.trip_risk_w ? "bad" : w >= b.available_power_w ? "warn" : "ok";
}

function dayName(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const weekday = DAYS[(new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7];
  return `${tr(`home.day_long.${weekday}`)} ${d} ${tr("home.months_long")[m - 1]}`;
}

// The bars of the period shown: [{label, kwh, peak_w, estimated, current, title}].
function energyBars(data, range) {
  const todayIso = data.today ? data.today.day : null;
  const now = boardNow();
  const todayKey = todayIso || `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}-${String(now.getUTCDate()).padStart(2, "0")}`;
  if (range === "year") {
    const months = tr("home.months_short");
    return data.year.months.map((m, i) => ({
      label: months[i], kwh: m.kwh, estimated: m.estimated, current: todayKey.startsWith(m.month),
      title: m.kwh == null ? "" : tr("home.energy_bar", { when: tr("home.months_long")[i], kwh: fmt(m.kwh, 1) }),
    }));
  }
  const span = data[range];
  const labels = tr("climate.days");
  return span.days.map((d, i) => {
    const iso = d ? d.day : null;
    const n = i + 1;
    const label = range === "week" ? labels[i] : (n === 1 || n % 5 === 0 ? String(n) : "");
    if (!d || d.kwh == null) return { label, kwh: null };
    return {
      label, kwh: d.kwh, peak_w: d.peak_w, estimated: d.source === "power", current: iso === todayKey,
      title: d.peak_w != null
        ? tr("home.energy_bar_peak", { when: dayName(iso), kwh: fmt(d.kwh, 1), peak: fmtPower(d.peak_w) })
        : tr("home.energy_bar", { when: dayName(iso), kwh: fmt(d.kwh, 1) }),
    };
  });
}

function energyTile(device, r, control = "") {
  const data = state.energy[device.name];
  const level = powerLevel(device, r.power_w);
  const today = data && data.today;
  const peak = today && today.peak_w != null
    ? tr("home.energy_peak_today", { value: fmtPower(today.peak_w), when: whenText(wall(today.peak_ts * 1000)) }) : "";
  const todayKwh = r.day_kwh != null ? r.day_kwh : today && today.kwh;
  const yesterdayKwh = r.yesterday_kwh != null ? r.yesterday_kwh : data && data.yesterday && data.yesterday.kwh;
  const facts = [todayKwh != null ? tr("home.energy_today", { kwh: fmt(todayKwh, 1) }) : "",
    yesterdayKwh != null ? tr("home.energy_yesterday", { kwh: fmt(yesterdayKwh, 1) }) : "",
    r.voltage_v != null ? `${fmt(r.voltage_v, 0)} V` : "", r.current_a != null ? `${fmt(r.current_a, 2)} A` : ""].filter(Boolean);
  let chart = "";
  if (data) {
    const range = state.energyRange[device.name] || "week";
    const bars = energyBars(data, range);
    const top = Math.max(0.1, ...bars.map(b => b.kwh || 0));
    const picked = state.energyPick[device.name];
    const pick = picked != null && bars[picked] && bars[picked].kwh != null ? bars[picked] : null;
    const span = data[range];
    const total = tr(`home.energy_total_${range}`, { kwh: fmt(span.kwh, 1) });
    const since = data.since && data.since > span.from ? tr("home.energy_since", { date: dayName(data.since) }) : "";
    chart = `
      <div class="segmented energy-ranges" role="group" aria-label="${esc(tr("home.energy_ranges"))}">
        ${ENERGY_RANGES.map(k => `<button type="button" data-energy-range="${k}" aria-pressed="${k === range}">${esc(tr(`home.energy_tab_${k}`))}</button>`).join("")}
      </div>
      <div class="energy-bars" style="--n: ${bars.length}">
        ${bars.map((b, i) => `<button type="button" class="energy-bar" data-energy-pick="${i}" style="--h: ${b.kwh == null ? 0 : Math.max(2, b.kwh / top * 100).toFixed(1)}%"
          ${b.kwh == null ? "disabled" : ""} data-current="${!!b.current}" data-estimated="${!!b.estimated}" aria-pressed="${pick === b}"
          title="${esc(b.title || "")}" aria-label="${esc(b.title || b.label)}"></button>`).join("")}
      </div>
      <div class="energy-labels" style="--n: ${bars.length}">${bars.map(b => `<span>${esc(b.label)}</span>`).join("")}</div>
      <div class="energy-total"><b>${esc(pick ? pick.title : total)}</b>${since ? ` <span>· ${esc(since)}</span>` : ""}</div>
      ${span.estimated ? `<div class="tile-note">${esc(tr("home.energy_estimated"))}</div>` : ""}`;
  }
  const budget = device.power_budget ? `<div class="tile-note">${esc(tr("sensors.power_budget_note", {
    available: fmt(device.power_budget.available_power_w / 1000, 1), trip: fmt(device.power_budget.trip_risk_w / 1000, 1) }))}</div>` : "";
  return `<div class="tile energy" data-sensor="${esc(device.name)}">
    <div class="tile-head"><span class="tile-name">${esc(displayName(device.name))}</span>${control}</div>
    <div class="energy-now" data-level="${level}">
      <span class="tile-value">${r.power_w != null ? `${fmt(r.power_w, 0)}<small>W</small>` : "--"}</span>
      ${peak ? `<span class="energy-peak" data-level="${powerLevel(device, today.peak_w)}">${esc(peak)}</span>` : ""}
    </div>
    ${facts.length ? `<div class="tile-note">${esc(facts.join(" · "))}</div>` : ""}
    ${chart}${budget}${readingNote(r)}
  </div>`;
}

async function refreshEnergy() {
  // Meters, and plugs that measure their power.
  const meters = houseDevices().filter(d => d.kind === "power_meter"
    || (d.kind === "switch" && (state.readings[d.name] || {}).power_w != null));
  await Promise.all(meters.map(async d => {
    try {
      state.energy[d.name] = await api(`/api/energy/${encodeURIComponent(d.name)}`);
    } catch (e) { /* keep what we had */ }
  }));
  if (meters.length) steady(renderHouse);
}

function rangeHtml(device, reading) {
  const range = device.range || { min: 0, max: 100, unit: "%" };
  const value = reading.value != null ? reading.value : range.min;
  return `<label class="tile-note">${esc(tr("home.level"))} <b>${fmt(value, 0)}${esc(range.unit || "")}</b>
    <input type="range" min="${range.min}" max="${range.max}" value="${value}" data-level="${esc(device.name)}"></label>`;
}

// A device whose last read failed says so - the reading comes back as
// {ok: false, error} or, through api(), as a failed request.
function readingNote(reading) {
  return reading.ok === false ? `<div class="tile-note tile-off">${esc(tr("home.not_answering"))}</div>` : "";
}

async function onHouseClick(event) {
  const button = event.target.closest("button");
  if (!button) return;
  // An energy card's period and bars only change this page.
  const energyCard = button.closest(".tile.energy");
  if (energyCard && button.dataset.energyRange) {
    state.energyRange[energyCard.dataset.sensor] = button.dataset.energyRange;
    delete state.energyPick[energyCard.dataset.sensor];
    steady(renderHouse, button);
    return;
  }
  if (energyCard && button.dataset.energyPick !== undefined) {
    const name = energyCard.dataset.sensor;
    const i = Number(button.dataset.energyPick);
    if (state.energyPick[name] === i) delete state.energyPick[name];
    else state.energyPick[name] = i;
    steady(renderHouse, button);
    return;
  }
  if (button.dataset.switch) { await flipSwitch(button); return; }
  button.disabled = true;
  try {
    if (button.dataset.cover) {
      await api(`/api/covers/${encodeURIComponent(button.dataset.cover)}/action/${button.dataset.coverAction}`, {});
    }
  } catch (e) {
    notify(errorText(e));
  } finally {
    button.disabled = false;
  }
}

async function onHouseChange(event) {
  const input = event.target.closest("input[data-level]");
  if (!input) return;
  try {
    await api(`/api/sensors/${encodeURIComponent(input.dataset.level)}/set-value`, { value: Number(input.value) });
    await refreshDevices(input);
  } catch (e) {
    notify(errorText(e));
  }
}

// ------------------------------------------------------------ refreshes --

// Each refresh is numbered: an answer to one asked before an action (or
// before a newer refresh) is older than what the page already shows - it's
// dropped, not drawn over it. A switch tapped on never flicks back off.
const asked = { climate: 0, devices: 0 };

// `anchor`: what was just touched - held under the finger. Otherwise
// what's in the middle of the screen stays where it is.
async function refreshClimate(anchor) {
  const ticket = ++asked.climate;
  try {
    const climate = await api("/api/climate/zones");
    if (ticket !== asked.climate) return;
    state.climate = climate;
    climateZones = state.climate.zones || [];
    if (state.climate.mode && state.climate.mode !== climateMode) {
      climateMode = state.climate.mode;
      ensurePaintValueForMode();
    }
    state.failures = 0;
    const missing = state.climate.zones.filter(z => !state.weeks[z.name]);
    if (missing.length) await loadWeeks(missing.map(z => z.name));
    if (ticket !== asked.climate) return;
  } catch (e) {
    if (ticket !== asked.climate) return;
    state.failures += 1;
    if (state.failures >= 2) {
      const offline = document.getElementById("offline");
      steady(() => {
        offline.hidden = false;
        setText(offline, tr("home.offline"));
      }, anchor);
    }
    return;
  }
  steady(() => {
    document.getElementById("offline").hidden = true;
    renderHero();
    renderZones();
    renderPlant();
    renderHealth();
  }, anchor);
}

async function loadWeeks(names) {
  await Promise.all(names.map(async name => {
    const weeks = {};
    for (const mode of ["heat", "cool"]) {
      try {
        weeks[mode] = (await api(`/api/climate/zones/${encodeURIComponent(name)}/schedule?mode=${mode}`)).week;
      } catch (e) { /* no week for that season */ }
    }
    state.weeks[name] = weeks;
  }));
}

async function refreshHistory() {
  if (!state.climate) return;
  historyAt = Date.now();
  await Promise.all(state.climate.zones.map(async zone => {
    try {
      const [temps, runs, hours] = await Promise.all([
        api(`/api/sensors/${encodeURIComponent(zone.sensor)}/history?field=temperature_c&range=24h`),
        api(`/api/sensors/${encodeURIComponent(zone.valve)}/history?field=is_on&range=24h`),
        api(`/api/climate/zones/${encodeURIComponent(zone.name)}/on-hours`),
      ]);
      state.history[zone.name] = { temps: temps.series.temperature_c || [], runs: runs.series.is_on || [] };
      state.onHours[zone.name] = hours;
    } catch (e) { /* keep what we had */ }
  }));
  steady(() => { renderHero(); renderZones(); });
}

async function refreshDevices(anchor) {
  const ticket = ++asked.devices;
  try {
    if (!state.devices) state.devices = await api("/api/devices");
    // All of them in one request (a dozen on a real install, which a
    // browser sends six at a time).
    const [all, vmc] = await Promise.all([
      api("/api/readings"),
      state.devices.sensors.some(d => d.kind === "number" && (d.presets || []).length)
        ? api("/api/vmc-schedule").catch(() => null) : Promise.resolve(state.vmc),
    ]);
    if (ticket !== asked.devices) return;
    state.devices.sensors.forEach(d => { state.readings[d.name] = all.readings[d.name] || { ok: false }; });
    state.dayRanges = all.day_ranges || {};
    state.vmc = vmc;
  } catch (e) {
    return;
  }
  steady(() => {
    renderOutdoor();
    renderPlant();
    renderHouse();
  }, anchor);
}

async function refreshProblems() {
  const health = document.getElementById("health");
  const text = document.getElementById("health-text");
  try {
    const data = await api("/api/problems");
    state.problems = data.problems || [];
    renderHealth();
  } catch (e) {
    health.dataset.state = "unknown";
  }
}

// What the chip says: a problem first; else the Shellys not connected yet
// (the admin panel's to do); else all is well.
function renderHealth() {
  const health = document.getElementById("health");
  if (!state.problems) return;
  const count = residentProblems(state.problems).length;
  const todo = ((state.climate && state.climate.to_configure) || []).length;
  health.dataset.state = count ? "warn" : todo ? "setup" : "ok";
  setText(document.getElementById("health-text"), count ? tr("home.health_problems", { count })
    : todo ? tr("home.health_setup") : tr("home.health_ok"));
}

// A Shelly still to configure, as the page calls it.
function setupName(item) {
  if (item.role === "sensor" || item.role === "valve") return tr(`home.setup_${item.role}`, { zone: item.zone });
  return displayName(item.name);
}

// ---------------------------------------------------------------- start --

document.getElementById("zones").addEventListener("click", onZoneClick);
document.getElementById("zones").addEventListener("submit", onManualSubmit);
document.getElementById("plant-body").addEventListener("click", onPlantClick);
document.getElementById("plant-body").addEventListener("input", onVmcSlide);
document.getElementById("plant-body").addEventListener("change", onVmcSlideEnd);
// The program's line, opened or closed, stays so across the card's redraws.
document.getElementById("plant-body").addEventListener("toggle", event => {
  if (event.target.id === "vmc-plan") state.vmcPlanOpen = event.target.open;
}, true);
document.getElementById("season-devices").addEventListener("click", onSeasonDeviceClick);
document.getElementById("house-body").addEventListener("click", onHouseClick);
document.getElementById("house-body").addEventListener("change", onHouseChange);
document.getElementById("away-chip").addEventListener("click", openAway);
document.getElementById("season-chip").addEventListener("click", openSeason);
document.getElementById("health").addEventListener("click", openHealth);
document.getElementById("sheet").addEventListener("click", e => state.sheet && state.sheet.onClick && state.sheet.onClick(e));
document.getElementById("sheet-close").addEventListener("click", closeSheet);
document.getElementById("sheet-backdrop").addEventListener("click", closeSheet);
document.addEventListener("keydown", e => { if (e.key === "Escape" && state.sheet) closeSheet(); });
// Safari shows :active on a tap only when the page listens to touches.
document.addEventListener("touchstart", () => {}, { passive: true });

// Back to the page - from another app, the phone woken up: what it shows
// is brought up to date at once and in place, not after the next round,
// and without a reload that would scroll it back to the top.
let historyAt = 0;

document.addEventListener("visibilitychange", () => {
  if (document.hidden || !state.climate) return;
  refreshClimate();
  refreshDevices();
  refreshProblems();
  if (Date.now() - historyAt > HISTORY_MS) refreshHistory();
});

(async function start() {
  // Until the board's clock has answered, "now" would be this phone's.
  for (let i = 0; i < 30 && !(window.ServerClock && window.ServerClock.now()); i++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await refreshClimate();
  await Promise.all([refreshHistory(), refreshDevices(), refreshProblems()]);
  refreshAirHistory();
  setInterval(refreshAirHistory, HISTORY_MS);
  refreshEnergy();
  setInterval(refreshEnergy, HISTORY_MS);
  setInterval(refreshClimate, REFRESH_MS);
  setInterval(refreshDevices, REFRESH_MS);
  setInterval(refreshHistory, HISTORY_MS);
  setInterval(refreshProblems, PROBLEMS_MS);
  setInterval(updateNowHighlight, 60000);
})();
