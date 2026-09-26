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
  if (vars) for (const k in vars) str = str.split(`{${k}}`).join(vars[k]);
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

// Is the board still answering? Every periodic refresh reports in here.
// The page keeps its last values when the board goes away - without this
// banner a frozen dashboard looks live, which is the one thing a
// dashboard must never do.
const connection = { lastOk: Date.now(), failures: 0 };
const OFFLINE_AFTER_MS = 15000;

function markOnline() {
  connection.lastOk = Date.now();
  if (connection.failures) {
    connection.failures = 0;
    updateConnectionBanner();
  }
}

function markOffline() {
  connection.failures += 1;
  updateConnectionBanner();
}

function updateConnectionBanner() {
  const banner = document.getElementById("connection-banner");
  if (!banner) return;
  // Two failures in a row, not one: a single dropped request while the
  // board reboots after an update shouldn't flash a warning.
  const offline = connection.failures >= 2 && Date.now() - connection.lastOk > OFFLINE_AFTER_MS;
  banner.hidden = !offline;
  document.body.classList.toggle("is-offline", offline);
  if (offline) {
    banner.textContent = tr("app.offline", {
      time: new Date(connection.lastOk).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }),
    });
  }
}

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
// A reading older than the board's own boot was never missed: nobody was
// listening. After a power cut a battery sensor takes until its next
// wake-up (up to a couple of hours) to be heard, and calling that "no
// data for 53h" reads as a broken sensor when nothing is wrong.
function predatesBoot(ageSeconds) {
  if (ageSeconds == null) return false;
  const up = (window.ServerClock && window.ServerClock.upFor()) || {};
  const since = up.board != null ? up.board : up.process;
  return since != null && ageSeconds > since + 60;
}

function waitingSinceBootNote() {
  const up = (window.ServerClock && window.ServerClock.upFor()) || {};
  const since = up.board != null ? up.board : up.process;
  return tr("sensors.waiting_since_boot", { age: formatAge(since) || "?" });
}

function staleValueNote(ageSeconds) {
  const relative = formatAge(ageSeconds);
  if (predatesBoot(ageSeconds)) return waitingSinceBootNote();
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
        <h2>${escapeHtml(name)}</h2>
        <span class="state" id="cover-state-${slug(name)}">${tr("covers.loading")}</span>
      </div>
      <div class="visual">
        <div class="cover" id="cover-fill-${slug(name)}"></div>
        <span class="pct-badge" id="cover-badge-${slug(name)}">--</span>
      </div>
      <div class="buttons">
        <button class="icon-btn open" onclick="sendCoverAction(${jsArg(name)}, 'open')" title="${tr("covers.btn_open")}">${ICON_OPEN}</button>
        <button class="icon-btn stop" onclick="sendCoverAction(${jsArg(name)}, 'stop')" title="${tr("covers.btn_stop")}">${ICON_STOP}</button>
        <button class="icon-btn close" onclick="sendCoverAction(${jsArg(name)}, 'close')" title="${tr("covers.btn_close")}">${ICON_CLOSE}</button>
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
        stateEl.className = "state";
        stateEl.title = "";
        const card = stateEl.closest(".card");
        if (card) card.classList.remove("is-faulty");
        stateEl.textContent = (COVER_STATE_LABELS[data.state] || data.state) + (data.calibrated ? "" : tr("covers.not_calibrated"));
        if (document.activeElement !== sliderEl) {
          sliderEl.value = data.percent;
          pctEl.textContent = `${data.percent}%`;
          setCoverVisual(name, data.percent);
        }
      } else {
        showCoverFault(name, data.code, data.error);
      }
    } catch (e) {
      showCoverFault(name, null, tr("covers.unreachable"));
    }
  }
}

// What a driver's error code means for the person looking at the card:
// the two a Tuya installation actually hits (drivers/tuya/client.py).
const COVER_FAULTS = { 905: "covers.fault_unreachable", 914: "covers.fault_key" };

