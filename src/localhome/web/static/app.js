// Dashboard frontend. Deliberately plain JS with no build step - this is
// meant to be a project people poke at directly, not a bundled app.
//
// The page never hardcodes which sensors exist: it asks the backend
// (`/api/devices`) and renders one card per sensor based on its `kind`
// (power_meter / climate / switch / number). Adding a new driver
// instance in config.yaml makes a new card show up with no frontend
// change, as long as it's one of those kinds - see
// docs/adding-a-driver.md for what a genuinely new kind needs here.

// Looks up a dotted key (e.g. "covers.summary") in window.I18N - the
// translations object rendered server-side from locales/<lang>.json -
// and substitutes any {placeholder} in it from `vars`. Falls back to the
// key itself if a translation is missing, so a stale/partial locale file
// never breaks rendering.
function tr(path, vars) {
  const value = path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), window.I18N);
  let str = value != null ? value : path;
  if (vars) for (const k in vars) str = str.replace(`{${k}}`, vars[k]);
  return str;
}

const LOCALE = (window.I18N && window.I18N.locale) || undefined;

const FIELD_META = {
  power_w: { label: tr("fields.power_w"), unit: "W", digits: 0 },
  voltage_v: { label: tr("fields.voltage_v"), unit: "V", digits: 0 },
  current_a: { label: tr("fields.current_a"), unit: "A", digits: 2 },
  day_kwh: { label: tr("fields.day_kwh"), unit: "kWh", digits: 2 },
  yesterday_kwh: { label: tr("fields.yesterday_kwh"), unit: "kWh", digits: 2 },
  month_kwh: { label: tr("fields.month_kwh"), unit: "kWh", digits: 1 },
  temperature_c: { label: tr("fields.temperature_c"), unit: "°C", digits: 1 },
  humidity_pct: { label: tr("fields.humidity_pct"), unit: "%", digits: 0 },
  co2_ppm: { label: tr("fields.co2_ppm"), unit: "ppm", digits: 0 },
  ch2o_mgm3: { label: tr("fields.ch2o_mgm3"), unit: "mg/m3", digits: 3 },
  voc_mgm3: { label: tr("fields.voc_mgm3"), unit: "mg/m3", digits: 3 },
  pm25_ugm3: { label: tr("fields.pm25_ugm3"), unit: "µg/m3", digits: 0 },
  pm10_ugm3: { label: tr("fields.pm10_ugm3"), unit: "µg/m3", digits: 0 },
  battery_pct: { label: tr("fields.battery_pct"), unit: "%", digits: 0 },
};

const AIR_QUALITY_LEVELS = {
  level_1: { label: tr("air_quality.level_1"), cls: "" },
  level_2: { label: tr("air_quality.level_2"), cls: "" },
  level_3: { label: tr("air_quality.level_3"), cls: "" },
  level_4: { label: tr("air_quality.level_4"), cls: "warn" },
  level_5: { label: tr("air_quality.level_5"), cls: "critical" },
  level_6: { label: tr("air_quality.level_6"), cls: "critical" },
};

const HIDDEN_FIELDS = new Set(["ok", "name", "online", "seconds_since_update", "is_on", "value", "error"]);
const HEADLINE_FIELDS = { power_meter: "power_w", climate: "temperature_c" };

const ICON_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"></polyline></svg>';
const ICON_STOP = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"></rect></svg>';
const ICON_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>';

function slug(name) {
  return name.replace(/[^a-z0-9]/gi, "-").toLowerCase();
}

function fmt(value, digits) {
  return typeof value === "number" ? value.toFixed(digits) : "--";
}

// ---------------------------------------------------------------- covers ---

