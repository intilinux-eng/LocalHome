/* The window's charts: a zone, outside or the house's power over 6 hours,
   24 hours or 7 days, and their days - opened from a zone or a tile. Only
   what the board already keeps, read with its own API (a sensor's history,
   the zones' hours and lows and highs, the outdoor days, the energy), drawn
   here in plain SVG with nothing loaded from elsewhere. In a narrow window
   they take its place, with a way back; in a wide one they sit beside the
   house. Loaded before app.js: only definitions, used once it runs. */
"use strict";

const OUTDOOR = "Outdoor Sensor";
// What a range asks the board for: its length, and the step its history
// answers in (services/history.py RANGE_BUCKETS).
const RANGES = { "6h": { s: 6 * 3600, bucket: 60 }, "24h": { s: 86400, bucket: 300 }, "7d": { s: 7 * 86400, bucket: 3600 } };
const WRITE_S = 300;                  // the board keeps a value every 5 minutes
const DETAIL_STALE_MS = 5 * 60000;    // so the charts are read again no sooner
const WIDE = window.matchMedia("(min-width: 660px)");

const detail = {
  subject: null,     // { kind: "zone" | "outdoor" | "power", name }
  range: "24h",
  outside: false,    // a zone's temperature with outside's beside it
  data: null, key: "", at: 0, ticket: 0, error: null, loading: false, drawn: "", width: 0,
};
// Each chart drawn: how it maps time and value, for the pointer.
const geometry = {};
let houseScroll = 0;

// --------------------------------------------------------------- time --

function boardNowS() {
  return now.clock ? (now.clock.epoch_ms + Date.now() - now.clockAt) / 1000 : Date.now() / 1000;
}

function offsetS() {
  return now.clock ? (now.clock.utc_offset_minutes || 0) * 60 : -new Date().getTimezoneOffset() * 60;
}

// On the board's wall clock, whatever this PC's says.
function wallDate(ts) { return new Date((ts + offsetS()) * 1000); }
function hhmm(ts) { return wallDate(ts).toISOString().slice(11, 16); }
function dayWords(date, options) { return date.toLocaleDateString(LANG, Object.assign({ timeZone: "UTC" }, options)); }
function isoDay(iso, options) { return dayWords(new Date(`${iso}T12:00:00Z`), options); }
function boardToday() { return wallDate(boardNowS()).toISOString().slice(0, 10); }

function timeLabel(ts) {
  return detail.range === "7d" ? `${dayWords(wallDate(ts), { weekday: "short", day: "numeric" })}, ${hhmm(ts)}` : hhmm(ts);
}

// The last `count` days to today, oldest first, as ISO dates.
function lastDays(count) {
  const today = Date.parse(`${boardToday()}T00:00:00Z`);
  return Array.from({ length: count }, (_, i) => new Date(today - (count - 1 - i) * 86400000).toISOString().slice(0, 10));
}

// ----------------------------------------------------------- subjects --

function outdoorShown() {
  const reading = now.readings[OUTDOOR];
  return !!(reading && reading.temperature_c != null) || !!(now.devices && now.devices.sensors.some(d => d.name === OUTDOOR));
}

function powerMeter() {
  return now.devices && now.devices.sensors.find(d => d.kind === "power_meter");
}

function subjectKey(s) { return s ? `${s.kind}:${s.name || ""}` : ""; }

function subjectThere(s) {
  if (!s || !now.climate) return false;
  if (s.kind === "zone") {
    const zone = now.climate.zones.find(z => z.name === s.name);
    return !!zone && zone.sensor_configured !== false;
  }
  if (s.kind === "outdoor") return outdoorShown();
  const meter = powerMeter();
  return s.kind === "power" && !!meter && meter.name === s.name;
}

function firstSubject() {
  const zone = now.climate && now.climate.zones.find(z => z.sensor_configured !== false);
  return zone ? { kind: "zone", name: zone.name } : outdoorShown() ? { kind: "outdoor" } : null;
}

function isSelected(kind, name) {
  return !!detail.subject && detail.subject.kind === kind && (kind === "outdoor" || detail.subject.name === name);
}

// What's open is marked in the house: kept on whatever was drawn last.
function markSelected() {
  document.querySelectorAll("[data-detail]").forEach(el => {
    const on = isSelected(el.dataset.detail, el.dataset.name);
    const card = el.closest(".zone") || el;
    if (on) card.dataset.selected = "true";
    else delete card.dataset.selected;
  });
}

function openDetail(kind, name) {
  if (!WIDE.matches && !detail.subject) houseScroll = window.scrollY;
  setSubject({ kind, name });
  if (!WIDE.matches) window.scrollTo(0, 0);
}

function setSubject(s) {
  const changed = subjectKey(s) !== subjectKey(detail.subject);
  detail.subject = s;
  document.body.dataset.view = s ? "detail" : "house";
  if (changed) {
    Object.assign(detail, { data: null, key: "", error: null, drawn: "" });
    // Another subject comes in from the side, also in a wide window.
    const box = $("detail");
    box.classList.remove("enter");
    void box.offsetWidth;
    box.classList.add("enter");
  }
  markSelected();
  drawDetail();
  if (s) loadDetail(changed);
}

