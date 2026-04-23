"""Solar production, consumption, and battery simulation models."""

from datetime import datetime

from .config import (
    HOURLY_SOLAR_WEIGHT, HOURLY_CONSUMPTION_WEIGHT,
    CONDITION_MAP, TEMP_BANDS, NZ_SEASONS, WEEKEND_DAYS,
    BATTERY_CAPACITY_KWH, BATTERY_RESERVE_PCT,
    PEAK_START_HOUR, PEAK_END_HOUR,
)
from .db import get_param


def get_temp_factor(db, temp_c):
    """Get the consumption multiplier for a given temperature."""
    if temp_c is None:
        return 1.0
    for threshold, param_key in TEMP_BANDS:
        if temp_c < threshold:
            return get_param(db, param_key) or 1.0
    return get_param(db, "temp_factor_warm") or 0.85


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


def build_hourly_solar_radiation(raw_solar, hourly_forecast, sw_efficiency_by_hour=None):
    """Build hourly solar production dict using shortwave radiation (Model B).

    If sw_efficiency_by_hour is provided (dict of hour -> kWh per W/m²),
    converts shortwave radiation directly to production using the per-hour
    learned factor.  This accounts for panels facing different directions
    producing more or less at different times of day.

    Otherwise falls back to proportionally distributing the Forecast.Solar
    daily total across hours by relative shortwave intensity.

    Returns None if shortwave data is not available.
    """
    sw_by_hour = {}
    for hfc in hourly_forecast:
        hour = hfc["hour"]
        sw = hfc.get("shortwave_wm2")
        if sw is not None and hour in HOURLY_SOLAR_WEIGHT:
            sw_by_hour[hour] = max(0, sw)

    if not sw_by_hour or sum(sw_by_hour.values()) <= 0:
        return None

    if sw_efficiency_by_hour:
        # Direct conversion per hour: kWh = sw_wm2 × hour-specific efficiency
        result = {}
        for hour, sw in sw_by_hour.items():
            eff = sw_efficiency_by_hour.get(hour, 0)
            result[hour] = sw * eff if eff > 0 else 0
        if sum(result.values()) <= 0:
            # Fall through to proportional if all efficiencies are zero
            total_sw = sum(sw_by_hour.values())
            result = {hour: raw_solar * (sw / total_sw) for hour, sw in sw_by_hour.items()}
    else:
        # Fallback: distribute Forecast.Solar total proportionally
        total_sw = sum(sw_by_hour.values())
        result = {hour: raw_solar * (sw / total_sw) for hour, sw in sw_by_hour.items()}

    return result


def get_sw_efficiency_map(db):
    """Load per-hour shortwave efficiency values from learning params."""
    return {
        hour: get_param(db, f"sw_efficiency_{hour}") or 0.018
        for hour in HOURLY_SOLAR_WEIGHT
    }


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

    Each (hour, soc_pct) in hourly_soc represents the SOC at the START of
    that hour (before solar/consumption), matching the :00 poll reading.

    Returns dict with:
        min_soc: lowest SOC reached (%)
        min_soc_hour: hour when minimum occurred
        shortfall_kwh: max energy below reserve (0 if never breached)
        hourly_soc: list of (hour, soc_pct) tuples — SOC at start of hour
    """
    soc_kwh = (starting_soc_pct / 100.0) * BATTERY_CAPACITY_KWH
    reserve_kwh = (BATTERY_RESERVE_PCT / 100.0) * BATTERY_CAPACITY_KWH

    min_soc_kwh = soc_kwh
    min_soc_hour = PEAK_START_HOUR
    max_shortfall_kwh = 0.0
    hourly_soc = []

    for hour in range(PEAK_START_HOUR, PEAK_END_HOUR):
        # Record SOC at the start of the hour (before energy flow)
        soc_pct = (soc_kwh / BATTERY_CAPACITY_KWH) * 100.0
        hourly_soc.append((hour, round(soc_pct, 1)))

        solar = hourly_solar.get(hour, 0.0)
        consumption = hourly_consumption.get(hour, 0.0)

        soc_kwh += solar - consumption
        soc_kwh = max(0.0, min(BATTERY_CAPACITY_KWH, soc_kwh))

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
