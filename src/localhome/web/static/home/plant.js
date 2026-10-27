/* The home page (templates/home.html), in parts - see core.js. */
// The plant beyond the zones: the dehumidifier, the season's devices,
// and the VMC.
"use strict";

// --------------------------------------------------------- the plant --
// What the season runs besides the zones - ventilation, the bypass
// valve, the dehumidifier - with what each does in this season.

function deviceByName(name) {
  return (state.devices ? state.devices.sensors : []).find(d => d.name === name);
}

// The VMC's speed: the level paired with a switch that has speed presets.
function vmcSpeedDevice() {
  return (state.devices ? state.devices.sensors : []).find(d => d.kind === "number" && d.paired_switch && (d.presets || []).length);
}

function renderPlant() {
  const tiles = [];
  const vmcSpeed = vmcSpeedDevice();
  if (vmcSpeed) tiles.push(vmcTile(vmcSpeed));
  const body = document.getElementById("plant-body");
  body.hidden = !tiles.length;
  morph(body, tiles.join(""));
  renderSeasonDevices();
}

function showPlant() {
  document.getElementById("plant").hidden = document.getElementById("plant-body").hidden
    && document.getElementById("season-devices").hidden;
}

// What this season has to be switched by hand (the dehumidifier, in
// summer): first in the plant's section, a switch each - always there, on
// or off; the top of the page says it when it runs. How the plant follows them - a valve
// opened for its water, a bypass moving with the season - is the admin
// panel's: nothing a home needs to see.
function renderSeasonDevices() {
  const host = document.getElementById("season-devices");
  const climate = state.climate;
  const rules = (climate && climate.season_effects && climate.season_effects.rules) || [];
  const cards = rules.filter(rule => rule[climate.mode] === "manual").map(rule => {
    const device = deviceByName(rule.switch);
    if (!device || !device.controllable) return "";
    if (climate.away_until) return awayDeviceCard(rule.switch);
    if (climate.dehumidifier && climate.dehumidifier.switch === rule.switch) return dehumidifierCard(rule.switch, climate.dehumidifier);
    const on = !!(state.readings[rule.switch] || {}).is_on;
    return `<div class="season-device" data-on="${on}">
        <span class="season-device-icon">${ICONS.drop}</span>
        <span class="season-device-text"><b>${esc(displayName(rule.switch))}</b><span>${esc(tr(on ? "home.on" : "home.off_state"))}</span></span>
        <button class="switch" type="button" role="switch" data-switch="${esc(rule.switch)}" aria-checked="${on}" aria-label="${esc(displayName(rule.switch))}"></button>
      </div>`;
  }).filter(Boolean);
  host.hidden = !cards.length;
  morph(host, cards.join(""));
  showPlant();
}

// The dehumidifier running, said at the top: its card is down in the plant.
function dehumidifierLine(climate) {
  const d = climate.dehumidifier;
  if (!d || d.on !== true) return "";
  return d.until_at ? tr("home.line_dehum_until", { until: untilText(boardDate(d.until_at)) }) : tr("home.line_dehum");
}

// Away: off until the way back - no switch, no durations (the way back
// is on the zones and the away chip).
function awayDeviceCard(name) {
  return `<div class="season-device" data-on="false" data-state="away">
      <span class="season-device-icon">${ICONS.drop}</span>
      <span class="season-device-text"><b>${esc(displayName(name))}</b><span>${esc(tr("home.away_device_off"))}</span></span>
    </div>`;
}

