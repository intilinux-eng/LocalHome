"use strict";

// Same tiny translation helper as home.js, copied rather than shared -
// this page is a separate, occasional-use bundle on purpose (see
// docs/configuration.md's climate_admin section).
function tr(path, vars) {
  const value = path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), window.I18N);
  let str = value != null ? value : path;
  if (vars) for (const k in vars) str = str.split(`{${k}}`).join(vars[k]);
  return str;
}

function fmt(value, digits) {
  return typeof value === "number" ? value.toFixed(digits) : "--";
}

// A compact age, this page's own - see this file's own top comment.
function formatAge(seconds) {
  if (seconds == null) return null;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

const LOCALE = (window.I18N && window.I18N.locale) || undefined;

// The "last known value" fallback (drivers/mqtt/sensor.py's read())
// only carries an age in seconds, not a timestamp - Date.now() at
// render time is close enough (this is display-only; the thermostat
// never uses this value, see services/thermostat.py's tick()) to turn
// that into an actual date/time, which is what the operator actually
// wants to see, not just a relative "19h ago".
function staleValueNote(ageSeconds) {
  const relative = formatAge(ageSeconds);
  if (relative == null) return tr("sensors.stale_value_note", { date: "?", time: "?" });
  const date = new Date(Date.now() - ageSeconds * 1000).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" });
  return tr("sensors.stale_value_note", { date, time: relative });
}

// A zone's sensor can be stale (every MQTT topic past
// stale_after_seconds - see drivers/mqtt/sensor.py's read()) while
// still having a last known reading attached - most often a battery
// sensor (Shelly H&T) between wake cycles, not broken hardware.
function zoneHasLastKnownValue(zone) {
  return !zone.sensor_ok && zone.temperature_c != null;
}

// ---------------------------------------------------------------- restart ----
// Every save above needs this before it actually does anything - see
// services/self_restart.py. Reused wherever a save's result or the
// live status section's "restart pending" banner needs to offer it.

function restartButtonHtml() {
  return `<button class="range-btn" onclick="restartLocalHome(this)">${tr("climate_admin.restart_button")}</button>`;
}

async function restartLocalHome(button) {
  if (!await confirmAction(tr("climate_admin.restart_confirm"), { confirmLabel: tr("climate_admin.restart_button") })) return;
  restartInProgress = true;
  button.disabled = true;
  const container = button.parentElement;
  let data = { ok: true };
  try {
    const res = await fetch("/api/climate-admin/restart", { method: "POST" });
    data = await res.json();
  } catch (e) {
    // A connection reset right as the process turns over is the
    // expected outcome on a real restart - treat it as "restarting",
    // not as this request having failed.
  }
  if (data && data.ok === false && data.platform_unsupported) {
    if (container) container.innerHTML = `<p class="admin-mode-exclusion">${tr("climate_admin.restart_unsupported_windows")}</p>`;
    button.disabled = false;
    return;
  }
  if (container) container.innerHTML = tr("climate_admin.restart_in_progress");
  await waitForServerAndReload(container);
}

// Polls instead of a single blind reload after a fixed delay - a real
// restart's timing varies (import time, MQTT/cloud driver setup), and
// reloading into a server that isn't back yet just shows the browser's
// own connection-error page - indistinguishable from a restart that's
// genuinely stuck (the exact confusion this replaces).
async function waitForServerAndReload(container) {
  // The server only turns over ~1s after answering (services/self_restart.py):
  // polling right away could reach the old process and reload too early.
  await new Promise(r => setTimeout(r, 2500));
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const res = await fetch("/api/climate-admin/map", { cache: "no-store" });
      if (res.ok) {
        location.reload();
        return;
      }
    } catch (e) {
      // Not back yet - keep polling.
    }
  }
  if (container) container.innerHTML = `<p class="admin-mode-exclusion">${tr("climate_admin.restart_timeout")}</p>`;
}

const SENSOR_MODES = ["shelly", "simulated", "manual"];
const VALVE_MODES = ["shelly", "simulated"];

let savedMap = null; // last GET /api/climate-admin/map response
let extraSwitchesRestartPending = false; // from the last GET /api/climate-admin/extra-switches
let settingsRestartPending = false; // from the last GET /api/climate-admin/settings
let restartInProgress = false;
let guideDecided = false;
let pollTimer = null;

// ------------------------------------------------------------- overview ----
// The tiles at the top of the page and the pills in each section's
// header. Each is filled by the request that already runs for its
// section - none of this asks the server anything of its own.

function setTile(id, state, value, sub) {
  const tile = document.getElementById(id);
  if (!tile) return;
  tile.dataset.state = state;
  tile.querySelector(".ov-value").textContent = value;
  tile.querySelector(".ov-sub").textContent = sub || "";
}

function pillHtml(state, text) {
  return `<span class="admin-status ${state}">${escapeHtml(text)}</span>`;
}

function setPill(id, html) {
  const el = document.getElementById(id);
  if (el) morph(el, html);
}

// An outdoor sensor's or VMC's header: its source, coloured by whether
// the real device answers (`live`: "ok"/"neutral"/"bad", or null before
// the first reading) - or `todo`, its Shelly still without an id.
function rolePillHtml(mode, restartPending, live, todo = false) {
  if (restartPending) return pillHtml("neutral", tr("climate_admin.pill_restart"));
  if (todo) return pillHtml("todo", tr("climate_admin.pill_todo"));
  return pillHtml(mode === "shelly" && live ? live : "idle", tr(`climate_admin.pill_${mode}`));
}

// A Shelly the panel has no id for yet - not a fault: one to do.
function todoHtml() {
  return `<span class="admin-status todo">${escapeHtml(tr("climate_admin.todo"))}</span>`;
}

// Every save on this page waits for a restart; this names the ones that
// are waiting, with the button, at the top of the page.
function updateRestartBar() {
  const bar = document.getElementById("restart-bar");
  if (!bar || restartInProgress) return;
  const waiting = [
    [savedMap && savedMap.restart_pending, "zones_heading"],
    [extraSwitchesRestartPending, "extra_switches_heading"],
    [extraSensorRestartPending, "extra_sensor_heading"],
    [vmcRestartPending, "vmc_heading"],
    [settingsRestartPending, "settings_heading"],
  ].filter(([pending]) => pending).map(([, key]) => tr(`climate_admin.${key}`));
  bar.hidden = !waiting.length;
  if (!waiting.length) return;
  document.getElementById("restart-bar-text").textContent = tr("climate_admin.restart_bar", { what: waiting.join(", ") });
  const action = bar.querySelector(".restart-bar-action");
  if (!action.querySelector("button")) action.innerHTML = restartButtonHtml();
}

// A valve that doesn't answer is a fault; a battery sensor that hasn't
// reported yet may just be asleep - "to check", not red; a Shelly with no
// id yet is still to configure.
function zoneHealth(zone) {
  if (zone.sensor_configured === false || zone.valve_configured === false) return "todo";
  if (!zone.valve_ok) return "bad";
  return zone.sensor_ok ? "ok" : "neutral";
}

function updatePlantOverview(data, restartPending) {
  if (data.mode) {
    const name = tr(`climate.mode_${data.mode}`);
    let state = data.mode;
    let sub = tr("climate_admin.ov_season_normal");
    if (data.diagnostic_mode) {
      state = "bad";
      sub = tr("climate_admin.ov_season_diag");
    } else if (data.away_until) {
      state = "neutral";
      sub = tr("climate_admin.ov_season_away", {
        until: new Date(data.away_until).toLocaleString(LOCALE, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }),
      });
    }
    setTile("ov-season", state, name, sub);
    setPill("season-summary", pillHtml(`season-${data.mode}`, name));
  }
  const zones = data.zones || [];
  // Only the zones with a reading - none yet on a new board.
  const temps = zones.filter(z => z.temperature_c != null).map(z => `${z.name} ${fmt(z.temperature_c, 1)}°`).join(" · ");
  const health = zones.map(zoneHealth);
  const toCheck = health.filter(h => h === "bad" || h === "neutral").length;
  const todo = health.includes("todo");
  let state = health.includes("bad") ? "bad" : toCheck ? "neutral" : todo ? "todo" : "ok";
  let value = toCheck ? tr("climate_admin.ov_zones_check", { count: toCheck })
    : todo ? tr("climate_admin.ov_zones_todo") : tr("climate_admin.ov_zones_ok");
  if (restartPending) {
    state = "neutral";
    value = tr("climate_admin.ov_zones_restart");
  } else if (!zones.length) {
    state = "idle";
    value = tr("climate.no_zones");
  }
  setTile("ov-zones", state, value, temps);
  setPill("zones-summary", zones.length ? pillHtml(state, restartPending || toCheck || todo ? value
    : tr("climate_admin.pill_zones_ok", { count: zones.length })) : "");
}

// "1 anomalia", not "1 anomalie".
function problemsCountText(count) {
  return tr(count === 1 ? "climate_admin.system_problems_count_one" : "climate_admin.system_problems_count", { count });
}

function updateBoardOverview(data) {
  const problems = data.problems || [];
  // The first problem under the count - the devices have their own tile.
  setTile("ov-system", problems.length ? "bad" : "ok",
    problems.length ? problemsCountText(problems.length) : tr("climate_admin.system_all_ok"),
    problems.length ? problemTitle(problems[0]) + (problems.length > 1 ? " …" : "") : "");
  const p = data.process || {};
  const h = data.host || {};
  const clock = h.clock_synced;
  setTile("ov-board", clock === false ? "neutral" : "ok",
    tr("climate_admin.ov_board_up", { age: formatDuration(typeof h.uptime_seconds === "number" ? h.uptime_seconds : p.uptime_seconds) }),
    clock == null ? tr("climate_admin.ov_clock_unknown", { ram: formatBytes(p.rss_bytes) })
      : tr(clock ? "climate_admin.ov_clock_ok" : "climate_admin.ov_clock_bad"));
}

// A battery this low is flagged.
const LOW_BATTERY_PCT = 10;

// ------------------------------------------------------------ config form ----

function zoneFormHtml(zone, index) {
  // A zone that doesn't exist yet: its Shellys to configure - each one
  // saved when its id is known, one at a time.
  const z = zone || { name: "", sensor_mode: "shelly", sensor_device_id: "", valve_mode: "shelly", valve_device_id: "", cool_enabled: true };
  const sensorOptions = SENSOR_MODES.map(
    m => `<option value="${m}" ${z.sensor_mode === m ? "selected" : ""}>${tr(`climate_admin.mode_${m}`)}</option>`
  ).join("");
  const valveOptions = VALVE_MODES.map(
    m => `<option value="${m}" ${(z.valve_mode || "shelly") === m ? "selected" : ""}>${tr(`climate_admin.mode_${m}`)}</option>`
  ).join("");
  // The status block below is filled in by refreshStatus() - it's part
  // of this same card (not a separate page section) so editing a
  // device id and checking whether it's actually reporting happen
  // without scrolling anywhere. refreshStatus() only ever touches
  // #zone-status-N's innerHTML, never this card as a whole, so a poll
  // tick never resets a field the operator is mid-edit on.
  return `
    <div class="admin-zone-card" data-index="${index}">
      <div class="admin-field">
        <label>${tr("climate_admin.name_label")}</label>
        <input type="text" class="zone-name" value="${escapeHtml(z.name || "")}" placeholder="${tr("climate_admin.name_placeholder")}">
      </div>
      <div class="admin-field">
        <label>${tr("climate_admin.sensor_mode_label")}</label>
        <select class="zone-sensor-mode" onchange="onSensorModeChange(this)">${sensorOptions}</select>
      </div>
      <div class="admin-field zone-sensor-id-field"${z.sensor_mode !== "shelly" ? " hidden" : ""}>
        <label>${tr("climate_admin.sensor_device_id_label")}</label>
        <input type="text" class="zone-sensor-device-id" value="${escapeHtml(z.sensor_device_id || "")}" placeholder="shellyhtg3-XXXXXXXXXXXX" onchange="checkShelly(this, 'sensor')">
        <p class="hint-text shelly-check" aria-live="polite"></p>
      </div>
      <p class="hint-text zone-manual-hint"${z.sensor_mode !== "manual" ? " hidden" : ""}>${tr("climate_admin.manual_mode_hint")}</p>
      <div class="admin-field">
        <label>${tr("climate_admin.valve_mode_label")}</label>
        <select class="zone-valve-mode" onchange="onValveModeChange(this)">${valveOptions}</select>
      </div>
      <div class="admin-field zone-valve-id-field"${(z.valve_mode || "shelly") !== "shelly" ? " hidden" : ""}>
        <label>${tr("climate_admin.valve_device_id_label")}</label>
        <input type="text" class="zone-valve-device-id" value="${escapeHtml(z.valve_device_id || "")}" placeholder="shelly1g3-XXXXXXXXXXXX" onchange="checkShelly(this, 'valve')">
        <p class="hint-text shelly-check" aria-live="polite"></p>
      </div>
      <label class="admin-checkbox">
        <input type="checkbox" class="zone-cool-enabled" ${z.cool_enabled !== false ? "checked" : ""}>
        ${tr("climate_admin.cool_enabled_label")}
      </label>
      <div class="admin-zone-status" id="zone-status-${index}">
        <p class="hint-text">${tr("climate_admin.status_loading")}</p>
      </div>
    </div>
  `;
}

function onSensorModeChange(select) {
  const card = select.closest(".admin-zone-card");
  card.querySelector(".zone-sensor-id-field").hidden = select.value !== "shelly";
  card.querySelector(".zone-manual-hint").hidden = select.value !== "manual";
}

function onValveModeChange(select) {
  const card = select.closest(".admin-zone-card");
  card.querySelector(".zone-valve-id-field").hidden = select.value !== "shelly";
}

async function loadMap() {
  const res = await fetch("/api/climate-admin/map");
  savedMap = await res.json();
  // The backend always sends exactly 2 well-formed zones now - either
  // the saved mapping, or (when nothing's saved yet) a snapshot of
  // config.yaml's own current setup, real device ids included - see
  // services/climate_map_store.py's snapshot_from_config().
  const zones = savedMap.zones || [null, null];
  document.getElementById("zone-forms").innerHTML = zones.map((z, i) => zoneFormHtml(z, i)).join("");
  const fresh = document.getElementById("zones-fresh-note");
  if (fresh) fresh.hidden = !!savedMap.configured;
  // Only while there are no zones at all - none saved here, none in
  // config.yaml - and then open, unless closed by hand in this tab.
  const guide = document.getElementById("guide");
  if (guide && !guideDecided) {
    guideDecided = true;
    guide.hidden = !!savedMap.configured;
    if (!guide.hidden && !(guide.id in openSectionsSaved)) guide.open = true;
  }
  updateRestartBar();
  refreshAll();
}

function collectZonesFromForm() {
  return [...document.querySelectorAll("#zone-forms .admin-zone-card")].map(card => {
    const sensorMode = card.querySelector(".zone-sensor-mode").value;
    const valveMode = card.querySelector(".zone-valve-mode").value;
    return {
      name: card.querySelector(".zone-name").value.trim(),
      sensor_mode: sensorMode,
      sensor_device_id: sensorMode === "shelly" ? card.querySelector(".zone-sensor-device-id").value.trim() : null,
      valve_mode: valveMode,
      valve_device_id: valveMode === "shelly" ? card.querySelector(".zone-valve-device-id").value.trim() : null,
      cool_enabled: card.querySelector(".zone-cool-enabled").checked,
    };
  });
}

// ----------------------------------------------------------------- broker ----
// Written by hand in config/mqtt_broker.json, then a restart: here only
// where it is and whether with a user - whether it answers is
// refreshBroker()'s, below.

async function loadBroker() {
  let data;
  try {
    data = await (await fetch("/api/climate-admin/broker")).json();
  } catch (e) {
    return;   // offline: the connection banner says so already
  }
  document.getElementById("broker-current").textContent = data.host
    ? tr(data.username ? "climate_admin.broker_current_auth" : "climate_admin.broker_current",
      { address: `${data.host}:${data.port || 1883}`, user: data.username })
    : tr("climate_admin.broker_none");
}

async function saveMap(button) {
  const resultEl = document.getElementById("save-result");
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/map", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ zones: collectZonesFromForm() }),
    });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = tr("sensors.error_prefix") + apiError(data);
      resultEl.className = "hint-text admin-error";
      return;
    }
    let text = tr("climate_admin.save_success");
    if (data.warnings && data.warnings.length) text += " " + data.warnings.join(" ");
    resultEl.innerHTML = `${text} ${restartButtonHtml()}`;
    resultEl.className = "hint-text admin-success";
    await loadMap();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

// -------------------------------------------------------- extra switches ----
// Two more fixed, standalone Shelly Plus 1 on/off switches every
// "reference model" apartment has (a bathroom bypass valve and a
// dehumidifier) - no sensor, no schedule, not tied to a zone. Each is
// independently optional (leave blank if not wired yet). Unlike a zone
// there's no name field: the panel always builds these under a fixed
// name - see services/climate_map_store.py's EXTRA_SWITCH_FIELDS and
// its module docstring for what that means for an existing config.yaml
// interlock/mode_switch that already controls one of these by name.

