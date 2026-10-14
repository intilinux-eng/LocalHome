/* The home page (templates/home.html), in parts - see core.js. */
// A zone's details: its hourly table, the hours it ran, and its lowest
// and highest, today's and the last 7 days'.
"use strict";

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
  // "In use" on its heading while the zone follows it - by hand, the note
  // below says what holds instead.
  const inUse = locked ? "" : tr("home.plan_in_use");
  plan.querySelectorAll(".sched-season-label, .plan-toggle-label").forEach(label => {
    if (label.dataset.inUse !== inUse) label.dataset.inUse = inUse;
  });
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
  wireTierInfo(plan);
  // Not loaded (the board didn't answer): the next refresh tries again.
  loadZoneSchedule(zone.name).then(loaded => { if (!loaded) delete plan.dataset.mode; });
}

// ---------------------------------------------------- a zone's hours --
// How long its valve was open (services/run_hours.py): closed, the last
// 24 hours; open, the last 7 or 30 days as bars - the energy card's -
// a tap naming the day, its hours and its starts. Hours with the valve
// open: what the plant was asked for, not what it used - the heat meter
// counts that.

function runsDayName(iso, long) {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(LOCALE, long
    ? { weekday: "long", day: "numeric", month: "long" } : { weekday: "short" });
}

function runsHtml(zone) {
  const data = state.runHours && state.runHours.zones && state.runHours.zones[zone.name];
  if (!data) return "";
  const range = state.runsRange[zone.name] || 7;
  const days = state.runHours.days.slice(-range);
  // Today's bar and the last 24 hours with the minutes the history hasn't yet.
  const live = liveRunHours(zone);
  const hours = data.hours.slice(-range).map((h, i, all) => (i === all.length - 1 && live ? (h || 0) + live : h));
  const starts = data.starts.slice(-range);
  const known = hours.filter(h => h != null);
  const total = known.reduce((a, b) => a + b, 0);
  const top = Math.max(0.5, ...known);
  const title = (i) => {
    const day = runsDayName(days[i], true);
    if (hours[i] == null) return tr("home.runs_day_unknown", { day });
    const n = starts[i] || 0;
    return tr("home.runs_day", { day, time: fmtDuration(hours[i]),
      starts: n === 1 ? tr("home.runs_starts_one") : tr("home.runs_starts_many", { n }) });
  };
  // Under the bars: a week by its days, a month every week, and today.
  const label = (iso, i) => (i === days.length - 1 ? tr("home.runs_today")
    : range === 7 ? runsDayName(iso, false) : (days.length - 1 - i) % 7 === 0 ? String(Number(iso.slice(8))) : "");
  const pick = state.runsPick[zone.name];
  const picked = pick != null && pick < days.length ? pick : null;
  const line = picked != null ? title(picked)
    : known.length ? tr("home.runs_total", { days: range, time: fmtDuration(total), avg: fmtDuration(total / known.length) })
    : tr("home.runs_none");
  return `<details class="zone-runs" data-zone-runs="${esc(zone.name)}"${state.runsOpen[zone.name] ? " open" : ""}>
    <summary><span class="runs-title">${esc(tr("home.runs_title"))}</span>
      <span class="runs-24h">${esc(tr("home.runs_24h", { time: fmtDuration((data.last_24h || 0) + live) }))}</span>${ICONS.chevron}</summary>
    <div class="segmented energy-ranges" role="group" aria-label="${esc(tr("home.runs_title"))}">
      ${[7, 30].map(n => `<button type="button" data-action="runs-range" data-range="${n}" aria-pressed="${n === range}">${esc(tr("home.runs_days", { n }))}</button>`).join("")}
    </div>
    <div class="energy-bars" style="--n: ${days.length}">
      ${days.map((d, i) => `<button type="button" class="energy-bar" data-action="runs-pick" data-pick="${i}"
        style="--h: ${hours[i] == null ? 0 : Math.max(2, hours[i] / top * 100).toFixed(1)}%" ${hours[i] == null ? "disabled" : ""}
        data-current="${i === days.length - 1}" aria-pressed="${picked === i}" title="${esc(title(i))}" aria-label="${esc(title(i))}"></button>`).join("")}
    </div>
    <div class="energy-labels" style="--n: ${days.length}">${days.map((d, i) => `<span>${esc(label(d, i))}</span>`).join("")}</div>
    <div class="energy-total"><b>${esc(line)}</b></div>
    <p class="runs-note">${esc(tr("home.runs_note"))}</p>
  </details>`;
}

