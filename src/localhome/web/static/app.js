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
  brightness: { label: tr("fields.brightness"), unit: "%", digits: 0 },
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

// A raw seconds_since_update (see core/poller.py's ReadingCache) is
// meaningless to glance at once it's more than a couple minutes old -
// "743s ago" takes real effort to parse. This is the one formatter every
// "last updated"/staleness display in the app goes through.
function formatAge(seconds) {
  if (seconds == null) return null;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function formatUpdatedAgo(seconds) {
  const age = formatAge(seconds);
  return age == null ? "" : tr("sensors.updated_ago", { time: age });
}

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
  for (const name of names) {
    const stateEl = document.getElementById(`cover-state-${slug(name)}`);
    const sliderEl = document.getElementById(`cover-slider-${slug(name)}`);
    const pctEl = document.getElementById(`cover-pct-${slug(name)}`);
    try {
      const res = await fetch(`/api/covers/${encodeURIComponent(name)}/status`);
      const data = await res.json();
      if (data.ok) {
        stateEl.textContent = (COVER_STATE_LABELS[data.state] || data.state) + (data.calibrated ? "" : tr("covers.not_calibrated"));
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

// A `number` sensor whose config sets `paired_switch` to a real switch's
// name renders its slider inside that switch's card instead of its own -
// see docs/integrations/tplink.md. Precomputed once per /api/devices
// fetch, before any card is built or polled.
function attachPairings(sensors) {
  const byName = {};
  sensors.forEach(s => { byName[s.name] = s; });
  sensors.forEach(s => {
    const target = s.kind === "number" && byName[s.paired_switch];
    if (target) {
      target.pairedBrightness = s;
      s.embedded = true;
    }
  });
}

// One-tap values configured as `presets` on a number (e.g. a VMC's
// night/day/boost speeds). A preset's label is its own `label`, or the
// translated vmc_presets.<key>.
function presetLabel(preset) {
  return preset.label || tr(`vmc_presets.${preset.key}`);
}

function presetRowHtml(sensor) {
  if (!sensor.presets || !sensor.presets.length) return "";
  const unit = (sensor.range || {}).unit || "";
  return `
    <div class="preset-row" id="number-presets-${slug(sensor.name)}">
      ${sensor.presets.map(p => `
        <button class="preset-btn" data-name="${sensor.name}" data-value="${p.value}">
          <span class="preset-name">${presetLabel(p)}</span>
          <span class="preset-value">${p.value}${unit === "%" ? "%" : ` ${unit}`}</span>
        </button>`).join("")}
    </div>
  `;
}

function sliderRowHtml(sensor) {
  const id = slug(sensor.name);
  const range = sensor.range || { min: 0, max: 100, unit: "%" };
  return `
    ${presetRowHtml(sensor)}
    <div class="slider-row" style="margin-top:4px">
      <input type="range" min="${range.min}" max="${range.max}" value="${range.min}"
             class="number-slider" id="number-slider-${id}" data-name="${sensor.name}">
      <span class="pct" id="number-value-${id}">-- ${range.unit}</span>
    </div>
  `;
}

function buildSensorCard(sensor) {
  const id = slug(sensor.name);
  const hasChart = sensor.kind === "power_meter" || sensor.kind === "climate";

  if (sensor.embedded) {
    // No card of its own - refreshSensor()/updateSensorCard() still need
    // somewhere to write the reading, just not anywhere visible; the
    // slider itself is what actually shows up, nested in the paired
    // switch's card below.
    return `
      <div hidden>
        <span class="dot" id="dot-${id}"></span>
        <span id="updated-${id}"></span>
        <div id="metrics-${id}"></div>
      </div>
      ${sliderRowHtml(sensor)}
    `;
  }

  return `
    <div class="sensor-card kind-${sensor.kind}" id="sensor-${id}" data-visible-in-mode="${sensor.visible_in_mode || ""}">
      <div class="sensor-top">
        <span class="sensor-title"><span class="dot" id="dot-${id}"></span><span>${sensor.name}</span></span>
        <span class="state" id="updated-${id}"></span>
      </div>
      <div id="metrics-${id}"></div>
      ${sensor.kind === "number" ? sliderRowHtml(sensor) : ""}
      ${sensor.kind === "switch" && sensor.pairedBrightness ? buildSensorCard(sensor.pairedBrightness) : ""}
      ${hasChart ? `
        <div class="chart-filters">
          <button class="range-btn" data-sensor="${id}" data-range="6h">6h</button>
          <button class="range-btn active" data-sensor="${id}" data-range="24h">24h</button>
          <button class="range-btn" data-sensor="${id}" data-range="7d">7d</button>
        </div>
        <div class="chart-readout" id="readout-${id}"></div>
        <div class="chart-wrap">
          <svg id="chart-${id}" viewBox="0 0 400 130" preserveAspectRatio="none"></svg>
          <div class="chart-tooltip" id="tooltip-${id}" hidden></div>
        </div>
      ` : ""}
    </div>
  `;
}

async function setNumberValue(name, value, sensors) {
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
  // Re-read both the number and the switch it's paired into: with
  // on_with_value, setting a speed can also have switched it on.
  const number = (sensors || []).find(s => s.name === name);
  if (number) {
    refreshSensor(number);
    const owner = sensors.find(s => s.name === number.paired_switch);
    if (owner) refreshSensor(owner);
  }
}

function renderSensors(sensors, containerId = "sensor-cards") {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = sensors.filter(s => !s.embedded).map(buildSensorCard).join("");

  // Scoped to this container, not the whole document - renderSensors() is
  // called once per dashboard tab (Home, then the Climate tab's "extra"
  // switches like the dehumidifier), and a document-wide query here would
  // re-attach duplicate listeners onto the other tab's already-rendered cards.
  el.querySelectorAll(".range-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.sensor;
      el.querySelectorAll(`.range-btn[data-sensor="${id}"]`).forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      chartRanges[id] = btn.dataset.range;
      const sensor = sensors.find(s => slug(s.name) === id);
      loadChart(sensor);
    });
  });

  el.querySelectorAll(".number-slider").forEach(slider => {
    const name = slider.dataset.name;
    const sensor = sensors.find(s => s.name === name);
    const unit = (sensor.range || {}).unit || "";
    const valueEl = document.getElementById(`number-value-${slug(name)}`);
    slider.addEventListener("input", () => {
      if (valueEl) valueEl.textContent = `${slider.value} ${unit}`;
    });
    slider.addEventListener("change", () => setNumberValue(name, slider.value, sensors));
  });

  el.querySelectorAll(".preset-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.name;
      // Show the choice at once; the refresh after the command confirms it.
      btn.parentElement.querySelectorAll(".preset-btn").forEach(b => b.classList.toggle("active", b === btn));
      setNumberValue(name, btn.dataset.value, sensors);
    });
  });

  for (const sensor of sensors) {
    chartRanges[slug(sensor.name)] = "24h";
    refreshSensor(sensor);
    loadChart(sensor);
  }
}

function refreshSensor(sensor) {
  // A switch tagged `linked_valve` (see the dehumidifier/interlock example
  // in docs/configuration.md) also needs its follower valve's live state
  // fetched alongside its own reading, so the card can show what it's
  // actually doing right now - not just "on", but "on, and that also
  // opened the bathroom valve" - see updateSensorCard()'s switch branch.
  const mainFetch = fetch(`/api/sensors/${encodeURIComponent(sensor.name)}`).then(r => r.json());
  const linkedFetch = sensor.linked_valve
    ? fetch(`/api/sensors/${encodeURIComponent(sensor.linked_valve)}`).then(r => r.json()).catch(() => null)
    : Promise.resolve(null);

  Promise.all([mainFetch, linkedFetch])
    .then(([reading, linkedReading]) => updateSensorCard(sensor, reading, linkedReading))
    .catch(() => updateSensorCard(sensor, { ok: false, error: "unreachable" }, null));
}

// A driver that went stale rather than never-connected (see
// drivers/mqtt/sensor.py's read()) still reports ok: false - a control
// loop must not act on it as fresh - but keeps its last known reading
// attached instead of discarding it. HAS_LAST_KNOWN_VALUE_FIELDS is
// what a card checks to tell "stale, but I still know the value" apart
// from "truly nothing to show" (a driver that never got this
// treatment, e.g. a fresh install, or a non-MQTT error with no
// fallback at all).
const HAS_LAST_KNOWN_VALUE_FIELDS = ["temperature_c", "humidity_pct", "power_w"];