async function loadExtraSwitches() {
  const res = await fetch("/api/climate-admin/extra-switches");
  const data = await res.json();
  document.getElementById("extra-bypass-valve-id").value = data.bypass_valve_device_id || "";
  document.getElementById("extra-dehumidifier-id").value = data.dehumidifier_device_id || "";
  document.getElementById("extra-bypass-normally-open").checked = data.bypass_valve_normally_open !== false;
  // The zone whose valve the dehumidifier draws its water through.
  const zoneSelect = document.getElementById("extra-dehumidifier-zone");
  zoneSelect.innerHTML = [`<option value="">${escapeHtml(tr("climate_admin.extra_dehumidifier_zone_none"))}</option>`,
    ...(data.zones || []).map(z => `<option value="${escapeAttr(z)}">${escapeHtml(z)}</option>`)].join("");
  zoneSelect.value = data.dehumidifier_water_zone || "";
  extraSwitchesRestartPending = !!data.restart_pending;
  const missing = [data.bypass_valve_device_id, data.dehumidifier_device_id].filter(id => !id).length;
  setPill("extra-switches-summary", extraSwitchesRestartPending ? pillHtml("neutral", tr("climate_admin.pill_restart"))
    : missing ? pillHtml("todo", tr("climate_admin.pill_todo_count", { count: missing }))
    : pillHtml("idle", tr("climate_admin.pill_switches", { count: 2 })));
  updateRestartBar();
}

async function saveExtraSwitches(button) {
  const resultEl = document.getElementById("save-extra-result");
  const body = {
    bypass_valve_device_id: document.getElementById("extra-bypass-valve-id").value.trim() || null,
    dehumidifier_device_id: document.getElementById("extra-dehumidifier-id").value.trim() || null,
    bypass_valve_normally_open: document.getElementById("extra-bypass-normally-open").checked,
    dehumidifier_water_zone: document.getElementById("extra-dehumidifier-zone").value || null,
  };
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/extra-switches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = tr("sensors.error_prefix") + apiError(data);
      resultEl.className = "hint-text admin-error";
      return;
    }
    resultEl.innerHTML = `${tr("climate_admin.save_success")} ${restartButtonHtml()}`;
    resultEl.className = "hint-text admin-success";
    await loadExtraSwitches();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

// --------------------------------------------------------- extra sensor ----
// The outdoor temperature sensor: a standalone Shelly H&T Gen3 - no
// valve, no schedule, not tied to a zone. Like a zone's sensor it has a
// source: the real device (its id), simulated (a believable day/night
// curve, so the outdoor chart exists before the hardware does) or none.
// Fixed name, see services/climate_map_store.py's EXTRA_SENSOR_FIELDS.
const EXTRA_SENSOR_NAME = "Outdoor Sensor";
let extraSensorRestartPending = false;

// The device id is only asked for when the source is a real Shelly.
function syncExtraRoleFields() {
  const pairs = [["extra-outdoor-sensor-mode", "extra-outdoor-sensor-id-field"], ["extra-vmc-mode", "extra-vmc-id-field"]];
  for (const [selectId, fieldId] of pairs) {
    const select = document.getElementById(selectId);
    const field = document.getElementById(fieldId);
    if (select && field) field.hidden = select.value !== "shelly";
  }
}

async function loadExtraSensor() {
  const res = await fetch("/api/climate-admin/extra-sensors");
  const data = await res.json();
  document.getElementById("extra-outdoor-sensor-id").value = data.outdoor_sensor_device_id || "";
  document.getElementById("extra-outdoor-sensor-mode").value = data.outdoor_sensor_mode || "none";
  extraSensorRestartPending = !!data.restart_pending;
  syncExtraRoleFields();
  setPill("extra-sensor-summary", rolePillHtml(data.outdoor_sensor_mode || "none", extraSensorRestartPending, null,
    data.outdoor_sensor_mode === "shelly" && !data.outdoor_sensor_device_id));
  updateRestartBar();
}

async function saveExtraSensor(button) {
  const resultEl = document.getElementById("save-extra-sensor-result");
  const body = {
    outdoor_sensor_device_id: document.getElementById("extra-outdoor-sensor-id").value.trim() || null,
    outdoor_sensor_mode: document.getElementById("extra-outdoor-sensor-mode").value,
  };
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/extra-sensors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = tr("sensors.error_prefix") + apiError(data);
      resultEl.className = "hint-text admin-error";
      return;
    }
    resultEl.innerHTML = `${tr("climate_admin.save_success")} ${restartButtonHtml()}`;
    resultEl.className = "hint-text admin-success";
    await loadExtraSensor();
    refreshExtraSensorStatus();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

// Reuses the generic /api/sensors/<name> route (web/app.py) rather than
// anything zone-specific - this sensor isn't part of any zone. Adapts
// its plain ok/error shape onto the same sensor_ok/sensor_error shape
// sensorStatusHtml() already expects from /api/climate/zones, so the
// exact same "reporting / not reachable yet / gone offline" rendering
// (and guidance text) applies here without duplicating it.
async function refreshExtraSensorStatus() {
  const slot = document.getElementById("extra-sensor-status");
  if (!slot) return;
  const mode = document.getElementById("extra-outdoor-sensor-mode").value;
  if (mode === "none") {
    morph(slot, "");
    return;
  }
  if (mode === "shelly" && !document.getElementById("extra-outdoor-sensor-id").value.trim()) {
    morph(slot, `<div>${tr("climate_admin.sensor_label")}: ${todoHtml()}</div>`);
    return;
  }
  if (extraSensorRestartPending) {
    setPill("extra-sensor-summary", rolePillHtml(mode, true, null));
    morph(slot, `<p class="admin-restart-pending-inline">${tr("climate_admin.restart_pending")} ${restartButtonHtml()}</p>`);
    return;
  }
  try {
    const res = await fetch(`/api/sensors/${encodeURIComponent(EXTRA_SENSOR_NAME)}`);
    const reading = await res.json();
    const zoneShaped = {
      sensor_ok: reading.ok,
      sensor_error: reading.error,
      temperature_c: reading.temperature_c,
      humidity_pct: reading.humidity_pct,
      battery_pct: reading.battery_pct,
      wifi_rssi_dbm: reading.wifi_rssi_dbm,
      sensor_last_value_age_seconds: reading.last_value_age_seconds,
    };
    morph(slot, `<div>${tr("climate_admin.sensor_label")}: ${sensorStatusHtml(zoneShaped, mode)}</div>`);
    setPill("extra-sensor-summary", rolePillHtml(mode, false, reading.ok ? "ok" : "neutral"));
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place.
  }
}

// ----------------------------------------------------------------- VMC ----
// The ventilation unit, on a Shelly 0/1-10V Dimmer Gen3: one device id
// builds its on/off switch and its speed (see services/climate_map_store.py's
// EXTRA_DIMMER_FIELDS). Mains powered, so unlike a battery sensor "not
// responding" is never normal - the status says so plainly.
const VMC_NAME = "VMC";
const VMC_SPEED_NAME = "VMC Speed";
let vmcRestartPending = false;

async function loadVmc() {
  const res = await fetch("/api/climate-admin/extra-dimmers");
  const data = await res.json();
  const input = document.getElementById("extra-vmc-id");
  const hint = document.getElementById("vmc-suggestion");
  const mode = document.getElementById("extra-vmc-mode");
  input.value = data.vmc_device_id || "";
  mode.value = data.vmc_mode || "none";
  vmcRestartPending = !!data.restart_pending;
  setPill("vmc-summary", rolePillHtml(data.vmc_mode || "none", vmcRestartPending, null,
    data.vmc_mode === "shelly" && !data.vmc_device_id));
  // Not managed here yet, but config.yaml already has one: pre-fill it,
  // and say that saving takes it over.
  if (!data.vmc_device_id && data.suggested_vmc) {
    input.value = data.suggested_vmc.device_id;
    mode.value = "shelly";
    hint.textContent = tr("climate_admin.vmc_suggestion", { name: data.suggested_vmc.name || "?" });
    hint.hidden = false;
  } else {
    hint.hidden = true;
  }
  syncExtraRoleFields();
  updateRestartBar();
}

async function saveVmc(button) {
  const resultEl = document.getElementById("save-vmc-result");
  const body = {
    vmc_device_id: document.getElementById("extra-vmc-id").value.trim() || null,
    vmc_mode: document.getElementById("extra-vmc-mode").value,
  };
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/extra-dimmers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = tr("sensors.error_prefix") + apiError(data);
      resultEl.className = "hint-text admin-error";
      return;
    }
    resultEl.innerHTML = `${tr("climate_admin.save_success")} ${restartButtonHtml()}`;
    resultEl.className = "hint-text admin-success";
    await loadVmc();
    refreshVmcStatus();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

// The VMC's night/day/boost percentages (services/vmc_speeds.py): in use
// as soon as they're saved, on the cards and in the programs.
async function loadVmcSpeeds() {
  const box = document.getElementById("vmc-speeds");
  if (!box) return;
  let data;
  try {
    data = await (await fetch("/api/climate-admin/vmc-speeds", { cache: "no-store" })).json();
  } catch (e) {
    return;
  }
  box.hidden = !(data.ok && data.device);
  if (box.hidden) return;
  box.querySelectorAll("[data-speed]").forEach(input => {
    if (document.activeElement !== input) input.value = data.speeds[input.dataset.speed] ?? "";
  });
}

async function saveVmcSpeeds(button) {
  const resultEl = document.getElementById("save-vmc-speeds-result");
  const body = {};
  document.querySelectorAll("#vmc-speeds [data-speed]").forEach(input => {
    body[input.dataset.speed] = input.value === "" ? null : Number(input.value);
  });
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/vmc-speeds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = tr("sensors.error_prefix") + apiError(data);
      resultEl.className = "hint-text admin-error";
      return;
    }
    resultEl.textContent = tr("climate_admin.vmc_speeds_saved");
    resultEl.className = "hint-text admin-success";
    await loadVmcSpeeds();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

async function refreshVmcStatus() {
  const slot = document.getElementById("vmc-status");
  if (!slot) return;
  const vmcMode = document.getElementById("extra-vmc-mode").value;
  if (vmcMode === "none" || (vmcMode === "shelly" && !document.getElementById("vmc-suggestion").hidden)) {
    morph(slot, "");
    return;
  }
  if (vmcMode === "shelly" && !document.getElementById("extra-vmc-id").value.trim()) {
    morph(slot, `<div>${todoHtml()}</div>`);
    return;
  }
  if (vmcRestartPending) {
    morph(slot, `<p class="admin-restart-pending-inline">${tr("climate_admin.restart_pending")} ${restartButtonHtml()}</p>`);
    setPill("vmc-summary", rolePillHtml(vmcMode, true, null));
    return;
  }
  try {
    const [switchRes, speedRes] = await Promise.all([
      fetch(`/api/sensors/${encodeURIComponent(VMC_NAME)}`),
      fetch(`/api/sensors/${encodeURIComponent(VMC_SPEED_NAME)}`),
    ]);
    const sw = await switchRes.json();
    const speed = await speedRes.json();
    setPill("vmc-summary", rolePillHtml(vmcMode, false, sw.ok ? "ok" : "bad"));
    if (!sw.ok) {
      morph(slot, `<div><span class="admin-status bad">${tr("climate_admin.vmc_offline")}</span> ${tr("climate_admin.vmc_offline_hint")}</div>`
        + (sw.error ? `<div class="admin-problem-detail">${escapeHtml(sw.error)}</div>` : ""));
      return;
    }
    const state = sw.is_on ? tr("climate_admin.vmc_on") : tr("climate_admin.vmc_off");
    const speedText = speed.ok && typeof speed.value === "number"
      ? ` · ${tr("climate_admin.vmc_speed", { value: Math.round(speed.value) })}` : "";
    const badge = vmcMode === "simulated"
      ? `<span class="admin-pill simulated">${tr("climate_admin.simulated_pill")}</span>`
      : `<span class="admin-status ok">${tr("climate_admin.vmc_reachable")}</span>`;
    morph(slot, `<div>${badge} ${state}${speedText}</div>`);
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place.
  }
}

// -------------------------------------------------------------- status ----
// Reuses /api/climate/zones as-is (see web/app.py) - joined client-side
// against the *saved* map by zone name, so a config edit not yet applied
// (restart pending) never gets confused with the live-running mapping.

// The usual Wi-Fi signal bands, in words.
function wifiQuality(dbm) {
  if (dbm >= -60) return "good";
  if (dbm >= -75) return "fair";
  return "poor";
}

function wifiLabel(zone) {
  if (zone.wifi_rssi_dbm == null) return "";
  return tr(`climate.wifi_${wifiQuality(zone.wifi_rssi_dbm)}`, { value: Math.round(zone.wifi_rssi_dbm) });
}

// A battery H&T only reports on a >=0.5C/5% change or an unconditional
// heartbeat every ~2h (confirmed live: an overnight watch of two real
// sensors landed every heartbeat within about a minute of exactly 2h,
// regardless of firmware version or WiFi signal) - so an age climbing
// toward 2h is completely normal, not a sign of trouble. Showing the
// raw age alone ("110m fa") reads as a warning to anyone who doesn't
// already know that; this is the one place that context gets attached
// to the number itself, right where the age is displayed, instead of
// only living in the "how this works" reference section above.
const NORMAL_AGE_CONTEXT_SECONDS = 1800; // 30 min - below this, age alone needs no caveat
function freshnessNote(zone, mode) {
  if (mode !== "shelly") return ""; // only this device family has a known, steady heartbeat to compare against
  const seconds = zone.sensor_seconds_since_update;
  if (seconds == null) return "";
  const time = formatAge(seconds);
  // On USB-C it reports every 6 minutes: an older value is no "normal cadence".
  if (seconds < NORMAL_AGE_CONTEXT_SECONDS || zone.external_power) {
    return ` · ${tr("sensors.updated_ago", { time })}`;
  }
  return ` · ${tr("climate_admin.updated_ago_normal_cadence", { time })}`;
}

function sensorStatusHtml(zone, mode) {
  // No id saved yet: nothing to read.
  if (zone.sensor_configured === false) return todoHtml();
  if (mode === "simulated") {
    return `<span class="admin-pill simulated">${tr("climate_admin.simulated_pill")}</span> ${fmt(zone.temperature_c, 1)}°C`;
  }
  if (mode === "manual") {
    if (zone.sensor_ok) {
      return `<span class="admin-pill manual">${tr("climate_admin.manual_pill")}</span> ${fmt(zone.temperature_c, 1)}°C`;
    }
    return `<span class="admin-status neutral">${tr("climate_admin.manual_no_value")}</span>`;
  }
  // shelly (or unknown - default to the same real-device reading)
  if (zone.sensor_ok || zoneHasLastKnownValue(zone)) {
    let extra = "";
    if (zone.humidity_pct != null) extra += ` · ${fmt(zone.humidity_pct, 0)}%`;
    if (zone.external_power) {
      extra += ` · ${tr("climate_admin.shelly_usb")}`;
    } else if (zone.battery_pct != null) {
      // A low battery highlighted, so this page alone is enough to
      // notice it.
      const batteryClass = zone.battery_pct <= LOW_BATTERY_PCT ? "admin-status bad" : "";
      const batteryText = `${tr("fields.battery_pct")} ${fmt(zone.battery_pct, 0)}%`;
      extra += ` · ${batteryClass ? `<span class="${batteryClass}">${batteryText}</span>` : batteryText}`;
    }
    const wifi = wifiLabel(zone);
    if (wifi) extra += ` · ${wifi}`;
    // Every MQTT topic went stale (see drivers/mqtt/sensor.py's
    // read()) but at least one still has a last-known value - most
    // often a battery sensor between wake cycles, not broken hardware.
    // Neutral, not the red "offline" pill below: the value shown is
    // real, just not necessarily current.
    if (!zone.sensor_ok) {
      // Just after a power-up: not heard from since, which is normal.
      const note = (zone.sensor_waiting ? `${tr("climate_admin.first_report_after_boot")} - ` : "")
        + staleValueNote(zone.sensor_last_value_age_seconds);
      return `<span class="admin-status neutral">${note}</span> ${fmt(zone.temperature_c, 1)}°C${extra}`;
    }
    return `<span class="admin-status ok">${tr("climate_admin.reporting")}</span> ${fmt(zone.temperature_c, 1)}°C${extra}${freshnessNote(zone, mode)}`;
  }
  const error = zone.sensor_error || "";
  if (error.includes("no MQTT message received yet")) {
    return `<span class="admin-status neutral">${tr("climate_admin.not_reachable_battery")}</span>`;
  }
  // Same red "offline" treatment as the stale-inferred case below, just
  // a stronger, LWT-confirmed reason (drivers/mqtt/base.py's
  // Availability) - reachable in practice only when this device has
  // never sent a single message in this process's lifetime (so there's
  // no last-known value at all to fall back to above) while its
  // broker-side availability was already false at that moment, e.g.
  // right after a restart with the device already disconnected.
  if (error.includes("marked offline on its availability topic") || error.includes("probably offline")) {
    return `<span class="admin-status bad">${tr("climate_admin.not_reachable_offline")}</span>`;
  }
  return `<span class="admin-status neutral">${error || tr("climate_admin.not_reachable_generic")}</span>`;
}

function valveStatusHtml(zone, valveMode) {
  if (zone.valve_configured === false) return todoHtml();
  const pill = valveMode === "simulated" ? `<span class="admin-pill simulated">${tr("climate_admin.simulated_pill")}</span> ` : "";
  if (zone.valve_ok) {
    const driven = zone.valve_driver && zone.valve_driver.on
      ? ` · ${tr("home.st_driven", { name: displayName(zone.valve_driver.name) })}` : "";
    const state = (zone.valve_on ? tr("climate_admin.valve_open") : tr("climate_admin.valve_closed")) + driven;
    return `${pill}<span class="admin-status ok">${tr("climate_admin.reporting")}</span> ${state}`;
  }
  return `${pill}<span class="admin-status bad">${tr("climate_admin.valve_not_reachable")}</span>`;
}

function targetHtml(zone) {
  const target = zone.target_c != null ? `${fmt(zone.target_c, 1)}°C` : tr("climate.no_target");
  return `${tr("climate_admin.target_label")}: ${target}`;
}