function showCoverFault(name, code, detail) {
  const id = slug(name);
  const stateEl = document.getElementById(`cover-state-${id}`);
  const card = stateEl.closest(".card");
  stateEl.textContent = tr(COVER_FAULTS[code] || "covers.fault_generic");
  stateEl.className = "state cover-fault";
  stateEl.title = detail || "";     // the full reason, for whoever wants it
  if (card) card.classList.add("is-faulty");
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
    if (!data.ok) notify(tr("sensors.error_prefix") + apiError(data));
  } catch (e) {
    notify(tr("sensors.request_failed"));
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
      notify(tr("sensors.error_prefix") + apiError(data));
      sliderEl.disabled = false;
      refreshCovers(window.COVER_NAMES);
      return;
    }
    setTimeout(() => {
      sliderEl.disabled = false;
      refreshCovers(window.COVER_NAMES);
    }, (data.duration || 0) * 1000 + 500);
  } catch (e) {
    notify(tr("sensors.request_failed"));
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
        <button class="preset-btn" data-name="${escapeAttr(sensor.name)}" data-value="${p.value}">
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
             class="number-slider" id="number-slider-${id}" data-name="${escapeAttr(sensor.name)}">
      <span class="pct" id="number-value-${id}">-- ${range.unit}</span>
    </div>
    ${programPanelHtml(sensor)}
  `;
}

// The day/night program, on the device it drives - it belongs with the
// speed buttons, not in the installer's panel. Only offered for a device
// with speed presets (a VMC): a plain dimmer has no day/night to it.
// Every change saves by itself, like the zones' schedule grids.
function programPanelHtml(sensor) {
  if (!sensor.presets || !sensor.presets.length) return "";
  const id = slug(sensor.name);
  const period = (key, icon) => `
      <div class="vmc-period-row">
        <span class="vmc-period-name">${icon} ${tr(`climate.vmc_period_${key}`)}</span>
        <label>${tr("climate.vmc_from")}
          <input type="time" id="vmc-${key}-at-${id}" data-program="${escapeAttr(sensor.name)}">
        </label>
        <label>${tr("climate.vmc_speed_label")}
          <input type="number" min="0" max="100" step="5" id="vmc-${key}-value-${id}" data-program="${escapeAttr(sensor.name)}">
        </label>
      </div>`;
  return `
    <div class="vmc-program" id="vmc-program-${id}">
      <div class="vmc-program-head">
        <label class="vmc-program-toggle">
          <input type="checkbox" id="vmc-program-on-${id}" data-program="${escapeAttr(sensor.name)}">
          <span>${tr("climate.vmc_program_title")}</span>
        </label>
        <span class="vmc-program-state" id="vmc-program-state-${id}"></span>
      </div>
      <div class="vmc-program-fields" id="vmc-program-fields-${id}" hidden>
        ${period("day", ICON_DAY)}${period("night", ICON_NIGHT)}
      </div>
    </div>`;
}

const ICON_DAY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`;
const ICON_NIGHT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`;

// The humidity chart, below the temperature one - hidden by
// drawClimateChart() for a sensor that reports no humidity.
function humidityChartHtml(id) {
  return `
    <div class="humidity-chart" id="humchart-${id}">
      <div class="chart-title">${tr("fields.humidity_pct")}</div>
      <div class="chart-wrap">
        <svg id="chart-${id}-hum" viewBox="0 0 400 80" preserveAspectRatio="none"></svg>
      </div>
    </div>`;
}

// The fixed English names the admin panel gives its extra devices (see
// services/climate_map_store.py), shown in the page's language.
const DISPLAY_NAMES = {
  "Outdoor Sensor": "climate.outdoor_name",
  "Bathroom Bypass Valve": "climate.bypass_name",
  "Dehumidifier": "climate.dehumidifier",
};

function displayName(name) {
  return DISPLAY_NAMES[name] ? tr(DISPLAY_NAMES[name]) : name;
}

// Simulated readings say so wherever they're shown: a demo or a test
// bench must never pass for the real apartment's temperatures.
function simulatedTagHtml(simulated) {
  return simulated ? ` <span class="sim-tag">${tr("sensors.simulated_tag")}</span>` : "";
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
        <span class="sensor-title"><span class="dot" id="dot-${id}"></span><span>${escapeHtml(displayName(sensor.name))}</span>${simulatedTagHtml(sensor.simulated)}</span>
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
        ${sensor.kind === "climate" ? `<div class="chart-title">${tr("fields.temperature_c")}</div>` : ""}
        <div class="chart-wrap">
          <svg id="chart-${id}" viewBox="0 0 400 130" preserveAspectRatio="none"></svg>
          <div class="chart-tooltip" id="tooltip-${id}" hidden></div>
        </div>
        ${sensor.kind === "climate" ? humidityChartHtml(id) : ""}
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
    if (!data.ok) notify(tr("sensors.error_prefix") + apiError(data));
  } catch (e) {
    notify(tr("sensors.request_failed"));
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

  el.querySelectorAll("[data-program]").forEach(input => {
    input.addEventListener("change", () => onVmcProgramChange(input));
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
    .then(([reading, linkedReading]) => {
      markOnline();
      updateSensorCard(sensor, reading, linkedReading);
    })
    .catch(() => {
      markOffline();
      updateSensorCard(sensor, { ok: false, error: "unreachable" }, null);
    });
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
  if (sensor.name === outdoorSensorName) updateOutdoorChip(reading);
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
          <input type="checkbox" ${reading.is_on ? "checked" : ""} onchange="onSwitchToggle(${jsArg(sensor.name)}, this)">
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
  }

  metrics.innerHTML = html;
}

async function onSwitchToggle(name, el) {
  if (!el.checked) {
    const ok = await confirmAction(tr("sensors.confirm_turn_off"), {
      confirmLabel: tr("sensors.switch_off_button"),
      danger: true,
    });
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
      notify(tr("sensors.error_prefix") + apiError(data));
      el.checked = !el.checked;
    }
  } catch (e) {
    notify(tr("sensors.request_failed"));
    el.checked = !el.checked;
  }
  el.disabled = false;
}

// The ventilation unit's day/night program (services/vmc_schedule.py),
// on the card of the device it drives. Without it, a speed that changes
// by itself at 22:00 looks like the dashboard did something random.
let vmcSaveTimer = null;

async function refreshVmcProgram() {
  if (!document.querySelector(".vmc-program")) return;
  try {
    const data = await (await fetch("/api/vmc-schedule")).json();
    if (data.ok) fillVmcProgram(data);
  } catch (e) { /* leave the panel as it is */ }
}

function fillVmcProgram(data) {
  const { schedule, status } = data;
  document.querySelectorAll(".vmc-program").forEach(panel => {
    const id = panel.id.replace("vmc-program-", "");
    // The saved program drives one device; another device's card shows
    // the same controls, but off.
    const mine = schedule.target && slug(schedule.target) === id;
    const on = mine && schedule.enabled;
    const toggle = document.getElementById(`vmc-program-on-${id}`);
    if (document.activeElement !== toggle) toggle.checked = on;
    document.getElementById(`vmc-program-fields-${id}`).hidden = !on;
    for (const key of ["day", "night"]) {
      const at = document.getElementById(`vmc-${key}-at-${id}`);
      const value = document.getElementById(`vmc-${key}-value-${id}`);
      if (document.activeElement !== at) at.value = schedule[key].at;
      if (document.activeElement !== value) value.value = schedule[key].value;
    }
    document.getElementById(`vmc-program-state-${id}`).textContent = on && status.enabled
      ? tr("climate.vmc_program_running", {
          period: tr(`vmc_presets.${status.period}`).toLowerCase(),
          value: status.value,
          until: status.until,
        })
      : tr("climate.vmc_program_off");
  });
}

// Every change saves, like a zone's schedule grid: no save button to
// forget, and the speed of the period running now is applied at once.
function onVmcProgramChange(input) {
  const name = input.dataset.program;
  const id = slug(name);
  document.getElementById(`vmc-program-fields-${id}`).hidden = !document.getElementById(`vmc-program-on-${id}`).checked;
  clearTimeout(vmcSaveTimer);
  vmcSaveTimer = setTimeout(() => saveVmcProgram(name), 500);
}

async function saveVmcProgram(name) {
  const id = slug(name);
  const body = {
    enabled: document.getElementById(`vmc-program-on-${id}`).checked,
    // Turning it on from this card is what picks the device it drives.
    target: name,
    day: {
      at: document.getElementById(`vmc-day-at-${id}`).value,
      value: Number(document.getElementById(`vmc-day-value-${id}`).value),
    },
    night: {
      at: document.getElementById(`vmc-night-at-${id}`).value,
      value: Number(document.getElementById(`vmc-night-value-${id}`).value),
    },
  };
  if (body.day.at === body.night.at) {
    notify(tr("climate.vmc_same_time"));
    return refreshVmcProgram();
  }
  try {
    const res = await fetch("/api/vmc-schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      notify(tr("sensors.error_prefix") + apiError(data));
      return refreshVmcProgram();
    }
    fillVmcProgram(data);
    refreshSensor({ name });  // the speed it just applied
  } catch (e) {
    notify(tr("sensors.request_failed"));
  }
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

function drawEmpty(svg, height = CHART_H, width = CHART_W) {
  svg.innerHTML = `<foreignObject x="0" y="0" width="${width}" height="${height}">
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

// A climate sensor's history is two separate charts on the same time
// range, each with its own title and time axis - one crowded plot mixing
// degrees and percent was hard to read:
//   1. temperature, with the target it was held to (a band of +-
//      hysteresis around a solid line);
//   2. humidity, below it - context, not what drives the valve.
// For a zone, the stretches the system (its valve) was on shade both
// charts, and the list of runs above them (renderRuns) says what each one
// did. Hovering (or
// touching) either chart moves a marker on both and fills the readout
// line above them. Drawn in real pixels - one viewBox unit per CSS pixel -
// so text and lines look the same on a phone and on a wide desktop card;
// the plots get a little taller as the card gets wider.
const TEMP_CHART = { padL: 40, padR: 8, top: 8, axisH: 14 };
const HUM_CHART = { top: 8, axisH: 14 };
const CHART_TEXT_SCALE = 1.15;

// Round local-time ticks for the time axis, as many as fit (`maxLabels`):
// every 1, 2, 3, 4, 6 or 12 hours, or every midnight once the data spans
// more than two days. Sized on the data, not the range - a history
// cleared this morning still gets hourly labels on the 24h view.
function timeTicks(minTs, maxTs, maxLabels) {
  const ticks = [];
  const d = new Date(minTs * 1000);
  const spanHours = (maxTs - minTs) / 3600;
  if (spanHours > 48) {
    d.setHours(0, 0, 0, 0);
    while (d.getTime() / 1000 <= maxTs) {
      if (d.getTime() / 1000 >= minTs) ticks.push(d.getTime() / 1000);
      d.setDate(d.getDate() + 1);
    }
    return { ticks, daily: true };
  }
  const stepHours = [1, 2, 3, 4, 6, 12].find(step => spanHours / step <= maxLabels) || 12;
  d.setMinutes(0, 0, 0);
  while (d.getTime() / 1000 <= maxTs) {
    if (d.getTime() / 1000 >= minTs && d.getHours() % stepHours === 0) ticks.push(d.getTime() / 1000);
    d.setHours(d.getHours() + 1);
  }
  return { ticks, daily: false };
}

function formatTick(ts, daily) {
  const d = new Date(ts * 1000);
  return daily
    ? d.toLocaleDateString(LOCALE, { weekday: "short" })
    : d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
}

// Latest reading / reading under the pointer, drawn with every chart
// state the pointer or the refresh leaves behind.
const climateChartState = {};
// Each chart's last data, to draw it again at a new width without
// fetching it again: whenever its width changes - a window resize, a
// hidden tab, zone or pane shown, a card that grows because the one next
// to it was hidden - the observer below redraws it.
const climateChartArgs = {};

function redrawClimateChart(id) {
  if (climateChartArgs[id]) drawClimateChart(id, ...climateChartArgs[id]);
}

const chartResizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(entries => {
  const ids = [];
  for (const entry of entries) {
    const svg = entry.target;
    const width = Math.round(entry.contentRect.width);
    const drawnAt = Number((svg.getAttribute("viewBox") || "").split(" ")[2]);
    if (svg.isConnected && width && width !== drawnAt) ids.push(svg.id.replace(/^chart-/, ""));
  }
  // On the next frame, not inside the callback: redrawing changes the
  // chart's height, and resizing an observed element from its own
  // observer is what the browser reports as a "ResizeObserver loop".
  if (ids.length) requestAnimationFrame(() => ids.forEach(redrawClimateChart));
});

// Helpers bound to one SVG: add elements, write axis text (scaled back up
// on a phone, where the whole SVG is drawn smaller), draw the time grid.
function chartCanvas(svg, textScale) {
  const add = (tag, attrs) => svg.appendChild(svgEl(tag, attrs));
  const text = (x, y, content, attrs) => {
    const el = add("text", { x, y, fill: AXIS_TEXT_COLOR, ...attrs });
    el.setAttribute("font-size", (attrs && attrs["font-size"] || 9) * textScale);
    el.textContent = content;
    return el;
  };
  return { add, text };
}

function drawTimeAxis(canvas, axis, xOf, top, bottom, plotL, plotR, textScale) {
  for (const ts of axis.ticks) {
    const x = xOf(ts);
    canvas.add("line", { x1: x, x2: x, y1: top, y2: bottom, stroke: GRID_COLOR, "stroke-width": 1 });
    if (x > plotL + 14 * textScale && x < plotR - 14 * textScale) {
      canvas.text(x, bottom + 4 + 7 * textScale, formatTick(ts, axis.daily), { "text-anchor": "middle" });
    }
  }
}

// Same rule as services/history.py's MIN_OFF_SECONDS: an "off" shorter
// than this between two stretches on - a restart, one odd reading - doesn't
// split them, so the list of runs agrees with the tiles' count.
const MIN_OFF_SECONDS = 300;

// When the system was on, from the valve's bucketed is_on series: runs of
// buckets at >= 0.5 (on for most of the bucket), split by an "off" - or
// nothing recorded - longer than MIN_OFF_SECONDS (or than half a bucket,
// on the hourly 7d view). `ongoing`: it reaches the latest bucket, so it's
// still on now.
function onRuns(valves, bucket) {
  const maxStep = Math.max(bucket * 1.5, bucket + MIN_OFF_SECONDS);
  const runs = [];
  let run = null;
  for (const p of valves) {
    if (p.value < 0.5) continue;
    if (run && p.ts - run.last <= maxStep) { run.last = p.ts; continue; }
    if (run) runs.push(run);
    run = { start: p.ts, last: p.ts };
  }
  if (run) runs.push(run);
  const latest = valves.length ? valves[valves.length - 1].ts : null;
  return runs.map(r => ({ start: r.start, end: r.last + bucket, ongoing: r.last === latest }));
}

// "3h 05m", "40 min" - how long, for someone who isn't counting decimals
// of an hour.
function formatDuration(seconds) {
  const minutes = Math.max(0, Math.round(seconds / 60));
  if (minutes < 60) return tr("climate.duration_m", { m: minutes });
  const h = Math.floor(minutes / 60), m = minutes % 60;
  if (h >= 100) return tr("climate.duration_h", { h });
  return tr("climate.duration_hm", { h, m: String(m).padStart(2, "0") });
}

function drawClimateChart(id, tempPoints, humPoints, range, valvePoints, targetPoints) {
  const svg = document.getElementById(`chart-${id}`);
  if (!svg) return;
  climateChartArgs[id] = [tempPoints, humPoints, range, valvePoints, targetPoints];
  if (chartResizeObserver) chartResizeObserver.observe(svg);
  // A chart in a hidden tab, zone or pane has no width yet: the observer
  // draws it once it's shown.
  const W = Math.round(svg.getBoundingClientRect().width);
  if (!W) return;
  const humSvg = document.getElementById(`chart-${id}-hum`);
  const humWrap = document.getElementById(`humchart-${id}`);
  const hasValve = valvePoints != null;
  const textScale = CHART_TEXT_SCALE;
  const L = TEMP_CHART;
  const plotH = Math.round(Math.min(180, Math.max(110, W * 0.2)));
  const humPlotH = Math.round(Math.min(90, Math.max(52, W * 0.09)));
  const plotL = Math.round(L.padL * textScale), plotR = W - L.padR, plotW = plotR - plotL;
  const tempBottom = L.top + plotH;
  const tempAxisAt = tempBottom;
  const tempHeight = tempAxisAt + Math.round(L.axisH * textScale);
  const humBottom = HUM_CHART.top + humPlotH;
  const humHeight = humBottom + Math.round(HUM_CHART.axisH * textScale);
  svg.setAttribute("viewBox", `0 0 ${W} ${tempHeight}`);
  svg.innerHTML = "";
  if (humSvg) {
    humSvg.setAttribute("viewBox", `0 0 ${W} ${humHeight}`);
    humSvg.innerHTML = "";
  }

  const temps = tempPoints || [], hums = humPoints || [];
  if (humWrap) humWrap.hidden = hums.length < 2;  // a sensor without humidity: no empty chart
  if (temps.length < 2) {
    delete climateChartState[id];
    renderClimateReadout(id, null);
    renderRuns(id, null);
    return drawEmpty(svg, tempHeight, W);
  }

  const bucket = CHART_BUCKET_SECONDS[range] || 300;
  const hyst = climateHysteresis || 0;
  const minTs = temps[0].ts, maxTs = temps[temps.length - 1].ts;
  const targets = (targetPoints || []).filter(p => p.ts >= minTs - bucket && p.ts <= maxTs);
  const valves = valvePoints || [];
  const runs = hasValve ? onRuns(valves, bucket) : [];
  const xOf = ts => plotL + ((ts - minTs) / Math.max(1, maxTs - minTs)) * plotW;
  const clampX = x => Math.max(plotL, Math.min(plotR, x));
  const axis = timeTicks(minTs, maxTs, Math.max(3, Math.floor(plotW / (44 * textScale))));
  const stepRects = (points, draw) => points.forEach((p, i) => {
    const x1 = clampX(xOf(p.ts));
    const next = points[i + 1];
    const x2 = clampX(next && next.ts - p.ts <= bucket * 1.5 ? xOf(next.ts) : xOf(p.ts + bucket));
    draw(x1, Math.max(1, x2 - x1), p);
  });
  // One rect per stretch the system was on - never one per bucket, which
  // left hairline gaps between them.
  const runRects = (canvas, y, height, attrs) => runs.forEach(r => {
    const x1 = clampX(xOf(r.start)), x2 = clampX(xOf(r.end));
    if (x2 > x1) canvas.add("rect", { x: x1, y, width: Math.max(1, x2 - x1), height, ...attrs });
  });

  // ------------------------------------------------ 1. temperature chart --
  const t = chartCanvas(svg, textScale);
  // Whole degrees, always an even span so the middle gridline is a whole
  // degree too; it fits the target band as well, or a target far from the
  // room's reading would be clipped off.
  let [tMin, tMax] = niceBounds(temps.map(p => p.value).concat(targets.flatMap(p => [p.value - hyst, p.value + hyst])), 0.4, 1);
  if ((tMax - tMin) % 2) tMax += 1;
  const yT = v => tempBottom - ((v - tMin) / (tMax - tMin)) * plotH;

  // When the system was on, behind everything: the temperature line
  // crossing these stretches is the effect it had.
  runRects(t, L.top, plotH, { fill: VALVE_BAND_COLOR, opacity: 0.17 });
  drawTimeAxis(t, axis, xOf, L.top, tempAxisAt, plotL, plotR, textScale);
  // A gridline per degree (per two on a wide span), to read a reading
  // off the axis without hovering.
  const degreeStep = tMax - tMin > 6 ? 2 : 1;
  for (let v = tMin; v <= tMax; v += degreeStep) {
    const y = yT(v);
    t.add("line", { x1: plotL, x2: plotR, y1: y, y2: y, stroke: GRID_COLOR, "stroke-width": 1 });
    t.text(plotL - 6, y + 3 * textScale, `${v}°`, { "text-anchor": "end", fill: TEMP_COLOR });
  }

  for (const run of consecutiveRuns(targets, bucket)) {
    // Buckets with the same target merged into one step: a band drawn a
    // bucket at a time showed hairline gaps between them.
    const steps = [];
    stepRects(run, (x, w, p) => {
      const last = steps[steps.length - 1];
      if (last && last.value === p.value && Math.abs(last.x + last.w - x) < 0.5) last.w = x + w - last.x;
      else steps.push({ x, w, value: p.value });
    });
    let d = "";
    for (const { x, w, value } of steps) {
      if (hyst > 0) {
        t.add("rect", {
          x, y: yT(value + hyst), width: w, height: Math.max(1, yT(value - hyst) - yT(value + hyst)),
          fill: TARGET_COLOR, opacity: 0.16,
        });
      }
      const y = yT(value).toFixed(1);
      d += `${d ? "L" : "M"}${x.toFixed(1)},${y} L${(x + w).toFixed(1)},${y} `;
    }
    t.add("path", { d, fill: "none", stroke: TARGET_COLOR, "stroke-width": 1.4, opacity: 0.9 });
  }
  if (hasValve && !targets.length) {
    t.text(plotL + 6, L.top + 4 + 7 * textScale, tr("climate.no_target_in_range"), { "font-size": 8.5 });
  }

  for (const run of consecutiveRuns(temps, bucket)) {
    const d = run.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.ts).toFixed(1)},${yT(p.value).toFixed(1)}`).join(" ");
    t.add("path", { d, fill: "none", stroke: TEMP_COLOR, "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" });
  }

  // The stretch picked in the list of runs below the charts.
  const tempPick = hasValve && t.add("rect", {
    y: L.top, height: tempAxisAt - L.top, fill: "none", stroke: "#ffffff", "stroke-width": 1.2, opacity: 0.8, visibility: "hidden",
  });

  // --------------------------------------------------- 2. humidity chart --
  let h = null, yH = null;
  if (humSvg && hums.length >= 2) {
    h = chartCanvas(humSvg, textScale);
    const [hMin, hMax] = niceBounds(hums.map(p => p.value), 2, 10);
    yH = v => humBottom - ((v - hMin) / Math.max(1, hMax - hMin)) * humPlotH;
    runRects(h, HUM_CHART.top, humPlotH, { fill: VALVE_BAND_COLOR, opacity: 0.17 });
    drawTimeAxis(h, axis, xOf, HUM_CHART.top, humBottom, plotL, plotR, textScale);
    [0, 0.5, 1].forEach(frac => {
      const y = humBottom - humPlotH * frac;
      h.add("line", { x1: plotL, x2: plotR, y1: y, y2: y, stroke: GRID_COLOR, "stroke-width": 1 });
      h.text(plotL - 6, y + 3 * textScale, `${Math.round(hMin + (hMax - hMin) * frac)}%`, { "text-anchor": "end", fill: HUM_COLOR });
    });
    for (const run of consecutiveRuns(hums, bucket)) {
      const d = run.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.ts).toFixed(1)},${yH(p.value).toFixed(1)}`).join(" ");
      h.add("path", { d, fill: "none", stroke: HUM_COLOR, "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" });
    }
  }

  // ------------------------------- hover: one moment, marked on both charts --
  const cross = t.add("line", { y1: L.top, y2: tempAxisAt, stroke: "#ffffff", "stroke-width": 1, opacity: 0.45, visibility: "hidden" });
  const dot = t.add("circle", { r: 3.5, fill: TEMP_COLOR, stroke: "#141414", "stroke-width": 1.2, visibility: "hidden" });
  const humCross = h && h.add("line", { y1: HUM_CHART.top, y2: humBottom, stroke: "#ffffff", "stroke-width": 1, opacity: 0.45, visibility: "hidden" });
  const humDot = h && h.add("circle", { r: 3.5, fill: HUM_COLOR, stroke: "#141414", "stroke-width": 1.2, visibility: "hidden" });
  const humPick = hasValve && h && h.add("rect", {
    y: HUM_CHART.top, height: humPlotH, fill: "none", stroke: "#ffffff", "stroke-width": 1.2, opacity: 0.8, visibility: "hidden",
  });

  const show = (el, attrs) => { for (const k in attrs) el.setAttribute(k, attrs[k]); };
  // Outlines one run on both charts - from the list below them.
  const pick = run => [tempPick, humPick].forEach(el => {
    if (!el) return;
    if (!run) return el.setAttribute("visibility", "hidden");
    const x1 = clampX(xOf(run.start)), x2 = clampX(xOf(run.end));
    show(el, { x: x1, width: Math.max(2, x2 - x1), visibility: "visible" });
  });

  const state = { range, bucket, hyst, temps, hums, targets, valves, hasValve, runs, pick };
  climateChartState[id] = state;
  renderClimateReadout(id, readingAt(state, maxTs), true);
  if (hasValve) renderRuns(id, state);

  const hide = () => [cross, dot, humCross, humDot].forEach(el => el && el.setAttribute("visibility", "hidden"));
  const onMove = (e, target) => {
    const rect = target.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (W / rect.width);
    const reading = readingAt(state, minTs + ((x - plotL) / plotW) * (maxTs - minTs));
    if (!reading) return;
    const cx = xOf(reading.ts);
    show(cross, { x1: cx, x2: cx, visibility: "visible" });
    show(dot, { cx, cy: yT(reading.temp), visibility: "visible" });
    if (humCross) {
      show(humCross, { x1: cx, x2: cx, visibility: "visible" });
      if (reading.hum != null) show(humDot, { cx, cy: yH(reading.hum), visibility: "visible" });
      else humDot.setAttribute("visibility", "hidden");
    }
    renderClimateReadout(id, reading, false);
  };
  const onLeave = () => { hide(); renderClimateReadout(id, readingAt(state, maxTs), true); };
  for (const [target, canvas, height] of [[svg, t, tempHeight], [humSvg, h, humHeight]]) {
    if (!canvas) continue;
    const hit = canvas.add("rect", { x: plotL, y: 0, width: plotW, height, fill: "transparent" });
    hit.addEventListener("pointermove", e => onMove(e, target));
    hit.addEventListener("pointerdown", e => onMove(e, target));
    hit.addEventListener("pointerleave", onLeave);
  }
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
      ? `<span class="ro-val ro-target">${tr("climate.target")} ${reading.target.toFixed(1)}°</span>`
      : `<span class="ro-dim">${tr("climate.no_target")}</span>`);
  }
  if (reading.hum != null) parts.push(`<span class="ro-val" style="color:${HUM_COLOR}">${reading.hum.toFixed(0)}%</span>`);
  if (reading.hasValve && reading.valveOpen != null) {
    parts.push(`<span class="ro-valve${reading.valveOpen ? " open" : ""}">${reading.valveOpen ? tr("climate.system_on") : tr("climate.system_off")}</span>`);
  }
  el.innerHTML = parts.join("");
}

