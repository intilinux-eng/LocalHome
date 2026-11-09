/* The "Impianto" tab: how each device is doing and the main settings of the
   installation, read only. Nothing here sends a command: the tab shows, the
   board's own admin panel changes. It reads only GET endpoints the board has
   already (MainForm.cs lists them), and it never shows a device id, a network
   name, an address or the raw text of an error: words take their place.
   Loaded before app.js: only definitions, used once it runs. */
"use strict";

const PLANT_SYSTEM_MS = 2 * 60000;    // system-status runs nmcli on the board: not more often
const PLANT_CONFIG_MS = 10 * 60000;   // the settings change seldom
const PLANT_HEALTH_MS = 10 * 60000;   // shelly-health costs about 1 s: read when its revision changes
const PLANT_CONFIG = {
  map: "/api/climate-admin/map",
  switches: "/api/climate-admin/extra-switches",
  outdoorSource: "/api/climate-admin/extra-sensors",
  vmcSource: "/api/climate-admin/extra-dimmers",
  settings: "/api/climate-admin/settings",
  speeds: "/api/climate-admin/vmc-speeds",
  vmcAuto: "/api/climate-admin/vmc-auto",
  dehum: "/api/dehumidifier/settings",
  broker: "/api/climate-admin/broker",
  led: "/api/climate-admin/led-matrix",
  backups: "/api/climate-admin/maintenance",
  record: "/api/climate-admin/dataset",
};
const LEVEL_ORDER = { bad: 0, warn: 1, todo: 2, ok: 3, idle: 4 };

const plant = {
  data: {},          // what each endpoint said last
  missing: {},       // endpoints an older board doesn't have (404)
  at: {},            // when each was read
  revision: null,    // shelly-health's revision when it was read
  busy: false,
  open: new Set(),   // the devices opened
  closed: new Set(), // the settings cards folded
  filter: "all",
  trends: {},        // a battery sensor's 7 days of battery and signal
};

function plantShown() {
  return document.body.dataset.tab === "plant";
}

// ------------------------------------------------------------- reading --

// A GET that may be missing on an older board: a 404 is remembered and not
// asked again until the next settings round; any other failure keeps the
// last answer.
async function readSoft(key, path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const res = await fetch(path, { cache: "no-store", signal: controller.signal });
    if (res.status === 404) {
      plant.missing[key] = true;
      plant.at[key] = Date.now();
      delete plant.data[key];
      return;
    }
    const data = await res.json().catch(() => null);
    if (res.ok && data && data.ok !== false) {
      plant.data[key] = data;
      plant.missing[key] = false;
      plant.at[key] = Date.now();
    }
  } catch (e) { /* the next reading tries again */ } finally {
    clearTimeout(timer);
  }
}

async function loadPlant(force) {
  if (plant.busy || document.hidden || !plantShown()) return;
  plant.busy = true;
  const due = (key, ms) => force || !plant.at[key] || Date.now() - plant.at[key] >= ms;
  try {
    const jobs = [readSoft("brokerState", "/api/climate-admin/broker-status"),
      readSoft("revision", "/api/climate-admin/shelly-health/revision")];
    if (due("system", PLANT_SYSTEM_MS)) jobs.push(readSoft("system", "/api/system-status"));
    Object.entries(PLANT_CONFIG).forEach(([key, path]) => { if (due(key, PLANT_CONFIG_MS)) jobs.push(readSoft(key, path)); });
    await Promise.all(jobs);
    const revision = plant.data.revision ? plant.data.revision.revision : null;
    if (force || revision !== plant.revision || due("health", PLANT_HEALTH_MS)) {
      await readSoft("health", "/api/climate-admin/shelly-health");
      plant.revision = revision;
    }
  } finally {
    plant.busy = false;
  }
  drawPlant();
}

// A battery sensor's last 7 days, read when its row is opened.
async function loadTrend(name) {
  const known = plant.trends[name];
  if (known && Date.now() - known.at < DETAIL_STALE_MS) return;
  plant.trends[name] = { at: Date.now(), loading: true };
  try {
    const series = await history(name, ["battery_pct", "wifi_rssi_dbm"], "7d");
    plant.trends[name] = { at: Date.now(), battery: clean(series.battery_pct), signal: clean(series.wifi_rssi_dbm) };
  } catch (e) {
    plant.trends[name] = { at: Date.now(), battery: [], signal: [] };
  }
  drawPlant();
}

// --------------------------------------------------------------- words --

function ago(seconds) {
  if (seconds == null) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return tr("pl_now");
  if (s < 3600) return tr("pl_min_ago", { m: Math.round(s / 60) });
  if (s < 86400) return tr("pl_h_ago", { h: Math.floor(s / 3600), m: String(Math.floor(s % 3600 / 60)).padStart(2, "0") });
  return tr("pl_d_ago", { d: Math.floor(s / 86400) });
}

function lasting(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 3600) return tr("pl_for_min", { m: Math.max(1, Math.round(s / 60)) });
  if (s < 86400) return tr("pl_for_h", { h: Math.floor(s / 3600), m: Math.floor(s % 3600 / 60) });
  return tr("pl_for_d", { d: Math.floor(s / 86400), h: Math.floor(s % 86400 / 3600) });
}

function when(ts) {
  if (ts == null) return "";
  const seconds = typeof ts === "number" ? ts : Date.parse(ts) / 1000;
  if (!Number.isFinite(seconds)) return "";
  return `${dayWords(wallDate(seconds), { day: "numeric", month: "short" })}, ${hhmm(seconds)}`;
}

function onDay(iso) {
  return iso ? isoDay(String(iso).slice(0, 10), { day: "numeric", month: "short", year: "numeric" }) : "";
}

function bytes(value) {
  if (value == null) return "";
  if (value >= 1e9) return `${fmt(value / 1e9, 1)} GB`;
  return `${fmt(value / 1e6, 0)} MB`;
}

function reasonWord(reason) {
  const word = TEXTS[LANG][`pl_reason_${reason.code}`];
  return word || tr("pl_attention");
}

// The device's own name for a role, as the residents' pages call it.
function friendly(name) {
  const rules = (now.climate && now.climate.season_effects && now.climate.season_effects.rules) || [];
  const rule = rules.find(r => r.switch === name);
  if (rule && rule.role === "bypass_valve_device_id") return tr("pl_bypass");
  if (rule && rule.role === "dehumidifier_device_id") return tr("dehum");
  return name;
}

// ----------------------------------------------------------- the model --

