/* The home page (templates/home.html), in parts - see core.js. */
// Keeping the page current - the refreshes, the board not answering - the
// listeners, and the start. Loaded last: everything it wires up exists.
"use strict";

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
    noteValveChanges(state.climate, climate);
    state.climate = climate;
    climateZones = state.climate.zones || [];
    if (state.climate.mode && state.climate.mode !== climateMode) {
      climateMode = state.climate.mode;
      ensurePaintValueForMode();
    }
    state.failures = 0;
    const missing = state.climate.zones.filter(z => !state.weeks[z.name]);
    if (missing.length) await loadWeeks(missing.map(z => z.name));
    else await refreshWeeks();
    if (ticket !== asked.climate) return;
  } catch (e) {
    if (ticket !== asked.climate) return;
    state.failures += 1;
    if (!state.offlineSince) state.offlineSince = Date.now();
    // Not answering: said at once, with since when, the page's values
    // greyed as the last ones received - and asked again every few
    // seconds instead of every 20, so it's back as soon as the board is.
    const offline = document.getElementById("offline");
    steady(() => {
      offline.hidden = false;
      const gone = (Date.now() - state.offlineSince) / 1000;
      setText(offline, tr("home.offline_since", { age: gone < 60 ? tr("home.offline_moments") : fmtDuration(gone / 3600) }));
      document.body.dataset.offline = "true";
    }, anchor);
    clearTimeout(state.retryTimer);
    state.retryTimer = setTimeout(() => refreshClimate(), OFFLINE_RETRY_MS);
    return;
  }
  const wasOffline = !!state.offlineSince;
  state.offlineSince = null;
  clearTimeout(state.retryTimer);
  // Back: everything else asked again now, not at its next round.
  if (wasOffline) {
    refreshDevices();
    refreshProblems();
  }
  steady(() => {
    document.getElementById("offline").hidden = true;
    delete document.body.dataset.offline;
    renderHero();
    renderZones();
    renderPlant();
    renderHealth();
  }, anchor);
}

// The weeks as the board has them now - changed from the admin panel or
// another phone - for the line under the modes, the day's strip and the
// table. Seen: the admin cleared the week and an open page still said
// "then 24° tomorrow at 00:00". The table is left alone while it's being
// painted here, or holds hours of its own not saved yet.
async function refreshWeeks() {
  const mode = state.climate.mode;
  await Promise.all(state.climate.zones.map(async zone => {
    let week;
    try {
      week = (await api(`/api/climate/zones/${encodeURIComponent(zone.name)}/schedule?mode=${mode}`)).week;
    } catch (e) {
      return;
    }
    const weeks = state.weeks[zone.name] || (state.weeks[zone.name] = {});
    if (sameWeek(weeks[mode], week)) return;
    weeks[mode] = week;
    if (mode !== climateMode || !scheduleSavedWeeks[zone.name]) return;
    const unsaved = !sameWeek(scheduleWeeks[zone.name], scheduleSavedWeeks[zone.name]);
    if (!isPainting && !planTouchedRecently() && !unsaved && !sameWeek(scheduleSavedWeeks[zone.name], week)) {
      loadZoneSchedule(zone.name);
    }
  }));
}

// Two weeks the same hour by hour - whatever order their days came in.
function sameWeek(a, b) {
  if (!a || !b) return a === b;
  return DAYS.every(day => JSON.stringify(a[day]) === JSON.stringify(b[day]));
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

// A valve opened or closed since the last answer: when, for the minutes the
// page counts itself (zones.js's liveRun()), and the history read again
// once the board has a sample of it, not at its next 5-minute round.
function noteValveChanges(before, after) {
  if (!before) return;
  const was = Object.fromEntries((before.zones || []).map(z => [z.name, z.valve_on]));
  const now = boardEpochMs();
  let changed = false;
  (after.zones || []).forEach(zone => {
    if (!(zone.name in was) || !was[zone.name] === !zone.valve_on) return;
    changed = true;
    const open = state.liveRuns[zone.name];
    state.liveRuns[zone.name] = zone.valve_on ? [now, null] : [open ? open[0] : state.historyAt, now];
  });
  if (!changed) return;
  clearTimeout(state.historyTimer);
  state.historyTimer = setTimeout(refreshHistory, HISTORY_AFTER_CHANGE_MS);
}

async function refreshHistory() {
  if (!state.climate) return;
  state.historyAt = boardEpochMs();
  await Promise.all([...state.climate.zones.map(async zone => {
    try {
      const [temps, runs] = await Promise.all([
        api(`/api/sensors/${encodeURIComponent(zone.sensor)}/history?field=temperature_c&range=24h`),
        api(`/api/sensors/${encodeURIComponent(zone.valve)}/history?field=is_on&range=24h`),
      ]);
      state.history[zone.name] = { temps: temps.series.temperature_c || [], runs: runs.series.is_on || [] };
    } catch (e) { /* keep what we had */ }
  }), api("/api/climate/run-hours").then(runs => { state.runHours = runs; }).catch(() => {}),
  api("/api/climate/temps").then(temps => { state.temps = temps; }).catch(() => {})]);
  steady(() => { renderHero(); renderZones(); });
}

async function refreshDevices(anchor) {
  const ticket = ++asked.devices;
  let rangesMoved = false;
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
    // Today's lowest and highest - outside's, and each zone's under its
    // readings: those redrawn when they moved, not left to the next round.
    rangesMoved = JSON.stringify(all.day_ranges || {}) !== JSON.stringify(state.dayRanges || {});
    state.dayRanges = all.day_ranges || {};
    state.vmc = vmc;
  } catch (e) {
    return;
  }
  steady(() => {
    renderOutdoor();
    renderPlant();
    renderHouse();
    if (rangesMoved && state.climate) renderZones();
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
// A zone's hours, opened or closed, stay so across the card's redraws.
document.getElementById("zones").addEventListener("toggle", event => {
  if (event.target.matches && event.target.matches(".zone-runs")) state.runsOpen[event.target.dataset.zoneRuns] = event.target.open;
  if (event.target.matches && event.target.matches(".zone-temps")) state.tempsOpen[event.target.dataset.zoneTemps] = event.target.open;
  // Its details: remembered on this phone, and the week's plan (on a
  // phone, inside them - CSS) follows at once.
  if (event.target.matches && event.target.matches(".zone-more")) {
    const name = event.target.dataset.zoneMore;
    state.moreOpen[name] = event.target.open;
    try { localStorage.setItem(MORE_OPEN_KEY, JSON.stringify(state.moreOpen)); } catch (e) { /* kept for this visit */ }
    const card = event.target.closest(".zone");
    if (card) card.dataset.more = event.target.open ? "open" : "closed";
  }
}, true);
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
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !state.climate) return;
  refreshClimate();
  refreshDevices();
  refreshProblems();
  if (boardEpochMs() - state.historyAt > HISTORY_MS) refreshHistory();
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