function closeDetail() {
  if (WIDE.matches || !detail.subject) return;
  setSubject(null);
  window.scrollTo(0, houseScroll);
}

function setRange(range) {
  if (!RANGES[range] || range === detail.range) return;
  detail.range = range;
  loadDetail(true);
}

// After each reading of the board: the subject still there (a wide window
// always has one), its head current, its history read again when due.
function detailAfterRefresh() {
  if (detail.subject && !subjectThere(detail.subject)) setSubject(WIDE.matches ? firstSubject() : null);
  else if (WIDE.matches && !detail.subject) setSubject(firstSubject());
  else markSelected();
  if (!detail.subject) return;
  drawDetail();
  loadDetail(false);
}

function onWidth() {
  // Narrowed: the house first. Widened: something beside it.
  if (!WIDE.matches && detail.subject) setSubject(null);
  else if (WIDE.matches && !detail.subject && now.climate) setSubject(firstSubject());
  sizeButton();
}

function sizeButton() {
  const button = $("size");
  if (!button) return;
  const word = tr(WIDE.matches ? "narrow" : "widen");
  button.title = word;
  button.setAttribute("aria-label", word);
  button.dataset.wide = WIDE.matches ? "true" : "false";
}

// ------------------------------------------------------------- reading --

const clean = list => (list || []).filter(p => p && p.value != null).sort((a, b) => a.ts - b.ts);

function history(sensor, fields, range) {
  const asked = fields.map(f => `field=${encodeURIComponent(f)}`).join("&");
  return api(`/api/sensors/${encodeURIComponent(sensor)}/history?${asked}&range=${range}`).then(a => a.series || {});
}

async function readDetail(s, range) {
  if (s.kind === "zone") {
    const zone = now.climate.zones.find(z => z.name === s.name);
    const valve = zone.valve && zone.valve_configured !== false;
    const [air, runs, outside, hours, temps] = await Promise.all([
      history(zone.sensor, ["temperature_c", "humidity_pct"], range),
      valve ? history(zone.valve, ["is_on"], range).catch(() => ({})) : {},
      outdoorShown() ? history(OUTDOOR, ["temperature_c"], range).catch(() => ({})) : {},
      api("/api/climate/run-hours").catch(() => null),
      api("/api/climate/temps").catch(() => null)]);
    return { temp: clean(air.temperature_c), rh: clean(air.humidity_pct), runs: valve ? clean(runs.is_on) : null,
             outside: clean(outside.temperature_c), hours, temps };
  }
  if (s.kind === "outdoor") {
    const [air, days] = await Promise.all([history(OUTDOOR, ["temperature_c", "humidity_pct"], range),
      api("/api/climate/outdoor").catch(() => null)]);
    return { temp: clean(air.temperature_c), rh: clean(air.humidity_pct), days: (days && days.days) || [] };
  }
  const [power, energy] = await Promise.all([history(s.name, ["power_w"], range),
    api(`/api/energy/${encodeURIComponent(s.name)}`).catch(() => null)]);
  return { power: clean(power.power_w), energy };
}

async function loadDetail(force) {
  const s = detail.subject;
  if (!s || document.hidden || !now.climate) return;
  const key = `${subjectKey(s)}|${detail.range}`;
  if (!force && detail.key === key && Date.now() - detail.at < DETAIL_STALE_MS) return;
  if (detail.loading && !force) return;
  const ticket = ++detail.ticket;
  detail.loading = true;
  drawDetail();
  try {
    const data = await readDetail(s, detail.range);
    if (ticket !== detail.ticket) return;
    Object.assign(detail, { data, key, at: Date.now(), error: null, loading: false, drawn: "" });
  } catch (e) {
    if (ticket !== detail.ticket) return;
    Object.assign(detail, { error: e.message || tr("no_answer"), loading: false, drawn: "" });
  }
  drawDetail();
}

// ------------------------------------------------------------- drawing --

function drawDetail() {
  const s = detail.subject;
  const box = $("detail");
  if (!s) {
    put($("detail-head"), "");
    return;
  }
  put($("detail-head"), detailHeadHtml(s));
  put($("detail-range"), `<div class="levels" role="group" aria-label="${esc(tr("ch_range"))}">${Object.keys(RANGES).map(r =>
    `<button type="button" data-range="${r}" aria-pressed="${r === detail.range}">${esc(tr(`r_${r}`))}</button>`).join("")}</div>`);
  box.classList.toggle("is-loading", detail.loading);
  const body = $("detail-body");
  const width = Math.floor(body.clientWidth);
  if (!width) return;
  const signature = `${detail.key}|${detail.at}|${width}|${detail.outside}|${detail.error}|${!!detail.data}`;
  if (detail.drawn === signature) return;
  detail.drawn = signature;
  detail.width = width;
  hideTip();
  Object.keys(geometry).forEach(k => { delete geometry[k]; });
  if (!detail.data) {
    // While the history comes: grey cards of the charts' size, not an empty pane.
    body.innerHTML = detail.error ? `<p class="detail-note">${esc(tr("ch_failed", { why: detail.error }))}</p>`
      : `<p class="sr-only" role="status">${esc(tr("ch_loading"))}</p>${[180, 96, 150].map(h => `<section class="chart-card" aria-hidden="true">
          <div class="skel skel-title"></div><div class="skel" style="height:${h}px"></div></section>`).join("")}`;
    return;
  }
  const cards = s.kind === "zone" ? zoneCharts(s, detail.data) : s.kind === "outdoor" ? outdoorCharts(detail.data) : powerCharts(s, detail.data);
  body.innerHTML = cards.map(c => `<section class="chart-card">
      <div class="chart-head"><b>${esc(c.title)}</b>${c.side || ""}</div>
      <div class="chart" data-chart="${c.id}"></div>${c.foot || ""}</section>`).join("")
    + (detail.error ? `<p class="detail-note">${esc(tr("ch_failed", { why: detail.error }))}</p>` : "");
  cards.forEach(c => {
    const el = body.querySelector(`[data-chart="${c.id}"]`);
    el.innerHTML = c.draw(Math.max(200, el.clientWidth));
  });
}