// Every device of the house in its place: each zone's sensor and valve, the
// plant, outside, energy, shutters, the rest - with what's still to be set up.
function plantGroups() {
  const climate = now.climate || { zones: [] };
  const sensors = (now.devices && now.devices.sensors) || [];
  const healthList = (plant.data.health && plant.data.health.devices) || [];
  const used = new Set();
  const byName = name => sensors.find(d => d.name === name);
  const healthOf = name => healthList.find(h => (h.names || [h.name]).includes(name));
  const row = (name, label, extra) => {
    const device = byName(name);
    if (device) used.add(name);
    return Object.assign({ key: name, name, label, device, kind: device && device.kind, simulated: !!(device && device.simulated),
                           reading: now.readings[name], health: healthOf(name) }, extra || {});
  };
  const todo = (now.climate && now.climate.to_configure) || [];
  const todoRow = t => ({ key: `todo:${t.role}:${t.name}`, name: t.name, label: tr(`pl_role_${t.role}`), todo: true });
  const groups = [];

  climate.zones.forEach(z => {
    const rows = [];
    rows.push(z.sensor_configured === false || !z.sensor ? todoRow({ role: "sensor", name: `${z.name} Sensor` })
      : row(z.sensor, tr("pl_role_sensor"), { role: "sensor" }));
    rows.push(z.valve_configured === false || !z.valve ? todoRow({ role: "valve", name: `${z.name} Valve` })
      : row(z.valve, tr("pl_role_valve"), { role: "valve" }));
    groups.push({ key: `zone:${z.name}`, title: tr("pl_zone", { name: z.name }), rows });
  });

  const plantRows = [];
  const speed = vmcSpeed();
  if (speed) {
    used.add(speed.name);
    plantRows.push(row(speed.paired_switch, tr("vmc"), { role: "vmc", speed: now.readings[speed.name],
                                                          health: healthOf(speed.paired_switch) || healthOf(speed.name) }));
  }
  const rules = (climate.season_effects && climate.season_effects.rules) || [];
  rules.forEach(r => {
    if (byName(r.switch) && !used.has(r.switch)) {
      plantRows.push(row(r.switch, r.role === "bypass_valve_device_id" ? tr("pl_bypass") : tr("dehum"),
                         { role: r.role === "bypass_valve_device_id" ? "bypass" : "dehum", normallyOpen: !!r.normally_open }));
    }
  });
  todo.filter(t => ["bypass", "dehumidifier", "vmc"].includes(t.role)).forEach(t => plantRows.push(todoRow(t)));
  if (plantRows.length) groups.push({ key: "plant", title: tr("pl_group_plant"), rows: plantRows });

  const outside = [];
  if (byName(OUTDOOR)) outside.push(row(OUTDOOR, tr("pl_role_outdoor"), { role: "sensor" }));
  todo.filter(t => t.role === "outdoor").forEach(t => outside.push(todoRow(t)));
  if (outside.length) groups.push({ key: "outside", title: tr("outside"), rows: outside });

  const energy = sensors.filter(d => !used.has(d.name) && (d.kind === "power_meter"
    || (now.readings[d.name] && now.readings[d.name].power_w != null)))
    .map(d => row(d.name, d.kind === "power_meter" ? tr("pl_role_meter") : tr("pl_role_plug"), { role: d.kind === "power_meter" ? "meter" : "plug" }));
  if (energy.length) groups.push({ key: "energy", title: tr("pl_group_energy"), rows: energy });

  const coverNames = (now.devices && now.devices.covers) || [];
  if (coverNames.length) {
    groups.push({ key: "covers", title: tr("covers"), rows: coverNames.map(name => ({
      key: `cover:${name}`, name, label: coverLabel(name, coverNames), cover: now.covers[name] || {}, role: "cover" })) });
  }

  const rest = sensors.filter(d => !used.has(d.name) && !(d.kind === "number" && d.paired_switch && used.has(d.paired_switch)))
    .map(d => row(d.name, d.name, { role: "other" }));
  if (rest.length) groups.push({ key: "other", title: tr("pl_group_other"), rows: rest });

  groups.forEach(g => g.rows.forEach(r => { r.state = rowState(r); }));
  return groups;
}

function rowState(r) {
  if (r.todo) return { level: "todo", word: tr("to_setup") };
  if (r.cover) {
    if (r.cover.reached === false) return { level: "bad", word: tr("pl_offline") };
    if (r.cover.percent == null) return { level: "idle", word: tr("pl_cover_unknown") };   // never run end to end: not a fault
    return { level: "ok", word: tr("pl_ok") };
  }
  if (r.simulated) return { level: "idle", word: tr("simulated"), dashed: true };
  const a = r.health && r.health.assessment;
  if (a && a.state === "offline") return { level: "bad", word: tr("pl_offline") };
  if (a && a.state === "attention") {
    const words = (a.reasons || []).map(reasonWord);
    return { level: "warn", word: words.length > 1 ? tr("pl_more", { label: words[0], count: words.length - 1 }) : words[0] || tr("pl_attention") };
  }
  if (!r.reading) return { level: "idle", word: tr("pl_waiting") };
  if (r.reading.ok === false) return { level: "bad", word: tr("pl_offline") };
  return { level: "ok", word: tr("pl_ok") };
}

// What a device reads now, in a few words.
function rowValue(r) {
  if (r.todo) return "";
  if (r.cover) {
    const c = r.cover;
    if (c.moving) return tr(c.moving === "open" ? "c_opening" : "c_closing");
    return c.percent == null ? "" : c.percent >= 100 ? tr("c_open") : c.percent <= 0 ? tr("c_closed") : tr("c_at", { pct: c.percent });
  }
  const v = r.reading;
  if (!v || v.ok === false) return "";
  if (v.temperature_c != null) return `${fmt(v.temperature_c)}°${v.humidity_pct != null ? ` · ${fmt(v.humidity_pct, 0)}%` : ""}`;
  if (r.role === "vmc") {
    const value = r.speed && r.speed.value;
    return v.is_on === false ? tr("pl_vmc_off") : value != null ? tr("pl_vmc_on", { pct: fmt(value, 0) }) : tr("pl_vmc_on_plain");
  }
  if (r.kind === "power_meter" || (v.power_w != null && v.is_on == null)) return `${fmt(v.power_w, 0)} W`;
  if (typeof v.is_on === "boolean") {
    if (r.role === "valve") return tr(v.is_on ? "pl_valve_open" : "pl_valve_closed");
    if (r.role === "bypass") return tr(v.is_on === r.normallyOpen ? "pl_valve_closed" : "pl_valve_open");
    return `${tr(v.is_on ? "on" : "off")}${v.power_w != null ? ` · ${fmt(v.power_w, 0)} W` : ""}`;
  }
  if (v.value != null) return fmt(v.value, 0);
  return "";
}

