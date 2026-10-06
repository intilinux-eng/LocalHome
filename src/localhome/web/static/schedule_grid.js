/* The weekly schedule grid: the palette, painting hours with a drag,
   copying Monday to the week or the whole week to another zone, and saving
   only the hours that changed - on the home page's zone cards (home.js).

   It relies on what the page defines:
   tr(), notify(), confirmAction(), slug(), jsArg(), escapeHtml(),
   apiError(data), zoneExcluded(zone), updateZoneNext(zone) and the
   climateZones / climateMode it keeps up to date. */

function scheduleNow() {
  const server = window.ServerClock && window.ServerClock.now();
  if (server) return { dayKey: DAY_KEYS[(server.getUTCDay() + 6) % 7], hour: server.getUTCHours() };
  const local = new Date();
  return { dayKey: currentDayKey(local), hour: local.getHours() };
}

const DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
// Each season paints from its own range: heating setpoints live around
// 16-24C, cooling ones around 20-27C, and offering 16/18 while cooling (or
// 27 while heating) is just noise. 19 fills the 18-20 gap in heating.
// There's still one weekly grid per zone, shared by both seasons - this
// only changes what you can paint with and how it's colored.
const SCHEDULE_PRESETS = {
  heat: [null, 16, 18, 19, 20, 21, 22, 24],
  cool: [null, 20, 21, 22, 23, 24, 25, 26, 27],
};
const DEFAULT_PAINT_VALUE = { heat: 21, cool: 24 };

const scheduleWeeks = {};   // zone name -> working copy of its week grid
const saveTimers = {};      // zone name -> debounce timer for schedule saves
let paintValue = DEFAULT_PAINT_VALUE.heat;
let isPainting = false;
let strokeZone = null;      // the zone a stroke painted, until the pointer is released

function schedulePresets() {
  return SCHEDULE_PRESETS[climateMode] || SCHEDULE_PRESETS.heat;
}

// Keeps the selected brush valid across a season switch - a heating 18
// isn't on the cooling palette, and painting with an invisible value
// would be confusing.
function ensurePaintValueForMode() {
  if (!schedulePresets().includes(paintValue)) paintValue = DEFAULT_PAINT_VALUE[climateMode] ?? DEFAULT_PAINT_VALUE.heat;
}

// A continuous hue ramp put 4 of the 6 preset values (18/20/21/22) all in
// the same green band, hard to tell apart at a glance - see user feedback.
// These stops instead give each preset its own clearly-named color;
// values in between (e.g. old data outside the current presets)
// interpolate between the nearest two stops. Cooling gets its own stops:
// on the heating scale 24-27 would all come out the same red. Both run
// cold-colored to warm-colored, so a lower setpoint always reads "colder".
const TEMP_COLOR_STOPS = {
  heat: [
    { v: 16, h: 212, s: 70, l: 50 },
    { v: 18, h: 184, s: 65, l: 42 },
    { v: 19, h: 162, s: 58, l: 40 },
    { v: 20, h: 140, s: 55, l: 42 },
    { v: 21, h: 48, s: 75, l: 48 },
    { v: 22, h: 28, s: 80, l: 50 },
    { v: 24, h: 4, s: 70, l: 52 },
  ],
  // Blues for comfort - the deeper, the cooler the target - and a muted
  // green for economy (26 and up), so no summer setpoint reads as "hot".
  cool: [
    { v: 20, h: 224, s: 75, l: 44 },
    { v: 21, h: 216, s: 72, l: 47 },
    { v: 22, h: 208, s: 70, l: 48 },
    { v: 23, h: 200, s: 66, l: 47 },
    { v: 24, h: 194, s: 60, l: 45 },
    { v: 25, h: 188, s: 55, l: 42 },
    { v: 26, h: 150, s: 32, l: 40 },
    { v: 27, h: 140, s: 26, l: 36 },
  ],
};
// Each season's targets split into "comfort" and "economy" (less heating
// or cooling, lower consumption): heating is economy below 20°C, cooling
// from 26°C up. Shown as two boxes around the palette and next to the
// zone's current target, so the choice is explained, not just coloured.
const TIER_OF = {
  heat: v => (v < 20 ? "economy" : "comfort"),
  cool: v => (v >= 26 ? "economy" : "comfort"),
};

