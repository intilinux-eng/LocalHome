// Admin panel: Home-tab device layout (show/hide, drag-to-reorder,
// number/switch pairing). Deliberately a separate page/script from the
// main dashboard's app.js - this is a low-key, occasional-use, form-heavy
// screen that shouldn't bloat the bundle every regular visit loads.

function tr(path, vars) {
  const value = path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), window.I18N);
  let str = value != null ? value : path;
  if (vars) for (const k in vars) str = str.replace(`{${k}}`, vars[k]);
  return str;
}

function slug(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

let devices = [];
let switchNames = [];

async function loadDevices() {
  const res = await fetch("/api/admin/devices");
  const data = await res.json();
  if (!data.ok) return;
  devices = data.devices;
  switchNames = data.switch_names;
  renderDeviceList();
}

// One dropdown covers both "tab" and "hidden" - once a device is hidden,
// which tab it would otherwise appear on doesn't matter, so presenting
// them as three mutually-exclusive options is simpler than a tab
// selector plus a separate hidden checkbox that's usually irrelevant.
function showOnValue(device) {
  return device.hidden ? "hidden" : device.tab;
}

function buildDeviceRow(device) {
  const pairedSwitchHtml = device.kind === "number" ? `
    <label class="admin-field">
      <span>${tr("admin.paired_switch_label")}</span>
      <select data-field="paired_switch" data-name="${device.name}">
        <option value="">${tr("admin.paired_switch_none")}</option>
        ${switchNames.map(n => `<option value="${n}" ${device.paired_switch === n ? "selected" : ""}>${n}</option>`).join("")}
      </select>
    </label>
  ` : "";

  const showOn = showOnValue(device);
  return `
    <li class="device-row" draggable="true" data-name="${device.name}">
      <span class="drag-handle">⠿</span>
      <span class="device-name">${device.name}</span>
      <span class="device-kind">${device.kind}</span>
      <label class="admin-field">
        <span>${tr("admin.show_on_label")}</span>
        <select data-field="show_on" data-name="${device.name}">
          <option value="home" ${showOn === "home" ? "selected" : ""}>${tr("admin.show_on_home")}</option>
          <option value="climate" ${showOn === "climate" ? "selected" : ""}>${tr("admin.show_on_climate")}</option>
          <option value="hidden" ${showOn === "hidden" ? "selected" : ""}>${tr("admin.show_on_hidden")}</option>
        </select>
      </label>
      <label class="admin-field">
        <span>${tr("admin.visible_in_mode_label")}</span>
        <select data-field="visible_in_mode" data-name="${device.name}">
          <option value="" ${!device.visible_in_mode ? "selected" : ""}>${tr("admin.visible_in_mode_always")}</option>
          <option value="heat" ${device.visible_in_mode === "heat" ? "selected" : ""}>${tr("admin.visible_in_mode_heat")}</option>
          <option value="cool" ${device.visible_in_mode === "cool" ? "selected" : ""}>${tr("admin.visible_in_mode_cool")}</option>
        </select>
      </label>
      ${pairedSwitchHtml}
    </li>
  `;
}

function renderDeviceList() {
  const list = document.getElementById("device-list");
  list.innerHTML = devices.map(buildDeviceRow).join("");
  list.querySelectorAll("select").forEach(select => select.addEventListener("change", onFieldChange));
  list.querySelectorAll(".device-row").forEach(row => {
    row.addEventListener("dragstart", onDragStart);
    row.addEventListener("dragend", onDragEnd);
  });
}

async function postJson(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) alert(`${tr("admin.save_failed")}: ${data.error}`);
  } catch (e) {
    alert(tr("admin.save_failed"));
  }
}

function onFieldChange(e) {
  const select = e.target;
  const { name, field } = select.dataset;
  if (field === "show_on") {
    const value = select.value;
    postJson(`/api/admin/devices/${encodeURIComponent(name)}/override`,
      value === "hidden" ? { hidden: true } : { hidden: false, tab: value });
  } else if (field === "visible_in_mode") {
    postJson(`/api/admin/devices/${encodeURIComponent(name)}/override`, { visible_in_mode: select.value || null });
  } else if (field === "paired_switch") {
    postJson(`/api/admin/devices/${encodeURIComponent(name)}/override`, { paired_switch: select.value || null });
  }
}

function onDragStart(e) {
  e.dataTransfer.setData("text/plain", e.currentTarget.dataset.name);
  e.currentTarget.classList.add("dragging");
}

function onDragEnd(e) {
  e.currentTarget.classList.remove("dragging");
  const order = [...document.querySelectorAll("#device-list .device-row")].map(r => r.dataset.name);
  postJson("/api/admin/devices/order", { order });
}

// One listener on the container, not per-row: reorders the live DOM
// during drag by finding which sibling's vertical midpoint the pointer
// has crossed - the standard no-library HTML5 drag-and-drop pattern.
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("device-list").addEventListener("dragover", e => {
    e.preventDefault();
    const list = document.getElementById("device-list");
    const dragging = list.querySelector(".dragging");
    if (!dragging) return;
    const after = [...list.querySelectorAll(".device-row:not(.dragging)")].find(row => {
      const box = row.getBoundingClientRect();
      return e.clientY < box.top + box.height / 2;
    });
    list.insertBefore(dragging, after || null);
  });
  loadDevices();
});