// The dehumidifier: on for a while or until the room is dry enough (the
// admin panel's thresholds and choices - services/dehumidifier.py); by
// itself only if the panel says so. With the air dry enough it can't be
// switched on, and says so: "off", the humidity, from when it would.
function dehumidifierCard(name, d) {
  const on = d.on === true;
  const dry = !on && d.humidity != null && d.humidity < d.start_from;
  const auto = d.auto || {};
  const until = d.until_at ? untilText(boardDate(d.until_at)) : "";
  const stop = Math.round(d.stop_below), start = Math.round(d.start_from);
  let note;
  if (on) {
    note = d.mode === "auto" ? tr("home.dehum_on_auto", { stop, until })
      : d.mode === "target" ? tr("home.dehum_on_target", { stop, until })
      : until ? tr("home.dehum_on_until", { until }) : tr("home.on");
  } else if (dry) {
    note = tr(auto.enabled ? "home.dehum_disabled_auto" : "home.dehum_disabled", { rh: d.humidity, start });
  } else {
    note = tr("home.off_state");
  }
  const humidity = d.humidity != null && !dry ? `<span class="season-device-rh">${ICONS.drop}${esc(tr("home.dehum_rh", { rh: d.humidity }))}</span>` : "";
  const label = h => (h < 1 ? tr("home.vmc_minutes", { m: Math.round(h * 60) }) : tr("home.vmc_hours", { h }));
  let controls = "";
  if (on) {
    controls = `<button class="chip" type="button" data-dehum-off>${esc(tr("home.dehum_off"))}</button>`;
  } else if (!dry) {
    controls = `<span class="season-device-for">${esc(tr("home.dehum_for"))}</span>`
      + d.durations.map(h => `<button class="chip" type="button" data-dehum-hours="${h}">${esc(label(h))}</button>`).join("")
      + (d.until_target ? `<button class="chip" type="button" data-dehum-target>${esc(tr("home.dehum_until_target", { stop }))}</button>` : "");
  }
  // On by itself: when, or why not now.
  let autoNote = "";
  if (auto.enabled && !on) {
    const window = auto.from_hour === 0 && auto.to_hour === 24 ? tr("home.dehum_auto_all_day")
      : tr("home.dehum_auto_window", { from: String(auto.from_hour).padStart(2, "0"), to: String(auto.to_hour).padStart(2, "0") });
    autoNote = auto.paused_until_at
      ? tr("home.dehum_auto_paused", { until: untilText(boardDate(auto.paused_until_at)) })
      : auto.used_today_seconds >= auto.max_hours_day * 3600 - 60 ? tr("home.dehum_auto_done", { h: auto.max_hours_day })
      : tr("home.dehum_auto_on", { start, window });
  }
  return `<div class="season-device season-device-dehum" data-on="${on}" data-state="${on ? "on" : dry ? "disabled" : "off"}">
      <span class="season-device-icon">${ICONS.drop}</span>
      <span class="season-device-text"><b>${esc(displayName(name))}</b><span>${esc(note)}</span>${humidity}</span>
      ${controls ? `<span class="season-device-controls">${controls}</span>` : ""}
      ${autoNote ? `<span class="season-device-auto">${ICONS.clock}<span>${esc(autoNote)}</span></span>` : ""}
    </div>`;
}

async function onSeasonDeviceClick(event) {
  const dehum = event.target.closest("button[data-dehum-hours], button[data-dehum-target], button[data-dehum-off]");
  if (dehum) {
    const host = dehum.closest(".season-device");
    asked.climate++;
    const done = await act(dehum, () => (dehum.hasAttribute("data-dehum-off") ? api("/api/dehumidifier/off", {})
      : dehum.hasAttribute("data-dehum-target") ? api("/api/dehumidifier/on", { until: "target" })
      : api("/api/dehumidifier/on", { hours: Number(dehum.dataset.dehumHours) })));
    if (done && state.climate) {
      state.climate.dehumidifier = done.dehumidifier;
      steady(renderSeasonDevices, host);
    }
    refreshClimate(host);
    refreshDevices(host);
    return;
  }
  const toggle = event.target.closest("button[data-switch]");
  if (toggle) await flipSwitch(toggle);
}

// A switch slides at once, and back if the board refuses. Not dimmed
// while it's asked - a second tap meanwhile is just ignored.
const switching = new Set();

async function flipSwitch(toggle) {
  const name = toggle.dataset.switch;
  if (switching.has(name)) return;
  const on = toggle.getAttribute("aria-checked") !== "true";
  const before = state.readings[name];
  toggle.setAttribute("aria-checked", String(on));
  state.readings[name] = Object.assign({}, before, { is_on: on });
  switching.add(name);
  asked.devices++;
  try {
    await api(`/api/sensors/${encodeURIComponent(name)}/${on ? "on" : "off"}`, {});
  } catch (e) {
    notify(errorText(e));
    state.readings[name] = before;
    toggle.setAttribute("aria-checked", String(!on));
  } finally {
    switching.delete(name);
  }
  await refreshDevices(toggle);
}

// ------------------------------------------------------------- the VMC --
// What it's doing at a glance - a dial filled to its speed, why it runs
// so and what comes next, today's program as a strip - then its switch,
// five levels and a fine slider, a speed for a while (boost, guests), one
// choice for the rest of the time: manual (the speed stays what you pick)
// or a ready-made program (services/vmc_schedule.py runs it as a week of
// hours), each with its day drawn - and how long the filters have left.

const VMC_ICONS = { min: "leaf", night: "moon", day: "sun", high: "breeze", boost: "wind" };
const VMC_BOOST_MINUTES = [30, 60, 120];
const VMC_GUESTS_MINUTES = 180;   // high, for guests: a dinner's length
const VMC_EXTEND_MINUTES = 30;    // a speed for a while made longer, a tap at a time
const VMC_MAX_MINUTES = 24 * 60;  // the longest the board takes (services/vmc_schedule.py)
// The dial: three quarters of a circle open at the bottom, as a car's.
const VMC_ARC = "M24.64 95.36A50 50 0 1 1 95.36 95.36";
// Ready-made programs in the unit's own speeds: [from hour, speed] per day.
const VMC_PROGRAMS = [
  { key: "day_night", all: [[0, "night"], [7, "day"], [22, "night"]] },
  { key: "work", weekdays: [[0, "night"], [6, "day"], [8, "night"], [17, "day"], [23, "night"]],
    weekend: [[0, "night"], [8, "day"], [23, "night"]] },
  { key: "peaks", all: [[0, "night"], [7, "boost"], [8, "day"], [19, "boost"], [20, "day"], [22, "night"]] },
];