const TIER_ICONS = {
  // flame / snowflake for comfort (matches the season), leaf for economy
  heat: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>`,
  cool: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20"/><path d="M3.34 7 20.66 17"/><path d="M20.66 7 3.34 17"/></svg>`,
  economy: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/></svg>`,
  off: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/></svg>`,
};

function targetTier(value) {
  const tierOf = TIER_OF[climateMode];
  if (!tierOf || value == null) return null;
  return tierOf(value);
}

function swatchHtml(v) {
  return `<button class="sched-swatch${v === paintValue ? " active" : ""}" data-value="${v == null ? "" : v}"
                  style="${v == null ? "" : `background:${tempColor(v)}`}" onclick="selectPaintValue(this)">${v == null ? "✕" : v}</button>`;
}

function paletteHtml() {
  const values = schedulePresets();
  if (!TIER_OF[climateMode]) return values.map(swatchHtml).join("");
  const temps = values.filter(v => v != null);
  // Boxes in the palette's own order: economy first when heating (lower
  // targets), comfort first when cooling. "Off" gets a box of its own, so
  // the ✕ is labelled like the rest.
  const order = [...new Set(temps.map(targetTier))];
  const box = (name, swatches) => `
    <div class="sched-tier sched-tier-${name} sched-tier-${climateMode}">
      <span class="sched-tier-label">${TIER_ICONS[name === "comfort" ? climateMode : name]}${tr(`climate.tier_${name}`)}</span>
      <div class="sched-tier-swatches">${swatches.map(swatchHtml).join("")}</div>
    </div>`;
  const offBox = values.includes(null) ? box("off", [null]) : "";
  return offBox + order.map(name => box(name, temps.filter(v => targetTier(v) === name))).join("");
}

function tempColor(value) {
  const stops = TEMP_COLOR_STOPS[climateMode] || TEMP_COLOR_STOPS.heat;
  if (value <= stops[0].v) return `hsl(${stops[0].h}, ${stops[0].s}%, ${stops[0].l}%)`;
  for (let i = 0; i < stops.length - 1; i++) {
    const a = stops[i], b = stops[i + 1];
    if (value <= b.v) {
      const t = (value - a.v) / (b.v - a.v);
      return `hsl(${a.h + (b.h - a.h) * t}, ${a.s + (b.s - a.s) * t}%, ${a.l + (b.l - a.l) * t}%)`;
    }
  }
  const last = stops[stops.length - 1];
  return `hsl(${last.h}, ${last.s}%, ${last.l}%)`;
}

// One <span> per hour (plus a leading spacer matching the grid's day-label
// column), so the axis shares the exact same 25-column grid as .sched-grid
// below it and each label sits precisely above its own hour - a handful of
// labels spaced with plain flexbox justify-content:space-between would NOT
// line up correctly here, since space-between divides N items into N-1
// equal gaps regardless of which hour each one is meant to represent. Every
// span carries data-hour (so updateNowHighlight() can bold the current
// one), but only every 3rd hour gets a visible number, to keep the row
// readable instead of cramming in 24 tiny digits.
function hourAxisHtml() {
  let cells = `<span></span>`;
  for (let h = 0; h < 24; h++) {
    cells += `<span data-hour="${h}">${h % 3 === 0 ? h : ""}</span>`;
  }
  return cells;
}

