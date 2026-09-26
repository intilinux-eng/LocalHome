"use strict";

// Same tiny translation helper as app.js, copied rather than shared -
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

// Same compact age formatter as app.js, copied rather than shared -
// see this file's own top comment.
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
let pollTimer = null;

// Same threshold as the main dashboard's app.js (LOW_BATTERY_PCT) -
// copied rather than shared, matching this file's own "separate
// bundle" convention (see the top of this file).
const LOW_BATTERY_PCT = 10;

// ------------------------------------------------------------ config form ----

function zoneFormHtml(zone, index) {
  // A zone that doesn't exist yet starts simulated: on a board with no
  // hardware installed, "Shelly (real device)" can't even be saved (an
  // id is required) and every status line would report a device nobody
  // ever installed. Picking the real thing is one dropdown away.
  const z = zone || { name: "", sensor_mode: "simulated", sensor_device_id: "", valve_mode: "simulated", valve_device_id: "", cool_enabled: true };
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
        <input type="text" class="zone-sensor-device-id" value="${escapeHtml(z.sensor_device_id || "")}" placeholder="shellyht-XXXXXXXXXXXX">
      </div>
      <p class="hint-text zone-manual-hint"${z.sensor_mode !== "manual" ? " hidden" : ""}>${tr("climate_admin.manual_mode_hint")}</p>
      <div class="admin-field">
        <label>${tr("climate_admin.valve_mode_label")}</label>
        <select class="zone-valve-mode" onchange="onValveModeChange(this)">${valveOptions}</select>
      </div>
      <div class="admin-field zone-valve-id-field"${(z.valve_mode || "shelly") !== "shelly" ? " hidden" : ""}>
        <label>${tr("climate_admin.valve_device_id_label")}</label>
        <input type="text" class="zone-valve-device-id" value="${escapeHtml(z.valve_device_id || "")}" placeholder="shellyplus1-XXXXXXXXXXXX">
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

// ------------------------------------------------------------ broker form ----
// Editing config/mqtt_broker.json from the web - see
// services/mqtt_broker_config.py. Loaded once (not on every poll tick,
// unlike refreshBroker()'s reachability pill below) so it never
// overwrites an in-progress edit out from under the operator.

async function loadBroker() {
  const res = await fetch("/api/climate-admin/broker");
  const data = await res.json();
  document.getElementById("broker-host").value = data.host || "";
  document.getElementById("broker-port").value = data.port || "";
  document.getElementById("broker-username").value = data.username || "";
  document.getElementById("broker-change-password").checked = false;
  document.getElementById("broker-password").value = "";
  document.getElementById("broker-password-field").hidden = true;
  document.getElementById("broker-password-toggle-label").textContent =
    data.has_password ? tr("climate_admin.broker_change_password_label") : tr("climate_admin.broker_set_password_label");
}

function onChangePasswordToggle(checkbox) {
  document.getElementById("broker-password-field").hidden = !checkbox.checked;
}

async function testBroker(button) {
  const resultEl = document.getElementById("broker-test-result");
  const host = document.getElementById("broker-host").value.trim();
  const port = parseInt(document.getElementById("broker-port").value, 10);
  resultEl.textContent = "";
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/broker/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, port }),
    });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = tr("sensors.error_prefix") + data.error;
      resultEl.className = "hint-text admin-error";
      return;
    }
    resultEl.textContent = data.reachable ? tr("climate_admin.broker_ok") : `${tr("climate_admin.broker_down")} (${data.error})`;
    resultEl.className = data.reachable ? "hint-text admin-success" : "hint-text admin-error";
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
}

async function saveBroker(button) {
  const resultEl = document.getElementById("broker-save-result");
  const changePassword = document.getElementById("broker-change-password").checked;
  const body = {
    host: document.getElementById("broker-host").value.trim(),
    port: parseInt(document.getElementById("broker-port").value, 10),
    username: document.getElementById("broker-username").value.trim(),
    change_password: changePassword,
  };
  if (changePassword) body.password = document.getElementById("broker-password").value;
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/broker", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = tr("sensors.error_prefix") + data.error;
      resultEl.className = "hint-text admin-error";
      return;
    }
    resultEl.innerHTML = `${tr("climate_admin.broker_save_success")} ${restartButtonHtml()}`;
    resultEl.className = "hint-text admin-success";
    await loadBroker();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  } finally {
    button.disabled = false;
  }
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
      resultEl.textContent = tr("sensors.error_prefix") + data.error;
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
  extraSwitchesRestartPending = !!data.restart_pending;
}