function detailHeadHtml(s) {
  const back = `<button class="back" type="button" data-back title="${esc(tr("back"))}" aria-label="${esc(tr("back"))}">${icon("\uE72B")}</button>`;
  let name, values = "", more = "";
  if (s.kind === "zone") {
    const zone = now.climate.zones.find(z => z.name === s.name) || { name: s.name };
    name = zone.name;
    if (zone.temperature_c != null && zone.sensor_ok) {
      values = `${fmt(zone.temperature_c)}°${zone.humidity_pct != null ? ` · ${fmt(zone.humidity_pct, 0)}%` : ""}`;
      const c = comfortOf(zone, now.climate);
      if (c) more = `<span class="comfort" data-level="${c.level}">${esc(comfortWord(c))}</span>`;
    }
    if (zone.valve_on === true) more += `<span class="pill">${esc(tr(now.climate.mode === "cool" ? "pill_cool" : "pill_heat"))}</span>`;
    if (zone.target_c != null) more += `<small>${esc(tr("target", { target: fmtTarget(zone.target_c) }))}</small>`;
  } else if (s.kind === "outdoor") {
    const r = now.readings[OUTDOOR] || {};
    name = tr("outside");
    if (r.temperature_c != null) values = `${fmt(r.temperature_c)}°${r.humidity_pct != null ? ` · ${fmt(r.humidity_pct, 0)}%` : ""}`;
  } else {
    const r = now.readings[s.name] || {};
    name = tr("power");
    if (r.power_w != null) values = `${fmt(r.power_w, 0)} W`;
  }
  return `${back}<div class="detail-title"><b>${esc(name)}</b>${values ? `<span>${values}</span>` : ""}</div>
    ${more ? `<div class="detail-more">${more}</div>` : ""}`;
}

// Days before the first one with data are left out: a new board's charts
// start with its data, not with a month of nothing. A week stays at least.
function fromFirst(items, has, keep = 7) {
  const first = items.findIndex(has);
  return first < 0 ? items : items.slice(Math.min(first, Math.max(0, items.length - keep)));
}

function legend(items) {
  return `<span class="legend">${items.join("")}</span>`;
}

function swatch(cls, label) {
  return `<span class="lg"><i class="sw ${cls}"></i>${esc(label)}</span>`;
}

function zoneCharts(s, d) {
  const zone = now.climate.zones.find(z => z.name === s.name) || { name: s.name };
  const r = RANGES[detail.range];
  const to = boardNowS(), from = to - r.s;
  const cards = [];
  const outside = detail.outside && d.outside.length > 0;
  const series = [{ cls: "s-main", label: zone.name, points: d.temp, unit: "°", digits: 1 }];
  if (outside) series.push({ cls: "s-out", label: tr("outside"), points: d.outside, unit: "°", digits: 1 });
  const runWord = tr("ch_run");
  const items = [swatch("s-main", zone.name)];
  if (d.outside.length) {
    items.push(`<button class="lg lg-toggle" type="button" data-detail-outside aria-pressed="${outside}" title="${esc(tr("ch_compare"))}">
      <i class="sw s-out"></i>${esc(tr("outside"))}</button>`);
  }
  if (d.runs) items.push(swatch("sw-run", runWord));
  cards.push({ id: "temp", title: tr("ch_temp"), side: legend(items),
    draw: w => lineChart("temp", w, 180, { from, to, bucket: r.bucket, series, minSpan: 2, runs: d.runs, runWord,
      label: tr("ch_temp_of", { name: zone.name }) }) });
  if (d.rh.length) {
    cards.push({ id: "rh", title: tr("ch_rh"),
      draw: w => lineChart("rh", w, 96, { from, to, bucket: r.bucket, minSpan: 10, label: tr("ch_rh"),
        series: [{ cls: "s-rh", label: tr("ch_rh"), points: d.rh, unit: "%", digits: 0 }] }) });
  }
  const temps = d.temps && d.temps.zones && d.temps.zones[zone.name];
  if (temps && temps.some(Boolean)) {
    const days = d.temps.days.map((day, i) => {
      const t = temps[i];
      const label = isoDay(day, { weekday: "short", day: "numeric" });
      return { label, lo: t ? t.min : null, hi: t ? t.max : null,
               tip: t ? [isoDay(day, { weekday: "long", day: "numeric", month: "long" }),
                         tr("ch_low_at", { v: fmt(t.min), time: hhmm(t.min_at) }), tr("ch_high_at", { v: fmt(t.max), time: hhmm(t.max_at) })]
                 : [label, tr("ch_no_data")] };
    });
    cards.push({ id: "lows", title: tr("ch_lows_highs"),
      draw: w => rangeChart("lows", w, 150, days, { numbers: true, every: 1, cls: "r-main", unit: "°", label: tr("ch_lows_highs") }) });
  }
  const hours = d.runs && d.hours && d.hours.zones && d.hours.zones[zone.name];
  if (hours) {
    const days = fromFirst(d.hours.days.map((day, i) => {
      const h = hours.hours[i], n = hours.starts ? hours.starts[i] : null;
      const label = isoDay(day, { day: "numeric", month: "short" });
      return { label, value: h, tip: h == null ? [label, tr("ch_no_data")]
        : [isoDay(day, { weekday: "long", day: "numeric", month: "long" }), tr("ch_hours_day", { h: fmt(h, 1) })
           + (n ? ` · ${tr(n === 1 ? "ch_start" : "ch_starts", { n })}` : "")] };
    }), it => it.value != null);
    cards.push({ id: "hours", title: tr(now.climate.mode === "cool" ? "ch_hours_cool" : "ch_hours_heat"),
      side: `<span class="side">${esc(tr("ch_hours_24", { h: fmt(hours.last_24h || 0, 1) }))}</span>`,
      draw: w => barChart("hours", w, 110, days, { unit: " h", digits: 1, every: 7, cls: "b-run", floor: 1, label: tr("ch_hours_heat") }) });
  }
  return cards;
}

