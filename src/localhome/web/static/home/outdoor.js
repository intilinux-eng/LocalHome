/* The home page (templates/home.html), in parts - see core.js. */
// The outdoor sensor's sheet, opened from its tile at the top: its last 24
// hours as a line; its days, weeks and months as bars from the lowest to
// the highest; its years month by month. All from what the board keeps of
// it (/api/climate/outdoor: each day's mean, lowest and highest, six
// years; /api/sensors/.../history: the last 24 hours). It only reads.
"use strict";

const OUTDOOR = "Outdoor Sensor";
const OUTDOOR_VIEWS = ["24h", "days", "weeks", "months", "years"];
// How many bars each view has: a month of days, half a year of weeks, a
// year of months.
const OUTDOOR_BARS = { days: 30, weeks: 26, months: 12 };
// Readings further apart than this are a gap in the line, not a slope.
const OUTDOOR_GAP_S = 20 * 60;
const DAY_MS = 86400000;

// Only with a sensor, real or simulated: renderOutdoor() makes the tile a
// button then, and nothing else opens this.
async function openOutdoor() {
  if (!deviceByName(OUTDOOR)) return;
  openSheet(tr("home.outdoor_title"), `<div class="outdoor-sheet" id="outdoor-sheet">
    <p class="sheet-note">${esc(tr("home.outdoor_loading"))}</p></div>`, outdoorClick);
  const sheet = state.sheet;
  const box = document.getElementById("outdoor-sheet");
  box.addEventListener("pointerdown", outdoorPointer);
  box.addEventListener("pointermove", outdoorPointer);
  let days, history;
  try {
    [days, history] = await Promise.all([api("/api/climate/outdoor"),
      api(`/api/sensors/${encodeURIComponent(OUTDOOR)}/history?field=temperature_c&range=24h`)]);
  } catch (error) {
    if (state.sheet === sheet) morph(box, `<p class="sheet-note">${esc(tr("home.outdoor_failed", { why: errorText(error) }))}</p>`);
    return;
  }
  // Closed meanwhile, or another sheet opened: nothing to draw.
  if (state.sheet !== sheet) return;
  state.outdoor.days = days.days || [];
  state.outdoor.line = ((history.series && history.series.temperature_c) || []).filter(p => p.value != null);
  renderOutdoorSheet();
}

function renderOutdoorSheet() {
  const box = document.getElementById("outdoor-sheet");
  if (!box) return;
  const device = deviceByName(OUTDOOR);
  const simulated = device && device.simulated ? `<p class="sheet-note">${esc(tr("home.outdoor_simulated"))}</p>` : "";
  const days = outdoorDays();
  if (!Object.keys(days).length && !state.outdoor.line.length) {
    morph(box, `${simulated}<p class="sheet-note">${esc(tr("home.outdoor_no_data"))}</p>`);
    return;
  }
  const view = state.outdoor.view;
  const views = `<div class="segmented outdoor-views" role="group" aria-label="${esc(tr("home.outdoor_views"))}">
    ${OUTDOOR_VIEWS.map(v => `<button type="button" data-outdoor-view="${v}" aria-pressed="${v === view}">${esc(tr(`home.outdoor_view_${v}`))}</button>`).join("")}</div>`;
  const body = view === "24h" ? outdoorLineHtml() : view === "years" ? outdoorYearsHtml(days) : outdoorBarsHtml(view, days);
  morph(box, `${simulated}${views}${body}<p class="runs-note">${esc(tr("home.outdoor_kept"))}</p>`);
}

function outdoorClick(event) {
  const view = event.target.closest("[data-outdoor-view]");
  if (view) {
    state.outdoor.view = view.dataset.outdoorView;
    renderOutdoorSheet();
    return;
  }
  // A bar tapped: its numbers under the chart; tapped again, the period's.
  const bar = event.target.closest("[data-outdoor-pick]");
  if (bar) {
    const i = Number(bar.dataset.outdoorPick);
    state.outdoor.pick[state.outdoor.view] = state.outdoor.pick[state.outdoor.view] === i ? null : i;
    renderOutdoorSheet();
  }
}