// ------------------------------------------ a zone's lowest and highest --
// Today's under the readings: a bar from the lowest to the highest, and
// where the room is now - when each came said on it (a hover, a screen
// reader) and in the fold. There, the last 7 days as the same bars, a
// day's own on a tap, and how today went against yesterday.

function epochHhmm(seconds) {
  return seconds == null ? "" : hhmm(wall(seconds * 1000));
}

// Today's lowest and highest so far, and when (epoch seconds) - the
// board's, kept as it records (/api/readings' day_ranges), and the
// reading itself: it may be newer than the last sample, as outside's.
function todayRange(zone) {
  const kept = ((state.dayRanges || {})[zone.sensor] || {}).temperature_c;
  if (!zone.sensor_ok || zone.sensor_manual) return kept || null;
  return withReading(kept, zone.temperature_c);
}

// A day's lowest and highest with a newer reading counted - a new lowest
// or highest is now's.
function withReading(kept, reading) {
  if (!kept || reading == null) return kept || null;
  const t = Math.round(reading * 10) / 10;
  const now = Math.round(wallToEpoch(boardNow()) / 1000);
  return { min: Math.min(kept.min, t), max: Math.max(kept.max, t),
           min_at: t < kept.min ? now : kept.min_at, max_at: t > kept.max ? now : kept.max_at };
}

// "Oggi: min 25,8° alle 06:10, max 26,4° alle 18:40" - without the times
// for a day kept before they were.
function dayRangeText(day, v) {
  const values = { day, min: fmt(v.min), max: fmt(v.max) };
  return v.min_at == null || v.max_at == null ? tr("home.temps_day_plain", values)
    : tr("home.temps_day", { ...values, min_at: epochHhmm(v.min_at), max_at: epochHhmm(v.max_at) });
}

function todayRangeHtml(zone) {
  const today = todayRange(zone);
  // "min 25,8° max 25,8°" says nothing the reading doesn't.
  if (!today || fmt(today.min) === fmt(today.max)) return "";
  return rangeBarHtml(today, zone.temperature_c, dayRangeText(tr("home.dr_today"), today));
}

// A day's lowest and highest at the ends of a bar, a ring on it where `t`
// is now - a zone's under its readings, outside's under its temperature;
// `said` the whole of it, for a hover and a screen reader.
function rangeBarHtml(today, t, said, extra = "") {
  const now = t == null ? ""
    : `<span class="tr-now" style="--at: ${(Math.min(1, Math.max(0, (t - today.min) / (today.max - today.min))) * 100).toFixed(1)}%"></span>`;
  return `<div class="today-range${extra ? ` ${extra}` : ""}" role="img" aria-label="${esc(said)}" title="${esc(said)}">
      <span class="tr-end"><small>${esc(tr("home.dr_min"))}</small><b>${fmt(today.min)}°</b></span>
      <span class="tr-track">${now}</span>
      <span class="tr-end"><small>${esc(tr("home.dr_max"))}</small><b>${fmt(today.max)}°</b></span>
    </div>`;
}

// Today against yesterday: the highest, then the lowest.
function againstYesterday(today, yesterday) {
  const versus = key => {
    const gap = Math.round((today[key] - yesterday[key]) * 10) / 10;
    const what = tr(key === "max" ? "home.dr_max_word" : "home.dr_min_word");
    return gap === 0 ? tr("home.dr_same", { what })
      : tr(gap > 0 ? "home.dr_higher" : "home.dr_lower", { what, d: fmt(Math.abs(gap)) });
  };
  return tr("home.dr_compare", { max: versus("max"), min: versus("min") });
}

