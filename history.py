import sqlite3
import threading
import time

import ewelink_power

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

_started = False
_lock = threading.Lock()


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
            reading = ewelink_power.get_power_reading()
            if reading.get("ok"):
                conn.execute(
                    "INSERT INTO energy_readings (ts, power_w, voltage_v, current_a, day_kwh, month_kwh) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        int(time.time()),
                        reading["power_w"],
                        reading["voltage_v"],
                        reading["current_a"],
                        reading["day_kwh"],
                        reading["month_kwh"],
                    ),
                )
                conn.commit()
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        conn.close()


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


def query_peak_today() -> float:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(power_w) FROM energy_readings WHERE ts >= ?",
            (_start_of_today_ts(),),
        ).fetchone()
    finally:
        conn.close()
    return row[0] or 0.0


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