function rowSignal(r) {
  if (r.reading && r.reading.wifi_rssi_dbm != null) return r.reading.wifi_rssi_dbm;
  return r.health ? r.health.wifi_rssi_dbm : null;
}

function rowBattery(r) {
  const v = r.reading || {}, h = r.health || {};
  if (v.external_power === true || h.external_power === true) return { external: true };
  const pct = v.battery_pct != null ? v.battery_pct : h.battery_pct;
  return pct == null ? null : { pct };
}

function rowAge(r) {
  if (r.todo) return tr("pl_todo_hint");
  const v = r.reading;
  if (!v) return r.cover ? "" : tr("pl_never");
  const age = v.ok === false && v.last_value_age_seconds != null ? v.last_value_age_seconds : v.seconds_since_update;
  return age == null ? "" : tr("pl_updated", { ago: ago(age) });
}

// A real Shelly on the board: only then does a broker that doesn't answer matter.
function usesShellys() {
  return ((plant.data.health && plant.data.health.devices) || []).some(d => !d.simulated);
}

// What the board says needs doing, the server's verdict first.
function plantTodo() {
  const items = [];
  const climate = now.climate || {};
  if (climate.diagnostic_mode) items.push({ level: "bad", text: tr("pl_diag_on") });
  const severe = ["device_offline", "loop_stalled", "lan_unreachable", "integration_failed"];
  now.problems.forEach(p => items.push({ level: severe.includes(p.code) ? "bad" : "warn", text: problemText(p) }));
  const broker = plant.data.brokerState;
  if (broker && broker.reachable === false && usesShellys()) {
    items.push({ level: "bad", text: tr("pl_broker_down") });
  }
  (climate.to_configure || []).forEach(t => items.push({ level: "todo", text: tr("pl_todo_item", { what: tr(`pl_role_${t.role}`), name: t.name }) }));
  const waiting = ["map", "switches", "outdoorSource", "vmcSource", "settings"].some(k => plant.data[k] && plant.data[k].restart_pending);
  if (waiting) items.push({ level: "warn", text: tr("pl_restart") });
  return items;
}

// ------------------------------------------------------------- drawing --

const STATE_ICON = { ok: "", warn: "", bad: "", todo: "", idle: "" };

function pill(level, word, dashed) {
  return `<span class="spill" data-level="${level}"${dashed ? ' data-dashed="true"' : ""}>${esc(word)}</span>`;
}

function signalHtml(dbm) {
  if (dbm == null) return "";
  const bars = dbm >= -55 ? 4 : dbm >= -65 ? 3 : dbm >= -75 ? 2 : dbm >= -85 ? 1 : 0;
  const label = tr("pl_signal_title", { dbm: Math.round(dbm) });
  return `<span class="sig" data-weak="${dbm < -80}" title="${esc(label)}" aria-label="${esc(label)}"><svg viewBox="0 0 15 12" aria-hidden="true">${[0, 1, 2, 3]
    .map(i => `<rect x="${i * 4}" y="${9 - i * 3}" width="3" height="${3 + i * 3}" rx="0.8"${i < bars ? ' class="on"' : ""}/>`).join("")}</svg></span>`;
}

function batteryHtml(b) {
  if (!b) return "";
  if (b.external) return `<span class="bat" title="${esc(tr("pl_usb"))}">${icon("")}<span>USB-C</span></span>`;
  const pct = Math.round(b.pct);
  const level = pct <= 10 ? "bad" : pct <= 25 ? "warn" : "ok";
  const label = tr("pl_battery_title", { pct });
  return `<span class="bat" data-level="${level}" title="${esc(label)}" aria-label="${esc(label)}"><svg viewBox="0 0 17 9" aria-hidden="true">
    <rect class="bat-box" x="0.6" y="0.6" width="13.8" height="7.8" rx="1.8"/><rect class="bat-tip" x="15" y="3" width="1.6" height="3" rx="0.6"/>
    <rect class="bat-fill" x="2.1" y="2.1" width="${(10.8 * Math.max(4, pct) / 100).toFixed(1)}" height="4.8" rx="0.7"/></svg><span>${pct}%</span></span>`;
}

function drawPlant() {
  const box = $("plant");
  if (!box || !plantShown()) return;
  if (!now.climate) {
    put($("plant-hero"), `<p class="sr-only" role="status">${esc(tr("wait"))}</p><div class="skel" style="height:86px"></div>`);
    return;
  }
  const groups = plantGroups();
  const rows = groups.flatMap(g => g.rows);
  const todo = plantTodo();
  put($("plant-hero"), heroHtml(rows, todo));
  put($("plant-tiles"), tilesHtml(rows));
  put($("plant-devices"), devicesHtml(groups, rows));
  put($("plant-config"), configHtml());
  const count = todo.filter(t => t.level !== "todo").length;
  const badge = $("tab-plant-badge");
  badge.hidden = !count;
  badge.textContent = count;
  rows.filter(r => plant.open.has(r.key) && rowBattery(r) && !rowBattery(r).external && r.kind === "climate").forEach(r => loadTrend(r.name));
}

function heroHtml(rows, todo) {
  const system = plant.data.system;
  const worst = todo.reduce((w, t) => (LEVEL_ORDER[t.level] < LEVEL_ORDER[w] ? t.level : w), "ok");
  const counted = rows.filter(r => !r.todo && r.state.level !== "idle");
  const fine = counted.filter(r => r.state.level === "ok").length;
  const title = !todo.length ? tr("pl_all_fine") : todo.length === 1 ? tr("pl_one_thing") : tr("pl_things", { count: todo.length });
  const said = [counted.length ? tr("pl_devices_fine", { ok: fine, total: counted.length }) : ""];
  if (system && system.host) said.push(tr("pl_board_up", { span: lasting(system.host.uptime_seconds) }));
  if (system && system.software) said.push(`LocalHome ${system.software.version}`);
  const list = todo.length ? `<ul class="todo">${todo.map(t => `<li data-level="${t.level}">${icon(STATE_ICON[t.level])}<span>${esc(t.text)}</span></li>`).join("")}</ul>` : "";
  return `<div class="hero" data-level="${worst}">
      <span class="hero-icon">${icon(STATE_ICON[worst])}</span>
      <div class="hero-text"><h1>${esc(title)}</h1><p>${said.filter(Boolean).map(esc).join(" · ")}</p></div>
      <div class="hero-actions">
        <button class="act" type="button" data-plant-refresh title="${esc(tr("pl_refresh"))}" aria-label="${esc(tr("pl_refresh"))}">${icon("")}</button>
        <a class="act wide" href="#" data-open="/climate-admin">${icon("")}<span>${esc(tr("pl_full_panel"))}</span></a>
      </div>
    </div>${list}`;
}