// The schedule pane of a zone: season, palette, the grid and its copy
// buttons - the same markup on both pages.
function schedulePaneHtml(zone, id) {
  return `
        <div class="sched-season-label">${tr(`climate.schedule_heading_${climateMode}`)}</div>
        <div class="sched-palette">${paletteHtml()}</div>
        <p class="hint-text">${tr("climate.schedule_hint")}</p>
        <p class="sched-empty-hint" id="sched-empty-${id}" hidden>${tr(`climate.schedule_empty_${climateMode}`)}</p>

        <div class="sched-wrap">
          <div class="sched-hour-axis-wrap">
            <div class="sched-hour-axis">${hourAxisHtml()}</div>
            <span class="sched-hour-axis-end">24h</span>
          </div>
          <div class="sched-grid" id="sched-${id}"></div>
        </div>

        <div class="zone-actions">
          <span class="copy-label">${tr("climate.copy_label")}</span>
          <button class="range-btn" onclick="copyMondayToWeek(${jsArg(zone.name)}, '${id}', true)">${tr("climate.copy_weekdays")}</button>
          <button class="range-btn" onclick="copyMondayToWeek(${jsArg(zone.name)}, '${id}', false)">${tr("climate.copy_monday")}</button>
        </div>
        ${copyToZoneHtml(zone)}
`;
}

function applyCellStyle(cell, value) {
  cell.style.background = value == null ? "" : tempColor(value);
  cell.classList.toggle("off", value == null);
  cell.title = value == null ? tr("climate.target_off") : `${value}°C`;
  cell.dataset.value = value == null ? "" : value;
}

function currentDayKey(date) {
  return DAY_KEYS[(date.getDay() + 6) % 7]; // JS: 0=Sunday; DAY_KEYS: 0=Monday
}

function buildScheduleGrid(zoneId, week) {
  const grid = document.getElementById(`sched-${zoneId}`);
  if (!grid) return;
  // Already built: its hours are recoloured in place (a copied week,
  // another phone's edit) - nothing rebuilt, nothing blinks.
  const cells = grid.querySelectorAll(".sched-cell");
  if (cells.length === DAY_KEYS.length * 24) {
    cells.forEach(cell => applyCellStyle(cell, week[cell.dataset.day][Number(cell.dataset.hour)]));
    return;
  }
  grid.innerHTML = "";
  const dayLabels = tr("climate.days");
  const { dayKey: nowDayKey, hour: nowHour } = scheduleNow();

  // Each cell carries its place in the turned grid (--c, --r): hours across
  // on a wide card, the DOM's order; on a narrow one the stylesheet turns
  // it - hours down, days across - and the hour labels show.
  DAY_KEYS.forEach((dayKey, d) => {
    const label = document.createElement("span");
    label.className = "sched-day-label" + (dayKey === nowDayKey ? " now" : "");
    label.textContent = dayLabels[d] || dayKey;
    label.style.setProperty("--c", d + 2);
    grid.appendChild(label);

    for (let h = 0; h < 24; h++) {
      const cell = document.createElement("div");
      cell.className = "sched-cell" + (dayKey === nowDayKey && h === nowHour ? " now" : "");
      cell.dataset.day = dayKey;
      cell.dataset.hour = String(h);
      cell.style.setProperty("--c", d + 2);
      cell.style.setProperty("--r", h + 2);
      applyCellStyle(cell, week[dayKey][h]);
      grid.appendChild(cell);
    }
  });
  for (let h = 0; h < 24; h++) {
    const label = document.createElement("span");
    label.className = "sched-hour-label" + (h === nowHour ? " now" : "");
    label.textContent = String(h).padStart(2, "0");
    label.style.setProperty("--r", h + 2);
    grid.appendChild(label);
  }
  wireScheduleGrid(grid, zoneId);
}