function vmcLevelName(speed, value) {
  if (value === 0) return tr("home.vmc_off");
  const preset = speed.presets.find(p => p.value === value);
  return preset ? tr(`vmc_presets.${preset.key}`) : `${fmt(value, 0)}%`;
}

function vmcProgramWeek(program, speed) {
  const value = key => (speed.presets.find(p => p.key === key) || speed.presets.find(p => p.key === "day") || { value: 50 }).value;
  const day = steps => {
    const hours = new Array(24);
    steps.forEach(([from, key], i) => {
      const to = i + 1 < steps.length ? steps[i + 1][0] : 24;
      for (let h = from; h < to; h++) hours[h] = value(key);
    });
    return hours;
  };
  const week = {};
  DAYS.forEach((d, i) => { week[d] = day(program.all || (i < 5 ? program.weekdays : program.weekend)); });
  return week;
}

function sameWeek(a, b) {
  return !!a && !!b && DAYS.every(d => a[d].every((v, h) => v === b[d][h]));
}

function vmcRunning(speed) {
  const program = state.vmc && state.vmc.schedule;
  const programOn = !!(program && program.enabled && program.target === speed.name);
  const status = state.vmc && state.vmc.status;
  return { program, programOn, running: programOn && status && status.enabled ? status : null,
           boost: status && status.override && status.override.device === speed.name ? status.override : null };
}

// Which choice the board is on: "manual", a ready-made program, or
// "custom" - a week saved otherwise (by the API, or an older file).
function vmcChoice(speed) {
  const { program, programOn } = vmcRunning(speed);
  if (!programOn) return "manual";
  const match = VMC_PROGRAMS.find(p => sameWeek(program.week, vmcProgramWeek(p, speed)));
  return match ? match.key : "custom";
}

// A speed in a word: its level's name, or "Custom" between levels.
function vmcSpeedWord(speed, value) {
  return speed.presets.some(p => p.value === value) ? vmcLevelName(speed, value) : tr("home.vmc_custom");
}

// The dial's filled part for a speed - none at all when it's off.
function vmcArcStyle(value) {
  const v = Math.max(0, Math.min(100, value || 0));
  return `stroke-dasharray: ${v} 100; opacity: ${v > 0 ? 1 : 0}`;
}

// How far along the slider a speed is: from the slowest level to 100%.
function vmcFill(speed, value) {
  const lowest = Math.min(...speed.presets.map(p => p.value));
  return `${(Math.max(0, Math.min(1, (value - lowest) / (100 - lowest || 1))) * 100).toFixed(1)}%`;
}

// A speed's shade on today's strip and on the programs: the season's
// colour, stronger the faster it runs.
function vmcShade(value) {
  return `${value > 0 ? Math.round(24 + 0.76 * Math.min(100, value)) : 0}%`;
}

// The dial: filled up to the speed, a notch at each level, the speed in
// the middle and its level's name under it; off, the power sign.
function vmcGauge(speed, value, label, blank) {
  const notches = speed.presets.map(p => {
    const angle = (135 + 2.7 * p.value) * Math.PI / 180;
    return `<circle class="vmc-notch" cx="${(60 + 50 * Math.cos(angle)).toFixed(2)}" cy="${(60 + 50 * Math.sin(angle)).toFixed(2)}" r="1.8"/>`;
  }).join("");
  const shown = value != null;
  return `<div class="vmc-gauge" role="img" aria-label="${esc(shown ? tr("home.vmc_gauge", { pct: fmt(value, 0), level: label }) : label)}">
      <svg class="vmc-ring" viewBox="0 0 120 120" aria-hidden="true"><path class="vmc-ring-track" d="${VMC_ARC}"/>
        <path class="vmc-ring-fill" d="${VMC_ARC}" pathLength="100" style="${vmcArcStyle(shown ? value : 0)}"/>${notches}</svg>
      <span class="vmc-gauge-text" aria-hidden="true">${shown ? `<span class="vmc-gauge-value">${fmt(value, 0)}<small>%</small></span>`
        : `<span class="vmc-gauge-blank">${blank}</span>`}<span class="vmc-gauge-level">${esc(label)}</span></span>
    </div>`;
}