// Below a zone's charts: each time the system was on in the range shown,
// newest first - when, for how long, and what temperature and humidity
// did meanwhile. The effect of switching on, without reading a chart.
const RUNS_SHOWN = 5;
const runsExpanded = {};

function formatRunTime(ts, sameDayAs) {
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  const day = new Date((sameDayAs || Date.now() / 1000) * 1000);
  return d.toDateString() === day.toDateString() ? time : `${d.toLocaleDateString(LOCALE, { weekday: "short" })} ${time}`;
}

function startsText(count) {
  if (!count) return tr("climate.starts_none");
  return count === 1 ? tr("climate.starts_one") : tr("climate.starts_many", { n: count });
}

// A reading near `ts` (within a bucket and a half), or null.
function valueNear(points, ts, bucket) {
  const p = nearestPoint(points, ts);
  return p && Math.abs(p.ts - ts) <= bucket * 1.5 ? p.value : null;
}

function runHtml(run, index, state) {
  const nowTs = Date.now() / 1000;
  const end = run.ongoing ? Math.max(run.start, Math.min(run.end, nowTs)) : run.end;
  const latest = state.temps[state.temps.length - 1];
  const tStart = valueNear(state.temps, run.start, state.bucket);
  const tEnd = run.ongoing ? latest.value : valueNear(state.temps, run.end, state.bucket);
  const hStart = valueNear(state.hums, run.start, state.bucket);
  const hEnd = run.ongoing && state.hums.length ? state.hums[state.hums.length - 1].value : valueNear(state.hums, run.end, state.bucket);
  let temp = "";
  if (tStart != null && tEnd != null) {
    const delta = Math.round((tEnd - tStart) * 10) / 10;
    const cls = delta < 0 ? "down" : delta > 0 ? "up" : "flat";
    const deltaText = delta === 0 ? tr("climate.run_steady") : `${delta > 0 ? "+" : "−"}${Math.abs(delta).toFixed(1)}°`;
    temp = `<span class="run-temp">${tStart.toFixed(1)}° → ${tEnd.toFixed(1)}° <b class="run-delta ${cls}">${deltaText}</b></span>`;
  }
  const hum = hStart != null && hEnd != null ? `<span class="run-hum">${hStart.toFixed(0)}% → ${hEnd.toFixed(0)}%</span>` : "";
  const until = run.ongoing ? `<b class="run-live">${tr("climate.run_ongoing")}</b>` : formatRunTime(end, run.start);
  return `
    <div class="run-row" data-run="${index}">
      <div class="run-when"><span>${formatRunTime(run.start)} → ${until}</span><span class="run-dur">${formatDuration(end - run.start)}</span></div>
      <div class="run-effect">${temp}${hum}</div>
    </div>`;
}