// Strokes, on the grid itself: the cell under the pointer is looked up as
// it moves - a finger stays captured by the cell it started on, so a
// pointerenter on the next one never came and a drag painted one hour.
// On the turned grid (a phone) a finger paints on a tap or a sideways
// drag - the same hour across days - and an up-down swipe scrolls the
// page instead: painting on touch-down would paint whatever cell a scroll
// started on. A mouse or a pen paints at once, any direction.
let pendingTap = null;   // {zoneId, cell, x, y} - a finger down, not yet a tap or a drag
let strokeValue = null;  // what this stroke paints - decided by its first cell (startStroke)

function wireScheduleGrid(grid, zoneId) {
  if (grid.dataset.wired) return;
  grid.dataset.wired = "1";
  const cellAt = e => {
    const el = document.elementFromPoint(e.clientX, e.clientY);
    const cell = el && el.closest(".sched-cell");
    return cell && grid.contains(cell) ? cell : null;
  };
  grid.addEventListener("pointerdown", e => {
    const cell = e.target.closest(".sched-cell");
    if (!cell) return;
    const turned = getComputedStyle(grid).getPropertyValue("--turned").trim() === "1";
    if (e.pointerType === "touch" && turned) {
      pendingTap = { zoneId, cell, x: e.clientX, y: e.clientY };
      return;
    }
    e.preventDefault();
    isPainting = true;
    startStroke(zoneId, cell);
  });
  grid.addEventListener("pointermove", e => {
    if (pendingTap) {
      const dx = Math.abs(e.clientX - pendingTap.x), dy = Math.abs(e.clientY - pendingTap.y);
      if (dx >= 8 && dx > dy) {
        isPainting = true;
        startStroke(pendingTap.zoneId, pendingTap.cell);
        pendingTap = null;
      } else if (dy >= 8) {
        pendingTap = null;        // a scroll, not a stroke
      }
      return;
    }
    if (!isPainting) return;
    const cell = cellAt(e);
    if (cell) paintCell(zoneId, cell, strokeValue);
  });
  grid.addEventListener("pointerup", () => {
    if (pendingTap) {
      const tap = pendingTap;
      pendingTap = null;
      startStroke(tap.zoneId, tap.cell);
    }
  });
}

// What moves the layout around the grid waits for the end of the stroke:
// the empty-schedule note vanishing at the first cell used to shift the
// grid under a still-pressed pointer, and whatever cell slid under it
// got painted too - two cells for one click.
function endStroke() {
  isPainting = false;
  pendingTap = null;
  if (!strokeZone) return;
  const zoneName = strokeZone;
  strokeZone = null;
  updateEmptyScheduleHint(zoneName);
  const zone = climateZones.find(z => z.name === zoneName);
  if (zone) updateZoneNext(zone);
}

document.addEventListener("pointerup", endStroke);
document.addEventListener("pointercancel", endStroke);

// A tap or a stroke's first cell decides what it paints: an hour that
// already has the chosen temperature is cleared - a second tap undoes the
// first, no trip to the palette's OFF - anything else gets the chosen one.
// The rest of a stroke paints the same, never alternating along the way.
function startStroke(zoneId, cell) {
  const zone = climateZones.find(z => slug(z.name) === zoneId);
  const week = zone && scheduleWeeks[zone.name];
  if (!week) return;
  const current = week[cell.dataset.day][Number(cell.dataset.hour)];
  strokeValue = paintValue != null && current === paintValue ? null : paintValue;
  paintCell(zoneId, cell, strokeValue);
}

function paintCell(zoneId, cell, value) {
  const zone = climateZones.find(z => slug(z.name) === zoneId);
  const week = zone && scheduleWeeks[zone.name];
  if (!week) return;
  applyCellStyle(cell, value);
  week[cell.dataset.day][Number(cell.dataset.hour)] = value;
  scheduleSave(zone.name);
}

function selectPaintValue(btn) {
  paintValue = btn.dataset.value === "" ? null : Number(btn.dataset.value);
  document.querySelectorAll(".sched-swatch").forEach(b => b.classList.toggle("active", b.dataset.value === btn.dataset.value));
}