function renderCovers(names) {
  const el = document.getElementById("cover-cards");
  el.innerHTML = names.map(name => `
    <div class="card">
      <div class="card-head">
        <h2>${name}</h2>
        <span class="state" id="cover-state-${slug(name)}">${tr("covers.loading")}</span>
      </div>
      <div class="visual">
        <div class="cover" id="cover-fill-${slug(name)}"></div>
        <span class="pct-badge" id="cover-badge-${slug(name)}">--</span>
      </div>
      <div class="buttons">
        <button class="icon-btn open" onclick="sendCoverAction('${name}', 'open')" title="${tr("covers.btn_open")}">${ICON_OPEN}</button>
        <button class="icon-btn stop" onclick="sendCoverAction('${name}', 'stop')" title="${tr("covers.btn_stop")}">${ICON_STOP}</button>
        <button class="icon-btn close" onclick="sendCoverAction('${name}', 'close')" title="${tr("covers.btn_close")}">${ICON_CLOSE}</button>
      </div>
      <div class="slider-row">
        <input type="range" min="0" max="100" value="0" class="pct-slider" id="cover-slider-${slug(name)}" data-name="${name}">
        <span class="pct" id="cover-pct-${slug(name)}">0%</span>
      </div>
    </div>
  `).join("");

  // iOS Safari's native range input only responds to dragging the round
  // thumb; tapping elsewhere on the track does nothing. Add manual
  // tap-to-jump support so it behaves the same on every platform.
  document.querySelectorAll(".pct-slider").forEach(slider => {
    const name = slider.dataset.name;
    const pctEl = document.getElementById(`cover-pct-${slug(name)}`);

    slider.addEventListener("input", () => {
      pctEl.textContent = slider.value + "%";
      setCoverVisual(name, parseInt(slider.value, 10));
    });
    slider.addEventListener("change", () => movePercent(name, slider.value));
    slider.addEventListener("pointerdown", e => {
      const rect = slider.getBoundingClientRect();
      const thumbX = rect.left + (slider.value / 100) * rect.width;
      if (Math.abs(e.clientX - thumbX) > 20) {
        const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
        const value = Math.round(ratio * 100);
        slider.value = value;
        pctEl.textContent = value + "%";
        setCoverVisual(name, value);
        movePercent(name, value);
      }
    });
  });
}

function setCoverVisual(name, percent) {
  const fill = document.getElementById(`cover-fill-${slug(name)}`);
  const badge = document.getElementById(`cover-badge-${slug(name)}`);
  if (fill && percent !== null) fill.style.height = `${100 - percent}%`;
  if (badge) badge.textContent = percent === 0 ? tr("covers.badge_closed") : percent === 100 ? tr("covers.badge_open") : `${percent}%`;
}

const COVER_STATE_LABELS = {
  open: tr("covers.state_open"),
  close: tr("covers.state_close"),
  stop: tr("covers.state_stop"),
  unknown: tr("covers.state_unknown"),
};

async function refreshCovers(names) {
  const percents = [];
  for (const name of names) {
    const stateEl = document.getElementById(`cover-state-${slug(name)}`);
    const sliderEl = document.getElementById(`cover-slider-${slug(name)}`);
    const pctEl = document.getElementById(`cover-pct-${slug(name)}`);
    try {
      const res = await fetch(`/api/covers/${encodeURIComponent(name)}/status`);
      const data = await res.json();
      if (data.ok) {
        stateEl.textContent = (COVER_STATE_LABELS[data.state] || data.state) + (data.calibrated ? "" : tr("covers.not_calibrated"));
        percents.push(data.percent);
        if (document.activeElement !== sliderEl) {
          sliderEl.value = data.percent;
          pctEl.textContent = `${data.percent}%`;
          setCoverVisual(name, data.percent);
        }
      } else {
        stateEl.textContent = tr("covers.error", { error: data.error });
      }
    } catch (e) {
      stateEl.textContent = tr("covers.unreachable");
    }
  }
  const summaryEl = document.getElementById("summary");
  if (percents.length) summaryEl.textContent = tr("covers.summary", { open: percents.filter(p => p > 0).length, total: percents.length });
}

const COVER_ACTION_LABELS = {
  open: tr("covers.action_open"),
  close: tr("covers.action_close"),
  stop: tr("covers.action_stop"),
};

