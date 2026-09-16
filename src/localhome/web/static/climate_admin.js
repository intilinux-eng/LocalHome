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

const SENSOR_MODES = ["shelly", "simulated", "manual"];
const VALVE_MODES = ["shelly", "simulated"];

let savedMap = null; // last GET /api/climate-admin/map response
let extraSwitchesRestartPending = false; // from the last GET /api/climate-admin/extra-switches
let pollTimer = null;

// ------------------------------------------------------------ config form ----

function zoneFormHtml(zone, index) {
  const z = zone || { name: "", sensor_mode: "shelly", sensor_device_id: "", valve_mode: "shelly", valve_device_id: "", cool_enabled: true };
  const sensorOptions = SENSOR_MODES.map(
    m => `<option value="${m}" ${z.sensor_mode === m ? "selected" : ""}>${tr(`climate_admin.mode_${m}`)}</option>`
  ).join("");
  const valveOptions = VALVE_MODES.map(
    m => `<option value="${m}" ${(z.valve_mode || "shelly") === m ? "selected" : ""}>${tr(`climate_admin.mode_${m}`)}</option>`
  ).join("");
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

async function testBroker() {
  const resultEl = document.getElementById("broker-test-result");
  const host = document.getElementById("broker-host").value.trim();
  const port = parseInt(document.getElementById("broker-port").value, 10);
  resultEl.textContent = "";
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
  }
}

async function saveBroker() {
  const resultEl = document.getElementById("broker-save-result");
  const changePassword = document.getElementById("broker-change-password").checked;
  const body = {
    host: document.getElementById("broker-host").value.trim(),
    port: parseInt(document.getElementById("broker-port").value, 10),
    username: document.getElementById("broker-username").value.trim(),
    change_password: changePassword,
  };
  if (changePassword) body.password = document.getElementById("broker-password").value;
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
    resultEl.textContent = tr("climate_admin.broker_save_success");
    resultEl.className = "hint-text admin-success";
    await loadBroker();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  }
}

async function saveMap() {
  const resultEl = document.getElementById("save-result");
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
    resultEl.textContent = text;
    resultEl.className = "hint-text admin-success";
    await loadMap();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
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

async function saveExtraSwitches() {
  const resultEl = document.getElementById("save-extra-result");
  const body = {
    bypass_valve_device_id: document.getElementById("extra-bypass-valve-id").value.trim() || null,
    dehumidifier_device_id: document.getElementById("extra-dehumidifier-id").value.trim() || null,
  };
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
    resultEl.textContent = tr("climate_admin.save_success");
    resultEl.className = "hint-text admin-success";
    await loadExtraSwitches();
  } catch (e) {
    resultEl.textContent = tr("sensors.request_failed");
    resultEl.className = "hint-text admin-error";
  }
}

// -------------------------------------------------------------- status ----
// Reuses /api/climate/zones as-is (see web/app.py) - joined client-side
// against the *saved* map by zone name, so a config edit not yet applied
// (restart pending) never gets confused with the live-running mapping.

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
  if (zone.sensor_ok) {
    let extra = "";
    if (zone.humidity_pct != null) extra += ` · ${fmt(zone.humidity_pct, 0)}%`;
    if (zone.battery_pct != null) extra += ` · ${tr("fields.battery_pct")} ${fmt(zone.battery_pct, 0)}%`;
    return `<span class="admin-status ok">${tr("climate_admin.reporting")}</span> ${fmt(zone.temperature_c, 1)}°C${extra}`;
  }
  const error = zone.sensor_error || "";
  if (error.includes("no MQTT message received yet")) {
    return `<span class="admin-status neutral">${tr("climate_admin.not_reachable_battery")}</span>`;
  }
  if (error.includes("probably offline")) {
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

function statusRowHtml(zone, mapEntry) {
  const mode = mapEntry ? mapEntry.sensor_mode : "shelly";
  const valveMode = mapEntry ? (mapEntry.valve_mode || "shelly") : "shelly";
  const manualBoxHtml = mode === "manual" ? `
    <div class="admin-manual-box">
      <input type="number" step="0.1" class="manual-temp-input" placeholder="${tr("climate_admin.manual_temp_label")}">
      <input type="number" step="0.1" class="manual-humidity-input" placeholder="${tr("climate_admin.manual_humidity_label")}">
      <button class="range-btn" onclick="setManualValue('${zone.name}', this)">${tr("climate_admin.manual_set_button")}</button>
    </div>` : "";

  return `
    <div class="admin-status-row">
      <div class="admin-status-name">${zone.name}</div>
      <div>${tr("climate_admin.sensor_label")}: ${sensorStatusHtml(zone, mode)}</div>
      <div>${targetHtml(zone)}</div>
      <div>${tr("climate_admin.valve_label")}: ${valveStatusHtml(zone, valveMode)}</div>
      <p class="hint-text admin-valve-test-hint">${tr("climate_admin.valve_test_hint")}</p>
      ${manualBoxHtml}
    </div>
  `;
}

function modeAwayLineHtml(data) {
  if (!data.mode) return "";
  const modeLabel = data.mode === "cool" ? tr("climate.mode_cool") : tr("climate.mode_heat");
  const awayText = data.away_until
    ? tr("climate.away_active", { until: new Date(data.away_until).toLocaleString() })
    : tr("climate.away_idle");
  return `${modeLabel} · ${awayText}`;
}

function isTypingAManualValue() {
  const active = document.activeElement;
  return !!(active && active.matches && active.matches(".manual-temp-input, .manual-humidity-input"));
}

async function refreshStatus() {
  // Rebuilding #status-list wholesale (below) would otherwise wipe out
  // the temperature/humidity inputs mid-keystroke on every poll tick -
  // exactly the "can't even finish typing before it refreshes" bug -
  // so skip this tick entirely while the operator is actively focused
  // on one of them. Clicking "Imposta" itself still refreshes right
  // away (setManualValue() calls this directly, and by then focus has
  // moved to the button, not an input).
  if (isTypingAManualValue()) return;
  try {
    const res = await fetch("/api/climate/zones");
    const data = await res.json();
    document.getElementById("mode-away-line").textContent = modeAwayLineHtml(data);
    // A single all-or-nothing signal, not per-zone: a restart rebuilds
    // every zone's drivers together, so "one thing changed" means the
    // whole section below is stale, not just the zone that changed -
    // see web/app.py's ACTIVE_CLIMATE_ZONES/ACTIVE_EXTRA_SWITCHES.
    if ((savedMap && savedMap.restart_pending) || extraSwitchesRestartPending) {
      document.getElementById("status-list").innerHTML = `<p class="hint-text admin-restart-pending">${tr("climate_admin.restart_pending")}</p>`;
      return;
    }
    const byName = {};
    ((savedMap && savedMap.zones) || []).forEach(z => { byName[z.name] = z; });
    const rows = (data.zones || []).map(zone => statusRowHtml(zone, byName[zone.name])).join("");
    document.getElementById("status-list").innerHTML = rows || `<p class="hint-text">${tr("climate_admin.status_none")}</p>`;
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
startPolling();