// One day of a program as a thin strip, a stretch per speed - the
// programs' previews.
function vmcMiniDay(day) {
  return `<span class="vmc-mini">${bands(day).map(b => `<span style="flex: ${b.end - b.start}; --lv: ${vmcShade(b.value)}"></span>`).join("")}</span>`;
}

// Today's program: each stretch in its shade, now a line across it, a
// speed held for a while (by hand, or timed) striped from now to its end.
function vmcToday(speed, week, holdUntil) {
  const now = boardNow();
  const day = week[DAYS[dayIndex(now)]];
  const at = hours => `${(hours / 24 * 100).toFixed(2)}%`;
  const hh = hour => `${String(hour).padStart(2, "0")}:00`;
  const nowHours = now.getUTCHours() + now.getUTCMinutes() / 60;
  const stretches = bands(day);
  const strip = stretches.map(b => `<span class="vmc-day-band" style="left: ${at(b.start)}; width: ${at(b.end - b.start)}; --lv: ${vmcShade(b.value)}"
      title="${esc(`${hh(b.start)}–${hh(b.end)} · ${vmcLevelName(speed, b.value)}`)}"></span>`).join("");
  let hold = "";
  if (holdUntil) {
    const end = boardDate(holdUntil);
    const today = end.toISOString().slice(0, 10) === now.toISOString().slice(0, 10);
    const endHours = today ? end.getUTCHours() + end.getUTCMinutes() / 60 : 24;
    if (endHours > nowHours) hold = `<span class="vmc-day-hold" style="left: ${at(nowHours)}; width: ${at(endHours - nowHours)}"></span>`;
  }
  const said = stretches.map(b => `${hh(b.start)}–${hh(b.end)} ${vmcLevelName(speed, b.value)}`).join(", ");
  return `<div class="vmc-day" role="img" aria-label="${esc(tr("home.vmc_today_aria", { stretches: said }))}">
      <span class="vmc-day-label">${esc(tr("home.vmc_today"))}</span>
      <span class="vmc-day-strip">${strip}${hold}<span class="vmc-day-now" style="left: ${at(nowHours)}"></span></span>
      <span class="vmc-day-hours" aria-hidden="true"><span>0</span><span>6</span><span>12</span><span>18</span><span>24</span></span>
    </div>`;
}

// Minutes left of a speed for a while.
function vmcMinutesLeft(boost) {
  return Math.max(1, Math.round((boardDate(boost.until).getTime() - boardNow().getTime()) / 60000));
}

