/* The home page (templates/home.html), in parts - see core.js. */
// A zone's card: what its valve does, its readings and facts, today's
// strip, its modes and the change by hand.
"use strict";

// ------------------------------------------------ what the valve does --
// No forecasts: the sensors report every couple of hours, too seldom to
// tell when a room will get there. What is certain is the thermostat's
// own rule (services/thermostat.py's decide()): while heating it opens
// below target - hysteresis and closes above target + hysteresis, and in
// between keeps doing what it was doing; cooling is the mirror image.

// The thermostat's own two numbers for this target (services/
// thermostat.py's thresholds()): it stops at the target, and opens again
// half a degree away - an economy target further on.
function valveRule(zone, climate) {
  // A valve that doesn't answer won't do what the rule says.
  if (zone.target_c == null || zone.temperature_c == null || zone.valve_opens_at == null
      || !zone.sensor_ok || !zone.valve_ok) return null;
  const cool = climate.mode === "cool";
  if (zone.valve_on) return tr(cool ? "home.runs_until_cool" : "home.runs_until_heat", { value: fmt(zone.valve_stops_at) });
  return tr("home.restarts_at", { value: fmt(zone.valve_opens_at) });
}

// Comfort or economy, as the palette calls it - with the same icon.
function tierChip(tier, mode) {
  if (!tier || typeof TIER_ICONS === "undefined") return "";
  return `<span class="tier-chip" data-tier="${tier}">${TIER_ICONS[tier === "comfort" ? mode : "economy"]}${esc(tr(`climate.tier_${tier}`))}</span>`;
}

// What the two levels do, in this season, with the thermostat's numbers:
// each level's on its box (a hover), both behind an "i" at the palette's
// end (a tap - a phone has no hover), out of the way until asked for.
function tierNumbers() {
  const climate = state.climate || {};
  const h = climate.hysteresis_c, e = climate.economy_tolerance_c;
  return h == null ? null : { comfort: fmt(h), economy: fmt(h + (e || 0)) };
}

function tierNoteHtml() {
  const numbers = tierNumbers();
  if (!numbers) return "";
  return `<p class="sched-tier-note" hidden>${esc(tr(climateMode === "cool" ? "climate.tier_note_cool" : "climate.tier_note_heat", numbers))}</p>`;
}

function wireTierInfo(plan) {
  const numbers = tierNumbers();
  const palette = plan.querySelector(".sched-palette");
  if (!numbers || !palette || plan.querySelector(".tier-info")) return;
  ["comfort", "economy"].forEach(tier => {
    const box = palette.querySelector(`.sched-tier-${tier}`);
    if (box) box.title = tr(`home.tier_tip_${tier}_${climateMode === "cool" ? "cool" : "heat"}`, numbers);
  });
  const hint = plan.querySelector(".hint-text");
  const note = plan.querySelector(".sched-tier-note");
  if (!hint) return;
  hint.insertAdjacentHTML("beforeend", ` <button class="tier-info" type="button" data-action="tier-info"
    aria-expanded="false" aria-label="${esc(tr("home.tier_info"))}" title="${esc(tr("home.tier_info"))}">i</button>`);
  if (note) hint.after(note);
}

// "0.8° below target", "at target" - to the tenth the sensor reports.
function gapText(zone) {
  if (zone.target_c == null || zone.temperature_c == null) return "";
  const gap = Math.round((zone.temperature_c - zone.target_c) * 10) / 10;
  if (gap === 0) return tr("home.at_target");
  return tr(gap < 0 ? "home.gap_below" : "home.gap_above", { value: fmt(Math.abs(gap)) });
}

// Its target reached: on the side the season wants it, within the
// hysteresis. Cooling, at or below the target - 24.3° with a 24° target is
// above it, even if the valve waits for 24.5° to open again (seen: it said
// "just right"); heating, at or above. To the tenth the sensor reports.
function targetReached(zone, climate) {
  if (zone.target_c == null || zone.temperature_c == null) return false;
  const h = climate.hysteresis_c || 0.5;
  const gap = Math.round((zone.temperature_c - zone.target_c) * 10) / 10;
  return climate.mode === "cool" ? gap <= 0 && gap >= -h : gap >= 0 && gap <= h;
}

// ----------------------------------------------------------- the zones --

