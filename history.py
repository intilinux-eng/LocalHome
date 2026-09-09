import sqlite3
import threading
import time

import ewelink_power
import notify
import tuya_air_quality

DB_FILE = "history.db"
POLL_INTERVAL_SECONDS = 30

# IRETI (Italian DSO) power thresholds for this meter, per IRETI's own
# GEMIS meter guide ("Guida al contatore_2022_GEMIS.pdf"). The guide gives
# concrete numbers for a 3kW contract; we scale them to this 4.5kW one.
#   - potenza disponibile = potenza impegnata + 10%, drawable indefinitely
#   - tolerated for >=3h (warnings at 2min / 92min) up to ~4/3x the
#     contracted power (IRETI's example: 3kW contract tolerates up to 4kW)
#   - above that, only ~2 minutes of tolerance before the breaker trips
CONTRACT_LIMIT_W = 4500
AVAILABLE_POWER_W = CONTRACT_LIMIT_W * 1.10  # 4950W — safe indefinitely
TRIP_RISK_W = CONTRACT_LIMIT_W * 4 / 3  # 6000W — only ~2min tolerance above this

# Alert as soon as we leave the "safe indefinitely" zone. While sustained
# above threshold, re-notify at most every ALERT_COOLDOWN_SECONDS instead of
# every poll, so a long dishwasher cycle doesn't flood the chat.
ALERT_THRESHOLD_W = AVAILABLE_POWER_W
ALERT_COOLDOWN_SECONDS = 600

_started = False
_lock = threading.Lock()
_alert_active = False
_last_alert_ts = 0


def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_readings (
            ts INTEGER NOT NULL,
            power_w REAL NOT NULL,
            voltage_v REAL,
            current_a REAL,
            day_kwh REAL,
            month_kwh REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_energy_ts ON energy_readings(ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS climate_readings (
            ts INTEGER NOT NULL,
            temperature_c REAL,
            humidity_pct REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_climate_ts ON climate_readings(ts)")
    return conn


def start_background():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_poll_loop, daemon=True).start()


def _poll_loop():
    conn = _connect()
    try:
        while True:
            now = int(time.time())

            reading = ewelink_power.get_power_reading()
            if reading.get("ok"):
                conn.execute(
                    "INSERT INTO energy_readings (ts, power_w, voltage_v, current_a, day_kwh, month_kwh) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        reading["power_w"],
                        reading["voltage_v"],
                        reading["current_a"],
                        reading["day_kwh"],
                        reading["month_kwh"],
                    ),
                )
                _check_power_alert(reading["power_w"])

            climate = tuya_air_quality.get_air_quality_reading()
            if climate.get("ok"):
                conn.execute(
                    "INSERT INTO climate_readings (ts, temperature_c, humidity_pct) VALUES (?, ?, ?)",
                    (now, climate["temperature_c"], climate["humidity_pct"]),
                )

            conn.commit()
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        conn.close()


def _check_power_alert(power_w: float):
    global _alert_active, _last_alert_ts
    now = time.time()

    if power_w >= ALERT_THRESHOLD_W:
        if not _alert_active or (now - _last_alert_ts) >= ALERT_COOLDOWN_SECONDS:
            notify.send(f"Consumo elevato: {power_w:.0f} W (soglia sicura {ALERT_THRESHOLD_W:.0f} W)")
            _last_alert_ts = now
        _alert_active = True
    elif _alert_active:
        notify.send(f"Consumo rientrato: {power_w:.0f} W")
        _alert_active = False


# Bucket sizes chosen so each range renders a reasonable number of points.
RANGE_BUCKETS = {
    "6h": (6 * 3600, 60),
    "24h": (24 * 3600, 300),
    "7d": (7 * 24 * 3600, 3600),
}


def _start_of_today_ts() -> int:
    now = time.localtime()
    start = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, now.tm_isdst))
    return int(time.mktime(start))


def query_peak_today() -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT power_w, ts FROM energy_readings WHERE ts >= ? ORDER BY power_w DESC LIMIT 1",
            (_start_of_today_ts(),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"power_w": 0.0, "ts": None}
    return {"power_w": row[0], "ts": row[1]}


def query_power_history(range_key: str):
    window_seconds, bucket_seconds = RANGE_BUCKETS.get(range_key, RANGE_BUCKETS["24h"])
    since = int(time.time()) - window_seconds

    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT (ts / ?) * ? AS bucket, AVG(power_w)
            FROM energy_readings
            WHERE ts >= ?
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (bucket_seconds, bucket_seconds, since),
        ).fetchall()
    finally:
        conn.close()

    return [{"ts": row[0], "power_w": round(row[1], 1)} for row in rows]


def query_climate_history(range_key: str):
    window_seconds, bucket_seconds = RANGE_BUCKETS.get(range_key, RANGE_BUCKETS["24h"])
    since = int(time.time()) - window_seconds

    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT (ts / ?) * ? AS bucket, AVG(temperature_c), AVG(humidity_pct)
            FROM climate_readings
            WHERE ts >= ?
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (bucket_seconds, bucket_seconds, since),
        ).fetchall()
    finally:
        conn.close()

    return [
        {"ts": row[0], "temperature_c": round(row[1], 1), "humidity_pct": round(row[2], 1)}
        for row in rows
    ]