function vmcTile(speed) {
  const reading = state.readings[speed.name] || {};
  const value = reading.value != null ? Math.round(reading.value) : null;
  const on = (state.readings[speed.paired_switch] || {}).is_on !== false;
  const { program, running, boost } = vmcRunning(speed);
  const fact = (icon, html, extra = "") => `<div class="fact${extra}"><span class="fact-icon">${icon}</span><span class="fact-text">${html}</span></div>`;
  // The fan turns faster as the speed goes up; its phase comes from the
  // clock, so a refresh that redraws the card doesn't make it jump.
  const turn = on && value > 0 ? Math.max(0.8, 90 / value) : 0;
  const spin = turn ? ` style="--turn: ${turn.toFixed(2)}s; --phase: -${((Date.now() / 1000) % turn).toFixed(2)}s"` : "";
  // Its name, and in one word where its speed comes from.
  const head = (word, tone, live, end) => `<div class="vmc-head">
      <span class="vmc-fan" data-spin="${!!turn}"${spin}>${ICONS.fan}</span>
      <span class="vmc-title"><span class="tile-name">${esc(displayName(speed.paired_switch))}${speed.simulated ? ` <span class="sim">${esc(tr("home.simulated"))}</span>` : ""}</span>
        <span class="state" data-tone="${tone}"${live ? " data-live" : ""}>${esc(word)}</span></span>
      ${end}
    </div>`;

  // Away: held at its night speed or off until "I'm back" - nothing to press.
  const held = state.vmc && state.vmc.status && state.vmc.status.away;
  if (held && held.device === speed.name) {
    const level = vmcLevelName(speed, held.value) + (held.value ? ` (${fmt(held.value, 0)}%)` : "");
    return `<div class="tile tile-wide vmc vmc-held" id="vmc-card" data-device="${esc(speed.name)}" data-on="${on}">
      <div class="vmc-live">
        ${head(tr("home.st_away"), "off", false, "")}
        <div class="vmc-now"><div class="vmc-now-main">
          ${vmcGauge(speed, on ? value : null, on && value != null ? vmcSpeedWord(speed, value) : tr("home.vmc_off"), ICONS.power)}
          <div class="vmc-facts">${fact(ICONS.suitcase, esc(tr(held.mode === "off" ? "home.vmc_away_off" : "home.vmc_away_level", { level })), " fact-rule")}</div>
        </div></div>
      </div>
    </div>`;
  }
  const until = at => (at ? untilText(boardDate(at)) : "");
  // A speed by its level's name, or "Custom 62%" between levels.
  const levelName = v => (speed.presets.some(p => p.value === v) ? vmcLevelName(speed, v) : tr("home.vmc_custom_level", { pct: fmt(v, 0) }));
  const auto = !boost && state.vmc && state.vmc.status && state.vmc.status.auto;
  // The program's next change: when, and to what.
  const next = running && running.until_at
    ? { when: whenText(boardDate(running.until_at)), level: levelName(running.next_value).toLowerCase() } : null;

  // Where the speed comes from: the program, a pick by hand (until the
  // program's next change, or for good with none), a speed for a while,
  // an automatic - or it's off, or the board can't reach it.
  const source = reading.ok === false ? "unreachable" : !on ? "off" : boost ? "timed" : auto ? "auto"
    : running && (value == null || running.value === value) ? "program" : running ? "hand" : "manual";
  const words = { program: "vmc_src_program", hand: "vmc_src_hand", manual: "vmc_src_manual", timed: "vmc_src_timed",
                  auto: "vmc_src_auto", off: "vmc_off", unreachable: "not_answering" };
  const tone = source === "unreachable" ? "warn" : source === "off" ? "off" : "active";

  // Why, and what comes next: a line or two beside the dial.
  const facts = [];
  if (source === "unreachable") {
    facts.push(fact(ICONS.power, esc(tr("home.vmc_unreachable"))));
  } else if (source === "off") {
    // Switched off by hand while the program runs it: back on at its
    // next change. Off by the program itself: what comes after.
    if (running && running.value > 0 && running.until_at) {
      facts.push(fact(ICONS.clock, esc(tr("home.vmc_back_on", { when: whenText(boardDate(running.until_at)) }))));
    } else if (running && running.value === 0) {
      facts.push(fact(ICONS.clock, esc(tr("home.vmc_off_program"))));
      if (next) facts.push(fact(ICONS.flow, esc(tr("home.vmc_next", next))));
    } else facts.push(fact(ICONS.power, esc(tr("home.vmc_off_hint"))));
  } else if (source === "timed") {
    facts.push(fact(ICONS.flow, esc(tr(running ? "home.vmc_then_program" : "home.vmc_then_before"))));
  } else if (source === "auto") {
    // An automatic change under way (services/vmc_auto.py): its reason.
    facts.push(fact(auto.reason === "cooling" ? ICONS.moon : ICONS.drop, esc(tr(`home.vmc_auto_${auto.reason}`, {
      zone: displayName(auto.zone || ""), rh: fmt(auto.humidity, 0), level: vmcLevelName(speed, auto.value).toLowerCase(),
      out: fmt(auto.reason === "cooling" ? auto.outdoor : auto.outdoor_dew), in: fmt(auto.reason === "cooling" ? auto.indoor : auto.indoor_dew) })),
      " fact-rule vmc-auto-note"));
  } else if (source === "program") {
    facts.push(next ? fact(ICONS.flow, esc(tr("home.vmc_next", next))) : fact(ICONS.clock, esc(tr("home.vmc_no_change"))));
  } else if (source === "hand") {
    facts.push(next ? fact(ICONS.clock, esc(tr("home.vmc_program_back", next))) : fact(ICONS.hand, esc(tr("home.vmc_stays"))));
  } else {
    facts.push(fact(ICONS.hand, `${esc(tr("home.vmc_stays"))}${program
      ? ` <button class="btn-link" type="button" data-vmc-open-plan>${esc(tr("home.vmc_pick_program"))}</button>` : ""}`));
  }
  // Today's program, while there's one - a pick by hand or a speed for a
  // while striped over it.
  const today = running && program.week
    ? vmcToday(speed, program.week, source === "timed" ? boost.until : source === "hand" ? running.until_at : null) : "";
  const label = !on ? tr("home.vmc_off") : source === "unreachable" ? tr("home.not_answering")
    : value != null ? vmcSpeedWord(speed, value) : "--";
  const gauge = vmcGauge(speed, on && source !== "unreachable" ? value : null, label, on ? "--" : ICONS.power);

  // Off and on: a switch of its own - the levels are speeds, and any of
  // them (or the slider) switches it back on.
  const power = deviceByName(speed.paired_switch);
  const powerSwitch = power && power.controllable
    ? `<button class="switch vmc-power" type="button" role="switch" data-vmc-power data-device="${esc(speed.paired_switch)}" aria-checked="${on}" aria-label="${esc(tr(on ? "home.vmc_power_off" : "home.vmc_power_on"))}"></button>`
    : "";
  // Each button names the device it sets: a light with its own level (a
  // Shelly dimmer on the Home tab) is another level paired with a switch.
  const buttons = speed.presets.map(p => `<button class="vmc-preset" type="button" data-vmc="${p.value}" data-device="${esc(speed.name)}" aria-pressed="${on && value === p.value}">
      ${ICONS[VMC_ICONS[p.key]] || ""}<span class="vmc-preset-name">${esc(tr(`vmc_presets.${p.key}`))}</span><span class="vmc-preset-value">${fmt(p.value, 0)}%</span></button>`).join("");
  // The fine slider: from the slowest level - the minimum set in the
  // admin panel - to 100%, a notch at each level; 0 is the switch's.
  // Sent when let go.
  const lowest = Math.min(...speed.presets.map(p => p.value));
  const shown = value != null ? Math.min(100, Math.max(lowest, value)) : lowest;
  const fine = `<div class="vmc-fine">
      <input type="range" min="${lowest}" max="100" step="1" value="${shown}" data-vmc-fine data-device="${esc(speed.name)}" style="--fill: ${vmcFill(speed, shown)}" aria-label="${esc(tr("home.vmc_fine"))}" aria-valuetext="${fmt(shown, 0)}%">
      <div class="vmc-fine-ticks" aria-hidden="true">${speed.presets.map(p => `<i style="left: ${vmcFill(speed, p.value)}"></i>`).join("")}</div>
      <div class="vmc-fine-scale" aria-hidden="true"><span>${fmt(lowest, 0)}%</span><span>100%</span></div>
    </div>`;

  // For a while: the maximum (a shower, cooking), or high for guests -
  // then back to the program, or to before. Under way: how long it has
  // left, half an hour more, or stop it now.
  const top = Math.max(...speed.presets.map(p => p.value));
  const high = (speed.presets.find(p => p.key === "high") || {}).value;
  const timedRow = (icon, text, minutes, level) => `<div class="vmc-boost"><span class="vmc-boost-icon">${icon}</span><span class="vmc-boost-text">${esc(text)}</span>
      <span class="vmc-boost-times">${minutes.map(m => `<button class="chip" type="button" data-vmc-boost="${m}" data-value="${level}" data-device="${esc(speed.name)}">${esc(m < 60 ? tr("home.vmc_minutes", { m }) : tr("home.vmc_hours", { h: m / 60 }))}</button>`).join("")}</span></div>`;
  let timed;
  if (boost) {
    const left = vmcMinutesLeft(boost);
    const longer = left + VMC_EXTEND_MINUTES <= VMC_MAX_MINUTES
      ? `<button class="chip" type="button" data-vmc-boost-extend>${esc(tr("home.vmc_boost_extend", { m: VMC_EXTEND_MINUTES }))}</button>` : "";
    timed = `<div class="vmc-boost" data-active="true"><span class="vmc-boost-icon">${ICONS.wind}</span>
        <span class="vmc-boost-text">${esc(tr("home.vmc_boost_running", { level: levelName(boost.value), until: until(boost.until) }))}
          <small>${esc(tr("home.vmc_boost_left", { left: fmtDuration(left / 60) }))}</small></span>
        <span class="vmc-boost-times">${longer}<button class="chip" type="button" data-vmc-boost-cancel>${esc(tr("home.vmc_boost_cancel"))}</button></span></div>`;
  } else {
    timed = timedRow(ICONS.wind, tr("home.vmc_boost_for", { level: vmcLevelName(speed, top) }), VMC_BOOST_MINUTES, top)
      + (high ? timedRow(ICONS.breeze, tr("home.vmc_guests", { level: vmcLevelName(speed, high) }), [VMC_GUESTS_MINUTES], high) : "");
  }

  // The rest of the time: one choice, manual or a program, behind one
  // line that opens - each program with its day drawn (weekdays and the
  // weekend when they differ). A week set elsewhere shows as "custom",
  // only while it's the one running.
  const choice = vmcChoice(speed);
  const choices = [
    { key: "manual", note: tr("home.vmc_tpl_manual_note") },
    ...VMC_PROGRAMS.map(p => ({ key: p.key, note: tr(`home.vmc_tpl_${p.key}_note`), week: vmcProgramWeek(p, speed) })),
    ...(choice === "custom" ? [{ key: "custom", note: tr("home.vmc_tpl_custom_note"), week: program.week }] : []),
  ];
  const preview = week => (week.mon.every((v, h) => v === week.sat[h]) ? vmcMiniDay(week.mon)
    : `<span class="vmc-mini-row"><small>${esc(tr("home.vmc_weekdays"))}</small>${vmcMiniDay(week.mon)}</span>
       <span class="vmc-mini-row"><small>${esc(tr("home.vmc_weekend"))}</small>${vmcMiniDay(week.sat)}</span>`);
  const programNote = running
    ? (running.until_at
        ? tr("home.vmc_program_now", { level: vmcLevelName(speed, running.value), until: until(running.until_at),
            next: vmcLevelName(speed, running.next_value).toLowerCase() })
        : tr("home.vmc_program_always", { level: vmcLevelName(speed, running.value) }))
    : tr("home.vmc_program_manual");
  const programs = program ? `<details class="vmc-plan" id="vmc-plan"${state.vmcPlanOpen ? " open" : ""}>
      <summary><span class="vmc-label">${esc(tr("home.vmc_program"))}</span>
        <span class="vmc-plan-name">${esc(tr(`home.vmc_tpl_${choice}`))}</span>${ICONS.chevron}</summary>
      <div class="vmc-programs" role="radiogroup" aria-label="${esc(tr("home.vmc_program"))}">
        ${choices.map(c => `<button class="vmc-template" type="button" role="radio" aria-checked="${c.key === choice}"
            data-vmc-choice="${c.key}" data-device="${esc(speed.name)}">
          <span class="vmc-template-name">${esc(tr(`home.vmc_tpl_${c.key}`))}</span>
          ${c.week ? `<span class="vmc-template-days" aria-hidden="true">${preview(c.week)}</span>` : ""}
          <span class="vmc-template-note">${esc(c.note)}</span></button>`).join("")}
      </div>
      <p class="vmc-program-note">${ICONS.clock}<span>${esc(programNote)}</span></p>
    </details>` : "";

  // The filters: how long they have left once a cleaning is recorded,
  // and when that was, in small; a reminder from two weeks before
  // they're due (services/vmc_filters.py).
  const filters = state.vmc && state.vmc.filters;
  const last = filters && filters.cleaned_on
    ? `<small>${esc(tr("home.vmc_filters_last", { date: new Date(`${filters.cleaned_on}T12:00:00Z`)
        .toLocaleDateString(LOCALE, { day: "numeric", month: "short", timeZone: "UTC" }) }))}</small>` : "";
  let filtersRow = "";
  if (filters && (filters.state === "soon" || filters.state === "due")) {
    const n = filters.days_left;
    filtersRow = `<div class="vmc-filters" data-state="${filters.state}">${ICONS.filter}<span>${esc(n > 1 ? tr("home.vmc_filters_soon", { n })
        : n === 1 ? tr("home.vmc_filters_tomorrow") : n === 0 ? tr("home.vmc_filters_today")
        : n === -1 ? tr("home.vmc_filters_yesterday") : tr("home.vmc_filters_due", { n: -n }))}${last}</span>
        <button class="chip" type="button" data-vmc-filters-cleaned>${esc(tr("home.vmc_filters_cleaned"))}</button></div>`;
  } else if (filters && filters.state === "ok" && filters.days_left != null) {
    const life = Math.max(0, Math.min(100, filters.days_left / (filters.interval_days || 1) * 100));
    filtersRow = `<div class="vmc-filter-life">${ICONS.filter}<span>${esc(tr("home.vmc_filters_ok", { n: filters.days_left }))}${last}</span>
        <span class="vmc-life" style="--life: ${life.toFixed(0)}%" aria-hidden="true"></span></div>`;
  }

  return `<div class="tile tile-wide vmc" id="vmc-card" data-device="${esc(speed.name)}" data-on="${on}" data-source="${source}">
    <div class="vmc-live">
      ${head(tr(`home.${words[source]}`), tone, source === "timed" || source === "auto", powerSwitch)}
      <div class="vmc-now">
        <div class="vmc-now-main">${gauge}<div class="vmc-facts">${facts.join("")}</div></div>
        ${today}
      </div>
      <div class="vmc-controls">
        <p class="vmc-label">${esc(tr("home.vmc_speed_heading"))}</p>
        <div class="vmc-presets" data-count="${speed.presets.length}" role="group" aria-label="${esc(tr("home.vmc_speed"))}">${buttons}</div>
        ${fine}
      </div>
      <div class="vmc-side"><p class="vmc-label">${esc(tr("home.vmc_for_a_while"))}</p>${timed}</div>
    </div>
    ${programs}${filtersRow}
  </div>`;
}