// A cool_enabled:false zone (e.g. a condensation-prone bathroom) is
// still shown here in every mode, because this page's
// whole job is showing *why* a valve is in the state it's in, and
// silently disappearing a configured zone would be more confusing, not
// less. What it's *not* driven by needs saying instead: the thermostat
// doesn't touch this valve for temperature at all in this mode, so
// whatever state it's actually in is either idle or another mechanism
// entirely (an interlock/mode_switch - see docs/configuration.md).
function modeExclusionNoticeHtml(zone, mode) {
  if (mode !== "cool" || zone.cool_enabled !== false) return "";
  return `<p class="admin-mode-exclusion">${tr("climate_admin.zone_excluded_cool")}</p>`;
}

// Surfaces services/thermostat.py's ThermostatController failsafe (a
// zone whose sensor has been unreachable long enough gets its valve
// forced closed rather than left frozen in whatever it was doing
// forever) - this, not a Telegram push, is where that gets seen.
// sensor_unreachable_hours is only non-null while *currently*
// unreachable (cleared the instant the sensor reports again - see
// ThermostatController._on_sensor_recovered()), so this naturally
// disappears again on recovery with no extra state to track here.
function failsafeNoticeHtml(zone) {
  if (zone.sensor_unreachable_hours == null) return "";
  if (zone.sensor_failsafe_active) {
    return `<p class="admin-mode-exclusion">${tr("climate_admin.failsafe_active", { hours: fmt(zone.sensor_unreachable_hours, 1) })}</p>`;
  }
  return `<p class="hint-text">${tr("climate_admin.failsafe_pending", {
    hours: fmt(zone.sensor_unreachable_hours, 1), threshold: fmt(zone.sensor_failsafe_hours, 0),
  })}</p>`;
}

// Fills #zone-status-N inside zoneFormHtml()'s own card - deliberately
// not a standalone row with its own name/border the way this used to
// render in a separate "Live status" section: the card it's inside
// already has the name and the fields that determine this state, so
// repeating either here would just be distance-adding noise.
function zoneStatusInnerHtml(zone, mapEntry, mode) {
  const sensorMode = mapEntry ? mapEntry.sensor_mode : "shelly";
  const valveMode = mapEntry ? (mapEntry.valve_mode || "shelly") : "shelly";
  // Right under the sensor's line, titled: it *is* the sensor while the
  // zone is in manual mode. Prefilled with the value in use.
  const manualValue = value => (zone.sensor_ok && value != null ? String(value) : "");
  const manualBoxHtml = sensorMode === "manual" ? `
    <div class="admin-manual-box">
      <p class="admin-manual-title">${tr("climate_admin.manual_box_title")}</p>
      <label>${tr("climate_admin.manual_temp_label")}
        <input type="number" step="0.1" min="-30" max="60" class="manual-temp-input" value="${manualValue(zone.temperature_c)}"></label>
      <label>${tr("climate_admin.manual_humidity_label")}
        <input type="number" step="0.1" min="0" max="100" class="manual-humidity-input" value="${manualValue(zone.humidity_pct)}"></label>
      <button class="range-btn" onclick="setManualValue(${jsArg(zone.name)}, this)">${tr("climate_admin.manual_set_button")}</button>
      <p class="hint-text">${tr("climate_admin.manual_box_note")}</p>
    </div>` : "";

  return `
    <div>${tr("climate_admin.sensor_label")}: ${sensorStatusHtml(zone, sensorMode)}</div>
    ${manualBoxHtml}
    ${failsafeNoticeHtml(zone)}
    <div>${targetHtml(zone)}</div>
    <div>${tr("climate_admin.valve_label")}: ${valveStatusHtml(zone, valveMode)}</div>
    ${modeExclusionNoticeHtml(zone, mode)}
  `;
}

// The one thing that decides which formula every zone's valve below
// follows - see climate_admin.css's .admin-mode-banner. Spelled out
// explicitly (not just "Heating"/"Cooling") because a zone card below
// only shows "open"/"closed", never *why* - this is the only place on
// this page that says which direction is currently correct.
function modeAwayLineHtml(data) {
  if (!data.mode) return "";
  const isCool = data.mode === "cool";
  const icon = isCool ? "❄️" : "🔥";
  const modeLabel = isCool ? tr("climate.mode_cool") : tr("climate.mode_heat");
  const formula = isCool ? tr("climate_admin.mode_formula_cool") : tr("climate_admin.mode_formula_heat");
  const awayText = data.away_until
    ? tr("climate.away_active", { until: new Date(data.away_until).toLocaleString() })
    : tr("climate.away_idle");
  return `<span class="admin-mode-icon">${icon}</span> <strong>${modeLabel}</strong> - ${formula} · ${awayText}`;
}

function isTypingAManualValue() {
  const active = document.activeElement;
  return !!(active && active.matches && active.matches(".manual-temp-input, .manual-humidity-input"));
}

// ---------------------------------------------------------- diagnostic mode --
// System-wide override (services/thermostat.py's ScheduleStore.
// get_diagnostic_mode()) - while on, ThermostatController and
// InterlockController both back off every valve entirely, so a manual
// command here is the *only* thing moving anything, and it sticks
// until changed again (or diagnostic mode turns off) instead of being
// reasserted by the next control tick a few seconds later.

let diagnosticModeEnabled = false;
// Every switch tagged dashboard_tab: "climate" - not just this panel's
// own fixed two zone valves and two extra-switch roles: an apartment
// that still hand-configures its bypass valve/dehumidifier in
// config.yaml directly (own name, own dashboard_tab: "climate", not
// migrated to this panel's extra_switches store) must see those here
// too, since diagnostic mode's whole point is "every valve this
// installation has", not just the ones this panel happens to manage.
// Fetched once at load from /api/devices (only ever lists drivers
// DeviceManager actually built, so nothing not-yet-restarted shows up
// as controllable before it actually is).
let climateSwitches = [];   // {name, normallyOpen}: the switches diagnostic mode drives

async function loadClimateSwitchNames() {
  try {
    const res = await fetch("/api/devices");
    const data = await res.json();
    climateSwitches = (data.sensors || [])
      .filter(s => s.kind === "switch" && s.tab === "climate")
      .map(s => ({ name: s.name, normallyOpen: !!s.normally_open }));
  } catch (e) {
    climateSwitches = [];
  }
}