async function saveExtraSwitches(button) {
  const resultEl = document.getElementById("save-extra-result");
  const body = {
    bypass_valve_device_id: document.getElementById("extra-bypass-valve-id").value.trim() || null,
    dehumidifier_device_id: document.getElementById("extra-dehumidifier-id").value.trim() || null,
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
      resultEl.textContent = tr("sensors.error_prefix") + data.error;
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
      resultEl.textContent = tr("sensors.error_prefix") + data.error;
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
  if (mode === "none" || (mode === "shelly" && !document.getElementById("extra-outdoor-sensor-id").value.trim())) {
    slot.innerHTML = "";
    return;
  }
  if (extraSensorRestartPending) {
    slot.innerHTML = `<p class="admin-restart-pending-inline">${tr("climate_admin.restart_pending")} ${restartButtonHtml()}</p>`;
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
    slot.innerHTML = `<div>${tr("climate_admin.sensor_label")}: ${sensorStatusHtml(zoneShaped, mode)}</div>`;
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
      resultEl.textContent = tr("sensors.error_prefix") + data.error;
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

async function refreshVmcStatus() {
  const slot = document.getElementById("vmc-status");
  if (!slot) return;
  const vmcMode = document.getElementById("extra-vmc-mode").value;
  if (vmcMode === "none" || (vmcMode === "shelly"
      && (!document.getElementById("extra-vmc-id").value.trim() || !document.getElementById("vmc-suggestion").hidden))) {
    slot.innerHTML = "";
    return;
  }
  if (vmcRestartPending) {
    slot.innerHTML = `<p class="admin-restart-pending-inline">${tr("climate_admin.restart_pending")} ${restartButtonHtml()}</p>`;
    return;
  }
  try {
    const [switchRes, speedRes] = await Promise.all([
      fetch(`/api/sensors/${encodeURIComponent(VMC_NAME)}`),
      fetch(`/api/sensors/${encodeURIComponent(VMC_SPEED_NAME)}`),
    ]);
    const sw = await switchRes.json();
    const speed = await speedRes.json();
    if (!sw.ok) {
      slot.innerHTML = `<div><span class="admin-status bad">${tr("climate_admin.vmc_offline")}</span> ${tr("climate_admin.vmc_offline_hint")}</div>`
        + (sw.error ? `<div class="admin-problem-detail">${escapeHtml(sw.error)}</div>` : "");
      return;
    }
    const state = sw.is_on ? tr("climate_admin.vmc_on") : tr("climate_admin.vmc_off");
    const speedText = speed.ok && typeof speed.value === "number"
      ? ` · ${tr("climate_admin.vmc_speed", { value: Math.round(speed.value) })}` : "";
    const badge = vmcMode === "simulated"
      ? `<span class="admin-pill simulated">${tr("climate_admin.simulated_pill")}</span>`
      : `<span class="admin-status ok">${tr("climate_admin.vmc_reachable")}</span>`;
    slot.innerHTML = `<div>${badge} ${state}${speedText}</div>`;
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place.
  }
}

// -------------------------------------------------------------- status ----
// Reuses /api/climate/zones as-is (see web/app.py) - joined client-side
// against the *saved* map by zone name, so a config edit not yet applied
// (restart pending) never gets confused with the live-running mapping.

// Same thresholds/labels as the main dashboard's app.js (wifiQuality/
// wifiLabel) - copied rather than shared, matching this file's own
// "separate bundle" convention (see the top of this file).
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
  if (seconds < NORMAL_AGE_CONTEXT_SECONDS) {
    return ` · ${tr("sensors.updated_ago", { time })}`;
  }
  return ` · ${tr("climate_admin.updated_ago_normal_cadence", { time })}`;
}

function sensorStatusHtml(zone, mode) {
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
    if (zone.battery_pct != null) {
      // Same 10% threshold as the main dashboard's own low-battery
      // popup (app.js) - highlighted here too so this page alone is
      // enough to notice it, without needing that dashboard-only,
      // session-scoped popup to still be around to have caught it.
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
      const note = staleValueNote(zone.sensor_last_value_age_seconds);
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
  const pill = valveMode === "simulated" ? `<span class="admin-pill simulated">${tr("climate_admin.simulated_pill")}</span> ` : "";
  const testButtons = `
    <button class="range-btn admin-valve-test" onclick="testValve(${jsArg(zone.valve)}, 'on', this)">${tr("climate_admin.valve_test_open")}</button>
    <button class="range-btn admin-valve-test" onclick="testValve(${jsArg(zone.valve)}, 'off', this)">${tr("climate_admin.valve_test_close")}</button>
  `;
  if (zone.valve_ok) {
    const state = zone.valve_on ? tr("climate_admin.valve_open") : tr("climate_admin.valve_closed");
    return `${pill}<span class="admin-status ok">${tr("climate_admin.reporting")}</span> ${state} ${testButtons}`;
  }
  return `${pill}<span class="admin-status bad">${tr("climate_admin.valve_not_reachable")}</span> ${testButtons}`;
}

function targetHtml(zone) {
  const target = zone.target_c != null ? `${fmt(zone.target_c, 1)}°C` : tr("climate.no_target");
  return `${tr("climate_admin.target_label")}: ${target}`;
}

// A cool_enabled:false zone (e.g. a condensation-prone bathroom) is
// still shown here in every mode - unlike the main dashboard, which
// just hides its card while cooling (see app.js) - because this page's
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
  const manualBoxHtml = sensorMode === "manual" ? `
    <div class="admin-manual-box">
      <input type="number" step="0.1" class="manual-temp-input" placeholder="${tr("climate_admin.manual_temp_label")}">
      <input type="number" step="0.1" class="manual-humidity-input" placeholder="${tr("climate_admin.manual_humidity_label")}">
      <button class="range-btn" onclick="setManualValue(${jsArg(zone.name)}, this)">${tr("climate_admin.manual_set_button")}</button>
    </div>` : "";

  return `
    <div>${tr("climate_admin.sensor_label")}: ${sensorStatusHtml(zone, sensorMode)}</div>
    ${failsafeNoticeHtml(zone)}
    <div>${targetHtml(zone)}</div>
    <div>${tr("climate_admin.valve_label")}: ${valveStatusHtml(zone, valveMode)}</div>
    <p class="hint-text admin-valve-test-hint">${tr("climate_admin.valve_test_hint")}</p>
    ${modeExclusionNoticeHtml(zone, mode)}
    ${manualBoxHtml}
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
let climateSwitchNames = [];

async function loadClimateSwitchNames() {
  try {
    const res = await fetch("/api/devices");
    const data = await res.json();
    climateSwitchNames = (data.sensors || [])
      .filter(s => s.kind === "switch" && s.tab === "climate")
      .map(s => s.name);
  } catch (e) {
    climateSwitchNames = [];
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
      notify(tr("sensors.error_prefix") + data.error);
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

function diagnosticValveRowHtml(name, isOn, isOk) {
  const state = !isOk
    ? `<span class="admin-status bad">${tr("climate_admin.valve_not_reachable")}</span>`
    : isOn
      ? `<span class="admin-status ok">${tr("climate_admin.valve_open")}</span>`
      : `<span class="admin-status neutral">${tr("climate_admin.valve_closed")}</span>`;
  return `
    <div class="admin-zone-card admin-diagnostic-row">
      <div><strong>${escapeHtml(name)}</strong></div>
      <div>${state}</div>
      <div class="toolbar">
        <button class="range-btn" onclick="diagnosticToggle(${jsArg(name)}, 'on', this)">${tr("climate_admin.valve_test_open")}</button>
        <button class="range-btn" onclick="diagnosticToggle(${jsArg(name)}, 'off', this)">${tr("climate_admin.valve_test_close")}</button>
      </div>
    </div>
  `;
}

// Same underlying route as testValve() (the generic on/off switch
// route every controllable device already has) - the only difference
// is what happens after: no thermostat/interlock tick is going to
// reassert anything while diagnostic mode is on, so this command is
// the valve's new, lasting state, not a transient test of it.
async function diagnosticToggle(name, action, button) {
  button.disabled = true;
  try {
    const res = await fetch(`/api/sensors/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) notify(tr("sensors.error_prefix") + data.error);
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
  diagnosticModeEnabled = !!data.diagnostic_mode;
  if (toggle) toggle.checked = diagnosticModeEnabled;
  if (banner) banner.hidden = !diagnosticModeEnabled;
  if (!container) return;
  if (!diagnosticModeEnabled) {
    container.innerHTML = "";
    return;
  }
  const rows = [];
  for (const name of climateSwitchNames) {
    try {
      const res = await fetch(`/api/sensors/${encodeURIComponent(name)}`);
      const reading = await res.json();
      rows.push({ name, is_on: reading.is_on, ok: reading.ok });
    } catch (e) {
      rows.push({ name, is_on: false, ok: false });
    }
  }
  container.innerHTML = `<div class="admin-diagnostic-grid">${rows.map(r => diagnosticValveRowHtml(r.name, r.is_on, r.ok)).join("")}</div>`;
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
    document.getElementById("mode-away-line").innerHTML = modeAwayLineHtml(data);
    renderSeason(data);
    await refreshDiagnosticSection(data);
    // A single all-or-nothing signal, not per-zone: a restart rebuilds
    // every zone's drivers together, so "one thing changed" means
    // every card's status is stale, not just the zone that changed -
    // see web/app.py's ACTIVE_CLIMATE_ZONES/ACTIVE_EXTRA_SWITCHES.
    const restartPending = (savedMap && savedMap.restart_pending) || extraSwitchesRestartPending;
    const byName = {};
    (data.zones || []).forEach(z => { byName[z.name] = z; });
    mapZones.forEach((mapEntry, index) => {
      const slot = document.getElementById(`zone-status-${index}`);
      if (!slot) return;
      if (restartPending) {
        slot.innerHTML = `<p class="admin-restart-pending-inline">${tr("climate_admin.restart_pending")} ${restartButtonHtml()}</p>`;
        return;
      }
      const zone = byName[mapEntry.name];
      slot.innerHTML = zone ? zoneStatusInnerHtml(zone, mapEntry, data.mode) : `<p class="hint-text">${tr("climate_admin.status_none")}</p>`;
    });
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place and
    // keep polling rather than blanking the page.
  }
}