function updateSensorCard(sensor, reading, linkedReading) {
  const id = slug(sensor.name);
  const dot = document.getElementById(`dot-${id}`);
  const updated = document.getElementById(`updated-${id}`);
  const metrics = document.getElementById(`metrics-${id}`);
  if (!dot || !metrics) return;

  const hasLastKnownValue = !reading.ok && HAS_LAST_KNOWN_VALUE_FIELDS.some(f => reading[f] != null);

  if (!reading.ok && !hasLastKnownValue) {
    dot.classList.remove("on");
    updated.textContent = reading.error || "error";
    metrics.innerHTML = `<div class="headline"><div class="big">--</div></div>`;
    return;
  }

  const online = reading.online === undefined || reading.online === true || reading.online === "ONLINE";
  dot.classList.toggle("on", reading.ok && online && (reading.is_on === undefined || reading.is_on));
  updated.textContent = reading.ok
    ? formatUpdatedAgo(reading.seconds_since_update)
    : staleValueNote(reading.last_value_age_seconds);

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
    // A non-controllable switch (e.g. a mode_switch, driven purely by
    // the thermostat's mode - see services/interlock.py) shows the same
    // status headline but no toggle: a manual flip here would just get
    // overridden on the controller's next tick, so offering one would
    // be misleading rather than actually inert.
    // `hide_toggle`: something meant to keep running (a VMC) - its state
    // is shown, but switching it off from here isn't offered.
    const toggleHtml = sensor.controllable && !sensor.hide_toggle ? `
        <label class="toggle">
          <input type="checkbox" ${reading.is_on ? "checked" : ""} onchange="onSwitchToggle('${sensor.name}', this)">
          <span class="track"></span>
        </label>` : "";
    html += `
      <div class="switch-body">
        <div>
          <div class="headline"><div class="big">${reading.power_w != null ? fmt(reading.power_w, 0) + " <span>W</span>" : sensor.hide_toggle ? (reading.is_on ? tr("sensors.running") : tr("sensors.stopped")) : (reading.is_on ? tr("sensors.switch_on") : tr("sensors.switch_off"))}</div></div>
          ${sensor.hide_toggle && !reading.is_on && sensor.pairedBrightness && sensor.pairedBrightness.presets ? `<div class="switch-sub">${tr("sensors.stopped_pick_preset")}</div>` : ""}
          <div class="switch-sub">${reading.voltage_v != null ? fmt(reading.voltage_v, 0) + " V · " + fmt(reading.current_a, 2) + " A" : ""}</div>
        </div>
        ${toggleHtml}
      </div>
    `;
    if (sensor.linked_valve) {
      const linkedOn = !!(linkedReading && linkedReading.ok && linkedReading.is_on);
      const key = linkedOn ? "climate.linked_valve_on" : "climate.linked_valve_off";
      html += `<div class="switch-sub linked-valve-note">${tr(key, { valve: sensor.linked_valve })}</div>`;
    }
  } else if (sensor.kind === "number") {
    const unit = (sensor.range || {}).unit || "";
    html += `<div class="headline"><div class="big">${fmt(reading.value, 0)} <span>${unit}</span></div></div>`;
    const sliderEl = document.getElementById(`number-slider-${id}`);
    const valueEl = document.getElementById(`number-value-${id}`);
    if (sliderEl && document.activeElement !== sliderEl) sliderEl.value = reading.value;
    if (valueEl) valueEl.textContent = `${fmt(reading.value, 0)} ${unit}`;
    document.querySelectorAll(`#number-presets-${id} .preset-btn`).forEach(btn => {
      btn.classList.toggle("active", Math.round(reading.value) === Number(btn.dataset.value));
    });
  }

  const gridFields = sensor.kind === "climate"
    ? knownFields.filter(f => !["temperature_c", "humidity_pct", "air_quality_index"].includes(f))
    : sensor.kind === "power_meter"
      ? knownFields.filter(f => f !== "power_w")
      : knownFields.filter(f => {
          if (["power_w", "voltage_v", "current_a", "is_on"].includes(f)) return false;
          if (f === "brightness" && sensor.pairedBrightness) return false; // shown via the slider instead
          return true;
        });

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
const TEMP_COLOR = "#ff9f0a", HUM_COLOR = "#3987e5", VALVE_BAND_COLOR = "#30d158";
const chartRanges = {};
const chartPointsCache = {};

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

// Rounds a chart's Y-axis max up to a "nice" round number instead of the
// exact data max (743W -> 1000, 5.2 -> 10, etc.), the same way graphing
// libraries pick axis bounds - Math.log10 finds which power of ten the
// value sits in, then the loop picks the smallest of a few human-friendly
// multiples of that power that's still >= value.
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
      // A thermostat zone's chart (sensor.valve set) also overlays when
      // its valve was on, fetched the same generic way as the
      // temperature/humidity series - is_on is just another recorded
      // history_fields entry on that switch (see services/history.py),
      // keyed by the valve's own name instead of the sensor's.
      // target_c is the target the thermostat actually drove the valve
      // against, recorded next to the zone's own temperature (only while
      // one was in control - see ThermostatController.tick()).
      const fields = sensor.valve ? "field=temperature_c&field=humidity_pct&field=target_c" : "field=temperature_c&field=humidity_pct";
      const fetches = [fetch(`/api/sensors/${encodeURIComponent(sensor.name)}/history?${fields}&range=${range}`)];
      if (sensor.valve) fetches.push(fetch(`/api/sensors/${encodeURIComponent(sensor.valve)}/history?field=is_on&range=${range}`));
      const [res, valveRes] = await Promise.all(fetches);
      const data = await res.json();
      const valveData = valveRes ? await valveRes.json() : null;
      if (data.ok) {
        drawClimateChart(id, data.series.temperature_c, data.series.humidity_pct, range,
          valveData && valveData.ok ? valveData.series.is_on : null,
          sensor.valve ? (data.series.target_c || []) : null);
      }
    } catch (e) { /* keep previous render */ }
  }
}

function zoneColor(w, availableW, tripRiskW) {
  if (w >= tripRiskW) return CRITICAL_COLOR;
  if (w >= availableW) return WARNING_COLOR;
  return SERIES_COLOR;
}

function drawEmpty(svg, height = CHART_H) {
  svg.innerHTML = `<foreignObject x="0" y="0" width="${CHART_W}" height="${height}">
    <div xmlns="http://www.w3.org/1999/xhtml" class="chart-empty">${tr("sensors.not_enough_data")}</div>
  </foreignObject>`;
}

// Every chart is drawn into a fixed CHART_W x CHART_H SVG viewBox (see the
// CHART_* constants near the top of this file) rather than sized off the
// real DOM - so the same pixel math works whatever CSS ends up scaling
// the <svg> to. `xOf`/`yOf` below are just linear maps from data units
// (a unix timestamp, a watt/degree/percent value) to that fixed pixel
// space: "where the oldest/newest point sits" and "where zero/max sits"
// become the two ends of each map.
function drawPowerChart(id, points, range, budget) {
  chartPointsCache[id] = points;  // hover needs the raw values back, not just what got drawn - see onPowerChartHover
  const svg = document.getElementById(`chart-${id}`);
  if (!svg) return;
  svg.innerHTML = "";
  if (!points || points.length < 2) return drawEmpty(svg);

  const availableW = budget ? budget.available_power_w : Math.max(...points.map(p => p.value)) * 1.2;
  const tripRiskW = budget ? budget.trip_risk_w : availableW * 1.2;
  const dataMax = Math.max(...points.map(p => p.value));
  // Axis max is whichever is bigger: a nice-rounded ceiling over the data
  // itself, or enough headroom to fit the trip-risk reference line - so
  // that line is never drawn above the top of the chart when it's high.
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

  // One invisible full-width/height rect handles hover for every bar,
  // instead of a listener per bar - simpler, and it still works over the
  // gaps between bars, not just on top of one.
  const hit = svgEl("rect", { x: CHART_PAD_L, y: 0, width: plotW, height: CHART_H, fill: "transparent" });
  hit.addEventListener("pointermove", e => onPowerChartHover(e, id, svg, xOf, range));
  hit.addEventListener("pointerleave", () => hideTooltip(id));
  svg.appendChild(hit);
}

function onPowerChartHover(e, id, svg, xOf, range) {
  const points = chartPointsCache[id];
  // The pointer event gives CSS pixels in the page; the chart's own math
  // is all in CHART_W/CHART_H viewBox units - this rescales one to the
  // other using however big the <svg> actually renders on screen, so
  // hover still lines up correctly whatever width the card ends up at.
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
  placeTooltip(tooltip, xOf(nearest.ts) * (rect.width / CHART_W));
}

// Centers the tooltip over the hovered point (it's styled translate(-50%)),
// but keeps it inside its chart - near either edge it used to hang off the
// card and get clipped, now more noticeably since the climate tooltip also
// carries the target.
function placeTooltip(tooltip, centerX) {
  const container = tooltip.offsetParent;
  const half = tooltip.offsetWidth / 2;
  const max = container ? container.clientWidth - half : centerX;
  tooltip.style.left = Math.max(half, Math.min(centerX, max)) + "px";
  tooltip.style.top = "0px";
}