function zoneStatus(zone, climate) {
  const mode = climate.mode;
  if (!zoneReady(zone)) return { tone: "off", text: tr(setupKey(zone, "home.st_setup")) };
  // Held open by another device (a dehumidifier drawing its water
  // through this valve) - even in a zone that sits out the cooling.
  if (mode === "cool" && !zone.cool_enabled) return { tone: "off", text: tr("home.st_excluded") };
  // Its Shelly not answering - unplugged, out of the Wi-Fi: whatever the
  // thermostat decides, the valve won't follow. Seen: a cooling target
  // below the room's, the valve offline, and the card said "resting". Red:
  // the zone can't be run - in the board's first minutes, only amber.
  if (zone.valve_ok === false) {
    return zone.valve_waiting ? { tone: "warn", text: tr("home.st_valve_waiting") }
      : { tone: "bad", text: tr("home.st_valve_offline"), valveDown: true };
  }
  if (zone.sensor_failsafe_active) return { tone: "warn", text: tr("home.st_failsafe") };
  if (zone.sensor_manual && !zone.sensor_ok) return { tone: "warn", text: tr("home.st_manual_waiting") };
  // The board just came up: the sensor hasn't reported since - not a fault.
  if (zone.sensor_waiting) return { tone: "off", text: tr("home.st_first_report") };
  if (!zone.sensor_ok && zone.temperature_c == null) return { tone: "warn", text: tr("home.st_waiting") };
  if (!zone.sensor_ok) return { tone: "warn", text: tr("home.st_stale") };
  if (climate.away_until) return { tone: "off", text: tr("home.st_away") };
  if (zone.target_c == null) return { tone: "off", text: tr("home.st_off") };
  if (zone.valve_on) return { tone: "active", text: tr(mode === "cool" ? "home.st_cooling" : "home.st_heating"), active: true };
  const reached = targetReached(zone, climate);
  return { tone: "ok", text: tr(reached ? "home.st_reached" : "home.st_idle"), reached };
}

function zoneId(name) {
  return "zone-" + name.replace(/[^a-z0-9]/gi, "-").toLowerCase();
}