// A manual test, not the thermostat's own control - reuses the generic
// switch route every controllable device already has (web/app.py's
// api_sensor_action()), so pressing it just proves the physical relay
// responds; the thermostat's own next tick re-asserts its schedule
// shortly after, same as it would for any manual toggle.
async function testValve(valveName, action, button) {
  button.disabled = true;
  try {
    const res = await fetch(`/api/sensors/${encodeURIComponent(valveName)}/${action}`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) notify(tr("sensors.error_prefix") + data.error);
    refreshStatus();
  } catch (e) {
    notify(tr("sensors.request_failed"));
  } finally {
    button.disabled = false;
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
    const where = `${data.host || "?"}:${data.port || "?"}`;
    if (data.reachable) {
      el.innerHTML = `<span class="admin-status ok">${tr("climate_admin.broker_ok")}</span> ${where}`;
    } else {
      el.innerHTML = `<span class="admin-status bad">${tr("climate_admin.broker_down")}</span> ${where} - ${tr("climate_admin.broker_down_hint")}`;
    }
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
}

async function setManualValue(zoneName, button) {
  const box = button.closest(".admin-manual-box");
  const temperature_c = parseFloat(box.querySelector(".manual-temp-input").value);
  if (Number.isNaN(temperature_c)) {
    notify(tr("climate_admin.manual_temp_required"));
    return;
  }
  const humidityRaw = box.querySelector(".manual-humidity-input").value;
  const body = { temperature_c };
  if (humidityRaw !== "") body.humidity_pct = parseFloat(humidityRaw);
  try {
    const res = await fetch(`/api/climate-admin/zones/${encodeURIComponent(zoneName)}/manual-value`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      notify(tr("sensors.error_prefix") + data.error);
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

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
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

function problemHtml(problem) {
  const title = tr(`climate_admin.system_problem_${problem.code}`, { subject: problem.subject });
  const detail = problem.detail ? `<div class="admin-problem-detail">${escapeHtml(problem.detail)}</div>` : "";
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
      statRowHtml(tr("climate_admin.system_host_ram"), freeOf(h.mem_available_bytes, h.mem_total_bytes)),
      statRowHtml(tr("climate_admin.system_cpu_temp"),
        typeof h.cpu_temp_c === "number" ? `${h.cpu_temp_c.toFixed(0)}°C` : "--", h.cpu_temp_c >= 80),
      statRowHtml(tr("climate_admin.system_load"), h.load_average ? `${h.load_average.join(" / ")} (${h.cpu_count} CPU)` : "--"),
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
        statRowHtml(tr("climate_admin.system_emmc_writes"),
          data.storage.written_per_day_bytes == null ? "--" : tr("climate_admin.system_emmc_per_day", { amount: formatBytes(data.storage.written_per_day_bytes) })),
      ] : []),
    ].join("")],
    [tr("climate_admin.system_group_network"), networkRows(data.network)],
    [tr("climate_admin.system_group_history"), [
      statRowHtml(tr("climate_admin.system_db_size"), formatBytes(db.file_bytes)),
      statRowHtml(tr("climate_admin.system_db_rows_24h"), count(db.rows_24h)),
      statRowHtml(tr("climate_admin.system_db_rows"), count(db.rows)),
      statRowHtml(tr("climate_admin.system_db_oldest"), tr("climate_admin.system_db_retention", { oldest, days: db.retention_days })),
    ].join("")],
  ];
  const statsHtml = groups.map(([title, rows]) =>
    `<div class="admin-zone-card admin-stat-group"><h3>${escapeHtml(title)}</h3>${rows}</div>`).join("");

  // Offline first: they're what someone opening this list is looking for.
  const devices = [...(data.devices || [])].sort((a, b) => (a.ok === b.ok ? a.name.localeCompare(b.name) : a.ok ? 1 : -1));
  const deviceRows = devices.map(d => {
    const status = d.ok
      ? `<span class="admin-status ok">${tr("climate_admin.system_device_ok")}</span>`
      : `<span class="admin-status bad">${tr("climate_admin.system_device_offline")}</span>`;
    const age = d.seconds_since_update == null
      ? tr("climate_admin.system_device_never")
      : tr("climate_admin.system_device_age", { age: formatAge(d.seconds_since_update) });
    const error = d.error ? `<div class="admin-problem-detail">${escapeHtml(d.error)}</div>` : "";
    return `<li><div class="admin-device-line">${status}<strong>${escapeHtml(d.name)}</strong>`
      + `<span class="admin-pill">${escapeHtml(d.kind)}</span><span class="admin-device-age">${escapeHtml(age)}</span></div>${error}</li>`;
  }).join("");

  const skipped = (data.skipped_driver_modules || []).map(s =>
    `<li><code>${escapeHtml(s.module)}</code> - ${escapeHtml(tr("climate_admin.system_skipped_line", { package: s.missing_package }))} <code>${escapeHtml(s.install)}</code></li>`).join("");
  const skippedHtml = skipped
    ? `<h3>${tr("climate_admin.system_skipped_heading")}</h3><p class="hint-text">${tr("climate_admin.system_skipped_note")}</p><ul class="admin-device-list">${skipped}</ul>`
    : "";

  return `
    <div class="admin-stat-grid">${statsHtml}</div>
    <h3>${tr("climate_admin.system_devices_heading", { count: devices.length })}</h3>
    <ul class="admin-device-list">${deviceRows}</ul>
    ${bootsHtml(data.boots)}
    ${skippedHtml}
    <p class="hint-text admin-path">${escapeHtml(tr("climate_admin.system_config_path", { path: data.config_path }))}</p>`;
}

