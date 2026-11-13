/* The compact window's page. It reads the board's API and tells the program
   around it (window.chrome.webview) what to say in the tray: how things are,
   and the notices - a zone turning on, something to check, the board out of
   reach. It uses nothing of the board but its API, so the board needs no
   change for it; the requests it may make are listed in MainForm.cs. */
"use strict";

const params = new URLSearchParams(location.search);
const LANG = TEXTS[params.get("lang")] ? params.get("lang") : "it";
const BOARD = params.get("board") || "";
const NAME = params.get("name") || "LocalHome";
// The program puts Windows 11's Mica behind the page when it can: the page
// is then see-through, its cards a light veil on it.
document.documentElement.dataset.backdrop = params.get("backdrop") === "mica" ? "mica" : "";
const SHOWN_MS = 20000;            // on screen: as the home page reads it
const HIDDEN_MS = 60000;           // in the tray: enough for the notices
const FAILING_MS = 15000;          // the board not answering: tried this often
const COVER_MOVING_MS = 1500;      // a shutter moving: followed closely
const DOWN_NOTICE_MS = 3 * 60000;  // a short gap (a restart) isn't worth a notice
const REPEAT_PROBLEM_MS = 30 * 60000;  // a problem coming and going says so once
const READ_MS = 12000;
const COMMAND_MS = 16000;
const KEEP_KEY = `localhome.desktop.${BOARD}`;   // another board starts from nothing
// A zone's temperature for a while: half a degree a tap, what the board takes,
// and where a zone with no target now starts from.
const TARGET = { min: 5, max: 35, step: 0.5, start: { heat: 21, cool: 25 } };
const HOLD_HOURS = [2, 4, 8, 24];
const AWAY_HOURS = [24, 72, 168];

const now = {
  climate: null, devices: null, readings: {}, vmc: null, covers: {}, problems: [],
  clock: null, clockAt: 0, lastGood: null, failingSince: null, acting: 0,
  pending: {},       // zone -> the temperature being chosen, nothing sent yet
  awayOpen: false,   // "away" asked: how long, nothing sent yet
};
// What the notices compare with. Kept across a reload of the page, gone when
// the program closes: the program starting says again what's still wrong.
const seen = { valves: {}, problems: {}, owedFixed: false, owedBack: false };
try { Object.assign(seen, JSON.parse(sessionStorage.getItem(KEEP_KEY) || "{}")); } catch (e) { /* as new */ }

const $ = id => document.getElementById(id);