function outdoorCharts(d) {
  const r = RANGES[detail.range];
  const to = boardNowS(), from = to - r.s;
  const cards = [{ id: "temp", title: tr("ch_temp"),
    draw: w => lineChart("temp", w, 180, { from, to, bucket: r.bucket, minSpan: 2, label: tr("ch_temp_of", { name: tr("outside") }),
      series: [{ cls: "s-main", label: tr("outside"), points: d.temp, unit: "°", digits: 1 }] }) }];
  if (d.rh.length) {
    cards.push({ id: "rh", title: tr("ch_rh"),
      draw: w => lineChart("rh", w, 96, { from, to, bucket: r.bucket, minSpan: 10, label: tr("ch_rh"),
        series: [{ cls: "s-rh", label: tr("ch_rh"), points: d.rh, unit: "%", digits: 0 }] }) });
  }
  const byDay = Object.fromEntries(d.days.map(x => [x.day, x]));
  const days = fromFirst(lastDays(30).map(day => {
    const x = byDay[day];
    const label = isoDay(day, { day: "numeric", month: "short" });
    return { label, lo: x ? x.min : null, hi: x ? x.max : null, mean: x ? x.mean : null,
             tip: x ? [isoDay(day, { weekday: "long", day: "numeric", month: "long" }), tr("ch_low_at", { v: fmt(x.min), time: hhmm(x.min_at) }),
                       tr("ch_high_at", { v: fmt(x.max), time: hhmm(x.max_at) }), tr("ch_mean", { v: fmt(x.mean) })]
               : [label, tr("ch_no_data")] };
  }), it => it.lo != null);
  if (days.some(x => x.lo != null)) {
    cards.push({ id: "days", title: tr("ch_out_days"), side: legend([swatch("sw-range", tr("ch_low_high")), swatch("sw-mean", tr("ch_mean_word"))]),
      draw: w => rangeChart("days", w, 150, days, { every: 7, cls: "r-out", unit: "°", label: tr("ch_out_days") }) });
  }
  return cards;
}

function powerCharts(s, d) {
  const r = RANGES[detail.range];
  const to = boardNowS(), from = to - r.s;
  const meter = powerMeter();
  const budget = meter && meter.power_budget && meter.power_budget.available_power_w;
  const top = d.power.reduce((m, p) => Math.max(m, p.value), 0);
  const ref = budget && top >= budget * 0.6 ? { value: budget, label: tr("ch_budget", { w: fmt(budget, 0) }) } : null;
  const cards = [{ id: "power", title: tr("ch_power"),
    draw: w => lineChart("power", w, 180, { from, to, bucket: r.bucket, minSpan: 500, ref, padL: 42, label: tr("ch_power"),
      series: [{ cls: "s-power", label: tr("ch_power"), points: d.power, unit: " W", digits: 0 }] }) }];
  const e = d.energy;
  if (e) {
    const byDay = {};
    [e.week, e.month].forEach(block => {
      if (!block || !block.days) return;
      block.days.forEach((x, i) => {
        if (x) byDay[x.day || new Date(Date.parse(`${block.from}T00:00:00Z`) + i * 86400000).toISOString().slice(0, 10)] = x;
      });
    });
    [e.yesterday, e.today].forEach(x => { if (x && x.day) byDay[x.day] = x; });
    const days = fromFirst(lastDays(30).map(day => {
      const x = byDay[day];
      const label = isoDay(day, { day: "numeric", month: "short" });
      return { label, value: x ? x.kwh : null,
               tip: x ? [isoDay(day, { weekday: "long", day: "numeric", month: "long" }), `${fmt(x.kwh, 1)} kWh`,
                         x.peak_w != null ? tr("ch_peak", { w: fmt(x.peak_w, 0), time: hhmm(x.peak_ts) }) : ""].filter(Boolean)
                 : [label, tr("ch_no_data")] };
    }), it => it.value != null);
    const sums = [["en_today", e.today], ["en_yesterday", e.yesterday], ["en_week", e.week], ["en_month", e.month]]
      .filter(([, x]) => x && x.kwh != null)
      .map(([key, x]) => `<span><small>${esc(tr(key))}</small><b>${fmt(x.kwh, 1)}</b> kWh</span>`);
    cards.push({ id: "kwh", title: tr("ch_energy"),
      foot: sums.length ? `<div class="sums">${sums.join("")}</div>` : "",
      draw: w => barChart("kwh", w, 120, days, { unit: " kWh", digits: 1, every: 7, cls: "b-energy", floor: 1, label: tr("ch_energy") }) });
  }
  return cards;
}