function hideTooltip(id) {
  const tooltip = document.getElementById(`tooltip-${id}`);
  if (tooltip) tooltip.hidden = true;
  document.querySelectorAll(`#chart-${id} .power-bar`).forEach(bar => { bar.style.filter = ""; });
}

// Like niceMax, but for a full [min, max] range with a bit of breathing
// room (`pad`) on each side, snapped outward to the nearest multiple of
// `step` - so the temperature axis lands on round degrees and the
// humidity one on round tens, instead of whatever the raw data happened
// to range over.
function niceBounds(values, pad, step) {
  const min = Math.min(...values) - pad, max = Math.max(...values) + pad;
  return [Math.floor(min / step) * step, Math.ceil(max / step) * step];
}

// Bucket width per range - must match services/history.py's RANGE_BUCKETS.
// Used to tell a real gap (sensor down, no target in control) from
// consecutive buckets.
const CHART_BUCKET_SECONDS = { "6h": 60, "24h": 300, "7d": 3600 };
const TARGET_COLOR = "#d9dde8";
const GRID_COLOR = "#2c2c2a", AXIS_TEXT_COLOR = "#898781";

// Splits a bucketed series into runs of consecutive buckets, so a stretch
// with no data (away, off hours, sensor down...) shows as a gap instead
// of a line bridging across it.
function consecutiveRuns(points, bucketSeconds) {
  const runs = [];
  for (const p of points) {
    const run = runs[runs.length - 1];
    if (run && p.ts - run[run.length - 1].ts <= bucketSeconds * 1.5) run.push(p);
    else runs.push([p]);
  }
  return runs;
}

// The point of a bucketed step series in effect at `ts` (each bucket holds
// until the next one), or null past a gap - "no target was in control
// then", not "the last target seen, hours ago".
function stepPointAt(points, ts, bucketSeconds) {
  let found = null;
  for (const p of points) {
    if (p.ts <= ts + bucketSeconds / 2) found = p;
    else break;
  }
  return found && ts - found.ts <= bucketSeconds * 1.5 ? found : null;
}

function nearestPoint(points, ts) {
  let nearest = null, best = Infinity;
  for (const p of points) {
    const dist = Math.abs(p.ts - ts);
    if (dist < best) { best = dist; nearest = p; }
  }
  return nearest;
}

// The climate chart is three panels stacked on one shared time axis, so
// each series gets its own space instead of competing for one:
//   1. temperature, with the target it was held to (a band of +-
//      hysteresis around a solid line) - the valve only changes state
//      once the temperature leaves that band;
//   2. a strip showing when the valve was actually open (zone charts only);
//   3. humidity, smaller - context, not what drives the valve.
// Everything is in viewBox units (CHART_W wide); the height follows from
// which panels are present.
const CLIMATE_LAYOUT = { padL: 34, padR: 8, tempTop: 8, tempH: 104, gap: 9, laneH: 14, humH: 34, axisH: 14 };

// `textScale` > 1 on a chart rendered narrower than its viewBox (a phone):
// the SVG scales down as a whole, so its text and the room left for the
// axis labels are scaled back up to stay readable.
function climateLayout(hasValve, textScale = 1) {
  const L = { ...CLIMATE_LAYOUT, padL: Math.round(CLIMATE_LAYOUT.padL * textScale) };
  const tempBottom = L.tempTop + L.tempH;
  const laneTop = tempBottom + L.gap;
  const laneBottom = hasValve ? laneTop + L.laneH : tempBottom;
  const humTop = laneBottom + L.gap;
  const humBottom = humTop + L.humH;
  return { ...L, tempBottom, laneTop, laneBottom, humTop, humBottom, height: humBottom + Math.round(L.axisH * textScale), plotL: L.padL, plotR: CHART_W - L.padR };
}

// Round local-time ticks for the shared time axis: every hour on 6h,
// every 6 hours on 24h, every midnight on 7d.
function timeTicks(minTs, maxTs, range) {
  const ticks = [];
  const d = new Date(minTs * 1000);
  if (range === "7d") {
    d.setHours(0, 0, 0, 0);
    while (d.getTime() / 1000 <= maxTs) {
      if (d.getTime() / 1000 >= minTs) ticks.push(d.getTime() / 1000);
      d.setDate(d.getDate() + 1);
    }
    return ticks;
  }
  const stepHours = range === "6h" ? 1 : 6;
  d.setMinutes(0, 0, 0);
  while (d.getTime() / 1000 <= maxTs) {
    if (d.getTime() / 1000 >= minTs && d.getHours() % stepHours === 0) ticks.push(d.getTime() / 1000);
    d.setHours(d.getHours() + 1);
  }
  return ticks;
}

function formatTick(ts, range) {
  const d = new Date(ts * 1000);
  return range === "7d"
    ? d.toLocaleDateString(LOCALE, { weekday: "short" })
    : d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
}

// Latest reading / reading under the pointer, drawn with every chart
// state the pointer or the refresh leaves behind.
const climateChartState = {};