function tile(level, glyph, label, value, sub) {
  return `<div class="tile" data-level="${level}"><span class="tile-label">${icon(glyph)}${esc(label)}<i class="dot"></i></span>
    <b>${esc(value)}</b><small>${esc(sub || "")}</small></div>`;
}

function tilesHtml(rows) {
  const out = [];
  const counted = rows.filter(r => !r.todo && r.state.level !== "idle");
  const down = counted.filter(r => r.state.level === "bad").length;
  const toFix = counted.filter(r => r.state.level === "warn").length;
  out.push(tile(down ? "bad" : toFix ? "warn" : "ok", "", tr("pl_devices"),
    down ? tr("pl_down", { count: down }) : toFix ? tr("pl_to_fix", { count: toFix }) : tr("pl_all_answer"),
    tr("pl_devices_fine", { ok: counted.length - down - toFix, total: counted.length })));
  const broker = plant.data.brokerState, brokerConf = plant.data.broker;
  if (broker) {
    const local = ["localhost", "127.0.0.1", "::1"].includes(String(broker.host || "").toLowerCase());
    const sub = [tr(local ? "pl_broker_local" : "pl_broker_lan"),
      brokerConf ? tr(brokerConf.has_password ? "pl_broker_password" : "pl_broker_open") : ""].filter(Boolean).join(" · ");
    out.push(tile(broker.reachable ? "ok" : usesShellys() ? "bad" : "idle", "", tr("pl_broker"),
      tr(broker.reachable ? "pl_reachable" : "pl_unreachable"), sub));
  }
  const system = plant.data.system;
  if (system && system.host) {
    const h = system.host, hw = h.hardware || {};
    const hot = h.cpu_temp_c != null && hw.throttle_c != null && h.cpu_temp_c >= hw.throttle_c - 10;
    out.push(tile(hot || hw.cpu_capped ? "warn" : "ok", "", tr("pl_board"),
      h.cpu_temp_c != null ? tr("pl_cpu_temp", { t: fmt(h.cpu_temp_c, 0) }) : tr("pl_ok"),
      tr("pl_board_up", { span: lasting(h.uptime_seconds) })));
    const db = system.database;
    if (db && db.max_bytes) {
      const share = db.used_bytes / db.max_bytes;
      out.push(tile(share >= 0.9 || (system.disk && system.disk.low) ? "warn" : "ok", "", tr("pl_history"),
        tr("pl_of", { a: bytes(db.used_bytes), b: bytes(db.max_bytes) }), tr("pl_history_days", { days: db.retention_days })));
    }
  } else if (!plant.missing.system) {
    out.push(`<div class="tile skel-tile" aria-hidden="true"><div class="skel"></div></div>`);
  }
  const climate = now.climate;
  const season = tr(climate.mode === "cool" ? "pl_cooling" : "pl_heating");
  const seasonSub = climate.diagnostic_mode ? tr("pl_diag_short") : climate.away_until ? tr("pl_away_until", { until: untilWords(climate.away_until) })
    : climate.mode_changed && climate.mode_changed.at ? tr("pl_since", { when: when(climate.mode_changed.at) }) : "";
  out.push(tile(climate.diagnostic_mode ? "bad" : "ok", climate.mode === "cool" ? "" : "", tr("pl_season"), season, seasonSub));
  const backups = plant.data.backups && plant.data.backups.backups;
  if (backups) {
    const last = backups.reduce((m, b) => (b.mtime > (m ? m.mtime : 0) ? b : m), null);
    out.push(tile(last ? "ok" : "idle", "", tr("pl_backup"), last ? when(last.mtime) : tr("pl_none"),
      last ? tr("pl_backup_count", { count: backups.length }) : ""));
  }
  return out.join("");
}

function devicesHtml(groups, rows) {
  const toFix = rows.filter(r => ["bad", "warn", "todo"].includes(r.state.level));
  if (plant.filter === "fix" && !toFix.length) plant.filter = "all";
  const chips = `<div class="chips" role="group" aria-label="${esc(tr("pl_filter"))}">
      <button class="chip" type="button" data-plant-filter="all" aria-pressed="${plant.filter === "all"}">${esc(tr("pl_all", { count: rows.length }))}</button>
      ${toFix.length ? `<button class="chip" type="button" data-plant-filter="fix" aria-pressed="${plant.filter === "fix"}">${esc(tr("pl_fix", { count: toFix.length }))}</button>` : ""}</div>`;
  const body = groups.map(g => {
    const shown = g.rows.filter(r => plant.filter === "all" || toFix.includes(r));
    if (!shown.length) return "";
    const bad = g.rows.filter(r => ["bad", "warn", "todo"].includes(r.state.level)).length;
    return `<section class="dv-group"><h3>${esc(g.title)}<small>${esc(bad ? tr("pl_to_fix", { count: bad }) : tr("pl_group_ok"))}</small></h3>
      ${shown.map(deviceHtml).join("")}</section>`;
  }).join("");
  const healthNote = plant.missing.health ? `<p class="plant-note">${esc(tr("pl_no_health"))}</p>` : "";
  return `<div class="card-head"><b>${esc(tr("pl_devices"))}</b>${chips}</div>${body}${healthNote}`;
}

function deviceHtml(r) {
  const open = plant.open.has(r.key);
  const s = r.state;
  const sub = [r.label !== r.name ? r.name : "", rowAge(r)].filter(Boolean).join(" · ");
  return `<div class="dv" data-level="${s.level}">
    <button class="dv-head" type="button" data-dv="${esc(r.key)}" aria-expanded="${open}">
      <span class="dv-icon">${icon(STATE_ICON[s.level])}</span>
      <span class="dv-name"><b>${esc(r.label)}</b><small title="${esc(sub)}">${esc(sub)}</small></span>
      <span class="dv-value">${esc(rowValue(r))}</span>
      <span class="dv-meta">${signalHtml(rowSignal(r))}${batteryHtml(rowBattery(r))}</span>
      ${pill(s.level, s.word, s.dashed)}
      <i class="ic chev" aria-hidden="true"></i>
    </button>${open ? `<div class="dv-body">${deviceFacts(r)}</div>` : ""}</div>`;
}