function renderZones() {
  const climate = state.climate;
  const host = document.getElementById("zones");
  if (!climate || !climate.zones.length) {
    morph(host, `<div class="tile tile-wide"><div class="tile-name">${esc(tr("home.no_zones"))}</div>
      <div class="tile-note">${esc(tr("home.no_zones_note"))}</div><a class="link-btn" href="/climate-admin">${esc(tr("home.admin_link"))}</a></div>`);
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
  const excludedNow = climate.mode === "cool" && !zone.cool_enabled;
  // Out of the season, its valve only opens for another device (the
  // dehumidifier's water): not this zone running - no runs, no hours.
  const seen = state.history[zone.name] || { temps: [], runs: [] };
  const history = excludedNow ? { temps: seen.temps, runs: [] } : seen;
  const now = boardNow();
  const change = nextChange(week, now);
  card.dataset.active = status.active ? "true" : "false";
  // The card's colour (CSS): running, at its target, a problem - resting
  // inside its band and off stay plain.
  card.dataset.tone = status.tone === "ok" && !status.reached ? "rest" : status.tone;
  card.dataset.zone = zone.name;

  // The two readings side by side, large: the temperature, and the
  // humidity with what it means - today's lowest and highest under them.
  const temp = zone.temperature_c != null
    ? `<span class="now-value">${fmt(zone.temperature_c)}</span><span class="now-unit">°</span>`
    : `<span class="now-missing">--</span>`;
  const readings = `<div class="readings">
      <div class="readings-now">
        <div class="reading"><div class="reading-value">${temp}</div><div class="reading-label">${esc(tr("home.reading_temp"))}</div></div>
        ${zone.humidity_pct != null ? `<div class="reading reading-rh" data-rh="${rhLevel(zone.humidity_pct)}">
          <div class="reading-value"><span class="now-value">${fmt(zone.humidity_pct, 0)}</span><span class="now-unit">%</span></div>
          <div class="reading-label">${ICONS.drop}${esc(humidityWord(zone.humidity_pct))}</div></div>` : ""}
      </div>
      ${todayRangeHtml(zone)}
    </div>`;
  // The extras, one a row with its icon: on the right of the readings, or
  // under them in two columns on a narrow card. The target and what the
  // valve waits for in sight; the next change and the dew point in the
  // details.
  const fact = (icon, html, extra = "") => `<div class="fact${extra}"><span class="fact-icon">${icon}</span><span class="fact-text">${html}</span></div>`;
  const facts = [], moreFacts = [];
  const excluded = mode === "cool" && !zone.cool_enabled;
  // Away: nothing to reach - every zone off until the way back.
  if (zone.target_c != null && !excluded && !climate.away_until) {
    const gap = zone.sensor_ok ? gapText(zone) : "";
    const chip = tierChip(zone.tier, mode);
    const sub = chip || gap ? `<span class="fact-sub">${chip}${gap ? `<span class="trend">${esc(gap)}</span>` : ""}</span>` : "";
    facts.push(fact(ICONS.target, `<span>${esc(tr("home.target"))} <b>${fmtTarget(zone.target_c)}</b></span>${sub}`, " fact-target"));
    const rule = valveRule(zone, climate);
    if (rule) facts.push(fact(ICONS.flow, esc(rule), " fact-rule"));
  }
  if (mode === "cool" && zone.humidity_pct != null && zone.dew_point_c != null) {
    moreFacts.push(fact(ICONS.drop, esc(tr("home.dew_point_long", { value: fmt(zone.dew_point_c) }))));
  }

  const foot = [];
  // A typed value isn't a reading: no "updated a moment ago" under it.
  const age = zone.sensor_manual ? "" : fmtAge(zone.sensor_seconds_since_update ?? zone.sensor_last_value_age_seconds);
  // The sensor's, said as such - the hours run today after it are the
  // valve's. A value it hasn't confirmed since: the last one saved, and
  // how old. Its power in plain words: on battery (how full), or powered
  // (an H&T on USB-C - the server leaves its battery out).
  const sensorBits = [];
  if (age) sensorBits.push(tr(zone.sensor_ok ? "home.updated" : "home.last_saved", { age }));
  if (zone.external_power) sensorBits.push(tr("home.powered"));
  else if (zone.battery_pct != null) sensorBits.push(tr("home.on_battery", { pct: Math.round(zone.battery_pct) }));
  if (sensorBits.length) foot.push(esc(tr("home.sensor_foot", { what: sensorBits.join(", ") })));
  const hoursToday = excludedNow ? 0 : (state.onHours[zone.name] || {}).today;
  if (hoursToday) foot.push(esc(tr(mode === "cool" ? "home.ran_today_cool" : "home.ran_today", { time: fmtDuration(hoursToday) })));

  // At a glance: the readings, the target and the way to change it, and
  // what needs doing. The rest - the day's strip, the sensor, the hours,
  // the week's temperatures, on a phone the week's plan - in its details,
  // closed until asked for (remembered on this phone). A zone out of the
  // season: one line, its readings beside its name.
  // As they are on the card (a tap's "toggle" event comes a moment after
  // it, a redraw may come first); new, as last left here - open from the
  // start on a wide screen, where the week's plan beside the card leaves
  // the room for them.
  // A zone out of the season stays one line there too.
  const compact = excludedNow && zoneReady(zone);
  const shownMore = card.querySelector(".zone-more");
  const open = shownMore ? shownMore.open
    : zone.name in state.moreOpen ? !!state.moreOpen[zone.name] : !compact && window.matchMedia("(min-width: 960px)").matches;
  card.dataset.more = open ? "open" : "closed";
  card.dataset.compact = compact ? "true" : "false";
  const name = `<h2 class="zone-name">${esc(zone.name)}${zone.sensor_simulated ? ` <span class="sim">${esc(tr("home.simulated"))}</span>` : ""}${zone.sensor_manual ? ` <span class="sim">${esc(tr("home.manual"))}</span>` : ""}</h2>`;
  const inline = compact && zone.temperature_c != null
    ? `<span class="zone-inline">${fmt(zone.temperature_c)}°${zone.humidity_pct != null ? ` · ${fmt(zone.humidity_pct, 0)}%` : ""}</span>` : "";
  const main = `<div class="zone-main">
      ${readings}
      ${facts.length ? `<div class="zone-facts">${facts.join("")}</div>` : ""}
    </div>`;
  const quick = `<div class="quick">${quickHtml(zone, climate, change)}</div>`;
  morph(card.querySelector(".zone-live"), `
    <div class="zone-head">${name}${inline}
      <span class="state" data-tone="${status.tone}">${esc(status.text)}</span>
    </div>
    ${compact ? "" : main}
    ${valveNoteHtml(zone, status, mode)}
    ${sensorNoteHtml(zone)}
    ${compact ? "" : quick}
    ${zone.sensor_manual ? manualHtml(zone) : ""}
    <details class="zone-more" data-zone-more="${esc(zone.name)}"${open ? " open" : ""}>
      <summary><span class="runs-title">${esc(tr("home.details"))}</span>${ICONS.chevron}</summary>
      ${compact ? quick + main : ""}
      ${moreFacts.length ? `<div class="zone-facts zone-facts-more">${moreFacts.join("")}</div>` : ""}
      ${excludedNow ? "" : `<div class="ribbon">
        ${ribbonSvg(zone, week, history, mode)}
        <div class="ribbon-caption"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
        <div class="ribbon-readout">${legendHtml()}</div>
      </div>`}
      <div class="zone-foot">${foot.join(" · ")}</div>
      ${compact ? "" : runsHtml(zone)}
      ${tempsHtml(zone)}
    </details>`);
  wireRibbon(card, week, history, mode);
}

// Its sensor's batteries, said where they're seen: to change soon, or now
// (the admin panel's "battery low" too). On battery or powered is only
// said under the card: either is as good.
const BATTERY_SOON_PCT = 20;
const BATTERY_NOW_PCT = 10;
function sensorNoteHtml(zone) {
  if (zone.external_power || zone.battery_pct == null || zone.battery_pct > BATTERY_SOON_PCT) return "";
  const level = zone.battery_pct <= BATTERY_NOW_PCT ? "now" : "soon";
  const text = tr(level === "now" ? "home.battery_now" : "home.battery_soon", { pct: Math.round(zone.battery_pct) });
  return `<div class="sensor-note" data-level="${level}">${ICONS.battery}<span>${esc(text)}</span></div>`;
}

// Its valve not answering: what that means for the zone, and what to
// check - said in red inside the card, not only in its corner.
function valveNoteHtml(zone, status, mode) {
  if (!status.valveDown) return "";
  const age = fmtAge(zone.valve_seconds_since_update);
  const text = tr("home.valve_down", {
    action: tr(mode === "cool" ? "home.valve_down_cool" : "home.valve_down_heat"),
    since: age ? tr("home.valve_down_since", { age }) : "" });
  return `<div class="valve-note" role="alert">${ICONS.power}<span>${esc(text)}</span></div>`;
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
    await refreshClimate(form);
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
  return wallToEpoch(todayWall()) / 1000;
}

function todayWall() {
  const now = boardNow();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

// Where a moment falls on the strip: its hour on the wall clock, as the
// table's 24 hours are. The night October's clocks go back, the hour from
// 2 to 3 is drawn twice over the same place; March's skipped one has nothing.
function dayHourOf() {
  const midnight = todayWall().getTime();
  return ts => (wall(ts * 1000).getTime() - midnight) / 3600000;
}

// Where the valve opens again for this target - the far edge of its
// tolerance band (services/thermostat.py's thresholds(): the hysteresis,
// and the economy tolerance for an economy target).
function bandEdge(target, mode) {
  const climate = state.climate || {};
  const tier = typeof TIER_OF !== "undefined" && TIER_OF[mode] ? TIER_OF[mode](target) : null;
  const spread = (climate.hysteresis_c != null ? climate.hysteresis_c : 0.5)
    + (tier === "economy" ? climate.economy_tolerance_c || 0 : 0);
  return mode === "cool" ? target + spread : target - spread;
}

function ribbonScale(zone, week, history, mode) {
  const today = week ? week[DAYS[dayIndex(boardNow())]] : [];
  const targets = today.filter(v => v != null);
  const values = [...history.temps.map(p => p.value), ...targets, ...targets.map(v => bandEdge(v, mode))];
  if (zone.temperature_c != null) values.push(zone.temperature_c);
  if (zone.override && zone.override.target != null) values.push(zone.override.target, bandEdge(zone.override.target, mode));
  if (!values.length) return null;
  let lo = Math.min(...values) - 0.4, hi = Math.max(...values) + 0.4;
  if (hi - lo < 2) { const mid = (hi + lo) / 2; lo = mid - 1; hi = mid + 1; }
  return { lo, hi, y: v => RB.bottom - (v - lo) / (hi - lo) * (RB.bottom - RB.top), today };
}

function ribbonSvg(zone, week, history, mode) {
  const scale = ribbonScale(zone, week, history, mode);
  const x = hour => (hour / 24) * RB.w;
  const start = dayStartEpoch();
  const hourOf = dayHourOf();
  const now = boardNow();
  const nowHour = now.getUTCHours() + now.getUTCMinutes() / 60;
  const parts = [];
  for (const h of [6, 12, 18]) parts.push(`<line class="rb-grid" x1="${x(h)}" x2="${x(h)}" y1="0" y2="${RB.bottom}" vector-effect="non-scaling-stroke"/>`);
  if (scale) {
    bands(scale.today).forEach(b => {
      const color = b.value == null ? null : tempColor(b.value);   // the grid's colours
      if (color) parts.push(`<rect class="rb-block" x="${x(b.start)}" y="0" width="${x(b.end) - x(b.start)}" height="${RB.bottom}" fill="${color}"/>`);
    });
    // Each hour's tolerance band, under its target line.
    const band = (from, to, target) => {
      const edge = bandEdge(target, mode);
      const top = scale.y(Math.max(target, edge)), bottom = scale.y(Math.min(target, edge));
      const tier = TIER_OF[mode] ? TIER_OF[mode](target) : "comfort";
      parts.push(`<rect class="rb-band" data-tier="${tier}" x="${x(from)}" y="${top.toFixed(1)}" width="${(x(to) - x(from)).toFixed(1)}" height="${(bottom - top).toFixed(1)}"/>`);
    };
    bands(scale.today).forEach(b => { if (b.value != null) band(b.start, b.end, b.value); });
    let d = "";
    scale.today.forEach((v, h) => {
      if (v == null) return;
      const prev = h > 0 ? scale.today[h - 1] : null;
      d += `${prev == null ? "M" : "L"}${x(h)},${scale.y(v)} L${x(h + 1)},${scale.y(v)} `;
    });
    if (d) parts.push(`<path class="rb-target" d="${d}" vector-effect="non-scaling-stroke"/>`);
    // What's ahead of now is a plan, not what happened: veiled.
    if (nowHour < 24) parts.push(`<rect class="rb-future" x="${x(nowHour)}" y="0" width="${x(24) - x(nowHour)}" height="${RB.bottom}"/>`);
    if (zone.override && zone.override.target != null) {
      const until = zone.override.until ? boardDate(zone.override.until) : null;
      const endHour = until ? Math.min(24, (until - new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))) / 3600000) : 24;
      const y = scale.y(zone.override.target);
      if (endHour > nowHour) band(nowHour, endHour, zone.override.target);
      parts.push(`<path class="rb-override" d="M${x(nowHour)},${y} L${x(endHour)},${y}" vector-effect="non-scaling-stroke"/>`);
    }
    // A line again from 2 o'clock when the clock went back to it.
    const lines = [[]];
    history.temps.filter(p => p.ts >= start).forEach(p => {
      const h = hourOf(p.ts);
      const line = lines[lines.length - 1];
      if (line.length && h < line[line.length - 1][0]) lines.push([]);
      lines[lines.length - 1].push([h, p.value]);
    });
    const last = lines[lines.length - 1];
    if (zone.temperature_c != null && last.length) last.push([nowHour, zone.temperature_c]);
    lines.filter(line => line.length > 1).forEach(line => {
      const points = line.map(([h, v]) => `${x(h).toFixed(1)},${scale.y(v).toFixed(1)}`);
      parts.push(`<polyline class="rb-temp" points="${points.join(" ")}" vector-effect="non-scaling-stroke"/>`);
    });
  }
  runSpans(history.runs, start, hourOf).forEach(([from, to]) => {
    parts.push(`<rect class="rb-run" x="${x(from)}" y="${RB.runY}" width="${Math.max(2, x(to) - x(from))}" height="${RB.runH}"/>`);
  });
  parts.push(`<line class="rb-now" x1="${x(nowHour)}" x2="${x(nowHour)}" y1="0" y2="${RB.runY + RB.runH}" vector-effect="non-scaling-stroke"/>`);
  parts.push(`<line class="rb-cursor" x1="0" x2="0" y1="0" y2="${RB.runY + RB.runH}" vector-effect="non-scaling-stroke" visibility="hidden"/>`);
  const label = tr("home.ribbon_label", { name: zone.name });
  return `<svg viewBox="0 0 ${RB.w} ${RB.runY + RB.runH}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">${parts.join("")}</svg>`;
}

