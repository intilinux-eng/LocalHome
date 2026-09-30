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
  offsetMs: 0,            // board wall clock minus this device's clock
  utcOffsetMin: 0,
  climate: null,
  devices: null,
  readings: {},
  weeks: {},              // zone -> {heat: week, cool: week}
  history: {},            // zone -> {temps: [...], runs: [...]}
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

// The grid marks "now" by the board's clock (scheduleNow() reads this).
window.ServerClock = { now: () => boardNow() };

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
// whatever this phone's own time zone.

function boardNow() {
  return new Date(Date.now() + state.offsetMs);
}

function wall(epochMs) {
  return new Date(epochMs + state.utcOffsetMin * 60000);
}

// "2026-09-30T21:30:00" from the server is the board's own local time.
function boardDate(iso) {
  return /[zZ]|[+-]\d\d:\d\d$/.test(iso) ? wall(Date.parse(iso)) : new Date(Date.parse(iso + "Z"));
}

function wallToEpoch(wallDate) {
  return wallDate.getTime() - state.utcOffsetMin * 60000;
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

async function syncClock() {
  try {
    const t0 = Date.now();
    const data = await api("/api/server-time");
    const rtt = Date.now() - t0;
    state.utcOffsetMin = data.utc_offset_minutes || 0;
    state.offsetMs = data.epoch_ms + state.utcOffsetMin * 60000 + rtt / 2 - Date.now();
  } catch (e) { /* the refresh says so */ }
}

function tickClock() {
  document.getElementById("clock").textContent = hhmm(boardNow());
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
  const age = fmtAge(zone.sensor_seconds_since_update ?? zone.sensor_last_value_age_seconds);
  if (age) foot.push(esc(tr("home.updated", { age })));
  if (zone.battery_pct != null) foot.push(esc(tr("home.battery", { pct: Math.round(zone.battery_pct) })));
  const hoursToday = (state.onHours[zone.name] || {}).today;
  if (hoursToday) foot.push(esc(tr(mode === "cool" ? "home.ran_today_cool" : "home.ran_today", { time: fmtDuration(hoursToday) })));

  card.querySelector(".zone-live").innerHTML = `
    <div class="zone-head">
      <h2 class="zone-name">${esc(zone.name)}${zone.sensor_simulated ? ` <span class="sim">${esc(tr("home.simulated"))}</span>` : ""}</h2>
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
    <div class="zone-foot">${foot.join(" · ")}</div>`;
  wireRibbon(card, zone, week, history, mode);
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
    const nowTs = Date.now() / 1000 + state.offsetMs / 1000 - state.utcOffsetMin * 60;
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

  const zones = climate.zones || [];
  const active = zones.filter(z => zoneStatus(z, climate).active);
  const waiting = zones.filter(z => zoneStatus(z, climate).tone === "warn");
  let title, detail = "";
  if (climate.away_until) {
    title = tr("home.hero_away", { until: untilText(boardDate(climate.away_until)) });
    detail = tr("home.hero_away_detail");
  } else if (active.length) {
    title = tr(mode === "cool" ? "home.hero_cooling" : "home.hero_heating", { zones: listNames(active.map(z => z.name)) });
    detail = active.filter(z => z.temperature_c != null && z.target_c != null)
      .map(z => tr("home.hero_zone", { zone: z.name, temp: `${fmt(z.temperature_c)}°`, target: fmtTarget(z.target_c) }))
      .join(" · ");
  } else if (zones.length) {
    title = tr("home.hero_calm");
    const next = zones.map(z => ({ zone: z, change: nextChange((state.weeks[z.name] || {})[mode], boardNow()) }))
      .filter(n => n.change).sort((a, b) => a.change.at - b.change.at)[0];
    if (next) detail = tr("home.hero_next", { zone: next.zone.name, target: fmtTarget(next.change.target), when: whenText(next.change.at) });
  } else {
    title = tr("home.no_zones");
  }
  if (waiting.length) detail = `${detail} ${tr("home.hero_waiting", { zones: listNames(waiting.map(z => z.name)) })}`.trim();
  document.getElementById("hero-title").textContent = title;
  document.getElementById("hero-detail").textContent = detail;

  const away = document.getElementById("away-chip");
  away.setAttribute("aria-pressed", climate.away_until ? "true" : "false");
  away.innerHTML = `${ICONS.suitcase}<span>${esc(climate.away_until ? tr("home.away_on") : tr("home.away_off"))}</span>`;
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

function vmcTile(speed) {
  const reading = state.readings[speed.name] || {};
  const value = reading.value;
  const current = (speed.presets || []).find(p => p.value === value);
  const buttons = speed.presets.map(p => `<button type="button" data-vmc="${p.value}" aria-pressed="${value === p.value}">${esc(tr(`vmc_presets.${p.key}`))}</button>`).join("");
  const program = state.vmc && state.vmc.schedule;
  const programNote = program && program.enabled
    ? tr("home.vmc_program_on", { day: program.day.at, night: program.night.at })
    : tr("home.vmc_program_off");
  return `<div class="tile tile-wide">
    <div class="tile-head"><span class="tile-name">${esc(displayName(speed.paired_switch))}</span>
      <span class="tile-value">${value != null ? `${fmt(value, 0)}<small>%</small>` : "--"}</span></div>
    <div class="segmented" role="group" aria-label="${esc(tr("home.vmc_speed"))}">${buttons}</div>
    <div class="tile-head"><span class="tile-note">${esc(current ? tr("home.vmc_preset", { preset: tr(`vmc_presets.${current.key}`) }) : "")} ${esc(programNote)}</span>
      ${program ? `<button class="switch" type="button" role="switch" data-vmc-program aria-checked="${!!program.enabled}" aria-label="${esc(tr("home.vmc_program"))}"></button>` : ""}</div>
  </div>`;
}

async function onPlantClick(event) {
  const target = event.target.closest("button");
  if (!target) return;
  target.disabled = true;
  try {
    if (target.dataset.vmc) {
      const speed = (state.devices.sensors).find(d => d.kind === "number" && d.paired_switch);
      await api(`/api/sensors/${encodeURIComponent(speed.name)}/set-value`, { value: Number(target.dataset.vmc) });
    } else if (target.hasAttribute("data-vmc-program")) {
      const schedule = Object.assign({}, state.vmc.schedule, { enabled: !state.vmc.schedule.enabled });
      await api("/api/vmc-schedule", schedule);
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

function rangeHtml(device, reading) {
  const range = device.range || { min: 0, max: 100, unit: "%" };
  const value = reading.value != null ? reading.value : range.min;
  return `<label class="tile-note">${esc(tr("home.level"))} <b>${fmt(value, 0)}${esc(range.unit || "")}</b>
    <input type="range" min="${range.min}" max="${range.max}" value="${value}" data-level="${esc(device.name)}"></label>`;
}

function readingNote(reading) {
  if (reading.ok === false && reading.error) return `<div class="tile-note">${esc(tr("home.not_answering"))}</div>`;
  return "";
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
  await syncClock();
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(syncClock, 5 * 60000);
  await refreshClimate();
  await Promise.all([refreshHistory(), refreshDevices(), refreshProblems()]);
  setInterval(refreshClimate, REFRESH_MS);
  setInterval(refreshDevices, REFRESH_MS);
  setInterval(refreshHistory, HISTORY_MS);
  setInterval(refreshProblems, PROBLEMS_MS);
  setInterval(updateNowHighlight, 60000);
})();
