/* The redesigned home page - see templates/home.html.

   Three questions, in the order people ask them:
   1. Glance - is the house comfortable, is it doing anything, does
      anything need me? A sentence at the top, then one card per zone.
   2. Tweak - warmer or off for a while, from the card itself.
   3. Plan - the week, in time bands instead of 168 cells, behind a
      button in a sheet.

   Everything is timed by the board's clock, not the phone's: schedules
   run on the board, so "until 20:30" must mean the board's 20:30.
   Same API as the classic page; nothing new on the server. */
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
const DURATIONS = [1, 2, 4];
const AWAY_OPTIONS = [24, 72, 168];

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

async function api(path, body) {
  const options = body === undefined ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({ ok: false }));
  if (!res.ok || data.ok === false) {
    const error = new Error(data.error || res.statusText);
    error.data = data;
    throw error;
  }
  return data;
}

function errorText(error) {
  return apiError(error.data) || error.message || tr("home.request_failed");
}

// ------------------------------------------- what the shared grid needs --
// static/schedule_grid.js - the weekly grid, the same as the classic
// page's - relies on these, defined the way app.js defines them.

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

// The server's refusal in this page's language (same as app.js's).
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

// A painted hour changes what the card says comes next: the card reads
// the grid being edited.
function updateZoneNext(zone) {
  const week = scheduleWeeks[zone.name];
  const mode = scheduleWeekModes[zone.name];
  if (week && mode) state.weeks[zone.name] = Object.assign({}, state.weeks[zone.name], { [mode]: week });
  const card = document.getElementById(zoneId(zone.name));
  const current = state.climate && state.climate.zones.find(z => z.name === zone.name);
  if (card && current) fillZone(card, current, state.climate);
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

function valveRule(zone, climate) {
  const h = climate.hysteresis_c;
  if (zone.target_c == null || zone.temperature_c == null || h == null || !zone.sensor_ok) return null;
  const cool = climate.mode === "cool";
  if (zone.valve_on) {
    return tr(cool ? "home.runs_until_cool" : "home.runs_until_heat",
      { value: fmt(cool ? zone.target_c - h : zone.target_c + h) });
  }
  return tr(cool ? "home.restarts_above" : "home.restarts_below",
    { value: fmt(cool ? zone.target_c + h : zone.target_c - h) });
}

// "0.8° below target", "at target" - to the tenth the sensor reports.
function gapText(zone) {
  if (zone.target_c == null || zone.temperature_c == null) return "";
  const gap = Math.round((zone.temperature_c - zone.target_c) * 10) / 10;
  if (gap === 0) return tr("home.at_target");
  return tr(gap < 0 ? "home.gap_below" : "home.gap_above", { value: fmt(Math.abs(gap)) });
}

// ----------------------------------------------------------- the zones --

function zoneStatus(zone, climate) {
  const mode = climate.mode;
  if (mode === "cool" && !zone.cool_enabled) return { tone: "off", text: tr("home.st_excluded") };
  if (zone.sensor_failsafe_active) return { tone: "warn", text: tr("home.st_failsafe") };
  if (zone.sensor_manual && !zone.sensor_ok) return { tone: "warn", text: tr("home.st_manual_waiting") };
  // The board just came up: the sensor hasn't reported since - not a fault.
  if (zone.sensor_waiting) return { tone: "off", text: tr("home.st_first_report") };
  if (!zone.sensor_ok && zone.temperature_c == null) return { tone: "warn", text: tr("home.st_waiting") };
  if (!zone.sensor_ok) return { tone: "warn", text: tr("home.st_stale") };
  if (climate.away_until && !zone.override) return { tone: "off", text: tr("home.st_away") };
  if (zone.target_c == null) return { tone: "off", text: tr("home.st_off") };
  if (zone.valve_on) return { tone: "active", text: tr(mode === "cool" ? "home.st_cooling" : "home.st_heating"), active: true };
  const near = Math.abs(zone.temperature_c - zone.target_c) <= (climate.hysteresis_c || 0.5);
  return { tone: "ok", text: tr(near ? "home.st_reached" : "home.st_idle") };
}

function zoneId(name) {
  return "zone-" + name.replace(/[^a-z0-9]/gi, "-").toLowerCase();
}

function renderZones() {
  const climate = state.climate;
  const host = document.getElementById("zones");
  if (!climate || !climate.zones.length) {
    host.innerHTML = `<div class="tile tile-wide"><div class="tile-name">${esc(tr("home.no_zones"))}</div>
      <div class="tile-note">${esc(tr("home.no_zones_note"))}</div><a class="link-btn" href="/climate-admin">${esc(tr("home.admin_link"))}</a></div>`;
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
  const history = state.history[zone.name] || { temps: [], runs: [] };
  const now = boardNow();
  const change = nextChange(week, now);
  card.dataset.active = status.active ? "true" : "false";
  card.dataset.zone = zone.name;

  const temp = zone.temperature_c != null
    ? `<span class="now-value">${fmt(zone.temperature_c)}</span><span class="now-unit">°</span>`
    : `<span class="now-missing">--</span>`;
  const facts = [];
  const excluded = mode === "cool" && !zone.cool_enabled;
  if (zone.target_c != null && !excluded) {
    const gap = zone.sensor_ok ? gapText(zone) : "";
    facts.push(`<div class="fact">${esc(tr("home.target"))} <b>${fmtTarget(zone.target_c)}</b>${gap ? `<span class="trend">${esc(gap)}</span>` : ""}</div>`);
    const rule = climate.away_until && !zone.override ? "" : valveRule(zone, climate);
    if (rule) facts.push(`<div class="fact fact-rule">${esc(rule)}</div>`);
  }
  if (change && !zone.override && !climate.away_until && !excluded) {
    facts.push(`<div class="fact">${esc(tr("home.next_change", { when: whenText(change.at), target: fmtTarget(change.target) }))}</div>`);
  }
  if (zone.humidity_pct != null) {
    const dew = mode === "cool" && zone.dew_point_c != null ? ` · ${tr("home.dew_point", { value: fmt(zone.dew_point_c) })}` : "";
    facts.push(`<div class="fact">${esc(tr("home.humidity"))} <b>${fmt(zone.humidity_pct, 0)}%</b> · ${esc(humidityWord(zone.humidity_pct))}${esc(dew)}</div>`);
  }

  const foot = [];
  // A typed value isn't a reading: no "updated a moment ago" under it.
  const age = zone.sensor_manual ? "" : fmtAge(zone.sensor_seconds_since_update ?? zone.sensor_last_value_age_seconds);
  // A value the sensor hasn't confirmed since: the last one saved, and how old.
  if (age) foot.push(esc(tr(zone.sensor_ok ? "home.updated" : "home.last_saved", { age })));
  if (zone.battery_pct != null) foot.push(esc(tr("home.battery", { pct: Math.round(zone.battery_pct) })));
  const hoursToday = (state.onHours[zone.name] || {}).today;
  if (hoursToday) foot.push(esc(tr(mode === "cool" ? "home.ran_today_cool" : "home.ran_today", { time: fmtDuration(hoursToday) })));

  card.querySelector(".zone-live").innerHTML = `
    <div class="zone-head">
      <h2 class="zone-name">${esc(zone.name)}${zone.sensor_simulated ? ` <span class="sim">${esc(tr("home.simulated"))}</span>` : ""}${zone.sensor_manual ? ` <span class="sim">${esc(tr("home.manual"))}</span>` : ""}</h2>
      <span class="state" data-tone="${status.tone}">${esc(status.text)}</span>
    </div>
    <div class="zone-main">
      <div class="now">${temp}</div>
      <div class="zone-facts">${facts.join("")}</div>
    </div>
    <div class="ribbon">
      ${ribbonSvg(zone, week, history, mode)}
      <div class="ribbon-caption"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
      <div class="ribbon-readout">${legendHtml()}</div>
    </div>
    <div class="quick">${quickHtml(zone, climate, change)}</div>
    ${zone.sensor_manual ? manualHtml(zone) : ""}
    <div class="zone-foot">${foot.join(" · ")}</div>`;
  wireRibbon(card, zone, week, history, mode);
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
    await refreshClimate();
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

function ribbonScale(zone, week, history, mode) {
  const today = week ? week[DAYS[dayIndex(boardNow())]] : [];
  const values = [...history.temps.map(p => p.value), ...today.filter(v => v != null)];
  if (zone.temperature_c != null) values.push(zone.temperature_c);
  if (zone.override && zone.override.target != null) values.push(zone.override.target);
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
    let d = "";
    scale.today.forEach((v, h) => {
      if (v == null) return;
      const prev = h > 0 ? scale.today[h - 1] : null;
      d += `${prev == null ? "M" : "L"}${x(h)},${scale.y(v)} L${x(h + 1)},${scale.y(v)} `;
    });
    if (d) parts.push(`<path class="rb-target" d="${d}" vector-effect="non-scaling-stroke"/>`);
    if (zone.override && zone.override.target != null) {
      const until = boardDate(zone.override.until);
      const endHour = Math.min(24, (until - new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))) / 3600000);
      const y = scale.y(zone.override.target);
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
    <span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><rect x="1" y="2" width="16" height="4" rx="1" class="lg-run"/></svg>${esc(tr("home.lg_run"))}</span>`;
}

// Touch or hover the strip: what happened (or is planned) at that time.
function wireRibbon(card, zone, week, history, mode) {
  const svg = card.querySelector(".ribbon svg");
  const readout = card.querySelector(".ribbon-readout");
  const cursor = svg.querySelector(".rb-cursor");
  const start = dayStartEpoch();
  const today = week ? week[DAYS[dayIndex(boardNow())]] : null;
  const show = event => {
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
  const hide = () => {
    cursor.setAttribute("visibility", "hidden");
    readout.innerHTML = legendHtml();
  };
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
}

// -------------------------------------------------------- quick change --

function quickHtml(zone, climate, change) {
  const mode = climate.mode;
  if (mode === "cool" && !zone.cool_enabled) {
    return `<span class="tile-note">${esc(tr("home.excluded_note"))}</span>`;
  }
  const pending = state.pending[zone.name];
  if (pending) {
    const options = DURATIONS.map(h => `<button type="button" data-action="duration" data-hours="${h}" aria-pressed="${pending.hours === h}">${esc(fmtDuration(h))}</button>`);
    if (change) {
      options.push(`<button type="button" data-action="duration" data-hours="next" aria-pressed="${pending.hours === "next"}">${esc(tr("home.until_time", { time: hhmm(change.at) }))}</button>`);
    }
    const title = pending.target == null
      ? tr("home.pending_off")
      : tr("home.pending_target", { target: fmtTarget(pending.target) });
    return `
      <div class="pending">
        <div class="pending-title">${esc(title)}</div>
        <div class="stepper" ${pending.target == null ? "hidden" : ""}>
          <button type="button" data-action="step" data-delta="-0.5" aria-label="${esc(tr("home.colder"))}">−</button>
          <span class="stepper-value">${fmtTarget(pending.target)}</span>
          <button type="button" data-action="step" data-delta="0.5" aria-label="${esc(tr("home.warmer"))}">+</button>
        </div>
        <div class="segmented" role="group" aria-label="${esc(tr("home.for_how_long"))}">${options.join("")}</div>
        <div class="pending-actions">
          <button class="btn" type="button" data-action="cancel-pending">${esc(tr("home.cancel"))}</button>
          <button class="btn btn-primary" type="button" data-action="apply">${esc(tr("home.apply"))}</button>
        </div>
      </div>`;
  }
  if (zone.override) {
    const until = boardDate(zone.override.until);
    const text = zone.override.target == null
      ? tr("home.override_off", { until: untilText(until) })
      : tr("home.override_on", { target: fmtTarget(zone.override.target), until: untilText(until) });
    return `<div class="override-note"><span>${esc(text)}</span>
      <button class="btn" type="button" data-action="resume">${esc(tr("home.resume"))}</button></div>`;
  }
  const shown = zone.target_c != null ? zone.target_c : null;
  return `
    <div class="stepper">
      <button type="button" data-action="start" data-delta="-0.5" aria-label="${esc(tr("home.colder"))}">−</button>
      <span class="stepper-value">${fmtTarget(shown)}</span>
      <button type="button" data-action="start" data-delta="0.5" aria-label="${esc(tr("home.warmer"))}">+</button>
    </div>
    <span class="quick-spacer"></span>
    ${zone.target_c != null ? `<button class="btn" type="button" data-action="off">${esc(tr("home.turn_off"))}</button>` : ""}`;
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
  const pending = state.pending[zone.name];
  if (action === "start") {
    state.pending[zone.name] = { target: clampTarget(lastTarget(zone) + Number(button.dataset.delta)), hours: 2 };
  } else if (action === "off") {
    state.pending[zone.name] = { target: null, hours: 2 };
  } else if (action === "step" && pending) {
    pending.target = clampTarget(pending.target + Number(button.dataset.delta));
  } else if (action === "duration" && pending) {
    pending.hours = button.dataset.hours === "next" ? "next" : Number(button.dataset.hours);
  } else if (action === "cancel-pending") {
    delete state.pending[zone.name];
  } else if (action === "apply" && pending) {
    button.disabled = true;
    const body = pending.hours === "next" ? { target: pending.target, until: "next_change" } : { target: pending.target, hours: pending.hours };
    try {
      await api(`/api/climate/zones/${encodeURIComponent(zone.name)}/override`, body);
      delete state.pending[zone.name];
      await refreshClimate();
      return;
    } catch (e) {
      notify(errorText(e));
      button.disabled = false;
    }
  } else if (action === "resume") {
    button.disabled = true;
    try {
      await api(`/api/climate/zones/${encodeURIComponent(zone.name)}/override/cancel`, {});
      await refreshClimate();
      return;
    } catch (e) {
      notify(errorText(e));
      button.disabled = false;
    }
  } else {
    return;
  }
  fillZone(card, zone, state.climate);
}

// ---------------------------------------------------------------- hero --

function renderHero() {
  const climate = state.climate;
  if (!climate) return;
  const mode = climate.mode || "heat";
  document.body.dataset.season = mode;
  document.getElementById("hero-season").textContent = tr(mode === "cool" ? "home.season_cool" : "home.season_heat");
  const season = document.getElementById("season-chip");
  season.innerHTML = `${mode === "cool" ? ICONS.snow : ICONS.sun}<span>${esc(tr(mode === "cool" ? "home.summer" : "home.winter"))}</span>`;

  // The title says what the plant is doing as a whole, the line under it
  // where each zone stands - nothing a reading can't back up.
  const zones = climate.zones || [];
  const cool = mode === "cool";
  const running = zones.filter(z => !zoneExcluded(z));
  const active = running.filter(z => zoneStatus(z, climate).active);
  const off = running.filter(z => z.target_c == null);
  let title;
  if (!zones.length) title = tr("home.no_zones");
  else if (climate.away_until) title = tr("home.hero_away", { until: untilText(boardDate(climate.away_until)) });
  else if (active.length) title = tr(cool ? "home.hero_cooling" : "home.hero_heating", { zones: listNames(active.map(z => z.name)) });
  else if (running.length && off.length === running.length) title = tr(cool ? "home.hero_all_off_cool" : "home.hero_all_off_heat");
  else if (running.length && !off.length && running.every(z => zoneStatus(z, climate).tone === "ok")) title = tr("home.hero_all_ok");
  else title = tr(cool ? "home.hero_idle_cool" : "home.hero_idle_heat");
  const detail = climate.away_until ? tr("home.hero_away_detail") : zones.map(z => zoneLine(z, climate)).join(" · ");
  document.getElementById("hero-title").textContent = title;
  document.getElementById("hero-detail").textContent = detail;

  const away = document.getElementById("away-chip");
  away.setAttribute("aria-pressed", climate.away_until ? "true" : "false");
  away.innerHTML = `${ICONS.suitcase}<span>${esc(climate.away_until ? tr("home.away_on") : tr("home.away_off"))}</span>`;
}

// One zone's state in a few words, for the line under the title.
function zoneLine(zone, climate) {
  const temp = zone.temperature_c != null ? `${fmt(zone.temperature_c)}°` : "";
  if (zoneExcluded(zone)) return tr("home.line_excluded", { zone: zone.name });
  if (zone.sensor_waiting) return tr("home.line_first_report", { zone: zone.name });
  if (!zone.sensor_ok && zone.temperature_c == null) return tr("home.line_waiting", { zone: zone.name });
  if (zone.target_c == null) {
    const change = zone.override ? null : nextChange((state.weeks[zone.name] || {})[climate.mode], boardNow());
    const until = zone.override ? boardDate(zone.override.until) : change && change.at;
    return until ? tr("home.line_off_until", { zone: zone.name, until: untilText(until) }) : tr("home.line_off", { zone: zone.name });
  }
  if (zoneStatus(zone, climate).active) return tr("home.line_on", { zone: zone.name, temp, target: fmtTarget(zone.target_c) });
  // Resting: within the hysteresis of its target, or past it - 26° with a
  // 20° heating target isn't "at temperature", it's well above.
  const diff = zone.temperature_c != null ? zone.temperature_c - zone.target_c : 0;
  if (Math.abs(diff) <= (climate.hysteresis_c || 0.5)) return tr("home.line_ok", { zone: zone.name, temp });
  return tr(diff > 0 ? "home.line_above" : "home.line_below", { zone: zone.name, temp, target: fmtTarget(zone.target_c) });
}

function listNames(names) {
  if (names.length < 2) return names.join("");
  return `${names.slice(0, -1).join(", ")} ${tr("home.and")} ${names[names.length - 1]}`;
}

function renderOutdoor() {
  const chip = document.getElementById("outdoor-chip");
  const reading = state.readings["Outdoor Sensor"];
  if (!reading || reading.temperature_c == null) { chip.hidden = true; return; }
  chip.hidden = false;
  const rh = reading.humidity_pct != null ? ` · ${fmt(reading.humidity_pct, 0)}%` : "";
  chip.innerHTML = `${ICONS.leaf}<span>${esc(tr("home.outside"))} <b>${fmt(reading.temperature_c)}°</b>${esc(rh)}</span>`;
}

const ICONS = {
  sun: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`,
  snow: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v20M4 6l16 12M20 6L4 18M9 3l3 3 3-3M9 21l3-3 3 3"/></svg>`,
  suitcase: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>`,
  leaf: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20c0-9 6-15 16-16-1 10-7 16-16 16z"/><path d="M4 20l8-8"/></svg>`,
  moon: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z"/></svg>`,
  wind: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 8h10.5a3 3 0 1 0-3-3"/><path d="M3 12h15.5a3 3 0 1 1-3 3"/><path d="M3 16h8"/></svg>`,
  clock: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  power: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v8"/><path d="M6.6 6.6a7.5 7.5 0 1 0 10.8 0"/></svg>`,
  fan: `<svg viewBox="0 0 24 24" aria-hidden="true"><g class="fan-blades">${[0, 120, 240].map(a => `<path transform="rotate(${a} 12 12)" d="M12 12C9.6 9.6 9.4 4.9 11.6 3.3c2.4-1.7 5.6.4 4.5 3.6-.8 2.2-2.4 3.7-4.1 5.1z"/>`).join("")}<circle cx="12" cy="12" r="1.9"/></g></svg>`,
};

// ---------------------------------------------------------- away sheet --

function openAway() {
  const away = state.climate && state.climate.away_until;
  const options = AWAY_OPTIONS.map(h => `<button class="btn" type="button" data-away="${h}">${esc(tr(`home.away_${h}`))}</button>`).join("");
  openSheet(tr("home.away_title"), `
    <p class="sheet-note">${esc(tr("home.away_note"))}</p>
    ${away ? `<p><b>${esc(tr("home.hero_away", { until: untilText(boardDate(away)) }))}</b></p>` : ""}
    <div class="sheet-actions">${options}</div>
    ${away ? `<div class="sheet-actions"><button class="btn btn-primary" type="button" data-away="back">${esc(tr("home.away_back"))}</button></div>` : ""}`,
    async event => {
      const button = event.target.closest("button[data-away]");
      if (!button) return;
      button.disabled = true;
      try {
        if (button.dataset.away === "back") await api("/api/climate/away/cancel", {});
        else await api("/api/climate/away", { hours: Number(button.dataset.away) });
        closeSheet();
        await refreshClimate();
      } catch (e) {
        notify(errorText(e));
        button.disabled = false;
      }
    });
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
// Each card's week, right under its day: the classic page's own grid
// (static/schedule_grid.js) - painted with a drag, copied to the week or
// to another zone, saved as it's painted, only the hours that changed.
// Built once per season and left alone: the card's live part above it is
// refilled every refresh, the grid never is, so a stroke is never cut.

function fillPlan(card, zone) {
  const plan = card.querySelector(".zone-plan");
  const excluded = zoneExcluded(zone);
  plan.hidden = excluded;
  if (excluded || plan.dataset.mode === climateMode) return;
  plan.dataset.mode = climateMode;
  plan.innerHTML = schedulePaneHtml(zone, slug(zone.name));
  loadZoneSchedule(zone.name);
}

// --------------------------------------------------------- the plant --
// What the season runs besides the zones - ventilation, the bypass
// valve, the dehumidifier - with what each does in this season.

function deviceByName(name) {
  return (state.devices ? state.devices.sensors : []).find(d => d.name === name);
}

function renderPlant() {
  const climate = state.climate;
  const tiles = [];
  const vmcSpeed = (state.devices ? state.devices.sensors : []).find(d => d.kind === "number" && d.paired_switch && (d.presets || []).length);
  if (vmcSpeed) tiles.push(vmcTile(vmcSpeed));
  const rules = (climate && climate.season_effects && climate.season_effects.rules) || [];
  rules.forEach(rule => {
    const device = deviceByName(rule.switch);
    if (!device) return;
    const reading = state.readings[rule.switch] || {};
    const what = rule[climate.mode];
    const control = what === "manual" && device.controllable
      ? `<button class="switch" type="button" role="switch" data-switch="${esc(rule.switch)}" aria-checked="${!!reading.is_on}" aria-label="${esc(displayName(rule.switch))}"></button>`
      : `<span class="dot-state" data-on="${!!reading.is_on}">${esc(tr(reading.is_on ? "home.on" : "home.off_state"))}</span>`;
    tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${esc(displayName(rule.switch))}</span>${control}</div>
      <div class="tile-note">${esc(tr(`home.rule_${what}_${climate.mode}`))}</div></div>`);
  });
  document.getElementById("plant").hidden = !tiles.length;
  document.getElementById("plant-body").innerHTML = tiles.join("");
}

// ------------------------------------------------------------- the VMC --
// Its speeds, the maximum for a while, and one choice for the rest of
// the time: manual (the speed stays what you pick) or a ready-made
// program (services/vmc_schedule.py runs it as a week of hours).

const VMC_ICONS = { night: "moon", day: "sun", boost: "wind" };
const VMC_BOOST_MINUTES = [30, 60, 120];
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
// "custom" - a week set elsewhere (the classic page's own day and night).
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
  const current = speed.presets.find(p => p.value === value);
  const { program, running, boost } = vmcRunning(speed);
  // Each button names the device it sets: with light strips on the Home
  // tab there's more than one level paired with a switch. Off first, as
  // on a fan's dial; a speed switches it back on.
  const power = deviceByName(speed.paired_switch);
  const offButton = power && power.controllable
    ? `<button class="vmc-preset vmc-off" type="button" data-vmc-off data-device="${esc(speed.paired_switch)}" aria-pressed="${!on}">
      ${ICONS.power}<span class="vmc-preset-name">${esc(tr("home.vmc_off"))}</span><span class="vmc-preset-value">OFF</span></button>`
    : "";
  const buttons = offButton + speed.presets.map(p => `<button class="vmc-preset" type="button" data-vmc="${p.value}" data-device="${esc(speed.name)}" aria-pressed="${on && value === p.value}">
      ${ICONS[VMC_ICONS[p.key]] || ""}<span class="vmc-preset-name">${esc(tr(`vmc_presets.${p.key}`))}</span><span class="vmc-preset-value">${fmt(p.value, 0)}%</span></button>`).join("");

  const until = at => (at ? untilText(boardDate(at)) : "");
  // A timed speed is spelled out in its own row below: up here, just its name.
  const status = boost ? vmcLevelName(speed, boost.value)
    : !on ? (running ? (running.value === 0 ? tr("home.vmc_off_program") : tr("home.vmc_off_until", { until: running.until || "" }))
      : tr("home.vmc_off"))
    : current ? tr(`vmc_presets.${current.key}`)
    : value != null ? tr("home.vmc_custom") : "";
  // The maximum for a while, then back to the program (or to before).
  const top = Math.max(...speed.presets.map(p => p.value));
  const boostRow = boost
    ? `<div class="vmc-boost" data-active="true">${ICONS.wind}<span class="vmc-boost-text">${esc(tr("home.vmc_boost_running", { level: vmcLevelName(speed, boost.value), until: until(boost.until) }))}</span>
        <button class="chip" type="button" data-vmc-boost-cancel>${esc(tr("home.vmc_boost_cancel"))}</button></div>`
    : `<div class="vmc-boost">${ICONS.wind}<span class="vmc-boost-text">${esc(tr("home.vmc_boost_for", { level: vmcLevelName(speed, top) }))}</span>
        <span class="vmc-boost-times">${VMC_BOOST_MINUTES.map(m => `<button class="chip" type="button" data-vmc-boost="${m}" data-value="${top}" data-device="${esc(speed.name)}">${esc(m < 60 ? tr("home.vmc_minutes", { m }) : tr("home.vmc_hours", { h: m / 60 }))}</button>`).join("")}</span></div>`;

  // The rest of the time: one choice, manual or a program. A week set
  // elsewhere shows as "custom", only while it's the one running.
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
  const programs = program ? `<div class="vmc-plan">
      <div class="sched-season-label">${esc(tr("home.vmc_program"))}</div>
      <div class="vmc-programs" role="radiogroup" aria-label="${esc(tr("home.vmc_program"))}">
        ${choices.map(c => `<button class="vmc-template" type="button" role="radio" aria-checked="${c.key === choice}"
            data-vmc-choice="${c.key}" data-device="${esc(speed.name)}">
          <span class="vmc-template-name">${esc(tr(`home.vmc_tpl_${c.key}`))}</span>
          <span class="vmc-template-note">${esc(c.note)}</span></button>`).join("")}
      </div>
      <p class="vmc-program-note">${ICONS.clock}<span>${esc(programNote)}</span></p>
    </div>` : "";

  // The fan turns faster as the speed goes up; its phase comes from the
  // clock, so a refresh that redraws the card doesn't make it jump.
  const turn = on && value > 0 ? Math.max(0.8, 90 / value) : 0;
  const spin = turn ? ` style="--turn: ${turn.toFixed(2)}s; --phase: -${((Date.now() / 1000) % turn).toFixed(2)}s"` : "";
  return `<div class="tile tile-wide vmc" id="vmc-card" data-device="${esc(speed.name)}" data-on="${on}">
    <div class="vmc-live">
      <div class="vmc-head">
        <span class="vmc-fan" data-spin="${!!turn}"${spin}>${ICONS.fan}</span>
        <span class="vmc-title"><span class="tile-name">${esc(displayName(speed.paired_switch))}</span>
          <span class="tile-note">${esc(status)}</span></span>
        <span class="tile-value">${!on ? "" : value != null ? `${fmt(value, 0)}<small>%</small>` : "--"}</span>
      </div>
      <div class="vmc-presets" data-count="${speed.presets.length + (offButton ? 1 : 0)}" role="group" aria-label="${esc(tr("home.vmc_speed"))}">${buttons}</div>
      ${boostRow}
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
  target.disabled = true;
  try {
    const boosting = speed && vmcRunning(speed).boost;
    if (target.hasAttribute("data-vmc-off") && target.dataset.device) {
      // A speed picked by hand ends a timed one, so it isn't undone later.
      if (boosting) await api("/api/vmc-schedule/boost/cancel", {});
      await api(`/api/sensors/${encodeURIComponent(target.dataset.device)}/off`, {});
    } else if (target.dataset.vmcBoost && target.dataset.device) {
      await api("/api/vmc-schedule/boost", { device: target.dataset.device, value: Number(target.dataset.value),
                                             minutes: Number(target.dataset.vmcBoost) });
    } else if (target.hasAttribute("data-vmc-boost-cancel")) {
      await api("/api/vmc-schedule/boost/cancel", {});
    } else if (target.dataset.vmc && target.dataset.device) {
      if (boosting) await api("/api/vmc-schedule/boost/cancel", {});
      await api(`/api/sensors/${encodeURIComponent(target.dataset.device)}/set-value`, { value: Number(target.dataset.vmc) });
    } else if (target.dataset.vmcChoice === "manual") {
      // No program: the week stays saved, unused, for the classic page.
      await api("/api/vmc-schedule", { enabled: false });
    } else if (target.dataset.vmcChoice && speed) {
      // A program is its week, run on this VMC - picking one is what
      // switches it on and names the device it drives.
      const program = VMC_PROGRAMS.find(p => p.key === target.dataset.vmcChoice);
      await api("/api/vmc-schedule", { enabled: true, target: speed.name, week: vmcProgramWeek(program, speed) });
    } else if (target.dataset.switch) {
      const on = target.getAttribute("aria-checked") !== "true";
      await api(`/api/sensors/${encodeURIComponent(target.dataset.switch)}/${on ? "on" : "off"}`, {});
    } else {
      return;
    }
    await refreshDevices();
  } catch (e) {
    notify(errorText(e));
  } finally {
    target.disabled = false;
  }
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
    if (d.kind === "switch") {
      const level = numbersByPair[d.name];
      const slider = level ? rangeHtml(level, state.readings[level.name] || {}) : "";
      tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${name}</span>
        ${d.controllable && !d.hide_toggle ? `<button class="switch" type="button" role="switch" data-switch="${esc(d.name)}" aria-checked="${!!r.is_on}" aria-label="${name}"></button>`
          : `<span class="dot-state" data-on="${!!r.is_on}">${esc(tr(r.is_on ? "home.on" : "home.off_state"))}</span>`}</div>
        ${r.power_w != null ? `<div class="tile-note">${fmt(r.power_w, 0)} W</div>` : ""}${slider}${readingNote(r)}</div>`);
    } else if (d.kind === "number") {
      tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${name}</span></div>${rangeHtml(d, r)}${readingNote(r)}</div>`);
    } else if (d.kind === "power_meter") {
      tiles.push(`<div class="tile"><div class="tile-head"><span class="tile-name">${name}</span></div>
        <div class="tile-value">${r.power_w != null ? `${fmt(r.power_w, 0)}<small>W</small>` : "--"}</div>
        <div class="tile-note">${[r.voltage_v != null ? `${fmt(r.voltage_v, 0)} V` : "", r.current_a != null ? `${fmt(r.current_a, 2)} A` : ""].filter(Boolean).join(" · ")}</div>${readingNote(r)}</div>`);
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
  document.getElementById("house").hidden = !tiles.length;
  document.getElementById("house-body").innerHTML = tiles.join("");
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
  return `<div class="tile air" data-sensor="${esc(device.name)}">
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
  if (sensors.length) renderHouse();
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
  return reading.ok === false ? `<div class="tile-note">${esc(tr("home.not_answering"))}</div>` : "";
}

async function onHouseClick(event) {
  const button = event.target.closest("button");
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.switch) {
      const on = button.getAttribute("aria-checked") !== "true";
      await api(`/api/sensors/${encodeURIComponent(button.dataset.switch)}/${on ? "on" : "off"}`, {});
      await refreshDevices();
    } else if (button.dataset.cover) {
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
    await refreshDevices();
  } catch (e) {
    notify(errorText(e));
  }
}

// ------------------------------------------------------------ refreshes --

async function refreshClimate() {
  try {
    state.climate = await api("/api/climate/zones");
    climateZones = state.climate.zones || [];
    if (state.climate.mode && state.climate.mode !== climateMode) {
      climateMode = state.climate.mode;
      ensurePaintValueForMode();
    }
    state.failures = 0;
    document.getElementById("offline").hidden = true;
    const missing = state.climate.zones.filter(z => !state.weeks[z.name]);
    if (missing.length) await loadWeeks(missing.map(z => z.name));
  } catch (e) {
    state.failures += 1;
    if (state.failures >= 2) {
      const offline = document.getElementById("offline");
      offline.hidden = false;
      offline.textContent = tr("home.offline");
    }
    return;
  }
  renderHero();
  renderZones();
  renderPlant();
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
  renderHero();
  renderZones();
}

async function refreshDevices() {
  try {
    if (!state.devices) state.devices = await api("/api/devices");
    const names = state.devices.sensors.map(d => d.name);
    const readings = await Promise.all(names.map(n => api(`/api/sensors/${encodeURIComponent(n)}`).catch(() => ({ ok: false }))));
    names.forEach((n, i) => { state.readings[n] = readings[i]; });
    if (state.devices.sensors.some(d => d.kind === "number" && (d.presets || []).length)) {
      state.vmc = await api("/api/vmc-schedule").catch(() => null);
    }
  } catch (e) {
    return;
  }
  renderOutdoor();
  renderPlant();
  renderHouse();
}

async function refreshProblems() {
  const health = document.getElementById("health");
  const text = document.getElementById("health-text");
  try {
    const data = await api("/api/problems");
    const count = (data.problems || []).length;
    health.dataset.state = count ? "warn" : "ok";
    text.textContent = count ? tr("home.health_problems", { count }) : tr("home.health_ok");
  } catch (e) {
    health.dataset.state = "unknown";
  }
}

// ---------------------------------------------------------------- start --

document.getElementById("zones").addEventListener("click", onZoneClick);
document.getElementById("zones").addEventListener("submit", onManualSubmit);
document.getElementById("plant-body").addEventListener("click", onPlantClick);
document.getElementById("house-body").addEventListener("click", onHouseClick);
document.getElementById("house-body").addEventListener("change", onHouseChange);
document.getElementById("away-chip").addEventListener("click", openAway);
document.getElementById("outdoor-chip").addEventListener("click", () => {
  const plant = document.getElementById("plant");
  if (!plant.hidden) plant.scrollIntoView({ behavior: "smooth" });
});
document.getElementById("sheet").addEventListener("click", e => state.sheet && state.sheet.onClick && state.sheet.onClick(e));
document.getElementById("sheet-close").addEventListener("click", closeSheet);
document.getElementById("sheet-backdrop").addEventListener("click", closeSheet);
document.addEventListener("keydown", e => { if (e.key === "Escape" && state.sheet) closeSheet(); });

(async function start() {
  // Until the board's clock has answered, "now" would be this phone's.
  for (let i = 0; i < 30 && !(window.ServerClock && window.ServerClock.now()); i++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await refreshClimate();
  await Promise.all([refreshHistory(), refreshDevices(), refreshProblems()]);
  refreshAirHistory();
  setInterval(refreshAirHistory, HISTORY_MS);
  setInterval(refreshClimate, REFRESH_MS);
  setInterval(refreshDevices, REFRESH_MS);
  setInterval(refreshHistory, HISTORY_MS);
  setInterval(refreshProblems, PROBLEMS_MS);
  setInterval(updateNowHighlight, 60000);
})();