// The stretches the system ran today, as [from, to] hours on the strip
// (dayHourOf()): 5-minute samples joined into runs, a gap of one sample
// bridged - a stretch again from 2 when October's clock went back to it.
function runSpans(runs, start, hourOf) {
  const spans = [];
  runs.filter(p => p.ts >= start && p.value >= 0.5).forEach(p => {
    const h = hourOf(p.ts);
    const last = spans[spans.length - 1];
    if (last && h >= last[1] - 5 / 60 && h - last[1] <= 0.17) last[1] = Math.max(last[1], h + 5 / 60);
    else spans.push([h, h + 5 / 60]);
  });
  return spans;
}

// The four things the eye sees, in plain words: the room's temperature,
// the hours programmed (the dashed line their target), when it actually
// ran, and now. The tolerance band is said in words where it matters -
// "switches on above 25.5°" - not as one more thing to decode.
function legendHtml() {
  return `<span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><path d="M1 6 L6 3 L11 5 L17 2" class="lg-temp"/></svg>${esc(tr("home.lg_temp"))}</span>
    <span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><rect x="1" y="0.5" width="16" height="7" rx="1" class="lg-block"/><path d="M2 4 H16" class="lg-target"/></svg>${esc(tr("home.lg_planned"))}</span>
    <span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><rect x="1" y="2" width="16" height="4" rx="1" class="lg-run"/></svg>${esc(tr("home.lg_run"))}</span>
    <span class="lg"><svg viewBox="0 0 18 8" aria-hidden="true"><path d="M9 0 V8" class="lg-now"/></svg>${esc(tr("home.lg_now"))}</span>`;
}