function renderSystemProblems(problems) {
  const box = document.getElementById("system-problems");
  const summary = document.getElementById("system-status-summary");
  if (!problems.length) {
    box.hidden = true;
    summary.innerHTML = `<span class="admin-status ok">${tr("climate_admin.system_all_ok")}</span>`;
    return;
  }
  box.hidden = false;
  box.innerHTML = `
    <strong>${tr("climate_admin.system_problems_heading", { count: problems.length })}</strong>
    <ul>${problems.map(problemHtml).join("")}</ul>`;
  summary.innerHTML = `<span class="admin-status bad">${tr("climate_admin.system_problems_count", { count: problems.length })}</span>`;
}

async function refreshSystemStatus(force) {
  if (!force && Date.now() - systemStatusFetchedAt < SYSTEM_STATUS_INTERVAL_MS) return;
  systemStatusFetchedAt = Date.now();
  try {
    const res = await fetch("/api/system-status");
    const data = await res.json();
    if (!data.ok) return;
    renderSystemProblems(data.problems || []);
    document.getElementById("system-status-body").innerHTML = systemStatusHtml(data);
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
    zones.filter(z => z.cool_enabled === false).forEach(z => items.push(tr("climate_admin.season_zone_excluded", { zone: escapeHtml(z.name) })));
  }
  (effects.mode_switches || []).forEach(m => items.push(tr(
    m.active_in_mode === mode ? "climate_admin.season_switch_on" : "climate_admin.season_switch_off", { name: escapeHtml(m.switch) })));
  (effects.mode_limited || []).forEach(m => items.push(tr(
    m.only_in_mode === mode ? "climate_admin.season_limited_usable" : "climate_admin.season_limited_off", { name: escapeHtml(m.switch) })));
  items.push(tr(`climate_admin.season_schedule_${mode}`));
  return items;
}