// ------------------------------------------------------------ the marks --

// A round step near span / count: 1, 2 or 5 times a power of ten.
function niceStep(span, count) {
  const raw = Math.max(span, 1e-9) / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const n = raw / power;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * power;
}

// The values' span, never less than minSpan (half a degree of noise isn't
// drawn as a cliff), to round steps.
function yDomain(values, minSpan, floorZero) {
  let lo = Math.min(...values), hi = Math.max(...values);
  if (floorZero) lo = 0;
  if (hi - lo < minSpan) {
    const mid = (lo + hi) / 2;
    lo = floorZero ? 0 : mid - minSpan / 2;
    hi = floorZero ? minSpan : mid + minSpan / 2;
  }
  const step = niceStep(hi - lo, 3);
  return { lo: Math.floor(lo / step + 1e-9) * step, hi: Math.ceil(hi / step - 1e-9) * step, step };
}

function yAxis(parts, dom, y, x0, x1, unit) {
  const digits = dom.step < 1 ? 1 : 0;
  const count = Math.round((dom.hi - dom.lo) / dom.step);
  for (let i = 0; i <= count; i++) {
    const v = dom.lo + i * dom.step, yy = y(v).toFixed(1);
    parts.push(`<line class="grid${i === 0 ? " base" : ""}" x1="${x0}" x2="${x1}" y1="${yy}" y2="${yy}"/>`
      + `<text class="ax" x="${x0 - 6}" y="${yy}" dy="0.32em" text-anchor="end">${fmt(v, digits)}${unit || ""}</text>`);
  }
}

function timeTicks(from, to, width) {
  const span = to - from;
  const step = span <= 6 * 3600 ? 3600 : span <= 86400 ? (width < 420 ? 6 : 3) * 3600 : 86400;
  const off = offsetS();
  const ticks = [];
  for (let t = Math.ceil((from + off) / step) * step - off; t <= to; t += step) ticks.push(t);
  return { ticks, daily: step === 86400 };
}

function svgOpen(id, width, height, label) {
  return `<svg data-chart="${id}" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}">`;
}

// Lines through the history, broken where the board kept nothing; a valve's
// openings as a strip under them; one cross and its values under the pointer.
function lineChart(id, width, height, o) {
  const strip = o.runs ? 10 : 0;
  const x0 = o.padL || 36, x1 = width - 8, y0 = 8, y1 = height - 20 - (strip ? strip + 6 : 0);
  const half = o.bucket / 2;
  const shown = s => s.points.filter(p => p.ts + half >= o.from);
  const values = o.series.flatMap(s => shown(s).map(p => p.value));
  if (!values.length) return `<p class="detail-note">${esc(tr("ch_empty"))}</p>`;
  if (o.ref) values.push(o.ref.value);
  const dom = yDomain(values, o.minSpan, !!o.ref || o.series[0].cls === "s-power");
  const x = ts => Math.max(x0, Math.min(x1, x0 + (ts - o.from) / (o.to - o.from) * (x1 - x0)));
  const y = v => y1 - (v - dom.lo) / (dom.hi - dom.lo) * (y1 - y0);
  const parts = [];
  yAxis(parts, dom, y, x0, x1, "");
  const { ticks, daily } = timeTicks(o.from, o.to, x1 - x0);
  ticks.forEach(t => {
    const xx = x(t).toFixed(1);
    if (daily) {
      parts.push(`<line class="grid day" x1="${xx}" x2="${xx}" y1="${y0}" y2="${y1}"/>`);
      const mid = x(t + 43200);
      if (x(t + 86400) - x(t) > 30 && mid < x1 - 10) {
        parts.push(`<text class="ax" x="${mid.toFixed(1)}" y="${height - 5}" text-anchor="middle">${esc(dayWords(wallDate(t + 43200), { weekday: "short", day: "numeric" }))}</text>`);
      }
    } else {
      parts.push(`<line class="tick" x1="${xx}" x2="${xx}" y1="${y1}" y2="${y1 + 4}"/>`);
      if (x(t) - x0 > 14 && x1 - x(t) > 14) parts.push(`<text class="ax" x="${xx}" y="${height - 5}" text-anchor="middle">${hhmm(t)}</text>`);
    }
  });
  const step = Math.max(o.bucket, WRITE_S);
  if (strip) {
    const top = y1 + 6;
    parts.push(`<rect class="strip" x="${x0}" y="${top}" width="${(x1 - x0).toFixed(1)}" height="${strip}" rx="2"/>`);
    spans(o.runs, step).forEach(sp => {
      const a = x(sp.from), b = x(sp.to);
      if (b - a <= 0) return;
      const h = strip * sp.value;
      parts.push(`<rect class="run" x="${a.toFixed(1)}" y="${(top + strip - h).toFixed(1)}" width="${Math.max(1, b - a).toFixed(1)}" height="${h.toFixed(1)}"/>`);
    });
  }
  if (o.ref) {
    const yy = y(o.ref.value).toFixed(1);
    parts.push(`<line class="ref" x1="${x0}" x2="${x1}" y1="${yy}" y2="${yy}"/><text class="ref-label" x="${x1 - 2}" y="${yy}" dy="-4" text-anchor="end">${esc(o.ref.label)}</text>`);
  }
  const gap = step * 2.5;
  o.series.forEach(s => parts.push(`<path class="line ${s.cls}" d="${linePath(shown(s), p => x(p.ts + half), p => y(p.value), gap)}"/>`));
  parts.push(`<line class="cross" x1="0" x2="0" y1="${y0}" y2="${y1}" visibility="hidden"/><g class="dots"></g>`);
  parts.push(`<rect class="hit" x="${x0}" y="0" width="${(x1 - x0).toFixed(1)}" height="${height}"/>`);
  geometry[id] = { kind: "line", x0, x1, from: o.from, to: o.to, x, y, half, gap, step, series: o.series, runs: o.runs, runWord: o.runWord };
  const lo = Math.min(...values), hi = Math.max(...values), s0 = o.series[0];
  const label = `${o.label}: ${fmt(lo, s0.digits)}${s0.unit} – ${fmt(hi, s0.digits)}${s0.unit}`;
  return `${svgOpen(id, width, height, label)}${parts.join("")}</svg>`;
}