// When a target really switches it on: past the far edge of its
// tolerance - above it in summer, below it in winter.
function switchesOnText(target, mode) {
  return tr(mode === "cool" ? "home.on_above" : "home.on_below", { edge: fmtTarget(bandEdge(target, mode)) });
}

// Touch or hover the strip: what happened (or is planned) at that time.
// Wired once - the strip is updated in place, not rebuilt - reading the
// day the card was last filled with.
function wireRibbon(card, week, history, mode) {
  card.ribbonDay = { week, history, mode };
  const svg = card.querySelector(".ribbon svg");
  if (!svg || svg.ribbonWired) return;    // none out of the season
  svg.ribbonWired = true;
  let hideTimer = null;
  const show = event => {
    clearTimeout(hideTimer);
    const { week, history, mode } = card.ribbonDay;
    const readout = card.querySelector(".ribbon-readout");
    const cursor = svg.querySelector(".rb-cursor");
    const today = week ? week[DAYS[dayIndex(boardNow())]] : null;
    const box = svg.getBoundingClientRect();
    const k = Math.max(0, Math.min(0.9999, (event.clientX - box.left) / box.width));
    const hour = k * 24;
    cursor.setAttribute("x1", k * RB.w);
    cursor.setAttribute("x2", k * RB.w);
    cursor.setAttribute("visibility", "visible");
    const ts = wallToEpoch(new Date(todayWall().getTime() + hour * 3600000)) / 1000;
    const time = hhmm(wall(ts * 1000));
    const bits = [time];
    const nowTs = wallToEpoch(boardNow()) / 1000;
    if (ts <= nowTs) {
      const near = history.temps.reduce((best, p) => (!best || Math.abs(p.ts - ts) < Math.abs(best.ts - ts) ? p : best), null);
      if (near && Math.abs(near.ts - ts) < 1800) bits.push(`${fmt(near.value)}°`);
      const run = history.runs.find(p => Math.abs(p.ts - ts) <= 180);
      if (run && run.value >= 0.5) bits.push(tr(mode === "cool" ? "home.was_cooling" : "home.was_heating"));
    }
    if (today) {
      const planned = today[Math.floor(hour)];
      bits.push(tr("home.planned", { target: fmtTarget(planned) }));
      if (ts > nowTs && planned != null) bits.push(switchesOnText(planned, mode));
    }
    readout.textContent = bits.join(" · ");
  };
  // A finger lifted leaves the strip at once (pointerleave follows
  // pointerup on a touch): what it touched stays a moment to be read -
  // a tap used to show nothing at all.
  const hide = event => {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      svg.querySelector(".rb-cursor").setAttribute("visibility", "hidden");
      morph(card.querySelector(".ribbon-readout"), legendHtml());
    }, event && event.pointerType === "touch" ? 4000 : 0);
  };
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
}