function drawClimateChart(id, tempPoints, humPoints, range, valvePoints, targetPoints) {
  const svg = document.getElementById(`chart-${id}`);
  if (!svg) return;
  const hasValve = valvePoints != null;
  const renderedWidth = svg.getBoundingClientRect().width || CHART_W;
  const textScale = Math.min(1.6, Math.max(1, CHART_W / renderedWidth));
  const lay = climateLayout(hasValve, textScale);
  svg.setAttribute("viewBox", `0 0 ${CHART_W} ${lay.height}`);
  svg.innerHTML = "";
  const temps = tempPoints || [], hums = humPoints || [];
  if (temps.length < 2) {
    delete climateChartState[id];
    renderClimateReadout(id, null);
    return drawEmpty(svg, lay.height);
  }

  const bucket = CHART_BUCKET_SECONDS[range] || 300;
  const hyst = climateHysteresis || 0;
  const minTs = temps[0].ts, maxTs = temps[temps.length - 1].ts;
  const targets = (targetPoints || []).filter(p => p.ts >= minTs - bucket && p.ts <= maxTs);
  const valves = valvePoints || [];
  const plotW = lay.plotR - lay.plotL;
  const xOf = ts => lay.plotL + ((ts - minTs) / Math.max(1, maxTs - minTs)) * plotW;
  const clampX = x => Math.max(lay.plotL, Math.min(lay.plotR, x));

  // Temperature axis in whole degrees, always an even span so the middle
  // gridline is a whole degree too. It fits the target band as well, or a
  // target far from the room's reading would be clipped off.
  let [tMin, tMax] = niceBounds(temps.map(p => p.value).concat(targets.flatMap(p => [p.value - hyst, p.value + hyst])), 0.4, 1);
  if ((tMax - tMin) % 2) tMax += 1;
  const yT = t => lay.tempBottom - ((t - tMin) / (tMax - tMin)) * lay.tempH;
  const humValues = hums.map(p => p.value);
  const [hMin, hMax] = humValues.length ? niceBounds(humValues, 2, 10) : [0, 100];
  const yH = h => lay.humBottom - ((h - hMin) / Math.max(1, hMax - hMin)) * lay.humH;

  const add = (tag, attrs) => svg.appendChild(svgEl(tag, attrs));
  const text = (x, y, content, attrs) => {
    const el = add("text", { x, y, fill: AXIS_TEXT_COLOR, ...attrs });
    el.setAttribute("font-size", (attrs && attrs["font-size"] || 9) * textScale);
    el.textContent = content;
    return el;
  };
  const stepRects = (points, fromX, toX) => points.forEach((p, i) => {
    const x1 = clampX(xOf(p.ts));
    const next = points[i + 1];
    const x2 = clampX(next && next.ts - p.ts <= bucket * 1.5 ? xOf(next.ts) : xOf(p.ts + bucket));
    fromX(x1, Math.max(1, x2 - x1), p);
  });

  // Shared time axis: one vertical gridline per tick through all panels.
  for (const ts of timeTicks(minTs, maxTs, range)) {
    const x = xOf(ts);
    add("line", { x1: x, x2: x, y1: lay.tempTop, y2: lay.humBottom, stroke: GRID_COLOR, "stroke-width": 1 });
    if (x > lay.plotL + 14 * textScale && x < lay.plotR - 14 * textScale) {
      text(x, lay.humBottom + 4 + 7 * textScale, formatTick(ts, range), { "text-anchor": "middle" });
    }
  }

  // --- 1. temperature panel
  [0, 0.5, 1].forEach(frac => {
    const y = lay.tempBottom - lay.tempH * frac;
    add("line", { x1: lay.plotL, x2: lay.plotR, y1: y, y2: y, stroke: GRID_COLOR, "stroke-width": 1 });
    text(lay.plotL - 6, y + 3 * textScale, `${Math.round(tMin + (tMax - tMin) * frac)}°`, { "text-anchor": "end", fill: TEMP_COLOR });
  });

  const openValves = valves.filter(p => p.value >= 0.5);
  // A faint echo of the valve strip below, so an open valve lines up with
  // the temperature it was reacting to without reading across panels.
  stepRects(openValves, (x, w) => add("rect", {
    x, y: lay.tempTop, width: w, height: lay.tempH, fill: VALVE_BAND_COLOR, opacity: 0.07,
  }));

  const targetRuns = consecutiveRuns(targets, bucket);
  for (const run of targetRuns) {
    let d = "";
    stepRects(run, (x, w, p) => {
      if (hyst > 0) {
        add("rect", {
          x, y: yT(p.value + hyst), width: w, height: Math.max(1, yT(p.value - hyst) - yT(p.value + hyst)),
          fill: TARGET_COLOR, opacity: 0.16,
        });
      }
      const y = yT(p.value).toFixed(1);
      d += `${d ? "L" : "M"}${x.toFixed(1)},${y} L${(x + w).toFixed(1)},${y} `;
    });
    add("path", { d, fill: "none", stroke: TARGET_COLOR, "stroke-width": 1.4, opacity: 0.9 });
  }
  if (hasValve && !targets.length) {
    text(lay.plotL + 6, lay.tempTop + 4 + 7 * textScale, tr("climate.no_target_in_range"), { "font-size": 8.5 });
  }

  for (const run of consecutiveRuns(temps, bucket)) {
    const d = run.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.ts).toFixed(1)},${yT(p.value).toFixed(1)}`).join(" ");
    add("path", { d, fill: "none", stroke: TEMP_COLOR, "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" });
  }

  // Where the valve opened, marked on the temperature line itself: the
  // reading at that moment against the band is the "why".
  valves.forEach((p, i) => {
    if (p.value < 0.5 || (i > 0 && valves[i - 1].value >= 0.5)) return;
    if (p.ts < minTs || p.ts > maxTs) return;
    const t = nearestPoint(temps, p.ts);
    if (!t || Math.abs(t.ts - p.ts) > bucket * 1.5) return;
    add("circle", { cx: xOf(p.ts), cy: yT(t.value), r: 3, fill: VALVE_BAND_COLOR, stroke: "#141414", "stroke-width": 1.2 });
  });

  // --- 2. valve strip
  if (hasValve) {
    text(lay.plotL - 6, lay.laneTop + lay.laneH / 2 + 3 * textScale, tr("climate.valve_lane"), { "text-anchor": "end", "font-size": 8.5 });
    add("rect", { x: lay.plotL, y: lay.laneTop, width: plotW, height: lay.laneH, rx: 3, fill: "#232322" });
    stepRects(openValves, (x, w) => add("rect", {
      x, y: lay.laneTop, width: w, height: lay.laneH, fill: VALVE_BAND_COLOR, opacity: 0.85,
    }));
  }

  // --- 3. humidity panel
  [0, 1].forEach(frac => {
    const y = lay.humBottom - lay.humH * frac;
    add("line", { x1: lay.plotL, x2: lay.plotR, y1: y, y2: y, stroke: GRID_COLOR, "stroke-width": 1 });
    text(lay.plotL - 6, y + 3 * textScale, `${Math.round(hMin + (hMax - hMin) * frac)}%`, { "text-anchor": "end", fill: HUM_COLOR });
  });
  for (const run of consecutiveRuns(hums, bucket)) {
    const d = run.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.ts).toFixed(1)},${yH(p.value).toFixed(1)}`).join(" ");
    add("path", { d, fill: "none", stroke: HUM_COLOR, "stroke-width": 1.5, "stroke-linecap": "round", "stroke-linejoin": "round" });
  }

  // --- hover: one crosshair through every panel, values in the readout
  const cross = add("line", { y1: lay.tempTop, y2: lay.humBottom, stroke: "#ffffff", "stroke-width": 1, opacity: 0.45, visibility: "hidden" });
  const dot = add("circle", { r: 3.5, fill: TEMP_COLOR, stroke: "#141414", "stroke-width": 1.2, visibility: "hidden" });

  const state = { range, bucket, hyst, temps, hums, targets, valves, hasValve };
  climateChartState[id] = state;
  renderClimateReadout(id, readingAt(state, maxTs), true);

  const hit = add("rect", { x: lay.plotL, y: 0, width: plotW, height: lay.height, fill: "transparent" });
  const onMove = e => {
    const rect = svg.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (CHART_W / rect.width);
    const ts = minTs + ((x - lay.plotL) / plotW) * (maxTs - minTs);
    const reading = readingAt(state, ts);
    if (!reading) return;
    const cx = xOf(reading.ts);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", yT(reading.temp)); dot.setAttribute("visibility", "visible");
    renderClimateReadout(id, reading, false);
  };
  hit.addEventListener("pointermove", onMove);
  hit.addEventListener("pointerdown", onMove);
  hit.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    renderClimateReadout(id, readingAt(state, maxTs), true);
  });
}

// Every series' value at one moment, for the readout.
function readingAt(state, ts) {
  const t = nearestPoint(state.temps, ts);
  if (!t) return null;
  const h = nearestPoint(state.hums, t.ts);
  const target = stepPointAt(state.targets, t.ts, state.bucket);
  const valve = stepPointAt(state.valves, t.ts, state.bucket);
  return {
    ts: t.ts,
    temp: t.value,
    hum: h && Math.abs(h.ts - t.ts) <= state.bucket * 1.5 ? h.value : null,
    target: target ? target.value : null,
    valveOpen: valve ? valve.value >= 0.5 : null,
    hasValve: state.hasValve,
    hyst: state.hyst,
    range: state.range,
  };
}

// The line above the chart: the latest values by default, the values
// under the pointer while hovering/touching - never a tooltip covering
// the very lines being read.
function renderClimateReadout(id, reading, isLatest) {
  const el = document.getElementById(`readout-${id}`);
  if (!el) return;
  if (!reading) { el.innerHTML = ""; return; }
  const when = isLatest
    ? tr("climate.readout_latest", { time: formatTime(reading.ts, "24h") })
    : formatTime(reading.ts, reading.range === "7d" ? "7d" : "24h");
  const parts = [
    `<span class="ro-time">${when}</span>`,
    `<span class="ro-val" style="color:${TEMP_COLOR}">${reading.temp.toFixed(1)}°C</span>`,
  ];
  if (reading.hasValve) {
    parts.push(reading.target != null
      ? `<span class="ro-val ro-target">${tr("climate.target")} ${reading.target.toFixed(1)}°${reading.hyst ? ` <span class="ro-dim">±${reading.hyst}</span>` : ""}</span>`
      : `<span class="ro-dim">${tr("climate.no_target")}</span>`);
  }
  if (reading.hum != null) parts.push(`<span class="ro-val" style="color:${HUM_COLOR}">${reading.hum.toFixed(0)}%</span>`);
  if (reading.hasValve && reading.valveOpen != null) {
    parts.push(`<span class="ro-valve${reading.valveOpen ? " open" : ""}">${reading.valveOpen ? tr("climate.valve_open") : tr("climate.valve_closed")}</span>`);
  }
  el.innerHTML = parts.join("");
}

// ----------------------------------------------------------------- tabs ----

function switchTab(tab) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.getElementById("tab-home").hidden = tab !== "home";
  document.getElementById("tab-climate").hidden = tab !== "climate";
  try { localStorage.setItem("localhome-tab", tab); } catch (e) { /* private browsing etc - just skip remembering it */ }
}

// A deliberately low-key settings panel (a gear icon, not a prominent
// control on the main tab) for anything that shouldn't be one accidental
// tap away - right now just the heat/cool season switch, which stays in
// effect until someone opens this and changes it again (persisted to
// schedules.json - see ScheduleStore in services/thermostat.py - so it
// survives a restart/power cut, not just a page reload).
function openSettings() {
  document.getElementById("settings-modal").hidden = false;
}

function closeSettings() {
  document.getElementById("settings-modal").hidden = true;
}

// Same "season" framing the settings modal's toggle already uses
// ("Stagione riscaldamento / raffrescamento") - a snowflake+"Inverno" or
// sun+"Estate" reads as the current state at a glance, instead of a
// static "Heating & Cooling" label that named the tab's topic but never
// said which of the two was actually running.
const SEASON_ICON_WINTER = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20"></path><path d="M3.34 7 20.66 17"></path><path d="M20.66 7 3.34 17"></path></svg>`;
const SEASON_ICON_SUMMER = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4"></path><path d="M12 18v4"></path><path d="m4.93 4.93 2.83 2.83"></path><path d="m16.24 16.24 2.83 2.83"></path><path d="M2 12h4"></path><path d="M18 12h4"></path><path d="m4.93 19.07 2.83-2.83"></path><path d="m16.24 7.76 2.83-2.83"></path></svg>`;