// The other zones this season's schedule can be copied onto. In cooling
// a bathroom is left out of the season entirely (zoneExcluded), so it
// isn't offered there - the button only shows up when there's really
// another grid to fill.
function copyTargets(zone) {
  return climateZones.filter(other => other.name !== zone.name && !zoneExcluded(other));
}

function copyToZoneHtml(zone) {
  const targets = copyTargets(zone);
  if (!targets.length) return "";
  return `
        <div class="zone-actions">
          <span class="copy-label">${tr("climate.copy_zone_label")}</span>
          ${targets.map(target => `
            <button class="range-btn" onclick="copyScheduleToZone(${jsArg(zone.name)}, ${jsArg(target.name)})">${escapeHtml(target.name)}</button>`).join("")}
        </div>`;
}

// The whole week, for the season on screen only - heating and cooling
// keep their own grids, and copying one over the other is never what
// someone means here.
async function copyScheduleToZone(fromName, toName) {
  const week = scheduleWeeks[fromName];
  if (!week) return;
  const season = tr(`climate.season_name_${scheduleWeekModes[fromName] || climateMode}`);
  const ok = await confirmAction(tr("climate.copy_zone_confirm", { from: fromName, to: toName, season }),
                                 { confirmLabel: tr("climate.copy_zone_button") });
  if (!ok) return;
  const copy = {};
  for (const day of DAY_KEYS) copy[day] = week[day].slice();
  scheduleWeeks[toName] = copy;
  scheduleWeekModes[toName] = scheduleWeekModes[fromName] || climateMode;
  buildScheduleGrid(slug(toName), copy);
  updateEmptyScheduleHint(toName);
  const zone = climateZones.find(z => z.name === toName);
  if (zone) updateZoneNext(zone);
  await saveSchedule(toName);
  updateNowHighlight();
  notify(tr("climate.copy_zone_done", { to: toName }), "ok");
}

function copyMondayToWeek(zoneName, zoneId, weekdaysOnly) {
  const week = scheduleWeeks[zoneName];
  if (!week) return;
  const monday = week.mon.slice();
  const targets = weekdaysOnly ? ["tue", "wed", "thu", "fri"] : ["tue", "wed", "thu", "fri", "sat", "sun"];
  targets.forEach(day => { week[day] = monday.slice(); });
  buildScheduleGrid(zoneId, week);
  scheduleSave(zoneName);
}

// Which season each loaded grid belongs to (heating and cooling are
// separate grids - see services/thermostat.py's SEASON_KEYS). Saves go to
// the season the grid was *loaded* for, never "whatever the season is by
// the time the debounced save fires": a season switch in between would
// otherwise file a winter grid as the summer one.
const scheduleWeekModes = {};

function updateEmptyScheduleHint(zoneName) {
  const hint = document.getElementById(`sched-empty-${slug(zoneName)}`);
  const week = scheduleWeeks[zoneName];
  if (hint && week) hint.hidden = Object.values(week).some(hours => hours.some(v => v != null));
}

function scheduleSave(zoneName) {
  if (isPainting) {
    strokeZone = zoneName;   // see endStroke()
  } else {
    updateEmptyScheduleHint(zoneName);
    const zone = climateZones.find(z => z.name === zoneName);
    if (zone) updateZoneNext(zone);
  }
  clearTimeout(saveTimers[zoneName]);
  saveTimers[zoneName] = setTimeout(() => saveSchedule(zoneName), 600);
}

// The week as the server last said it was, per zone - what an edit is
// compared against to know which hours to send.
const scheduleSavedWeeks = {};

function copyWeek(week) {
  const out = {};
  for (const day of DAY_KEYS) out[day] = week[day].slice();
  return out;
}