async function onPlantClick(event) {
  const target = event.target.closest("button");
  if (!target) return;
  const card = target.closest("#vmc-card");
  const speed = card && deviceByName(card.dataset.device);
  if (target.dataset.vmcChoice && target.getAttribute("aria-checked") === "true") return;
  if (target.dataset.switch) { await flipSwitch(target); return; }
  // "Pick a program": its line opened, and brought into view.
  if (target.hasAttribute("data-vmc-open-plan")) {
    const plan = document.getElementById("vmc-plan");
    if (plan) {
      plan.open = true;
      plan.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    return;
  }
  pressNow(target);
  busy(target, true);
  asked.devices++;
  try {
    const boosting = speed && vmcRunning(speed).boost;
    if (target.hasAttribute("data-vmc-power") && target.dataset.device) {
      // Off ends a timed speed, so it isn't switched back on later.
      const turnOn = target.getAttribute("aria-checked") !== "true";
      if (!turnOn && boosting) await api("/api/vmc-schedule/boost/cancel", {});
      await api(`/api/sensors/${encodeURIComponent(target.dataset.device)}/${turnOn ? "on" : "off"}`, {});
    } else if (target.dataset.vmcBoost && target.dataset.device) {
      await api("/api/vmc-schedule/boost", { device: target.dataset.device, value: Number(target.dataset.value),
                                             minutes: Number(target.dataset.vmcBoost) });
    } else if (target.hasAttribute("data-vmc-boost-extend") && boosting) {
      // Half an hour more: the same speed, asked again for what's left
      // and 30 minutes - the board keeps what to go back to.
      await api("/api/vmc-schedule/boost", { device: boosting.device, value: boosting.value,
                                             minutes: Math.min(VMC_MAX_MINUTES, vmcMinutesLeft(boosting) + VMC_EXTEND_MINUTES) });
    } else if (target.hasAttribute("data-vmc-boost-cancel")) {
      await api("/api/vmc-schedule/boost/cancel", {});
    } else if (target.hasAttribute("data-vmc-filters-cleaned")) {
      const done = await api("/api/vmc-filters/cleaned", {});
      if (state.vmc && done) state.vmc.filters = done.filters;
    } else if (target.dataset.vmc && target.dataset.device) {
      // A speed picked by hand ends a timed one, so it isn't undone later.
      if (boosting) await api("/api/vmc-schedule/boost/cancel", {});
      await api(`/api/sensors/${encodeURIComponent(target.dataset.device)}/set-value`, { value: Number(target.dataset.vmc) });
    } else if (target.dataset.vmcChoice === "manual") {
      // No program: the week stays saved, unused.
      await api("/api/vmc-schedule", { enabled: false });
    } else if (target.dataset.vmcChoice && speed) {
      // A program is its week, run on this VMC - picking one is what
      // switches it on and names the device it drives.
      const program = VMC_PROGRAMS.find(p => p.key === target.dataset.vmcChoice);
      await api("/api/vmc-schedule", { enabled: true, target: speed.name, week: vmcProgramWeek(program, speed) });
    } else {
      return;
    }
  } catch (e) {
    notify(errorText(e));
  } finally {
    busy(target, false);
  }
  await refreshDevices(target);
}

// The fine slider: the dial, its number, its level's name and the level
// it's on follow the finger; the speed is sent when it's let go.
function onVmcSlide(event) {
  const fine = event.target.closest("[data-vmc-fine]");
  if (!fine) return;
  const card = fine.closest("#vmc-card");
  const speed = deviceByName(fine.dataset.device);
  const v = Number(fine.value);
  fine.setAttribute("aria-valuetext", `${fmt(v, 0)}%`);
  card.dataset.sliding = "true";
  if (speed) fine.style.setProperty("--fill", vmcFill(speed, v));
  const fill = card.querySelector(".vmc-ring-fill");
  if (fill) fill.setAttribute("style", vmcArcStyle(v));
  const text = card.querySelector(".vmc-gauge-text");
  if (text && speed) text.innerHTML = `<span class="vmc-gauge-value">${fmt(v, 0)}<small>%</small></span><span class="vmc-gauge-level">${esc(vmcSpeedWord(speed, v))}</span>`;
  card.querySelectorAll(".vmc-preset[data-vmc]").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.vmc) === v)));
}

async function onVmcSlideEnd(event) {
  const fine = event.target.closest("[data-vmc-fine]");
  if (!fine) return;
  const speed = deviceByName(fine.dataset.device);
  asked.devices++;
  try {
    if (speed && vmcRunning(speed).boost) await api("/api/vmc-schedule/boost/cancel", {});
    await api(`/api/sensors/${encodeURIComponent(fine.dataset.device)}/set-value`, { value: Number(fine.value) });
  } catch (e) {
    notify(errorText(e));
  }
  const card = fine.closest("#vmc-card");
  if (card) delete card.dataset.sliding;
  fine.blur();     // the board's value shows again from the next refresh
  await refreshDevices(fine);
}

// A choice in a group lights up as it's tapped, the others go out - the
// board's answer (refreshDevices) confirms it or puts it back.
function pressNow(button) {
  const group = button.closest("[role=group], [role=radiogroup]");
  const attr = button.hasAttribute("aria-checked") ? "aria-checked" : button.hasAttribute("aria-pressed") ? "aria-pressed" : null;
  if (!group || !attr) return;
  group.querySelectorAll(`[${attr}]`).forEach(b => b.setAttribute(attr, String(b === button)));
}