function updateSeasonHeading() {
  const icon = document.getElementById("climate-season-icon");
  const label = document.getElementById("climate-season-label");
  if (!icon || !label) return;
  const isSummer = climateMode === "cool";
  icon.innerHTML = isSummer ? SEASON_ICON_SUMMER : SEASON_ICON_WINTER;
  label.textContent = isSummer ? tr("climate.season_summer") : tr("climate.season_winter");
}

// "Away" is a temporary hold on top of the weekly schedule - see
// get_away_until() in services/thermostat.py for why it's not just
// painting every hour off for a day. climateAwayUntil is null (not away)
// or an ISO string (guaranteed still in the future - the backend clears
// it itself once it's passed, see ThermostatController.tick()).
let climateAwayUntil = null;

function openAway() {
  updateAwayStatusText();
  document.getElementById("away-modal").hidden = false;
}

function closeAway() {
  document.getElementById("away-modal").hidden = true;
}

function updateAwayBadge() {
  const badge = document.getElementById("climate-away-badge");
  if (!badge) return;
  badge.textContent = tr("climate.away_button");
  badge.className = `mode-badge away-badge${climateAwayUntil ? " active" : ""}`;
}

function formatAwayUntil(iso) {
  return new Date(iso).toLocaleString(LOCALE, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function updateAwayStatusText() {
  const el = document.getElementById("away-status");
  const cancelBtn = document.getElementById("away-cancel-btn");
  if (!el) return;
  if (climateAwayUntil) {
    el.textContent = tr("climate.away_active", { until: formatAwayUntil(climateAwayUntil) });
    if (cancelBtn) cancelBtn.hidden = false;
  } else {
    el.textContent = tr("climate.away_idle");
    if (cancelBtn) cancelBtn.hidden = true;
  }
}

// Patches just the banner atop the zone cards instead of going through
// renderClimateZones()'s full rebuild - away starting/ending never adds or
// removes a zone card (unlike a mode switch), so a full rebuild would just
// be extra work (and would need every schedule grid reloaded again too).
function updateAwayBanner() {
  const container = document.getElementById("climate-zones");
  if (!container) return;
  let banner = container.querySelector(".away-banner");
  if (climateAwayUntil) {
    if (!banner) {
      banner = document.createElement("div");
      banner.className = "away-banner";
      container.prepend(banner);
    }
    banner.innerHTML = `${tr("climate.away_active", { until: formatAwayUntil(climateAwayUntil) })} <button class="range-btn" onclick="cancelAway()">${tr("climate.away_return_now")}</button>`;
  } else if (banner) {
    banner.remove();
  }
}

async function setAway(hours) {
  try {
    const res = await fetch("/api/climate/away", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hours }),
    });
    const data = await res.json();
    if (!data.ok) {
      alert(tr("sensors.error_prefix") + data.error);
      return;
    }
    climateAwayUntil = data.away_until;
    updateAwayBadge();
    updateAwayStatusText();
    closeAway();
    await refreshClimateZonesLive();
  } catch (e) {
    alert(tr("sensors.request_failed"));
  }
}