function renderSeason(data) {
  const el = document.getElementById("season-cards");
  if (!el || !data || !data.mode) return;
  el.innerHTML = ["heat", "cool"].map(mode => {
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
      </div>`;
  }).join("");
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
      result.textContent = tr("sensors.error_prefix") + data.error;
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

async function loadSettings() {
  try {
    const res = await fetch("/api/climate-admin/settings");
    const data = await res.json();
    if (!data.ok) return;
    historyStats = data.history || null;
    settingsLoaded = Object.fromEntries(data.settings.map(s => [s.key, s.source === "panel" ? s.value : null]));
    document.getElementById("settings-form").innerHTML = data.settings.map(settingFieldHtml).join("");
    document.getElementById("settings-status").innerHTML = data.restart_pending
      ? `<span class="admin-status neutral">${tr("climate_admin.settings_restart_pending")}</span>` : "";
  } catch (e) {
    // Keep the form as it was.
  }
}

let historyStats = null; // from the last GET /api/climate-admin/settings
let settingsLoaded = {};  // key -> the value set here when the form was loaded (null = not set here)

// "300 MB" means nothing without knowing what's in there now. The history
// holds measurements only - one row per sensor field per cycle - while
// schedules, settings and the zone mapping live in their own files, which
// is the first thing people assume is filling it up.
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
      ${s.key === "history_max_db_mb" ? historyUsageHtml(Number(s.value)) : ""}
    </div>`;
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

async function postSettings(values, button) {
  const result = document.getElementById("settings-result");
  button.disabled = true;
  try {
    const res = await fetch("/api/climate-admin/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const data = await res.json();
    if (!data.ok) {
      result.textContent = tr("sensors.error_prefix") + data.error;
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
  const result = document.getElementById("settings-result");
  let firstError = null;
  document.querySelectorAll("#settings-form [data-setting]").forEach(el => {
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
  postSettings(values, button);
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
    result.textContent = tr("sensors.error_prefix") + data.error;
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
  document.getElementById("led-matrix-summary").innerHTML =
    `<span class="admin-status ${running ? "ok" : data.status === "no_router" || data.status === "off" ? "neutral" : "bad"}">${
      escapeHtml(tr(`climate_admin.led_status_short_${data.status}`))}</span>`;
  const status = document.getElementById("led-matrix-status");
  status.textContent = tr(`climate_admin.led_status_${data.status}`, { error: data.error || "" });
  // Red only for what someone should fix: a board with no matrix at all
  // (a PC, a Raspberry Pi) isn't a fault.
  status.className = `hint-text ${data.status === "no_sketch" || data.status === "error" ? "admin-error" : ""}`;
  if (data.screen) {
    document.getElementById("led-matrix-mirror").innerHTML = ledGridHtml(data.screen.frame, "large");
    document.getElementById("led-matrix-now").textContent = ledNowText(data.screen, data.state);
  }
  if (!ledLegendDrawn && data.legend) {
    ledLegendDrawn = true;
    document.getElementById("led-matrix-legend").innerHTML = data.legend.map(entry => `
      <div class="led-legend-item">
        ${ledGridHtml(entry.frame, "small")}
        <div>
          <strong>${escapeHtml(tr(`climate_admin.led_legend_${entry.key}_title`))}</strong>
          <span class="hint-text">${escapeHtml(tr(`climate_admin.led_legend_${entry.key}`))}</span>
        </div>
      </div>`).join("");
    document.getElementById("led-matrix-codes").innerHTML = data.codes.map(code => `
      <div class="led-code"><code>${escapeHtml(code)}</code><span>${escapeHtml(ledCodeMeaning(code))}</span></div>`).join("");
  }
}

function watchLedMatrix() {
  const section = document.getElementById("led-matrix");
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
// The same rule as the dashboard (app.js's updateConnectionBanner): two
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

loadMap();
loadBroker();
loadExtraSwitches();
loadExtraSensor();
loadVmc();
loadMaintenance();
loadSettings();
watchLedMatrix();

// A link to a collapsed section (the guide's, or a bookmark) opens it.
function openLinkedSection() {
  const target = location.hash && document.getElementById(location.hash.slice(1));
  if (target && target.tagName === "DETAILS") target.open = true;
}
window.addEventListener("hashchange", openLinkedSection);
openLinkedSection();
loadClimateSwitchNames();
refreshSystemStatus(true);
startPolling();