async function sendCoverAction(name, action) {
  const targets = name === "all" ? window.COVER_NAMES : [name];
  for (const n of targets) {
    const stateEl = document.getElementById(`cover-state-${slug(n)}`);
    if (stateEl) stateEl.textContent = COVER_ACTION_LABELS[action] || "";
    setCoverVisual(n, action === "open" ? 100 : action === "close" ? 0 : null);
  }
  try {
    const res = await fetch(`/api/covers/${encodeURIComponent(name)}/action/${action}`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) alert(tr("sensors.error_prefix") + data.error);
  } catch (e) {
    alert(tr("sensors.request_failed"));
  }
  setTimeout(() => refreshCovers(window.COVER_NAMES), 1000);
}

async function movePercent(name, percent) {
  const stateEl = document.getElementById(`cover-state-${slug(name)}`);
  const sliderEl = document.getElementById(`cover-slider-${slug(name)}`);
  stateEl.textContent = tr("covers.moving_to", { percent });
  sliderEl.disabled = true;
  try {
    const res = await fetch(`/api/covers/${encodeURIComponent(name)}/position`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ percent: parseInt(percent, 10) }),
    });
    const data = await res.json();
    if (!data.ok) {
      alert(tr("sensors.error_prefix") + data.error);
      sliderEl.disabled = false;
      refreshCovers(window.COVER_NAMES);
      return;
    }
    setTimeout(() => {
      sliderEl.disabled = false;
      refreshCovers(window.COVER_NAMES);
    }, (data.duration || 0) * 1000 + 500);
  } catch (e) {
    alert(tr("sensors.request_failed"));
    sliderEl.disabled = false;
  }
}

// ------------------------------------------------------------- sensors -----

const peakCache = {};

function buildSensorCard(sensor) {
  const id = slug(sensor.name);
  const hasChart = sensor.kind === "power_meter" || sensor.kind === "climate";
  const range = sensor.range || { min: 0, max: 100, unit: "%" };
  return `
    <div class="sensor-card kind-${sensor.kind}" id="sensor-${id}">
      <div class="sensor-top">
        <span class="sensor-title"><span class="dot" id="dot-${id}"></span><span>${sensor.name}</span></span>
        <span class="state" id="updated-${id}"></span>
      </div>
      <div id="metrics-${id}"></div>
      ${sensor.kind === "number" ? `
        <div class="slider-row" style="margin-top:4px">
          <input type="range" min="${range.min}" max="${range.max}" value="${range.min}"
                 class="number-slider" id="number-slider-${id}" data-name="${sensor.name}">
          <span class="pct" id="number-value-${id}">-- ${range.unit}</span>
        </div>
      ` : ""}
      ${hasChart ? `
        <div class="chart-filters">
          <button class="range-btn" data-sensor="${id}" data-range="6h">6h</button>
          <button class="range-btn active" data-sensor="${id}" data-range="24h">24h</button>
          <button class="range-btn" data-sensor="${id}" data-range="7d">7d</button>
        </div>
        <div class="chart-wrap">
          <svg id="chart-${id}" viewBox="0 0 400 130" preserveAspectRatio="none"></svg>
          <div class="chart-tooltip" id="tooltip-${id}" hidden></div>
        </div>
      ` : ""}
    </div>
  `;
}

async function setNumberValue(name, value) {
  try {
    const res = await fetch(`/api/sensors/${encodeURIComponent(name)}/set-value`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: parseFloat(value) }),
    });
    const data = await res.json();
    if (!data.ok) alert(tr("sensors.error_prefix") + data.error);
  } catch (e) {
    alert(tr("sensors.request_failed"));
  }
}