// Everything the board knows of one device, in words. (app.js has the
// house's factsHtml: a different name keeps them apart.)
function deviceFacts(r) {
  const facts = [];
  const add = (label, value, level) => { if (value != null && value !== "") facts.push([label, value, level]); };
  const h = r.health || {}, v = r.reading || {}, st = h.stats || {};
  const a = h.assessment || {};
  if (r.todo) return `<p class="plant-note">${esc(tr("pl_todo_long", { name: r.name }))}</p>`;
  (a.reasons || []).forEach(reason => add(tr("pl_to_check"), reasonDetail(reason), "warn"));
  add(tr("pl_reading"), rowValue(r));
  if (r.cover) {
    add(tr("pl_cover_state"), r.cover.reached === false ? tr("pl_offline") : r.cover.percent == null ? tr("pl_cover_unknown") : tr("c_at", { pct: r.cover.percent }));
  }
  if (h.model) add(tr("pl_model"), h.model);
  if (h.firmware) add(tr("pl_firmware"), h.update ? tr("pl_firmware_update", { v: h.firmware }) : tr("pl_firmware_ok", { v: h.firmware }), h.update ? "warn" : null);
  const dbm = rowSignal(r);
  if (dbm != null) {
    const avg = st.signal_24h && st.signal_24h.avg != null ? tr("pl_signal_avg", { d: st.signal_24h.avg, w: st.signal_7d && st.signal_7d.avg != null ? st.signal_7d.avg : "–" }) : "";
    add(tr("pl_signal"), `${Math.round(dbm)} dBm${avg ? ` · ${avg}` : ""}`, dbm < -80 ? "warn" : null);
  }
  const battery = rowBattery(r);
  if (battery) add(tr("pl_power_supply"), battery.external ? tr("pl_usb") : `${Math.round(battery.pct)}%${(v.battery_v || h.battery_v) ? ` (${fmt(v.battery_v || h.battery_v, 1)} V)` : ""}`,
    battery.pct != null && battery.pct <= 25 ? "warn" : null);
  if (h.device_temp_c != null) add(tr("pl_device_temp"), tr("pl_device_temp_v", { t: fmt(h.device_temp_c, 0), max: (plant.data.health || {}).hot_c || 80 }));
  if (h.voltage_v != null) add(tr("pl_voltage"), `${fmt(h.voltage_v, 0)} V`);
  if (h.booted_at) add(tr("pl_running_for"), lasting(boardNowS() - h.booted_at));
  if (st.reports_24h != null) add(tr("pl_reports"), tr("pl_reports_v", { n: st.reports_24h, missed: st.missed_24h || 0, week: st.missed_7d || 0 }), st.missed_24h ? "warn" : null);
  if (st.next_heartbeat_at) add(tr("pl_next_report"), when(st.next_heartbeat_at));
  if (st.switch_ons_24h != null) add(tr("pl_switch_ons"), tr("pl_switch_ons_v", { n: fmt(st.switch_ons_24h, 0), max: (plant.data.health || {}).switch_ons_24h_max || 48 }));
  if (st.on_seconds_today != null) add(tr("pl_on_today"), lasting(st.on_seconds_today));
  const restarts = ["power", "wanted", "crash", "brownout", "unknown"].map(k => [k, st[`restarts_${k}_7d`] || 0]).filter(([, n]) => n);
  if (st.restarts_7d != null || restarts.length) {
    add(tr("pl_restarts"), restarts.length ? restarts.map(([k, n]) => tr(`pl_restart_${k}`, { n })).join(" · ") : tr("pl_none"),
        restarts.some(([k]) => k === "crash" || k === "brownout") ? "warn" : null);
  }
  (h.settings_wrong || []).forEach(sw => add(tr("pl_setting_wrong"), `${sw.key}: ${sw.value} → ${sw.expected}`, "warn"));
  if (!r.health && v.ok === false) add(tr("pl_state"), tr("pl_offline_hint"), "bad");
  const notes = (a.notes || []).map(noteWords).filter(Boolean);
  const trend = plant.trends[r.name];
  let charts = "";
  if (r.kind === "climate" && battery && !battery.external) {
    // The battery is read from its voltage, which moves a few tenths with
    // each reading: a week of it is noise, its median is the level.
    const levels = trend && !trend.loading ? (trend.battery || []).map(p => p.value) : [];
    if (levels.length > 2) {
      const sorted = [...levels].sort((x, y) => x - y);
      add(tr("pl_battery_week_label"), tr("pl_battery_week", { median: fmt(sorted[Math.floor(sorted.length / 2)], 0),
        lo: fmt(sorted[0], 0), hi: fmt(sorted[sorted.length - 1], 0) }));
      notes.push(tr("pl_battery_note"));
    }
    charts = !trend || trend.loading ? `<div class="minis"><div class="skel" style="height:58px"></div></div>`
      : `<div class="minis">${signalChart(trend.signal)}</div>`;
  }
  return `<dl class="facts-list">${facts.map(([l, val, level]) => `<div${level ? ` data-level="${level}"` : ""}><dt>${esc(l)}</dt><dd>${esc(val)}</dd></div>`).join("")}</dl>
    ${notes.length ? `<ul class="notes">${notes.map(n => `<li>${icon("")}<span>${esc(n)}</span></li>`).join("")}</ul>` : ""}${charts}`;
}

function reasonDetail(reason) {
  const word = reasonWord(reason);
  const d = reason.detail;
  if (reason.code === "report_late" && d != null) return `${word}: ${ago(d)}`;
  if (reason.code === "battery_low" && d != null) return `${word}: ${d}%`;
  if (reason.code === "hot" && d != null) return `${word}: ${d} °C`;
  if (["reports_missed", "short_cycling", "crashes", "power_unstable"].includes(reason.code) && d != null) return `${word}: ${d}`;
  if (reason.code === "settings" && d) return `${word}: ${d}`;
  return word;     // device_errors' detail is raw device text: never shown
}

function noteWords(note) {
  const key = `pl_note_${note.code}`;
  if (!TEXTS[LANG][key]) return "";
  return tr(note.count === 1 && TEXTS[LANG][`${key}_one`] ? `${key}_one` : key,
            { count: note.count, when: note.at ? when(note.at) : "", avg: note.avg != null ? Math.round(note.avg) : "" });
}

// The Wi-Fi signal's quality, by the same steps as the bars in a row.
const SIGNAL_BANDS = [[-55, "great"], [-65, "good"], [-75, "fair"], [-85, "weak"], [-Infinity, "bad"]];

function signalBand(dbm) {
  return SIGNAL_BANDS.find(([floor]) => dbm >= floor)[1];
}