function tempsHtml(zone) {
  if (!state.temps || !state.temps.days) return "";
  const days = state.temps.days, last = days.length - 1;
  const kept = (state.temps.zones || {})[zone.name] || [];
  // Today's live, as under the readings.
  const values = days.map((d, i) => (i === last && todayRange(zone)) || kept[i] || null);
  const known = values.filter(Boolean);
  if (!known.length) return "";
  const low = Math.min(...known.map(v => v.min)), high = Math.max(...known.map(v => v.max));
  // A little room above and below, so the steadiest day is still a bar.
  const span = Math.max(high - low, 1), bottom = low - span * 0.12, top = high + span * 0.12;
  const at = t => ((t - bottom) / (top - bottom) * 100).toFixed(1);
  // A line at each degree - each half on a steady week, fewer on a wide one.
  const step = top - bottom <= 2.5 ? 0.5 : top - bottom <= 5 ? 1 : top - bottom <= 10 ? 2 : 5;
  const lines = [];
  for (let t = Math.ceil(bottom / step) * step; t <= top; t += step) lines.push(t);
  const name = i => (i === last ? tr("home.dr_today") : runsDayName(days[i], true));
  const title = i => (values[i] ? dayRangeText(name(i), values[i]) : tr("home.runs_day_unknown", { day: name(i) }));
  const pick = state.tempsPick[zone.name];
  const picked = pick != null && pick <= last ? pick : null;
  // Under the bars the day picked, or today - with how it went against
  // yesterday.
  const shown = picked != null ? picked : values[last] ? last : null;
  const line = shown != null ? title(shown) : tr("home.temps_range", { min: fmt(low), max: fmt(high) });
  const versus = shown === last && values[last] && values[last - 1] ? againstYesterday(values[last], values[last - 1]) : "";
  // Today's bar has the room now on it, as the one under the readings.
  const today = values[last], t = zone.temperature_c;
  const now = today && t != null ? `<i class="temp-now" style="--now: ${at(Math.min(today.max, Math.max(today.min, t)))}%"></i>` : "";
  return `<details class="zone-temps" data-zone-temps="${esc(zone.name)}"${state.tempsOpen[zone.name] ? " open" : ""}>
    <summary><span class="runs-title">${esc(tr("home.temps_title"))}</span>
      <span class="runs-24h">${esc(tr("home.temps_summary", { min: fmt(low), max: fmt(high) }))}</span>${ICONS.chevron}</summary>
    <div class="temp-chart">
      <div class="temp-lines" aria-hidden="true">${lines.map(v => `<span style="--y: ${at(v)}%"><i>${fmt(v, Number.isInteger(v) ? 0 : 1)}°</i></span>`).join("")}</div>
      <div class="temp-bars" style="--n: ${days.length}" data-picked="${picked != null}">
        ${days.map((d, i) => `<button type="button" class="temp-bar" data-action="temps-pick" data-pick="${i}"
          ${values[i] ? `style="--lo: ${at(values[i].min)}%; --hi: ${at(values[i].max)}%"` : "disabled"}
          data-current="${i === last}" aria-pressed="${picked === i}" title="${esc(title(i))}" aria-label="${esc(title(i))}">${values[i] ? "<span></span>" : ""}${i === last ? now : ""}</button>`).join("")}
      </div>
    </div>
    <div class="energy-labels" style="--n: ${days.length}">${days.map((d, i) => `<span${picked === i ? ' class="on"' : ""}>${esc(i === last ? tr("home.runs_today") : runsDayName(d, false))}</span>`).join("")}</div>
    <div class="energy-total"><b>${esc(line)}</b>${versus ? `<span class="temps-versus">${esc(versus)}</span>` : ""}</div>
    <p class="runs-note">${esc(tr("home.temps_note"))}</p>
  </details>`;
}