async function onDiagnosticModeToggle(checkbox) {
  const enabled = checkbox.checked;
  if (enabled && !await confirmAction(tr("climate_admin.diagnostic_confirm_on"), { danger: true })) {
    checkbox.checked = false;
    return;
  }
  checkbox.disabled = true;
  try {
    const res = await fetch("/api/climate/diagnostic-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const data = await res.json();
    if (!data.ok) {
      notify(tr("sensors.error_prefix") + apiError(data));
      checkbox.checked = !enabled;
      return;
    }
  } catch (e) {
    notify(tr("sensors.request_failed"));
    checkbox.checked = !enabled;
    return;
  } finally {
    checkbox.disabled = false;
  }
  refreshStatus();
}

// The valve's state first, the Shelly's contact after it: "open" and
// "close" always mean the valve. A zone valve opens when the contact
// closes; a normally open one (the bypass) closes when it does.
function diagnosticRowHtml(row) {
  const contact = tr(row.isOn ? "climate_admin.diag_contact_closed" : "climate_admin.diag_contact_open");
  let state;
  let buttons;
  if (row.valve) {
    const open = row.normallyOpen ? !row.isOn : row.isOn;
    state = `<span class="admin-status ${open ? "ok" : "neutral"}">${tr(open ? "climate_admin.valve_open" : "climate_admin.valve_closed")}</span>`;
    const [openAction, closeAction] = row.normallyOpen ? ["off", "on"] : ["on", "off"];
    buttons = `
        <button class="range-btn" onclick="diagnosticToggle(${jsArg(row.name)}, '${openAction}', this)">${tr("climate_admin.diag_open_valve")}</button>
        <button class="range-btn" onclick="diagnosticToggle(${jsArg(row.name)}, '${closeAction}', this)">${tr("climate_admin.diag_close_valve")}</button>`;
  } else {
    state = `<span class="admin-status ${row.isOn ? "ok" : "neutral"}">${tr(row.isOn ? "climate_admin.diag_on" : "climate_admin.diag_off")}</span>`;
    buttons = `
        <button class="range-btn" onclick="diagnosticToggle(${jsArg(row.name)}, 'on', this)">${tr("climate_admin.diag_turn_on")}</button>
        <button class="range-btn" onclick="diagnosticToggle(${jsArg(row.name)}, 'off', this)">${tr("climate_admin.diag_turn_off")}</button>`;
  }
  if (!row.ok) state = `<span class="admin-status bad">${tr("climate_admin.valve_not_reachable")}</span>`;
  return `
    <div class="admin-zone-card admin-diagnostic-row">
      <div><strong>${escapeHtml(displayName(row.name))}</strong></div>
      <div>${state}${row.ok ? ` <span class="hint-text diag-contact">${escapeHtml(contact)}</span>` : ""}</div>
      <div class="toolbar">${buttons}
      </div>
    </div>
  `;
}

// The generic on/off route every controllable device has: while
// diagnostic mode is on no thermostat/interlock tick reasserts anything,
// so this command is the valve's new, lasting state - the one way to
// move a valve by hand from this page.
async function diagnosticToggle(name, action, button) {
  button.disabled = true;
  try {
    const res = await fetch(`/api/sensors/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) notify(tr("sensors.error_prefix") + apiError(data));
    refreshStatus();
  } catch (e) {
    notify(tr("sensors.request_failed"));
  } finally {
    button.disabled = false;
  }
}

async function refreshDiagnosticSection(data) {
  const toggle = document.getElementById("diagnostic-mode-toggle");
  const banner = document.getElementById("diagnostic-mode-banner");
  const container = document.getElementById("diagnostic-valves");
  const wasEnabled = diagnosticModeEnabled;
  diagnosticModeEnabled = !!data.diagnostic_mode;
  if (toggle) toggle.checked = diagnosticModeEnabled;
  if (banner) banner.hidden = !diagnosticModeEnabled;
  const alert = document.getElementById("diagnostic-alert");
  if (alert) alert.hidden = !diagnosticModeEnabled;
  setPill("diagnostic-summary", pillHtml(diagnosticModeEnabled ? "bad" : "idle",
    tr(diagnosticModeEnabled ? "climate_admin.diag_pill_on" : "climate_admin.diag_pill_off")));
  // Turned on - here or from another phone: its section opens by itself.
  const section = document.getElementById("diagnostic");
  if (section && diagnosticModeEnabled && !wasEnabled) section.open = true;
  if (!container) return;
  if (!diagnosticModeEnabled) {
    morph(container, "");
    return;
  }
  // A valve - a zone's, or a normally open one like the bypass - is
  // opened and closed; anything else (the dehumidifier) switched on and off.
  const zoneValves = new Set((data.zones || []).map(z => z.valve));
  const rows = [];
  for (const device of climateSwitches) {
    const row = { ...device, valve: zoneValves.has(device.name) || device.normallyOpen };
    try {
      const reading = await (await fetch(`/api/sensors/${encodeURIComponent(device.name)}`)).json();
      rows.push({ ...row, isOn: !!reading.is_on, ok: !!reading.ok });
    } catch (e) {
      rows.push({ ...row, isOn: false, ok: false });
    }
  }
  morph(container, `<p class="hint-text">${tr("climate_admin.diag_contact_note")}</p>`
    + `<div class="admin-diagnostic-grid">${rows.map(diagnosticRowHtml).join("")}</div>`);
}

async function refreshStatus() {
  // Only ever writes into #zone-status-N (one per card, appended by
  // zoneFormHtml()) - never the card as a whole, so a poll tick never
  // wipes out a device id or a manual-value input mid-edit. The
  // isTypingAManualValue() guard below still applies on top of that,
  // since even an untouched innerHTML write would blur a focused
  // input by replacing its container's content around it.
  if (isTypingAManualValue()) return;
  const mapZones = (savedMap && savedMap.zones) || [];
  try {
    const res = await fetch("/api/climate/zones");
    const data = await res.json();
    morph(document.getElementById("mode-away-line"), modeAwayLineHtml(data));
    renderSeason(data);
    await refreshSeasonRules(data);
    await refreshDiagnosticSection(data);
    // A single all-or-nothing signal, not per-zone: a restart rebuilds
    // every zone's drivers together, so "one thing changed" means
    // every card's status is stale, not just the zone that changed -
    // see web/app.py's ACTIVE_CLIMATE_ZONES/ACTIVE_EXTRA_SWITCHES.
    const restartPending = (savedMap && savedMap.restart_pending) || extraSwitchesRestartPending;
    updatePlantOverview(data, restartPending);
    deviceZones = devicesByZone(data);
    setupTodo = data.to_configure || [];
    renderDevices();
    const byName = {};
    (data.zones || []).forEach(z => { byName[z.name] = z; });
    mapZones.forEach((mapEntry, index) => {
      const slot = document.getElementById(`zone-status-${index}`);
      if (!slot) return;
      if (restartPending) {
        morph(slot, `<p class="admin-restart-pending-inline">${tr("climate_admin.restart_pending")} ${restartButtonHtml()}</p>`);
        return;
      }
      const zone = byName[mapEntry.name];
      morph(slot, zone ? zoneStatusInnerHtml(zone, mapEntry, data.mode) : `<p class="hint-text">${tr("climate_admin.status_none")}</p>`);
    });
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place and
    // keep polling rather than blanking the page.
  }
}

// Raw TCP reachability, not "is any zone reporting" - the two are
// deliberately checked separately (see services/mqtt_broker_status.py)
// so a broker that's simply not running isn't mistaken for a device
// fault ("not reachable yet" on every zone at once is a broker problem,
// not a Shelly button worth pressing).
async function refreshBroker() {
  const el = document.getElementById("broker-status");
  try {
    const res = await fetch("/api/climate-admin/broker-status");
    const data = await res.json();
    const hint = document.getElementById("broker-down-hint");
    // No mqtt_broker.json at all: nothing to reach yet - an installation
    // with only simulated devices, not a broker that's down.
    if (!data.host) {
      morph(el, pillHtml("idle", tr("climate_admin.broker_not_configured")));
      if (hint) hint.hidden = true;
      brokerView = { configured: false };
      updateDevicesTile();
      return;
    }
    const where = `${data.host}:${data.port || "?"}`;
    const text = tr(data.reachable ? "climate_admin.broker_ok" : "climate_admin.broker_down");
    morph(el, `${pillHtml(data.reachable ? "ok" : "bad", text)} <span class="sec-note">${escapeHtml(where)}</span>`);
    if (hint) hint.hidden = !!data.reachable;
    brokerView = { configured: true, reachable: !!data.reachable, where };
    updateDevicesTile();
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place.
  }
}

async function refreshAll() {
  await refreshBroker();
  await refreshStatus();
  await refreshExtraSensorStatus();
  await refreshVmcStatus();
  await refreshSystemStatus();
  refreshLedSummary();
  refreshShellyRevision();
}

// While its section is closed the LED matrix isn't polled; its header
// and tile still follow it, at the system status's pace.
let ledSummaryFetchedAt = 0;

function refreshLedSummary() {
  if (ledTimer || Date.now() - ledSummaryFetchedAt < SYSTEM_STATUS_INTERVAL_MS) return;
  ledSummaryFetchedAt = Date.now();
  refreshLedMatrix();
}

async function setManualValue(zoneName, button) {
  const box = button.closest(".admin-manual-box");
  const temperature_c = parseFloat(box.querySelector(".manual-temp-input").value);
  if (Number.isNaN(temperature_c)) {
    notify(tr("climate_admin.manual_temp_required"));
    return;
  }
  // The same limits the server enforces, said here in the page's language:
  // a typo like 215 for 21.5 would otherwise drive the valves on nonsense.
  if (temperature_c < -30 || temperature_c > 60) {
    notify(tr("climate_admin.manual_temp_range"));
    return;
  }
  const humidityRaw = box.querySelector(".manual-humidity-input").value;
  const body = { temperature_c };
  if (humidityRaw !== "") {
    body.humidity_pct = parseFloat(humidityRaw);
    if (Number.isNaN(body.humidity_pct) || body.humidity_pct < 0 || body.humidity_pct > 100) {
      notify(tr("climate_admin.manual_humidity_range"));
      return;
    }
  }
  try {
    const res = await fetch(`/api/climate-admin/zones/${encodeURIComponent(zoneName)}/manual-value`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      notify(tr("sensors.error_prefix") + apiError(data));
      return;
    }
    refreshStatus();
  } catch (e) {
    notify(tr("sensors.request_failed"));
  }
}

// ------------------------------------------------------- system status ----
// What's wrong with the installation itself rather than with one zone:
// integrations that failed to load (and how to fix them), devices that
// stopped reporting, background loops that stopped cycling, and the
// process/disk/database numbers that tell a slow leak or a filling disk
// apart from normal - see GET /api/system-status. Polled far less often
// than the zones: none of it changes second to second.

const SYSTEM_STATUS_INTERVAL_MS = 15000;
let systemStatusFetchedAt = 0;

// The server's refusal in this page's language: its code and parameters
// (errors.<code> in the locale files, see localhome/errors.py), a
// setting's own label for its key, and the English message only when
// there's no translation.
function apiError(data) {
  if (!data) return "";
  const params = Object.assign({}, data.params || {});
  if (params.key) {
    const label = tr(`climate_admin.setting_${params.key}`);
    params.label = label.startsWith("climate_admin.") ? params.key : label;
  }
  if (params.period) {
    const period = tr(`errors.period_${params.period}`);
    if (!period.startsWith("errors.")) params.period = period;
  }
  if (typeof data.code === "string") {
    const text = tr(`errors.${data.code}`, params);
    if (text !== `errors.${data.code}`) return text;
  }
  return data.error || "";
}

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// The fixed English names the panel gives its extra devices (see
// services/climate_map_store.py), shown in the page's language - same
// table as home.js's.
const DISPLAY_NAMES = {
  "Outdoor Sensor": "climate.outdoor_name",
  "Bathroom Bypass Valve": "climate.bypass_name",
  "Dehumidifier": "climate.dehumidifier",
};

function displayName(name) {
  return DISPLAY_NAMES[name] ? tr(DISPLAY_NAMES[name]) : name;
}

function escapeAttr(value) {
  return escapeHtml(value);
}

// A name as an inline handler's argument: a JSON string literal (any
// apostrophe or quote inside is fine), escaped for the attribute.
function jsArg(value) {
  return escapeHtml(JSON.stringify(String(value)));
}

function formatBytes(bytes) {
  if (typeof bytes !== "number") return "--";
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}

// Longer spans than formatAge() shows well ("3d 4h", not just "3d").
function formatDuration(seconds) {
  if (typeof seconds !== "number") return "--";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function statRowHtml(label, value, bad) {
  return `<div class="admin-stat${bad ? " bad" : ""}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function problemTitle(problem) {
  const subject = problem.code === "protection_missing"
    ? tr(`climate_admin.protection_${problem.subject}`) : deviceLabel(problem.subject || "");
  return tr(`climate_admin.system_problem_${problem.code}`, { subject });
}

function problemHtml(problem) {
  const title = problemTitle(problem);
  const text = problem.settings ? problem.settings.map(key => tr(`climate_admin.shelly_setting_${key}`)).join(", ") : problem.detail;
  const detail = text ? `<div class="admin-problem-detail">${escapeHtml(text)}</div>` : "";
  return `<li>${escapeHtml(title)}${detail}</li>`;
}

function wifiSignalLabel(percent) {
  if (percent >= 70) return tr("climate_admin.system_wifi_excellent");
  if (percent >= 50) return tr("climate_admin.system_wifi_good");
  if (percent >= 30) return tr("climate_admin.system_wifi_fair");
  return tr("climate_admin.system_wifi_poor");
}

function networkRows(network) {
  if (!network) return "";
  const rows = [];
  const wifi = network.wifi;
  if (wifi) {
    rows.push(statRowHtml(tr("climate_admin.system_wifi_network"), wifi.ssid || "--"));
    if (typeof wifi.signal_percent === "number") {
      rows.push(statRowHtml(tr("climate_admin.system_wifi_signal"),
        `${wifi.signal_percent}% (${wifiSignalLabel(wifi.signal_percent)})`, wifi.signal_percent < 30));
    }
    const band = [wifi.band, wifi.channel ? tr("climate_admin.system_wifi_channel", { channel: wifi.channel }) : null].filter(Boolean).join(", ");
    if (band) rows.push(statRowHtml(tr("climate_admin.system_wifi_band"), band));
    rows.push(statRowHtml(tr("climate_admin.system_ip"), wifi.ip || "--"));
  } else {
    rows.push(statRowHtml(tr("climate_admin.system_wifi_network"), tr("climate_admin.system_wifi_none")));
  }
  rows.push(statRowHtml(tr("climate_admin.system_name"), network.mdns_name));
  return rows.join("");
}

function formatWhen(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString(LOCALE, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

// "21 set, 18:35 (1d 4h fa)" - when, and how long it's been running.
function sinceText(epochSeconds, uptimeSeconds) {
  if (typeof epochSeconds !== "number") return formatDuration(uptimeSeconds);
  return tr("climate_admin.system_since", { when: formatWhen(epochSeconds), ago: formatDuration(uptimeSeconds) });
}

function versionText(software) {
  if (!software || !software.commit) return "--";
  const date = software.date ? new Date(software.date).toLocaleDateString(LOCALE, { day: "2-digit", month: "short", year: "numeric" }) : "";
  return date ? `${software.commit} · ${date}` : software.commit;
}

function bootsHtml(boots) {
  if (!boots || !boots.length) return "";
  const rows = boots.map(b => `<li><span>${escapeHtml(formatWhen(b.booted_at))}</span>`
    + `<span class="boot-end boot-${b.ended}">${escapeHtml(tr(`climate_admin.system_boot_${b.ended}`))}</span></li>`).join("");
  return `<h3>${tr("climate_admin.system_boots_heading")}</h3>
    <p class="hint-text">${tr("climate_admin.system_boots_note")}</p>
    <ul class="admin-device-list boot-list">${rows}</ul>`;
}

// What deploy/tune-board.sh sets up for running all year on Wi-Fi - on,
// missing, or not applicable on this board. Null off a board (a PC).
// What the board's hardware says about itself (system_status.board_hardware):
// read from files on every refresh, left out where a machine hasn't it.

function boardText(hw) {
  return [hw.model, hw.soc, hw.serial ? tr("climate_admin.system_board_serial", { serial: hw.serial }) : ""]
    .filter(Boolean).join(" · ");
}

// "41°C (slows down at 90°C)": how far from the kernel cooling it by
// lowering its speed.
function cpuTempText(temp, throttle) {
  if (typeof temp !== "number") return "--";
  return throttle ? tr("climate_admin.system_cpu_temp_value", { value: temp.toFixed(0), throttle: Math.round(throttle) })
    : `${temp.toFixed(0)}°C`;
}

function otherTempsText(temps) {
  return ["wifi", "gpu", "modem"].filter(k => temps[k] != null)
    .map(k => `${tr(`climate_admin.system_temp_${k}`)} ${Math.round(temps[k])}°C`).join(" · ");
}

function cpuFreqText(hw) {
  const ghz = mhz => (mhz / 1000).toLocaleString(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  if (hw.cpu_capped) return tr("climate_admin.system_cpu_freq_capped", { max: ghz(hw.cpu_mhz_max) });
  const text = tr("climate_admin.system_cpu_freq_value", { now: ghz(hw.cpu_mhz_now), max: ghz(hw.cpu_mhz_max) });
  return hw.cpu_top_share == null ? text
    : `${text} · ${tr("climate_admin.system_cpu_top_share", { share: Math.round(hw.cpu_top_share * 100) })}`;
}

// Errors are counted since boot, against every packet: a handful in a
// hundred thousand is radio noise, more than 1 in 100 is a bad link.
function wifiErrorsRow(hw) {
  if (hw.wifi_packets == null) return "";
  const share = hw.wifi_packets ? hw.wifi_errors / hw.wifi_packets : 0;
  return statRowHtml(tr("climate_admin.system_wifi_errors"),
    tr("climate_admin.system_wifi_errors_value", { errors: hw.wifi_errors.toLocaleString(LOCALE), packets: hw.wifi_packets.toLocaleString(LOCALE) }),
    share > 0.01);
}

// The 5-minute load average as a share of the board's processors, in
// words: "1.2 / 0.9 / 0.7 (4 CPU)" meant nothing to most people. At or
// past 100% there's more work than the processors keep up with.
function loadRowHtml(h) {
  if (!h.load_average || !h.cpu_count) return statRowHtml(tr("climate_admin.system_load"), "--");
  const percent = Math.round(h.load_average[1] / h.cpu_count * 100);
  const level = percent >= 100 ? "high" : percent >= 50 ? "medium" : "low";
  return statRowHtml(tr("climate_admin.system_load"), tr(`climate_admin.system_load_${level}`, { percent }), level === "high");
}

function protectionRows(protections) {
  if (!protections) return "";
  return Object.entries(protections.items).map(([key, on]) => statRowHtml(
    tr(`climate_admin.protection_${key}`),
    tr(on === null ? "climate_admin.protection_na" : on ? "climate_admin.protection_on" : "climate_admin.protection_off"),
    on === false)).join("");
}

// One watchdog action in words: the log keeps a code and its values.
function watchdogEventText(event) {
  const values = event.values ? event.values.split(" ") : [];
  // A Shelly's safety timer names the device, as the rest of the page does.
  const vars = { a: values[0] || "", b: values[1] || "",
    all: event.code === "safety_timer" ? displayName(event.values) : event.values };
  if (event.code === "back") {
    const seconds = Number(values[0]);
    vars.a = seconds < 60 ? `${seconds}s` : formatDuration(seconds);
  }
  const text = tr(`climate_admin.wd_${event.code}`, vars);
  return text.startsWith("climate_admin.") ? `${event.code} ${event.values}` : text;
}

function watchdogHistoryHtml(events) {
  const rows = (events || []).map(event => {
    const detail = event.code === "unhealthy" && event.values
      ? `<div class="admin-problem-detail">${escapeHtml(event.values)}</div>` : "";
    return `<li><div class="admin-device-line">
        <span class="admin-pill">${escapeHtml(tr(`climate_admin.watchdog_source_${event.source}`))}</span>
        <span>${escapeHtml(watchdogEventText(event))}</span>
        <span class="admin-device-age">${escapeHtml(formatWhen(Date.parse(event.when.replace(" ", "T")) / 1000))}</span>
      </div>${detail}</li>`;
  }).join("");
  return `<h3>${tr("climate_admin.watchdog_heading")}</h3>
    <p class="hint-text">${tr("climate_admin.watchdog_note")}</p>
    ${rows ? `<ul class="admin-device-list">${rows}</ul>` : `<p class="hint-text">${tr("climate_admin.watchdog_none")}</p>`}`;
}

function systemStatusHtml(data) {
  const p = data.process || {};
  const h = data.host || {};
  const disk = data.disk || {};
  const db = data.database || {};

  const loopRows = (data.loops || []).map(loop => statRowHtml(
    tr(`climate_admin.system_loop_${loop.name}`),
    loop.last_ok_seconds_ago == null
      ? tr("climate_admin.system_loop_never")
      : tr("climate_admin.system_loop_ago", { age: formatAge(loop.last_ok_seconds_ago), every: formatAge(loop.interval_seconds) }),
    loop.stalled,
  )).join("");

  const oldest = typeof db.oldest_ts === "number"
    ? new Date(db.oldest_ts * 1000).toLocaleDateString(LOCALE, { day: "2-digit", month: "short" })
    : "--";
  const freeOf = (free, total) => (typeof total === "number"
    ? tr("climate_admin.system_free_of", { free: formatBytes(free), total: formatBytes(total) })
    : "--");
  const count = n => (typeof n === "number" ? n.toLocaleString(LOCALE) : "--");

  const hw = h.hardware || {};
  const groups = [
    [tr("climate_admin.system_group_process"), [
      statRowHtml(tr("climate_admin.system_started"), sinceText(p.started_at, p.uptime_seconds)),
      statRowHtml(tr("climate_admin.system_version"), versionText(data.software)),
      statRowHtml(tr("climate_admin.system_ram"), `${formatBytes(p.rss_bytes)} (${tr("climate_admin.system_peak")} ${formatBytes(p.peak_rss_bytes)})`),
      statRowHtml(tr("climate_admin.system_cpu"), typeof p.cpu_percent === "number" ? `${p.cpu_percent.toFixed(1)}%` : "--"),
      statRowHtml(tr("climate_admin.system_threads"), p.threads ?? "--"),
      loopRows,
    ].join("")],
    [tr("climate_admin.system_group_host"), [
      hw.model ? statRowHtml(tr("climate_admin.system_board"), boardText(hw)) : "",
      statRowHtml(tr("climate_admin.system_host_ram"), freeOf(h.mem_available_bytes, h.mem_total_bytes)),
      hw.swap_total_bytes ? statRowHtml(tr("climate_admin.system_swap"),
        tr("climate_admin.system_swap_value", { used: formatBytes(hw.swap_used_bytes), total: formatBytes(hw.swap_total_bytes) })) : "",
      statRowHtml(tr("climate_admin.system_cpu_temp"), cpuTempText(h.cpu_temp_c, hw.throttle_c), h.cpu_temp_c >= 80),
      Object.keys(hw.temps || {}).length ? statRowHtml(tr("climate_admin.system_other_temps"), otherTempsText(hw.temps)) : "",
      loadRowHtml(h),
      hw.cpu_mhz_max ? statRowHtml(tr("climate_admin.system_cpu_freq"), cpuFreqText(hw), hw.cpu_capped) : "",
      statRowHtml(tr("climate_admin.system_last_boot"), sinceText(h.booted_at, h.uptime_seconds)),
      statRowHtml(tr("climate_admin.system_clock"),
        h.clock_synced == null ? "--" : tr(h.clock_synced ? "climate_admin.system_clock_ok" : "climate_admin.system_clock_bad"),
        h.clock_synced === false),
      statRowHtml(tr("climate_admin.system_os"), h.os ? `${h.os}${h.kernel ? ` · ${h.kernel}` : ""}` : "--"),
      statRowHtml(tr("climate_admin.system_disk"), freeOf(disk.free_bytes, disk.total_bytes), disk.low),
      ...(data.storage ? [
        statRowHtml(tr("climate_admin.system_emmc_wear"),
          tr(`climate_admin.system_emmc_${data.storage.pre_eol}`, {
            wear: data.storage.wear_percent_max == null ? "?" : `${Math.max(0, data.storage.wear_percent_max - 10)}-${data.storage.wear_percent_max}%`,
          }),
          data.storage.pre_eol !== "normal"),
        // Only the last hour's rate: an average since boot is mostly the
        // start-up's own writes, projected over a whole day.
        statRowHtml(tr("climate_admin.system_emmc_writes"),
          data.storage.written_per_day_basis === "recent"
            ? tr("climate_admin.system_emmc_per_day_recent",
              { amount: formatBytes(data.storage.written_per_day_bytes), minutes: data.storage.per_day_minutes })
            : tr("climate_admin.system_emmc_measuring")),
      ] : []),
    ].join("")],
    [tr("climate_admin.system_group_network"), networkRows(data.network) + wifiErrorsRow(hw)],
    ...(data.protections ? [[tr("climate_admin.system_group_protections"), protectionRows(data.protections)]] : []),
    [tr("climate_admin.system_group_history"), [
      statRowHtml(tr("climate_admin.system_db_size"), formatBytes(db.file_bytes)),
      statRowHtml(tr("climate_admin.system_db_rows_24h"), count(db.rows_24h)),
      statRowHtml(tr("climate_admin.system_db_rows"), count(db.rows)),
      statRowHtml(tr("climate_admin.system_db_oldest"), tr("climate_admin.system_db_retention", { oldest, days: db.retention_days })),
    ].join("")],
  ];
  const statsHtml = groups.map(([title, rows]) =>
    `<div class="admin-zone-card admin-stat-group"><h3>${escapeHtml(title)}</h3>${rows}</div>`).join("");

  const skipped = (data.skipped_driver_modules || []).map(s =>
    `<li><code>${escapeHtml(s.module)}</code> - ${escapeHtml(tr("climate_admin.system_skipped_line", { package: s.missing_package }))} <code>${escapeHtml(s.install)}</code></li>`).join("");
  const skippedHtml = skipped
    ? `<h3>${tr("climate_admin.system_skipped_heading")}</h3><p class="hint-text">${tr("climate_admin.system_skipped_note")}</p><ul class="admin-device-list">${skipped}</ul>`
    : "";

  return `
    <div class="admin-stat-grid">${statsHtml}</div>
    ${bootsHtml(data.boots)}
    ${data.protections || (data.watchdog_history || []).length ? watchdogHistoryHtml(data.watchdog_history) : ""}
    ${skippedHtml}
    <p class="hint-text admin-path">${escapeHtml(tr("climate_admin.system_config_path", { path: data.config_path }))}</p>`;
}

function renderSystemProblems(problems) {
  const box = document.getElementById("system-problems");
  const summary = document.getElementById("system-status-summary");
  if (!problems.length) {
    box.hidden = true;
    morph(summary, `<span class="admin-status ok">${tr("climate_admin.system_all_ok")}</span>`);
    return;
  }
  box.hidden = false;
  morph(box, `
    <strong>${tr("climate_admin.system_problems_heading", { count: problems.length })}</strong>
    <ul>${problems.map(problemHtml).join("")}</ul>`);
  morph(summary, `<span class="admin-status bad">${escapeHtml(problemsCountText(problems.length))}</span>`);
}

// Over now, worth knowing: a safety timer that acted, in the last 72
// hours - neutral, not counted with the problems.
function renderSystemRecent(events) {
  const box = document.getElementById("system-recent");
  if (!box) return;
  box.hidden = !events.length;
  morph(box, events.length ? `
    <strong>${tr("climate_admin.system_recent_heading")}</strong>
    <ul>${events.map(e => {
      const text = tr(`climate_admin.system_recent_${e.code}`, { subject: displayName(e.subject) });
      return `<li>${escapeHtml(text.startsWith("climate_admin.") ? `${e.code} ${e.subject}` : text)}
        <div class="admin-problem-detail">${escapeHtml(formatWhen(e.at))}</div></li>`;
    }).join("")}</ul>` : "");
}

async function refreshSystemStatus(force) {
  if (!force && Date.now() - systemStatusFetchedAt < SYSTEM_STATUS_INTERVAL_MS) return;
  systemStatusFetchedAt = Date.now();
  try {
    const res = await fetch("/api/system-status");
    const data = await res.json();
    if (!data.ok) return;
    renderSystemProblems(data.problems || []);
    renderSystemRecent(data.recent || []);
    morph(document.getElementById("system-status-body"), systemStatusHtml(data));
    updateBoardOverview(data);
    deviceStatus = [...(data.devices || [])];
    renderDevices();
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place.
  }
}

// --------------------------------------------------------------- season ----
// Heating or cooling for the whole plant. Seasonal - so it's here, not on
// the dashboard, and switching needs the new season's name typed in. Each
// card spells out what that season does on *this* installation (which
// zones run, what's held closed, what the mode switches do).

const SEASON_ICONS = {
  heat: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>`,
  cool: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20"/><path d="M3.34 7 20.66 17"/><path d="M20.66 7 3.34 17"/></svg>`,
};
let seasonPending = null;   // the season being confirmed, or null

function seasonEffects(mode, data) {
  const zones = data.zones || [];
  const effects = data.season_effects || {};
  const items = [];
  const running = mode === "cool" ? zones.filter(z => z.cool_enabled !== false) : zones;
  if (running.length) items.push(tr(`climate_admin.season_zones_${mode}`, { zones: running.map(z => escapeHtml(z.name)).join(", ") }));
  if (mode === "cool") {
    // An excluded zone whose valve a dehumidifier opens isn't "always closed".
    zones.filter(z => z.cool_enabled === false).forEach(z => {
      const link = (effects.links || []).find(l => (!l.mode || l.mode === mode) && (l.zone || l.follower) === z.name);
      items.push(link
        ? tr("climate_admin.season_zone_excluded_linked", { zone: escapeHtml(z.name), leader: escapeHtml(displayName(link.leader)) })
        : tr("climate_admin.season_zone_excluded", { zone: escapeHtml(z.name) }));
    });
  }
  // Every device the season decides on - what the home page doesn't say
  // (it shows only what's switched by hand): a valve's Shelly said as such.
  (effects.rules || []).forEach(rule => {
    const action = rule[mode];
    if (!action) return;
    const dehum = data.dehumidifier && data.dehumidifier.switch === rule.switch ? data.dehumidifier : null;
    const fromHome = dehum ? dehumidifierRuleText(dehum) : tr("climate_admin.season_rule_from_home");
    const what = ruleActionText(action, rule.normally_open) + (action === "manual" ? ` - ${fromHome}` : "");
    items.push(`${escapeHtml(displayName(rule.switch))}: ${escapeHtml(what)}`);
  });
  items.push(tr(`climate_admin.season_schedule_${mode}`));
  return items;
}

// -------------------------------------------------------- dehumidifier --
// Its thresholds and the ways it's switched on (services/dehumidifier.py),
// effective at once - no restart. The summer card says what they make of
// it, in one line.

function hoursLabel(h) {
  return h < 1 ? tr("climate_admin.dehum_minutes", { m: Math.round(h * 60) }) : tr("climate_admin.dehum_hours", { h });
}

function listWords(items) {
  if (items.length < 2) return items.join("");
  return `${items.slice(0, -1).join(", ")} ${tr("climate_admin.dehum_or")} ${items[items.length - 1]}`;
}

function dehumidifierRuleText(d) {
  const stop = Math.round(d.stop_below), start = Math.round(d.start_from);
  const ways = (d.durations || []).map(hoursLabel);
  if (d.until_target) ways.push(tr("climate_admin.dehum_way_target", { stop, max: d.target_max_hours }));
  const auto = d.auto || {};
  const byItself = auto.enabled
    ? tr("climate_admin.dehum_rule_auto", { start, window: dehumWindowText(auto.from_hour, auto.to_hour), max: auto.max_hours_day })
    : tr("climate_admin.dehum_rule_never_alone");
  return tr("climate_admin.dehum_rule", { ways: listWords(ways), alone: byItself, zone: displayName(d.humidity_zone || "?"), stop, start });
}

function dehumWindowText(from, to) {
  return from === 0 && to === 24 ? tr("climate_admin.dehum_all_day")
    : tr("climate_admin.dehum_window", { from: String(from).padStart(2, "0"), to: String(to).padStart(2, "0") });
}

async function loadDehumSettings() {
  const box = document.getElementById("dehum-settings");
  if (!box) return;
  let data;
  try {
    data = await (await fetch("/api/dehumidifier/settings")).json();
  } catch (e) {
    return;
  }
  box.hidden = !data.configured;
  if (!data.configured) return;
  const s = data.settings, r = data.ranges;
  const num = (key, extra = "") => `<input type="number" data-dehum="${key}" min="${r[key][0]}" max="${r[key][1]}" step="1" value="${s[key]}"${extra}>`;
  box.innerHTML = `
    <h3 class="admin-subheading">${escapeHtml(tr("climate_admin.dehum_heading"))}</h3>
    <p class="hint-text">${escapeHtml(tr("climate_admin.dehum_note"))}</p>
    <div class="admin-zone-card">
      <div class="dehum-grid">
        <div class="admin-field">
          <label>${escapeHtml(tr("climate_admin.setting_stop_below"))}</label>
          <span class="dehum-pct">${num("stop_below")}%</span>
          <span class="hint-text">${escapeHtml(tr("climate_admin.dehum_stop_help"))}</span>
        </div>
        <div class="admin-field">
          <label>${escapeHtml(tr("climate_admin.setting_start_from"))}</label>
          <span class="dehum-pct">${num("start_from")}%</span>
          <span class="hint-text">${escapeHtml(tr("climate_admin.dehum_start_help"))}</span>
        </div>
      </div>
      <div class="admin-field" style="margin-top:14px">
        <label>${escapeHtml(tr("climate_admin.dehum_durations"))}</label>
        <div class="dehum-choices">${data.duration_choices.map(h => `<label><input type="checkbox" data-dehum-duration="${h}"${s.durations.includes(h) ? " checked" : ""}> ${escapeHtml(hoursLabel(h))}</label>`).join("")}</div>
        <label class="dehum-check"><input type="checkbox" data-dehum="until_target"${s.until_target ? " checked" : ""}>
          ${escapeHtml(tr("climate_admin.dehum_until_target"))} ${num("target_max_hours")} ${escapeHtml(tr("climate_admin.dehum_hours_short"))}</label>
      </div>
      <div class="dehum-auto">
        <label class="dehum-check dehum-check-long"><input type="checkbox" data-dehum="auto"${s.auto ? " checked" : ""} onchange="syncDehumAuto()">
          <span>${escapeHtml(tr("climate_admin.dehum_auto"))}</span></label>
        <div class="dehum-auto-fields" id="dehum-auto-fields">
          ${escapeHtml(tr("climate_admin.dehum_auto_from"))} ${num("auto_from_hour")}
          ${escapeHtml(tr("climate_admin.dehum_auto_to"))} ${num("auto_to_hour")}
          ${escapeHtml(tr("climate_admin.dehum_auto_max"))} ${num("auto_max_hours_day")} ${escapeHtml(tr("climate_admin.dehum_auto_max_unit"))}
        </div>
        <p class="hint-text">${escapeHtml(tr("climate_admin.dehum_auto_help"))}</p>
      </div>
      <div class="toolbar admin-actions">
        <button class="tbtn open" type="button" onclick="saveDehumSettings(this)">${escapeHtml(tr("climate_admin.dehum_save"))}</button>
        <button class="tbtn" type="button" onclick="saveDehumSettings(this, true)">${escapeHtml(tr("climate_admin.dehum_reset"))}</button>
      </div>
      <p class="hint-text" id="dehum-settings-result"></p>
    </div>`;
  syncDehumAuto();
}

function syncDehumAuto() {
  const on = document.querySelector('#dehum-settings [data-dehum="auto"]');
  const fields = document.getElementById("dehum-auto-fields");
  if (!on || !fields) return;
  fields.setAttribute("aria-disabled", String(!on.checked));
  fields.querySelectorAll("input").forEach(i => { i.disabled = !on.checked; });
}

async function saveDehumSettings(button, reset = false) {
  const box = document.getElementById("dehum-settings");
  const result = document.getElementById("dehum-settings-result");
  const body = { durations: [...box.querySelectorAll("[data-dehum-duration]")].filter(i => i.checked).map(i => Number(i.dataset.dehumDuration)) };
  box.querySelectorAll("[data-dehum]").forEach(i => { body[i.dataset.dehum] = i.type === "checkbox" ? i.checked : Number(i.value); });
  button.disabled = true;
  try {
    const res = await fetch("/api/dehumidifier/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reset ? { reset: true } : body) });
    const data = await res.json();
    if (!data.ok) {
      result.textContent = tr("sensors.error_prefix") + apiError(data);
      result.className = "hint-text admin-error";
      return;
    }
    await loadDehumSettings();
    const after = document.getElementById("dehum-settings-result");
    after.textContent = tr(reset ? "climate_admin.dehum_reset_done" : "climate_admin.dehum_saved");
    after.className = "hint-text admin-success";
    refreshStatus();
  } catch (e) {
    result.textContent = tr("sensors.request_failed");
    result.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

function renderSeason(data) {
  const el = document.getElementById("season-cards");
  if (!el || !data || !data.mode) return;
  morph(el, ["heat", "cool"].map(mode => {
    const active = data.mode === mode;
    const action = active
      ? `<span class="season-active-pill">${tr("climate_admin.season_active")}</span>`
      : `<button class="range-btn season-switch-btn" onclick="askSeason('${mode}')">${tr(`climate_admin.season_switch_to_${mode}`)}</button>`;
    return `
      <div class="season-card season-${mode}${active ? " active" : ""}">
        <div class="season-card-head">
          <span class="season-icon">${SEASON_ICONS[mode]}</span>
          <div>
            <div class="season-name">${tr(`climate.mode_${mode}`)}</div>
            <div class="season-sub">${tr(mode === "heat" ? "climate.season_winter" : "climate.season_summer")}</div>
          </div>
          ${action}
        </div>
        <ul class="season-effects">${seasonEffects(mode, data).map(i => `<li>${i}</li>`).join("")}</ul>
        ${active && data.mode_changed && data.mode_changed.at ? `<p class="season-changed">${escapeHtml(tr("climate_admin.season_changed", {
          when: formatWhen(data.mode_changed.at), by: data.mode_changed.by || "?" }))}</p>` : ""}
      </div>`;
  }).join(""));
}

// ---------------------------------------------------------- season rules --
// What each switch does in each season: "on"/"off" forced by LocalHome,
// "manual" left to the dashboard (services/interlock.py's SeasonRule). The
// panel's own two switches are edited here and apply at once; rules from
// config.yaml are shown as they are. Each row also says what the switch is
// doing now, and warns when that isn't what its rule says for longer than
// a control cycle takes to fix it.

const RULE_GRACE_MS = 90000;   // the interlock loop runs every minute
const ruleMismatchSince = {};
let seasonRuleRoles = [];       // from GET /api/climate-admin/season-rules

// A normally open valve's "on" is its Shelly on - the valve closed.
function ruleActionText(action, normallyOpen) {
  return tr(`climate.rule_action_${action}${normallyOpen ? "_no" : ""}`);
}

function ruleSelectHtml(role, mode, value, normallyOpen) {
  const options = ["off", "on", "manual"].map(a =>
    `<option value="${a}"${a === value ? " selected" : ""}>${escapeHtml(ruleActionText(a, normallyOpen))}</option>`).join("");
  return `<label class="rule-season">${escapeHtml(tr(mode === "heat" ? "climate.season_winter" : "climate.season_summer"))}
    <select data-role="${escapeAttr(role)}" data-mode="${mode}">${options}</select></label>`;
}

// A rule that isn't changed here, as text - filled from the live rule
// by refreshSeasonRules().
const RULE_FIXED_HTML = '<span class="hint-text rule-fixed"></span>';

function ruleFixedText(rule) {
  return `${tr("climate.season_winter")}: ${ruleActionText(rule.heat, rule.normally_open)}`
    + ` · ${tr("climate.season_summer")}: ${ruleActionText(rule.cool, rule.normally_open)}`;
}

function seasonRuleRowHtml(name, body, note) {
  return `
    <div class="season-rule" data-switch="${escapeAttr(name)}">
      <strong>${escapeHtml(displayName(name))}</strong>
      <div class="rule-seasons">${body}</div>
      <span class="rule-live"></span>
      ${note ? `<p class="hint-text rule-note">${escapeHtml(note)}</p>` : ""}
    </div>`;
}

async function loadSeasonRules() {
  let data;
  try {
    data = await (await fetch("/api/climate-admin/season-rules")).json();
  } catch (e) {
    return;
  }
  if (!data.ok) return;
  // A panel switch with no device id yet is a place to fill in - unless
  // config.yaml already has its own switches and rules, listed below:
  // then it would only show the same dehumidifier or bypass twice.
  const roles = data.roles.filter(r => r.wired || r.in_config || !data.config_rules);
  seasonRuleRoles = roles;
  const el = document.getElementById("season-rules");
  el.innerHTML = roles.map(r => seasonRuleRowHtml(r.switch,
    r.in_config || !data.thermostat ? RULE_FIXED_HTML
      : ruleSelectHtml(r.role, "heat", r.heat, r.normally_open) + ruleSelectHtml(r.role, "cool", r.cool, r.normally_open),
    r.in_config ? tr("climate_admin.rule_in_config") : !r.wired ? tr("climate_admin.rule_not_wired") : "")).join("");
  document.getElementById("season-rules-actions").hidden = !data.thermostat || roles.every(r => r.in_config);
}

async function saveSeasonRules(button) {
  const result = document.getElementById("season-rules-result");
  const rules = {};
  document.querySelectorAll("#season-rules select").forEach(select => {
    rules[select.dataset.role] = rules[select.dataset.role] || {};
    rules[select.dataset.role][select.dataset.mode] = select.value;
  });
  button.disabled = true;
  try {
    const data = await (await fetch("/api/climate-admin/season-rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules }),
    })).json();
    result.textContent = data.ok ? tr("climate_admin.rules_saved") : tr("sensors.error_prefix") + apiError(data);
    result.className = `hint-text ${data.ok ? "admin-success" : "admin-error"}`;
    if (data.ok) refreshStatus();
  } catch (e) {
    result.textContent = tr("sensors.request_failed");
    result.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

async function refreshSeasonRules(data) {
  const el = document.getElementById("season-rules");
  if (!el) return;
  const rules = (data.season_effects || {}).rules || [];
  let worst = "ok";
  for (const rule of rules) {
    let row = el.querySelector(`.season-rule[data-switch="${CSS.escape(rule.switch)}"]`);
    if (!row) {
      // A rule config.yaml gives a switch the panel doesn't manage.
      el.insertAdjacentHTML("beforeend", seasonRuleRowHtml(rule.switch, RULE_FIXED_HTML, tr("climate_admin.rule_in_config")));
      row = el.lastElementChild;
    }
    const fixed = row.querySelector(".rule-fixed");
    if (fixed) fixed.textContent = ruleFixedText(rule);
    let reading = {};
    try {
      reading = await (await fetch(`/api/sensors/${encodeURIComponent(rule.switch)}`)).json();
    } catch (e) {
      reading = {};
    }
    const action = rule[data.mode];
    const want = action === "manual" ? null : action === "on";
    let state;
    let text;
    if (data.diagnostic_mode) {
      state = "idle";
      text = tr("climate_admin.rule_diag");
    } else if (!reading.ok) {
      state = "bad";
      text = tr("climate_admin.rule_offline");
    } else if (want === null || !!reading.is_on === want) {
      delete ruleMismatchSince[rule.switch];
      state = "ok";
      text = want === null ? tr("climate_admin.rule_free") : tr("climate_admin.rule_ok");
    } else {
      ruleMismatchSince[rule.switch] = ruleMismatchSince[rule.switch] || Date.now();
      const late = Date.now() - ruleMismatchSince[rule.switch] > RULE_GRACE_MS;
      state = late ? "bad" : "neutral";
      text = late ? tr(rule.normally_open ? (want ? "climate_admin.rule_should_be_closed" : "climate_admin.rule_should_be_open")
        : (want ? "climate_admin.rule_should_be_on" : "climate_admin.rule_should_be_off"))
        : tr("climate_admin.rule_waiting");
    }
    if (state === "bad" || (state === "neutral" && worst === "ok")) worst = state;
    const nowKey = rule.normally_open ? (reading.is_on ? "rule_now_closed" : "rule_now_open")
      : (reading.is_on ? "rule_now_on" : "rule_now_off");
    const now = reading.ok ? `${tr(`climate_admin.${nowKey}`)} ` : "";
    morph(row.querySelector(".rule-live"), `${escapeHtml(now)}${pillHtml(state, text)}`);
  }
  // What one switch opens with it - the valve a dehumidifier draws its
  // water through - and whether it holds right now: the valve on with its
  // leader, off with it, or, past a control cycle, said not to be.
  const links = (data.season_effects || {}).links || [];
  const linksEl = document.getElementById("season-links");
  if (linksEl) {
    linksEl.hidden = !links.length;
    const rows = [];
    for (const link of links) {
      const zone = link.zone || displayName(link.follower);
      let live;
      if (data.diagnostic_mode) {
        live = pillHtml("idle", tr("climate_admin.rule_diag"));
      } else if (link.mode && link.mode !== data.mode) {
        live = pillHtml("idle", tr("climate_admin.link_off_season"));
      } else {
        let leader = {};
        let valve = {};
        try {
          [leader, valve] = await Promise.all([link.leader, link.follower].map(name =>
            fetch(`/api/sensors/${encodeURIComponent(name)}`).then(r => r.json())));
        } catch (e) { /* said below */ }
        const key = `link:${link.leader}`;
        if (!leader.ok || !valve.ok) {
          live = pillHtml("bad", tr("climate_admin.rule_offline"));
          worst = "bad";
        } else {
          const want = !!leader.is_on;
          const now = tr("climate_admin.link_now", { leader: displayName(link.leader),
            leader_state: tr(want ? "home.on" : "home.off_state"), valve_state: tr(valve.is_on ? "home.valve_open" : "home.valve_closed") });
          if (!!valve.is_on === want) {
            delete ruleMismatchSince[key];
            live = `${escapeHtml(now)} ${pillHtml("ok", tr("climate_admin.rule_ok"))}`;
          } else {
            ruleMismatchSince[key] = ruleMismatchSince[key] || Date.now();
            const late = Date.now() - ruleMismatchSince[key] > RULE_GRACE_MS;
            if (late) worst = "bad";
            else if (worst === "ok") worst = "neutral";
            live = `${escapeHtml(now)} ${pillHtml(late ? "bad" : "neutral", late
              ? tr(want ? "climate_admin.rule_should_be_open" : "climate_admin.rule_should_be_closed") : tr("climate_admin.rule_waiting"))}`;
          }
        }
      }
      rows.push(`<div class="season-link"><span>${escapeHtml(tr("climate_admin.link_row", { leader: displayName(link.leader), zone }))}</span>
        <span class="admin-pill">${escapeHtml(tr(`climate_admin.link_when_${link.mode || "always"}`))}</span>
        <span class="rule-live">${live}</span></div>`);
    }
    morph(linksEl, links.length ? `<h3 class="admin-subheading">${escapeHtml(tr("climate_admin.links_heading"))}</h3>${rows.join("")}` : "");
  }
  // A switch not doing what its rule says outranks the header's count.
  setPill("season-rules-summary", worst !== "ok" ? pillHtml(worst, tr("climate_admin.pill_rule_check")) : "");
}

function askSeason(mode) {
  seasonPending = mode;
  const word = tr(`climate_admin.season_word_${mode}`);
  document.getElementById("season-confirm-text").textContent = tr("climate_admin.season_confirm", { word });
  const input = document.getElementById("season-confirm-input");
  input.value = "";
  input.placeholder = word;
  document.getElementById("season-confirm-go").disabled = true;
  document.getElementById("season-confirm").hidden = false;
  document.getElementById("season-result").textContent = "";
  input.focus();
}

function onSeasonConfirmInput(input) {
  const word = seasonPending ? tr(`climate_admin.season_word_${seasonPending}`) : null;
  document.getElementById("season-confirm-go").disabled = input.value.trim().toUpperCase() !== word;
}

function cancelSeason() {
  seasonPending = null;
  document.getElementById("season-confirm").hidden = true;
}

async function confirmSeason(button) {
  const mode = seasonPending;
  const result = document.getElementById("season-result");
  button.disabled = true;
  try {
    const res = await fetch("/api/climate/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await res.json();
    if (!data.ok) {
      result.textContent = tr("sensors.error_prefix") + apiError(data);
      result.className = "hint-text admin-error";
      return;
    }
    cancelSeason();
    result.textContent = tr("climate_admin.season_done", { season: tr(`climate.mode_${mode}`) });
    result.className = "hint-text admin-success";
    refreshStatus();
  } catch (e) {
    result.textContent = tr("sensors.request_failed");
    result.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

// ------------------------------------------------------------- settings ----
// Thermostat tuning, history limits, language - see services/panel_settings.py.
// Each field shows where its current value comes from (set here,
// config.yaml, or the built-in default); a blank field falls back.

// Shown in the Plant group, next to the zones; the rest under Board ->
// Settings. One form all the same: saveSettings() sends every field.
const PLANT_SETTING_KEYS = new Set(["hysteresis_c", "economy_tolerance_c", "thermostat_poll_seconds", "sensor_failsafe_hours"]);

async function loadSettings() {
  try {
    const res = await fetch("/api/climate-admin/settings");
    const data = await res.json();
    if (!data.ok) return;
    historyStats = data.history || null;
    settingsDisk = data.disk || null;
    settingsLoaded = Object.fromEntries(data.settings.map(s => [s.key, s.source === "panel" ? s.value : null]));
    settingsValues = Object.fromEntries(data.settings.map(s => [s.key, s.value]));
    const plant = data.settings.filter(s => PLANT_SETTING_KEYS.has(s.key));
    document.getElementById("settings-form").innerHTML = data.settings.filter(s => !PLANT_SETTING_KEYS.has(s.key))
      .map(settingFieldHtml).join("");
    const plantForm = document.getElementById("plant-settings-form");
    if (plantForm) plantForm.innerHTML = plant.map(settingFieldHtml).join("");
    settingsRestartPending = !!data.restart_pending;
    const pill = keys => {
      const setHere = keys.filter(key => settingsLoaded[key] != null).length;
      return settingsRestartPending ? pillHtml("neutral", tr("climate_admin.pill_restart"))
        : setHere ? pillHtml("idle", tr("climate_admin.pill_settings_custom", { count: setHere })) : "";
    };
    setPill("settings-status", pill(Object.keys(settingsLoaded).filter(key => !PLANT_SETTING_KEYS.has(key))));
    setPill("plant-settings-status", pill(plant.map(s => s.key)));
    updateRestartBar();
  } catch (e) {
    // Keep the form as it was.
  }
}

let historyStats = null; // from the last GET /api/climate-admin/settings
let settingsDisk = null;  // the disk the history is on, from the same
let settingsLoaded = {};  // key -> the value set here when the form was loaded (null = not set here)
let settingsValues = {};  // key -> the value in force, wherever it comes from

// "300 MB" means nothing without knowing what's in there now. The history
// holds measurements only - one row per sensor field per cycle - while
// schedules, settings and the zone mapping live in their own files, which
// is the first thing people assume is filling it up.
// Where the history's maximum comes from: the disk it's on, kept well
// inside (panel_settings.history_ceiling_mb).
function historyDiskHtml(s) {
  const d = settingsDisk;
  if (!d || !d.total_bytes) return "";
  const gb = bytes => (bytes / 1e9).toLocaleString(LOCALE, { maximumFractionDigits: 1 });
  let text = tr("climate_admin.setting_history_disk", { max: s.max, total: gb(d.total_bytes), free: gb(d.free_bytes) });
  if (s.capped_to) text += " " + tr("climate_admin.setting_history_capped", { value: s.value, max: s.capped_to });
  return `<span class="hint-text setting-usage">${escapeHtml(text)}</span>`;
}

function historyUsageHtml(maxMb) {
  const h = historyStats;
  if (!h || !h.rows) return `<span class="hint-text setting-usage">${escapeHtml(tr("climate_admin.setting_history_empty"))}</span>`;
  const usedMb = (h.used_bytes || h.file_bytes || 0) / 1e6;
  const percent = maxMb ? Math.round((usedMb / maxMb) * 100) : 0;
  const parts = [tr("climate_admin.setting_history_now", {
    used: formatMb(usedMb), max: maxMb, percent,
    rows: formatCount(h.rows), sensors: h.sensors || 0,
    since: h.oldest_ts ? formatDay(h.oldest_ts) : "-",
  })];
  const top = (h.by_field || []).slice(0, 3).map(f =>
    `${fieldName(f.field)} ${Math.round((f.rows / h.rows) * 100)}%`).join(" · ");
  parts.push(tr("climate_admin.setting_history_contents", { fields: top }));
  parts.push(h.size_prune_ts
    ? tr("climate_admin.setting_history_cap_hit", { when: formatDay(h.size_prune_ts) })
    : tr("climate_admin.setting_history_cap_never"));
  if (h.disk_hold_bytes) {
    parts.push(tr("climate_admin.setting_history_disk_hold", {
      size: formatMb(h.disk_hold_bytes / 1e6), since: formatDay(h.disk_hold_since) }));
  }
  return `<span class="hint-text setting-usage">${escapeHtml(parts.join(" "))}</span>`;
}

function fieldName(field) {
  const name = tr(`fields.${field}`);
  return name === `fields.${field}` ? field : name;
}

function formatMb(mb) {
  return mb >= 100 ? Math.round(mb).toLocaleString(LOCALE) : mb.toLocaleString(LOCALE, { maximumFractionDigits: 1 });
}

function formatCount(rows) {
  if (rows >= 1e6) return (rows / 1e6).toLocaleString(LOCALE, { maximumFractionDigits: 1 }) + "M";
  if (rows >= 1e4) return Math.round(rows / 1e3).toLocaleString(LOCALE) + "k";
  return rows.toLocaleString(LOCALE);
}

function formatDay(ts) {
  return new Date(ts * 1000).toLocaleDateString(LOCALE, { day: "numeric", month: "short", year: "numeric" });
}

function settingFieldHtml(s) {
  const label = tr(`climate_admin.setting_${s.key}`);
  const help = tr(`climate_admin.setting_${s.key}_help`);
  const source = tr(`climate_admin.settings_source_${s.source}`);
  const value = s.source === "panel" ? s.value : "";
  const placeholder = s.source === "config" ? s.config_value : s.default;
  if (s.type === "text") return textSettingHtml(s, label, help, source, value, placeholder);
  const input = s.type === "choice"
    ? `<select data-setting="${s.key}"><option value="">${escapeHtml(tr("climate_admin.settings_inherit", { value: placeholder }))}</option>`
      + s.choices.map(c => `<option value="${c}"${value === c ? " selected" : ""}>${c}</option>`).join("") + `</select>`
    : `<input type="number" data-setting="${s.key}" min="${s.min}" max="${s.max}" step="${s.step}" value="${value}" placeholder="${placeholder}">`;
  return `
    <div class="admin-field">
      <label>${escapeHtml(label)} <span class="setting-range">${s.type === "choice" ? "" : `${s.min}–${s.max}`}</span></label>
      ${input}
      <span class="hint-text setting-help">${escapeHtml(help)} <em>${escapeHtml(tr("climate_admin.settings_now", { value: s.value, source }))}</em></span>
      ${s.key === "sensor_failsafe_hours" ? failsafeExampleHtml(Number(s.value)) : ""}
      ${s.key === "economy_tolerance_c" ? toleranceExampleHtml() : ""}
      ${s.key === "history_max_db_mb" ? historyDiskHtml(s) + historyUsageHtml(Number(s.capped_to || s.value)) : ""}
    </div>`;
}

// What the failsafe's hours mean in practice: an H&T reads as unreachable
// after 4 hours without a report (its stale_after_seconds - see
// climate_map_store.py), and the hours count from there.
const SENSOR_UNREACHABLE_HOURS = 4;

// What the hysteresis and the economy tolerance make of the four targets
// where the two levels meet - as the thermostat works it out (services/
// thermostat.py's thresholds()): stops at the target, opens again that
// far away.
function toleranceExampleHtml() {
  const h = Number(settingsValues.hysteresis_c), e = Number(settingsValues.economy_tolerance_c);
  if (!Number.isFinite(h) || !Number.isFinite(e)) return "";
  const show = n => n.toLocaleString(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  return `<span class="hint-text setting-help">${escapeHtml(tr("climate_admin.setting_tolerance_example", {
    c_heat: show(20.5 - h), e_heat: show(19.5 - h - e), c_cool: show(25.5 + h), e_cool: show(26.5 + h + e) }))}</span>`;
}

function failsafeExampleHtml(hours) {
  if (!Number.isFinite(hours)) return "";
  const show = n => n.toLocaleString(LOCALE, { maximumFractionDigits: 1 });
  return `<span class="hint-text setting-help">${escapeHtml(tr("climate_admin.setting_sensor_failsafe_example",
    { hours: show(hours), total: show(hours + SENSOR_UNREACHABLE_HOURS) }))}</span>`;
}

// A name is the one setting someone can get wrong in a way the box
// itself can show: the count is there while typing, and the browser stops
// at the limit (the server checks again anyway - see panel_settings.py).
function textSettingHtml(s, label, help, source, value, placeholder) {
  const used = String(value).length;
  return `
    <div class="admin-field">
      <label>${escapeHtml(label)} <span class="setting-range" id="count-${s.key}">${used}/${s.max_length}</span></label>
      <input type="text" data-setting="${s.key}" maxlength="${s.max_length}"
             value="${escapeHtml(String(value))}" placeholder="${escapeHtml(String(placeholder))}"
             oninput="countSetting(this)">
      <span class="hint-text setting-help">${escapeHtml(help)} <em>${escapeHtml(tr("climate_admin.settings_now", { value: s.value, source }))}</em></span>
    </div>`;
}

function countSetting(input) {
  const counter = document.getElementById(`count-${input.dataset.setting}`);
  if (!counter) return;
  const max = Number(input.maxLength);
  counter.textContent = `${input.value.length}/${max}`;
  counter.classList.toggle("at-limit", input.value.length >= max);
}

async function postSettings(values, button, result = document.getElementById("settings-result")) {
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const data = await res.json();
    if (!data.ok) {
      result.textContent = tr("sensors.error_prefix") + apiError(data);
      result.className = "hint-text admin-error";
      return;
    }
    // Only some settings need a restart; the page name is live on the
    // next reload, and asking for a restart it doesn't need would teach
    // people to restart for nothing.
    // Said about what actually changed: the page name is live on the
    // next reload, the rest needs a restart, and nothing changed is just
    // "saved".
    const nameChanged = (values.site_name || null) !== (settingsLoaded.site_name || null);
    result.innerHTML = data.restart_required
      ? `${tr("climate_admin.save_success")} ${restartButtonHtml()}`
      : nameChanged ? tr("climate_admin.settings_saved_live") : tr("climate_admin.settings_saved");
    result.className = "hint-text admin-success";
    loadSettings();
  } catch (e) {
    result.textContent = tr("sensors.request_failed");
    result.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

// Checked here first, in the page's language and on the field's own
// label: the server checks again, but its answer names the setting's
// internal key in English. And a half-typed number ("-", "1e") reads as
// empty in the browser - which here means "back to the default" - so it
// has to be caught, not saved as a silent reset.
function settingsInputError(el) {
  const label = el.closest(".admin-field").querySelector("label").firstChild.textContent.trim();
  if (el.type === "number") {
    if (el.validity.badInput) return tr("climate_admin.settings_not_a_number", { label });
    if (el.value === "") return null;
    const value = Number(el.value);
    const min = Number(el.min), max = Number(el.max);
    if (value < min || value > max) {
      return tr("climate_admin.settings_out_of_range", {
        label, min: min.toLocaleString(LOCALE), max: max.toLocaleString(LOCALE),
      });
    }
  }
  return null;
}

function saveSettings(button) {
  const values = {};
  // The answer next to the button pressed; every field sent, wherever shown.
  const result = button.closest("details").querySelector(".settings-result");
  let firstError = null;
  document.querySelectorAll("#settings-form [data-setting], #plant-settings-form [data-setting]").forEach(el => {
    const error = settingsInputError(el);
    el.classList.toggle("input-invalid", !!error);
    if (error && !firstError) firstError = { el, error };
    values[el.dataset.setting] = el.value === "" ? null : el.value;
  });
  if (firstError) {
    result.textContent = firstError.error;
    result.className = "hint-text admin-error";
    firstError.el.focus();
    return;
  }
  postSettings(values, button, result);
}

async function resetSettings(button) {
  if (!await confirmAction(tr("climate_admin.settings_defaults_confirm"))) return;
  postSettings({}, button);
}

// --------------------------------------------------------- maintenance ----
// Clear the history, clear every schedule, reset to a fresh installation,
// download a backup. Each destructive action needs "RESET" typed in and
// saves a backup on the board first - see services/maintenance.py.

const MAINTENANCE_ACTIONS = ["clear-history", "clear-schedules", "factory-reset"];

async function loadMaintenance() {
  try {
    const res = await fetch("/api/climate-admin/maintenance");
    const data = await res.json();
    if (!data.ok) return;
    const el = document.getElementById("maintenance-backups");
    const backups = data.backups || [];
    if (!backups.length) {
      el.textContent = tr("climate_admin.maint_backups_none");
    } else {
      const latest = new Date(backups[0].mtime * 1000).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" });
      el.textContent = tr("climate_admin.maint_backups_some", { count: backups.length, latest });
    }
    const schedules = document.getElementById("maint-row-clear-schedules");
    if (schedules) schedules.hidden = !data.has_thermostat;
  } catch (e) {
    // Leave the previous text.
  }
}

function askMaintenance(action) {
  MAINTENANCE_ACTIONS.forEach(a => {
    const box = document.getElementById(`maint-confirm-${a}`);
    if (box) box.hidden = a !== action;
  });
  const input = document.getElementById(`maint-input-${action}`);
  input.value = "";
  document.getElementById(`maint-go-${action}`).disabled = true;
  input.focus();
}

function onMaintenanceInput(action, input) {
  document.getElementById(`maint-go-${action}`).disabled = input.value.trim() !== "RESET";
}

function cancelMaintenance(action) {
  document.getElementById(`maint-confirm-${action}`).hidden = true;
}

async function runMaintenance(action, button) {
  const result = document.getElementById("maintenance-result");
  button.disabled = true;
  let data;
  try {
    const res = await fetch(`/api/climate-admin/maintenance/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "RESET" }),
    });
    data = await res.json();
  } catch (e) {
    result.textContent = tr("sensors.request_failed");
    result.className = "hint-text admin-error";
    button.disabled = false;
    return;
  }
  cancelMaintenance(action);
  if (!data.ok) {
    result.textContent = tr("sensors.error_prefix") + apiError(data);
    result.className = "hint-text admin-error";
    return;
  }
  const backup = data.backup || data.history_backup || tr("climate_admin.maint_no_backup");
  result.className = "hint-text admin-success";
  if (action === "factory-reset") {
    result.textContent = data.restarting
      ? tr("climate_admin.maint_reset_done_restarting", { backup })
      : tr("climate_admin.maint_reset_done_manual", { backup });
    if (data.restarting) await waitForServerAndReload(result);
    return;
  }
  result.textContent = tr(`climate_admin.maint_done_${action.replace("-", "_")}`, {
    backup, rows: data.rows_removed != null ? data.rows_removed.toLocaleString(LOCALE) : "",
  });
  loadMaintenance();
  if (action === "clear-schedules") refreshStatus();
}

// ----------------------------------------------------------- LED matrix ----
// The UNO Q's 13x8 LEDs - see services/led_matrix.py. The mirror polls
// every 350 ms, and only while the section is open: nobody is watching
// it otherwise. Not every second: the heartbeat changes every half
// second, and a 1 s poll would always catch it in the same phase.

const LED_POLL_MS = 350;

let ledTimer = null;
let ledLegendDrawn = false;

function ledGridHtml(frame, size) {
  return `<div class="led-grid ${size}" aria-hidden="true">${
    frame.map(row => row.map(level => `<i style="--l:${level}"></i>`).join("")).join("")}</div>`;
}

function ledCodeMeaning(code) {
  const zone = code.match(/^([SV])([12])$/);
  if (zone) return tr(`climate_admin.led_code_${zone[1]}`, { zone: zone[2] });
  return tr(`climate_admin.led_code_${code}`);
}

// In words, what the matrix is showing right now.
function ledNowText(screen, state) {
  if (!screen) return "";
  if (screen.screen === "name" || (screen.screen === "boot" && screen.showing === "name")) {
    return tr("climate_admin.led_now_name", { name: screen.name });
  }
  if (screen.screen === "boot") return tr("climate_admin.led_now_boot", { ip: screen.ip });
  if (screen.screen === "problem") {
    return tr("climate_admin.led_now_problem", { code: screen.code, meaning: ledCodeMeaning(screen.code), subject: screen.subject });
  }
  if (!state || !state.thermostat || !(state.zones || []).length) return tr("climate_admin.led_now_idle");
  const what = state.diagnostic ? tr("climate_admin.led_legend_diagnostic_title")
    : state.away ? tr("climate_admin.led_legend_away_title")
    : tr(`climate_admin.led_legend_${state.mode === "cool" ? "cool" : "heat"}_title`);
  const zones = state.zones.slice(0, 2).map((open, i) => tr(
    open === true ? "climate_admin.led_now_zone_open" : open === false ? "climate_admin.led_now_zone_closed" : "climate_admin.led_now_zone_unknown",
    { zone: (state.zone_names || [])[i] || String(i + 1) }));
  return [what, ...zones].join(" · ");
}

async function refreshLedMatrix() {
  let data;
  try {
    data = await (await fetch(`/api/climate-admin/led-matrix${ledLegendDrawn ? "" : "?legend=1"}`)).json();
  } catch (e) {
    return;
  }
  if (!data.ok) return;
  const running = data.status === "running";
  const ledState = running ? "ok" : data.status === "no_router" || data.status === "off" ? "idle" : "bad";
  const ledShort = tr(`climate_admin.led_status_short_${data.status}`);
  morph(document.getElementById("led-matrix-summary"), pillHtml(ledState, ledShort));
  setTile("ov-led", ledState, ledShort.charAt(0).toUpperCase() + ledShort.slice(1),
    running && data.screen ? ledNowText(data.screen, data.state) : "");
  const status = document.getElementById("led-matrix-status");
  status.textContent = tr(`climate_admin.led_status_${data.status}`, { error: data.error || "" });
  // Red only for what someone should fix: a board with no matrix at all
  // (a PC, a Raspberry Pi) isn't a fault.
  status.className = `hint-text ${data.status === "no_sketch" || data.status === "error" ? "admin-error" : ""}`;
  if (data.screen) {
    morph(document.getElementById("led-matrix-mirror"), ledGridHtml(data.screen.frame, "large"));
    document.getElementById("led-matrix-now").textContent = ledNowText(data.screen, data.state);
  }
  if (!ledLegendDrawn && data.legend) {
    ledLegendDrawn = true;
    morph(document.getElementById("led-matrix-legend"), data.legend.map(entry => `
      <div class="led-legend-item">
        ${ledGridHtml(entry.frame, "small")}
        <div>
          <strong>${escapeHtml(tr(`climate_admin.led_legend_${entry.key}_title`))}</strong>
          <span class="hint-text">${escapeHtml(tr(`climate_admin.led_legend_${entry.key}`))}</span>
        </div>
      </div>`).join(""));
    morph(document.getElementById("led-matrix-codes"), data.codes.map(code => `
      <div class="led-code"><code>${escapeHtml(code)}</code><span>${escapeHtml(ledCodeMeaning(code))}</span></div>`).join(""));
  }
}

// Polled while the system status, where it's shown, is open.
function watchLedMatrix() {
  const section = document.getElementById("system-status");
  if (!section) return;
  const sync = () => {
    if (ledTimer) clearInterval(ledTimer);
    ledTimer = null;
    if (section.open && !document.hidden) {
      refreshLedMatrix();
      ledTimer = setInterval(refreshLedMatrix, LED_POLL_MS);
    }
  };
  section.addEventListener("toggle", sync);
  document.addEventListener("visibilitychange", sync);
  refreshLedMatrix();   // the header's status, even while it's collapsed
}

// ----------------------------------------------------------- connection ----
// Two
// failed checks in a row and 15 s without an answer mean the board isn't
// answering - and everything on this page is then the last known state,
// which an installer watching it must not take for live. Checked on the
// cheapest request there is, every 5 s.

const adminConnection = { lastOk: Date.now(), failures: 0 };

async function checkConnection() {
  try {
    const res = await fetch("/api/server-time", { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    adminConnection.lastOk = Date.now();
    adminConnection.failures = 0;
  } catch (e) {
    adminConnection.failures += 1;
  }
  const banner = document.getElementById("connection-banner");
  if (!banner) return;
  const offline = adminConnection.failures >= 2 && Date.now() - adminConnection.lastOk > 15000;
  banner.hidden = !offline;
  document.body.classList.toggle("is-offline", offline);
  if (offline) {
    banner.textContent = tr("app.offline", {
      time: new Date(adminConnection.lastOk).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }),
    });
  }
}

setInterval(checkConnection, 5000);

// -------------------------------------------------------------- polling ----
// Fast enough to watch live during install verification, but paused
// while the tab isn't focused so a session left open all day doesn't
// add needless load.

function startPolling() {
  stopPolling();
  pollTimer = setInterval(refreshAll, 3500);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
  } else {
    systemStatusFetchedAt = 0; // whatever it showed is stale by now
    refreshAll();
    startPolling();
  }
});

// ----------------------------------------------------------- navigation ----
// Which sections are open survives a reload in the same tab - above all
// the one the restart button does - so whoever just saved the VMC and
// restarted lands back on the VMC's status, not on a folded page.

const OPEN_SECTIONS_KEY = "localhome-admin-open";

function readOpenSections() {
  try {
    const value = JSON.parse(sessionStorage.getItem(OPEN_SECTIONS_KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (e) {
    return {};
  }
}

const openSectionsSaved = readOpenSections();

function rememberOpenSections() {
  document.querySelectorAll("details.admin-details[id]").forEach(section => {
    if (typeof openSectionsSaved[section.id] === "boolean") section.open = openSectionsSaved[section.id];
    section.addEventListener("toggle", () => {
      const now = readOpenSections();
      now[section.id] = section.open;
      try {
        sessionStorage.setItem(OPEN_SECTIONS_KEY, JSON.stringify(now));
      } catch (e) {
        // Private mode or storage off: sections just start as designed.
      }
    });
  });
}

// The link of the section being read is highlighted - the last one
// whose top has passed just under the bar, or at the very bottom of the
// page the last one on screen - and scrolled into view in the bar on a
// phone. Once per frame at most, a dozen positions each time.
function watchNav() {
  const nav = document.getElementById("admin-nav");
  if (!nav) return;
  const links = new Map([...nav.querySelectorAll("a")].map(a => [a.getAttribute("href").slice(1), a]));
  const targets = [...document.querySelectorAll("[data-nav]")];
  let current = null;
  let queued = false;
  const update = () => {
    queued = false;
    const line = nav.getBoundingClientRect().bottom + 40;
    const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
    let pick = targets[0];
    for (const el of targets) {
      const top = el.getBoundingClientRect().top;
      if (atBottom ? top < window.innerHeight : top <= line) pick = el;
    }
    if (!pick || pick.dataset.nav === current) return;
    current = pick.dataset.nav;
    links.forEach((a, id) => a.classList.toggle("current", id === current));
    const link = links.get(current);
    if (link && nav.scrollWidth > nav.clientWidth) {
      nav.scrollTo({ left: link.offsetLeft - (nav.clientWidth - link.offsetWidth) / 2, behavior: "smooth" });
    }
  };
  const queue = () => {
    if (!queued) {
      queued = true;
      requestAnimationFrame(update);
    }
  };
  window.addEventListener("scroll", queue, { passive: true });
  window.addEventListener("resize", queue);
  document.addEventListener("toggle", queue, true);   // a section opening moves the rest
  queue();
}

// A link to a closed section - the bar's, a tile, the diagnostic
// banner's - opens it. (A repeated click on the same link fires no
// hashchange, hence this on top of openLinkedSection().)
document.addEventListener("click", event => {
  const link = event.target.closest && event.target.closest('a[href^="#"]');
  if (!link) return;
  const target = document.getElementById(link.getAttribute("href").slice(1));
  if (target && target.tagName === "DETAILS") target.open = true;
});

// ---------------------------------------------------------- Shelly health --
// What each Shelly says about itself (services/shelly_health.py): its
// Wi-Fi signal now and over the last day and week; for a battery sensor,
// its battery and whether its reports come on time; for a mains one, its
// own temperature, since when it's been on, and firmware updates.

const SHELLY_HEALTH_EVERY_MS = 60000;

// The usual Wi-Fi bands. Information, never a fault on its own: a weak
// signal that gets every report through is fine - lost reports are what
// turn red.
function signalQuality(dbm) {
  if (dbm >= -65) return "excellent";
  if (dbm >= -75) return "good";
  if (dbm >= -85) return "weak";
  return "critical";
}

function signalText(dbm) {
  return tr("climate_admin.shelly_signal_value", { value: Math.round(dbm), quality: tr(`climate_admin.shelly_quality_${signalQuality(dbm)}`) });
}

function signalRow(label, stats) {
  if (!stats) return "";
  return statRowHtml(label, tr("climate_admin.shelly_signal_stats", { avg: stats.avg, min: stats.min }));
}

// ------------------------------------------------------ check at insertion --
// The Shelly whose id was just typed: there or not, and the settings its
// role needs that it has otherwise - asked at once over MQTT, before any
// save or restart, nothing changed on it (services/shelly_check.py).

// Pressing Save is what takes the focus from an id field, and so starts
// its check: the line written under the field then moved the button from
// under the pointer between press and release, and the save was lost -
// typing an id and saving straight away is the usual way. The line waits
// for the release (a second at most, should it never come).
let pointerPressed = false;
document.addEventListener("pointerdown", () => { pointerPressed = true; }, true);
document.addEventListener("pointerup", () => { pointerPressed = false; }, true);
document.addEventListener("pointercancel", () => { pointerPressed = false; }, true);

function afterRelease() {
  if (!pointerPressed) return Promise.resolve();
  return new Promise(resolve => {
    const done = () => setTimeout(resolve);   // after the click it ends in
    document.addEventListener("pointerup", done, { once: true, capture: true });
    setTimeout(resolve, 1000);
  });
}

async function checkShelly(input, role) {
  const out = input.parentElement.querySelector(".shelly-check");
  if (!out) return;
  const id = input.value.trim();
  await afterRelease();
  out.className = "hint-text shelly-check";
  if (!id) {
    out.textContent = "";
    return;
  }
  out.textContent = tr("climate_admin.check_running");
  let data;
  try {
    data = await (await fetch(`/api/climate-admin/shelly-check?id=${encodeURIComponent(id)}&role=${role}`)).json();
  } catch (e) {
    out.textContent = tr("sensors.request_failed");
    return;
  }
  await afterRelease();
  if (input.value.trim() !== id) return;   // typed again meanwhile
  const wrong = data.settings_wrong || [];
  const good = data.answered && !wrong.length && !data.settings_error;
  out.classList.add(good ? "admin-success" : data.battery ? "shelly-check-info" : "admin-error");
  out.innerHTML = `${escapeHtml(shellyCheckText(data))} <a href="#" class="shelly-check-again"
    onclick="checkShelly(this.closest('.admin-field').querySelector('input'), '${role}'); return false;">${escapeHtml(tr("climate_admin.check_again"))}</a>`;
}

function shellyCheckText(data) {
  if (!data.ok) return tr(data.reason === "id" ? "climate_admin.check_bad_id" : "sensors.request_failed");
  if (data.battery) return tr("climate_admin.check_battery");
  if (!data.answered) {
    if (data.reason === "no_broker") return tr("climate_admin.check_no_broker");
    return tr(data.standard_id === false ? "climate_admin.check_silent_prefix" : "climate_admin.check_silent");
  }
  const found = tr("climate_admin.check_found", { model: data.model || "Shelly", firmware: data.firmware || "?" });
  const wrong = data.settings_wrong || [];
  if (wrong.length) {
    return `${found} ${tr("climate_admin.check_fix", { settings: wrong.map(s => `${tr(`climate_admin.shelly_setting_${s.key}`)}: `
      + tr("climate_admin.shelly_setting_change", { now: shellySettingValue(s.key, s.value), want: shellySettingValue(s.key, s.expected) })).join("; ") })}`;
  }
  return `${found} ${tr(data.settings_error ? "climate_admin.check_settings_unread" : "climate_admin.check_settings_ok")}`;
}

// A setting a valve's Shelly needs otherwise - in the Shelly app's own
// words, so it's clear what to change there (shelly_health.settings_wrong).
function shellySettingValue(key, value) {
  if (key === "auto_off" || key === "auto_on") {
    // As the Shelly app writes it: 00:15:00, not 900 s.
    const hms = [Math.floor(value / 3600), Math.floor(value / 60) % 60, value % 60].map(n => String(n).padStart(2, "0")).join(":");
    return value ? tr("climate_admin.shelly_timer_on", { time: hms }) : tr("climate_admin.shelly_value_off");
  }
  if (typeof value === "boolean") return tr(value ? "climate_admin.shelly_value_on" : "climate_admin.shelly_value_off");
  const label = tr(`climate_admin.shelly_${key}_${value}`);
  return label === `climate_admin.shelly_${key}_${value}` ? String(value) : label;
}

// The server's verdict on a Shelly (services/shelly_health.py assess()):
// each reason to act on, and each note - what happened that isn't a
// fault - in a sentence.
// A wording with a count: its "_one" form for one, where it has one.
function trCount(key, count, vars = {}) {
  if (count === 1) {
    const one = tr(`${key}_one`, { ...vars, count });
    if (!one.startsWith("climate_admin.")) return one;
  }
  return tr(key, { ...vars, count });
}

function reasonText(reason) {
  const key = `climate_admin.device_why_${reason.code}`;
  if (reason.code === "crashes" || reason.code === "power_unstable") return trCount(key, reason.detail, { detail: reason.detail });
  if (reason.code === "report_late" && reason.early) {
    return tr(`${key}_period`, { detail: formatAge(reason.detail), period: formatAge(reason.period) });
  }
  return tr(key, { detail: reason.code === "report_late" ? formatAge(reason.detail) : reason.detail });
}

function noteText(note) {
  return trCount(`climate_admin.device_note_${note.code}`, note.count,
    { avg: note.avg, when: note.at ? formatWhen(note.at) : "" });
}

// A row's label: its first reason; how many more, after it.
function reasonLabel(reasons) {
  const first = tr(`climate_admin.device_reason_${reasons[0].code}`);
  return reasons.length > 1 ? tr("climate_admin.device_reason_more", { label: first, count: reasons.length - 1 }) : first;
}

function verdictHtml(verdict) {
  if (!verdict) return "";
  return verdict.reasons.map(r => `<p class="device-why">${escapeHtml(reasonText(r))}</p>`).join("")
    + verdict.notes.map(n => `<p class="device-note">${escapeHtml(noteText(n))}</p>`).join("");
}

function shellyStatsHtml(d, data, now) {
  const rows = (d.settings_wrong || []).map(s => statRowHtml(tr(`climate_admin.shelly_setting_${s.key}`),
    tr("climate_admin.shelly_setting_change", { now: shellySettingValue(s.key, s.value), want: shellySettingValue(s.key, s.expected) }),
    true));
  const s = d.stats || {};
  const reasons = (d.assessment && d.assessment.reasons) || [];
  if (d.battery) {
    const age = d.last_report_at == null ? null : now - d.last_report_at;
    // Late as the server judges it: in 4 hours on its batteries, in 18
    // minutes on USB-C (a report every 6).
    rows.push(statRowHtml(tr("climate_admin.shelly_last_report"),
      age == null ? tr("climate_admin.shelly_never") : tr("climate_admin.shelly_ago", { age: formatAge(Math.max(0, age)) }),
      reasons.some(r => r.code === "report_late")));
  } else {
    const state = d.error ? "not_answering" : d.online === false ? "offline" : (d.online || d.polled_at) ? "online" : null;
    if (state) rows.push(statRowHtml(tr("climate_admin.shelly_link"), tr(`climate_admin.shelly_${state}`), state !== "online"));
  }
  if (d.wifi_rssi_dbm != null) {
    rows.push(statRowHtml(tr("climate_admin.shelly_signal_now"), signalText(d.wifi_rssi_dbm)));
  }
  rows.push(signalRow(tr("climate_admin.shelly_signal_24h"), s.signal_24h));
  rows.push(signalRow(tr("climate_admin.shelly_signal_7d"), s.signal_7d));
  if (d.battery) {
    // On USB-C its battery isn't what keeps it going: not shown - its
    // power source is.
    if (d.external_power) rows.push(statRowHtml(tr("climate_admin.shelly_power"), tr("climate_admin.shelly_usb")));
    if (d.battery_pct != null && !d.external_power) {
      rows.push(statRowHtml(tr("fields.battery_pct"), d.battery_v != null
        ? tr("climate_admin.shelly_battery_value", { pct: Math.round(d.battery_pct), volts: d.battery_v.toFixed(2) })
        : `${Math.round(d.battery_pct)}%`, d.battery_pct <= LOW_BATTERY_PCT));
    }
    if (s.reports_24h != null && !s.counted_since) {
      // Nothing counted yet: the count starts at the sensor's next report.
      rows.push(statRowHtml(tr("climate_admin.shelly_reports_24h"), tr("climate_admin.shelly_reports_waiting")));
    } else if (s.reports_24h != null) {
      rows.push(statRowHtml(tr("climate_admin.shelly_reports_24h"), d.external_power
        ? (d.wakeup_period
          ? tr("climate_admin.shelly_reports_usb", { count: s.reports_24h, period: formatAge(d.wakeup_period) })
          : String(s.reports_24h))
        : s.heartbeats_24h != null
          ? tr("climate_admin.shelly_reports_split", { count: s.reports_24h, beats: s.heartbeats_24h, changes: s.changes_24h, missed: s.missed_24h })
          : tr("climate_admin.shelly_reports_value", { count: s.reports_24h, missed: s.missed_24h }),
        reasons.some(r => r.code === "reports_missed")));
      // When to expect it - on its batteries the heartbeat keeps its own
      // 2-hour clock (on USB-C, no promise to keep: not said).
      if (s.next_heartbeat_at && !d.external_power) {
        rows.push(statRowHtml(tr("climate_admin.shelly_next_heartbeat"),
          tr("climate_admin.shelly_next_heartbeat_value", {
            time: new Date(s.next_heartbeat_at * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }),
          })));
      }
      // A sensor added (or first watched) this week has less than a week behind it.
      const partial = s.counted_since && now - s.counted_since < 6.5 * 86400;
      if (s.missed_7d != null) rows.push(statRowHtml(tr("climate_admin.shelly_missed_7d"), partial
        ? tr("climate_admin.shelly_missed_since", { missed: s.missed_7d, since: formatWhen(s.counted_since) })
        : String(s.missed_7d)));
    }
  } else {
    if (d.device_temp_c != null) {
      rows.push(statRowHtml(tr("climate_admin.shelly_temp"), `${d.device_temp_c.toFixed(1)}°C`, d.device_temp_c >= data.hot_c));
    }
    if (d.voltage_v != null) rows.push(statRowHtml(tr("climate_admin.shelly_voltage"), `${Math.round(d.voltage_v)} V`));
    if (d.booted_at != null) rows.push(statRowHtml(tr("climate_admin.shelly_on_since"), sinceText(d.booted_at, now - d.booted_at)));
    // By why it started - only a crash or a brownout is the device's
    // trouble; a power cut, a restart asked for, are said, not in red.
    if (s.restarts_7d != null) {
      const parts = [["power", s.restarts_power_7d], ["crash", s.restarts_crash_7d], ["brownout", s.restarts_brownout_7d],
        ["unknown", s.restarts_unknown_7d], ["wanted", s.restarts_wanted_7d]]
        .filter(([, count]) => count).map(([kind, count]) => trCount(`climate_admin.shelly_restarts_${kind}`, count));
      rows.push(statRowHtml(tr("climate_admin.shelly_restarts_7d"),
        parts.length ? parts.join(" · ") : tr("climate_admin.shelly_restarts_none"),
        (s.restarts_crash_7d || 0) + (s.restarts_brownout_7d || 0) > 0));
    }
    // Its relay's own counters - a Gen3 keeps them, a Plus 1 doesn't.
    if (s.switch_ons_24h != null) {
      rows.push(statRowHtml(tr("climate_admin.shelly_switch_ons_24h"), String(Math.round(s.switch_ons_24h)),
        s.switch_ons_24h > data.switch_ons_24h_max));
    }
    if (s.on_seconds_today != null) {
      rows.push(statRowHtml(tr("climate_admin.shelly_on_today"), s.on_seconds_today >= 60
        ? formatDuration(s.on_seconds_today) : tr("climate_admin.shelly_on_today_none")));
    }
    if (d.errors.length) rows.push(statRowHtml(tr("climate_admin.shelly_errors"), d.errors.join(", "), true));
  }
  const firmware = [d.model, d.firmware].filter(Boolean).join(" · ");
  const update = d.update ? tr("climate_admin.shelly_update", { version: d.update }) : (d.firmware ? tr("climate_admin.shelly_up_to_date") : "");
  if (firmware || update) rows.push(statRowHtml(tr("climate_admin.shelly_firmware"), [firmware, update].filter(Boolean).join(" - ")));
  if (d.restart_required) rows.push(statRowHtml(tr("climate_admin.shelly_firmware"), tr("climate_admin.shelly_restart_required"), true));
  const error = d.error ? `<div class="admin-problem-detail">${escapeHtml(d.error)}</div>` : "";
  // A simulated H&T is watched like a real one, but has no device id.
  return `${verdictHtml(d.assessment)}${rows.join("")}${error}${d.simulated ? "" : `<p class="hint-text admin-path">${escapeHtml(d.id)}</p>`}`;
}

async function loadShellyHealth() {
  try {
    const data = await (await fetch("/api/climate-admin/shelly-health", { cache: "no-store" })).json();
    if (data.ok) shellyHealth = data;
  } catch (e) {
    return;   // offline: the connection banner says so already
  }
  renderDevices();
}

// Led by the Shellys: what they say (a setting changed in the app, a
// restart, back online) moves a mark kept in memory, asked with every
// refresh; the whole list - its day and week read from the history - is
// read again only then, and every minute for what time alone changes.
let shellyRevision = null;

async function refreshShellyRevision() {
  let revision;
  try {
    revision = (await (await fetch("/api/climate-admin/shelly-health/revision", { cache: "no-store" })).json()).revision;
  } catch (e) {
    return;
  }
  if (shellyRevision !== null && revision !== shellyRevision) loadShellyHealth();
  shellyRevision = revision;
}

// ------------------------------------------------------------- devices ----
// Every device LocalHome reads or drives, in one list: what the system
// status knows of each one (answering, how fresh) joined with what each
// Shelly says about itself (services/shelly_health.py). A Shelly is one
// row whatever it drives - a VMC dimmer's switch and speed are one
// device. Not answering first, then those with something to do, then
// the rest - each Shelly as the server judges it (its "assessment"), the
// same verdict the system's problems come from; a Shelly's row opens to
// its details, and stays open across refreshes.

let deviceStatus = null;   // devices from the last GET /api/system-status
let shellyHealth = null;   // the last GET /api/climate-admin/shelly-health
let deviceZones = null;    // each zone's sensor and valve, from the last GET /api/climate/zones
let setupTodo = [];        // the Shellys with no id yet, from the same
const openDeviceRows = new Set();
const DEVICE_STATE_ORDER = { bad: 0, neutral: 1, ok: 2, todo: 3 };
// What each Shelly to configure is - the model to look for in the app.
const ROLE_MODELS = {
  sensor: "Shelly H&T Gen3", outdoor: "Shelly H&T Gen3",
  valve: "Shelly 1 Gen3", bypass: "Shelly 1 Gen3", dehumidifier: "Shelly 1 Gen3",
  vmc: "Shelly Dimmer 0/1-10V Gen3",
};

// A device as the list names it: the fixed roles in the page's language,
// a zone's own as "Sensore Stanze" rather than "Stanze Sensor".
function deviceLabel(name) {
  if (DISPLAY_NAMES[name]) return displayName(name);
  const zone = (deviceZones || []).find(z => name === `${z.name} Sensor` || name === `${z.name} Valve`);
  if (!zone) return name;
  return tr(name === `${zone.name} Sensor` ? "climate_admin.zone_sensor_name" : "climate_admin.zone_valve_name", { zone: zone.name });
}

function deviceRows() {
  const shellys = (shellyHealth && shellyHealth.devices) || [];
  const shellyOf = new Map();
  shellys.forEach(s => (s.names && s.names.length ? s.names : [s.name]).forEach(name => shellyOf.set(name, s)));
  const rows = new Map();
  for (const d of deviceStatus || []) {
    const shelly = shellyOf.get(d.name);
    const key = shelly ? `shelly:${shelly.id}` : `device:${d.name}`;
    if (!rows.has(key)) rows.set(key, { key, shelly, devices: [] });
    rows.get(key).devices.push(d);
  }
  // A Shelly whose driver didn't load still says something about itself.
  shellys.forEach(s => {
    if (!rows.has(`shelly:${s.id}`)) rows.set(`shelly:${s.id}`, { key: `shelly:${s.id}`, shelly: s, devices: [] });
  });
  // And one with no id yet, a row to do.
  setupTodo.forEach(t => rows.set(`todo:${t.name}`, { key: `todo:${t.name}`, todo: t, devices: [] }));
  const now = Date.now() / 1000;
  return [...rows.values()].map(row => {
    if (row.todo) return { ...row, stats: "", state: "todo", names: [row.todo.name] };
    const stats = row.shelly ? shellyStatsHtml(row.shelly, shellyHealth, now) : "";
    const verdict = row.shelly && row.shelly.assessment;
    const state = row.devices.some(d => !d.ok) || (verdict && verdict.state === "offline") ? "bad"
      : verdict && verdict.state === "attention" && verdict.reasons.length ? "neutral" : "ok";
    const names = row.devices.length ? row.devices.map(d => d.name) : (row.shelly.names || [row.shelly.name]);
    return { ...row, stats, state, names };
  }).sort((a, b) => DEVICE_STATE_ORDER[a.state] - DEVICE_STATE_ORDER[b.state]
    || deviceLabel(a.names[0]).localeCompare(deviceLabel(b.names[0])));
}

function deviceRowHtml(row) {
  if (row.todo) {
    return `<li><div class="admin-device-line">${todoHtml()}<strong>${escapeHtml(deviceLabel(row.todo.name))}</strong>`
      + `<span class="admin-pill">${escapeHtml(ROLE_MODELS[row.todo.role] || "")}</span></div></li>`;
  }
  const s = row.shelly;
  const label = row.state === "neutral" ? reasonLabel(s.assessment.reasons)
    : tr(`climate_admin.${row.state === "bad" ? "system_device_offline" : "system_device_ok"}`);
  const pills = s
    ? `<span class="admin-pill">${tr(!s.battery ? "climate_admin.shelly_mains"
      : s.external_power ? "climate_admin.shelly_usb" : "climate_admin.shelly_battery")}</span>`
      + (s.simulated ? `<span class="admin-pill simulated">${tr("climate_admin.simulated_pill")}</span>` : "")
    : `<span class="admin-pill">${escapeHtml(row.devices[0].kind)}</span>`;
  const meta = s ? [s.wifi_rssi_dbm != null ? `${Math.round(s.wifi_rssi_dbm)} dBm` : "",
    s.battery && s.battery_pct != null && !s.external_power ? `${Math.round(s.battery_pct)}%` : ""].filter(Boolean).join(" · ") : "";
  const ages = row.devices.map(d => d.seconds_since_update).filter(age => age != null);
  const age = !row.devices.length ? ""
    : ages.length ? tr("climate_admin.system_device_age", { age: formatAge(Math.min(...ages)) })
    : tr("climate_admin.system_device_never");
  const errors = row.devices.filter(d => d.error).map(d =>
    `<div class="admin-problem-detail">${escapeHtml(row.devices.length > 1 ? `${displayName(d.name)}: ${d.error}` : d.error)}</div>`).join("");
  const line = `<span class="admin-status ${row.state}">${escapeHtml(label)}</span>`
    + `<strong>${row.names.map(name => escapeHtml(deviceLabel(name))).join(" · ")}</strong>${pills}`
    + (meta ? `<span class="device-meta">${escapeHtml(meta)}</span>` : "")
    + `<span class="admin-device-age">${escapeHtml(age)}</span>`;
  if (!s) return `<li><div class="admin-device-line">${line}</div>${errors}</li>`;
  return `<li><details class="device-row" data-key="${escapeAttr(row.key)}"${openDeviceRows.has(row.key) ? " open" : ""}>`
    + `<summary class="admin-device-line">${line}</summary>${errors}${row.stats}</details></li>`;
}

// What belongs to each zone: its sensor and valve; a switch linked to
// its valve (the dehumidifier drawing its water through it); and the
// plant's season switches - the bathrooms' bypass and dehumidifier - with
// the one zone kept out of cooling, the bathrooms.
function devicesByZone(data) {
  const effects = data.season_effects || {};
  const zones = data.zones || [];
  const excluded = zones.filter(z => z.cool_enabled === false);
  return zones.map(z => {
    const members = [z.sensor, z.valve];
    (effects.links || []).filter(l => l.follower === z.valve || l.zone === z.name).forEach(l => members.push(l.leader));
    if (excluded.length === 1 && excluded[0] === z) (effects.rules || []).forEach(r => members.push(r.switch));
    // Those still to configure that go with it (the bypass, the dehumidifier).
    (data.to_configure || []).filter(t => t.zone === z.name).forEach(t => members.push(t.name));
    return { name: z.name, members: [...new Set(members.filter(Boolean))] };
  });
}

// A zone's devices belong together: a group each, in that order whatever
// their state; every other device after them, trouble first.
function deviceGroups(rows) {
  const placed = new Set();
  const groups = [];
  for (const zone of deviceZones || []) {
    const members = zone.members.map(name => rows.find(r => !placed.has(r) && r.names.includes(name))).filter(Boolean);
    members.forEach(r => placed.add(r));
    if (members.length) groups.push({ title: tr("climate_admin.devices_zone_group", { zone: zone.name }), rows: members });
  }
  const rest = rows.filter(r => !placed.has(r));
  if (rest.length) groups.push({ title: groups.length ? tr("climate_admin.devices_other_group") : "", rows: rest });
  return groups;
}

function renderDevices() {
  const list = document.getElementById("device-list");
  if (!list || !deviceStatus) return;
  const rows = deviceRows();
  morph(list, rows.length
    ? deviceGroups(rows).map(g => (g.title ? `<li class="device-group">${escapeHtml(g.title)}</li>` : "")
      + g.rows.map(deviceRowHtml).join("")).join("")
    : `<li class="hint-text">${escapeHtml(tr("climate_admin.devices_none"))}</li>`);
  const bad = rows.filter(r => r.state === "bad").length;
  const check = rows.filter(r => r.state === "neutral").length;
  const todo = rows.filter(r => r.state === "todo").length;
  const real = rows.length - todo;
  deviceCounts = { bad, check, todo, real };
  updateDevicesTile();
  setPill("devices-summary", !rows.length ? ""
    : bad ? pillHtml("bad", tr("climate_admin.devices_bad", { count: bad, total: real }))
    : check ? pillHtml("neutral", tr("climate_admin.devices_check", { count: check, total: real }))
    : todo ? pillHtml("todo", tr("climate_admin.devices_todo", { count: todo }))
    : pillHtml("ok", tr("climate_admin.devices_ok", { count: rows.length })));
}

// The devices at a glance: the broker only when it's the trouble - every
// Shelly speaks through it - else what the list says.
let brokerView = null;
let deviceCounts = null;

function updateDevicesTile() {
  if (brokerView && brokerView.configured && !brokerView.reachable) {
    setTile("ov-devices", "bad", tr("climate_admin.ov_broker_down"), brokerView.where);
    return;
  }
  if (!deviceCounts) return;
  const { bad, check, todo, real } = deviceCounts;
  const todoSub = todo ? tr("climate_admin.ov_devices_todo", { count: todo }) : "";
  if (bad) setTile("ov-devices", "bad", tr("climate_admin.devices_bad", { count: bad, total: real }), todoSub);
  else if (check) setTile("ov-devices", "neutral", tr("climate_admin.devices_check", { count: check, total: real }), todoSub);
  else if (todo) setTile("ov-devices", "todo", tr("climate_admin.devices_todo", { count: todo }),
    real ? tr("climate_admin.ov_devices_answering", { count: real }) : "");
  else if (real) setTile("ov-devices", "ok", tr("climate_admin.ov_devices_all_ok"), tr("climate_admin.ov_devices_count", { count: real }));
  else setTile("ov-devices", "idle", tr("climate_admin.devices_none"), "");
}

function watchDeviceRows() {
  const list = document.getElementById("device-list");
  if (!list) return;
  // "toggle" doesn't bubble: caught on the way down instead.
  list.addEventListener("toggle", event => {
    const row = event.target;
    if (!row.classList || !row.classList.contains("device-row")) return;
    if (row.open) openDeviceRows.add(row.dataset.key);
    else openDeviceRows.delete(row.dataset.key);
  }, true);
}

rememberOpenSections();
watchNav();
loadMap();
loadBroker();
loadExtraSwitches();
loadSeasonRules();
loadDehumSettings();
loadExtraSensor();
loadVmc();
loadVmcSpeeds();
watchDeviceRows();
loadShellyHealth();
setInterval(loadShellyHealth, SHELLY_HEALTH_EVERY_MS);
loadMaintenance();
loadSettings();
watchLedMatrix();

// A link to a collapsed section (a bookmark, another page) opens it.
// A link to something inside one (the LED matrix's card) opens that one.
function openLinkedSection() {
  const target = location.hash && document.getElementById(location.hash.slice(1));
  const section = target && target.closest("details.admin-details");
  if (!section || section.open) return;
  section.open = true;
  if (section !== target) target.scrollIntoView();
}
window.addEventListener("hashchange", openLinkedSection);
openLinkedSection();
loadClimateSwitchNames();
refreshSystemStatus(true);
startPolling();