function renderRuns(id, state) {
  const el = document.getElementById(`runs-${id}`);
  if (!el) return;
  if (!state) { el.innerHTML = ""; return; }
  const { runs, valves, bucket, range } = state;
  const rangeText = tr(`climate.range_${range}`);
  const nowTs = Date.now() / 1000;
  // From each bucket's share of samples on, not the runs' lengths (whole
  // buckets): on 7d those are whole hours, and the total came out above
  // the month's tile.
  const onSeconds = valves.reduce((sum, p) => sum + p.value * Math.max(0, Math.min(bucket, nowTs - p.ts)), 0);
  const covered = valves.length ? Math.min(valves[valves.length - 1].ts + bucket, nowTs) - valves[0].ts : 0;
  const summary = runs.length
    ? tr("climate.runs_summary", {
        duration: formatDuration(onSeconds),
        pct: covered > 0 ? Math.min(100, Math.round((100 * onSeconds) / covered)) : 0,
        count: startsText(runs.length),
      })
    : tr("climate.runs_none");
  const newestFirst = runs.map((r, i) => [r, i]).reverse();
  const shown = runsExpanded[id] ? newestFirst : newestFirst.slice(0, RUNS_SHOWN);
  const more = newestFirst.length > RUNS_SHOWN
    ? `<button class="range-btn runs-more">${runsExpanded[id] ? tr("climate.runs_fewer") : tr("climate.runs_all", { n: newestFirst.length })}</button>`
    : "";
  el.innerHTML = `
    <div class="chart-title">${tr("climate.runs_title", { range: rangeText })}</div>
    <div class="runs-summary">${summary}</div>
    ${shown.map(([r, i]) => runHtml(r, i, state)).join("")}
    ${more}
    ${runs.length ? `<p class="hint-text runs-note">${tr("climate.runs_note")}</p>` : ""}`;
  el.querySelectorAll(".run-row").forEach(row => {
    const run = runs[Number(row.dataset.run)];
    row.addEventListener("pointerenter", () => { row.classList.add("picked"); state.pick(run); });
    row.addEventListener("pointerleave", () => { row.classList.remove("picked"); state.pick(null); });
  });
  const moreBtn = el.querySelector(".runs-more");
  if (moreBtn) moreBtn.addEventListener("click", () => { runsExpanded[id] = !runsExpanded[id]; renderRuns(id, state); });
}

