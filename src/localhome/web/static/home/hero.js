/* The home page (templates/home.html), in parts - see core.js. */
// The top of the page - what the house is doing in a sentence - and the
// sheets its chips open: the season, the health, away.
"use strict";

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
  if (zone.valve_ok === false) {
    return tr(zone.valve_waiting ? "home.line_valve_waiting" : "home.line_valve_offline", { zone: zone.name });
  }
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
  // With a sensor, real or simulated, the tile opens its days, weeks,
  // months and years (home/outdoor.js); without one it's only words.
  const opens = !!device;
  chip.dataset.opens = String(opens);
  if (opens) {
    chip.setAttribute("role", "button");
    chip.setAttribute("aria-haspopup", "dialog");
    chip.tabIndex = 0;
    chip.title = tr("home.outdoor_open");
  } else {
    ["role", "aria-haspopup", "tabindex", "title"].forEach(name => chip.removeAttribute(name));
  }
  // A simulated one says so, as a zone's sensor does: an invented outdoor
  // temperature isn't read as the real one.
  const label = `<span class="outdoor-label">${ICONS.leaf}${esc(tr("home.outside"))}${device && device.simulated
    ? ` <span class="sim">${esc(tr("home.simulated"))}</span>` : ""}${opens ? `<span class="outdoor-open">${ICONS.next}</span>` : ""}</span>`;
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
  // Today's lowest and highest so far (the board's, kept as it records),
  // and when each came - the reading itself counted too: it may be newer
  // than the last sample (then it's now's).
  const today = withReading(((state.dayRanges || {})["Outdoor Sensor"] || {}).temperature_c, reading.temperature_c);
  let range = "";
  const low = today ? fmt(today.min) : "";
  const high = today ? fmt(today.max) : "";
  // "min 19,4° · max 19,4°" says nothing the reading doesn't.
  if (today && low !== high) {
    const said = today.min_at == null || today.max_at == null ? tr("home.outdoor_range", { min: low, max: high })
      : tr("home.outdoor_range_at", { min: low, max: high, min_at: epochHhmm(today.min_at), max_at: epochHhmm(today.max_at) });
    // The zones' bar: lowest to highest, a ring where it is now.
    range = rangeBarHtml(today, reading.temperature_c, said, "outdoor-range");
  }
  // Its batteries running down, said as a zone's are (zones.js's
  // sensorNoteHtml(), the same levels): it has no card of its own, and the
  // admin's "battery low" only comes at 10%.
  let battery = "";
  if (!reading.external_power && reading.battery_pct != null && reading.battery_pct <= BATTERY_SOON_PCT) {
    const level = reading.battery_pct <= BATTERY_NOW_PCT ? "now" : "soon";
    battery = `<span class="outdoor-battery" data-level="${level}">${ICONS.battery}${esc(tr(level === "now"
      ? "home.battery_now" : "home.battery_soon", { pct: Math.round(reading.battery_pct) }))}</span>`;
  }
  morph(chip, `${label}
    <span class="outdoor-value">${fmt(reading.temperature_c)}<small>°</small></span>${range}${rh}${battery}`);
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
  next: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>`,
  hand: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V11m0-6.5a1.5 1.5 0 0 1 3 0V11m0-4.5a1.5 1.5 0 0 1 3 0V13c0 4-2.5 7-6.5 7S5 17 5 13v-2a1.5 1.5 0 0 1 3 0"/></svg>`,
  battery: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="7" width="16" height="10" rx="2"/><path d="M21 10v4M6.5 10.5v3"/></svg>`,
  power: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v8"/><path d="M6.6 6.6a7.5 7.5 0 1 0 10.8 0"/></svg>`,
  fan: `<svg viewBox="0 0 24 24" aria-hidden="true"><g class="fan-blades">${[0, 120, 240].map(a => `<path transform="rotate(${a} 12 12)" d="M12 12C9.6 9.6 9.4 4.9 11.6 3.3c2.4-1.7 5.6.4 4.5 3.6-.8 2.2-2.4 3.7-4.1 5.1z"/>`).join("")}<circle cx="12" cy="12" r="1.9"/></g></svg>`,
};

// ------------------------------------------- season and health sheets --
// What the top's two chips mean, in plain words - the admin page, with
// its technical details, one link away.

// The year only when it isn't this one: "Thursday 15 October at 14:29".
function dateText(date) {
  const months = tr("home.months_long");
  const thisYear = date.getUTCFullYear() === boardNow().getUTCFullYear();
  return tr(thisYear ? "home.date_at_this_year" : "home.date_at", { day: tr(`home.day_long.${DAYS[dayIndex(date)]}`),
    date: date.getUTCDate(), month: Array.isArray(months) ? months[date.getUTCMonth()] : "", year: date.getUTCFullYear(),
    time: hhmm(date) });
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
