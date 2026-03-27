#!/usr/bin/env python3
"""Solar Battery Optimizer for Deye Hybrid Inverter.

Self-learning system that optimizes Time-of-Use battery charging based on
solar production forecasts and weather conditions. Controls the Deye inverter
via Home Assistant's Solarman integration.

Usage:
    python3 solar_optimizer.py optimize     # Calculate + write tomorrow's TOU settings
    python3 solar_optimizer.py dry-run      # Like optimize but print-only
    python3 solar_optimizer.py record       # Record today's outcomes + run learning
    python3 solar_optimizer.py poll         # Record hourly snapshot (for cron)
    python3 solar_optimizer.py status       # Show current state + learning params
    python3 solar_optimizer.py history [N]  # Show last N days (default 14)
    python3 solar_optimizer.py set-param KEY VALUE  # Manually set a learning parameter
    python3 solar_optimizer.py reset        # Reset learning parameters to defaults

Environment:
    HASS_SERVER  - Home Assistant URL (e.g. http://localhost:8123)
    HASS_TOKEN   - Long-lived access token
"""

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "solar_optimizer.db"
ENV_PATH = SCRIPT_DIR / ".env"

# MetOcean API (MetService) — primary weather source for NZ
METOCEAN_API_URL = "https://forecast-v2.metoceanapi.com/point/time"
METOCEAN_API_KEY = None  # loaded from .env
METOCEAN_LAT = -43.505   # Christchurch, Parklands
METOCEAN_LON = 172.698

# Battery
BATTERY_CAPACITY_KWH = 15.0
BATTERY_RESERVE_PCT = 10
USABLE_CAPACITY_KWH = BATTERY_CAPACITY_KWH * (100 - BATTERY_RESERVE_PCT) / 100  # 13.5

# Peak hours (inclusive)
PEAK_START_HOUR = 7   # 7am
PEAK_END_HOUR = 21    # 9pm

# Charge deadline — battery should reach target SOC by this time
CHARGE_DEADLINE_HOUR = 6
CHARGE_DEADLINE_MIN = 30

# Minimum overnight SOC for power outage safety (Slot 1 floor)
OUTAGE_RESERVE_PCT = 30

# Charge power limits (watts)
CHARGE_POWER_MIN = 3000
CHARGE_POWER_MAX = 10000
CHARGE_POWER_DEFAULT = 3000

# TOU slot register addresses (Deye hybrid)
TOU_REGS = {
    "time":   [250, 251, 252, 253, 254, 255],   # 0x00FA-0x00FF
    "power":  [256, 257, 258, 259, 260, 261],   # 0x0100-0x0105
    "soc":    [268, 269, 270, 271, 272, 273],   # 0x010C-0x0111
    "enable": [274, 275, 276, 277, 278, 279],   # 0x0112-0x0117
    "tou_master": 248,                           # 0x00F8
}

# TOU slot layout — 6 slots tiling 00:00 to 00:00
# Off-peak: 00:00-07:00 (2 slots), Peak: 07:00-21:00 (3 slots), Off-peak: 21:00-00:00 (1 slot)
# Slot 1: 00:00-04:00  Overnight grid charge
# Slot 2: 04:00-07:00  Early morning grid charge
# Slot 3: 07:00-11:00  Peak morning (no grid)
# Slot 4: 11:00-15:00  Solar peak (no grid)
# Slot 5: 15:00-21:00  Afternoon peak (no grid)
# Slot 6: 21:00-00:00  Evening off-peak grid charge
SLOT_TIMES = [
    (0, 0),    # slot 1: 00:00
    (4, 0),    # slot 2: 04:00
    (7, 0),    # slot 3: 07:00
    (11, 0),   # slot 4: 11:00
    (15, 0),   # slot 5: 15:00
    (21, 0),   # slot 6: 21:00
]

SLOT_POWERS = [3000, 3000, 10000, 10000, 10000, 3000]  # watts (peak slots need high power for battery discharge)

# Enable registers (274-279 / 0x0112-0x0117) control grid/gen charging:
#   0 = no grid/gen charge, 1 = grid charge, 2 = gen charge, 3 = both
# Slot 1: grid charge enabled (but SOC=reserve, so no actual charge until poll adjusts)
# Slot 2: grid charge enabled (main charge window, poll shifts start time)
# Slots 3-5: peak, no grid charge
# Slot 6: no grid charge (never charge before midnight)
SLOT_ENABLES = [1, 1, 0, 0, 0, 1]

# HA entity IDs
SENSORS = {
    "solar_forecast_tomorrow": "sensor.energy_production_tomorrow",
    "solar_forecast_today": "sensor.energy_production_today",
    "weather": "weather.forecast_home",
    "battery_soc": "sensor.inverter_battery_soc",
    "daily_production": "sensor.inverter_daily_production",
    "daily_consumption": "sensor.inverter_daily_load_consumption",
    "daily_grid_bought": "sensor.inverter_daily_energy_bought",
    "daily_grid_sold": "sensor.inverter_daily_energy_sold",
    "daily_battery_charge": "sensor.inverter_daily_battery_charge",
    "daily_battery_discharge": "sensor.inverter_daily_battery_discharge",
    "grid_power": "sensor.inverter_total_grid_power",
    "pv_power": "sensor.inverter_pv_power",
    "load_power": "sensor.inverter_total_load_power",
}

# NZ seasons (month -> season name)
NZ_SEASONS = {
    12: "summer", 1: "summer", 2: "summer",
    3: "autumn", 4: "autumn", 5: "autumn",
    6: "winter", 7: "winter", 8: "winter",
    9: "spring", 10: "spring", 11: "spring",
}

# Monthly consumption multipliers (applied to legacy daily_consumption_avg)
# Used to seed seasonal averages with more realistic initial values
SEASONAL_FACTOR = {
    1: 0.85, 2: 0.90, 3: 0.95, 4: 1.05, 5: 1.10, 6: 1.15,
    7: 1.15, 8: 1.10, 9: 1.05, 10: 0.95, 11: 0.90, 12: 0.85,
}

# Hourly solar production weights (fraction of daily total per hour)
# Bell curve centered ~1pm NZDT, covering 7am-9pm peak hours
# These approximate a typical NZ solar day; sum ≈ 0.85 (peak_solar_ratio)
HOURLY_SOLAR_WEIGHT = {
    7: 0.02, 8: 0.05, 9: 0.08, 10: 0.10, 11: 0.12, 12: 0.13,
    13: 0.13, 14: 0.12, 15: 0.10, 16: 0.08, 17: 0.05, 18: 0.03,
    19: 0.01, 20: 0.00,
}

# Hourly consumption weights (fraction of daily total per peak hour)
# Double-hump: morning (breakfast/hot water) + evening (cooking/heating/lights)
# Sum ≈ 0.70 (matches peak_consumption_ratio default)
HOURLY_CONSUMPTION_WEIGHT = {
    7: 0.06, 8: 0.06, 9: 0.05, 10: 0.04, 11: 0.04, 12: 0.05,
    13: 0.05, 14: 0.04, 15: 0.04, 16: 0.05, 17: 0.07, 18: 0.07,
    19: 0.05, 20: 0.03,
}

# Weather condition normalization map (used in multiple places)
CONDITION_MAP = {
    "sunny": "sunny", "clear-night": "sunny", "clear": "sunny",
    "partlycloudy": "partlycloudy", "partly-cloudy": "partlycloudy",
    "cloudy": "cloudy", "fog": "cloudy",
    "rainy": "rainy", "pouring": "rainy", "snowy": "rainy",
    "lightning": "rainy", "lightning-rainy": "rainy",
    "hail": "rainy", "windy": "partlycloudy",
    "windy-variant": "partlycloudy", "exceptional": "cloudy",
}

# Temperature bands for consumption factor (°C thresholds)
# Below band_cold -> temp_factor_cold, band_cold-band_mild -> temp_factor_cool, etc.
TEMP_BANDS = [
    (10, "temp_factor_cold"),     # below 10°C
    (15, "temp_factor_cool"),     # 10-15°C
    (20, "temp_factor_mild"),     # 15-20°C
    (99, "temp_factor_warm"),     # above 20°C
]

# Weekend consumption factor — everyone home, heating all day if cold
# 0=Mon, 5=Sat, 6=Sun
WEEKEND_DAYS = {5, 6}

# Default learning parameters
DEFAULT_PARAMS = {
    "base_overnight_soc": 60.0,
    "sunny_correction": 1.0,
    "cloudy_correction": 0.70,
    "partlycloudy_correction": 0.85,
    "rainy_correction": 0.45,
    "learning_rate": 2.0,
    "daily_consumption_avg": 33.0,  # legacy — used with SEASONAL_FACTOR for comparison
    "consumption_avg_spring": 31.9,  # 33 × avg(Sep=1.05, Oct=0.95, Nov=0.90)
    "consumption_avg_summer": 28.6,  # 33 × avg(Dec=0.85, Jan=0.85, Feb=0.90)
    "consumption_avg_autumn": 34.1,  # 33 × avg(Mar=0.95, Apr=1.05, May=1.10)
    "consumption_avg_winter": 37.4,  # 33 × avg(Jun=1.15, Jul=1.15, Aug=1.10)
    "consumption_avg_year": 33.0,
    "min_overnight_soc": 30.0,
    "max_overnight_soc": 100.0,
    "safety_soc_floor": 15.0,
    "peak_consumption_ratio": 0.70,   # fraction of daily consumption during peak
    "peak_solar_ratio": 0.85,         # fraction of daily solar during peak
    "safety_margin_pct": 10.0,        # added to calculated SOC
    # Solar model preference: 0.0 = cloud-based, 1.0 = radiation-based
    "preferred_solar_model": 0.0,
    # Weekend consumption factor (Sat/Sun everyone home, heating on all day)
    "weekend_factor": 1.20,
    # Weekday consumption factor (baseline)
    "weekday_factor": 1.0,
    # Temperature-based consumption factors
    "temp_factor_cold": 1.35,         # below 10°C — heavy heating
    "temp_factor_cool": 1.15,         # 10-15°C — moderate heating
    "temp_factor_mild": 1.0,          # 15-20°C — baseline
    "temp_factor_warm": 0.85,         # above 20°C — less heating
}

FAILSAFE_OVERNIGHT_SOC = 80

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("solar_optimizer")


def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")

    # Also log to file
    log_path = SCRIPT_DIR / "solar_optimizer.log"
    fh = logging.FileHandler(log_path)
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_env():
    """Load .env file if it exists, populate os.environ."""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    server = os.environ.get("HASS_SERVER")
    token = os.environ.get("HASS_TOKEN")
    if not server or not token:
        log.error("HASS_SERVER and HASS_TOKEN must be set")
        sys.exit(1)

    global METOCEAN_API_KEY
    METOCEAN_API_KEY = os.environ.get("METOCEAN_API_KEY")
    if not METOCEAN_API_KEY:
        log.warning("METOCEAN_API_KEY not set — MetOcean forecasts will be unavailable")

    return server, token


# ---------------------------------------------------------------------------
# Home Assistant API
# ---------------------------------------------------------------------------