// A week of signal on a fixed scale, its quality bands behind: the line's
// height tells how good the signal is, not how much it wobbles.
function signalChart(points) {
  const label = tr("pl_trend_signal");
  if (!points || points.length < 2) return `<figure class="mini"><figcaption>${esc(label)}</figcaption><p class="plant-note">${esc(tr("ch_empty"))}</p></figure>`;
  const top = -35, bottom = -95;
  const y = v => (top - Math.max(bottom, Math.min(top, v))) / (top - bottom) * 40;
  const t0 = points[0].ts, t1 = points[points.length - 1].ts;
  const x = ts => (ts - t0) / Math.max(1, t1 - t0) * 100;
  let upper = top;
  const bands = SIGNAL_BANDS.map(([floor, band]) => {
    const lower = Math.max(bottom, floor);
    const rect = `<rect class="band-${band}" x="0" y="${y(upper).toFixed(2)}" width="100" height="${(y(lower) - y(upper)).toFixed(2)}"/>`;
    upper = lower;
    return rect;
  }).join("");
  const values = points.map(p => p.value);
  const sorted = [...values].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)], worst = sorted[0];
  const said = tr("pl_signal_summary", { band: tr(`pl_sig_${signalBand(median)}`), worst: Math.round(worst) });
  return `<figure class="mini"><figcaption>${esc(label)} <b>${Math.round(values[values.length - 1])} dBm</b><small>${esc(said)}</small></figcaption>
    <svg viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label="${esc(`${label}: ${said}`)}">${bands}
      <path d="${linePath(points, p => x(p.ts), p => y(p.value), 3 * 3600)}" vector-effect="non-scaling-stroke"/></svg></figure>`;
}

// --------------------------------------------------- the settings cards --

function setRow(label, value, extra, level) {
  return `<div class="set-row"${level ? ` data-level="${level}"` : ""}><span>${esc(label)}</span><span class="set-v">${esc(value)}${extra || ""}</span></div>`;
}

function srcTag(source) {
  return source ? `<small class="src">${esc(tr(`pl_src_${source}`))}</small>` : "";
}

function changedTag(value, fallback) {
  return fallback !== undefined && JSON.stringify(value) !== JSON.stringify(fallback) ? `<small class="src" data-changed="true">${esc(tr("pl_changed"))}</small>` : "";
}

function card(key, glyph, title, summary, rows, note) {
  const open = !plant.closed.has(key);
  return `<details class="set" data-card="${key}"${open ? " open" : ""}>
    <summary><span class="set-icon">${icon(glyph)}</span><span class="set-title"><b>${esc(title)}</b><small>${esc(summary || "")}</small></span>
      <i class="ic chev" aria-hidden="true"></i></summary>
    <div class="set-rows">${rows.join("")}${note ? `<p class="plant-note">${esc(note)}</p>` : ""}</div></details>`;
}

function configHtml() {
  const out = [seasonCard(), zonesCard(), thermostatCard(), vmcCard(), dehumCard(), boardCard()].filter(Boolean);
  return `<div class="card-head"><b>${esc(tr("pl_settings"))}</b><span>${esc(tr("pl_read_only"))}</span></div>${out.join("")}`;
}

function actionWord(action, normallyOpen) {
  if (action === "manual") return tr("pl_by_hand");
  if (normallyOpen) return tr(action === "on" ? "pl_valve_closed" : "pl_valve_open").toLowerCase();
  return tr(action === "on" ? "on" : "off").toLowerCase();
}

function seasonCard() {
  const c = now.climate;
  const season = tr(c.mode === "cool" ? "pl_cooling" : "pl_heating");
  const rows = [setRow(tr("pl_season"), season, c.mode_changed && c.mode_changed.at ? ` <small class="src">${esc(tr("pl_since", { when: when(c.mode_changed.at) }))}</small>` : "")];
  rows.push(setRow(tr("away_title"), c.away_until ? untilWords(c.away_until) : tr("pl_no")));
  rows.push(setRow(tr("pl_diag"), tr(c.diagnostic_mode ? "pl_diag_active" : "pl_diag_off"), "", c.diagnostic_mode ? "bad" : null));
  const effects = c.season_effects || {};
  const links = effects.links || [];
  (effects.rules || []).forEach(r => rows.push(setRow(friendly(r.switch),
    [tr("pl_rule", { heat: actionWord(r.heat, r.normally_open), cool: actionWord(r.cool, r.normally_open) }),
     ...links.filter(l => l.leader === r.switch).map(l => tr("pl_link", { zone: l.zone || friendly(l.follower) }))].join(" · "))));
  links.filter(l => !(effects.rules || []).some(r => r.switch === l.leader))
    .forEach(l => rows.push(setRow(friendly(l.leader), tr("pl_link", { zone: l.zone || friendly(l.follower) }))));
  return card("season", "", tr("pl_season_rules"), season, rows);
}

function sourceWord(mode, id) {
  if (mode === "simulated") return tr("pl_src_simulated");
  if (mode === "manual") return tr("pl_src_manual");
  if (mode === "none") return tr("pl_none");
  return tr(id ? "pl_src_shelly" : "pl_src_shelly_todo");
}

function zonesCard() {
  const map = plant.data.map;
  const zones = (map && map.zones) || now.climate.zones.map(z => ({ name: z.name, cool_enabled: z.cool_enabled,
    sensor_mode: z.sensor_simulated ? "simulated" : "shelly", sensor_device_id: z.sensor_configured === false ? null : true,
    valve_mode: "shelly", valve_device_id: z.valve_configured === false ? null : true }));
  const rows = zones.map(z => setRow(tr("pl_zone", { name: z.name }), tr("pl_zone_v", {
    sensor: sourceWord(z.sensor_mode, z.sensor_device_id), valve: sourceWord(z.valve_mode, z.valve_device_id),
    cool: tr(z.cool_enabled === false ? "pl_cool_no" : "pl_cool_yes") }), "",
    (z.sensor_mode === "shelly" && !z.sensor_device_id) || (z.valve_mode === "shelly" && !z.valve_device_id) ? "todo" : null));
  const sw = plant.data.switches;
  if (sw) {
    rows.push(setRow(tr("pl_bypass"), sw.bypass_valve_device_id ? tr(sw.bypass_valve_normally_open ? "pl_wired_no" : "pl_wired_nc") : tr("pl_not_wired")));
    rows.push(setRow(tr("dehum"), sw.dehumidifier_device_id ? tr("pl_dehum_wired", { zone: sw.dehumidifier_water_zone || "–" }) : tr("pl_not_wired")));
  }
  const outdoor = plant.data.outdoorSource;
  if (outdoor) rows.push(setRow(tr("pl_role_outdoor"), sourceWord(outdoor.outdoor_sensor_mode, outdoor.outdoor_sensor_device_id)));
  const pending = ["map", "switches", "outdoorSource", "vmcSource"].some(k => plant.data[k] && plant.data[k].restart_pending);
  const summary = tr("pl_zones_count", { count: zones.length });
  return card("zones", "", tr("pl_wiring"), summary, rows, pending ? tr("pl_restart") : map && !map.configured ? tr("pl_zones_from_config") : "");
}