function renderSensors(sensors) {
  const el = document.getElementById("sensor-cards");
  el.innerHTML = sensors.map(buildSensorCard).join("");

  document.querySelectorAll(".range-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.sensor;
      document.querySelectorAll(`.range-btn[data-sensor="${id}"]`).forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      chartRanges[id] = btn.dataset.range;
      const sensor = sensors.find(s => slug(s.name) === id);
      loadChart(sensor);
    });
  });

  document.querySelectorAll(".number-slider").forEach(slider => {
    const name = slider.dataset.name;
    const sensor = sensors.find(s => s.name === name);
    const unit = (sensor.range || {}).unit || "";
    const valueEl = document.getElementById(`number-value-${slug(name)}`);
    slider.addEventListener("input", () => {
      if (valueEl) valueEl.textContent = `${slider.value} ${unit}`;
    });
    slider.addEventListener("change", () => setNumberValue(name, slider.value));
  });

  for (const sensor of sensors) {
    chartRanges[slug(sensor.name)] = "24h";
    refreshSensor(sensor);
    loadChart(sensor);
  }
}

function refreshSensor(sensor) {
  const id = slug(sensor.name);
  fetch(`/api/sensors/${encodeURIComponent(sensor.name)}`)
    .then(r => r.json())
    .then(reading => updateSensorCard(sensor, reading))
    .catch(() => updateSensorCard(sensor, { ok: false, error: "unreachable" }));
}

function updateSensorCard(sensor, reading) {
  const id = slug(sensor.name);
  const dot = document.getElementById(`dot-${id}`);
  const updated = document.getElementById(`updated-${id}`);
  const metrics = document.getElementById(`metrics-${id}`);
  if (!dot || !metrics) return;

  if (!reading.ok) {
    dot.classList.remove("on");
    updated.textContent = reading.error || "error";
    metrics.innerHTML = `<div class="headline"><div class="big">--</div></div>`;
    return;
  }

  const online = reading.online === undefined || reading.online === true || reading.online === "ONLINE";
  dot.classList.toggle("on", online && (reading.is_on === undefined || reading.is_on));
  updated.textContent = reading.seconds_since_update != null ? `${reading.seconds_since_update}s ago` : "";

  const headlineField = HEADLINE_FIELDS[sensor.kind];
  const knownFields = Object.keys(FIELD_META).filter(f => f in reading && f !== headlineField);
  const extraFields = Object.keys(reading).filter(
    f => !HIDDEN_FIELDS.has(f) && !(f in FIELD_META) && typeof reading[f] === "number"
  );

  let html = "";

  if (sensor.kind === "power_meter") {
    html += `<div class="headline"><div class="big">${fmt(reading.power_w, 0)} <span>W</span></div></div>`;
  } else if (sensor.kind === "climate") {
    const level = reading.air_quality_index ? AIR_QUALITY_LEVELS[reading.air_quality_index] : null;
    html += `<div class="headline">`;
    if (level) html += `<div><div class="big ${level.cls}">${level.label}</div><div class="label">${tr("air_quality.label")}</div></div>`;
    if ("temperature_c" in reading) html += `<div><div class="big">${fmt(reading.temperature_c, 1)} <span>°C</span></div><div class="label">${tr("fields.temperature_c")}</div></div>`;
    if ("humidity_pct" in reading) html += `<div><div class="big">${fmt(reading.humidity_pct, 0)} <span>%</span></div><div class="label">${tr("fields.humidity_pct")}</div></div>`;
    html += `</div>`;
  } else if (sensor.kind === "switch") {
    html += `
      <div class="switch-body">
        <div>
          <div class="headline"><div class="big">${reading.power_w != null ? fmt(reading.power_w, 0) + " <span>W</span>" : (reading.is_on ? tr("sensors.switch_on") : tr("sensors.switch_off"))}</div></div>
          <div class="switch-sub">${reading.voltage_v != null ? fmt(reading.voltage_v, 0) + " V · " + fmt(reading.current_a, 2) + " A" : ""}</div>
        </div>
        <label class="toggle">
          <input type="checkbox" ${reading.is_on ? "checked" : ""} onchange="onSwitchToggle('${sensor.name}', this)">
          <span class="track"></span>
        </label>
      </div>
    `;
  } else if (sensor.kind === "number") {
    const unit = (sensor.range || {}).unit || "";
    html += `<div class="headline"><div class="big">${fmt(reading.value, 0)} <span>${unit}</span></div></div>`;
    const sliderEl = document.getElementById(`number-slider-${id}`);
    const valueEl = document.getElementById(`number-value-${id}`);
    if (sliderEl && document.activeElement !== sliderEl) sliderEl.value = reading.value;
    if (valueEl) valueEl.textContent = `${fmt(reading.value, 0)} ${unit}`;
  }

  const gridFields = sensor.kind === "climate"
    ? knownFields.filter(f => !["temperature_c", "humidity_pct", "air_quality_index"].includes(f))
    : sensor.kind === "power_meter"
      ? knownFields.filter(f => f !== "power_w")
      : knownFields.filter(f => !["power_w", "voltage_v", "current_a", "is_on"].includes(f));

  const cells = [...gridFields, ...extraFields].map(f => {
    const meta = FIELD_META[f] || { label: f, unit: "", digits: 0 };
    return `<div class="metric"><div class="value">${fmt(reading[f], meta.digits)}${meta.unit ? " " + meta.unit : ""}</div><div class="label">${meta.label}</div></div>`;
  });

  if (sensor.kind === "power_meter") {
    const peak = peakCache[id];
    if (peak) {
      cells.splice(1, 0, `
        <div class="metric">
          <div class="value ${sensor.power_budget && peak.value >= sensor.power_budget.trip_risk_w ? "critical" : ""}">${fmt(peak.value, 0)} W</div>
          <div class="value-sub">${peak.ts ? new Date(peak.ts * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : ""}</div>
          <div class="label">${tr("sensors.peak_today")}</div>
        </div>
      `);
    }
  }

  if (cells.length) html += `<div class="metric-grid">${cells.join("")}</div>`;

  if (sensor.kind === "power_meter" && sensor.power_budget) {
    const b = sensor.power_budget;
    html += `<div class="limit-note"><span class="swatch"></span> ${tr("sensors.power_budget_note", { available: (b.available_power_w / 1000).toFixed(2), trip: (b.trip_risk_w / 1000).toFixed(1) })}</div>`;
  } else if (sensor.kind === "climate" && "humidity_pct" in reading) {
    html += `<div class="limit-note"><span class="legend-dot" style="background:#ff9f0a"></span> ${tr("fields.temperature_c")} <span class="legend-dot" style="background:#3987e5;margin-left:10px"></span> ${tr("fields.humidity_pct")}</div>`;
  }

  metrics.innerHTML = html;
}