class HomeAssistantAPI:
    def __init__(self, server, token):
        self.server = server.rstrip("/")
        self.token = token

    def _request(self, method, path, data=None):
        url = f"{self.server}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            log.error(f"HA API error {e.code}: {e.read().decode()}")
            raise
        except urllib.error.URLError as e:
            log.error(f"HA API connection error: {e}")
            raise

    def get_state(self, entity_id):
        return self._request("GET", f"/api/states/{entity_id}")

    def get_sensor_float(self, entity_id):
        state = self.get_state(entity_id)
        val = state.get("state", "")
        if val in ("unavailable", "unknown", ""):
            log.warning(f"Sensor {entity_id} is {val}")
            return None
        try:
            return float(val)
        except ValueError:
            log.warning(f"Sensor {entity_id} has non-numeric state: {val}")
            return None

    def get_weather(self):
        state = self.get_state(SENSORS["weather"])
        attrs = state.get("attributes", {})
        return {
            "condition": state.get("state", "unknown"),
            "cloud_coverage": attrs.get("cloud_coverage", 50),
            "temperature": attrs.get("temperature"),
            "humidity": attrs.get("humidity"),
        }

    def get_weather_forecast(self):
        """Get daily weather forecast via HA service call."""
        try:
            result = self._request("POST", "/api/services/weather/get_forecasts?return_response", {
                "entity_id": SENSORS["weather"],
                "type": "daily",
            })
            # Response has service_response wrapper
            if isinstance(result, dict):
                sr = result.get("service_response", result)
                entity_data = sr.get(SENSORS["weather"], sr)
                if isinstance(entity_data, dict):
                    forecasts = entity_data.get("forecast", [])
                elif isinstance(entity_data, list):
                    forecasts = entity_data
                else:
                    forecasts = []
            else:
                forecasts = []

            # Forecast dates are end-of-day UTC, so tomorrow's forecast
            # has datetime like "2026-03-25T23:00:00+00:00"
            # Match by checking if the date portion is today (for tomorrow's weather)
            # or tomorrow
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            today_str = date.today().isoformat()
            for fc in forecasts:
                fc_date = fc.get("datetime", "")[:10]
                # met.no labels tomorrow's forecast with today's date at 23:00 UTC
                # or tomorrow's date — check both
                if fc_date == tomorrow or fc_date == today_str:
                    # Prefer the one closest to tomorrow
                    pass
            # More robust: return the second forecast entry (index 1 = tomorrow)
            if len(forecasts) >= 2:
                return forecasts[1]
            elif forecasts:
                return forecasts[0]
        except Exception as e:
            log.warning(f"Could not get weather forecast: {e}")
        return {}

    def get_hourly_forecast(self, target_date=None):
        """Get hourly weather forecast for a specific date.

        Returns a list of dicts with hour, condition, cloud_coverage, precipitation
        for peak hours (PEAK_START_HOUR to PEAK_END_HOUR).
        """
        try:
            result = self._request("POST", "/api/services/weather/get_forecasts?return_response", {
                "entity_id": SENSORS["weather"],
                "type": "hourly",
            })
            sr = result.get("service_response", result)
            entity_data = sr.get(SENSORS["weather"], sr)
            if isinstance(entity_data, dict):
                forecasts = entity_data.get("forecast", [])
            elif isinstance(entity_data, list):
                forecasts = entity_data
            else:
                forecasts = []

            if target_date is None:
                target_date = date.today().isoformat()

            peak_hours = []
            for fc in forecasts:
                fc_dt = fc.get("datetime", "")
                if not fc_dt[:10] == target_date:
                    continue
                try:
                    hour = int(fc_dt[11:13])
                except (ValueError, IndexError):
                    continue
                if PEAK_START_HOUR <= hour < PEAK_END_HOUR:
                    peak_hours.append({
                        "hour": hour,
                        "condition": fc.get("condition", "cloudy").lower(),
                        "cloud_coverage": fc.get("cloud_coverage", 50),
                        "precipitation": fc.get("precipitation", 0) or 0,
                        "temperature": fc.get("temperature"),
                    })
            return peak_hours
        except Exception as e:
            log.warning(f"Could not get hourly forecast: {e}")
            return []

    def write_register(self, register, value):
        """Write a single holding register via Solarman (using write_multiple)."""
        log.info(f"Writing register {register} = {value}")
        self._request("POST", "/api/services/solarman/write_multiple_holding_registers", {
            "register": register,
            "values": [int(value)],
        })

    def call_service(self, domain, service, data=None):
        return self._request("POST", f"/api/services/{domain}/{service}", data or {})


# ---------------------------------------------------------------------------
# MetOcean API (MetService NZ)
# ---------------------------------------------------------------------------