function thermostatCard() {
  const s = plant.data.settings;
  const c = now.climate;
  const byKey = s ? Object.fromEntries((s.settings || []).map(x => [x.key, x])) : {};
  const val = (key, fallback) => (byKey[key] ? byKey[key].value : fallback);
  const tag = key => (byKey[key] ? srcTag(byKey[key].source) : "");
  const rows = [];
  const hyst = val("hysteresis_c", c.hysteresis_c);
  if (hyst != null) rows.push(setRow(tr("pl_hysteresis"), `${fmt(hyst, 1)} °C`, tag("hysteresis_c")));
  const eco = val("economy_tolerance_c", c.economy_tolerance_c);
  if (eco != null) rows.push(setRow(tr("pl_economy"), `${fmt(eco, 1)} °C`, tag("economy_tolerance_c")));
  const frost = val("frost_protection", c.frost_protection);
  if (frost != null) {
    const on = frost === true || frost === "on" || (frost && frost.enabled);
    rows.push(setRow(tr("pl_frost"), on && byKey.frost_on_c ? tr("pl_frost_v", { on: fmt(byKey.frost_on_c.value, 0), off: fmt(byKey.frost_off_c ? byKey.frost_off_c.value : 0, 0) })
      : tr(on ? "on" : "off"), tag("frost_protection")));
  }
  if (byKey.thermostat_poll_seconds) rows.push(setRow(tr("pl_poll"), tr("pl_every_s", { s: byKey.thermostat_poll_seconds.value }), tag("thermostat_poll_seconds")));
  if (byKey.sensor_failsafe_hours) rows.push(setRow(tr("pl_failsafe"), tr("pl_after_h", { h: byKey.sensor_failsafe_hours.value }), tag("sensor_failsafe_hours")));
  if (byKey.history_retention_days) rows.push(setRow(tr("pl_retention"), tr("pl_days", { d: byKey.history_retention_days.value }), tag("history_retention_days")));
  if (byKey.history_max_db_mb) {
    const cap = byKey.history_max_db_mb;
    rows.push(setRow(tr("pl_history_max"), `${fmt(cap.capped_to != null ? cap.capped_to : cap.value, 0)} MB`, tag("history_max_db_mb")));
  }
  if (!rows.length) return "";
  return card("thermostat", "", tr("pl_thermostat"), tr("pl_hysteresis_short", { v: hyst != null ? fmt(hyst, 1) : "–" }), rows,
              s && s.restart_pending ? tr("pl_restart") : "");
}

function vmcCard() {
  const speed = vmcSpeed();
  const source = plant.data.vmcSource;
  if (!speed && !(source && source.vmc_mode !== "none")) return "";
  const rows = [];
  rows.push(setRow(tr("pl_connection"), speed ? (speed.simulated ? tr("pl_src_simulated")
    : source && source.vmc_mode === "shelly" ? tr("pl_src_shelly") : tr("pl_src_config")) : sourceWord(source.vmc_mode, source.vmc_device_id)));
  const speeds = plant.data.speeds && plant.data.speeds.speeds;
  if (speeds) {
    rows.push(setRow(tr("pl_speeds"), ["min", "night", "day", "high", "boost"].filter(k => speeds[k] != null)
      .map(k => `${tr(`vmc_${k}`)} ${speeds[k]}%`).join(" · "), srcTag(plant.data.speeds.saved ? "panel" : "device")));
  }
  const schedule = now.vmc && now.vmc.schedule;
  if (schedule) rows.push(setRow(tr("pl_vmc_program"), tr(schedule.enabled ? "pl_active" : "off")));
  const auto = plant.data.vmcAuto && plant.data.vmcAuto.settings;
  if (auto) {
    const on = ["shower", "humid", "dry", "cooling"].filter(k => auto[k]).map(k => tr(`pl_auto_${k}`));
    rows.push(setRow(tr("pl_vmc_auto"), on.length ? on.join(", ") : tr("pl_none"),
      auto.quiet_from_hour != null ? ` <small class="src">${esc(tr("pl_quiet", { from: auto.quiet_from_hour, to: auto.quiet_to_hour }))}</small>` : ""));
  }
  const f = now.vmc && now.vmc.filters;
  if (f && f.interval_days) {
    rows.push(setRow(tr("pl_filters"), tr("pl_filters_v", { days: f.interval_days, cleaned: onDay(f.cleaned_on) || "–", due: onDay(f.due_on) || "–" }), "",
      f.state === "due" ? "bad" : f.state === "soon" ? "warn" : null));
  }
  const level = speed && now.readings[speed.name];
  const power = speed && now.readings[speed.paired_switch];
  const summary = power && power.is_on === false ? tr("pl_vmc_off") : level && level.value != null ? tr("pl_vmc_on", { pct: fmt(level.value, 0) }) : "";
  return card("vmc", "", tr("vmc"), summary, rows);
}

function dehumCard() {
  const conf = plant.data.dehum;
  const live = now.climate.dehumidifier;
  if (!live && !(conf && conf.configured)) return "";
  const rows = [];
  const s = (conf && conf.settings) || live || {};
  const d = (conf && conf.defaults) || {};
  if (live) rows.push(setRow(tr("pl_this_season"), tr(live.allowed ? "pl_allowed" : "pl_not_allowed")));
  if (s.start_from != null) {
    rows.push(setRow(tr("pl_dehum_thresholds"), tr("pl_dehum_thresholds_v", { start: s.start_from, stop: s.stop_below }),
      changedTag([s.start_from, s.stop_below], d.start_from !== undefined ? [d.start_from, d.stop_below] : undefined)));
  }
  if (s.durations) rows.push(setRow(tr("pl_durations"), s.durations.map(h => tr("hours", { h })).join(", "), changedTag(s.durations, d.durations)));
  if (s.target_max_hours != null) rows.push(setRow(tr("pl_until_dry"), tr("pl_max_h", { h: s.target_max_hours }), changedTag(s.target_max_hours, d.target_max_hours)));
  const cs = conf && conf.settings, la = (live && live.auto) || {};
  const auto = cs ? cs.auto : la.enabled;
  const from = cs ? cs.auto_from_hour : la.from_hour;
  const to = cs ? cs.auto_to_hour : la.to_hour;
  const max = cs ? cs.auto_max_hours_day : la.max_hours_day;
  rows.push(setRow(tr("pl_dehum_auto"), auto ? tr("pl_dehum_auto_v", { from, to, max }) : tr("off")));
  return card("dehum", "", tr("dehum"), live ? tr(live.on ? "on" : "off") : "", rows);
}