function tr(key, vars) {
  let text = TEXTS[LANG][key];
  if (text == null) text = key;
  Object.entries(vars || {}).forEach(([k, v]) => { text = text.split(`{${k}}`).join(v); });
  return text;
}

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmt(value, digits = 1) {
  return Number(value).toLocaleString(LANG, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtTarget(value) {
  return `${Number(value).toLocaleString(LANG, { maximumFractionDigits: 1 })}°`;
}

function tell(message) {
  if (window.chrome && window.chrome.webview) window.chrome.webview.postMessage(message);
}

// Redrawn only when it changes: what's under the pointer stays itself.
function put(el, html) {
  if (el.dataset.drawn !== html) {
    el.innerHTML = html;
    el.dataset.drawn = html;
  }
}

async function api(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), body === undefined ? READ_MS : COMMAND_MS);
  const options = body === undefined ? { cache: "no-store", signal: controller.signal }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: controller.signal };
  try {
    const res = await fetch(path, options);
    const data = await res.json().catch(() => ({ ok: false }));
    if (!res.ok || data.ok === false) {
      const error = new Error(tr("refused"));
      error.data = data;
      error.unreachable = !!data.unreachable;
      throw error;
    }
    return data;
  } catch (e) {
    if (e.data) throw e;
    const error = new Error(tr("no_answer"));
    error.unreachable = true;
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

// The board's own clock: a timed speed ends by it, whatever this PC's says.
function boardWallMs() {
  if (!now.clock) return Date.now() - new Date().getTimezoneOffset() * 60000;
  return now.clock.epoch_ms + (now.clock.utc_offset_minutes || 0) * 60000 + (Date.now() - now.clockAt);
}

function wallMs(iso) { return Date.parse(`${iso.slice(0, 16)}Z`); }

function timeNow() { return new Date(boardWallMs()).toISOString().slice(11, 16); }

// ----------------------------------------------------------- the comfort --
// As on the home page (comfort.js, branch feature/comfort-indicator): the
// temperature against what the program wants now, the humidity in its band.
// With no program there's no temperature to miss: only a room really cold or
// hot for the season is said, 22° with the heating off in winter isn't.
const COMFORT_RH = { dry: 30, humid: 60, veryHumid: 70 };
const COMFORT_BAND = { heat: { lo: -1, hi: 1.5, far: 2.5 }, cool: { lo: -2, hi: 1, far: 2.5 } };
const COMFORT_FREE = { heat: { base: 21, lo: -3, hi: 3, far: 2.5 }, cool: { base: 25, lo: -3, hi: 3, far: 2.5 } };
const DIAL = { size: 240, inner: 34, outer: 102 };

function comfortOf(zone, climate) {
  if (zone.temperature_c == null || !zone.sensor_ok) return null;
  const mode = climate.mode === "cool" ? "cool" : "heat";
  const hasTarget = zone.target_c != null && !climate.away_until;
  const band = hasTarget ? COMFORT_BAND[mode] : COMFORT_FREE[mode];
  const base = hasTarget ? zone.target_c : band.base;
  const gap = Math.round((zone.temperature_c - base) * 10) / 10;
  let temp = "ok";
  if (gap < band.lo) temp = gap < band.lo - band.far + 1 ? "cold" : "cool";
  else if (gap > band.hi) temp = gap > band.hi + band.far - 1 ? "hot" : "warm";
  const rh = zone.humidity_pct;
  let air = null;
  if (rh != null) air = rh < COMFORT_RH.dry ? "dry" : rh > COMFORT_RH.veryHumid ? "very_humid" : rh > COMFORT_RH.humid ? "humid" : "ok";
  // A bathroom: humid air after a shower is what it's for - said, never alarmed.
  const showery = zone.cool_enabled === false && (air === "humid" || air === "very_humid");
  let level = "ok";
  if (temp !== "ok" || (air && air !== "ok")) level = "mild";
  if (temp === "cold" || temp === "hot" || (air === "very_humid" && !showery)) level = "warn";
  if (showery && temp === "ok") level = "mild";
  return { gap, temp, air, rh, level, band };
}

function comfortWord(c) {
  const parts = [];
  if (c.temp !== "ok") parts.push(tr(`t_${c.temp}`));
  if (c.air && c.air !== "ok") parts.push(tr(`rh_${c.air}`));
  if (!parts.length) return tr("c_ok");
  const said = parts.length === 2 ? tr("c_and", { a: parts[0], b: parts[1] }) : parts[0];
  return said.charAt(0).toUpperCase() + said.slice(1);
}

function comfortDot(c) {
  const mid = (c.band.lo + c.band.hi) / 2, half = (c.band.hi - c.band.lo) / 2;
  const rhMid = (COMFORT_RH.dry + COMFORT_RH.humid) / 2, rhHalf = (COMFORT_RH.humid - COMFORT_RH.dry) / 2;
  let u = c.rh == null ? 0 : (c.rh - rhMid) / rhHalf;
  let v = (c.gap - mid) / half;
  const reach = DIAL.outer / DIAL.inner, length = Math.hypot(u, v);
  if (length > reach) { u *= reach / length; v *= reach / length; }
  const k = 18 / DIAL.size, centre = DIAL.size / 2;
  return { x: (centre + u * DIAL.inner) * k, y: (centre - v * DIAL.inner) * k };
}

// ------------------------------------------------------------ the drawing --

function zoneAdjustable(zone, climate) {
  return zone.sensor_configured !== false && zone.valve_configured !== false && !climate.away_until
    && !(climate.mode === "cool" && zone.cool_enabled === false);
}

// "until 18:00", "until tomorrow at 07:00", "until Monday at 07:00".
function untilWords(iso) {
  if (!iso) return tr("until_changed");
  const time = iso.slice(11, 16);
  const day = ms => Math.floor(ms / 86400000);
  const days = day(wallMs(iso)) - day(boardWallMs());
  if (days <= 0) return tr("until_at", { time });
  if (days === 1) return tr("until_tomorrow", { time });
  const weekday = new Date(wallMs(iso)).toLocaleDateString(LANG, { weekday: "long", timeZone: "UTC" });
  return tr("until_day", { day: weekday, time });
}

// Windows' own icons (Segoe Fluent Icons), by their code.
// Each icon by its Segoe Fluent Icons code, drawn from the page's own SVG
// sprite (index.html): the same on Windows and Linux.
function icon(code, extra = "") {
  const id = code.charCodeAt(0).toString(16).toUpperCase();
  return `<svg class="ic${extra ? ` ${extra}` : ""}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
}

function stepper(value) {
  const button = (delta, sign, label) => `<button class="step" type="button" data-step="${delta}" aria-label="${esc(tr(label))}" title="${esc(tr(label))}">${sign}</button>`;
  return `<div class="adjust">${button(-TARGET.step, icon("\uE738"), "colder")}<span>${value}</span>${button(TARGET.step, icon("\uE710"), "warmer")}</div>`;
}

// Below a zone's readings: its target, half a degree either way. A tap sends
// nothing: only the time picked after it does, so a slip changes nothing.
function adjustHtml(zone, climate) {
  const said = zone.target_c != null ? tr("target", { target: fmtTarget(zone.target_c) }) : tr("zone_off");
  if (!zoneAdjustable(zone, climate)) {
    const excluded = climate.mode === "cool" && zone.cool_enabled === false;
    return `<p class="zone-target">${esc(excluded ? tr("zone_excluded") : said)}</p>`;
  }
  const pending = now.pending[zone.name];
  if (pending != null) {
    return `${stepper(`<b>${fmtTarget(pending)}</b>`)}
      <div class="row"><span>${esc(tr("hold_for"))}</span><span class="chips">
        ${HOLD_HOURS.map(h => `<button class="chip" type="button" data-hold="${h}">${esc(tr("hours", { h }))}</button>`).join("")}
        <button class="chip" type="button" data-pending-cancel>${esc(tr("pending_cancel"))}</button></span></div>`;
  }
  const held = zone.override ? `<div class="row held"><span>${esc(untilWords(zone.override.until))}</span>
      <button class="chip" type="button" data-resume>${esc(tr("resume"))}</button></div>` : "";
  return `${stepper(esc(said))}${held}`;
}

function zoneHtml(zone, climate) {
  const pill = zone.valve_on === true ? `<span class="pill">${esc(tr(climate.mode === "cool" ? "pill_cool" : "pill_heat"))}</span>`
    : zone.sensor_simulated ? `<small>${esc(tr("simulated"))}</small>` : "";
  let inside;
  if (zone.sensor_configured === false || zone.valve_configured === false) {
    inside = `<p class="zone-off">${esc(tr("to_setup"))}</p>`;
  } else if (zone.temperature_c == null || !zone.sensor_ok) {
    inside = `<p class="zone-off">${esc(tr("sensor_down"))}</p>`;
  } else {
    const c = comfortOf(zone, climate);
    const p = c && comfortDot(c);
    inside = `<div class="zone-values"><span class="zone-t">${fmt(zone.temperature_c)}°</span>${zone.humidity_pct != null ? `<span class="zone-rh">${fmt(zone.humidity_pct, 0)}%</span>` : ""}${trendHtml(zone.name)}</div>
      ${sparkSvg(zone.name)}
      ${c ? `<div class="comfort" data-level="${c.level}"><svg viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="7.6"/><circle class="dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.6"/></svg><span>${esc(comfortWord(c))}</span></div>` : ""}`;
  }
  const adjust = zone.sensor_configured === false || zone.valve_configured === false ? "" : adjustHtml(zone, climate);
  return `<div class="zone" data-zone="${esc(zone.name)}"${now.pending[zone.name] != null ? ' data-pending="true"' : ""}${zone.valve_on === true ? ' data-on="true"' : ""}>
    <a class="zone-main" href="#" data-detail="zone" data-name="${esc(zone.name)}" title="${esc(tr("ch_open"))}"><div class="zone-head"><b>${esc(zone.name)}</b>${pill}</div>${inside}</a>${adjust}</div>`;
}

// Away, as the home page says it: every zone off until the way back. The VMC
// follows the choice last made on the home page, said here before it's picked.
function awayHtml(climate) {
  if (climate.away_until) {
    return `<div class="row"><span><b>${esc(tr("away_title"))}</b><small>${esc(untilWords(climate.away_until))}</small></span>
      <button class="chip" type="button" data-away-back>${esc(tr("away_back"))}</button></div>`;
  }
  if (!now.awayOpen) return "";
  const choice = now.vmc && now.vmc.schedule && now.vmc.schedule.away_choice;
  const speed = vmcSpeed();
  const preset = speed && speed.presets.find(p => p.key === choice);
  const vmc = !speed ? "" : choice === "off" ? tr("away_vmc_off") : preset ? tr("away_vmc_level", { level: tr(`vmc_${preset.key}`) }) : "";
  return `<div class="card-head"><b>${esc(tr("away_title"))}</b></div>
    <p class="say-more">${esc(tr("away_what"))}${vmc ? ` ${esc(vmc)}` : ""}</p>
    <div class="row"><span>${esc(tr("hold_for"))}</span><span class="chips">
      ${AWAY_HOURS.map(h => `<button class="chip" type="button" data-away-hours="${h}">${esc(tr(`away_${h}`))}</button>`).join("")}
      <button class="chip" type="button" data-away-cancel>${esc(tr("pending_cancel"))}</button></span></div>`;
}

// In its season: on for a while or until the air is dry enough; with the air
// dry enough it can't be switched on, and says so.
function dehumHtml(d) {
  const on = d.on === true;
  const dry = !on && d.humidity != null && d.humidity < d.start_from;
  const stop = Math.round(d.stop_below), start = Math.round(d.start_from);
  const until = d.until_at ? untilWords(d.until_at) : "";
  const word = on ? (d.mode === "target" || d.mode === "auto" ? tr("dehum_on_target", { stop, until }) : until ? tr("dehum_on_until", { until }) : tr("on"))
    : dry ? tr("dehum_dry", { rh: fmt(d.humidity, 0), start }) : tr("off");
  let controls = "";
  if (on) controls = `<span></span><button class="chip" type="button" data-dehum-off>${esc(tr("dehum_off"))}</button>`;
  else if (!dry) {
    controls = `<span>${esc(tr("hold_for"))}</span><span class="chips">
      ${(d.durations || []).map(h => `<button class="chip" type="button" data-dehum-hours="${h}">${esc(tr("hours", { h }))}</button>`).join("")}
      ${d.until_target ? `<button class="chip" type="button" data-dehum-target>${esc(tr("dehum_until_target", { stop }))}</button>` : ""}</span>`;
  }
  return `<div class="card-head"><b>${esc(tr("dehum"))}</b>${d.humidity != null && !dry ? `<span>${fmt(d.humidity, 0)}%</span>` : ""}</div>
    <p class="say-more">${esc(word)}</p>${controls ? `<div class="row">${controls}</div>` : ""}`;
}

function factsHtml() {
  const out = [];
  const outdoor = now.readings[OUTDOOR];
  if (outdoor && outdoor.temperature_c != null) {
    out.push(`<button class="fact" type="button" data-detail="outdoor" title="${esc(tr("ch_open"))}"><small>${esc(tr("outside"))}</small><b>${fmt(outdoor.temperature_c)}°</b>${outdoor.humidity_pct != null ? `<span>${fmt(outdoor.humidity_pct, 0)}%</span>` : ""}</button>`);
  }
  const meter = now.devices && now.devices.sensors.find(d => d.kind === "power_meter");
  const power = meter && now.readings[meter.name];
  if (power && power.power_w != null) {
    const budget = meter.power_budget;
    const level = !budget ? "ok" : power.power_w >= budget.trip_risk_w ? "bad" : power.power_w >= budget.available_power_w ? "warn" : "ok";
    out.push(`<button class="fact" type="button" data-level="${level}" data-detail="power" data-name="${esc(meter.name)}" title="${esc(tr("ch_open"))}"><small>${esc(tr("power"))}</small><b>${fmt(power.power_w, 0)} W</b></button>`);
  }
  return out.join("");
}

function vmcSpeed() {
  return now.devices && now.devices.sensors.find(d => d.kind === "number" && d.paired_switch && (d.presets || []).length);
}

function vmcLevel(speed, value) {
  const preset = speed.presets.find(p => p.value === value);
  return preset ? tr(`vmc_${preset.key}`) : `${fmt(value, 0)}%`;
}

function vmcHtml(speed) {
  const reading = now.readings[speed.name] || {};
  const value = reading.value != null ? Math.round(reading.value) : null;
  const on = (now.readings[speed.paired_switch] || {}).is_on !== false;
  const status = (now.vmc && now.vmc.status) || {};
  const program = now.vmc && now.vmc.schedule;
  const running = program && program.enabled && program.target === speed.name && status.enabled ? status : null;
  const boost = status.override && status.override.device === speed.name ? status.override : null;
  const held = status.away && status.away.device === speed.name;
  const source = reading.ok === false ? "unreachable" : held ? "away" : !on ? "off" : boost ? "timed" : status.auto ? "auto"
    : running && (value == null || running.value === value) ? "program" : running ? "hand" : "manual";
  const tone = source === "unreachable" ? "warn" : "";
  const shown = on && value != null ? `<small>${fmt(value, 0)}% · ${esc(vmcLevel(speed, value))}</small>` : "";
  const head = `<div class="card-head"><b>${esc(tr("vmc"))}${shown}</b><span data-tone="${tone}">${esc(tr(`src_${source}`))}</span></div>`;
  if (source === "away") return head;
  if (source === "off") {
    return `${head}<div class="row"><span></span><button class="chip" type="button" data-power="${esc(speed.paired_switch)}">${esc(tr("turn_on"))}</button></div>`;
  }
  const dead = source === "unreachable" ? " disabled" : "";
  const levels = speed.presets.map(p => `<button type="button" data-level-value="${p.value}" aria-pressed="${p.value === value}"${dead}>${esc(tr(`vmc_${p.key}`))}</button>`).join("");
  let timed;
  if (boost) {
    const left = Math.max(1, Math.round((wallMs(boost.until) - boardWallMs()) / 60000));
    const leftText = left < 60 ? tr("minutes", { m: left }) : tr("hours", { h: fmt(left / 60, left % 60 ? 1 : 0) });
    const more = left + 30 <= 24 * 60 ? `<button class="chip" type="button" data-boost-more="${left + 30}" data-boost-value="${boost.value}">${esc(tr("boost_more", { m: 30 }))}</button>` : "";
    timed = `<div class="row"><span>${esc(tr("boost_running", { level: vmcLevel(speed, boost.value), until: boost.until.slice(11, 16) }))}<small>${esc(tr("boost_left", { left: leftText }))}</small></span>
      <span class="chips">${more}<button class="chip" type="button" data-boost-cancel>${esc(tr("boost_cancel"))}</button></span></div>`;
  } else {
    const top = Math.max(...speed.presets.map(p => p.value));
    timed = `<div class="row"><span>${esc(tr("boost_for", { level: vmcLevel(speed, top) }))}</span><span class="chips">
      ${[30, 60, 120].map(m => `<button class="chip" type="button" data-boost="${m}" data-boost-value="${top}"${dead}>${esc(m < 60 ? tr("minutes", { m }) : tr("hours", { h: m / 60 }))}</button>`).join("")}</span></div>`;
  }
  return `${head}<div class="levels" role="group" aria-label="${esc(tr("levels"))}">${levels}</div>${timed}`;
}

const ARROWS = { open: icon("\uE70E"), stop: icon("\uEE95"), close: icon("\uE70D") };

// "Tapparella Cucina", "Tapparella Soggiorno": the word they share goes.
function coverLabel(name, names) {
  const first = name.split(" ")[0];
  return names.length > 1 && names.every(n => n.startsWith(`${first} `)) ? name.slice(first.length + 1) : name;
}

function coverWhere(cover) {
  if (!cover) return { text: "", tone: "", shut: null, going: null };
  const pct = cover.percent;
  const shut = pct == null ? null : 100 - pct;
  if (cover.reached === false) return { text: tr("c_unreached"), tone: "warn", shut, going: null };
  if (cover.moving) {
    const way = tr(cover.moving === "open" ? "c_opening" : "c_closing");
    return { text: pct == null ? way : `${way} · ${pct}%`, tone: "active", shut, going: cover.moving };
  }
  if (pct == null) return { text: tr("c_unknown"), tone: "", shut, going: null };
  if (pct >= 100) return { text: tr("c_open"), tone: "", shut: 0, going: null };
  if (pct <= 0) return { text: tr("c_closed"), tone: "", shut: 100, going: null };
  return { text: tr("c_at", { pct }), tone: "", shut, going: null };
}

function coversHtml(names) {
  const all = names.map(n => now.covers[n]);
  let sum = "";
  if (all.some(c => c && c.moving)) sum = tr("c_sum_moving");
  else if (all.every(c => c && c.reached !== false && c.percent != null)) {
    if (all.every(c => c.percent >= 100)) sum = tr("c_sum_open");
    else if (all.every(c => c.percent <= 0)) sum = tr("c_sum_closed");
  }
  const button = (target, action, inside, label, extra = "") => `<button type="button" data-cover="${esc(target)}" data-action="${action}"
    aria-label="${esc(label)}" title="${esc(label)}"${extra}>${inside}</button>`;
  const rows = names.map(name => {
    const label = coverLabel(name, names);
    const where = coverWhere(now.covers[name]);
    const going = action => (where.going === action ? ' data-going="true"' : "");
    return `<li><span class="pic" aria-hidden="true"${where.shut == null ? "" : ` style="--shut:${where.shut}%"`}></span>
      <span class="name">${esc(label)}${where.text ? `<small data-tone="${where.tone}">${esc(where.text)}</small>` : ""}</span>
      <span class="ctl">${button(name, "open", ARROWS.open, tr("open_one", { name: label }), going("open"))}${button(name, "stop", ARROWS.stop, tr("stop_one", { name: label }))}${button(name, "close", ARROWS.close, tr("close_one", { name: label }), going("close"))}</span></li>`;
  }).join("");
  return `<div class="card-head"><b>${esc(tr("covers"))}</b><span>${esc(sum)}</span></div>
    <div class="all">${button("all", "open", `${ARROWS.open}${esc(tr("open_all"))}`, tr("open_all"))}${button("all", "stop", ARROWS.stop, tr("stop_all"))}${button("all", "close", `${ARROWS.close}${esc(tr("close_all"))}`, tr("close_all"))}</div>
    <ul class="rows">${rows}</ul>`;
}

function drawOther() {
  const speed = vmcSpeed();
  $("vmc").hidden = !speed;
  if (speed) put($("vmc"), vmcHtml(speed));
  const names = now.devices ? now.devices.covers : [];
  $("covers").hidden = !names.length;
  if (names.length) put($("covers"), coversHtml(names));
  put($("facts"), factsHtml());
  markSelected();
}

function drawClimate() {
  const climate = now.climate;
  document.body.dataset.season = climate.mode === "cool" ? "cool" : "heat";
  put($("zones"), climate.zones.map(z => zoneHtml(z, climate)).join(""));
  $("away-chip").hidden = !!climate.away_until || now.awayOpen;
  const away = awayHtml(climate);
  $("away").hidden = !away;
  put($("away"), away);
  const dehum = climate.dehumidifier;
  const showDehum = !!dehum && climate.mode === "cool" && !climate.away_until;
  $("dehum").hidden = !showDehum;
  if (showDehum) put($("dehum"), dehumHtml(dehum));
  const season = tr(climate.mode === "cool" ? "summer" : "winter");
  const on = climate.zones.filter(z => z.valve_on === true).map(z => z.name);
  const kind = climate.mode === "cool" ? "cool" : "heat";
  const what = climate.away_until ? tr("away") : on.length ? tr(`running_${kind}`, { zones: on.join(", ") }) : tr(`idle_${kind}`);
  $("say").textContent = `${season} · ${what}`;
  markSelected();
}

// -------------------------------------------------- what's wrong, in words --

function subjectName(subject) {
  const zones = ((now.climate && now.climate.zones) || []).map(z => z.name);
  for (const [suffix, key] of [[" Sensor", "subject_sensor"], [" Valve", "subject_valve"]]) {
    if (subject && subject.endsWith(suffix) && zones.includes(subject.slice(0, -suffix.length))) {
      return tr(key, { zone: subject.slice(0, -suffix.length) });
    }
  }
  return subject || "";
}

function problemText(problem) {
  const said = TEXTS[LANG].problems[problem.code];
  const subject = subjectName(problem.subject);
  return said ? said.split("{subject}").join(subject) : `${problem.code}${subject ? `: ${subject}` : ""}`;
}

// A device that doesn't answer is one line: its lateness and its reports
// missed follow from it. Late with nothing worse, it's said by itself.
function residentProblems(problems) {
  const has = (subject, codes) => problems.some(p => p.subject === subject && codes.includes(p.code));
  return problems.filter(p => {
    if (p.code === "device_report_late") return !has(p.subject, ["device_offline", "integration_failed"]);
    if (p.code === "device_reports_missed") return !has(p.subject, ["device_offline", "integration_failed", "device_report_late"]);
    return true;
  });
}

function setAlerts(lines, level) {
  const box = $("alerts");
  box.hidden = !lines.length;
  box.dataset.level = level;
  put(box, lines.map(l => `<li>${esc(l)}</li>`).join(""));
}

function setHealth(level, text) {
  $("health").dataset.level = level;
  $("health-text").textContent = text;
}

// ----------------------------------------------------------------- notices --

function keepSeen() {
  try { sessionStorage.setItem(KEEP_KEY, JSON.stringify(seen)); } catch (e) { /* only forgets */ }
}

function noticeZones(climate) {
  const turnedOn = [];
  climate.zones.forEach(zone => {
    if (typeof zone.valve_on !== "boolean") return;
    if (seen.valves[zone.name] === false && zone.valve_on) turnedOn.push(zone);
    seen.valves[zone.name] = zone.valve_on;
  });
  if (!turnedOn.length) return;
  const lines = turnedOn.map(z => (z.temperature_c == null ? z.name
    : tr(z.target_c != null ? "notify_zone_target" : "notify_zone", { zone: z.name, temp: fmt(z.temperature_c), target: z.target_c != null ? fmtTarget(z.target_c) : "" })));
  tell({ type: "notify", kind: "zone", level: "info", text: lines.join("\n"),
         title: tr(climate.mode === "cool" ? "notify_on_cool" : "notify_on_heat", { zones: turnedOn.map(z => z.name).join(", ") }) });
}

function noticeProblems(problems) {
  const at = Date.now();
  const keyOf = p => `${p.code}|${p.subject}`;
  const fresh = problems.filter(p => !seen.problems[keyOf(p)] || at - seen.problems[keyOf(p)] > REPEAT_PROBLEM_MS);
  problems.forEach(p => { seen.problems[keyOf(p)] = at; });
  if (fresh.length) {
    seen.owedFixed = true;
    tell({ type: "notify", kind: "problem", level: "warn", text: fresh.map(problemText).join("\n"),
           title: fresh.length === 1 ? tr("notify_check", { name: NAME }) : tr("notify_check_many", { name: NAME, count: fresh.length }) });
  } else if (!problems.length && seen.owedFixed) {
    seen.owedFixed = false;
    tell({ type: "notify", kind: "fixed", level: "info", title: tr("notify_fixed", { name: NAME }), text: tr("notify_fixed_text") });
  }
}

function tip(climate, level) {
  if (level === "off") return `${NAME}: ${tr("unreachable_chip").toLowerCase()}`;
  const zones = climate.zones.filter(z => z.temperature_c != null).map(z => `${z.name} ${fmt(z.temperature_c)}°`);
  const outside = now.readings[OUTDOOR];
  if (outside && outside.temperature_c != null) zones.push(`${tr("outside")} ${fmt(outside.temperature_c)}°`);
  return zones.length ? zones.join(" · ") : NAME;
}

// What the tray's icon may show, as a number: a zone's temperature, outside's.
function figures(climate) {
  const out = climate.zones.filter(z => z.temperature_c != null && z.sensor_ok)
    .map(z => ({ key: `zone:${z.name}`, label: z.name, value: z.temperature_c }));
  const outside = now.readings[OUTDOOR];
  if (outside && outside.temperature_c != null) out.push({ key: "outdoor", label: tr("outside"), value: outside.temperature_c });
  return out;
}

// ---------------------------------------------------------------- reading --

let timer = null;

function next(ms) {
  clearTimeout(timer);
  timer = setTimeout(refresh, ms);
}

async function refresh() {
  clearTimeout(timer);
  const shown = !document.hidden;
  try {
    const [climate, problemsAnswer, clock, readings] = await Promise.all([api("/api/climate/zones"), api("/api/problems"),
      api("/api/server-time"), api("/api/readings")]);
    now.readings = readings.readings || {};
    now.clock = clock;
    now.clockAt = Date.now();
    now.climate = climate;
    now.problems = residentProblems(problemsAnswer.problems || []);
    if (shown) await readTheRest();
    noticeZones(climate);
    noticeProblems(now.problems);
    if (now.failingSince && seen.owedBack) {
      tell({ type: "notify", kind: "back", level: "info", title: tr("notify_back", { name: NAME }), text: tr("notify_back_text") });
      seen.owedBack = false;
    }
    now.failingSince = null;
    now.lastGood = timeNow();
    keepSeen();
    const level = now.problems.length ? "warn" : "ok";
    if (!now.acting) {
      drawClimate();
      if (shown) drawOther();
    }
    $("body").classList.remove("is-old");
    $("plant").classList.remove("is-old");
    setHealth(level, level === "ok" ? tr("all_good") : tr("to_check", { count: now.problems.length }));
    setAlerts(now.problems.map(problemText), "warn");
    $("updated").textContent = tr("updated", { time: now.lastGood });
    tell({ type: "status", level: climate.zones.some(z => z.valve_on === true) && level === "ok" ? "busy" : level, tip: tip(climate, level),
           figures: figures(climate) });
    if (shown && !now.acting) {
      detailAfterRefresh();
      loadSparks();
      if (plantShown()) {
        drawPlant();
        loadPlant(false);
      }
    }
    next(shown ? SHOWN_MS : HIDDEN_MS);
  } catch (e) {
    boardGone();
    next(FAILING_MS);
  }
}

async function readTheRest() {
  const [devices, vmc] = await Promise.all([api("/api/devices"), api("/api/vmc-schedule").catch(() => null)]);
  now.devices = devices;
  now.vmc = vmc;
  if (devices.covers && devices.covers.length) await readCovers();
}

async function readCovers() {
  const answer = await api("/api/covers");
  now.covers = Object.fromEntries((answer.covers || []).map(c => [c.name, c]));
  followCovers();
}

// While a shutter moves it's followed closely; otherwise with the rest.
let coverTimer = null;
function followCovers() {
  clearTimeout(coverTimer);
  if (document.hidden || !Object.values(now.covers).some(c => c.moving)) return;
  coverTimer = setTimeout(async () => {
    try {
      await readCovers();
      if (!now.acting) drawOther();
    } catch (e) { /* the next reading says */ }
  }, COVER_MOVING_MS);
}

function boardGone() {
  const at = Date.now();
  if (!now.failingSince) now.failingSince = at;
  if (at - now.failingSince >= DOWN_NOTICE_MS && !seen.owedBack) {
    seen.owedBack = true;
    keepSeen();
    tell({ type: "notify", kind: "down", level: "off", title: tr("notify_down", { name: NAME }), text: tr("notify_down_text", { board: BOARD }) });
  }
  // Old values are never passed off as current: faded, and said.
  $("body").classList.add("is-old");
  $("plant").classList.add("is-old");
  $("say").textContent = tr("unknown_now");
  setHealth("off", tr("unreachable_chip"));
  setAlerts([now.lastGood ? tr("unreachable_since", { board: BOARD, time: now.lastGood }) : tr("unreachable", { board: BOARD })], "off");
  tell({ type: "status", level: "off", tip: tip(now.climate, "off") });
}

// --------------------------------------------------------------- commands --

let noteTimer = null;
function note(text, tone) {
  const box = $("note");
  box.textContent = text;
  box.dataset.tone = tone;
  box.hidden = false;
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { box.hidden = true; }, tone === "bad" ? 12000 : 6000);
}

async function command(button, work) {
  const group = button.dataset.cover === "all" ? [...document.querySelectorAll('button[data-cover="all"]')] : [button];
  group.forEach(b => { b.disabled = true; b.setAttribute("aria-busy", "true"); });
  now.acting++;
  try {
    await work();
  } catch (e) {
    note(e.message || tr("refused"), "bad");
  } finally {
    now.acting--;
    group.forEach(b => { b.disabled = false; b.removeAttribute("aria-busy"); });
    // What's drawn may hold the waiting button: drawn again from the answer.
    ["vmc", "covers", "zones", "away", "dehum"].forEach(id => { delete $(id).dataset.drawn; });
  }
  await refresh();
}

function onVmc(button) {
  const speed = vmcSpeed();
  if (!speed) return;
  const status = (now.vmc && now.vmc.status) || {};
  const boosting = status.override && status.override.device === speed.name;
  const device = encodeURIComponent(speed.name);
  if (button.dataset.levelValue) {
    if (button.getAttribute("aria-pressed") === "true") return;
    // A speed picked by hand ends a timed one, so it isn't undone later.
    command(button, async () => {
      if (boosting) await api("/api/vmc-schedule/boost/cancel", {});
      await api(`/api/sensors/${device}/set-value`, { value: Number(button.dataset.levelValue) });
    });
  } else if (button.dataset.boost || button.dataset.boostMore) {
    command(button, () => api("/api/vmc-schedule/boost", { device: speed.name, value: Number(button.dataset.boostValue),
                                                            minutes: Number(button.dataset.boost || button.dataset.boostMore) }));
  } else if (button.hasAttribute("data-boost-cancel")) {
    command(button, () => api("/api/vmc-schedule/boost/cancel", {}));
  } else if (button.dataset.power) {
    command(button, () => api(`/api/sensors/${encodeURIComponent(button.dataset.power)}/on`, {}));
  }
}

function onZone(button) {
  const name = button.closest(".zone").dataset.zone;
  const zone = now.climate.zones.find(z => z.name === name);
  if (!zone) return;
  const path = `/api/climate/zones/${encodeURIComponent(name)}/override`;
  if (button.dataset.step) {
    const delta = Number(button.dataset.step);
    const clamp = v => Math.max(TARGET.min, Math.min(TARGET.max, Math.round(v / TARGET.step) * TARGET.step));
    const current = now.pending[name] != null ? now.pending[name] : zone.target_c;
    // A zone off starts from a usual temperature, whichever button was pressed.
    now.pending[name] = current == null ? TARGET.start[now.climate.mode === "cool" ? "cool" : "heat"] : clamp(current + delta);
    drawClimate();
  } else if (button.hasAttribute("data-pending-cancel")) {
    delete now.pending[name];
    drawClimate();
  } else if (button.dataset.hold) {
    command(button, async () => {
      await api(path, { target: now.pending[name], hours: Number(button.dataset.hold) });
      delete now.pending[name];
    });
  } else if (button.hasAttribute("data-resume")) {
    command(button, () => api(`${path}/cancel`, {}));
  }
}

function onAway(button) {
  if (button.id === "away-chip") {
    now.awayOpen = true;
    drawClimate();
  } else if (button.hasAttribute("data-away-cancel")) {
    now.awayOpen = false;
    drawClimate();
  } else if (button.dataset.awayHours) {
    command(button, async () => {
      await api("/api/climate/away", { hours: Number(button.dataset.awayHours) });
      now.awayOpen = false;
    });
  } else if (button.hasAttribute("data-away-back")) {
    command(button, () => api("/api/climate/away/cancel", {}));
  }
}

function onDehum(button) {
  if (button.hasAttribute("data-dehum-off")) command(button, () => api("/api/dehumidifier/off", {}));
  else if (button.hasAttribute("data-dehum-target")) command(button, () => api("/api/dehumidifier/on", { until: "target" }));
  else if (button.dataset.dehumHours) command(button, () => api("/api/dehumidifier/on", { hours: Number(button.dataset.dehumHours) }));
}

function onCover(button) {
  const target = button.dataset.cover, action = button.dataset.action;
  const names = now.devices ? now.devices.covers : [];
  const said = list => list.map(n => coverLabel(n.name || n, names)).join(", ");
  command(button, async () => {
    let result;
    try {
      result = await api(`/api/covers/${encodeURIComponent(target)}/action/${action}`, {});
    } catch (e) {
      // The shutter didn't answer (the board did): by name.
      if (e.data && e.data.failed) result = e.data;
      else if (target !== "all" && e.data && e.data.error) result = { failed: [{ name: target }] };
      else throw e;
    }
    const failed = result.failed || [], pending = result.pending || [];
    if (failed.length) note(tr("c_failed", { names: said(failed) }), "bad");
    else if (pending.length) note(tr("c_pending", { names: said(pending) }), "");
  });
}

// ------------------------------------------------------------------ tabs --
// The house to live in it, the plant to check it. The choice is kept for the
// next start; a page with nothing to keep starts on the house.
const TAB_KEY = "localhome.desktop.tab";

function setTab(tab, focus) {
  const plantTab = tab === "plant";
  document.body.dataset.tab = plantTab ? "plant" : "house";
  $("plant").hidden = !plantTab;
  $("house-view").hidden = plantTab;
  ["house", "plant"].forEach(t => {
    const button = $(`tab-${t}`);
    button.setAttribute("aria-selected", String(t === document.body.dataset.tab));
    button.tabIndex = t === document.body.dataset.tab ? 0 : -1;
  });
  if (focus) $(`tab-${document.body.dataset.tab}`).focus();
  try { localStorage.setItem(TAB_KEY, document.body.dataset.tab); } catch (e) { /* only forgets */ }
  hideTip();
  window.scrollTo(0, 0);
  if (plantTab && now.climate) {
    drawPlant();
    loadPlant(false);
  }
}

document.addEventListener("keydown", event => {
  if (event.ctrlKey && (event.key === "1" || event.key === "2")) {
    event.preventDefault();
    setTab(event.key === "1" ? "house" : "plant", true);
  } else if (event.target.closest && event.target.closest("#tabs") && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
    event.preventDefault();
    setTab(plantShown() ? "house" : "plant", true);
  }
});

document.addEventListener("click", event => {
  // Only the tab bar's buttons: the body carries data-tab too.
  const tabButton = event.target.closest("#tabs [role='tab']");
  if (tabButton) {
    setTab(tabButton.dataset.tab);
    return;
  }
  const opener = event.target.closest("[data-open]");
  if (opener) {
    event.preventDefault();
    tell({ type: "open", path: opener.dataset.open });
    return;
  }
  const charts = event.target.closest("[data-detail]");
  if (charts) {
    event.preventDefault();
    openDetail(charts.dataset.detail, charts.dataset.name);
    return;
  }
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  if (button.id === "settings") tell({ type: "settings" });
  else if (button.id === "size") tell({ type: "size", wide: !WIDE.matches });
  else if (button.hasAttribute("data-back")) closeDetail();
  else if (button.dataset.range) setRange(button.dataset.range);
  else if (button.hasAttribute("data-detail-outside")) {
    detail.outside = !detail.outside;
    drawDetail();
  }
  else if (button.closest("#zones")) onZone(button);
  else if (button.closest("#away") || button.id === "away-chip") onAway(button);
  else if (button.closest("#dehum")) onDehum(button);
  else if (button.closest("#vmc")) onVmc(button);
  else if (button.dataset.cover) onCover(button);
});

// Brought back from the tray: the state now, not the last read - and nothing
// half chosen from before.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    now.pending = {};
    now.awayOpen = false;
  } else refresh();
});

document.documentElement.lang = LANG;
$("say").textContent = tr("wait");
$("health-text").textContent = tr("wait");
document.title = NAME;
$("brand").textContent = NAME;
$("brand").title = NAME;
$("open-full").textContent = tr("open_full", { name: NAME });
// The settings and the window's size are the program's: with no program
// around it, no buttons.
$("settings").hidden = !(window.chrome && window.chrome.webview);
$("size").hidden = $("settings").hidden;
sizeButton();
wireDetail();
wirePlant();
document.body.dataset.view = "house";
$("tab-house-text").textContent = tr("tab_house");
$("tab-plant-text").textContent = tr("tab_plant");
$("tabs").setAttribute("aria-label", tr("tabs"));
let firstTab = "house";
try { firstTab = localStorage.getItem(TAB_KEY) === "plant" ? "plant" : "house"; } catch (e) { /* the house */ }
setTab(firstTab);
$("settings").title = tr("settings");
$("settings").setAttribute("aria-label", tr("settings"));
$("away-chip").textContent = tr("away_title");
refresh();