def get_metocean_hourly(target_date):
    """Fetch hourly forecast from MetOcean API for peak hours on target_date.

    Returns list of dicts with hour, condition, cloud_coverage, precipitation,
    temperature — same format as HomeAssistantAPI.get_hourly_forecast().
    Falls back to empty list on any error.
    """
    try:
        # NZ is UTC+12 (NZST) or UTC+13 (NZDT during DST)
        # target_date 7am NZDT = previous day 18:00 UTC
        # Start from previous day 17:00 UTC to cover all peak hours
        prev_day = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        from_utc = f"{prev_day}T17:00:00Z"

        body = json.dumps({
            "points": [{"lat": METOCEAN_LAT, "lon": METOCEAN_LON}],
            "variables": [
                "air.temperature.at-2m",
                "cloud.cover",
                "precipitation.rate",
                "radiation.flux.downward.shortwave",
            ],
            "time": {
                "from": from_utc,
                "interval": "1h",
                "repeat": 36,  # 36 hours covers the full day in any TZ offset
            },
        }).encode()

        req = urllib.request.Request(
            METOCEAN_API_URL,
            data=body,
            method="POST",
            headers={
                "x-api-key": METOCEAN_API_KEY,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        times = data["dimensions"]["time"]["data"]
        temps_k = data["variables"]["air.temperature.at-2m"]["data"]
        clouds = data["variables"]["cloud.cover"]["data"]
        precips = data["variables"]["precipitation.rate"]["data"]
        shortwave = data["variables"].get("radiation.flux.downward.shortwave", {}).get("data", [])

        # Convert UTC times to NZ local and filter peak hours on target_date
        # Determine NZ offset: NZDT is +13 Apr-Sep, NZST +12 rest
        # Simplify: try +13 first (NZDT covers most of the year for DST)
        from datetime import timezone as tz
        nz_offset = timedelta(hours=13)  # NZDT

        peak_hours = []
        for i, t_str in enumerate(times):
            utc_dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
            local_dt = utc_dt + nz_offset
            local_date = local_dt.strftime("%Y-%m-%d")
            local_hour = local_dt.hour

            if local_date != target_date:
                continue
            if not (PEAK_START_HOUR <= local_hour < PEAK_END_HOUR):
                continue

            temp_c = temps_k[i] - 273.15  # Kelvin to Celsius
            cloud_pct = clouds[i]
            precip_mm = precips[i]

            # Map cloud/precip to a condition string
            if precip_mm > 0.5:
                condition = "rainy"
            elif cloud_pct >= 80:
                condition = "cloudy"
            elif cloud_pct >= 40:
                condition = "partlycloudy"
            else:
                condition = "sunny"

            # Shortwave radiation in W/m² (if available)
            sw_wm2 = shortwave[i] if i < len(shortwave) else None

            peak_hours.append({
                "hour": local_hour,
                "condition": condition,
                "cloud_coverage": cloud_pct,
                "precipitation": precip_mm,
                "temperature": round(temp_c, 1),
                "shortwave_wm2": round(sw_wm2, 1) if sw_wm2 is not None else None,
            })

        log.info(f"MetOcean: got {len(peak_hours)} peak hours for {target_date}")
        return peak_hours

    except Exception as e:
        log.warning(f"MetOcean API failed, will fall back to HA forecast: {e}")
        return []


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

BACKUP_DIR = SCRIPT_DIR / "backups"
BACKUP_KEEP = 7  # number of daily backups to retain


def backup_db():
    """Create a daily SQLite backup, keeping the last BACKUP_KEEP copies."""
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    today = date.today().isoformat()
    backup_path = BACKUP_DIR / f"solar_optimizer_{today}.db"
    if backup_path.exists():
        return  # already backed up today

    # Use SQLite online backup API for a safe, consistent copy
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()
    log.info(f"Database backed up to {backup_path}")

    # Prune old backups
    backups = sorted(BACKUP_DIR.glob("solar_optimizer_*.db"))
    for old in backups[:-BACKUP_KEEP]:
        old.unlink()
        log.info(f"Removed old backup: {old.name}")


def get_db():
    db = sqlite3.connect(str(DB_PATH), timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.row_factory = sqlite3.Row
    init_db(db)
    return db


def init_db(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS daily_plan (
            date               TEXT PRIMARY KEY,
            created_at         TEXT NOT NULL,
            solar_forecast_kwh REAL,
            weather_condition  TEXT,
            cloud_coverage_pct REAL,
            precipitation_mm   REAL,
            temperature_high   REAL,
            adjusted_solar_kwh REAL,
            overnight_soc_target INTEGER,
            energy_deficit_kwh REAL,
            correction_factor  REAL,
            slot1_soc INTEGER, slot2_soc INTEGER, slot3_soc INTEGER,
            slot4_soc INTEGER, slot5_soc INTEGER, slot6_soc INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily_outcome (
            date                   TEXT PRIMARY KEY,
            recorded_at            TEXT NOT NULL,
            actual_production_kwh  REAL,
            actual_consumption_kwh REAL,
            grid_bought_kwh        REAL,
            grid_sold_kwh          REAL,
            battery_charge_kwh     REAL,
            battery_discharge_kwh  REAL,
            battery_soc_at_record  REAL,
            peak_grid_used         INTEGER DEFAULT 0,
            peak_grid_kwh          REAL DEFAULT 0,
            forecast_accuracy      REAL,
            weather_condition      TEXT
        );

        CREATE TABLE IF NOT EXISTS hourly_log (
            timestamp       TEXT PRIMARY KEY,
            date            TEXT NOT NULL,
            hour            INTEGER NOT NULL,
            battery_soc     REAL,
            grid_power_w    REAL,
            pv_power_w      REAL,
            load_power_w    REAL,
            grid_bought_kwh REAL,
            grid_sold_kwh   REAL
        );

        CREATE TABLE IF NOT EXISTS learning_params (
            param_key   TEXT PRIMARY KEY,
            param_value REAL NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_hourly_date ON hourly_log(date);

        CREATE TABLE IF NOT EXISTS forecast_tracking (
            date              TEXT NOT NULL,
            hour              INTEGER NOT NULL,
            model_cloud_kwh   REAL,
            model_rad_kwh     REAL,
            actual_pv_wh      REAL,
            est_consumption_kwh REAL,
            est_battery_soc   REAL,
            actual_consumption_wh REAL,
            actual_battery_soc REAL,
            weather_condition TEXT,
            cloud_pct         REAL,
            temperature       REAL,
            shortwave_wm2     REAL,
            PRIMARY KEY (date, hour)
        );
    """)
    db.commit()

    # Add temperature_high column to daily_outcome if missing (migration)
    try:
        db.execute("SELECT temperature_high FROM daily_outcome LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE daily_outcome ADD COLUMN temperature_high REAL")
        db.commit()

    # Add forecast_tracking columns if missing (migration)
    for col, coltype in [("est_consumption_kwh", "REAL"),
                         ("est_battery_soc", "REAL"),
                         ("actual_consumption_wh", "REAL"),
                         ("actual_battery_soc", "REAL"),
                         ("weather_condition", "TEXT"),
                         ("cloud_pct", "REAL"),
                         ("temperature", "REAL"),
                         ("shortwave_wm2", "REAL")]:
        try:
            db.execute(f"SELECT {col} FROM forecast_tracking LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE forecast_tracking ADD COLUMN {col} {coltype}")
            db.commit()

    # Seed default params if empty
    cursor = db.execute("SELECT COUNT(*) FROM learning_params")
    if cursor.fetchone()[0] == 0:
        now = datetime.now().isoformat()
        for key, val in DEFAULT_PARAMS.items():
            db.execute(
                "INSERT INTO learning_params (param_key, param_value, updated_at) VALUES (?, ?, ?)",
                (key, val, now),
            )
        db.commit()
        log.info("Initialized default learning parameters")


def get_param(db, key):
    row = db.execute("SELECT param_value FROM learning_params WHERE param_key=?", (key,)).fetchone()
    if row:
        return row[0]
    return DEFAULT_PARAMS.get(key)


def get_temp_factor(db, temp_c):
    """Get the consumption multiplier for a given temperature."""
    if temp_c is None:
        return 1.0
    for threshold, param_key in TEMP_BANDS:
        if temp_c < threshold:
            return get_param(db, param_key) or 1.0
    return get_param(db, "temp_factor_warm") or 0.85


def set_param(db, key, value):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT OR REPLACE INTO learning_params (param_key, param_value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Hour-by-Hour Battery Simulation
# ---------------------------------------------------------------------------

def build_hourly_solar(raw_solar, hourly_forecast, db):
    """Build hourly solar production dict using cloud-based corrections (Model A)."""
    result = {}
    for hfc in hourly_forecast:
        hour = hfc["hour"]
        weight = HOURLY_SOLAR_WEIGHT.get(hour, 0)
        if weight == 0:
            continue
        hour_solar = raw_solar * weight
        hcond = CONDITION_MAP.get(hfc["condition"], hfc["condition"])
        corr = get_param(db, f"{hcond}_correction") or 0.6
        cloud_mod = 1.0 - (hfc.get("cloud_coverage", 50) / 100.0) * 0.5
        result[hour] = hour_solar * corr * cloud_mod
    return result


def build_hourly_solar_radiation(raw_solar, hourly_forecast):
    """Build hourly solar production dict using shortwave radiation (Model B).

    Uses the relative shortwave radiation intensity per hour to distribute
    the Forecast.Solar daily total across hours. This directly captures
    atmospheric effects (clouds, aerosols, humidity) rather than inferring
    from cloud percentage.

    Returns None if shortwave data is not available.
    """
    # Collect shortwave values for peak hours
    sw_by_hour = {}
    for hfc in hourly_forecast:
        hour = hfc["hour"]
        sw = hfc.get("shortwave_wm2")
        if sw is not None and hour in HOURLY_SOLAR_WEIGHT:
            sw_by_hour[hour] = max(0, sw)  # clamp negative values

    if not sw_by_hour or sum(sw_by_hour.values()) <= 0:
        return None

    # Distribute the raw_solar total proportionally to shortwave intensity
    total_sw = sum(sw_by_hour.values())
    result = {}
    for hour, sw in sw_by_hour.items():
        result[hour] = raw_solar * (sw / total_sw)

    return result


def build_hourly_consumption(daily_consumption, seasonal_factor, temp_factor, day_factor=1.0):
    """Build hourly consumption dict from daily total and adjustment factors."""
    return {
        hour: daily_consumption * weight * seasonal_factor * temp_factor * day_factor
        for hour, weight in HOURLY_CONSUMPTION_WEIGHT.items()
    }


def get_season(target_date=None):
    """Get NZ season name for a date."""
    if target_date is None:
        month = datetime.now().month
    elif isinstance(target_date, str):
        month = datetime.strptime(target_date, "%Y-%m-%d").month
    else:
        month = target_date.month
    return NZ_SEASONS[month]


def get_seasonal_consumption(db, target_date=None):
    """Get the consumption average for the season of the given date."""
    season = get_season(target_date)
    return get_param(db, f"consumption_avg_{season}")


def get_day_factor(db, target_date):
    """Get weekday/weekend consumption factor for a given date."""
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    if target_date.weekday() in WEEKEND_DAYS:
        return get_param(db, "weekend_factor") or 1.20
    return get_param(db, "weekday_factor") or 1.0


def simulate_battery_hourly(starting_soc_pct, hourly_solar, hourly_consumption):
    """Simulate battery SOC hour-by-hour through peak hours.

    Returns dict with:
        min_soc: lowest SOC reached (%)
        min_soc_hour: hour when minimum occurred
        shortfall_kwh: max energy below reserve (0 if never breached)
        hourly_soc: list of (hour, soc_pct) tuples
    """
    soc_kwh = (starting_soc_pct / 100.0) * BATTERY_CAPACITY_KWH
    reserve_kwh = (BATTERY_RESERVE_PCT / 100.0) * BATTERY_CAPACITY_KWH

    min_soc_kwh = soc_kwh
    min_soc_hour = PEAK_START_HOUR
    max_shortfall_kwh = 0.0
    hourly_soc = []

    for hour in range(PEAK_START_HOUR, PEAK_END_HOUR):
        solar = hourly_solar.get(hour, 0.0)
        consumption = hourly_consumption.get(hour, 0.0)

        soc_kwh += solar - consumption
        soc_kwh = max(0.0, min(BATTERY_CAPACITY_KWH, soc_kwh))

        soc_pct = (soc_kwh / BATTERY_CAPACITY_KWH) * 100.0
        hourly_soc.append((hour, round(soc_pct, 1)))

        if soc_kwh < min_soc_kwh:
            min_soc_kwh = soc_kwh
            min_soc_hour = hour

        if soc_kwh < reserve_kwh:
            shortfall = reserve_kwh - soc_kwh
            if shortfall > max_shortfall_kwh:
                max_shortfall_kwh = shortfall

    return {
        "min_soc": round((min_soc_kwh / BATTERY_CAPACITY_KWH) * 100.0, 1),
        "min_soc_hour": min_soc_hour,
        "shortfall_kwh": round(max_shortfall_kwh, 2),
        "hourly_soc": hourly_soc,
    }


# ---------------------------------------------------------------------------
# Decision Algorithm
# ---------------------------------------------------------------------------

def calculate_plan(ha, db):
    """Calculate optimal TOU settings for tomorrow."""

    # Step 1: Get solar forecast
    raw_solar = ha.get_sensor_float(SENSORS["solar_forecast_tomorrow"])
    if raw_solar is None or raw_solar <= 0:
        log.warning("Solar forecast unavailable or zero, using failsafe")
        return make_failsafe_plan("no_forecast")

    # Step 2: Get weather
    weather = ha.get_weather()
    condition = weather["condition"].lower()
    cloud_pct = weather.get("cloud_coverage", 50)

    # Get tomorrow's specific forecast (overrides current weather)
    tomorrow_fc = ha.get_weather_forecast()
    if tomorrow_fc:
        condition = tomorrow_fc.get("condition", condition).lower()
        # met.no daily forecasts don't include cloud_coverage,
        # so estimate from condition and precipitation
        precipitation = tomorrow_fc.get("precipitation", 0) or 0
        temp_high = tomorrow_fc.get("temperature")
        # Estimate cloud coverage from condition if not provided
        if "cloud_coverage" in tomorrow_fc:
            cloud_pct = tomorrow_fc["cloud_coverage"]
        else:
            CONDITION_CLOUD_EST = {
                "sunny": 10, "clear-night": 10, "clear": 10,
                "partlycloudy": 45, "cloudy": 75, "fog": 85,
                "rainy": 90, "pouring": 95, "snowy": 90,
                "lightning": 90, "lightning-rainy": 95,
            }
            cloud_pct = CONDITION_CLOUD_EST.get(condition, 50)
    else:
        precipitation = 0
        temp_high = weather.get("temperature")

    # Step 3: Get hourly forecast — MetOcean primary, HA fallback
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    hourly = get_metocean_hourly(tomorrow_str)
    if not hourly:
        hourly = ha.get_hourly_forecast(tomorrow_str)

    # Step 4: Build hourly solar and consumption profiles
    daily_consumption = get_seasonal_consumption(db, tomorrow_str)
    season = get_season(tomorrow_str)

    if hourly and len(hourly) >= 6:
        # Model A: cloud-based corrections
        solar_cloud = build_hourly_solar(raw_solar, hourly, db)
        # Model B: shortwave radiation-based
        solar_rad = build_hourly_solar_radiation(raw_solar, hourly)

        # Prefer radiation model when shortwave data is available — it uses
        # actual atmospheric measurements rather than crude cloud% corrections.
        # Fall back to cloud model only when radiation data is missing.
        if solar_rad and sum(solar_rad.values()) > 0:
            hourly_solar_map = solar_rad
            active_model = "radiation"
        else:
            hourly_solar_map = solar_cloud
            active_model = "cloud"

        peak_solar = sum(hourly_solar_map.values())
        adjusted_solar = peak_solar
        correction_factor = adjusted_solar / raw_solar if raw_solar > 0 else 0

        # Use average temperature from hourly forecast
        temps = [h["temperature"] for h in hourly if h.get("temperature") is not None]
        if temps:
            temp_high = sum(temps) / len(temps)

        cloud_total = sum(solar_cloud.values())
        rad_total = sum(solar_rad.values()) if solar_rad else 0
        hours_detail = [f"{h}:{hourly_solar_map.get(h, 0):.1f}" for h in sorted(hourly_solar_map)]
        log.info(f"Solar forecast: {raw_solar:.1f} kWh, active={active_model}, "
                 f"cloud={cloud_total:.1f} kWh, radiation={rad_total:.1f} kWh "
                 f"[{', '.join(hours_detail)}]")
    else:
        # Fallback — single daily condition, spread using weights
        normalized = CONDITION_MAP.get(condition, None)
        correction_key = f"{normalized}_correction" if normalized else f"{condition}_correction"
        base_correction = get_param(db, correction_key)
        if base_correction is None:
            base_correction = 0.6
        cloud_modifier = 1.0 - (cloud_pct / 100.0) * 0.5
        correction_factor = base_correction * cloud_modifier
        adjusted_solar = raw_solar * correction_factor
        peak_solar = adjusted_solar * get_param(db, "peak_solar_ratio")

        # Build hourly solar from daily total spread by weight
        total_weight = sum(HOURLY_SOLAR_WEIGHT.values())
        hourly_solar_map = {
            h: peak_solar * (w / total_weight)
            for h, w in HOURLY_SOLAR_WEIGHT.items() if w > 0
        }
        log.info(f"Solar forecast: {raw_solar:.1f} kWh, adjusted: {adjusted_solar:.1f} kWh "
                 f"(daily fallback, correction: {correction_factor:.2f})")

    temp_factor = get_temp_factor(db, temp_high)
    day_factor = get_day_factor(db, tomorrow_str)
    # Seasonal variation is captured in the seasonal consumption avg directly
    hourly_consumption_map = build_hourly_consumption(daily_consumption, 1.0, temp_factor, day_factor)
    peak_consumption = sum(hourly_consumption_map.values())
    energy_deficit = max(0, peak_consumption - peak_solar)

    log.info(f"Peak consumption: {peak_consumption:.1f} kWh "
             f"(season={season}, base={daily_consumption:.1f}, "
             f"temp_factor={temp_factor:.2f}, day_factor={day_factor:.2f}), "
             f"peak solar: {peak_solar:.1f} kWh, "
             f"deficit: {energy_deficit:.1f} kWh")

    # Step 5: Binary search for minimum starting SOC where battery stays above reserve
    safety_margin = get_param(db, "safety_margin_pct")
    reserve_target = BATTERY_RESERVE_PCT + safety_margin  # e.g. 10% + 10% = 20% floor

    # Binary search: find lowest SOC where sim min never drops below reserve_target
    lo, hi = BATTERY_RESERVE_PCT, 100
    best_soc = hi  # worst case
    best_sim = None

    for _ in range(15):  # converges in ~7 iterations for 0-100 range
        mid = (lo + hi) / 2.0
        sim = simulate_battery_hourly(mid, hourly_solar_map, hourly_consumption_map)
        if sim["min_soc"] >= reserve_target:
            best_soc = mid
            best_sim = sim
            hi = mid
        else:
            lo = mid
        if hi - lo < 1:
            break

    # Round up to nearest 5 for safety
    candidate_soc = int(((best_soc + 4) // 5) * 5)

    # Final simulation at the rounded value
    sim = simulate_battery_hourly(candidate_soc, hourly_solar_map, hourly_consumption_map)

    soc_from_deficit = (energy_deficit / USABLE_CAPACITY_KWH) * 100 + safety_margin

    log.info(f"Simulation: min_soc={sim['min_soc']}% at {sim['min_soc_hour']}:00, "
             f"optimal start={best_soc:.0f}%, rounded={candidate_soc}%, "
             f"deficit-based={soc_from_deficit:.0f}%, "
             f"profile={sim['hourly_soc']}")

    min_soc = get_param(db, "min_overnight_soc")
    max_soc = get_param(db, "max_overnight_soc")
    overnight_soc = max(min_soc, min(max_soc, candidate_soc))

    # Round to nearest 5
    overnight_soc = int(round(overnight_soc / 5) * 5)

    log.info(f"SOC target: sim-optimal={candidate_soc}%, "
             f"deficit-based={soc_from_deficit:.0f}%, "
             f"final={overnight_soc}%")

    # Build slot SOC values
    # Slot 1 (00:00-04:00) and Slot 6 (21:00-00:00) stay at reserve — no early charging.
    # The hourly poll will dynamically shift Slot 2 start time and power
    # so the battery reaches target SOC by 06:30, minimizing time at 100%.
    slot_socs = [
        OUTAGE_RESERVE_PCT,   # slot 1: 00:00-04:00 charge to 30% for outage safety
        overnight_soc,        # slot 2: 04:00-07:00 charge window (poll shifts start)
        BATTERY_RESERVE_PCT,  # slot 3: 07:00-11:00 peak morning
        BATTERY_RESERVE_PCT,  # slot 4: 11:00-15:00 solar peak
        BATTERY_RESERVE_PCT,  # slot 5: 15:00-21:00 afternoon peak
        OUTAGE_RESERVE_PCT,   # slot 6: 21:00-00:00 outage safety (same as slot 1)
    ]

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "date": tomorrow,
        "solar_forecast_kwh": raw_solar,
        "weather_condition": condition,
        "cloud_coverage_pct": cloud_pct,
        "precipitation_mm": precipitation,
        "temperature_high": temp_high,
        "adjusted_solar_kwh": adjusted_solar,
        "overnight_soc_target": overnight_soc,
        "energy_deficit_kwh": energy_deficit,
        "correction_factor": correction_factor,
        "slot_socs": slot_socs,
    }


def make_failsafe_plan(reason):
    """Return a conservative failsafe plan."""
    log.warning(f"Using failsafe plan (reason: {reason})")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "date": tomorrow,
        "solar_forecast_kwh": 0,
        "weather_condition": "unknown",
        "cloud_coverage_pct": 100,
        "precipitation_mm": 0,
        "temperature_high": None,
        "adjusted_solar_kwh": 0,
        "overnight_soc_target": FAILSAFE_OVERNIGHT_SOC,
        "energy_deficit_kwh": 0,
        "correction_factor": 1.0,
        "slot_socs": [OUTAGE_RESERVE_PCT, FAILSAFE_OVERNIGHT_SOC,
                      BATTERY_RESERVE_PCT, BATTERY_RESERVE_PCT,
                      BATTERY_RESERVE_PCT, BATTERY_RESERVE_PCT],
    }


# ---------------------------------------------------------------------------
# Register Writing
# ---------------------------------------------------------------------------

def encode_time(hour, minute):
    """Encode time as Deye register value: hour*100 + minute (decimal packed)."""
    return hour * 100 + minute


def write_tou_config(ha, plan):
    """Write all TOU registers to the inverter."""
    slot_socs = plan["slot_socs"]
    writes = []

    # Time registers
    for i, (h, m) in enumerate(SLOT_TIMES):
        writes.append((TOU_REGS["time"][i], encode_time(h, m)))

    # SOC registers
    for i, soc in enumerate(slot_socs):
        writes.append((TOU_REGS["soc"][i], soc))

    # Power registers
    for i, power in enumerate(SLOT_POWERS):
        writes.append((TOU_REGS["power"][i], power))

    # Enable registers (also controls grid charge: 0=none, 1=grid, 2=gen, 3=both)
    for i, enable in enumerate(SLOT_ENABLES):
        writes.append((TOU_REGS["enable"][i], enable))

    # TOU master enable
    writes.append((TOU_REGS["tou_master"], 1))

    # Write with delays
    failed = []
    for register, value in writes:
        for attempt in range(3):
            try:
                ha.write_register(register, value)
                time.sleep(0.5)
                break
            except Exception as e:
                log.warning(f"Write register {register}={value} attempt {attempt+1} failed: {e}")
                if attempt == 2:
                    failed.append((register, value, str(e)))
                time.sleep(1)

    if failed:
        log.error(f"Failed to write {len(failed)} registers: {failed}")
    else:
        log.info(f"Successfully wrote all TOU registers")

    return len(failed) == 0


def write_dashboard_status(ha, db, plan=None):
    """Write a JSON status file for the HA dashboard card."""
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    status = {
        "updated_at": datetime.now().strftime("%H:%M %d %b"),
        "battery_soc": ha.get_sensor_float(SENSORS["battery_soc"]),
    }

    # Show today's plan during the day, tomorrow's plan after 9pm optimize
    p = plan
    if not p:
        now_hour = datetime.now().hour
        if now_hour >= 21:
            # After optimize — show tomorrow's plan
            row = db.execute("SELECT * FROM daily_plan WHERE date=?", (tomorrow,)).fetchone()
            if not row:
                row = db.execute("SELECT * FROM daily_plan WHERE date=?", (today,)).fetchone()
        else:
            # During the day — show today's plan
            row = db.execute("SELECT * FROM daily_plan WHERE date=?", (today,)).fetchone()
            if not row:
                row = db.execute("SELECT * FROM daily_plan WHERE date=?", (tomorrow,)).fetchone()
        if row:
            p = dict(row)
    if p:
        # Read actual TOU slot config from inverter sensors
        slots = []
        for i in range(6):
            n = i + 1
            slot_time = ha.get_state(f"sensor.inverter_time_of_use_time_{n}").get("state", "??:??")
            slot_soc = ha.get_sensor_float(f"sensor.inverter_time_of_use_soc_{n}")
            slot_power = ha.get_sensor_float(f"sensor.inverter_time_of_use_power_{n}")
            slot_enable = ha.get_sensor_float(f"sensor.inverter_time_of_use_enable_{n}")
            # Next slot time for "to" column
            if i < 5:
                next_time = ha.get_state(f"sensor.inverter_time_of_use_time_{n+1}").get("state", "??:??")
            else:
                next_time = "00:00"
            slots.append({
                "slot": n,
                "from": slot_time,
                "to": next_time,
                "soc": int(slot_soc) if slot_soc is not None else "?",
                "power": int(slot_power) if slot_power is not None else "?",
                "grid": "Yes" if slot_enable and int(slot_enable) & 1 else "No",
            })

        status["plan"] = {
            "date": p.get("date", tomorrow),
            "solar_forecast": p.get("solar_forecast_kwh"),
            "adjusted_solar": p.get("adjusted_solar_kwh"),
            "weather": p.get("weather_condition"),
            "cloud_pct": p.get("cloud_coverage_pct"),
            "overnight_soc": p.get("overnight_soc_target"),
            "deficit": p.get("energy_deficit_kwh"),
            "correction": p.get("correction_factor"),
            "temp_factor": get_temp_factor(db, p.get("temperature_high")),
            "slots": slots,
        }

    # Today's outcome (if recorded)
    outcome = db.execute("SELECT * FROM daily_outcome WHERE date=?", (today,)).fetchone()
    if outcome:
        status["today"] = {
            "production": outcome["actual_production_kwh"],
            "consumption": outcome["actual_consumption_kwh"],
            "grid_bought": outcome["grid_bought_kwh"],
            "grid_sold": outcome["grid_sold_kwh"],
            "peak_grid_used": bool(outcome["peak_grid_used"]),
            "peak_grid_kwh": outcome["peak_grid_kwh"],
            "forecast_accuracy": outcome["forecast_accuracy"],
        }
    # Live actuals for the dashboard (always include when no outcome yet)
    if not outcome:
        live = {}
        solar_today = ha.get_sensor_float(SENSORS["daily_production"])
        if solar_today is not None:
            live["solar"] = round(solar_today, 1)
        peak_start = get_param(db, "peak_start_grid_bought")
        current_bought = ha.get_sensor_float(SENSORS["daily_grid_bought"])
        if peak_start is not None and current_bought is not None:
            live["peak_grid_kwh"] = round(max(0, current_bought - peak_start), 1)
        else:
            live["peak_grid_kwh"] = 0.0
        batt = ha.get_sensor_float(SENSORS["battery_soc"])
        if batt is not None:
            live["battery_soc"] = round(batt, 0)
        weather = ha.get_weather()
        live["weather"] = weather.get("condition", "unknown").lower()
        status["live"] = live

    # Learning params summary
    status["learning"] = {
        "base_soc": get_param(db, "base_overnight_soc"),
        "consumption_avg": get_param(db, "daily_consumption_avg"),
        "consumption_spring": get_param(db, "consumption_avg_spring"),
        "consumption_summer": get_param(db, "consumption_avg_summer"),
        "consumption_autumn": get_param(db, "consumption_avg_autumn"),
        "consumption_winter": get_param(db, "consumption_avg_winter"),
        "consumption_year": get_param(db, "consumption_avg_year"),
        "sunny_corr": get_param(db, "sunny_correction"),
        "cloudy_corr": get_param(db, "cloudy_correction"),
        "partly_corr": get_param(db, "partlycloudy_correction"),
        "rainy_corr": get_param(db, "rainy_correction"),
        "temp_cold": get_param(db, "temp_factor_cold"),
        "temp_cool": get_param(db, "temp_factor_cool"),
        "temp_mild": get_param(db, "temp_factor_mild"),
        "temp_warm": get_param(db, "temp_factor_warm"),
    }

    # Days of data
    days = db.execute("SELECT COUNT(*) FROM daily_outcome").fetchone()[0]
    status["days_of_data"] = days

    # Model preference
    pref = get_param(db, "preferred_solar_model") or 0.0
    status["learning"]["model_pref"] = round(pref, 2)
    status["learning"]["active_model"] = "radiation"  # radiation preferred when available

    # Detailed hourly forecast for the detail view
    try:
        plan_date = p.get("date", today) if p else today
        raw_solar = ha.get_sensor_float(
            SENSORS["solar_forecast_today"] if plan_date == today
            else SENSORS["solar_forecast_tomorrow"]
        )

        hourly = get_metocean_hourly(plan_date)
        hourly_source = "metocean"
        if not hourly:
            hourly = ha.get_hourly_forecast(plan_date)
            hourly_source = "ha"

        if hourly and len(hourly) >= 6 and raw_solar and raw_solar > 0:
            solar_cloud = build_hourly_solar(raw_solar, hourly, db)
            solar_rad = build_hourly_solar_radiation(raw_solar, hourly)

            daily_consumption = get_seasonal_consumption(db, plan_date)
            season = get_season(plan_date)
            temps = [h["temperature"] for h in hourly if h.get("temperature") is not None]
            avg_temp = sum(temps) / len(temps) if temps else None
            temp_factor = get_temp_factor(db, avg_temp)
            day_factor = get_day_factor(db, plan_date)
            hourly_consumption_map = build_hourly_consumption(daily_consumption, 1.0, temp_factor, day_factor)

            # Use radiation model when available (matches plan/revision logic)
            active_solar = solar_rad if (solar_rad and sum(solar_rad.values()) > 0) else solar_cloud
            soc_target = p.get("overnight_soc_target", 70) if p else 70
            sim = simulate_battery_hourly(soc_target, active_solar, hourly_consumption_map)

            # Get actual hourly battery SOC from poll logs
            actual_soc_rows = db.execute(
                "SELECT hour, battery_soc FROM hourly_log WHERE date=? ORDER BY hour",
                (plan_date,)
            ).fetchall()
            actual_soc_by_hour = {r["hour"]: r["battery_soc"] for r in actual_soc_rows}

            # Build hourly detail rows
            sim_soc_map = dict(sim["hourly_soc"])
            detail_hours = []
            for hfc in hourly:
                h = hfc["hour"]
                forecast_soc = sim_soc_map.get(h)
                actual_soc = actual_soc_by_hour.get(h)
                diff = None
                if forecast_soc is not None and actual_soc is not None:
                    diff = round(actual_soc - forecast_soc, 1)
                detail_hours.append({
                    "hour": h,
                    "condition": hfc["condition"],
                    "cloud": round(hfc.get("cloud_coverage", 0)),
                    "temp": hfc.get("temperature"),
                    "sw_wm2": hfc.get("shortwave_wm2"),
                    "solar_cloud": round(solar_cloud.get(h, 0), 2),
                    "solar_rad": round((solar_rad or {}).get(h, 0), 2),
                    "consumption": round(hourly_consumption_map.get(h, 0), 2),
                    "battery_soc": forecast_soc,
                    "actual_soc": actual_soc,
                    "soc_diff": diff,
                })

            status["detail"] = {
                "source": hourly_source,
                "raw_solar": raw_solar,
                "plan_date": plan_date,
                "season": season,
                "seasonal_avg": daily_consumption,
                "temp_factor": round(temp_factor, 2),
                "day_factor": round(day_factor, 2),
                "avg_temp": round(avg_temp, 1) if avg_temp else None,
                "cloud_total": round(sum(solar_cloud.values()), 1),
                "rad_total": round(sum(solar_rad.values()), 1) if solar_rad else None,
                "consumption_total": round(sum(hourly_consumption_map.values()), 1),
                "sim_min_soc": sim["min_soc"],
                "sim_min_hour": sim["min_soc_hour"],
                "hours": detail_hours,
            }

            # Recent model tracking accuracy
            tracking = db.execute("""
                SELECT date,
                       SUM(ABS(model_cloud_kwh * 1000 - actual_pv_wh)) as cloud_err,
                       SUM(ABS(model_rad_kwh * 1000 - actual_pv_wh)) as rad_err,
                       COUNT(*) as n
                FROM forecast_tracking
                WHERE actual_pv_wh IS NOT NULL
                  AND model_cloud_kwh IS NOT NULL
                GROUP BY date ORDER BY date DESC LIMIT 7
            """).fetchall()
            if tracking:
                status["detail"]["model_accuracy"] = [
                    {"date": r["date"],
                     "cloud_err": round(r["cloud_err"]),
                     "rad_err": round(r["rad_err"]) if r["rad_err"] else None,
                     "hours": r["n"]}
                    for r in tracking
                ]
    except Exception as e:
        log.warning(f"Could not generate detail data: {e}")

    status_path = SCRIPT_DIR / "solar_optimizer_status.json"
    status_path.write_text(json.dumps(status, indent=2))
    log.info(f"Dashboard status written to {status_path}")


def store_plan(db, plan):
    """Store the plan in the database."""
    socs = plan["slot_socs"]
    db.execute("""
        INSERT OR REPLACE INTO daily_plan
        (date, created_at, solar_forecast_kwh, weather_condition, cloud_coverage_pct,
         precipitation_mm, temperature_high, adjusted_solar_kwh, overnight_soc_target,
         energy_deficit_kwh, correction_factor,
         slot1_soc, slot2_soc, slot3_soc, slot4_soc, slot5_soc, slot6_soc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        plan["date"], datetime.now().isoformat(),
        plan["solar_forecast_kwh"], plan["weather_condition"],
        plan["cloud_coverage_pct"], plan["precipitation_mm"],
        plan["temperature_high"], plan["adjusted_solar_kwh"],
        plan["overnight_soc_target"], plan["energy_deficit_kwh"],
        plan["correction_factor"],
        socs[0], socs[1], socs[2], socs[3], socs[4], socs[5],
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Recording Outcomes
# ---------------------------------------------------------------------------

def record_outcome(ha, db):
    """Record today's energy outcomes and run learning."""
    today = date.today().isoformat()

    production = ha.get_sensor_float(SENSORS["daily_production"])
    consumption = ha.get_sensor_float(SENSORS["daily_consumption"])
    grid_bought = ha.get_sensor_float(SENSORS["daily_grid_bought"])
    grid_sold = ha.get_sensor_float(SENSORS["daily_grid_sold"])
    batt_charge = ha.get_sensor_float(SENSORS["daily_battery_charge"])
    batt_discharge = ha.get_sensor_float(SENSORS["daily_battery_discharge"])
    batt_soc = ha.get_sensor_float(SENSORS["battery_soc"])

    # Calculate peak grid usage from differential metering
    # peak_start_grid_bought is recorded by the poll at PEAK_START_HOUR
    peak_start = get_param(db, "peak_start_grid_bought")
    if peak_start is not None and grid_bought is not None:
        peak_grid_kwh = max(0, grid_bought - peak_start)
        log.info(f"Peak grid (metered): {grid_bought:.2f} - {peak_start:.2f} = {peak_grid_kwh:.2f} kWh")
    else:
        peak_grid_kwh = 0
        log.warning("No peak-start grid_bought snapshot, peak grid usage unknown")

    peak_grid_used = 1 if (peak_grid_kwh or 0) > 0.5 else 0

    # Forecast accuracy
    plan = db.execute("SELECT solar_forecast_kwh FROM daily_plan WHERE date=?", (today,)).fetchone()
    forecast_accuracy = None
    if plan and plan["solar_forecast_kwh"] and plan["solar_forecast_kwh"] > 0 and production:
        forecast_accuracy = production / plan["solar_forecast_kwh"]

    weather = ha.get_weather()
    temp_high = weather.get("temperature")

    db.execute("""
        INSERT OR REPLACE INTO daily_outcome
        (date, recorded_at, actual_production_kwh, actual_consumption_kwh,
         grid_bought_kwh, grid_sold_kwh, battery_charge_kwh, battery_discharge_kwh,
         battery_soc_at_record, peak_grid_used, peak_grid_kwh,
         forecast_accuracy, weather_condition, temperature_high)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today, datetime.now().isoformat(),
        production, consumption, grid_bought, grid_sold,
        batt_charge, batt_discharge, batt_soc,
        peak_grid_used, peak_grid_kwh,
        forecast_accuracy, weather["condition"], temp_high,
    ))
    db.commit()

    log.info(f"Recorded outcome for {today}: production={production}, "
             f"consumption={consumption}, grid_bought={grid_bought}, "
             f"peak_grid={peak_grid_kwh:.1f} kWh, peak_used={'YES' if peak_grid_used else 'no'}")

    # Run learning
    update_learning(db)



# ---------------------------------------------------------------------------
# Hourly Polling
# ---------------------------------------------------------------------------

def poll_snapshot(ha, db):
    """Record an hourly snapshot and dynamically adjust overnight charging.

    During the overnight window (00:00–06:30), this calculates the latest
    possible charge start time and appropriate power level so the battery
    reaches its target SOC by 06:30, minimizing time spent at 100%.
    """
    now = datetime.now()
    ts = now.isoformat()
    day = now.date().isoformat()
    hour = now.hour

    battery_soc = ha.get_sensor_float(SENSORS["battery_soc"])
    grid_power = ha.get_sensor_float(SENSORS["grid_power"])
    pv_power = ha.get_sensor_float(SENSORS["pv_power"])
    load_power = ha.get_sensor_float(SENSORS["load_power"])
    grid_bought = ha.get_sensor_float(SENSORS["daily_grid_bought"])
    grid_sold = ha.get_sensor_float(SENSORS["daily_grid_sold"])

    db.execute("""
        INSERT OR REPLACE INTO hourly_log
        (timestamp, date, hour, battery_soc, grid_power_w, pv_power_w,
         load_power_w, grid_bought_kwh, grid_sold_kwh)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ts, day, hour, battery_soc, grid_power, pv_power,
          load_power, grid_bought, grid_sold))
    db.commit()

    log.info(f"Poll: SOC={battery_soc}%, grid={grid_power}W, "
             f"PV={pv_power}W, load={load_power}W")

    # --- Snapshot grid_bought at peak start for accurate peak metering ---
    if hour == PEAK_START_HOUR:
        peak_start_bought = ha.get_sensor_float(SENSORS["daily_grid_bought"])
        if peak_start_bought is not None:
            set_param(db, "peak_start_grid_bought", peak_start_bought)
            log.info(f"Recorded peak-start grid_bought: {peak_start_bought:.2f} kWh")

    # --- Track dual solar forecast models during peak hours ---
    if PEAK_START_HOUR <= hour < PEAK_END_HOUR:
        track_solar_models(ha, db, day, hour, pv_power, battery_soc, load_power)

    # --- Dynamic overnight charge adjustment ---
    adjust_overnight_charging(ha, db, now, battery_soc)


def track_solar_models(ha, db, day, hour, actual_pv_w, battery_soc, load_power_w):
    """Track forecast models and consumption estimates against actuals.

    Records per-hour: both solar model predictions, consumption estimate,
    simulated battery SOC, and actual PV/consumption/SOC for later analysis.
    """
    try:
        raw_solar = ha.get_sensor_float(SENSORS["solar_forecast_today"])
        if raw_solar is None or raw_solar <= 0:
            return

        hourly = get_metocean_hourly(day)
        if not hourly:
            hourly = ha.get_hourly_forecast(day)
        if not hourly:
            return

        # Get this hour's forecast from both models
        solar_cloud = build_hourly_solar(raw_solar, hourly, db)
        solar_rad = build_hourly_solar_radiation(raw_solar, hourly)

        cloud_kwh = solar_cloud.get(hour, 0)
        rad_kwh = (solar_rad or {}).get(hour, 0)
        actual_pv_wh = actual_pv_w  # W ≈ Wh for 1-hour sample

        # Extract weather conditions for this hour from forecast
        hour_fc = next((h for h in hourly if h["hour"] == hour), {})
        wx_condition = hour_fc.get("condition")
        wx_cloud = hour_fc.get("cloud_coverage")
        wx_temp = hour_fc.get("temperature")
        wx_sw = hour_fc.get("shortwave_wm2")

        # Get consumption and battery sim estimates for this hour
        daily_consumption = get_seasonal_consumption(db, day)
        temps = [h["temperature"] for h in hourly if h.get("temperature") is not None]
        avg_temp = sum(temps) / len(temps) if temps else None
        temp_factor = get_temp_factor(db, avg_temp)
        day_factor = get_day_factor(db, day)
        hourly_consumption_map = build_hourly_consumption(daily_consumption, 1.0, temp_factor, day_factor)
        est_consumption = hourly_consumption_map.get(hour, 0)

        # Simulate battery to get estimated SOC at this hour
        active_solar = solar_rad if (solar_rad and sum(solar_rad.values()) > 0) else solar_cloud
        plan_row = db.execute(
            "SELECT overnight_soc_target FROM daily_plan WHERE date=?", (day,)
        ).fetchone()
        starting_soc = plan_row["overnight_soc_target"] if plan_row else 70
        sim = simulate_battery_hourly(starting_soc, active_solar, hourly_consumption_map)
        sim_soc_map = dict(sim["hourly_soc"])
        est_soc = sim_soc_map.get(hour)

        db.execute("""
            INSERT OR REPLACE INTO forecast_tracking
            (date, hour, model_cloud_kwh, model_rad_kwh, actual_pv_wh,
             est_consumption_kwh, est_battery_soc, actual_consumption_wh, actual_battery_soc,
             weather_condition, cloud_pct, temperature, shortwave_wm2)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (day, hour, round(cloud_kwh, 3), round(rad_kwh, 3), actual_pv_wh,
              round(est_consumption, 3), est_soc,
              load_power_w, battery_soc,
              wx_condition, wx_cloud, wx_temp, wx_sw))
        db.commit()

        log.info(f"Model tracking h{hour}: cloud={cloud_kwh:.2f}kWh, "
                 f"rad={rad_kwh:.2f}kWh, actual_pv={actual_pv_wh:.0f}Wh, "
                 f"est_cons={est_consumption:.2f}kWh, actual_load={load_power_w:.0f}W, "
                 f"est_soc={est_soc}%, actual_soc={battery_soc}%, "
                 f"wx={wx_condition}, cloud={wx_cloud}%, sw={wx_sw}W/m²")

    except Exception as e:
        log.warning(f"Model tracking failed: {e}")


def update_model_preference(db):
    """Compare both solar models' accuracy over recent days and update preference.

    Called from update_learning(). Compares cumulative daily error
    (sum of |predicted - actual| across peak hours) for each model.
    Moves preference toward the more accurate model.
    """
    # Get last 7 days of tracking data
    rows = db.execute("""
        SELECT date,
               SUM(ABS(model_cloud_kwh * 1000 - actual_pv_wh)) as cloud_error,
               SUM(ABS(model_rad_kwh * 1000 - actual_pv_wh)) as rad_error,
               COUNT(*) as hours
        FROM forecast_tracking
        WHERE actual_pv_wh IS NOT NULL
          AND model_cloud_kwh IS NOT NULL
          AND model_rad_kwh IS NOT NULL
        GROUP BY date
        ORDER BY date DESC
        LIMIT 7
    """).fetchall()

    if len(rows) < 2:
        log.info("Not enough model tracking data yet (need 2+ days)")
        return

    total_cloud_err = sum(r["cloud_error"] for r in rows)
    total_rad_err = sum(r["rad_error"] for r in rows)
    total_hours = sum(r["hours"] for r in rows)

    if total_cloud_err + total_rad_err == 0:
        return

    # Lower error = better; calculate preference toward radiation (0-1)
    # If rad_err < cloud_err, preference moves toward 1.0
    if total_rad_err < total_cloud_err:
        target_pref = 1.0
    else:
        target_pref = 0.0

    current_pref = get_param(db, "preferred_solar_model") or 0.0
    # Move 30% toward target
    new_pref = current_pref + 0.30 * (target_pref - current_pref)
    new_pref = round(max(0.0, min(1.0, new_pref)), 3)

    avg_cloud = total_cloud_err / total_hours if total_hours > 0 else 0
    avg_rad = total_rad_err / total_hours if total_hours > 0 else 0

    if abs(new_pref - current_pref) > 0.01:
        log.info(f"Model preference: cloud_err={avg_cloud:.0f}Wh/hr, "
                 f"rad_err={avg_rad:.0f}Wh/hr, "
                 f"pref {current_pref:.2f} -> {new_pref:.2f} "
                 f"(active={'radiation' if new_pref >= 0.5 else 'cloud'}, "
                 f"n={total_hours}h over {len(rows)} days)")
        set_param(db, "preferred_solar_model", new_pref)
    else:
        active = "radiation" if new_pref >= 0.5 else "cloud"
        log.info(f"Model preference unchanged at {new_pref:.2f} ({active}), "
                 f"cloud_err={avg_cloud:.0f}, rad_err={avg_rad:.0f}")


def adjust_overnight_charging(ha, db, now, current_soc):
    """Dynamically adjust charge start time and power to hit target by 06:30.

    Called every hour by the poll cron. Only acts during 00:00–06:30.
    Writes Slot 1/2 time, SOC, and power registers to delay charging as
    late as possible while still reaching the target SOC by the deadline.
    """
    deadline_hour = CHARGE_DEADLINE_HOUR
    deadline_min = CHARGE_DEADLINE_MIN
    deadline_minutes = deadline_hour * 60 + deadline_min  # 390 min from midnight

    now_minutes = now.hour * 60 + now.minute

    # Only adjust during the overnight window (midnight to deadline)
    if now_minutes >= deadline_minutes or now.hour >= PEAK_START_HOUR:
        return

    if current_soc is None:
        log.warning("Cannot adjust charging: battery SOC unavailable")
        return

    # Get today's plan (optimize runs at 22:00 for tomorrow, so today's
    # date in the plan matches what was written last night)
    today = now.date().isoformat()
    plan_row = db.execute(
        "SELECT overnight_soc_target FROM daily_plan WHERE date=?", (today,)
    ).fetchone()

    if not plan_row:
        log.info("No plan found for today, skipping charge adjustment")
        return

    target_soc = plan_row["overnight_soc_target"]

    # Already at or above target — ensure slots are set to not charge
    if current_soc >= target_soc:
        log.info(f"SOC {current_soc}% >= target {target_soc}%, "
                 f"no charging needed")
        _write_charge_slots(ha, reserve_only=True)
        return

    # Calculate energy needed (kWh)
    soc_needed = target_soc - current_soc
    energy_kwh = (soc_needed / 100.0) * USABLE_CAPACITY_KWH

    # Time remaining until deadline (hours)
    minutes_left = deadline_minutes - now_minutes
    hours_left = minutes_left / 60.0

    # Calculate charge time at default power (3kW), then determine
    # the latest start time
    charge_hours_at_default = energy_kwh / (CHARGE_POWER_DEFAULT / 1000.0)

    # Check if we can even make it at max power
    charge_hours_at_max = energy_kwh / (CHARGE_POWER_MAX / 1000.0)

    if charge_hours_at_max > hours_left:
        # Can't make it even at max power — start NOW at max
        log.warning(f"Tight deadline! Need {energy_kwh:.1f}kWh in {hours_left:.1f}h, "
                    f"even {CHARGE_POWER_MAX}W needs {charge_hours_at_max:.1f}h. "
                    f"Starting immediately at max power.")
        charge_power = CHARGE_POWER_MAX
        start_hour = now.hour
        start_min = 0
    elif charge_hours_at_default <= hours_left:
        # Plenty of time at default power — delay start
        charge_power = CHARGE_POWER_DEFAULT
        # Add 15 min buffer for ramp-up and register write delays
        buffer_minutes = 15
        start_minutes = deadline_minutes - int(charge_hours_at_default * 60) - buffer_minutes
        # Don't start before now
        start_minutes = max(start_minutes, now_minutes)
        start_hour = start_minutes // 60
        start_min = start_minutes % 60
    else:
        # Not enough time at default but OK at higher power — calculate
        # the minimum power needed, then add margin
        min_power_kw = energy_kwh / hours_left
        # Add 20% margin and round up to nearest 500W
        charge_power_w = min_power_kw * 1000 * 1.2
        charge_power = min(CHARGE_POWER_MAX,
                           int((charge_power_w + 499) // 500) * 500)
        # Recalculate charge time at this power
        charge_hours = energy_kwh / (charge_power / 1000.0)
        buffer_minutes = 15
        start_minutes = deadline_minutes - int(charge_hours * 60) - buffer_minutes
        start_minutes = max(start_minutes, now_minutes)
        start_hour = start_minutes // 60
        start_min = start_minutes % 60

    # Round start_min to nearest 15 (Deye register precision is 1 min,
    # but cleaner for logging; round DOWN to start slightly earlier)
    start_min = (start_min // 15) * 15

    # Refresh weather forecast and re-evaluate target if conditions changed
    original_target = target_soc
    target_soc = _maybe_revise_target(ha, db, today, target_soc, current_soc)

    # Update the DB plan if target was revised so dashboard reflects it
    if target_soc != original_target:
        db.execute(
            "UPDATE daily_plan SET overnight_soc_target=?, slot1_soc=?, slot2_soc=? WHERE date=?",
            (target_soc, OUTAGE_RESERVE_PCT, target_soc, today))
        db.commit()

    # Recalculate if target changed
    soc_needed_revised = target_soc - current_soc
    if soc_needed_revised <= 0:
        log.info(f"Revised target {target_soc}% already met at SOC {current_soc}%")
        _write_charge_slots(ha, reserve_only=True)
        return
    if soc_needed_revised != soc_needed:
        # Recalculate with revised target
        energy_kwh = (soc_needed_revised / 100.0) * USABLE_CAPACITY_KWH
        charge_hours = energy_kwh / (charge_power / 1000.0)
        buffer_minutes = 15
        start_minutes = deadline_minutes - int(charge_hours * 60) - buffer_minutes
        start_minutes = max(start_minutes, now_minutes)
        start_hour = start_minutes // 60
        start_min = (start_minutes % 60 // 15) * 15

    log.info(f"Charge adjustment: SOC={current_soc}% -> {target_soc}%, "
             f"need {energy_kwh:.1f}kWh, power={charge_power}W, "
             f"start={start_hour:02d}:{start_min:02d}, "
             f"deadline={deadline_hour:02d}:{deadline_min:02d}")

    _write_charge_slots(ha, reserve_only=False, target_soc=target_soc,
                        start_hour=start_hour, start_min=start_min,
                        charge_power=charge_power)


def _maybe_revise_target(ha, db, today, current_target, current_soc):
    """Revise SOC target using hourly battery simulation.

    Uses MetOcean (or HA fallback) hourly forecast to simulate the battery
    hour-by-hour through peak hours. If the simulation shows the battery
    hitting reserve, the target is raised. Only lowers the target if the
    simulation shows significant headroom.

    Returns the (possibly revised) target SOC.
    """
    try:
        raw_solar = ha.get_sensor_float(SENSORS["solar_forecast_today"])
        if raw_solar is None or raw_solar <= 0:
            return current_target

        hourly = get_metocean_hourly(today)
        if not hourly:
            hourly = ha.get_hourly_forecast(today)
        if not hourly:
            log.info("No hourly forecast available, keeping target")
            return current_target

        solar_cloud = build_hourly_solar(raw_solar, hourly, db)
        solar_rad = build_hourly_solar_radiation(raw_solar, hourly)

        # Prefer radiation model when shortwave data is available
        if solar_rad and sum(solar_rad.values()) > 0:
            hourly_solar_map = solar_rad
            active_model = "radiation"
        else:
            hourly_solar_map = solar_cloud
            active_model = "cloud"

        if sum(hourly_solar_map.values()) <= 0:
            return current_target

        daily_consumption = get_seasonal_consumption(db, today)
        temps = [h["temperature"] for h in hourly if h.get("temperature") is not None]
        avg_temp = sum(temps) / len(temps) if temps else None
        temp_factor = get_temp_factor(db, avg_temp)

        day_factor = get_day_factor(db, today)
        hourly_consumption_map = build_hourly_consumption(daily_consumption, 1.0, temp_factor, day_factor)

        # Binary search for minimum viable SOC — same as calculate_plan
        safety_margin = get_param(db, "safety_margin_pct")
        reserve_target = BATTERY_RESERVE_PCT + safety_margin

        lo, hi = BATTERY_RESERVE_PCT, 100
        for _ in range(15):
            mid = (lo + hi) / 2.0
            sim = simulate_battery_hourly(mid, hourly_solar_map, hourly_consumption_map)
            if sim["min_soc"] >= reserve_target:
                hi = mid
            else:
                lo = mid
            if hi - lo < 1:
                break

        revised = int(((hi + 4) // 5) * 5)
        sim = simulate_battery_hourly(revised, hourly_solar_map, hourly_consumption_map)

        min_soc = get_param(db, "min_overnight_soc")
        max_soc = get_param(db, "max_overnight_soc")
        revised = max(min_soc, min(max_soc, revised))
        revised = int(round(revised / 5) * 5)

        cloud_total = sum(solar_cloud.values())
        rad_total = sum(solar_rad.values()) if solar_rad else 0
        log.info(f"Revision sim ({active_model}): solar cloud={cloud_total:.1f}kWh, "
                 f"rad={rad_total:.1f}kWh, "
                 f"min_soc={sim['min_soc']}% at {sim['min_soc_hour']}:00, "
                 f"optimal={revised}%, current_target={current_target}%")
        if revised != current_target:
            log.info(f"Target revised: {current_target}% -> {revised}%")
        return revised

    except Exception as e:
        log.warning(f"Weather revision failed, keeping target {current_target}%: {e}")
        return current_target


def _write_charge_slots(ha, reserve_only=False, target_soc=None,
                        start_hour=None, start_min=None, charge_power=None):
    """Write Slot 1 and Slot 2 registers to control overnight charging.

    If reserve_only=True, both slots are set to reserve SOC (no charging).
    Otherwise, Slot 1 covers 00:00 to start_hour:start_min at reserve,
    and Slot 2 covers start_hour:start_min to 07:00 at target_soc.
    """
    writes = []

    if reserve_only:
        # Both slots at outage reserve, default power, original times
        writes.append((TOU_REGS["soc"][0], OUTAGE_RESERVE_PCT))
        writes.append((TOU_REGS["soc"][1], OUTAGE_RESERVE_PCT))
        writes.append((TOU_REGS["power"][0], CHARGE_POWER_DEFAULT))
        writes.append((TOU_REGS["power"][1], CHARGE_POWER_DEFAULT))
        # Reset times to defaults
        writes.append((TOU_REGS["time"][0], encode_time(0, 0)))
        writes.append((TOU_REGS["time"][1], encode_time(4, 0)))
    else:
        # Slot 1: 00:00 to charge start — outage reserve (30% safety floor)
        writes.append((TOU_REGS["time"][0], encode_time(0, 0)))
        writes.append((TOU_REGS["soc"][0], OUTAGE_RESERVE_PCT))
        writes.append((TOU_REGS["power"][0], CHARGE_POWER_DEFAULT))

        # Slot 2: charge start to 07:00 — target SOC at calculated power
        writes.append((TOU_REGS["time"][1], encode_time(start_hour, start_min)))
        writes.append((TOU_REGS["soc"][1], target_soc))
        writes.append((TOU_REGS["power"][1], charge_power))

    failed = []
    for register, value in writes:
        for attempt in range(3):
            try:
                ha.write_register(register, value)
                time.sleep(0.5)
                break
            except Exception as e:
                log.warning(f"Write register {register}={value} "
                            f"attempt {attempt+1} failed: {e}")
                if attempt == 2:
                    failed.append((register, value, str(e)))
                time.sleep(1)

    if failed:
        log.error(f"Failed to write {len(failed)} charge slot registers: {failed}")
    else:
        if reserve_only:
            log.info("Charge slots set to reserve (no charging needed)")
        else:
            log.info(f"Charge slots updated: start={start_hour:02d}:{start_min:02d}, "
                     f"SOC={target_soc}%, power={charge_power}W")


# ---------------------------------------------------------------------------
# Self-Learning
# ---------------------------------------------------------------------------

def update_learning(db):
    """Adjust learning parameters based on recent outcomes."""
    # Get last 14 days of outcomes joined with plans
    rows = db.execute("""
        SELECT o.*, p.solar_forecast_kwh, p.overnight_soc_target
        FROM daily_outcome o
        LEFT JOIN daily_plan p ON o.date = p.date
        ORDER BY o.date DESC
        LIMIT 14
    """).fetchall()

    if len(rows) < 3:
        log.info("Not enough history for learning (need 3+ days)")
        return

    learning_rate = get_param(db, "learning_rate")

    # --- Adjustment 1: SOC target based on peak grid usage ---
    recent_7 = rows[:7]
    peak_grid_days = sum(1 for r in recent_7 if r["peak_grid_used"])
    good_days = sum(1 for r in recent_7
                    if not r["peak_grid_used"]
                    and (r["battery_soc_at_record"] or 0) > 30)

    current_base_soc = get_param(db, "base_overnight_soc")
    min_soc = get_param(db, "min_overnight_soc")

    if peak_grid_days >= 3:
        new_soc = min(100, current_base_soc + learning_rate * 2)
        log.info(f"Learning: peak grid used {peak_grid_days}/7 days, "
                 f"increasing base SOC {current_base_soc:.0f} -> {new_soc:.0f}")
        set_param(db, "base_overnight_soc", new_soc)
    elif peak_grid_days >= 1:
        new_soc = min(100, current_base_soc + learning_rate)
        log.info(f"Learning: peak grid used {peak_grid_days}/7 days, "
                 f"nudging base SOC {current_base_soc:.0f} -> {new_soc:.0f}")
        set_param(db, "base_overnight_soc", new_soc)
    elif good_days >= 5 and current_base_soc > min_soc:
        new_soc = max(min_soc, current_base_soc - learning_rate)
        log.info(f"Learning: {good_days}/7 good days, "
                 f"decreasing base SOC {current_base_soc:.0f} -> {new_soc:.0f}")
        set_param(db, "base_overnight_soc", new_soc)
    else:
        log.info(f"Learning: base SOC unchanged at {current_base_soc:.0f}%")

    # --- Adjustment 2: Weather correction factors ---
    for condition in ["sunny", "cloudy", "partlycloudy", "rainy"]:
        condition_days = [r for r in rows
                         if r["weather_condition"] == condition
                         and r["forecast_accuracy"] is not None]
        if len(condition_days) >= 3:
            avg_accuracy = sum(r["forecast_accuracy"] for r in condition_days) / len(condition_days)
            param_key = f"{condition}_correction"
            current = get_param(db, param_key)
            # Move 20% toward measured accuracy
            new_val = current + 0.20 * (avg_accuracy - current)
            new_val = max(0.2, min(1.3, round(new_val, 3)))
            if abs(new_val - current) > 0.005:
                log.info(f"Learning: {condition} correction {current:.3f} -> {new_val:.3f} "
                         f"(avg accuracy: {avg_accuracy:.2f}, n={len(condition_days)})")
                set_param(db, param_key, new_val)

    # --- Adjustment 3: Daily consumption average (legacy — kept for comparison) ---
    consumption_vals = [r["actual_consumption_kwh"] for r in rows
                        if r["actual_consumption_kwh"] is not None]
    if len(consumption_vals) >= 7:
        rolling_avg = sum(consumption_vals) / len(consumption_vals)
        current_avg = get_param(db, "daily_consumption_avg")
        new_avg = current_avg + 0.30 * (rolling_avg - current_avg)
        new_avg = round(new_avg, 1)
        if abs(new_avg - current_avg) > 0.1:
            log.info(f"Learning: consumption avg {current_avg:.1f} -> {new_avg:.1f} kWh")
            set_param(db, "daily_consumption_avg", new_avg)

    # --- Adjustment 3b: Seasonal consumption averages ---
    # Group outcomes by NZ season and update each season's average
    consumption_rows = [r for r in rows if r["actual_consumption_kwh"] is not None]
    season_groups = {}
    for r in consumption_rows:
        month = datetime.strptime(r["date"], "%Y-%m-%d").month
        s = NZ_SEASONS[month]
        season_groups.setdefault(s, []).append(r["actual_consumption_kwh"])
    for s, vals in season_groups.items():
        if len(vals) >= 3:
            rolling_avg = sum(vals) / len(vals)
            param_key = f"consumption_avg_{s}"
            current_avg = get_param(db, param_key)
            new_avg = current_avg + 0.30 * (rolling_avg - current_avg)
            new_avg = round(new_avg, 1)
            if abs(new_avg - current_avg) > 0.1:
                log.info(f"Learning: {param_key} {current_avg:.1f} -> {new_avg:.1f} kWh "
                         f"(n={len(vals)})")
                set_param(db, param_key, new_avg)
    # Year average — across all seasons
    all_consumption = [r["actual_consumption_kwh"] for r in consumption_rows]
    if len(all_consumption) >= 7:
        rolling_avg = sum(all_consumption) / len(all_consumption)
        current_avg = get_param(db, "consumption_avg_year")
        new_avg = current_avg + 0.30 * (rolling_avg - current_avg)
        new_avg = round(new_avg, 1)
        if abs(new_avg - current_avg) > 0.1:
            log.info(f"Learning: consumption_avg_year {current_avg:.1f} -> {new_avg:.1f} kWh")
            set_param(db, "consumption_avg_year", new_avg)

    # --- Adjustment 4: Temperature-based consumption factors ---
    # Group days by temperature band and compare actual vs expected consumption
    temp_rows = [r for r in rows
                 if r["actual_consumption_kwh"] is not None
                 and r["temperature_high"] is not None]
    current_consumption_avg = get_param(db, "daily_consumption_avg")

    for threshold, param_key in TEMP_BANDS:
        if threshold == 99:
            band_days = [r for r in temp_rows if r["temperature_high"] >= 20]
        else:
            prev_threshold = 0
            for t, _ in TEMP_BANDS:
                if t == threshold:
                    break
                prev_threshold = t
            band_days = [r for r in temp_rows
                         if r["temperature_high"] >= prev_threshold
                         and r["temperature_high"] < threshold]

        if len(band_days) >= 2:
            # Actual consumption ratio vs base average
            avg_consumption = sum(r["actual_consumption_kwh"] for r in band_days) / len(band_days)
            actual_ratio = avg_consumption / current_consumption_avg if current_consumption_avg > 0 else 1.0
            current_factor = get_param(db, param_key)
            # Move 20% toward measured ratio
            new_factor = current_factor + 0.20 * (actual_ratio - current_factor)
            new_factor = max(0.5, min(2.0, round(new_factor, 3)))
            if abs(new_factor - current_factor) > 0.005:
                log.info(f"Learning: {param_key} {current_factor:.3f} -> {new_factor:.3f} "
                         f"(avg consumption: {avg_consumption:.1f} kWh at "
                         f"{'<'+str(threshold) if threshold < 99 else '>=20'}°C, "
                         f"n={len(band_days)})")
                set_param(db, param_key, new_factor)

    # --- Adjustment 5: Weekend vs weekday consumption factor ---
    current_avg = get_param(db, "daily_consumption_avg")
    if current_avg and current_avg > 0:
        weekend_rows = [r for r in rows
                        if r["actual_consumption_kwh"] is not None
                        and datetime.strptime(r["date"], "%Y-%m-%d").date().weekday() in WEEKEND_DAYS]
        weekday_rows = [r for r in rows
                        if r["actual_consumption_kwh"] is not None
                        and datetime.strptime(r["date"], "%Y-%m-%d").date().weekday() not in WEEKEND_DAYS]

        if len(weekend_rows) >= 2:
            avg_weekend = sum(r["actual_consumption_kwh"] for r in weekend_rows) / len(weekend_rows)
            actual_ratio = avg_weekend / current_avg
            current_factor = get_param(db, "weekend_factor")
            new_factor = current_factor + 0.20 * (actual_ratio - current_factor)
            new_factor = max(0.8, min(2.0, round(new_factor, 3)))
            if abs(new_factor - current_factor) > 0.01:
                log.info(f"Learning: weekend_factor {current_factor:.2f} -> {new_factor:.2f} "
                         f"(avg {avg_weekend:.1f} kWh, n={len(weekend_rows)})")
                set_param(db, "weekend_factor", new_factor)

        if len(weekday_rows) >= 3:
            avg_weekday = sum(r["actual_consumption_kwh"] for r in weekday_rows) / len(weekday_rows)
            actual_ratio = avg_weekday / current_avg
            current_factor = get_param(db, "weekday_factor")
            new_factor = current_factor + 0.20 * (actual_ratio - current_factor)
            new_factor = max(0.5, min(1.5, round(new_factor, 3)))
            if abs(new_factor - current_factor) > 0.01:
                log.info(f"Learning: weekday_factor {current_factor:.2f} -> {new_factor:.2f} "
                         f"(avg {avg_weekday:.1f} kWh, n={len(weekday_rows)})")
                set_param(db, "weekday_factor", new_factor)

    # --- Adjustment 6: Solar model preference ---
    update_model_preference(db)


# ---------------------------------------------------------------------------
# Display / Status
# ---------------------------------------------------------------------------

def show_status(ha, db):
    """Show current optimizer status."""
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    batt_soc = ha.get_sensor_float(SENSORS["battery_soc"])
    solar_today = ha.get_sensor_float(SENSORS["solar_forecast_today"])
    solar_tomorrow = ha.get_sensor_float(SENSORS["solar_forecast_tomorrow"])
    weather = ha.get_weather()

    plan_today = db.execute("SELECT * FROM daily_plan WHERE date=?", (today,)).fetchone()
    plan_tomorrow = db.execute("SELECT * FROM daily_plan WHERE date=?", (tomorrow,)).fetchone()
    outcome_today = db.execute("SELECT * FROM daily_outcome WHERE date=?", (today,)).fetchone()

    print(f"\n{'='*60}")
    print(f"  Solar Optimizer Status - {today}")
    print(f"{'='*60}")
    print(f"  Battery SOC:            {batt_soc}%")
    print(f"  Weather:                {weather['condition']} ({weather['cloud_coverage']}% cloud)")
    print(f"  Solar forecast today:   {solar_today} kWh")
    print(f"  Solar forecast tomorrow:{solar_tomorrow} kWh")

    if plan_today:
        print(f"\n  Today's Plan:")
        print(f"    Overnight SOC target: {plan_today['overnight_soc_target']}%")
        print(f"    Adjusted solar:       {plan_today['adjusted_solar_kwh']:.1f} kWh")
        print(f"    Energy deficit:       {plan_today['energy_deficit_kwh']:.1f} kWh")

    if plan_tomorrow:
        print(f"\n  Tomorrow's Plan:")
        print(f"    Overnight SOC target: {plan_tomorrow['overnight_soc_target']}%")
        print(f"    Adjusted solar:       {plan_tomorrow['adjusted_solar_kwh']:.1f} kWh")

    if outcome_today:
        print(f"\n  Today's Outcome:")
        print(f"    Production:           {outcome_today['actual_production_kwh']} kWh")
        print(f"    Consumption:          {outcome_today['actual_consumption_kwh']} kWh")
        print(f"    Grid bought:          {outcome_today['grid_bought_kwh']} kWh")
        print(f"    Peak grid used:       {'YES' if outcome_today['peak_grid_used'] else 'No'}")

    print(f"\n  Learning Parameters:")
    params = db.execute("SELECT * FROM learning_params ORDER BY param_key").fetchall()
    for p in params:
        print(f"    {p['param_key']:30s} = {p['param_value']}")

    print(f"{'='*60}\n")


def show_history(db, days=14):
    """Show recent history of plans vs outcomes."""
    rows = db.execute("""
        SELECT p.date, p.solar_forecast_kwh, p.overnight_soc_target,
               p.weather_condition AS plan_weather, p.adjusted_solar_kwh,
               o.actual_production_kwh, o.actual_consumption_kwh,
               o.grid_bought_kwh, o.peak_grid_kwh, o.peak_grid_used,
               o.forecast_accuracy
        FROM daily_plan p
        LEFT JOIN daily_outcome o ON p.date = o.date
        ORDER BY p.date DESC
        LIMIT ?
    """, (days,)).fetchall()

    if not rows:
        print("No history available yet.")
        return

    print(f"\n{'Date':<12} {'Forecast':>8} {'Actual':>8} {'Weather':<14} "
          f"{'SOC%':>5} {'Grid':>6} {'Peak':>6} {'Accuracy':>8}")
    print("-" * 80)

    for r in rows:
        actual = f"{r['actual_production_kwh']:.1f}" if r['actual_production_kwh'] else "-"
        grid = f"{r['grid_bought_kwh']:.1f}" if r['grid_bought_kwh'] else "-"
        peak = f"{r['peak_grid_kwh']:.1f}" if r['peak_grid_kwh'] is not None else "-"
        acc = f"{r['forecast_accuracy']:.2f}" if r['forecast_accuracy'] else "-"
        peak_flag = " *" if r['peak_grid_used'] else ""

        print(f"{r['date']:<12} {r['solar_forecast_kwh']:>8.1f} {actual:>8} "
              f"{r['plan_weather'] or '-':<14} {r['overnight_soc_target']:>4}% "
              f"{grid:>6} {peak:>5}{peak_flag} {acc:>8}")

    print(f"\n  * = peak grid usage detected")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1].lower()
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    setup_logging(verbose)
    load_env()

    server = os.environ["HASS_SERVER"]
    token = os.environ["HASS_TOKEN"]
    ha = HomeAssistantAPI(server, token)
    db = get_db()

    try:
        if mode == "optimize":
            plan = calculate_plan(ha, db)
            success = write_tou_config(ha, plan)
            store_plan(db, plan)
            # Let write_dashboard_status auto-select which plan to show
            # (today's during the day, tomorrow's after 9pm) rather than
            # forcing the new plan which would wipe today's actuals
            write_dashboard_status(ha, db)
            print(f"Optimization complete for {plan['date']}:")
            print(f"  Overnight SOC target: {plan['overnight_soc_target']}%")
            print(f"  Solar forecast: {plan['solar_forecast_kwh']:.1f} kWh "
                  f"(adjusted: {plan['adjusted_solar_kwh']:.1f} kWh)")
            print(f"  Weather: {plan['weather_condition']}")
            print(f"  Energy deficit: {plan['energy_deficit_kwh']:.1f} kWh")
            print(f"  Registers written: {'OK' if success else 'SOME FAILURES'}")

        elif mode == "dry-run":
            plan = calculate_plan(ha, db)
            print(f"\nDRY RUN - Plan for {plan['date']}:")
            print(f"  Solar forecast:       {plan['solar_forecast_kwh']:.1f} kWh")
            print(f"  Weather:              {plan['weather_condition']} "
                  f"({plan['cloud_coverage_pct']}% cloud)")
            print(f"  Correction factor:    {plan['correction_factor']:.2f}")
            print(f"  Adjusted solar:       {plan['adjusted_solar_kwh']:.1f} kWh")
            print(f"  Energy deficit:       {plan['energy_deficit_kwh']:.1f} kWh")
            print(f"  Overnight SOC target: {plan['overnight_soc_target']}%")
            print(f"\n  Slot SOCs: {plan['slot_socs']}")
            print(f"\n  Register writes that would be made:")
            for i, (h, m) in enumerate(SLOT_TIMES):
                soc = plan['slot_socs'][i]
                gc = "YES" if SLOT_ENABLES[i] & 1 else "no"
                print(f"    Slot {i+1}: time={h:02d}:{m:02d} "
                      f"SOC={soc}% power={SLOT_POWERS[i]}W "
                      f"grid_charge={gc}")

        elif mode == "record":
            backup_db()
            record_outcome(ha, db)
            write_dashboard_status(ha, db)

        elif mode == "poll":
            poll_snapshot(ha, db)
            write_dashboard_status(ha, db)

        elif mode == "status":
            show_status(ha, db)

        elif mode == "history":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 14
            show_history(db, days)

        elif mode == "set-param":
            if len(sys.argv) < 4:
                print("Usage: solar_optimizer.py set-param KEY VALUE")
                sys.exit(1)
            key = sys.argv[2]
            value = float(sys.argv[3])
            old = get_param(db, key)
            set_param(db, key, value)
            print(f"Set {key}: {old} -> {value}")

        elif mode == "reset":
            now = datetime.now().isoformat()
            for key, val in DEFAULT_PARAMS.items():
                db.execute(
                    "INSERT OR REPLACE INTO learning_params (param_key, param_value, updated_at) VALUES (?, ?, ?)",
                    (key, val, now),
                )
            db.commit()
            print("Learning parameters reset to defaults")

        else:
            print(f"Unknown mode: {mode}")
            print(__doc__)
            sys.exit(1)

    except Exception as e:
        log.error(f"Fatal error in {mode}: {e}", exc_info=True)
        if mode == "optimize":
            log.warning("Attempting failsafe write...")
            try:
                plan = make_failsafe_plan(f"error: {e}")
                write_tou_config(ha, plan)
                store_plan(db, plan)
            except Exception as e2:
                log.critical(f"Failsafe also failed: {e2}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
