/* The home page (templates/home.html), in parts - see core.js. */
// The rest of the house: switches and lights, air quality, energy meters.
"use strict";

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
  const covers = state.devices ? state.devices.covers : [];
  if (covers.length) tiles.push(coversTile(covers));
  // The wide ones (the shutters, a meter's days, the air's day) first, the
  // small ones after: two wide ones fill a row, and the small ones the gaps
  // (CSS dense) - a wide one after a small one left a hole beside it.
  const wide = html => /^<div class="tile (covers|energy|air)\b/.test(html);
  tiles.sort((a, b) => wide(b) - wide(a));
  document.getElementById("house").hidden = !tiles.length;
  morph(document.getElementById("house-body"), tiles.join(""));
}

// ---------------------------------------------------- the shutters --
// Only with shutters in config.yaml: all of them in one card - open, stop
// or close them all at once, the everyday use, and each one by itself
// below. The card doesn't ask the shutters where they are (the cheap
// curtain switches only know their last command): it says what was sent
// and which ones didn't answer.

function coverLabel(name, names) {
  // "Tapparella Cucina", "Tapparella Soggiorno": the word they all share
  // is the card's title already - each row keeps only the rest.
  const first = name.split(" ")[0];
  const shared = names.length > 1 && names.every(n => n.startsWith(`${first} `) && n.length > first.length + 1);
  return shared ? name.slice(first.length + 1) : displayName(name);
}

// Where a shutter is, in words: moving which way (and how far, once a full
// run has shown where it is), open, closed, part way, or not answering.
function coverState(cover) {
  if (!cover) return { text: "", tone: "" };
  if (cover.reached === false) return { text: tr("covers.state_unreached"), tone: "warn" };
  const pct = cover.percent;
  if (cover.moving) {
    const way = tr(cover.moving === "open" ? "covers.state_opening" : "covers.state_closing");
    return { text: pct == null ? way : `${way} · ${pct}%`, tone: "active" };
  }
  if (pct == null) return { text: tr("covers.state_unknown"), tone: "" };
  if (pct >= 100) return { text: tr("covers.state_open"), tone: "" };
  if (pct <= 0) return { text: tr("covers.state_closed"), tone: "" };
  return { text: tr("covers.state_at", { pct }), tone: "" };
}

function coversTile(names) {
  const button = (target, action, text, label) => `<button type="button" data-cover="${esc(target)}"
    data-cover-action="${action}"${label ? ` aria-label="${esc(label)}" title="${esc(label)}"` : ""}>${esc(text)}</button>`;
  const rows = names.map(name => {
    const label = coverLabel(name, names);
    const where = coverState(state.coverStates[name]);
    return `<li class="cover-row"><span class="cover-name"><span class="cover-label">${esc(label)}</span>${where.text
      ? `<small class="cover-state" data-tone="${where.tone}">${esc(where.text)}</small>` : ""}</span>
      <span class="cover-ctl">${button(name, "open", "▲", tr("covers.one_btn_open", { name: label }))}${button(name, "stop", "■", tr("covers.one_btn_stop", { name: label }))}${button(name, "close", "▼", tr("covers.one_btn_close", { name: label }))}</span></li>`;
  }).join("");
  const note = state.coverNote;
  return `<div class="tile covers">
    <div class="tile-head"><span class="tile-name">${esc(tr("covers.heading"))}</span></div>
    <div class="covers-all">
      ${button("all", "open", tr("covers.open_all"))}${button("all", "stop", "■", tr("covers.stop_all"))}${button("all", "close", tr("covers.close_all"))}
    </div>
    <ul class="cover-rows">${rows}</ul>
    ${note ? `<div class="tile-note cover-note" data-tone="${note.tone}" role="status">${esc(note.text)}</div>` : ""}
  </div>`;
}

// What a command did, said in the card: sent, or which ones didn't answer
// (and which are still being tried) - gone again after a while.
function coverOutcome(target, action, result) {
  const names = state.devices ? state.devices.covers : [];
  const label = n => coverLabel(n, names);
  const failed = (result.failed || []).map(f => label(f.name));
  const pending = (result.pending || []).map(label);
  const sent = target === "all" ? (result.sent || []).length + pending.length > 0 : !failed.length;
  const parts = [];
  if (sent) parts.push(target === "all" ? tr(`covers.all_${action}`) : tr(`covers.one_${action}`, { name: label(target) }));
  if (failed.length) {
    parts.push(target === "all" ? tr("covers.not_answered", { names: failed.join(", ") })
      : tr("covers.one_not_answered", { name: failed[0] }));
  }
  if (pending.length) parts.push(tr("covers.still_trying", { names: pending.join(", ") }));
  state.coverNote = { text: parts.join(" · "), tone: !sent ? "bad" : failed.length || pending.length ? "warn" : "ok" };
  clearTimeout(state.coverNoteTimer);
  state.coverNoteTimer = setTimeout(() => { state.coverNote = null; steady(renderHouse); }, 30000);
  steady(renderHouse);
}

async function sendCover(button) {
  const target = button.dataset.cover, action = button.dataset.coverAction;
  // "All": its three buttons wait together - a second tap would only send it again.
  const group = target === "all" ? [...document.querySelectorAll('#house-body button[data-cover="all"]')] : [button];
  group.forEach(b => busy(b, true));
  try {
    coverOutcome(target, action, await api(`/api/covers/${encodeURIComponent(target)}/action/${action}`, {}));
    refreshCovers();
  } catch (e) {
    // The shutter didn't answer (the board did): said in the card, by name.
    if (e.data && e.data.failed) coverOutcome(target, action, e.data);
    else if (target !== "all" && e.data && e.data.error) coverOutcome(target, action, { failed: [{ name: target }] });
    else notify(errorText(e));
  } finally {
    group.forEach(b => busy(b, false));
  }
}

// Where each shutter is, as the board follows it (services/cover_watch.py):
// read with the devices, and every second and a half while one is moving.
const COVER_MOVING_MS = 1500;

async function refreshCovers() {
  if (!(state.devices && state.devices.covers.length)) return;
  clearTimeout(state.coverTimer);
  try {
    const data = await api("/api/covers");
    state.coverStates = Object.fromEntries(data.covers.map(c => [c.name, c]));
  } catch (e) {
    return;
  }
  steady(renderHouse);
  if (Object.values(state.coverStates).some(c => c.moving)) state.coverTimer = setTimeout(refreshCovers, COVER_MOVING_MS);
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
// last 7 days (today the last), the month or the year by month as bars - tap one for
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
  // The 7 days: each its weekday from the span's first, today "today".
  const [y, m, d0] = span.from.split("-").map(Number);
  const weekday = i => labels[(new Date(Date.UTC(y, m - 1, d0 + i)).getUTCDay() + 6) % 7];
  return span.days.map((d, i) => {
    const iso = d ? d.day : null;
    const n = i + 1;
    const label = range === "week" ? (i === span.days.length - 1 ? tr("home.runs_today") : weekday(i))
      : (n === 1 || n % 5 === 0 ? String(n) : "");
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
  if (button.dataset.cover) await sendCover(button);
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