// -------------------------------------------------------- quick change --

// A zone changed by hand, said at the top of its card - not only at the
// bottom, where it was easy to miss: what it holds, until when, and the
// way back to its schedule.
function manualBannerHtml(zone, climate) {
  if (!zone.override || climate.away_until || (climate.mode === "cool" && !zone.cool_enabled)) return "";
  const until = overrideUntil(zone.override);
  const text = zone.override.target == null
    ? tr("home.manual_off", { until })
    : tr("home.manual_on", { target: fmtTarget(zone.override.target), until });
  // What the week has for this hour: what "back" means, said before it's tapped.
  const back = zone.scheduled_target_c === undefined ? tr("home.resume")
    : zone.scheduled_target_c === null ? tr("home.resume_to_off")
    : tr("home.resume_to", { target: fmtTarget(zone.scheduled_target_c) });
  return `<div class="manual-banner">${ICONS.hand}<span>${esc(text)}</span>
    <button class="btn" type="button" data-action="resume">${esc(back)}</button></div>`;
}

function quickHtml(zone, climate, change) {
  const mode = climate.mode;
  if (!zoneReady(zone)) {
    delete state.pending[zone.name];
    return `<div class="excluded-note setup-note">${ICONS.link}<span>${esc(tr(setupKey(zone, "home.setup_note")))}</span>
      <a class="btn" href="/climate-admin">${esc(tr("home.setup_link"))}</a></div>`;
  }
  if (mode === "cool" && !zone.cool_enabled) {
    return `<div class="excluded-note">${ICONS.snow}<span>${esc(tr("home.excluded_note"))}</span></div>`;
  }
  // Away: off until the way back, and nothing to change meanwhile - the
  // board refuses it too. The way back right here.
  if (climate.away_until) {
    delete state.pending[zone.name];
    return `<div class="excluded-note away-note">${ICONS.suitcase}<span>${esc(tr("home.away_zone_note"))}</span>
      <button class="btn" type="button" data-action="back">${esc(tr("home.away_back"))}</button></div>`;
  }
  // Its mode in sight under a title of its own, always in the same place -
  // the week, by hand, or off by hand: what's being chosen, while a change
  // is being chosen. The quick way, beside the hours of the week's plan.
  const pending = state.pending[zone.name];
  const held = zone.override;
  const current = pending ? (pending.target == null ? "off" : "manual")
    : !held ? "schedule" : held.target == null ? "off" : "manual";
  const modeButton = (key, label) => `<button type="button" role="radio" data-action="mode-${key}"
    aria-checked="${current === key}" title="${esc(tr(`home.mode_tip_${key}`))}">${esc(tr(label))}</button>`;
  // What the modes do: on each at a hover, all three behind the title's
  // "i" (a tap - a phone has no hover).
  const info = !!state.quickInfo[zone.name];
  const modes = `<div class="quick-title">${esc(tr("home.quick_title"))}
      <button class="tier-info" type="button" data-action="quick-info" aria-expanded="${info}"
        aria-label="${esc(tr("home.quick_info"))}" title="${esc(tr("home.quick_info"))}">i</button></div>
    ${info ? `<p class="quick-hint">${esc(tr("home.quick_hint"))}</p>` : ""}
    <div class="mode-row" role="radiogroup" aria-label="${esc(tr("home.mode_label"))}">
      ${modeButton("schedule", "home.mode_schedule")}${modeButton("manual", "home.mode_manual")}${modeButton("off", "home.mode_off")}
    </div>`;
  if (pending) {
    const options = DURATIONS.map(h => `<button type="button" data-action="duration" data-hours="${h}" aria-pressed="${pending.hours === h}">${esc(fmtDuration(h))}</button>`);
    if (change) {
      options.push(`<button type="button" data-action="duration" data-hours="next" aria-pressed="${pending.hours === "next"}">${esc(untilText(change.at))}</button>`);
    }
    options.push(`<button type="button" data-action="duration" data-hours="manual" aria-pressed="${pending.hours === "manual"}">${esc(tr("home.until_changed_button"))}</button>`);
    // A temperature, or off: both a manual change, for the time chosen -
    // off, only how long is left to choose.
    const off = pending.target == null;
    return `${modes}
      <div class="pending" data-off="${off}">
        <div class="pending-title">${esc(tr(off ? "home.pending_title_off" : "home.pending_title"))}</div>
        <div class="stepper" ${off ? "hidden" : ""}>
          <button type="button" data-action="step" data-delta="-0.5" aria-label="${esc(tr("home.colder"))}">−</button>
          <span class="stepper-value">${fmtTarget(pending.target)}</span>
          <button type="button" data-action="step" data-delta="0.5" aria-label="${esc(tr("home.warmer"))}">+</button>
        </div>
        ${off ? "" : `<div class="pending-label">${esc(tr("home.for_how_long"))}</div>`}
        <div class="segmented durations" role="group" aria-label="${esc(tr("home.for_how_long"))}">${options.join("")}</div>
        <div class="pending-actions">
          <button class="btn" type="button" data-action="cancel-pending">${esc(tr("home.cancel"))}</button>
          <button class="btn btn-primary" type="button" data-action="apply">${esc(tr("home.apply"))}</button>
        </div>
      </div>`;
  }
  // Following the week: what it does next - by hand, the banner above says
  // until when and the way back. A temperature is changed with "Manual":
  // its - and + are in the choice it opens.
  let follows = "";
  if (!held) {
    const next = change ? tr("home.next_change", { when: whenText(change.at), target: fmtTarget(change.target) }) : "";
    follows = [tr("home.mode_follows"), change && change.target != null ? `${next} · ${switchesOnText(change.target, mode)}` : next]
      .filter(Boolean).join(". ");
  }
  // By hand: what it holds, until when, and the way back - in the place
  // the week's line has.
  const line = follows ? `<p class="mode-line"><span>${esc(follows)}</span>
    <button class="btn-link" type="button" data-action="show-plan">${esc(tr("home.show_plan"))}</button></p>` : "";
  return `${modes}${held ? manualBannerHtml(zone, climate) : line}`;
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
  let action = button.dataset.action;
  if (action === "temps-pick") {
    const i = Number(button.dataset.pick);
    state.tempsPick[zone.name] = state.tempsPick[zone.name] === i ? undefined : i;
    steady(() => renderZones());
    return;
  }
  if (action === "runs-range" || action === "runs-pick") {
    if (action === "runs-range") {
      state.runsRange[zone.name] = Number(button.dataset.range);
      delete state.runsPick[zone.name];
    } else {
      const i = Number(button.dataset.pick);
      state.runsPick[zone.name] = state.runsPick[zone.name] === i ? undefined : i;
    }
    steady(() => renderZones());
    return;
  }
  if (action === "show-plan") {
    const more = card.querySelector(".zone-more");
    if (more && !more.open) more.open = true;
    card.dataset.more = "open";
    const plan = card.querySelector(".zone-plan");
    const toggle = plan.querySelector(".plan-toggle");
    if (toggle && getComputedStyle(toggle).display !== "none" && plan.dataset.open !== "true") {
      plan.dataset.open = "true";
      toggle.setAttribute("aria-expanded", "true");
    }
    plan.scrollIntoView({ behavior: "smooth", block: "start" });
    plan.classList.remove("is-shown");
    void plan.offsetWidth;          // the highlight again, on a second tap
    plan.classList.add("is-shown");
    return;
  }
  if (action === "quick-info") {
    state.quickInfo[zone.name] = !state.quickInfo[zone.name];
    steady(() => fillZone(card, zone, state.climate), button);
    return;
  }
  if (action === "tier-info") {
    const note = card.querySelector(".sched-tier-note");
    if (note) {
      note.hidden = !note.hidden;
      button.setAttribute("aria-expanded", String(!note.hidden));
    }
    return;
  }
  if (action === "toggle-plan") {
    const plan = card.querySelector(".zone-plan");
    const open = plan.dataset.open !== "true";
    plan.dataset.open = String(open);
    button.setAttribute("aria-expanded", String(open));
    return;
  }
  const pending = state.pending[zone.name];
  // Until the schedule's next change, as most thermostats do - or 4 hours
  // without one.
  const firstHours = () => nextChange((state.weeks[zone.name] || {})[state.climate.mode], boardNow()) ? "next" : 4;
  // The three modes: back to the schedule is "resume"; manual and off
  // open the choice a - or + opens - the temperature, or off, and for how
  // long - with that one picked.
  // While a change is being chosen they switch it between a temperature
  // and off, and the week drops it.
  if (action === "mode-schedule") {
    if (pending) delete state.pending[zone.name];
    if (zone.override) action = "resume";
    else if (!pending) return;
  }
  if (action === "mode-manual") {
    if (!pending) state.pending[zone.name] = { target: clampTarget(lastTarget(zone)), hours: firstHours() };
    else if (pending.target == null) pending.target = pending.last ?? clampTarget(lastTarget(zone));
  } else if (action === "mode-off") {
    if (!pending) state.pending[zone.name] = { target: null, last: clampTarget(lastTarget(zone)), hours: firstHours() };
    else if (pending.target != null) {
      pending.last = pending.target;
      pending.target = null;
    }
  } else if (action === "mode-schedule") {
    // The change dropped, nothing held: just shown again.
  } else if (action === "step" && pending) {
    pending.target = clampTarget(pending.target + Number(button.dataset.delta));
  } else if (action === "duration" && pending) {
    pending.hours = ["next", "manual"].includes(button.dataset.hours) ? button.dataset.hours : Number(button.dataset.hours);
  } else if (action === "cancel-pending") {
    delete state.pending[zone.name];
  } else if (action === "apply" && pending) {
    const body = pending.hours === "next" ? { target: pending.target, until: "next_change" }
      : pending.hours === "manual" ? { target: pending.target, until: "manual" }
      : { target: pending.target, hours: pending.hours };
    asked.climate++;
    const done = await act(button, () => api(`/api/climate/zones/${encodeURIComponent(zone.name)}/override`, body));
    const quick = card.querySelector(".quick");
    if (done) {
      delete state.pending[zone.name];
      // The card says it at once; the valve's answer follows.
      zone.override = done.override;
      zone.target_c = done.override.target;
      steady(() => { renderHero(); fillZone(card, zone, state.climate); }, quick);
    }
    refreshClimate(quick);
    return;
  } else if (action === "resume") {
    asked.climate++;
    await act(button, () => api(`/api/climate/zones/${encodeURIComponent(zone.name)}/override/cancel`, {}));
    refreshClimate(card.querySelector(".quick"));
    return;
  } else if (action === "back") {
    await comeBack(button);
    return;
  } else {
    return;
  }
  steady(() => fillZone(card, zone, state.climate), button);
}

// An action: its button waits for the board, and is itself again before
// anything is redrawn. Refused, or not answered: said, and null - the
// caller then shows what the board says (unanswered isn't undone: it may
// have been done all the same).
async function act(button, request) {
  busy(button, true);
  try {
    return await request();
  } catch (e) {
    notify(errorText(e));
    return null;
  } finally {
    busy(button, false);
  }
}

// A button waiting for the board: dimmed, not pressed twice.
function busy(button, on) {
  button.disabled = on;
  if (on) button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
}
