/* The home page (templates/home.html), in parts - see core.js. */
// A zone's comfort: its temperature and humidity said in one word on its
// card, and in a sheet a dial with where the room sits, why, and what
// the house can do about it.
"use strict";

// ------------------------------------------------------ the judgement --
// Not fixed numbers: the temperature against the one the zone's program
// wants now - 19.5° with a 20° target is fine, with a 22° one it's cool -
// and, with no target (off, away), against a comfortable room of the
// season. The humidity in the band the card's label already uses
// (core.js's humidityWord()), with a step above it: past 70% a winter
// wall starts to grow mould.
const COMFORT_RH = { dry: 30, humid: 60, veryHumid: 70 };
const COMFORT_DEFAULT = { heat: 20, cool: 26 };
// The band around the target, and how far out "too": heating, a degree
// under is cool and two and a half cold; cooling, the mirror image.
const COMFORT_BAND = {
  heat: { lo: -1, hi: 1.5, far: 2.5 },
  cool: { lo: -2, hi: 1, far: 2.5 },
};

function comfortOf(zone, climate) {
  if (!zoneReady(zone) || zone.temperature_c == null || !zone.sensor_ok) return null;
  const mode = climate.mode === "cool" ? "cool" : "heat";
  const hasTarget = zone.target_c != null && !climate.away_until;
  const base = hasTarget ? zone.target_c : COMFORT_DEFAULT[mode];
  const band = COMFORT_BAND[mode];
  const t = zone.temperature_c;
  const gap = Math.round((t - base) * 10) / 10;
  let temp = "ok";
  if (gap < band.lo) temp = gap < band.lo - band.far + 1 ? "cold" : "cool";
  else if (gap > band.hi) temp = gap > band.hi + band.far - 1 ? "hot" : "warm";
  const rh = zone.humidity_pct;
  let air = null;
  if (rh != null) air = rh < COMFORT_RH.dry ? "dry" : rh > COMFORT_RH.veryHumid ? "very_humid" : rh > COMFORT_RH.humid ? "humid" : "ok";
  // A bathroom (the zone left out of cooling, as the VMC's settings take
  // it): humid air after a shower is what it's for - said, never alarmed.
  const bathroom = zone.cool_enabled === false;
  const showery = bathroom && (air === "humid" || air === "very_humid");
  let level = "ok";
  if (temp !== "ok" || (air && air !== "ok")) level = "mild";
  if (temp === "cold" || temp === "hot" || (air === "very_humid" && !showery)) level = "warn";
  if (showery && temp === "ok") level = "mild";
  return { mode, base, hasTarget, gap, temp, air, rh, t, bathroom, showery, level, band };
}