// Only the hours that changed go to the server, which merges them into
// the week as it is *now* and sends that back. Two phones editing the same
// zone used to each send their whole week, and the second save wiped the
// first one's changes; now both survive, and this page shows the other's
// too as soon as it saves.
async function saveSchedule(zoneName) {
  delete saveTimers[zoneName];
  const week = scheduleWeeks[zoneName];
  if (!week) return;
  const saved = scheduleSavedWeeks[zoneName];
  const cells = [];
  for (const day of DAY_KEYS) {
    for (let h = 0; h < 24; h++) {
      const value = week[day][h];
      if (!saved || saved[day][h] !== value) cells.push([day, h, value]);
    }
  }
  if (!cells.length) return;
  try {
    const res = await fetch(`/api/climate/zones/${encodeURIComponent(zoneName)}/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cells, mode: scheduleWeekModes[zoneName] || climateMode }),
      signal: timeLimit(15000),
    });
    const data = await res.json();
    if (!data.ok) {
      notify(`${tr("climate.save_failed")}: ${apiError(data)}`);
      return;
    }
    delete saveRetrying[zoneName];
    if (data.week) adoptServerWeek(zoneName, data.week);
  } catch (e) {
    // The board didn't answer: the hours stay painted here and are sent
    // again shortly, until it does - said once, not at every try.
    if (!saveRetrying[zoneName]) notify(tr("climate.save_failed"));
    saveRetrying[zoneName] = true;
    clearTimeout(saveTimers[zoneName]);
    saveTimers[zoneName] = setTimeout(() => saveSchedule(zoneName), SAVE_RETRY_MS);
  }
}

const saveRetrying = {};      // zone name -> a save is waiting for the board to answer
const SAVE_RETRY_MS = 15000;

// The merged week from the server becomes this page's: the grid is
// redrawn only if it differs (someone else's edit) and nobody is in the
// middle of painting on it, and edits made since the save was sent stay.
function adoptServerWeek(zoneName, serverWeek) {
  const local = scheduleWeeks[zoneName];
  scheduleSavedWeeks[zoneName] = copyWeek(serverWeek);
  if (saveTimers[zoneName] || isPainting) return;  // a newer local edit is on its way
  let differs = false;
  for (const day of DAY_KEYS) {
    for (let h = 0; h < 24; h++) {
      if (local[day][h] !== serverWeek[day][h]) differs = true;
    }
  }
  if (!differs) return;
  scheduleWeeks[zoneName] = copyWeek(serverWeek);
  buildScheduleGrid(slug(zoneName), scheduleWeeks[zoneName]);
  updateEmptyScheduleHint(zoneName);
  const zone = climateZones.find(z => z.name === zoneName);
  if (zone) updateZoneNext(zone);
}

// Saves any edit still waiting on its debounce right now, before a reload
// replaces the working copy it would have saved.
async function flushPendingScheduleSaves() {
  const pending = Object.keys(saveTimers);
  pending.forEach(zoneName => clearTimeout(saveTimers[zoneName]));
  await Promise.all(pending.map(saveSchedule));
}

// True once the grid is filled - the caller tries again later otherwise.
async function loadZoneSchedule(zoneName) {
  try {
    const res = await fetch(`/api/climate/zones/${encodeURIComponent(zoneName)}/schedule?mode=${encodeURIComponent(climateMode)}`,
                            { signal: timeLimit(10000) });
    const data = await res.json();
    if (!data.ok) return false;
    scheduleWeeks[zoneName] = data.week;
    scheduleSavedWeeks[zoneName] = copyWeek(data.week);
    scheduleWeekModes[zoneName] = data.mode || climateMode;
    buildScheduleGrid(slug(zoneName), data.week);
    updateEmptyScheduleHint(zoneName);
    const zone = climateZones.find(z => z.name === zoneName);
    if (zone) updateZoneNext(zone);
    return true;
  } catch (e) {
    return false;
  }
}

// A request the board never answers is given up after a while: a save
// that hung kept the hours painted on this page only, without a word.
function timeLimit(ms) {
  const controller = new AbortController();
  setTimeout(() => controller.abort(), ms);
  return controller.signal;
}