function linePath(points, px, py, gap) {
  let d = "", prev = null, alone = false;
  points.forEach(p => {
    const joined = prev && p.ts - prev.ts <= gap;
    if (!joined && alone) d += "h0.1";      // a lone value still shows, as a dot
    d += `${joined ? "L" : "M"}${px(p).toFixed(1)},${py(p).toFixed(1)}`;
    alone = !joined;
    prev = p;
  });
  return alone ? `${d}h0.1` : d;
}

// A valve's samples as spans of the same share open: whole in 6 and 24
// hours, the share of each hour in 7 days.
function spans(points, step) {
  const out = [];
  (points || []).forEach(p => {
    const value = Math.max(0, Math.min(1, Math.round(p.value * 20) / 20));
    if (value <= 0) return;
    const last = out[out.length - 1];
    if (last && last.value === value && p.ts - last.to <= step * 0.5) last.to = p.ts + step;
    else out.push({ from: p.ts, to: p.ts + step, value });
  });
  return out;
}

// Each day from its lowest to its highest; its mean marked when there is one.
function rangeChart(id, width, height, items, o) {
  const values = items.flatMap(i => [i.lo, i.hi]).filter(v => v != null);
  if (!values.length) return `<p class="detail-note">${esc(tr("ch_empty"))}</p>`;
  const x0 = o.numbers ? 8 : 36, x1 = width - 8, y0 = o.numbers ? 18 : 8, y1 = height - (o.numbers ? 34 : 20);
  const dom = yDomain(values, 2);
  const y = v => y1 - (v - dom.lo) / (dom.hi - dom.lo) * (y1 - y0);
  const col = (x1 - x0) / items.length;
  const bw = Math.max(3, Math.min(8, col * 0.4));
  const parts = [];
  if (!o.numbers) yAxis(parts, dom, y, x0, x1, "");
  items.forEach((it, i) => {
    const cx = x0 + col * (i + 0.5);
    if (it.lo != null && it.hi != null) {
      const a = y(it.hi), b = Math.max(y(it.lo), a + bw);
      parts.push(`<rect class="range ${o.cls}" x="${(cx - bw / 2).toFixed(1)}" y="${a.toFixed(1)}" width="${bw.toFixed(1)}" height="${(b - a).toFixed(1)}" rx="${(bw / 2).toFixed(1)}"/>`);
      if (it.mean != null) {
        const m = y(it.mean).toFixed(1);
        parts.push(`<line class="mean" x1="${(cx - bw).toFixed(1)}" x2="${(cx + bw).toFixed(1)}" y1="${m}" y2="${m}"/>`);
      }
      if (o.numbers) {
        parts.push(`<text class="val" x="${cx.toFixed(1)}" y="${(a - 5).toFixed(1)}" text-anchor="middle">${fmt(it.hi)}</text>`
          + `<text class="val lo" x="${cx.toFixed(1)}" y="${(b + 12).toFixed(1)}" text-anchor="middle">${fmt(it.lo)}</text>`);
      }
    }
    if ((items.length - 1 - i) % o.every === 0) {
      parts.push(`<text class="ax" x="${cx.toFixed(1)}" y="${height - 5}" text-anchor="middle">${esc(it.label)}</text>`);
    }
    parts.push(`<rect class="hit-col" x="${(x0 + col * i).toFixed(1)}" y="0" width="${col.toFixed(1)}" height="${height}" data-tip="${esc(JSON.stringify(it.tip))}"/>`);
  });
  geometry[id] = { kind: "columns" };
  const label = `${o.label}: ${fmt(Math.min(...values))}${o.unit} – ${fmt(Math.max(...values))}${o.unit}`;
  return `${svgOpen(id, width, height, label)}${parts.join("")}</svg>`;
}

