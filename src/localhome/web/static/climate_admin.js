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

let savedMap = null; // last GET /api/climate-admin/map response
let pollTimer = null;

// ------------------------------------------------------------ config form ----

function zoneFormHtml(zone, index) {
  const z = zone || { name: "", sensor_mode: "shelly", sensor_device_id: "", valve_device_id: "", cool_enabled: true };
  const options = SENSOR_MODES.map(
    m => `<option value="${m}" ${z.sensor_mode === m ? "selected" : ""}>${tr(`climate_admin.mode_${m}`)}</option>`
  ).join("");
  return `
    <div class="admin-zone-card" data-index="${index}">
      <div class="admin-field">
        <label>${tr("climate_admin.name_label")}</label>
        <input type="text" class="zone-name" value="${z.name || ""}" placeholder="${tr("climate_admin.name_placeholder")}">
      </div>
      <div class="admin-field">
        <label>${tr("climate_admin.sensor_mode_label")}</label>
        <select class="zone-sensor-mode" onchange="onSensorModeChange(this)">${options}</select>
      </div>
      <div class="admin-field zone-sensor-id-field"${z.sensor_mode !== "shelly" ? " hidden" : ""}>
        <label>${tr("climate_admin.sensor_device_id_label")}</label>
        <input type="text" class="zone-sensor-device-id" value="${z.sensor_device_id || ""}" placeholder="shellyht-XXXXXXXXXXXX">
      </div>
      <div class="admin-field">
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
}

async function loadMap() {
  const res = await fetch("/api/climate-admin/map");
  savedMap = await res.json();
  const zoneNames = savedMap.config_yaml_zone_names || [];
  const zones = [];
  for (let i = 0; i < 2; i++) {
    const saved = savedMap.zones && savedMap.zones[i];
    zones.push(saved || {
      name: zoneNames[i] || "", sensor_mode: "shelly", sensor_device_id: "", valve_device_id: "", cool_enabled: true,
    });
  }
  document.getElementById("zone-forms").innerHTML = zones.map((z, i) => zoneFormHtml(z, i)).join("");
  refreshStatus();
}

function collectZonesFromForm() {
  return [...document.querySelectorAll(".admin-zone-card")].map(card => {
    const mode = card.querySelector(".zone-sensor-mode").value;
    return {
      name: card.querySelector(".zone-name").value.trim(),
      sensor_mode: mode,
      sensor_device_id: mode === "shelly" ? card.querySelector(".zone-sensor-device-id").value.trim() : null,
      valve_device_id: card.querySelector(".zone-valve-device-id").value.trim(),
      cool_enabled: card.querySelector(".zone-cool-enabled").checked,
    };
  });
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

function valveStatusHtml(zone) {
  if (zone.valve_ok) {
    const state = zone.valve_on ? tr("climate_admin.valve_open") : tr("climate_admin.valve_closed");
    return `<span class="admin-status ok">${tr("climate_admin.reporting")}</span> ${state}`;
  }
  return `<span class="admin-status bad">${tr("climate_admin.valve_not_reachable")}</span>`;
}

function statusRowHtml(zone, mapEntry) {
  const mode = mapEntry ? mapEntry.sensor_mode : "shelly";
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
      <div>${tr("climate_admin.valve_label")}: ${valveStatusHtml(zone)}</div>
      ${manualBoxHtml}
    </div>
  `;
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/climate/zones");
    const data = await res.json();
    const byName = {};
    ((savedMap && savedMap.zones) || []).forEach(z => { byName[z.name] = z; });
    const rows = (data.zones || []).map(zone => statusRowHtml(zone, byName[zone.name])).join("");
    document.getElementById("status-list").innerHTML = rows || `<p class="hint-text">${tr("climate_admin.status_none")}</p>`;
  } catch (e) {
    // Transient fetch failure - leave the last rendering in place and
    // keep polling rather than blanking the page.
  }
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
  pollTimer = setInterval(refreshStatus, 3500);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
  } else {
    refreshStatus();
    startPolling();
  }
});

loadMap();
startPolling();