async function onSwitchToggle(name, el) {
  if (!el.checked) {
    const ok = confirm(tr("sensors.confirm_turn_off"));
    if (!ok) {
      el.checked = true;
      return;
    }
  }
  el.disabled = true;
  try {
    const res = await fetch(`/api/sensors/${encodeURIComponent(name)}/${el.checked ? "on" : "off"}`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      alert(tr("sensors.error_prefix") + data.error);
      el.checked = !el.checked;
    }
  } catch (e) {
    alert(tr("sensors.request_failed"));
    el.checked = !el.checked;
  }
  el.disabled = false;
}

// --------------------------------------------------------------- charts ----

const CHART_W = 400, CHART_H = 130, CHART_PAD_L = 34, CHART_PAD_TOP = 8, CHART_PAD_BOTTOM = 16;
const SERIES_COLOR = "#3987e5", WARNING_COLOR = "#fab219", CRITICAL_COLOR = "#d63b3b";
const TEMP_COLOR = "#ff9f0a", HUM_COLOR = "#3987e5";
const chartRanges = {};
const chartPointsCache = {};

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

function niceMax(value) {
  if (value <= 0) return 100;
  const pow = Math.pow(10, Math.floor(Math.log10(value)));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * pow) return step * pow;
  }
  return 10 * pow;
}

