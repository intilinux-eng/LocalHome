"use strict";

// Same tiny translation helper as app.js, copied rather than shared -
// this page is a separate, occasional-use bundle on purpose (see
// docs/configuration.md's climate_admin section).
function tr(path, vars) {
  const value = path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), window.I18N);
  let str = value != null ? value : path;
  if (vars) for (const k in vars) str = str.replace(`{${k}}`, vars[k]);
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
  if (!confirm(tr("climate_admin.restart_confirm"))) return;
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
        <input type="text" class="zone-name" value="${z.name || ""}" placeholder="${tr("climate_admin.name_placeholder")}">
      </div>
      <div class="admin-field">
        <label>${tr("climate_admin.sensor_mode_label")}</label>
        <select class="zone-sensor-mode" onchange="onSensorModeChange(this)">${sensorOptions}</select>
      </div>
      <div class="admin-field zone-sensor-id-field"${z.sensor_mode !== "shelly" ? " hidden" : ""}>
        <label>${tr("climate_admin.sensor_device_id_label")}</label>
        <input type="text" class="zone-sensor-device-id" value="${z.sensor_device_id || ""}" placeholder="shellyht-XXXXXXXXXXXX">
      </div>
      <p class="hint-text zone-manual-hint"${z.sensor_mode !== "manual" ? " hidden" : ""}>${tr("climate_admin.manual_mode_hint")}</p>
      <div class="admin-field">
        <label>${tr("climate_admin.valve_mode_label")}</label>
        <select class="zone-valve-mode" onchange="onValveModeChange(this)">${valveOptions}</select>
      </div>
      <div class="admin-field zone-valve-id-field"${(z.valve_mode || "shelly") !== "shelly" ? " hidden" : ""}>
        <label>${tr("climate_admin.valve_device_id_label")}</label>
        <input type="text" class="zone-valve-device-id" value="${z.valve_device_id || ""}" placeholder="shellyplus1-XXXXXXXXXXXX">
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
// One more fixed, standalone Shelly H&T Gen3 sensor (outdoor
// temperature/humidity) - no valve, no schedule, not tied to a zone,
// and unlike a zone's own sensor there's no simulated/manual mode: it's
// either a real device id or nothing, so the only fields are the id
// itself and this section's own live status. Fixed name, same
// convention as the extra switches above - see
// services/climate_map_store.py's EXTRA_SENSOR_FIELDS.
const EXTRA_SENSOR_NAME = "Outdoor Sensor";
let extraSensorRestartPending = false;

async function loadExtraSensor() {
  const res = await fetch("/api/climate-admin/extra-sensors");
  const data = await res.json();
  document.getElementById("extra-outdoor-sensor-id").value = data.outdoor_sensor_device_id || "";
  extraSensorRestartPending = !!data.restart_pending;
}

async function saveExtraSensor(button) {
  const resultEl = document.getElementById("save-extra-sensor-result");
  const body = {
    outdoor_sensor_device_id: document.getElementById("extra-outdoor-sensor-id").value.trim() || null,
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
  if (!document.getElementById("extra-outdoor-sensor-id").value.trim()) {
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
    slot.innerHTML = `<div>${tr("climate_admin.sensor_label")}: ${sensorStatusHtml(zoneShaped, "shelly")}</div>`;
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
    <button class="range-btn admin-valve-test" onclick="testValve('${zone.valve}', 'on', this)">${tr("climate_admin.valve_test_open")}</button>
    <button class="range-btn admin-valve-test" onclick="testValve('${zone.valve}', 'off', this)">${tr("climate_admin.valve_test_close")}</button>
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
      <button class="range-btn" onclick="setManualValue('${zone.name}', this)">${tr("climate_admin.manual_set_button")}</button>
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
  if (enabled && !confirm(tr("climate_admin.diagnostic_confirm_on"))) {
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
      alert(tr("sensors.error_prefix") + data.error);
      checkbox.checked = !enabled;
      return;
    }
  } catch (e) {
    alert(tr("sensors.request_failed"));
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
      <div><strong>${name}</strong></div>
      <div>${state}</div>
      <div class="toolbar">
        <button class="range-btn" onclick="diagnosticToggle('${name}', 'on', this)">${tr("climate_admin.valve_test_open")}</button>
        <button class="range-btn" onclick="diagnosticToggle('${name}', 'off', this)">${tr("climate_admin.valve_test_close")}</button>
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
    if (!data.ok) alert(tr("sensors.error_prefix") + data.error);
    refreshStatus();
  } catch (e) {
    alert(tr("sensors.request_failed"));
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
    if (!data.ok) alert(tr("sensors.error_prefix") + data.error);
    refreshStatus();
  } catch (e) {
    alert(tr("sensors.request_failed"));
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
}

async function setManualValue(zoneName, button) {
  const box = button.closest(".admin-manual-box");
  const temperature_c = parseFloat(box.querySelector(".manual-temp-input").value);
  if (Number.isNaN(temperature_c)) {
    alert(tr("climate_admin.manual_temp_required"));
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
      alert(tr("sensors.error_prefix") + data.error);
      return;
    }
    refreshStatus();
  } catch (e) {
    alert(tr("sensors.request_failed"));
  }
}

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
    refreshAll();
    startPolling();
  }
});

loadMap();
loadBroker();
loadExtraSwitches();
loadExtraSensor();
loadClimateSwitchNames();
startPolling();
