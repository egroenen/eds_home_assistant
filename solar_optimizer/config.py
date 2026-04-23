"""Configuration constants for the solar optimizer."""

from pathlib import Path

# SCRIPT_DIR is the project root (parent of this package directory)
SCRIPT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = SCRIPT_DIR / "solar_optimizer.db"
ENV_PATH = SCRIPT_DIR / ".env"

# MetOcean API (MetService) — primary weather source for NZ
METOCEAN_API_URL = "https://forecast-v2.metoceanapi.com/point/time"
METOCEAN_API_KEY = None  # loaded from .env by load_env()
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
# Aligned to :59 so the 07:00 hourly poll reads the true charged SOC
CHARGE_DEADLINE_HOUR = 6
CHARGE_DEADLINE_MIN = 59

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
SLOT_TIMES = [
    (0, 0),    # slot 1: 00:00
    (4, 0),    # slot 2: 04:00
    (7, 0),    # slot 3: 07:00
    (11, 0),   # slot 4: 11:00
    (15, 0),   # slot 5: 15:00
    (21, 0),   # slot 6: 21:00
]

SLOT_POWERS = [3000, 3000, 10000, 10000, 10000, 3000]

# Enable registers: 0=none, 1=grid charge, 2=gen charge, 3=both
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
SEASONAL_FACTOR = {
    1: 0.85, 2: 0.90, 3: 0.95, 4: 1.05, 5: 1.10, 6: 1.15,
    7: 1.15, 8: 1.10, 9: 1.05, 10: 0.95, 11: 0.90, 12: 0.85,
}

# Hourly solar production weights (fraction of daily total per hour)
HOURLY_SOLAR_WEIGHT = {
    7: 0.02, 8: 0.05, 9: 0.08, 10: 0.10, 11: 0.12, 12: 0.13,
    13: 0.13, 14: 0.12, 15: 0.10, 16: 0.08, 17: 0.05, 18: 0.03,
    19: 0.01, 20: 0.00,
}

# Last hour with meaningful solar production, by month.
# Christchurch (-43.5°) sunset times shift ~3 hours between solstices.
# MetOcean reports diffuse shortwave radiation well past sunset, but panels
# produce nothing — so the radiation model must zero out post-sunset hours.
# Values are conservative (last hour where any production is plausible).
SOLAR_LAST_HOUR = {
    1: 20, 2: 19, 3: 18, 4: 17, 5: 16, 6: 16,
    7: 16, 8: 16, 9: 17, 10: 18, 11: 18, 12: 19,
}

# Hourly consumption weights (fraction of daily total per peak hour)
# Derived from 13 days of actual hourly_log data (April 2026).
# Flatter midday load, lighter morning/evening than original profile.
HOURLY_CONSUMPTION_WEIGHT = {
    7: 0.04, 8: 0.03, 9: 0.06, 10: 0.07, 11: 0.05, 12: 0.06,
    13: 0.06, 14: 0.05, 15: 0.04, 16: 0.04, 17: 0.04, 18: 0.06,
    19: 0.05, 20: 0.05,
}

# Weather condition normalization map
CONDITION_MAP = {
    "sunny": "sunny", "clear-night": "sunny", "clear": "sunny",
    "partlycloudy": "partlycloudy", "partly-cloudy": "partlycloudy",
    "cloudy": "cloudy", "fog": "cloudy",
    "rainy": "rainy", "pouring": "rainy", "snowy": "rainy",
    "lightning": "rainy", "lightning-rainy": "rainy",
    "hail": "rainy", "windy": "partlycloudy",
    "windy-variant": "partlycloudy", "exceptional": "cloudy",
}

# Temperature bands for consumption factor
TEMP_BANDS = [
    (10, "temp_factor_cold"),
    (15, "temp_factor_cool"),
    (20, "temp_factor_mild"),
    (99, "temp_factor_warm"),
]

WEEKEND_DAYS = {5, 6}

# Default learning parameters
DEFAULT_PARAMS = {
    "base_overnight_soc": 60.0,
    "sunny_correction": 1.0,
    "cloudy_correction": 0.70,
    "partlycloudy_correction": 0.85,
    "rainy_correction": 0.45,
    "learning_rate": 2.0,
    "daily_consumption_avg": 33.0,
    "consumption_avg_spring": 31.9,
    "consumption_avg_summer": 28.6,
    "consumption_avg_autumn": 34.1,
    "consumption_avg_winter": 37.4,
    "consumption_avg_year": 33.0,
    "min_overnight_soc": 30.0,
    "max_overnight_soc": 100.0,
    "safety_soc_floor": 15.0,
    "peak_consumption_ratio": 0.70,
    "peak_solar_ratio": 0.85,
    "safety_margin_pct": 10.0,
    "preferred_solar_model": 0.0,
    "weekend_factor": 1.20,
    "weekday_factor": 1.0,
    "temp_factor_cold": 1.35,
    "temp_factor_cool": 1.15,
    "temp_factor_mild": 1.0,
    "temp_factor_warm": 0.85,
    # Per-hour shortwave efficiency (kWh produced per W/m² of shortwave radiation).
    # Varies by hour because panels face different directions.
    "sw_efficiency_7": 0.018,
    "sw_efficiency_8": 0.018,
    "sw_efficiency_9": 0.018,
    "sw_efficiency_10": 0.018,
    "sw_efficiency_11": 0.018,
    "sw_efficiency_12": 0.018,
    "sw_efficiency_13": 0.018,
    "sw_efficiency_14": 0.018,
    "sw_efficiency_15": 0.018,
    "sw_efficiency_16": 0.018,
    "sw_efficiency_17": 0.018,
    "sw_efficiency_18": 0.018,
    "sw_efficiency_19": 0.018,
    "sw_efficiency_20": 0.018,
}

FAILSAFE_OVERNIGHT_SOC = 80

# Backup settings
BACKUP_DIR = SCRIPT_DIR / "backups"
BACKUP_KEEP = 7