async function cancelAway() {
  try {
    const res = await fetch("/api/climate/away/cancel", { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      alert(tr("sensors.error_prefix") + data.error);
      return;
    }
    climateAwayUntil = null;
    updateAwayBadge();
    updateAwayStatusText();
    await refreshClimateZonesLive();
  } catch (e) {
    alert(tr("sensors.request_failed"));
  }
}

// ------------------------------------------------------- climate/thermostat

// Two-zone (or more) valve thermostat: each zone is an existing climate
// sensor + switch valve (already rendered as normal cards if not claimed by
// a zone - see boot()), plus a weekly per-hour setpoint schedule this tab
// edits directly. See services/thermostat.py for the control-loop side.

// Which day/hour cell "now" is - by the *server's* clock, because that's
// the one the thermostat picks each target with (services/thermostat.py).
// Highlighting by this browser's own clock instead is what hid a board
// left on UTC: the grid lit up 08:00 while 06:00 was actually being
// applied. Falls back to the browser only until the first sync lands.
function scheduleNow() {
  const server = window.ServerClock && window.ServerClock.now();
  if (server) return { dayKey: DAY_KEYS[(server.getUTCDay() + 6) % 7], hour: server.getUTCHours() };
  const local = new Date();
  return { dayKey: currentDayKey(local), hour: local.getHours() };
}

// Re-highlighting whichever schedule cell "now" points at, so it's always
// obvious which hour of which day is currently driving each zone's target -
// without this, the target number in the header was the only clue,
// disconnected from the grid that actually produced it. The clock itself
// lives in the page header (static/server_clock.js).
function updateNowHighlight() {
  const { dayKey, hour } = scheduleNow();
  document.querySelectorAll(".sched-cell.now, .sched-day-label.now, .sched-hour-axis span.now").forEach(c => c.classList.remove("now"));
  document.querySelectorAll(`.sched-day-label`).forEach((label, i) => {
    if (DAY_KEYS[i % 7] === dayKey) label.classList.add("now");
  });
  document.querySelectorAll(`.sched-cell[data-day="${dayKey}"][data-hour="${hour}"]`).forEach(c => c.classList.add("now"));
  document.querySelectorAll(`.sched-hour-axis span[data-hour="${hour}"]`).forEach(c => c.classList.add("now"));
}

const DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
// Each season paints from its own range: heating setpoints live around
// 16-24C, cooling ones around 20-27C, and offering 16/18 while cooling (or
// 27 while heating) is just noise. 19 fills the 18-20 gap in heating.
// There's still one weekly grid per zone, shared by both seasons - this
// only changes what you can paint with and how it's colored.
const SCHEDULE_PRESETS = {
  heat: [null, 16, 18, 19, 20, 21, 22, 24],
  cool: [null, 20, 21, 22, 23, 24, 25, 26, 27],
};
const DEFAULT_PAINT_VALUE = { heat: 21, cool: 24 };

let climateZones = [];
let climateMode = "heat";
let climateHysteresis = null; // thermostat-wide, from /api/climate/zones - drawn as the band around the target
const scheduleWeeks = {};   // zone name -> working copy of its week grid
const saveTimers = {};      // zone name -> debounce timer for schedule saves
let paintValue = DEFAULT_PAINT_VALUE.heat;
let isPainting = false;

function schedulePresets() {
  return SCHEDULE_PRESETS[climateMode] || SCHEDULE_PRESETS.heat;
}

// Keeps the selected brush valid across a season switch - a heating 18
// isn't on the cooling palette, and painting with an invisible value
// would be confusing.
function ensurePaintValueForMode() {
  if (!schedulePresets().includes(paintValue)) paintValue = DEFAULT_PAINT_VALUE[climateMode] ?? DEFAULT_PAINT_VALUE.heat;
}

// A continuous hue ramp put 4 of the 6 preset values (18/20/21/22) all in
// the same green band, hard to tell apart at a glance - see user feedback.
// These stops instead give each preset its own clearly-named color;
// values in between (e.g. old data outside the current presets)
// interpolate between the nearest two stops. Cooling gets its own stops:
// on the heating scale 24-27 would all come out the same red. Both run
// cold-colored to warm-colored, so a lower setpoint always reads "colder".
const TEMP_COLOR_STOPS = {
  heat: [
    { v: 16, h: 212, s: 70, l: 50 },
    { v: 18, h: 184, s: 65, l: 42 },
    { v: 19, h: 162, s: 58, l: 40 },
    { v: 20, h: 140, s: 55, l: 42 },
    { v: 21, h: 48, s: 75, l: 48 },
    { v: 22, h: 28, s: 80, l: 50 },
    { v: 24, h: 4, s: 70, l: 52 },
  ],
  cool: [
    { v: 20, h: 228, s: 65, l: 52 },
    { v: 21, h: 208, s: 70, l: 48 },
    { v: 22, h: 190, s: 68, l: 42 },
    { v: 23, h: 168, s: 55, l: 40 },
    { v: 24, h: 140, s: 55, l: 42 },
    { v: 25, h: 48, s: 75, l: 48 },
    { v: 26, h: 28, s: 80, l: 50 },
    { v: 27, h: 4, s: 70, l: 52 },
  ],
};
function tempColor(value) {
  const stops = TEMP_COLOR_STOPS[climateMode] || TEMP_COLOR_STOPS.heat;
  if (value <= stops[0].v) return `hsl(${stops[0].h}, ${stops[0].s}%, ${stops[0].l}%)`;
  for (let i = 0; i < stops.length - 1; i++) {
    const a = stops[i], b = stops[i + 1];
    if (value <= b.v) {
      const t = (value - a.v) / (b.v - a.v);
      return `hsl(${a.h + (b.h - a.h) * t}, ${a.s + (b.s - a.s) * t}%, ${a.l + (b.l - a.l) * t}%)`;
    }
  }
  const last = stops[stops.length - 1];
  return `hsl(${last.h}, ${last.s}%, ${last.l}%)`;
}

// One <span> per hour (plus a leading spacer matching the grid's day-label
// column), so the axis shares the exact same 25-column grid as .sched-grid
// below it and each label sits precisely above its own hour - a handful of
// labels spaced with plain flexbox justify-content:space-between would NOT
// line up correctly here, since space-between divides N items into N-1
// equal gaps regardless of which hour each one is meant to represent. Every
// span carries data-hour (so updateNowHighlight() can bold the current
// one), but only every 3rd hour gets a visible number, to keep the row
// readable instead of cramming in 24 tiny digits.
function hourAxisHtml() {
  let cells = `<span></span>`;
  for (let h = 0; h < 24; h++) {
    cells += `<span data-hour="${h}">${h % 3 === 0 ? h : ""}</span>`;
  }
  return cells;
}

// A zone's sensor can be stale (every MQTT topic past
// stale_after_seconds - see drivers/mqtt/sensor.py's read()) while
// still having a last known reading attached, rather than genuinely
// unreachable with nothing to show at all - most commonly a battery
// sensor (Shelly H&T) between wake cycles, not broken hardware.
function zoneHasLastKnownValue(zone) {
  return !zone.sensor_ok && zone.temperature_c != null;
}

// What the sensor slot shows when the reading itself is broken: the
// last known value's age if there is one (see zoneHasLastKnownValue()
// above), else the driver's own error string (e.g. a Tuya/Meross/Tapo
// network failure, since those poll the device directly and already
// surface a real error), falling back to the generic "unreachable"
// label if a driver didn't set one.
function zoneFreshnessText(zone) {
  if (zone.sensor_ok) return formatUpdatedAgo(zone.sensor_seconds_since_update);
  if (zoneHasLastKnownValue(zone)) return staleValueNote(zone.sensor_last_value_age_seconds);
  return zone.sensor_error || tr("climate.unreachable");
}

// Only a battery-powered sensor (e.g. a Shelly H&T) reports battery_pct
// at all - most zones' sensors are mains-powered or simulated and never
// will, so the element stays hidden rather than showing a permanent
// "no battery" placeholder.
const LOW_BATTERY_PCT = 10;

function batteryLabel(pct) {
  return pct == null ? "" : `${tr("fields.battery_pct")} ${Math.round(pct)}%`;
}

// One popup per low-battery *episode*, not one per poll - a battery
// sitting at 8% for weeks would otherwise nag every 5s forever. A flag
// in localStorage remembers "already told you about this one", cleared
// the moment the reading goes back above the threshold (a fresh battery
// swap, or the sensor just recovering) so a *future* drop pops up again.
function checkLowBatteryPopup(zone) {
  if (zone.battery_pct == null) return;
  const key = `localhome-low-battery-notified-${zone.name}`;
  if (zone.battery_pct <= LOW_BATTERY_PCT) {
    try {
      if (localStorage.getItem(key)) return;
      localStorage.setItem(key, "1");
    } catch (e) { /* private browsing etc - show it every time instead of never */ }
    openLowBattery(tr("climate.low_battery_message", {
      name: zone.name, threshold: LOW_BATTERY_PCT, value: Math.round(zone.battery_pct),
    }));
  } else {
    try { localStorage.removeItem(key); } catch (e) { /* fine, nothing to clean up */ }
  }
}

// Only meaningful while cooling, and only for a zone the thermostat
// actually cools (a cool_enabled:false zone like a condensation-prone
// bathroom radiant panel is never chased toward a cooling target, so
// its own dew point isn't an actionable number here - see the
// cool_enabled note in services/thermostat.py). dew_point_c itself is
// computed server-side (services/dewpoint.py) from that zone's own
// temperature/humidity - see web/app.py's /api/climate/zones.
function dewPointVisible(zone) {
  return climateMode === "cool" && zone.cool_enabled !== false && zone.dew_point_c != null;
}

function dewPointLabel(zone) {
  return zone.dew_point_c == null ? "" : tr("climate.dew_point", { value: zone.dew_point_c.toFixed(1) });
}

// Standard WiFi RSSI (dBm) quality bands - helps tell "sensor is just
// slow" apart from "sensor can barely reach the router", which matters
// once a battery device stops reporting for way longer than its own
// wakeup_period ceiling (see docs/integrations/mqtt.md).
function wifiQuality(dbm) {
  if (dbm >= -60) return "good";
  if (dbm >= -75) return "fair";
  return "poor";
}

function wifiLabel(zone) {
  if (zone.wifi_rssi_dbm == null) return "";
  return tr(`climate.wifi_${wifiQuality(zone.wifi_rssi_dbm)}`, { value: Math.round(zone.wifi_rssi_dbm) });
}

function openLowBattery(message) {
  document.getElementById("low-battery-message").textContent = message;
  document.getElementById("low-battery-modal").hidden = false;
}

function closeLowBattery() {
  document.getElementById("low-battery-modal").hidden = true;
}

function buildZoneCard(zone) {
  const id = slug(zone.name);
  const chartId = slug(zone.sensor);
  const currentText = (zone.sensor_ok || zoneHasLastKnownValue(zone)) && zone.temperature_c != null
    ? `${zone.temperature_c.toFixed(1)}°C${zone.humidity_pct != null ? " · " + zone.humidity_pct.toFixed(0) + "%" : ""}`
    : tr("climate.unreachable");
  return `
    <div class="zone-card" id="zone-${id}">
      <div class="zone-head">
        <div class="sensor-title"><span class="dot ${zone.valve_on ? "on" : ""}"></span><span class="zone-name">${zone.name}</span></div>
        <div>
          <div class="zone-current">${currentText}</div>
          <div class="zone-updated" id="zone-updated-${id}" title="${zone.sensor_error || ""}">${zoneFreshnessText(zone)}</div>
          <div class="zone-battery${zone.battery_pct != null && zone.battery_pct <= LOW_BATTERY_PCT ? " low" : ""}" id="zone-battery-${id}"${zone.battery_pct == null ? " hidden" : ""}>${batteryLabel(zone.battery_pct)}</div>
          <div class="zone-wifi ${zone.wifi_rssi_dbm != null ? wifiQuality(zone.wifi_rssi_dbm) : ""}" id="zone-wifi-${id}"${zone.wifi_rssi_dbm == null ? " hidden" : ""}>${wifiLabel(zone)}</div>
          <div class="zone-dewpoint" id="zone-dewpoint-${id}"${dewPointVisible(zone) ? "" : " hidden"}>${dewPointLabel(zone)}</div>
        </div>
      </div>
      <div class="zone-sub state-${zoneStatusClass(zone)}">${zoneStatusText(zone)}</div>

      <div class="sched-season-label">${tr(`climate.schedule_heading_${climateMode}`)}</div>
      <div class="sched-palette">
        ${schedulePresets().map(v => `
          <button class="sched-swatch${v === paintValue ? " active" : ""}" data-value="${v == null ? "" : v}"
                  style="${v == null ? "" : `background:${tempColor(v)}`}" onclick="selectPaintValue(this)">${v == null ? "✕" : v}</button>
        `).join("")}
      </div>
      <p class="hint-text">${tr("climate.schedule_hint")}</p>
      <p class="sched-empty-hint" id="sched-empty-${id}" hidden>${tr(`climate.schedule_empty_${climateMode}`)}</p>

      <div class="sched-hour-axis-wrap">
        <div class="sched-hour-axis">${hourAxisHtml()}</div>
        <span class="sched-hour-axis-end">24h</span>
      </div>
      <div class="sched-grid" id="sched-${id}"></div>

      <div class="zone-actions">
        <button class="range-btn" onclick="copyMondayToWeek('${zone.name}', '${id}', true)">${tr("climate.copy_weekdays")}</button>
        <button class="range-btn" onclick="copyMondayToWeek('${zone.name}', '${id}', false)">${tr("climate.copy_monday")}</button>
      </div>

      <div class="section-head" style="margin:18px 0 6px">
        <h2 style="font-size:0.85rem">${tr("climate.on_hours_heading")}</h2>
      </div>
      <div class="metric-grid" id="on-hours-${id}">
        <div class="metric"><div class="value">--</div><div class="label">${tr("climate.on_hours_today")}</div></div>
        <div class="metric"><div class="value">--</div><div class="label">${tr("climate.on_hours_week")}</div></div>
        <div class="metric"><div class="value">--</div><div class="label">${tr("climate.on_hours_month")}</div></div>
      </div>

      <div class="chart-filters">
        <button class="range-btn" data-sensor="${chartId}" data-range="6h">6h</button>
        <button class="range-btn active" data-sensor="${chartId}" data-range="24h">24h</button>
        <button class="range-btn" data-sensor="${chartId}" data-range="7d">7d</button>
      </div>
      <div class="chart-readout" id="readout-${chartId}"></div>
      <div class="chart-wrap">
        <svg id="chart-${chartId}" viewBox="0 0 400 200" preserveAspectRatio="none"></svg>
      </div>
      <div class="chart-legend">
        <span><span class="legend-dot" style="background:#ff9f0a"></span>${tr("fields.temperature_c")}</span>
        <span><span class="legend-target"></span>${tr("climate.target_line")}</span>
        <span><span class="legend-band legend-valve"></span>${tr("climate.valve_on_band")}</span>
        <span><span class="legend-open-dot"></span>${tr("climate.valve_opened_marker")}</span>
        <span><span class="legend-dot" style="background:#3987e5"></span>${tr("fields.humidity_pct")}</span>
      </div>
    </div>
  `;
}

function renderClimateZones() {
  ensurePaintValueForMode();
  const container = document.getElementById("climate-zones");
  const toggleLabel = document.querySelector(".mode-toggle");
  if (!climateZones.length) {
    container.innerHTML = `<p class="hint-text">${tr("climate.no_zones")}</p>`;
    if (toggleLabel) toggleLabel.style.display = "none";
    return;
  }
  if (toggleLabel) toggleLabel.style.display = "";

  // A zone with cool_enabled:false (e.g. a bathroom a radiant-ceiling
  // system can't safely cool - see docs/configuration.md) has nothing to
  // show or schedule while cooling: the thermostat never acts on it in
  // that mode, so its card would just be a schedule editor with no
  // effect. Still shown normally in heat mode.
  const visibleZones = climateZones.filter(z => z.cool_enabled !== false || climateMode !== "cool");
  const excludedCount = climateZones.length - visibleZones.length;

  container.innerHTML =
    (excludedCount > 0 ? `<p class="hint-text">${tr("climate.cooling_excluded_note", { count: excludedCount })}</p>` : "") +
    visibleZones.map(buildZoneCard).join("");
  updateAwayBanner();

  // Each zone's temperature/humidity trend reuses the exact same chart
  // code as a regular `climate` sensor card (loadChart/drawClimateChart) -
  // just pointed at the zone's underlying sensor name instead of a card
  // built by buildSensorCard(), via this small stand-in object.
  container.querySelectorAll(".range-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const chartId = btn.dataset.sensor;
      container.querySelectorAll(`.range-btn[data-sensor="${chartId}"]`).forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      chartRanges[chartId] = btn.dataset.range;
      const zone = climateZones.find(z => slug(z.sensor) === chartId);
      if (zone) loadChart({ name: zone.sensor, kind: "climate", valve: zone.valve });
    });
  });

  visibleZones.forEach(zone => {
    chartRanges[slug(zone.sensor)] = "24h";
    loadChart({ name: zone.sensor, kind: "climate", valve: zone.valve });
  });
}