// A bar a day from zero, rounded at its top; none for a day with nothing kept.
function barChart(id, width, height, items, o) {
  const values = items.map(i => i.value).filter(v => v != null);
  if (!values.length) return `<p class="detail-note">${esc(tr("ch_empty"))}</p>`;
  const x0 = 36, x1 = width - 8, y0 = 8, y1 = height - 20;
  const dom = yDomain(values, o.floor || 1, true);
  const y = v => y1 - (v - dom.lo) / (dom.hi - dom.lo) * (y1 - y0);
  const col = (x1 - x0) / items.length;
  const bw = Math.max(2, Math.min(18, col - 2));
  const parts = [];
  yAxis(parts, dom, y, x0, x1, "");
  items.forEach((it, i) => {
    const left = x0 + col * i + (col - bw) / 2;
    if (it.value != null && it.value > 0) {
      const top = y(it.value), h = y1 - top;
      const r = Math.min(3, bw / 2, h);
      parts.push(`<path class="bar ${o.cls}" d="M${left.toFixed(1)},${y1}V${(top + r).toFixed(1)}Q${left.toFixed(1)},${top.toFixed(1)} ${(left + r).toFixed(1)},${top.toFixed(1)}`
        + `H${(left + bw - r).toFixed(1)}Q${(left + bw).toFixed(1)},${top.toFixed(1)} ${(left + bw).toFixed(1)},${(top + r).toFixed(1)}V${y1}Z"/>`);
    }
    if ((items.length - 1 - i) % o.every === 0) {
      parts.push(`<text class="ax" x="${(left + bw / 2).toFixed(1)}" y="${height - 5}" text-anchor="middle">${esc(it.label)}</text>`);
    }
    parts.push(`<rect class="hit-col" x="${(x0 + col * i).toFixed(1)}" y="0" width="${col.toFixed(1)}" height="${height}" data-tip="${esc(JSON.stringify(it.tip))}"/>`);
  });
  geometry[id] = { kind: "columns" };
  const total = values.reduce((a, b) => a + b, 0);
  return `${svgOpen(id, width, height, `${o.label}: ${fmt(total, o.digits)}${o.unit}`)}${parts.join("")}</svg>`;
}

// ------------------------------------------------- the zones' small lines --
// Each zone's last 24 hours in its card, with its valve's openings under the
// line, and which way it goes now. Read again no sooner than the history
// gains a value.

const sparks = { data: {}, at: 0, busy: false };

async function loadSparks() {
  if (sparks.busy || document.hidden || !now.climate || Date.now() - sparks.at < DETAIL_STALE_MS) return;
  sparks.busy = true;
  try {
    await Promise.all(now.climate.zones.filter(z => z.sensor && z.sensor_configured !== false).map(async zone => {
      const valve = zone.valve && zone.valve_configured !== false;
      const [air, runs] = await Promise.all([history(zone.sensor, ["temperature_c"], "24h"),
        valve ? history(zone.valve, ["is_on"], "24h").catch(() => ({})) : {}]);
      sparks.data[zone.name] = { temp: clean(air.temperature_c), runs: valve ? clean(runs.is_on) : [] };
    }));
    sparks.at = Date.now();
  } catch (e) { /* the cards stay without their line until the next reading */ }
  sparks.busy = false;
  if (!now.acting) drawClimate();
}

function sparkSvg(name) {
  const s = sparks.data[name];
  const to = boardNowS(), from = to - 86400;
  const points = s ? s.temp.filter(p => p.ts >= from) : [];
  if (points.length < 2) return "";
  const values = points.map(p => p.value);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (hi - lo < 1) {
    const mid = (lo + hi) / 2;
    lo = mid - 0.5;
    hi = mid + 0.5;
  }
  const x = ts => Math.max(0, Math.min(100, (ts - from) / 86400 * 100));
  const y = v => 2 + (hi - v) / (hi - lo) * 19;
  const runs = spans(s.runs, WRITE_S).map(sp => `<rect class="spark-run" x="${x(sp.from).toFixed(2)}" y="25" width="${Math.max(0.4, x(sp.to) - x(sp.from)).toFixed(2)}" height="3"/>`).join("");
  const label = tr("spark_title", { lo: fmt(Math.min(...values)), hi: fmt(Math.max(...values)) });
  return `<svg class="spark" viewBox="0 0 100 28" preserveAspectRatio="none" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>
    <path class="spark-line" d="${linePath(points, p => x(p.ts), p => y(p.value), WRITE_S * 2.5)}" vector-effect="non-scaling-stroke"/>${runs}</svg>`;
}