function formatTime(ts, range) {
  const d = new Date(ts * 1000);
  return range === "7d"
    ? d.toLocaleDateString(LOCALE, { weekday: "short", hour: "2-digit" })
    : d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
}

async function loadChart(sensor) {
  const id = slug(sensor.name);
  const range = chartRanges[id] || "24h";
  if (sensor.kind === "power_meter") {
    try {
      const [histRes, peakRes] = await Promise.all([
        fetch(`/api/sensors/${encodeURIComponent(sensor.name)}/history?field=power_w&range=${range}`),
        fetch(`/api/sensors/${encodeURIComponent(sensor.name)}/peak-today?field=power_w`),
      ]);
      const hist = await histRes.json();
      const peak = await peakRes.json();
      if (peak.ok) peakCache[id] = peak;
      if (hist.ok) drawPowerChart(id, hist.series.power_w, range, sensor.power_budget);
    } catch (e) { /* keep previous render */ }
  } else if (sensor.kind === "climate") {
    try {
      const res = await fetch(`/api/sensors/${encodeURIComponent(sensor.name)}/history?field=temperature_c&field=humidity_pct&range=${range}`);
      const data = await res.json();
      if (data.ok) drawClimateChart(id, data.series.temperature_c, data.series.humidity_pct, range);
    } catch (e) { /* keep previous render */ }
  }
}

function zoneColor(w, availableW, tripRiskW) {
  if (w >= tripRiskW) return CRITICAL_COLOR;
  if (w >= availableW) return WARNING_COLOR;
  return SERIES_COLOR;
}

function drawEmpty(svg) {
  svg.innerHTML = `<foreignObject x="0" y="0" width="${CHART_W}" height="${CHART_H}">
    <div xmlns="http://www.w3.org/1999/xhtml" class="chart-empty">${tr("sensors.not_enough_data")}</div>
  </foreignObject>`;
}

function drawPowerChart(id, points, range, budget) {
  chartPointsCache[id] = points;
  const svg = document.getElementById(`chart-${id}`);
  if (!svg) return;
  svg.innerHTML = "";
  if (!points || points.length < 2) return drawEmpty(svg);

  const availableW = budget ? budget.available_power_w : Math.max(...points.map(p => p.value)) * 1.2;
  const tripRiskW = budget ? budget.trip_risk_w : availableW * 1.2;
  const dataMax = Math.max(...points.map(p => p.value));
  const maxVal = Math.max(niceMax(dataMax * 1.2), tripRiskW * 1.1);
  const minTs = points[0].ts, maxTs = points[points.length - 1].ts;
  const plotW = CHART_W - CHART_PAD_L, plotH = CHART_H - CHART_PAD_TOP - CHART_PAD_BOTTOM;
  const baseY = CHART_PAD_TOP + plotH;
  const xOf = ts => CHART_PAD_L + ((ts - minTs) / Math.max(1, maxTs - minTs)) * plotW;
  const yOf = w => CHART_PAD_TOP + plotH - (Math.min(w, maxVal) / maxVal) * plotH;

  [0, 0.5, 1].forEach(frac => {
    const y = CHART_PAD_TOP + plotH * (1 - frac);
    svg.appendChild(svgEl("line", { x1: CHART_PAD_L, x2: CHART_W, y1: y, y2: y, stroke: "#2c2c2a", "stroke-width": 1 }));
    const label = svgEl("text", { x: CHART_PAD_L - 6, y: y + 3, "text-anchor": "end", fill: "#898781", "font-size": 9 });
    label.textContent = Math.round(maxVal * frac);
    svg.appendChild(label);
  });

  if (budget) {
    const availY = yOf(availableW), tripY = yOf(tripRiskW);
    svg.appendChild(svgEl("rect", { x: CHART_PAD_L, y: tripY, width: plotW, height: Math.max(0, availY - tripY), fill: WARNING_COLOR, opacity: 0.08 }));
    svg.appendChild(svgEl("rect", { x: CHART_PAD_L, y: CHART_PAD_TOP, width: plotW, height: Math.max(0, tripY - CHART_PAD_TOP), fill: CRITICAL_COLOR, opacity: 0.1 }));

    const refLine = (val, color, label) => {
      if (val > maxVal) return;
      const y = yOf(val);
      svg.appendChild(svgEl("line", { x1: CHART_PAD_L, x2: CHART_W, y1: y, y2: y, stroke: color, "stroke-width": 1, "stroke-dasharray": "4,3", opacity: 0.85 }));
      const t = svgEl("text", { x: CHART_W, y: y - 3, "text-anchor": "end", fill: color, "font-size": 8.5, "font-weight": 700 });
      t.textContent = label;
      svg.appendChild(t);
    };
    refLine(tripRiskW, CRITICAL_COLOR, tr("sensors.trip_risk", { value: (tripRiskW / 1000).toFixed(1) }));
    refLine(availableW, WARNING_COLOR, tr("sensors.safe_limit", { value: (availableW / 1000).toFixed(2) }));
  }

  const slotW = points.length > 1 ? xOf(points[1].ts) - xOf(points[0].ts) : plotW;
  const barW = Math.max(2, Math.min(24, slotW * 0.7));
  points.forEach(p => {
    const x = xOf(p.ts) - barW / 2, y = yOf(p.value), h = Math.max(1, baseY - y);
    svg.appendChild(svgEl("rect", {
      x, y, width: barW, height: h, rx: Math.min(4, barW / 2),
      fill: zoneColor(p.value, availableW, tripRiskW), "data-ts": p.ts, class: "power-bar",
    }));
  });

  const hit = svgEl("rect", { x: CHART_PAD_L, y: 0, width: plotW, height: CHART_H, fill: "transparent" });
  hit.addEventListener("pointermove", e => onPowerChartHover(e, id, svg, xOf, range));
  hit.addEventListener("pointerleave", () => hideTooltip(id));
  svg.appendChild(hit);
}