function applyCellStyle(cell, value) {
  cell.style.background = value == null ? "" : tempColor(value);
  cell.classList.toggle("off", value == null);
  cell.title = value == null ? tr("climate.target_off") : `${value}°C`;
  cell.dataset.value = value == null ? "" : value;
}

function currentDayKey(date) {
  return DAY_KEYS[(date.getDay() + 6) % 7]; // JS: 0=Sunday; DAY_KEYS: 0=Monday
}

function buildScheduleGrid(zoneId, week) {
  const grid = document.getElementById(`sched-${zoneId}`);
  if (!grid) return;
  grid.innerHTML = "";
  const dayLabels = tr("climate.days");
  const { dayKey: nowDayKey, hour: nowHour } = scheduleNow();

  DAY_KEYS.forEach((dayKey, d) => {
    const label = document.createElement("span");
    label.className = "sched-day-label" + (dayKey === nowDayKey ? " now" : "");
    label.textContent = dayLabels[d] || dayKey;
    grid.appendChild(label);

    for (let h = 0; h < 24; h++) {
      const cell = document.createElement("div");
      cell.className = "sched-cell" + (dayKey === nowDayKey && h === nowHour ? " now" : "");
      cell.dataset.day = dayKey;
      cell.dataset.hour = String(h);
      applyCellStyle(cell, week[dayKey][h]);
      cell.addEventListener("pointerdown", e => {
        e.preventDefault();
        isPainting = true;
        paintCell(zoneId, cell);
      });
      cell.addEventListener("pointerenter", () => {
        if (isPainting) paintCell(zoneId, cell);
      });
      grid.appendChild(cell);
    }
  });
}

document.addEventListener("pointerup", () => { isPainting = false; });

function paintCell(zoneId, cell) {
  const zone = climateZones.find(z => slug(z.name) === zoneId);
  const week = zone && scheduleWeeks[zone.name];
  if (!week) return;
  applyCellStyle(cell, paintValue);
  week[cell.dataset.day][Number(cell.dataset.hour)] = paintValue;
  scheduleSave(zone.name);
}

function selectPaintValue(btn) {
  paintValue = btn.dataset.value === "" ? null : Number(btn.dataset.value);
  document.querySelectorAll(".sched-swatch").forEach(b => b.classList.toggle("active", b.dataset.value === btn.dataset.value));
}

function copyMondayToWeek(zoneName, zoneId, weekdaysOnly) {
  const week = scheduleWeeks[zoneName];
  if (!week) return;
  const monday = week.mon.slice();
  const targets = weekdaysOnly ? ["tue", "wed", "thu", "fri"] : ["tue", "wed", "thu", "fri", "sat", "sun"];
  targets.forEach(day => { week[day] = monday.slice(); });
  buildScheduleGrid(zoneId, week);
  scheduleSave(zoneName);
}

// Which season each loaded grid belongs to (heating and cooling are
// separate grids - see services/thermostat.py's SEASON_KEYS). Saves go to
// the season the grid was *loaded* for, never "whatever the season is by
// the time the debounced save fires": a season switch in between would
// otherwise file a winter grid as the summer one.
const scheduleWeekModes = {};

function updateEmptyScheduleHint(zoneName) {
  const hint = document.getElementById(`sched-empty-${slug(zoneName)}`);
  const week = scheduleWeeks[zoneName];
  if (hint && week) hint.hidden = Object.values(week).some(hours => hours.some(v => v != null));
}

function scheduleSave(zoneName) {
  updateEmptyScheduleHint(zoneName);
  clearTimeout(saveTimers[zoneName]);
  saveTimers[zoneName] = setTimeout(() => saveSchedule(zoneName), 600);
}