function capitalized(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

// "Comfortable", "Cool and humid", "Dry air".
function comfortWord(c) {
  const parts = [];
  if (c.temp !== "ok") parts.push(tr(`home.comfort_t_${c.temp}`));
  if (c.air && c.air !== "ok") parts.push(tr(`home.comfort_rh_${c.air}`));
  if (!parts.length) return tr("home.comfort_ok");
  return capitalized(parts.length === 2 ? tr("home.comfort_and", { a: parts[0], b: parts[1] }) : parts[0]);
}

// ------------------------------------------------------------ the dial --
// The room's place on two axes, both scaled to its own comfortable band:
// across, the humidity (dry to humid); up, the temperature (cold to
// warm). Inside the inner square, comfortable; the ring is three bands
// out, and anything further sits on it.
const DIAL = { size: 240, inner: 34, outer: 102 };

function comfortPoint(c) {
  const mid = (c.band.lo + c.band.hi) / 2, half = (c.band.hi - c.band.lo) / 2;
  const rhMid = (COMFORT_RH.dry + COMFORT_RH.humid) / 2, rhHalf = (COMFORT_RH.humid - COMFORT_RH.dry) / 2;
  let u = c.rh == null ? 0 : (c.rh - rhMid) / rhHalf;
  let v = (c.gap - mid) / half;
  const reach = DIAL.outer / DIAL.inner;
  const length = Math.hypot(u, v);
  if (length > reach) { u *= reach / length; v *= reach / length; }
  const centre = DIAL.size / 2;
  return { x: centre + u * DIAL.inner, y: centre - v * DIAL.inner };
}

function comfortDial(c, others) {
  const centre = DIAL.size / 2, s = DIAL.size;
  const p = comfortPoint(c);
  // The other zones, faint, for where this one is among them - each name
  // on the side away from this zone's dot, so it isn't covered.
  const faint = others.map(o => {
    const q = comfortPoint(o.comfort);
    const left = q.x < p.x;
    return `<g class="cd-other"><circle cx="${q.x.toFixed(1)}" cy="${q.y.toFixed(1)}" r="5"/>
      <text x="${(q.x + (left ? -9 : 9)).toFixed(1)}" y="${(q.y + 4).toFixed(1)}" text-anchor="${left ? "end" : "start"}">${esc(o.name)}</text></g>`;
  }).join("");
  const label = (x, y, key, anchor = "middle") =>
    `<text class="cd-axis" x="${x}" y="${y}" text-anchor="${anchor}">${esc(tr(key))}</text>`;
  return `<svg class="comfort-dial" viewBox="-44 0 ${s + 88} ${s}" role="img"
      aria-label="${esc(tr("home.comfort_dial_said", { word: comfortWord(c) }))}">
    <circle class="cd-ring" cx="${centre}" cy="${centre}" r="${DIAL.outer}"/>
    <rect class="cd-zone" x="${centre - DIAL.inner}" y="${centre - DIAL.inner}" width="${DIAL.inner * 2}" height="${DIAL.inner * 2}" rx="${DIAL.inner * 0.8}"/>
    <path class="cd-cross" d="M${centre} ${centre - DIAL.outer}V${centre + DIAL.outer}M${centre - DIAL.outer} ${centre}H${centre + DIAL.outer}"/>
    ${label(centre, 12, c.mode === "cool" ? "home.comfort_axis_hot" : "home.comfort_axis_warm")}
    ${label(centre, s - 3, "home.comfort_axis_cold")}
    ${label(centre - DIAL.outer - 8, centre + 4, "home.comfort_axis_dry", "end")}
    ${label(centre + DIAL.outer + 8, centre + 4, "home.comfort_axis_humid", "start")}
    ${faint}
    <circle class="cd-dot" data-level="${c.level}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="9"/>
  </svg>`;
}

// On the card: the word, with a small dial of its own for the eye.
function comfortChipHtml(zone, climate) {
  const c = comfortOf(zone, climate);
  if (!c) return "";
  const p = comfortPoint(c);
  const k = 18 / DIAL.size;
  return `<button class="comfort" type="button" data-action="comfort" data-level="${c.level}">
      <svg class="comfort-mini" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="7.6"/>
        <circle class="cm-dot" cx="${(p.x * k).toFixed(1)}" cy="${(p.y * k).toFixed(1)}" r="2.6"/></svg>
      <span>${esc(comfortWord(c))}</span>${ICONS.next}
    </button>`;
}

// --------------------------------------------------------- the sheet --

// Why, in plain words: the temperature against what the program wants,
// the humidity against its band.
function comfortWhy(c, zone) {
  const out = [];
  const at = { gap: fmt(Math.abs(c.gap)), target: fmtTarget(c.base) };
  if (c.temp === "ok") out.push(tr(c.hasTarget ? "home.comfort_why_t_ok" : "home.comfort_why_t_ok_free", at));
  else if (c.gap < 0) out.push(tr(c.hasTarget ? "home.comfort_why_t_below" : "home.comfort_why_t_below_free", at));
  else out.push(tr(c.hasTarget ? "home.comfort_why_t_above" : "home.comfort_why_t_above_free", at));
  if (c.air === "ok") out.push(tr("home.comfort_why_rh_ok"));
  else if (c.air === "dry") out.push(tr("home.comfort_why_rh_dry"));
  else if (c.showery) out.push(tr("home.comfort_why_rh_shower"));
  else if (c.air) out.push(tr(c.mode === "cool" ? "home.comfort_why_rh_humid_cool" : "home.comfort_why_rh_humid_heat"));
  if (c.mode === "cool" && zone.dew_point_c != null && c.air && c.air !== "ok" && c.air !== "dry") {
    out.push(tr("home.comfort_why_dew", { value: fmt(zone.dew_point_c) }));
  }
  return out;
}

// What to do, only with what this house has: the valves (already on
// their way, or a change by hand on the card), the VMC, the dehumidifier.
// Each a sentence, some with a button to the card that does it.
function comfortAdvice(c, zone, climate) {
  const out = [];
  const hasVmc = !!document.getElementById("vmc-card");
  const hasDehum = c.mode === "cool" && !!climate.dehumidifier;
  const tooCold = c.temp === "cool" || c.temp === "cold", tooWarm = c.temp === "warm" || c.temp === "hot";
  if (c.mode === "heat" && tooCold) {
    if (zone.valve_on) out.push({ text: tr("home.comfort_do_heating") });
    else if (!c.hasTarget) out.push({ text: tr(climate.away_until ? "home.comfort_do_away" : "home.comfort_do_off") });
    else out.push({ text: tr("home.comfort_do_warmer") });
  }
  if (c.mode === "cool" && tooWarm) {
    if (zone.valve_on) out.push({ text: tr("home.comfort_do_cooling") });
    else out.push({ text: tr(c.hasTarget ? "home.comfort_do_cooler" : "home.comfort_do_off") });
  }
  // A bathroom warm after a shower: the shower's warmth, not the program's.
  if (c.mode === "heat" && tooWarm && !c.showery) out.push({ text: tr("home.comfort_do_lower") });
  if (c.showery) {
    out.push({ text: tr(hasVmc ? "home.comfort_do_shower_vmc" : "home.comfort_do_shower") });
  } else if (c.air === "humid" || c.air === "very_humid") {
    if (hasDehum) out.push({ text: tr("home.comfort_do_dehum"), go: "dehum", label: tr("home.comfort_go_dehum") });
    else if (hasVmc) out.push({ text: tr(c.mode === "cool" ? "home.comfort_do_vmc_cool" : "home.comfort_do_vmc"), go: "vmc", label: tr("home.comfort_go_vmc") });
    else out.push({ text: tr("home.comfort_do_air") });
  }
  if (c.air === "dry") out.push({ text: tr(hasVmc ? "home.comfort_do_dry_vmc" : "home.comfort_do_dry") });
  return out;
}

function openComfort(zone) {
  const climate = state.climate;
  const c = comfortOf(zone, climate);
  if (!c) return;
  const others = climate.zones.filter(z => z.name !== zone.name)
    .map(z => ({ name: z.name, comfort: comfortOf(z, climate) })).filter(o => o.comfort);
  const advice = comfortAdvice(c, zone, climate);
  const value = (label, html) => `<div class="cs-value"><small>${esc(label)}</small><b>${html}</b></div>`;
  const values = [
    value(tr("home.reading_temp"), `${fmt(c.t)}°`),
    c.rh != null ? value(tr("home.comfort_humidity"), `${fmt(c.rh, 0)}%`) : "",
    c.hasTarget ? value(tr("home.target"), fmtTarget(c.base)) : "",
    c.mode === "cool" && zone.dew_point_c != null ? value(tr("home.comfort_dew"), `${fmt(zone.dew_point_c)}°`) : "",
  ].join("");
  openSheet(tr("home.comfort_title", { zone: zone.name }), `
    <p class="sheet-lead comfort-lead" data-level="${c.level}"><b>${esc(comfortWord(c))}</b></p>
    <div class="comfort-sheet">
      ${comfortDial(c, others)}
      <div class="cs-values">${values}</div>
    </div>
    <div class="sheet-label">${esc(tr("home.comfort_why"))}</div>
    <ul class="sheet-points">${comfortWhy(c, zone).map(p => `<li>${esc(p)}</li>`).join("")}</ul>
    ${advice.length ? `<div class="sheet-label">${esc(tr("home.comfort_what"))}</div>
      <ul class="sheet-points">${advice.map(a => `<li>${esc(a.text)}</li>`).join("")}</ul>
      ${advice.filter(a => a.go).map(a => `<div class="sheet-actions"><button class="btn" type="button" data-comfort-go="${a.go}">${esc(a.label)}</button></div>`).join("")}` : ""}
    <p class="sheet-note">${esc(tr("home.comfort_note"))}</p>`, event => {
    const go = event.target.closest("button[data-comfort-go]");
    if (!go) return;
    closeSheet();
    const where = go.dataset.comfortGo === "vmc" ? document.getElementById("vmc-card") : document.getElementById("season-devices");
    if (where) where.scrollIntoView({ behavior: "smooth", block: "center" });
  });
}