// The days as kept, today's lowest and highest with the reading now - the
// tile's own (renderOutdoor()), so the two never disagree.
function outdoorDays() {
  const out = {};
  for (const d of state.outdoor.days) out[d.day] = d;
  const today = dayValue(boardNow());
  const reading = state.readings[OUTDOOR];
  const live = withReading(((state.dayRanges || {})[OUTDOOR] || {}).temperature_c, reading && reading.temperature_c);
  if (live) out[today] = { mean: null, ...out[today], day: today, min: live.min, max: live.max, min_at: live.min_at, max_at: live.max_at };
  return out;
}

// --------------------------------------------------------------- dates --
// The board's days as "YYYY-MM-DD", counted on UTC dates: no clock change
// in between, whatever this phone's time zone.

function isoMs(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

function isoAdd(iso, days) {
  return dayValue(new Date(isoMs(iso) + days * DAY_MS));
}

function weekdayName(iso) {
  return tr(`home.day_long.${DAYS[(new Date(isoMs(iso)).getUTCDay() + 6) % 7]}`);
}

function monthLength(year, month0) {
  return new Date(Date.UTC(year, month0 + 1, 0)).getUTCDate();
}

// ------------------------------------------------------------- numbers --

// Days into one period's numbers: its lowest and highest, each with its
// day and time; the mean of its days' means; how many days it has, and
// how many went below zero.
function outdoorSummary(list) {
  const known = list.filter(Boolean);
  if (!known.length) return null;
  const low = known.reduce((a, b) => (b.min < a.min ? b : a));
  const high = known.reduce((a, b) => (b.max > a.max ? b : a));
  const means = known.filter(d => d.mean != null).map(d => d.mean);
  return {
    min: low.min, min_day: low.day, min_at: low.min_at, max: high.max, max_day: high.day, max_at: high.max_at,
    mean: means.length ? means.reduce((a, b) => a + b, 0) / means.length : null,
    count: known.length, frost: known.filter(d => d.min < 0).length,
  };
}

// A little room above and below, and a line at each step: half a degree on
// a steady day, more over a wide span.
function outdoorScale(low, high) {
  const span = Math.max(high - low, 1);
  const bottom = low - span * 0.12, top = high + span * 0.12, range = top - bottom;
  const step = range <= 2.5 ? 0.5 : range <= 5 ? 1 : range <= 10 ? 2 : range <= 25 ? 5 : 10;
  const lines = [];
  // (+ 0: a line at -0 would read "-0°".)
  for (let t = Math.ceil(bottom / step) * step; t <= top; t += step) lines.push(Math.round(t * 10) / 10 + 0);
  return { lines, at: t => (t - bottom) / range * 100 };
}

// Zero drawn a little stronger: below it, frost.
function outdoorLinesHtml(scale) {
  return `<div class="temp-lines" aria-hidden="true">${scale.lines.map(v => `<span style="--y: ${scale.at(v).toFixed(1)}%"${v === 0 ? ' class="zero"' : ""}>
    <i>${fmt(v, Number.isInteger(v) ? 0 : 1)}°</i></span>`).join("")}</div>`;
}

// -------------------------------------------------------- the last 24 h --

function outdoorLineHtml() {
  const points = state.outdoor.line;
  if (points.length < 2) return `<p class="sheet-note">${esc(tr("home.outdoor_no_line"))}</p>`;
  const end = boardEpochMs() / 1000, start = end - 86400;
  const scale = outdoorScale(Math.min(...points.map(p => p.value)), Math.max(...points.map(p => p.value)));
  const x = ts => Math.min(100, Math.max(0, (ts - start) / 864));
  let path = "", last = null;
  for (const p of points) {
    path += `${last != null && p.ts - last <= OUTDOOR_GAP_S ? "L" : "M"}${(x(p.ts) * 10).toFixed(1)} ${(100 - scale.at(p.value)).toFixed(2)} `;
    last = p.ts;
  }
  const low = points.reduce((a, b) => (b.value < a.value ? b : a));
  const high = points.reduce((a, b) => (b.value > a.value ? b : a));
  const dot = (p, kind) => `<i class="outdoor-dot" data-kind="${kind}" style="--x: ${x(p.ts).toFixed(2)}%; --y: ${scale.at(p.value).toFixed(2)}%"></i>`;
  // The hours under it at the board's 00, 06, 12 and 18 - on the day the
  // clocks change too - none too near an edge, where it would stick out.
  const hours = [];
  for (let t = Math.ceil(start / 3600) * 3600; t <= end; t += 3600) {
    const w = wall(t * 1000);
    if (w.getUTCHours() % 6 === 0 && x(t) > 4 && x(t) < 96) hours.push(`<span style="--x: ${x(t).toFixed(2)}%">${hhmm(w)}</span>`);
  }
  const summary = tr("home.outdoor_line_summary", { min: fmt(low.value), min_at: epochHhmm(low.ts), max: fmt(high.value), max_at: epochHhmm(high.ts) });
  state.outdoor.lineScale = { start, end, scale };
  return `<div class="outdoor-line" role="img" aria-label="${esc(summary)}">
      ${outdoorLinesHtml(scale)}
      <div class="outdoor-plot">
        <svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true"><path d="${path.trim()}" vector-effect="non-scaling-stroke"/></svg>
        ${dot(low, "min")}${dot(high, "max")}
        <i class="outdoor-cursor" hidden></i><i class="outdoor-dot" data-kind="cursor" hidden></i>
      </div>
    </div>
    <div class="outdoor-hours" aria-hidden="true">${hours.join("")}</div>
    <div class="energy-total"><b id="outdoor-line-text" aria-live="polite">${esc(summary)}</b></div>
    <p class="runs-note">${esc(tr("home.outdoor_line_note"))}</p>`;
}

// A finger or the mouse on the line: the reading nearest to it, said under
// the chart.
function outdoorPointer(event) {
  const plot = event.target.closest(".outdoor-line");
  const kept = state.outdoor.lineScale;
  if (!plot || !kept || state.outdoor.view !== "24h") return;
  const area = plot.querySelector(".outdoor-plot").getBoundingClientRect();
  if (!area.width) return;
  const ts = kept.start + Math.min(1, Math.max(0, (event.clientX - area.left) / area.width)) * (kept.end - kept.start);
  const p = state.outdoor.line.reduce((a, b) => (Math.abs(b.ts - ts) < Math.abs(a.ts - ts) ? b : a));
  const x = Math.min(100, Math.max(0, (p.ts - kept.start) / (kept.end - kept.start) * 100));
  const cursor = plot.querySelector(".outdoor-cursor"), dot = plot.querySelector('.outdoor-dot[data-kind="cursor"]');
  cursor.hidden = dot.hidden = false;
  cursor.style.setProperty("--x", `${x.toFixed(2)}%`);
  dot.style.cssText = `--x: ${x.toFixed(2)}%; --y: ${kept.scale.at(p.value).toFixed(2)}%`;
  setText(document.getElementById("outdoor-line-text"), tr("home.outdoor_line_point", { time: epochHhmm(p.ts), value: fmt(p.value) }));
}

// ------------------------------------------- days, weeks and months --

// The bars of a view, oldest first - each its days, missing ones null:
// [{key, days, size, current, sum}].
function outdoorBars(view, days) {
  const today = dayValue(boardNow());
  const n = OUTDOOR_BARS[view];
  const bars = [];
  for (let i = n - 1; i >= 0; i--) {
    if (view === "days") {
      const iso = isoAdd(today, -i);
      bars.push({ key: iso, days: [days[iso]], size: 1 });
    } else if (view === "weeks") {
      const monday = isoAdd(isoAdd(today, -((new Date(isoMs(today)).getUTCDay() + 6) % 7)), -7 * i);
      bars.push({ key: monday, days: Array.from({ length: 7 }, (_, k) => days[isoAdd(monday, k)]), size: 7 });
    } else {
      const [y, m] = today.split("-").map(Number);
      const first = new Date(Date.UTC(y, m - 1 - i, 1));
      const key = dayValue(first).slice(0, 7), size = monthLength(first.getUTCFullYear(), first.getUTCMonth());
      bars.push({ key, days: Array.from({ length: size }, (_, k) => days[`${key}-${String(k + 1).padStart(2, "0")}`]), size });
    }
  }
  return bars.map((b, i) => ({ ...b, current: i === bars.length - 1, sum: outdoorSummary(b.days) }));
}

// Under the bars: today and a day a week back; a week's Monday every four;
// each month's initial.
function outdoorLabel(view, bar, back) {
  if (view === "days") return back === 0 ? tr("home.runs_today") : back % 7 === 0 ? String(Number(bar.key.slice(8))) : "";
  if (view === "weeks") return back % 4 === 0 ? `${Number(bar.key.slice(8))}/${Number(bar.key.slice(5, 7))}` : "";
  return tr("home.months_short")[Number(bar.key.slice(5, 7)) - 1];
}

// A bar's period in words: a day's date, a week's Monday, a month and year.
function outdoorBarName(view, key) {
  if (view === "days") return dayName(key);
  if (view === "weeks") return tr("home.outdoor_week_name", { from: dayName(key) });
  const [y, m] = key.split("-").map(Number);
  return `${tr("home.months_long")[m - 1]} ${y}`;
}

function outdoorBarText(view, bar, days) {
  const s = bar.sum;
  const mean = s.mean != null ? ` · ${tr("home.outdoor_mean", { mean: fmt(s.mean) })}` : "";
  let text;
  if (view === "days") {
    text = dayRangeText(bar.current ? tr("home.dr_today") : dayName(bar.key), s) + mean;
  } else if (view === "weeks") {
    text = tr("home.outdoor_week_line", { week: outdoorBarName(view, bar.key), min: fmt(s.min), min_day: weekdayName(s.min_day),
                                          max: fmt(s.max), max_day: weekdayName(s.max_day) }) + mean;
  } else {
    const [y, m] = bar.key.split("-").map(Number);
    text = tr("home.outdoor_month_line", { month: outdoorBarName(view, bar.key), min: fmt(s.min),
                                           min_day: Number(s.min_day.slice(8)), max: fmt(s.max), max_day: Number(s.max_day.slice(8)) }) + mean;
    // The same month a year before, when the board has it.
    const before = `${y - 1}-${String(m).padStart(2, "0")}`;
    const then = outdoorSummary(Object.values(days).filter(d => d.day.startsWith(before)));
    if (then && then.mean != null) text += ` · ${tr("home.outdoor_year_before", { mean: fmt(then.mean) })}`;
  }
  if (bar.current) return view === "days" ? text : `${text} · ${tr("home.outdoor_in_progress")}`;
  return s.count < bar.size ? `${text} · ${tr("home.outdoor_few_days", { n: s.count, of: bar.size })}` : text;
}

// The whole period's coldest and warmest day, and its days below zero.
function outdoorPeriodText(view, bars) {
  const s = outdoorSummary(bars.flatMap(b => b.days));
  if (!s) return "";
  const text = tr(`home.outdoor_period_${view}`, { min: fmt(s.min), min_day: dayName(s.min_day), max: fmt(s.max), max_day: dayName(s.max_day) });
  return s.frost ? `${text} · ${tr(s.frost === 1 ? "home.outdoor_frost_one" : "home.outdoor_frost_many", { n: s.frost })}` : text;
}

function outdoorBarsHtml(view, days) {
  const bars = outdoorBars(view, days);
  const known = bars.filter(b => b.sum);
  if (!known.length) return `<p class="sheet-note">${esc(tr("home.outdoor_no_period"))}</p>`;
  const scale = outdoorScale(Math.min(...known.map(b => b.sum.min)), Math.max(...known.map(b => b.sum.max)));
  const picked = state.outdoor.pick[view];
  const pick = picked != null && bars[picked] && bars[picked].sum ? picked : null;
  // Unpicked, the line says the period in progress - or the last one known.
  const shown = pick != null ? pick : bars.map(b => !!b.sum).lastIndexOf(true);
  const t = (state.readings[OUTDOOR] || {}).temperature_c;
  const n = bars.length;
  return `<div class="temp-chart outdoor-chart">
      ${outdoorLinesHtml(scale)}
      <div class="temp-bars" style="--n: ${n}" data-picked="${pick != null}">
        ${bars.map((b, i) => {
          const title = b.sum ? outdoorBarText(view, b, days) : tr("home.runs_day_unknown", { day: outdoorBarName(view, b.key) });
          const style = b.sum ? `style="--lo: ${scale.at(b.sum.min).toFixed(1)}%; --hi: ${scale.at(b.sum.max).toFixed(1)}%${b.sum.mean != null ? `; --mean: ${scale.at(b.sum.mean).toFixed(1)}%` : ""}"` : "disabled";
          // The temperature now on the period it's in, as on a zone's 7 days.
          const now = b.current && b.sum && t != null
            ? `<i class="temp-now" style="--now: ${scale.at(Math.min(b.sum.max, Math.max(b.sum.min, t))).toFixed(1)}%"></i>` : "";
          return `<button type="button" class="temp-bar" data-outdoor-pick="${i}" ${style} data-current="${b.current}"
            aria-pressed="${pick === i}" title="${esc(title)}" aria-label="${esc(title)}">${b.sum ? `<span></span>${b.sum.mean != null ? '<i class="outdoor-mean"></i>' : ""}` : ""}${now}</button>`;
        }).join("")}
      </div>
    </div>
    <div class="energy-labels outdoor-labels" style="--n: ${n}" aria-hidden="true">${bars.map((b, i) => `<span${i === shown ? ' class="on"' : ""}>${esc(outdoorLabel(view, b, n - 1 - i))}</span>`).join("")}</div>
    <div class="energy-total"><b>${esc(outdoorBarText(view, bars[shown], days))}</b><span class="temps-versus">${esc(outdoorPeriodText(view, bars))}</span></div>
    <p class="runs-note">${esc(tr("home.outdoor_bars_note"))}</p>`;
}

// ---------------------------------------------------------------- years --

// Each month of each year kept - the newest year first - as the mean of its
// days' means; under them each year's coldest and warmest day.
function outdoorYearsHtml(days) {
  const months = {};
  for (const d of Object.values(days)) (months[d.day.slice(0, 7)] = months[d.day.slice(0, 7)] || []).push(d);
  const years = [...new Set(Object.keys(months).map(k => k.slice(0, 4)))].sort().reverse();
  if (!years.length) return `<p class="sheet-note">${esc(tr("home.outdoor_no_period"))}</p>`;
  const thisMonth = dayValue(boardNow()).slice(0, 7);
  const names = tr("home.months_long");
  const cell = (year, m) => {
    const key = `${year}-${String(m + 1).padStart(2, "0")}`;
    const s = outdoorSummary(months[key] || []);
    if (!s || s.mean == null) return '<td class="none">–</td>';
    const few = key !== thisMonth && s.count < monthLength(Number(year), m) / 2;
    const title = tr("home.outdoor_years_cell", { month: names[m], year, mean: fmt(s.mean), min: fmt(s.min), max: fmt(s.max), n: s.count });
    return `<td${key === thisMonth ? ' data-current="true"' : few ? ' data-few="true"' : ""} title="${esc(title)}">${fmt(s.mean)}°</td>`;
  };
  const extreme = (year, key) => {
    const s = outdoorSummary(Object.values(days).filter(d => d.day.startsWith(year)));
    if (!s) return '<td class="none">–</td>';
    const iso = key === "min" ? s.min_day : s.max_day;
    return `<td>${fmt(s[key])}°<small>${Number(iso.slice(8))}/${Number(iso.slice(5, 7))}</small></td>`;
  };
  return `<div class="outdoor-years">
      <table>
        <thead><tr><th scope="col">${esc(tr("home.outdoor_years_month"))}</th>${years.map(y => `<th scope="col">${y}</th>`).join("")}</tr></thead>
        <tbody>${names.map((name, m) => `<tr><th scope="row">${esc(name.slice(0, 3))}</th>${years.map(y => cell(y, m)).join("")}</tr>`).join("")}</tbody>
        <tfoot>
          <tr><th scope="row">${esc(tr("home.outdoor_years_coldest"))}</th>${years.map(y => extreme(y, "min")).join("")}</tr>
          <tr><th scope="row">${esc(tr("home.outdoor_years_warmest"))}</th>${years.map(y => extreme(y, "max")).join("")}</tr>
        </tfoot>
      </table>
    </div>
    <p class="runs-note">${esc(tr("home.outdoor_years_note"))}</p>`;
}