async function saveSchedule(zoneName) {
  delete saveTimers[zoneName];
  try {
    const res = await fetch(`/api/climate/zones/${encodeURIComponent(zoneName)}/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ week: scheduleWeeks[zoneName], mode: scheduleWeekModes[zoneName] || climateMode }),
    });
    const data = await res.json();
    if (!data.ok) alert(`${tr("climate.save_failed")}: ${data.error}`);
  } catch (e) {
    alert(tr("climate.save_failed"));
  }
}

// Saves any edit still waiting on its debounce right now, before a reload
// replaces the working copy it would have saved.
async function flushPendingScheduleSaves() {
  const pending = Object.keys(saveTimers);
  pending.forEach(zoneName => clearTimeout(saveTimers[zoneName]));
  await Promise.all(pending.map(saveSchedule));
}

async function loadZoneSchedule(zoneName) {
  try {
    const res = await fetch(`/api/climate/zones/${encodeURIComponent(zoneName)}/schedule?mode=${encodeURIComponent(climateMode)}`);
    const data = await res.json();
    if (!data.ok) return;
    scheduleWeeks[zoneName] = data.week;
    scheduleWeekModes[zoneName] = data.mode || climateMode;
    buildScheduleGrid(slug(zoneName), data.week);
    updateEmptyScheduleHint(zoneName);
  } catch (e) { /* grid stays empty; next tab visit retries */ }
}

async function refreshOnHours(zoneName) {
  try {
    const res = await fetch(`/api/climate/zones/${encodeURIComponent(zoneName)}/on-hours`);
    const data = await res.json();
    const el = document.getElementById(`on-hours-${slug(zoneName)}`);
    if (!el || !data.ok) return;
    const values = el.querySelectorAll(".value");
    const unit = tr("climate.hours_unit");
    values[0].textContent = `${data.today.toFixed(1)}${unit}`;
    values[1].textContent = `${data.week.toFixed(1)}${unit}`;
    values[2].textContent = `${data.month.toFixed(1)}${unit}`;
  } catch (e) { /* leave dashes */ }
}

// One place computing the zone status line, so the initial render
// (buildZoneCard) and the 5s live refresh (refreshClimateZonesLive) can
// never drift out of sync with each other.
//
// Deliberately mode-agnostic: there is one valve, with one state (open/
// closed) - the heat/cool mode toggle at the top of the tab is what
// changes *when* it opens (see services/thermostat.py), not the valve
// itself. Wording this as "heating active"/"cooling active" per zone was
// tried and reverted - it made one physical valve look like two different
// subsystems, which isn't the mental model this is supposed to match.
// Three distinct states, not two - "valve closed" alone doesn't say
// whether that's because there's no target this hour (off) or because
// the target was reached and it's satisfied. Conflating those was
// exactly the ambiguity asked to be fixed here.
function zoneStatusClass(zone) {
  if (climateAwayUntil) return "off";
  if (zone.target_c == null) return "off";
  return zone.valve_on ? "active" : "reached";
}

function zoneStatusText(zone) {
  if (climateAwayUntil) return tr("climate.status_away");
  if (zone.target_c == null) return tr("climate.no_target");
  const target = zone.target_c.toFixed(0) + "°C";
  return zone.valve_on ? tr("climate.status_active", { target }) : tr("climate.status_reached", { target });
}

// A device tagged `visible_in_mode: "heat"/"cool"` in config.yaml (e.g. a
// dehumidifier that only makes sense while cooling) hides its card outside
// that mode. Re-run whenever climateMode changes - on boot, after toggling
// it, and on every live refresh in case it changed from elsewhere.
function applyClimateModeVisibility() {
  document.querySelectorAll("#climate-extra .sensor-card").forEach(card => {
    const requiredMode = card.dataset.visibleInMode;
    card.hidden = !!requiredMode && requiredMode !== climateMode;
  });
}

async function refreshClimateZonesLive() {
  try {
    const res = await fetch("/api/climate/zones");
    const data = await res.json();
    if (!data.ok) return;
    const previousMode = climateMode;
    climateMode = data.mode || climateMode;
    if (data.hysteresis_c != null) climateHysteresis = data.hysteresis_c;
    // Season switched from somewhere else (another phone, /climate-admin):
    // the palette, grid colors and even which zone cards exist all depend
    // on it, so rebuild instead of patching values into stale cards.
    if (climateMode !== previousMode) {
      await rerenderClimateForSeason(data);
      return;
    }
    climateAwayUntil = data.away_until || null;
    // A zone's underlying sensor/valve driver *name* can change without
    // this page reloading - a climate-admin save+restart while this tab
    // was already open (e.g. switching a zone to a manual/simulated
    // source) renames "Zone Temperature"-style config.yaml drivers to
    // "Zone Sensor"/"Zone Valve". Keep this in sync every tick, not just
    // read once at page load, so the chart refresh below (and anything
    // else keyed off zone.sensor/zone.valve) doesn't keep polling a name
    // that stopped existing the moment the process restarted.
    climateZones = data.zones;
    updateSeasonHeading();
    updateAwayBadge();
    updateAwayBanner();
    data.zones.forEach(zone => {
      checkLowBatteryPopup(zone);
      const card = document.getElementById(`zone-${slug(zone.name)}`);
      if (!card) return;
      card.querySelector(".dot").classList.toggle("on", zone.valve_on);
      card.querySelector(".zone-current").textContent = (zone.sensor_ok || zoneHasLastKnownValue(zone)) && zone.temperature_c != null
        ? `${zone.temperature_c.toFixed(1)}°C${zone.humidity_pct != null ? " · " + zone.humidity_pct.toFixed(0) + "%" : ""}`
        : tr("climate.unreachable");
      const updated = card.querySelector(".zone-updated");
      if (updated) {
        updated.textContent = zoneFreshnessText(zone);
        updated.title = zone.sensor_error || "";
      }
      const battery = card.querySelector(".zone-battery");
      if (battery) {
        battery.hidden = zone.battery_pct == null;
        battery.textContent = batteryLabel(zone.battery_pct);
        battery.classList.toggle("low", zone.battery_pct != null && zone.battery_pct <= LOW_BATTERY_PCT);
      }
      const wifi = card.querySelector(".zone-wifi");
      if (wifi) {
        wifi.hidden = zone.wifi_rssi_dbm == null;
        wifi.textContent = wifiLabel(zone);
        wifi.className = `zone-wifi ${zone.wifi_rssi_dbm != null ? wifiQuality(zone.wifi_rssi_dbm) : ""}`;
      }
      const dewpoint = card.querySelector(".zone-dewpoint");
      if (dewpoint) {
        dewpoint.hidden = !dewPointVisible(zone);
        dewpoint.textContent = dewPointLabel(zone);
      }
      const sub = card.querySelector(".zone-sub");
      sub.textContent = zoneStatusText(zone);
      sub.className = `zone-sub state-${zoneStatusClass(zone)}`;
    });
    applyClimateModeVisibility();
  } catch (e) { /* keep last render */ }
}

// Everything that depends on the season, rebuilt together: heading and
// toggle, which zone cards exist, and each card's palette and grid colors
// (both season-specific - see SCHEDULE_PRESETS / TEMP_COLOR_STOPS).
async function rerenderClimateForSeason(zonesData) {
  await flushPendingScheduleSaves();
  updateSeasonHeading();
  applyClimateModeVisibility();
  const toggle = document.getElementById("climate-mode-toggle");
  if (toggle) toggle.checked = climateMode === "cool";
  climateAwayUntil = zonesData.away_until || null;
  updateAwayBadge();
  climateZones = zonesData.zones;
  renderClimateZones();
  for (const zone of climateZones) {
    await loadZoneSchedule(zone.name);
    refreshOnHours(zone.name);
  }
  updateNowHighlight();
}

async function onModeToggle(el) {
  const mode = el.checked ? "cool" : "heat";
  try {
    const res = await fetch("/api/climate/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await res.json();
    if (!data.ok) {
      alert(tr("sensors.error_prefix") + data.error);
      el.checked = !el.checked;
      return;
    }
    climateMode = mode;
    // A full reload, not just refreshClimateZonesLive()'s in-place patch:
    // switching mode can change *which* zone cards should even exist (a
    // cool_enabled:false zone's card only appears in heat mode), which a
    // live-patch of already-rendered cards can't add or remove.
    const zonesRes = await fetch("/api/climate/zones").then(r => r.json()).catch(() => null);
    if (zonesRes && zonesRes.ok) {
      await rerenderClimateForSeason(zonesRes);
    } else {
      updateSeasonHeading();
      applyClimateModeVisibility();
    }
  } catch (e) {
    alert(tr("sensors.request_failed"));
    el.checked = !el.checked;
  }
}

// ---------------------------------------------------------------- boot -----

async function boot() {
  try {
    const savedTab = localStorage.getItem("localhome-tab");
    if (savedTab) switchTab(savedTab);
  } catch (e) { /* fine, defaults to the Home tab */ }

  renderCovers(window.COVER_NAMES);
  refreshCovers(window.COVER_NAMES);
  setInterval(() => refreshCovers(window.COVER_NAMES), 8000);

  const [devicesData, climateData] = await Promise.all([
    fetch("/api/devices").then(r => r.json()),
    fetch("/api/climate/zones").then(r => r.json()).catch(() => ({ ok: false, zones: [] })),
  ]);

  // A zone's sensor/valve get their own purpose-built card in the Climate
  // tab (temperature + schedule + on-hours), so they're excluded from the
  // generic per-kind grids entirely rather than also showing up there.
  const zoneMemberNames = new Set();
  if (climateData.ok) climateData.zones.forEach(z => { zoneMemberNames.add(z.sensor); zoneMemberNames.add(z.valve); });

  attachPairings(devicesData.sensors);
  const homeSensors = devicesData.sensors.filter(s => s.tab !== "climate" && !zoneMemberNames.has(s.name));
  const climateExtraSensors = devicesData.sensors.filter(s => s.tab === "climate" && !zoneMemberNames.has(s.name));

  renderSensors(homeSensors, "sensor-cards");
  renderSensors(climateExtraSensors, "climate-extra");
  const pollableSensors = homeSensors.concat(climateExtraSensors);
  setInterval(() => pollableSensors.forEach(refreshSensor), 5000);
  setInterval(() => pollableSensors.forEach(loadChart), 60000);

  if (climateData.ok) {
    climateMode = climateData.mode || "heat";
    if (climateData.hysteresis_c != null) climateHysteresis = climateData.hysteresis_c;
    climateAwayUntil = climateData.away_until || null;
    updateSeasonHeading();
    updateAwayBadge();
    climateZones = climateData.zones;
    renderClimateZones();
    const toggle = document.getElementById("climate-mode-toggle");
    if (toggle) toggle.checked = climateMode === "cool";
    for (const zone of climateZones) {
      await loadZoneSchedule(zone.name);
      refreshOnHours(zone.name);
    }
    updateNowHighlight();
    setInterval(refreshClimateZonesLive, 5000);
    setInterval(() => climateZones.forEach(z => refreshOnHours(z.name)), 30000);
    setInterval(() => climateZones.forEach(z => loadChart({ name: z.sensor, kind: "climate", valve: z.valve })), 60000);
    // On the server clock's own minute boundary, so the highlight moves to
    // the next hour's cell exactly when the thermostat does - plus a slow
    // fallback in case the clock never syncs at all.
    document.addEventListener("serverclock:minute", updateNowHighlight);
    setInterval(updateNowHighlight, 30000);
  }
  applyClimateModeVisibility();
}

boot();
