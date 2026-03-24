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

# Battery
BATTERY_CAPACITY_KWH = 15.0
BATTERY_RESERVE_PCT = 10
USABLE_CAPACITY_KWH = BATTERY_CAPACITY_KWH * (100 - BATTERY_RESERVE_PCT) / 100  # 13.5

# Peak hours (inclusive)
PEAK_START_HOUR = 7   # 7am
PEAK_END_HOUR = 21    # 9pm

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
# Off-peak slots (1,2,6) get grid charge, peak slots (3,4,5) get none
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

# Seasonal consumption factors (NZ seasons)
SEASONAL_FACTOR = {
    1: 0.85, 2: 0.90, 3: 0.95, 4: 1.05, 5: 1.10, 6: 1.15,
    7: 1.15, 8: 1.10, 9: 1.05, 10: 0.95, 11: 0.90, 12: 0.85,
}

# Default learning parameters
DEFAULT_PARAMS = {
    "base_overnight_soc": 60.0,
    "sunny_correction": 1.0,
    "cloudy_correction": 0.70,
    "partlycloudy_correction": 0.85,
    "rainy_correction": 0.45,
    "learning_rate": 2.0,
    "daily_consumption_avg": 33.0,
    "min_overnight_soc": 30.0,
    "max_overnight_soc": 100.0,
    "safety_soc_floor": 15.0,
    "peak_consumption_ratio": 0.70,   # fraction of daily consumption during peak
    "peak_solar_ratio": 0.85,         # fraction of daily solar during peak
    "safety_margin_pct": 10.0,        # added to calculated SOC
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
# Database
# ---------------------------------------------------------------------------

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
    """)
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


def set_param(db, key, value):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT OR REPLACE INTO learning_params (param_key, param_value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )
    db.commit()


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

    # Step 3: Apply weather correction
    # Normalize weather condition to our correction keys
    CONDITION_MAP = {
        "sunny": "sunny",
        "clear-night": "sunny",
        "clear": "sunny",
        "partlycloudy": "partlycloudy",
        "partly-cloudy": "partlycloudy",
        "cloudy": "cloudy",
        "fog": "cloudy",
        "rainy": "rainy",
        "pouring": "rainy",
        "snowy": "rainy",
        "lightning": "rainy",
        "lightning-rainy": "rainy",
        "hail": "rainy",
        "windy": "partlycloudy",
        "windy-variant": "partlycloudy",
        "exceptional": "cloudy",
    }
    normalized = CONDITION_MAP.get(condition, None)
    if normalized:
        correction_key = f"{normalized}_correction"
    else:
        correction_key = f"{condition}_correction"

    base_correction = get_param(db, correction_key)
    if base_correction is None:
        log.warning(f"Unknown weather condition '{condition}', using 0.6 correction")
        base_correction = 0.6

    cloud_modifier = 1.0 - (cloud_pct / 100.0) * 0.5
    correction_factor = base_correction * cloud_modifier
    adjusted_solar = raw_solar * correction_factor

    log.info(f"Solar forecast: {raw_solar:.1f} kWh, condition: {condition}, "
             f"cloud: {cloud_pct}%, correction: {correction_factor:.2f}, "
             f"adjusted: {adjusted_solar:.1f} kWh")

    # Step 4: Estimate peak-hours energy balance
    daily_consumption = get_param(db, "daily_consumption_avg")
    seasonal = SEASONAL_FACTOR.get(date.today().month, 1.0)
    peak_ratio = get_param(db, "peak_consumption_ratio")
    solar_ratio = get_param(db, "peak_solar_ratio")

    peak_consumption = daily_consumption * peak_ratio * seasonal
    peak_solar = adjusted_solar * solar_ratio
    energy_deficit = max(0, peak_consumption - peak_solar)

    log.info(f"Peak consumption: {peak_consumption:.1f} kWh, "
             f"peak solar: {peak_solar:.1f} kWh, "
             f"deficit: {energy_deficit:.1f} kWh")

    # Step 5: Convert deficit to SOC target
    soc_from_deficit = (energy_deficit / USABLE_CAPACITY_KWH) * 100
    safety_margin = get_param(db, "safety_margin_pct")
    raw_soc_target = soc_from_deficit + safety_margin

    base_soc = get_param(db, "base_overnight_soc")
    overnight_soc = max(raw_soc_target, base_soc)

    min_soc = get_param(db, "min_overnight_soc")
    max_soc = get_param(db, "max_overnight_soc")
    overnight_soc = max(min_soc, min(max_soc, overnight_soc))

    # Round to nearest 5
    overnight_soc = int(round(overnight_soc / 5) * 5)

    log.info(f"SOC target: deficit-based={raw_soc_target:.0f}%, "
             f"learned-base={base_soc:.0f}%, final={overnight_soc}%")

    # Build slot SOC values
    slot_socs = [
        overnight_soc,        # slot 1: 00:00-04:00 overnight charge
        overnight_soc,        # slot 2: 04:00-07:00 early morning charge
        BATTERY_RESERVE_PCT,  # slot 3: 07:00-11:00 peak morning
        BATTERY_RESERVE_PCT,  # slot 4: 11:00-15:00 solar peak
        BATTERY_RESERVE_PCT,  # slot 5: 15:00-21:00 afternoon peak
        overnight_soc,        # slot 6: 21:00-00:00 evening off-peak charge
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
        "slot_socs": [FAILSAFE_OVERNIGHT_SOC, FAILSAFE_OVERNIGHT_SOC,
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

    # Tomorrow's plan
    p = plan
    if not p:
        row = db.execute("SELECT * FROM daily_plan WHERE date=?", (tomorrow,)).fetchone()
        if row:
            p = dict(row)
    if p:
        status["plan"] = {
            "date": p.get("date", tomorrow),
            "solar_forecast": p.get("solar_forecast_kwh"),
            "adjusted_solar": p.get("adjusted_solar_kwh"),
            "weather": p.get("weather_condition"),
            "cloud_pct": p.get("cloud_coverage_pct"),
            "overnight_soc": p.get("overnight_soc_target"),
            "deficit": p.get("energy_deficit_kwh"),
            "correction": p.get("correction_factor"),
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

    # Learning params summary
    status["learning"] = {
        "base_soc": get_param(db, "base_overnight_soc"),
        "consumption_avg": get_param(db, "daily_consumption_avg"),
        "sunny_corr": get_param(db, "sunny_correction"),
        "cloudy_corr": get_param(db, "cloudy_correction"),
        "partly_corr": get_param(db, "partlycloudy_correction"),
        "rainy_corr": get_param(db, "rainy_correction"),
    }

    # Days of data
    days = db.execute("SELECT COUNT(*) FROM daily_outcome").fetchone()[0]
    status["days_of_data"] = days

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

    # Estimate peak grid usage from hourly logs
    peak_grid_kwh = estimate_peak_grid(db, today)

    # Fallback heuristic if no hourly data
    if peak_grid_kwh is None and grid_bought is not None:
        # Estimate: overnight off-peak grid ~ baseload * 10hrs * 0.5kW = 5kWh
        expected_offpeak = 5.0
        peak_grid_kwh = max(0, (grid_bought or 0) - expected_offpeak)

    peak_grid_used = 1 if (peak_grid_kwh or 0) > 0.5 else 0

    # Forecast accuracy
    plan = db.execute("SELECT solar_forecast_kwh FROM daily_plan WHERE date=?", (today,)).fetchone()
    forecast_accuracy = None
    if plan and plan["solar_forecast_kwh"] and plan["solar_forecast_kwh"] > 0 and production:
        forecast_accuracy = production / plan["solar_forecast_kwh"]

    weather = ha.get_weather()

    db.execute("""
        INSERT OR REPLACE INTO daily_outcome
        (date, recorded_at, actual_production_kwh, actual_consumption_kwh,
         grid_bought_kwh, grid_sold_kwh, battery_charge_kwh, battery_discharge_kwh,
         battery_soc_at_record, peak_grid_used, peak_grid_kwh,
         forecast_accuracy, weather_condition)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today, datetime.now().isoformat(),
        production, consumption, grid_bought, grid_sold,
        batt_charge, batt_discharge, batt_soc,
        peak_grid_used, peak_grid_kwh,
        forecast_accuracy, weather["condition"],
    ))
    db.commit()

    log.info(f"Recorded outcome for {today}: production={production}, "
             f"consumption={consumption}, grid_bought={grid_bought}, "
             f"peak_grid={peak_grid_kwh:.1f} kWh, peak_used={'YES' if peak_grid_used else 'no'}")

    # Run learning
    update_learning(db)


def estimate_peak_grid(db, day):
    """Estimate grid usage during peak hours from hourly logs."""
    rows = db.execute("""
        SELECT hour, grid_power_w FROM hourly_log
        WHERE date=? AND hour >= ? AND hour < ?
        ORDER BY hour
    """, (day, PEAK_START_HOUR, PEAK_END_HOUR)).fetchall()

    if not rows or len(rows) < 4:
        return None  # Not enough hourly data

    # Sum positive grid power (importing) across peak hours
    # Each sample represents ~1 hour, so W ≈ Wh for that hour
    total_wh = 0
    for row in rows:
        power = row["grid_power_w"]
        if power is not None and power > 0:
            total_wh += power

    return total_wh / 1000  # Convert to kWh


# ---------------------------------------------------------------------------
# Hourly Polling
# ---------------------------------------------------------------------------

def poll_snapshot(ha, db):
    """Record an hourly snapshot of key metrics."""
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

    # --- Adjustment 3: Daily consumption average ---
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
            write_dashboard_status(ha, db, plan)
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
            record_outcome(ha, db)
            write_dashboard_status(ha, db)

        elif mode == "poll":
            poll_snapshot(ha, db)

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