function onPowerChartHover(e, id, svg, xOf, range) {
  const points = chartPointsCache[id];
  const rect = svg.getBoundingClientRect();
  const x = (e.clientX - rect.left) * (CHART_W / rect.width);
  let nearest = points[0], best = Infinity;
  for (const p of points) {
    const dist = Math.abs(xOf(p.ts) - x);
    if (dist < best) { best = dist; nearest = p; }
  }
  svg.querySelectorAll(".power-bar").forEach(bar => {
    bar.style.filter = Number(bar.dataset.ts) === nearest.ts ? "brightness(1.3)" : "";
  });
  const tooltip = document.getElementById(`tooltip-${id}`);
  tooltip.hidden = false;
  tooltip.innerHTML = `<span class="val">${nearest.value.toFixed(0)} W</span> · ${formatTime(nearest.ts, range)}`;
  tooltip.style.left = (xOf(nearest.ts) * (rect.width / CHART_W)) + "px";
  tooltip.style.top = "0px";
}

function hideTooltip(id) {
  const tooltip = document.getElementById(`tooltip-${id}`);
  if (tooltip) tooltip.hidden = true;
  document.querySelectorAll(`#chart-${id} .power-bar`).forEach(bar => { bar.style.filter = ""; });
}

function niceBounds(values, pad, step) {
  const min = Math.min(...values) - pad, max = Math.max(...values) + pad;
  return [Math.floor(min / step) * step, Math.ceil(max / step) * step];
}