function boardCard() {
  const s = plant.data.system;
  const rows = [];
  if (s) {
    const h = s.host || {}, hw = h.hardware || {}, p = s.process || {};
    if (s.software) rows.push(setRow("LocalHome", `${s.software.version}${s.software.date ? ` · ${onDay(s.software.date)}` : ""}`));
    if (hw.model) rows.push(setRow(tr("pl_board"), [hw.model, hw.soc].filter(Boolean).join(" · ")));
    if (h.os) rows.push(setRow(tr("pl_os"), h.os));
    if (h.uptime_seconds != null) rows.push(setRow(tr("pl_board_on"), lasting(h.uptime_seconds)));
    if (p.uptime_seconds != null) rows.push(setRow(tr("pl_app_on"), lasting(p.uptime_seconds)));
    const boots = (s.boots || []).slice(0, 3).map(b => `${when(b.booted_at)} ${tr(`pl_boot_${b.ended}`) !== `pl_boot_${b.ended}` ? tr(`pl_boot_${b.ended}`) : ""}`.trim());
    if (boots.length) rows.push(setRow(tr("pl_boots"), boots.join(" · ")));
    if (h.cpu_temp_c != null) {
      rows.push(setRow(tr("pl_cpu"), tr("pl_cpu_v", { t: fmt(h.cpu_temp_c, 0), load: fmt((h.load_average || [0])[0], 2) }), "",
        hw.cpu_capped ? "warn" : null));
    }
    if (h.mem_total_bytes) rows.push(setRow(tr("pl_memory"), tr("pl_memory_v", { app: bytes(p.rss_bytes), free: bytes(h.mem_available_bytes), total: bytes(h.mem_total_bytes) })));
    if (s.disk) rows.push(setRow(tr("pl_disk"), tr("pl_of_free", { a: bytes(s.disk.free_bytes), b: bytes(s.disk.total_bytes) }), "", s.disk.low ? "warn" : null));
    if (s.storage && s.storage.wear_percent_max != null) rows.push(setRow(tr("pl_storage"), tr("pl_wear", { pct: s.storage.wear_percent_max })));
    const w = s.network && s.network.wifi;
    if (w && w.signal_percent != null) rows.push(setRow(tr("pl_board_wifi"), [`${w.signal_percent}%`, w.band, w.channel ? tr("pl_channel", { c: w.channel }) : ""].filter(Boolean).join(" · ")));
    const prot = s.protections && s.protections.items;
    if (prot) {
      const all = Object.keys(prot), on = all.filter(k => prot[k]);
      rows.push(setRow(tr("pl_protections"), tr("pl_of_on", { a: on.length, b: all.length }), "", on.length < all.length ? "warn" : null));
    }
    const stalled = (s.loops || []).filter(l => l.stalled);
    if (stalled.length) rows.push(setRow(tr("pl_loops"), stalled.map(l => l.name).join(", "), "", "bad"));
    if ((s.failed_integrations || []).length) rows.push(setRow(tr("pl_integrations"), tr("pl_failed_count", { count: s.failed_integrations.length }), "", "bad"));
    const db = s.database;
    if (db && db.max_bytes) {
      rows.push(setRow(tr("pl_history"), tr("pl_history_v", { a: bytes(db.used_bytes), b: bytes(db.max_bytes), days: db.retention_days, since: db.oldest_ts ? when(db.oldest_ts) : "–" })));
    }
  }
  const clock = now.clock;
  if (clock) rows.push(setRow(tr("pl_clock"), tr(clock.clock_synced ? "pl_clock_ok" : "pl_clock_no", { tz: clock.tz_name || "" }), "", clock.clock_synced ? null : "warn"));
  const led = plant.data.led;
  if (led && led.status && led.status !== "off") {
    const known = TEXTS[LANG][`pl_led_${led.status}`];
    rows.push(setRow(tr("pl_led"), known || tr("pl_led_other"), "", led.status === "running" ? null : "warn"));
  }
  const broker = plant.data.brokerState, conf = plant.data.broker;
  if (broker) {
    const local = ["localhost", "127.0.0.1", "::1"].includes(String(broker.host || "").toLowerCase());
    rows.push(setRow(tr("pl_broker"), [tr(broker.reachable ? "pl_reachable" : "pl_unreachable"), tr(local ? "pl_broker_local" : "pl_broker_lan"),
      conf ? tr(conf.has_password ? "pl_broker_password" : "pl_broker_open") : ""].filter(Boolean).join(" · "), "",
      broker.reachable || !usesShellys() ? null : "bad"));
  }
  const record = plant.data.record;
  if (record) {
    rows.push(setRow(tr("pl_record"), !record.enabled ? tr("off") : record.first_ts
      ? tr("pl_record_v", { since: when(record.first_ts), size: fmt(record.size_mb || 0, 1) }) : tr("pl_record_empty")));
  }
  const backups = plant.data.backups && plant.data.backups.backups;
  if (backups && backups.length) {
    const last = backups.reduce((m, b) => (b.mtime > m.mtime ? b : m));
    rows.push(setRow(tr("pl_backup"), tr("pl_backup_v", { when: when(last.mtime), count: backups.length })));
  }
  if (!rows.length) return plant.missing.system ? "" : card("board", "", tr("pl_board_card"), "", [`<div class="skel" style="height:120px"></div>`]);
  return card("board", "", tr("pl_board_card"), s && s.software ? `LocalHome ${s.software.version}` : "", rows);
}

// ---------------------------------------------------------- listening --

function wirePlant() {
  const box = $("plant");
  box.addEventListener("click", event => {
    const head = event.target.closest("[data-dv]");
    if (head) {
      const key = head.dataset.dv;
      if (plant.open.has(key)) plant.open.delete(key);
      else plant.open.add(key);
      drawPlant();
      return;
    }
    const filter = event.target.closest("[data-plant-filter]");
    if (filter) {
      plant.filter = filter.dataset.plantFilter;
      drawPlant();
      return;
    }
    if (event.target.closest("[data-plant-refresh]")) {
      loadPlant(true);
      refresh();
    }
  });
  // A card folded stays folded when the tab is drawn again.
  box.addEventListener("toggle", event => {
    const card = event.target.closest && event.target.closest("details[data-card]");
    if (!card) return;
    if (card.open) plant.closed.delete(card.dataset.card);
    else plant.closed.add(card.dataset.card);
  }, true);
}