// Which way the zone goes: the last value against the one an hour before.
// Under a fifth of a degree is no way at all.
function trendHtml(name) {
  const s = sparks.data[name];
  if (!s || s.temp.length < 2) return "";
  const last = s.temp[s.temp.length - 1];
  const before = nearest(s.temp, last.ts - 3600, 900);
  if (!before || before === last) return "";
  const d = Math.round((last.value - before.value) * 10) / 10;
  if (Math.abs(d) < 0.2) return "";
  const up = d > 0;
  const said = tr(up ? "trend_up" : "trend_down", { d: `${up ? "+" : "−"}${fmt(Math.abs(d))}` });
  return `<span class="trend" data-way="${up ? "up" : "down"}" title="${esc(said)}" aria-label="${esc(said)}">${icon(up ? "\uE74A" : "\uE74B")}${up ? "+" : "−"}${fmt(Math.abs(d))}°</span>`;
}

// ------------------------------------------------------------ pointer --

function nearest(points, ts, within) {
  if (!points || !points.length) return null;
  let lo = 0, hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].ts < ts) lo = mid + 1;
    else hi = mid;
  }
  const best = [points[lo], points[lo - 1]].filter(Boolean)
    .reduce((a, b) => (Math.abs(a.ts - ts) <= Math.abs(b.ts - ts) ? a : b));
  return Math.abs(best.ts - ts) <= within ? best : null;
}

// One time for every line chart shown: the cross on each, their values together.
function hoverAt(ts, clientX, anchor) {
  const lines = [];
  Object.entries(geometry).forEach(([id, g]) => {
    if (g.kind !== "line") return;
    const svg = document.querySelector(`svg[data-chart="${id}"]`);
    if (!svg) return;
    const cx = g.x(ts).toFixed(1);
    const cross = svg.querySelector(".cross");
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
    cross.setAttribute("visibility", "visible");
    const dots = [];
    g.series.forEach(s => {
      const p = nearest(s.points, ts - g.half, g.gap / 2);
      if (!p) return;
      dots.push(`<circle class="dot ${s.cls}" cx="${g.x(p.ts + g.half).toFixed(1)}" cy="${g.y(p.value).toFixed(1)}" r="4"/>`);
      lines.push(`<span><i class="sw ${s.cls}"></i>${esc(s.label)} <b>${fmt(p.value, s.digits)}${s.unit}</b></span>`);
    });
    svg.querySelector(".dots").innerHTML = dots.join("");
    if (g.runs) {
      const p = nearest(g.runs, ts - g.step / 2, g.step / 2 + 1);
      const share = p ? p.value : 0;
      const word = detail.range === "7d" ? (share > 0 ? tr("ch_run_minutes", { what: g.runWord, m: Math.round(share * 60) }) : tr("ch_run_none"))
        : share >= 0.5 ? g.runWord : tr("ch_run_none");
      lines.push(`<span><i class="sw ${share > 0 ? "sw-run" : "sw-off"}"></i>${esc(word)}</span>`);
    }
  });
  showTip(`<small>${esc(timeLabel(ts))}</small>${lines.join("")}`, clientX, anchor);
}

function showTip(html, clientX, anchor) {
  const tip = $("tip");
  tip.innerHTML = html;
  tip.hidden = false;
  const box = anchor.getBoundingClientRect();
  const w = tip.offsetWidth;
  const left = clientX + 14 + w > window.innerWidth - 6 ? clientX - 14 - w : clientX + 14;
  tip.style.left = `${Math.max(6, left)}px`;
  tip.style.top = `${Math.max(6, box.top + 4)}px`;
}

function hideTip() {
  const tip = $("tip");
  if (tip) tip.hidden = true;
  document.querySelectorAll("#detail-body .cross").forEach(c => c.setAttribute("visibility", "hidden"));
  document.querySelectorAll("#detail-body .dots").forEach(d => { d.innerHTML = ""; });
}

function onChartPointer(event) {
  const column = event.target.closest(".hit-col");
  if (column) {
    let lines = [];
    try { lines = JSON.parse(column.dataset.tip || "[]"); } catch (e) { /* nothing to say */ }
    showTip(lines.map((l, i) => (i ? `<span>${esc(l)}</span>` : `<small>${esc(l)}</small>`)).join(""), event.clientX, column.closest("svg"));
    return;
  }
  const svg = event.target.closest("svg[data-chart]");
  const g = svg && geometry[svg.dataset.chart];
  if (!g || g.kind !== "line") {
    hideTip();
    return;
  }
  const px = event.clientX - svg.getBoundingClientRect().left;
  if (px < g.x0 || px > g.x1) {
    hideTip();
    return;
  }
  hoverAt(g.from + (px - g.x0) / (g.x1 - g.x0) * (g.to - g.from), event.clientX, svg);
}

function wireDetail() {
  const body = $("detail-body");
  body.addEventListener("pointermove", onChartPointer);
  body.addEventListener("pointerdown", onChartPointer);
  body.addEventListener("pointerleave", hideTip);
  new ResizeObserver(() => {
    if (Math.floor(body.clientWidth) !== detail.width) drawDetail();
  }).observe(body);
  WIDE.addEventListener("change", onWidth);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeDetail();
  });
  window.addEventListener("scroll", hideTip, { passive: true });
}