function drawClimateChart(id, tempPoints, humPoints, range) {
  const svg = document.getElementById(`chart-${id}`);
  if (!svg) return;
  svg.innerHTML = "";
  if (!tempPoints || tempPoints.length < 2 || !humPoints || humPoints.length < 2) return drawEmpty(svg);

  const CLIMATE_PAD_R = 34;
  const [tMin, tMax] = niceBounds(tempPoints.map(p => p.value), 1, 2);
  const [hMin, hMax] = niceBounds(humPoints.map(p => p.value), 5, 10);
  const minTs = tempPoints[0].ts, maxTs = tempPoints[tempPoints.length - 1].ts;
  const plotW = CHART_W - CHART_PAD_L - CLIMATE_PAD_R, plotH = CHART_H - CHART_PAD_TOP - CHART_PAD_BOTTOM;
  const xOf = ts => CHART_PAD_L + ((ts - minTs) / Math.max(1, maxTs - minTs)) * plotW;
  const yOfTemp = t => CHART_PAD_TOP + plotH - ((t - tMin) / (tMax - tMin)) * plotH;
  const yOfHum = h => CHART_PAD_TOP + plotH - ((h - hMin) / (hMax - hMin)) * plotH;

  [0, 0.5, 1].forEach(frac => {
    const y = CHART_PAD_TOP + plotH * (1 - frac);
    svg.appendChild(svgEl("line", { x1: CHART_PAD_L, x2: CHART_W - CLIMATE_PAD_R, y1: y, y2: y, stroke: "#2c2c2a", "stroke-width": 1 }));
    const tempLabel = svgEl("text", { x: CHART_PAD_L - 6, y: y + 3, "text-anchor": "end", fill: TEMP_COLOR, "font-size": 9 });
    tempLabel.textContent = Math.round(tMin + (tMax - tMin) * frac) + "°";
    svg.appendChild(tempLabel);
    const humLabel = svgEl("text", { x: CHART_W - CLIMATE_PAD_R + 6, y: y + 3, "text-anchor": "start", fill: HUM_COLOR, "font-size": 9 });
    humLabel.textContent = Math.round(hMin + (hMax - hMin) * frac) + "%";
    svg.appendChild(humLabel);
  });

  const lineFor = (points, yOf, color) => {
    const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.ts).toFixed(1)},${yOf(p.value).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  };
  lineFor(tempPoints, yOfTemp, TEMP_COLOR);
  lineFor(humPoints, yOfHum, HUM_COLOR);

  const hit = svgEl("rect", { x: CHART_PAD_L, y: 0, width: plotW, height: CHART_H, fill: "transparent" });
  hit.addEventListener("pointermove", e => onClimateChartHover(e, id, svg, xOf, tempPoints, humPoints, range));
  hit.addEventListener("pointerleave", () => hideTooltip(id));
  svg.appendChild(hit);
}

function onClimateChartHover(e, id, svg, xOf, tempPoints, humPoints, range) {
  const rect = svg.getBoundingClientRect();
  const x = (e.clientX - rect.left) * (CHART_W / rect.width);
  let nearest = tempPoints[0], nearestHum = humPoints[0], best = Infinity;
  for (let i = 0; i < tempPoints.length; i++) {
    const dist = Math.abs(xOf(tempPoints[i].ts) - x);
    if (dist < best) { best = dist; nearest = tempPoints[i]; nearestHum = humPoints[i]; }
  }
  const tooltip = document.getElementById(`tooltip-${id}`);
  tooltip.hidden = false;
  tooltip.innerHTML = `<span class="val" style="color:${TEMP_COLOR}">${nearest.value.toFixed(1)}°C</span> · <span class="val" style="color:${HUM_COLOR}">${nearestHum.value.toFixed(0)}%</span> · ${formatTime(nearest.ts, range)}`;
  tooltip.style.left = (xOf(nearest.ts) * (rect.width / CHART_W)) + "px";
  tooltip.style.top = "0px";
}

// ---------------------------------------------------------------- boot -----

async function boot() {
  renderCovers(window.COVER_NAMES);
  refreshCovers(window.COVER_NAMES);
  setInterval(() => refreshCovers(window.COVER_NAMES), 8000);

  const res = await fetch("/api/devices");
  const data = await res.json();
  renderSensors(data.sensors);
  setInterval(() => data.sensors.forEach(refreshSensor), 5000);
  setInterval(() => data.sensors.forEach(loadChart), 60000);
}

boot();