// ----------------------------------------------------------------- tabs ----

function switchTab(tab) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.getElementById("tab-home").hidden = tab !== "home";
  document.getElementById("tab-climate").hidden = tab !== "climate";
  try { localStorage.setItem("localhome-tab", tab); } catch (e) { /* private browsing etc - just skip remembering it */ }
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
  label.textContent = isSummer
    ? `${tr("climate.season_summer")} · ${tr("climate.mode_cool")}`
    : `${tr("climate.season_winter")} · ${tr("climate.mode_heat")}`;
  // The tab's accent follows the season: blue while cooling, orange while
  // heating (see .season-cool / .season-heat in style.css).
  const tab = document.getElementById("tab-climate");
  if (tab) {
    tab.classList.toggle("season-cool", isSummer);
    tab.classList.toggle("season-heat", !isSummer);
  }
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
      notify(tr("sensors.error_prefix") + apiError(data));
      return;
    }
    climateAwayUntil = data.away_until;
    updateAwayBadge();
    updateAwayStatusText();
    closeAway();
    await refreshClimateZonesLive();
  } catch (e) {
    notify(tr("sensors.request_failed"));
  }
}

async function cancelAway() {
  try {
    const res = await fetch("/api/climate/away/cancel", { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      notify(tr("sensors.error_prefix") + apiError(data));
      return;
    }
    climateAwayUntil = null;
    updateAwayBadge();
    updateAwayStatusText();
    await refreshClimateZonesLive();
  } catch (e) {
    notify(tr("sensors.request_failed"));
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
  climateZones.forEach(updateZoneNext);
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
  // Blues for comfort - the deeper, the cooler the target - and a muted
  // green for economy (26 and up), so no summer setpoint reads as "hot".
  cool: [
    { v: 20, h: 224, s: 75, l: 44 },
    { v: 21, h: 216, s: 72, l: 47 },
    { v: 22, h: 208, s: 70, l: 48 },
    { v: 23, h: 200, s: 66, l: 47 },
    { v: 24, h: 194, s: 60, l: 45 },
    { v: 25, h: 188, s: 55, l: 42 },
    { v: 26, h: 150, s: 32, l: 40 },
    { v: 27, h: 140, s: 26, l: 36 },
  ],
};
// Each season's targets split into "comfort" and "economy" (less heating
// or cooling, lower consumption): heating is economy below 20°C, cooling
// from 26°C up. Shown as two boxes around the palette and next to the
// zone's current target, so the choice is explained, not just coloured.
const TIER_OF = {
  heat: v => (v < 20 ? "economy" : "comfort"),
  cool: v => (v >= 26 ? "economy" : "comfort"),
};

const TIER_ICONS = {
  // flame / snowflake for comfort (matches the season), leaf for economy
  heat: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>`,
  cool: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20"/><path d="M3.34 7 20.66 17"/><path d="M20.66 7 3.34 17"/></svg>`,
  economy: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/></svg>`,
  off: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/></svg>`,
};

function targetTier(value) {
  const tierOf = TIER_OF[climateMode];
  if (!tierOf || value == null) return null;
  return tierOf(value);
}

function swatchHtml(v) {
  return `<button class="sched-swatch${v === paintValue ? " active" : ""}" data-value="${v == null ? "" : v}"
                  style="${v == null ? "" : `background:${tempColor(v)}`}" onclick="selectPaintValue(this)">${v == null ? "✕" : v}</button>`;
}

function paletteHtml() {
  const values = schedulePresets();
  if (!TIER_OF[climateMode]) return values.map(swatchHtml).join("");
  const temps = values.filter(v => v != null);
  // Boxes in the palette's own order: economy first when heating (lower
  // targets), comfort first when cooling. "Off" gets a box of its own, so
  // the ✕ is labelled like the rest.
  const order = [...new Set(temps.map(targetTier))];
  const box = (name, swatches) => `
    <div class="sched-tier sched-tier-${name} sched-tier-${climateMode}">
      <span class="sched-tier-label">${TIER_ICONS[name === "comfort" ? climateMode : name]}${tr(`climate.tier_${name}`)}</span>
      <div class="sched-tier-swatches">${swatches.map(swatchHtml).join("")}</div>
    </div>`;
  const offBox = values.includes(null) ? box("off", [null]) : "";
  return offBox + order.map(name => box(name, temps.filter(v => targetTier(v) === name))).join("");
}

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

// The Climate tab reads top to bottom: a tile per zone - temperature,
// humidity and what it's doing, all zones at a glance - then the zone
// picked there in detail, with its weekly schedule and its statistics as
// two panes instead of one very long card. Which zone and which pane are
// remembered in this browser, like the tab.
let selectedZoneId = null;
let selectedPane = "schedule";
try {
  if (localStorage.getItem("localhome-zone-pane") === "history") selectedPane = "history";
} catch (e) { /* private browsing etc - start on the schedule */ }

// A zone the thermostat leaves out this season (a bathroom while cooling):
// a tile with its valve's real state, no detail card.
function zoneExcluded(zone) {
  return climateMode === "cool" && zone.cool_enabled === false;
}

function ensureSelectedZone() {
  const ids = climateZones.filter(z => !zoneExcluded(z)).map(z => slug(z.name));
  if (ids.includes(selectedZoneId)) return;
  let saved = null;
  try { saved = localStorage.getItem("localhome-zone"); } catch (e) { /* no memory: the first zone */ }
  selectedZoneId = ids.includes(saved) ? saved : ids[0] || null;
}

function selectZone(id) {
  selectedZoneId = id;
  try { localStorage.setItem("localhome-zone", id); } catch (e) { /* just not remembered */ }
  document.querySelectorAll("#zone-overview .zone-tile").forEach(t => t.classList.toggle("selected", t.dataset.zone === id));
  document.querySelectorAll("#climate-zones .zone-card").forEach(c => { c.hidden = c.dataset.zone !== id; });
}

function selectPane(pane) {
  selectedPane = pane;
  try { localStorage.setItem("localhome-zone-pane", pane); } catch (e) { /* just not remembered */ }
  applyPane();
}

function applyPane() {
  document.querySelectorAll("#climate-zones .zone-card").forEach(card => {
    card.querySelectorAll(".pane-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.pane === selectedPane);
      b.setAttribute("aria-selected", String(b.dataset.pane === selectedPane));
    });
    card.querySelectorAll(".zone-pane").forEach(pane => { pane.hidden = pane.dataset.pane !== selectedPane; });
  });
}

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

function escapeAttr(text) {
  return String(text).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// Names come from people - "Camera dell'ospite", "Luce dell'ingresso" -
// and go into HTML: as text they're escaped, and as an inline handler's
// argument they're a JSON string literal (any quote inside is fine) made
// safe for the attribute around it. Never pasted in raw.
function escapeHtml(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function jsArg(value) {
  return escapeAttr(JSON.stringify(String(value)));
}

function zoneReadingParts(zone) {
  const known = (zone.sensor_ok || zoneHasLastKnownValue(zone)) && zone.temperature_c != null;
  return {
    temp: known ? `${zone.temperature_c.toFixed(1)}°` : "--",
    hum: known && zone.humidity_pct != null ? `${zone.humidity_pct.toFixed(0)}%` : "",
  };
}

function zoneTileHtml(zone) {
  const id = slug(zone.name);
  const { temp, hum } = zoneReadingParts(zone);
  const values = `<span class="zt-values"><span class="zt-temp${zone.sensor_ok ? "" : " stale"}">${temp}</span><span class="zt-hum">${hum}</span></span>`;
  // A reading that stopped coming still shows its last value, dimmed -
  // but how old it is has to be on the tile, not one tap away, or a
  // sensor that died on Tuesday reads as today's temperature.
  const stale = !zone.sensor_ok && zoneHasLastKnownValue(zone)
    ? `<span class="zt-stale">${predatesBoot(zone.sensor_last_value_age_seconds)
        ? waitingSinceBootNote()
        : tr("climate.tile_stale", { age: formatAge(zone.sensor_last_value_age_seconds) || "?" })}</span>`
    : "";
  if (zoneExcluded(zone)) {
    const valve = excludedValveState(zone);
    return `
      <div class="zone-tile excluded" data-zone="${id}" title="${escapeAttr(valve.title)}">
        <span class="zt-name">${escapeHtml(zone.name)}${simulatedTagHtml(zone.sensor_simulated)}</span>
        ${values}${stale}
        <span class="zt-status valve-${valve.cls}"><span class="zt-dot"></span>${valve.text}</span>
        <span class="zt-note">${tr("climate.excluded_note")}</span>
      </div>`;
  }
  const state = zoneState(zone);
  return `
    <button class="zone-tile state-${state.cls}${id === selectedZoneId ? " selected" : ""}" data-zone="${id}" onclick="selectZone('${id}')">
      <span class="zt-name">${escapeHtml(zone.name)}${simulatedTagHtml(zone.sensor_simulated)}</span>
      ${values}${stale}
      <span class="zt-status"><span class="zt-dot"></span>${state.short}</span>
    </button>`;
}

function renderZoneOverview() {
  const el = document.getElementById("zone-overview");
  if (!el) return;
  // Only when something on them changed: rebuilt on every 5s refresh, a
  // tap landing at that moment could be lost.
  const html = climateZones.map(zoneTileHtml).join("");
  if (el.dataset.html !== html) {
    el.innerHTML = html;
    el.dataset.html = html;
  }
}

// The sensor's own details, one line under the zone's status.
function zoneMetaHtml(zone) {
  const fresh = zone.sensor_ok ? tr("climate.sensor_updated", { ago: zoneFreshnessText(zone) }) : zoneFreshnessText(zone);
  const items = [`<span class="zone-updated" title="${escapeAttr(zone.sensor_error || "")}">${fresh}</span>`];
  if (zone.battery_pct != null) items.push(`<span class="zone-battery${zone.battery_pct <= LOW_BATTERY_PCT ? " low" : ""}">${batteryLabel(zone.battery_pct)}</span>`);
  if (zone.wifi_rssi_dbm != null) items.push(`<span class="zone-wifi ${wifiQuality(zone.wifi_rssi_dbm)}">${wifiLabel(zone)}</span>`);
  if (dewPointVisible(zone)) items.push(`<span class="zone-dewpoint">${dewPointLabel(zone)}</span>`);
  return items.join("");
}

function buildZoneCard(zone) {
  const id = slug(zone.name);
  const chartId = slug(zone.sensor);
  const state = zoneState(zone);
  const paneAttrs = name => `data-pane="${name}"${selectedPane === name ? "" : " hidden"}`;
  const paneBtn = (name, label) => `
          <button class="pane-btn${selectedPane === name ? " active" : ""}" role="tab" aria-selected="${selectedPane === name}"
                  data-pane="${name}" onclick="selectPane('${name}')">${label}</button>`;
  return `
    <section class="zone-card" id="zone-${id}" data-zone="${id}"${id === selectedZoneId ? "" : " hidden"}>
      <div class="zone-head">
        <div class="zone-head-row">
          <h3 class="zone-title">${escapeHtml(zone.name)}</h3>
          <span class="zone-state state-${state.cls}" id="zone-state-${id}"><span class="zs-dot"></span><span class="zs-label">${state.label}</span></span>
        </div>
        <div class="zone-detail" id="zone-detail-${id}">${state.detail}</div>
        <div class="zone-next" id="zone-next-${id}" hidden></div>
        <div class="zone-meta" id="zone-meta-${id}">${zoneMetaHtml(zone)}</div>
      </div>

      <div class="pane-nav" role="tablist">${paneBtn("schedule", tr("climate.pane_schedule"))}${paneBtn("history", tr("climate.pane_history"))}
      </div>

      <div class="zone-pane" ${paneAttrs("schedule")}>
        <div class="sched-season-label">${tr(`climate.schedule_heading_${climateMode}`)}</div>
        <div class="sched-palette">${paletteHtml()}</div>
        <p class="hint-text">${tr("climate.schedule_hint")}</p>
        <p class="sched-empty-hint" id="sched-empty-${id}" hidden>${tr(`climate.schedule_empty_${climateMode}`)}</p>

        <div class="sched-hour-axis-wrap">
          <div class="sched-hour-axis">${hourAxisHtml()}</div>
          <span class="sched-hour-axis-end">24h</span>
        </div>
        <div class="sched-grid" id="sched-${id}"></div>

        <div class="zone-actions">
          <span class="copy-label">${tr("climate.copy_label")}</span>
          <button class="range-btn" onclick="copyMondayToWeek(${jsArg(zone.name)}, '${id}', true)">${tr("climate.copy_weekdays")}</button>
          <button class="range-btn" onclick="copyMondayToWeek(${jsArg(zone.name)}, '${id}', false)">${tr("climate.copy_monday")}</button>
        </div>
        ${copyToZoneHtml(zone)}
      </div>

      <div class="zone-pane" ${paneAttrs("history")}>
        <div class="chart-filters">
          ${["6h", "24h", "7d"].map(range => `
            <button class="range-btn${range === "24h" ? " active" : ""}" data-sensor="${chartId}" data-range="${range}">${tr(`climate.range_btn_${range}`)}</button>`).join("")}
        </div>

        <div class="runs" id="runs-${chartId}"></div>

        <div class="trend-card">
          <div class="chart-readout" id="readout-${chartId}"></div>
          <div class="chart-title">${tr("fields.temperature_c")}</div>
          <div class="chart-wrap">
            <svg id="chart-${chartId}" viewBox="0 0 400 160" preserveAspectRatio="none"></svg>
          </div>
          ${humidityChartHtml(chartId)}
          <div class="chart-legend">
            <span><span class="legend-dot" style="background:#ff9f0a"></span>${tr("fields.temperature_c")}</span>
            <span><span class="legend-target"></span>${tr("climate.target_line")}</span>
            <span><span class="legend-band legend-valve"></span>${tr("climate.system_on")}</span>
          </div>
        </div>

        <p class="run-totals" id="on-hours-${id}"></p>
      </div>
    </section>
  `;
}

// Refreshed every 5s from /api/climate/zones - the same helpers as the
// first render, so the two never disagree.
function updateZoneCard(zone) {
  const id = slug(zone.name);
  const pill = document.getElementById(`zone-state-${id}`);
  if (!pill) return;
  const state = zoneState(zone);
  pill.className = `zone-state state-${state.cls}`;
  pill.querySelector(".zs-label").textContent = state.label;
  document.getElementById(`zone-detail-${id}`).textContent = state.detail;
  document.getElementById(`zone-meta-${id}`).innerHTML = zoneMetaHtml(zone);
  updateZoneNext(zone);
}

function renderClimateZones() {
  ensurePaintValueForMode();
  const container = document.getElementById("climate-zones");
  const toggleLabel = document.querySelector(".season-link");
  if (!climateZones.length) {
    container.innerHTML = `
      <div class="empty-state">
        <h2>${tr("climate.no_zones")}</h2>
        <p>${tr("climate.no_zones_note")}</p>
        <a class="tbtn open" href="/climate-admin">${tr("app.empty_open_admin")}</a>
      </div>`;
    if (toggleLabel) toggleLabel.style.display = "none";
    return;
  }
  if (toggleLabel) toggleLabel.style.display = "";

  ensureSelectedZone();
  const detailZones = climateZones.filter(z => !zoneExcluded(z));
  container.innerHTML = `
    <div class="zone-overview" id="zone-overview">${climateZones.map(zoneTileHtml).join("")}</div>
    ${detailZones.map(buildZoneCard).join("")}`;
  updateAwayBanner();

  // Each zone's temperature/humidity trend reuses the exact same chart
  // code as a regular `climate` sensor card (loadChart/drawClimateChart) -
  // just pointed at the zone's underlying sensor name instead of a card
  // built by buildSensorCard(), via this small stand-in object.
  container.querySelectorAll(".range-btn[data-sensor]").forEach(btn => {
    btn.addEventListener("click", () => {
      const chartId = btn.dataset.sensor;
      container.querySelectorAll(`.range-btn[data-sensor="${chartId}"]`).forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      chartRanges[chartId] = btn.dataset.range;
      const zone = climateZones.find(z => slug(z.sensor) === chartId);
      if (zone) loadChart({ name: zone.sensor, kind: "climate", valve: zone.valve });
    });
  });

  detailZones.forEach(zone => {
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

// The other zones this season's schedule can be copied onto. In cooling
// a bathroom is left out of the season entirely (zoneExcluded), so it
// isn't offered there - the button only shows up when there's really
// another grid to fill.
function copyTargets(zone) {
  return climateZones.filter(other => other.name !== zone.name && !zoneExcluded(other));
}

function copyToZoneHtml(zone) {
  const targets = copyTargets(zone);
  if (!targets.length) return "";
  return `
        <div class="zone-actions">
          <span class="copy-label">${tr("climate.copy_zone_label")}</span>
          ${targets.map(target => `
            <button class="range-btn" onclick="copyScheduleToZone(${jsArg(zone.name)}, ${jsArg(target.name)})">${escapeHtml(target.name)}</button>`).join("")}
        </div>`;
}

// The whole week, for the season on screen only - heating and cooling
// keep their own grids, and copying one over the other is never what
// someone means here.
async function copyScheduleToZone(fromName, toName) {
  const week = scheduleWeeks[fromName];
  if (!week) return;
  const season = tr(`climate.season_name_${scheduleWeekModes[fromName] || climateMode}`);
  const ok = await confirmAction(tr("climate.copy_zone_confirm", { from: fromName, to: toName, season }),
                                 { confirmLabel: tr("climate.copy_zone_button") });
  if (!ok) return;
  const copy = {};
  for (const day of DAY_KEYS) copy[day] = week[day].slice();
  scheduleWeeks[toName] = copy;
  scheduleWeekModes[toName] = scheduleWeekModes[fromName] || climateMode;
  buildScheduleGrid(slug(toName), copy);
  updateEmptyScheduleHint(toName);
  const zone = climateZones.find(z => z.name === toName);
  if (zone) updateZoneNext(zone);
  await saveSchedule(toName);
  updateNowHighlight();
  notify(tr("climate.copy_zone_done", { to: toName }), "ok");
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
  const zone = climateZones.find(z => z.name === zoneName);
  if (zone) updateZoneNext(zone);
  clearTimeout(saveTimers[zoneName]);
  saveTimers[zoneName] = setTimeout(() => saveSchedule(zoneName), 600);
}

// The week as the server last said it was, per zone - what an edit is
// compared against to know which hours to send.
const scheduleSavedWeeks = {};

function copyWeek(week) {
  const out = {};
  for (const day of DAY_KEYS) out[day] = week[day].slice();
  return out;
}

// Only the hours that changed go to the server, which merges them into
// the week as it is *now* and sends that back. Two phones editing the same
// zone used to each send their whole week, and the second save wiped the
// first one's changes; now both survive, and this page shows the other's
// too as soon as it saves.
async function saveSchedule(zoneName) {
  delete saveTimers[zoneName];
  const week = scheduleWeeks[zoneName];
  if (!week) return;
  const saved = scheduleSavedWeeks[zoneName];
  const cells = [];
  for (const day of DAY_KEYS) {
    for (let h = 0; h < 24; h++) {
      const value = week[day][h];
      if (!saved || saved[day][h] !== value) cells.push([day, h, value]);
    }
  }
  if (!cells.length) return;
  try {
    const res = await fetch(`/api/climate/zones/${encodeURIComponent(zoneName)}/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cells, mode: scheduleWeekModes[zoneName] || climateMode }),
    });
    const data = await res.json();
    if (!data.ok) {
      notify(`${tr("climate.save_failed")}: ${apiError(data)}`);
      return;
    }
    if (data.week) adoptServerWeek(zoneName, data.week);
  } catch (e) {
    notify(tr("climate.save_failed"));
  }
}

// The merged week from the server becomes this page's: the grid is
// redrawn only if it differs (someone else's edit) and nobody is in the
// middle of painting on it, and edits made since the save was sent stay.
function adoptServerWeek(zoneName, serverWeek) {
  const local = scheduleWeeks[zoneName];
  scheduleSavedWeeks[zoneName] = copyWeek(serverWeek);
  if (saveTimers[zoneName] || isPainting) return;  // a newer local edit is on its way
  let differs = false;
  for (const day of DAY_KEYS) {
    for (let h = 0; h < 24; h++) {
      if (local[day][h] !== serverWeek[day][h]) differs = true;
    }
  }
  if (!differs) return;
  scheduleWeeks[zoneName] = copyWeek(serverWeek);
  buildScheduleGrid(slug(zoneName), scheduleWeeks[zoneName]);
  updateEmptyScheduleHint(zoneName);
  const zone = climateZones.find(z => z.name === zoneName);
  if (zone) updateZoneNext(zone);
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
    scheduleSavedWeeks[zoneName] = copyWeek(data.week);
    scheduleWeekModes[zoneName] = data.mode || climateMode;
    buildScheduleGrid(slug(zoneName), data.week);
    updateEmptyScheduleHint(zoneName);
    const zone = climateZones.find(z => z.name === zoneName);
    if (zone) updateZoneNext(zone);
  } catch (e) { /* grid stays empty; next tab visit retries */ }
}

async function refreshOnHours(zoneName) {
  try {
    const res = await fetch(`/api/climate/zones/${encodeURIComponent(zoneName)}/on-hours`);
    const data = await res.json();
    const el = document.getElementById(`on-hours-${slug(zoneName)}`);
    if (!el || !data.ok) return;
    const starts = data.starts || {};
    el.textContent = tr("climate.run_totals", {
      week: formatDuration(data.week * 3600), week_starts: startsText(starts.week),
      month: formatDuration(data.month * 3600), month_starts: startsText(starts.month),
    });
  } catch (e) { /* leave dashes */ }
}

// What a zone is doing, in words - one place, so its tile, its detail
// header and the 5s refresh never disagree. There is one valve per zone:
// "on" means it's open and the system is working toward the target;
// "idle" the target is reached; "off" no target this hour; "away" and
// "warn" held off (away mode, or a sensor silent past its safety limit).
function formatTarget(value, withTier) {
  const tier = withTier ? targetTier(value) : null;
  return `${Number.isInteger(value) ? value : value.toFixed(1)}°${tier ? ` ${tr(`climate.tier_${tier}`)}` : ""}`;
}

function zoneState(zone) {
  const off = tr("climate.system_off");
  if (climateAwayUntil) {
    return { cls: "away", label: off, short: tr("climate.tile_away"), detail: tr("climate.detail_away", { until: formatAwayUntil(climateAwayUntil) }) };
  }
  if (zone.sensor_failsafe_active) {
    return { cls: "warn", label: off, short: tr("climate.tile_failsafe"), detail: tr("climate.detail_failsafe") };
  }
  // No reading at all (not even a last known one): whatever the valve is
  // doing, "target reached" would be a guess.
  if (!zone.sensor_ok && !zoneHasLastKnownValue(zone)) {
    const label = zone.valve_on ? tr("climate.system_on") : off;
    return { cls: "warn", label, short: tr("climate.tile_no_sensor"), detail: tr("climate.detail_no_sensor") };
  }
  if (zone.target_c == null) {
    return { cls: "off", label: off, short: tr("climate.tile_off"), detail: tr("climate.detail_no_target") };
  }
  if (zone.valve_on) {
    return {
      cls: "on", label: tr("climate.system_on"),
      short: tr("climate.tile_on", { target: formatTarget(zone.target_c) }),
      detail: tr("climate.detail_on", { target: formatTarget(zone.target_c, true) }),
    };
  }
  return {
    cls: "idle", label: off,
    short: tr("climate.tile_reached", { target: formatTarget(zone.target_c) }),
    detail: tr("climate.detail_reached", { target: formatTarget(zone.target_c, true) }),
  };
}

// While cooling, whether an excluded zone's valve is really closed is what
// matters - its real state, and why if it isn't. The backend holds it
// closed unless an interlock (e.g. a dehumidifier fed through it) drives
// it - see ThermostatController._tick().
function excludedValveState(zone) {
  const driver = zone.valve_driver;
  const note = driver ? tr("climate.excluded_driver_note", { driver: driver.name }) : "";
  if (!zone.valve_ok) {
    return { cls: "bad", text: tr("climate.excluded_short_unknown"), title: tr("climate.excluded_valve_unknown", { zone: zone.name, error: zone.valve_error || "?" }) };
  }
  if (!zone.valve_on) return { cls: "ok", text: tr("climate.excluded_short_closed"), title: note };
  if (driver && driver.on) return { cls: "info", text: tr("climate.excluded_short_feeding", { driver: driver.name }), title: note };
  return { cls: "warn", text: tr("climate.excluded_short_closing"), title: note };
}

// The next hour, up to a week ahead, whose scheduled target differs from
// this hour's: "what happens next" without reading the grid.
function nextScheduleChange(zoneName) {
  const week = scheduleWeeks[zoneName];
  if (!week) return null;
  const { dayKey, hour } = scheduleNow();
  const today = DAY_KEYS.indexOf(dayKey);
  const current = week[dayKey][hour];
  for (let step = 1; step < 7 * 24; step++) {
    const days = Math.floor((hour + step) / 24);
    const dayIndex = (today + days) % 7;
    const value = week[DAY_KEYS[dayIndex]][(hour + step) % 24];
    if (value !== current) return { days, dayIndex, hour: (hour + step) % 24, value };
  }
  return null;
}

function updateZoneNext(zone) {
  const el = document.getElementById(`zone-next-${slug(zone.name)}`);
  if (!el) return;
  const next = climateAwayUntil || zone.sensor_failsafe_active ? null : nextScheduleChange(zone.name);
  el.hidden = !next;
  if (!next) return;
  const time = `${String(next.hour).padStart(2, "0")}:00`;
  const when = next.days === 0 ? tr("climate.when_today", { time })
    : next.days === 1 ? tr("climate.when_tomorrow", { time })
    : tr("climate.when_day", { day: tr("climate.days")[next.dayIndex], time });
  el.textContent = next.value == null
    ? tr("climate.next_off", { when })
    : tr("climate.next_change", { when, target: formatTarget(next.value, true) });
}

// A device tagged `visible_in_mode: "heat"/"cool"` in config.yaml (e.g. a
// dehumidifier that only makes sense while cooling) hides its card outside
// that mode. Re-run whenever climateMode changes - on boot, after toggling
// it, and on every live refresh in case it changed from elsewhere. The
// "Other devices" heading goes with the last visible card.
function applyClimateModeVisibility() {
  let anyVisible = false;
  document.querySelectorAll("#climate-extra .sensor-card").forEach(card => {
    const requiredMode = card.dataset.visibleInMode;
    card.hidden = !!requiredMode && requiredMode !== climateMode;
    anyVisible = anyVisible || !card.hidden;
  });
  const head = document.getElementById("climate-extra-head");
  if (head) head.hidden = !anyVisible;
}

// The season's own effects on the Climate tab's other devices, from
// /api/climate/zones: a switch it turns on and off by itself (a bathroom
// bypass valve) and one only usable in one season (a dehumidifier).
let climateSeasonEffects = null;

// One line under each such device - otherwise "why is it on?" or "why
// can't I switch it on?" has no answer on the page.
function annotateSeasonDevices() {
  const effects = climateSeasonEffects || {};
  const notes = {};
  (effects.mode_switches || []).forEach(m => { notes[m.switch] = tr(`climate.device_auto_${m.active_in_mode}`); });
  (effects.mode_limited || []).forEach(m => { notes[m.switch] = tr(`climate.device_only_${m.only_in_mode}`); });
  for (const [name, text] of Object.entries(notes)) {
    const top = document.querySelector(`#climate-extra #sensor-${slug(name)} .sensor-top`);
    if (!top) continue;
    let note = top.parentElement.querySelector(".season-note");
    if (!note) {
      note = document.createElement("div");
      note.className = "season-note";
      top.after(note);
    }
    note.textContent = text;
  }
}

// The Climate tab's outdoor sensor - the first climate sensor among its
// other devices - also shown as a chip next to the season.
let outdoorSensorName = null;
let outdoorSensorSimulated = false;
const ICON_THERMOMETER = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/></svg>`;

function updateOutdoorChip(reading) {
  const chip = document.getElementById("climate-outdoor");
  if (!chip) return;
  const known = reading && reading.temperature_c != null;
  chip.hidden = !known;
  if (!known) return;
  chip.innerHTML = `${ICON_THERMOMETER}<span>${tr("climate.outdoor_label")}</span><b>${fmt(reading.temperature_c, 1)}°</b>`
    + (reading.humidity_pct != null ? `<span class="oc-hum">${fmt(reading.humidity_pct, 0)}%</span>` : "")
    + simulatedTagHtml(outdoorSensorSimulated);
}

function scrollToOutdoorCard() {
  const card = outdoorSensorName && document.getElementById(`sensor-${slug(outdoorSensorName)}`);
  if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
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
    markOnline();
    climateZones = data.zones;
    climateSeasonEffects = data.season_effects || null;
    updateSeasonHeading();
    updateAwayBadge();
    updateAwayBanner();
    renderZoneOverview();
    data.zones.forEach(zone => {
      checkLowBatteryPopup(zone);
      updateZoneCard(zone);
    });
    annotateSeasonDevices();
    applyClimateModeVisibility();
  } catch (e) {
    markOffline();  // keep the last render, but say it's not live
  }
}

// Everything that depends on the season, rebuilt together: heading and
// toggle, which zone cards exist, and each card's palette and grid colors
// (both season-specific - see SCHEDULE_PRESETS / TEMP_COLOR_STOPS).
async function rerenderClimateForSeason(zonesData) {
  await flushPendingScheduleSaves();
  updateSeasonHeading();
  applyClimateModeVisibility();
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
  // Switches before sensors: on the Climate tab the other devices are
  // mostly things to switch, and a sensor's charts make a tall card.
  const climateExtraSensors = devicesData.sensors
    .filter(s => s.tab === "climate" && !zoneMemberNames.has(s.name))
    .sort((a, b) => (a.kind === "climate") - (b.kind === "climate"));

  const outdoorSensor = climateExtraSensors.find(s => s.kind === "climate") || {};
  outdoorSensorName = outdoorSensor.name || null;
  outdoorSensorSimulated = !!outdoorSensor.simulated;
  // A fresh install has no devices yet: say so, and point at the panel
  // where they're added, instead of leaving an empty page.
  const coversSection = document.getElementById("covers-section");
  if (coversSection) coversSection.hidden = !(window.COVER_NAMES || []).length;
  const homeEmpty = document.getElementById("home-empty");
  if (homeEmpty) homeEmpty.hidden = !!(homeSensors.length || (window.COVER_NAMES || []).length);

  renderSensors(homeSensors, "sensor-cards");
  renderSensors(climateExtraSensors, "climate-extra");
  const pollableSensors = homeSensors.concat(climateExtraSensors);
  setInterval(() => pollableSensors.forEach(refreshSensor), 5000);
  refreshVmcProgram();
  setInterval(refreshVmcProgram, 60000);
  setInterval(() => pollableSensors.forEach(loadChart), 60000);

  if (climateData.ok) {
    climateMode = climateData.mode || "heat";
    if (climateData.hysteresis_c != null) climateHysteresis = climateData.hysteresis_c;
    climateAwayUntil = climateData.away_until || null;
    climateSeasonEffects = climateData.season_effects || null;
    updateSeasonHeading();
    updateAwayBadge();
    climateZones = climateData.zones;
    renderClimateZones();
    annotateSeasonDevices();
    applyClimateModeVisibility();
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
