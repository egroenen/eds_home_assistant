"""Decision algorithm for overnight charging plan."""

import logging
from datetime import date, timedelta

from .config import (
    SENSORS, CONDITION_MAP, FAILSAFE_OVERNIGHT_SOC,
    BATTERY_RESERVE_PCT, USABLE_CAPACITY_KWH, OUTAGE_RESERVE_PCT,
    HOURLY_SOLAR_WEIGHT,
)
from .db import get_param
from .models import (
    build_hourly_solar, build_hourly_solar_radiation,
    build_hourly_consumption, get_season, get_seasonal_consumption,
    get_day_factor, get_temp_factor, simulate_battery_hourly,
)
from .metocean import get_metocean_hourly

log = logging.getLogger("solar_optimizer")


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

    tomorrow_fc = ha.get_weather_forecast()
    if tomorrow_fc:
        condition = tomorrow_fc.get("condition", condition).lower()
        precipitation = tomorrow_fc.get("precipitation", 0) or 0
        temp_high = tomorrow_fc.get("temperature")
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
        solar_cloud = build_hourly_solar(raw_solar, hourly, db)
        solar_rad = build_hourly_solar_radiation(raw_solar, hourly)

        if solar_rad and sum(solar_rad.values()) > 0:
            hourly_solar_map = solar_rad
            active_model = "radiation"
        else:
            hourly_solar_map = solar_cloud
            active_model = "cloud"

        peak_solar = sum(hourly_solar_map.values())
        adjusted_solar = peak_solar
        correction_factor = adjusted_solar / raw_solar if raw_solar > 0 else 0

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
        normalized = CONDITION_MAP.get(condition, None)
        correction_key = f"{normalized}_correction" if normalized else f"{condition}_correction"
        base_correction = get_param(db, correction_key)
        if base_correction is None:
            base_correction = 0.6
        cloud_modifier = 1.0 - (cloud_pct / 100.0) * 0.5
        correction_factor = base_correction * cloud_modifier
        adjusted_solar = raw_solar * correction_factor
        peak_solar = adjusted_solar * get_param(db, "peak_solar_ratio")

        total_weight = sum(HOURLY_SOLAR_WEIGHT.values())
        hourly_solar_map = {
            h: peak_solar * (w / total_weight)
            for h, w in HOURLY_SOLAR_WEIGHT.items() if w > 0
        }
        log.info(f"Solar forecast: {raw_solar:.1f} kWh, adjusted: {adjusted_solar:.1f} kWh "
                 f"(daily fallback, correction: {correction_factor:.2f})")

    temp_factor = get_temp_factor(db, temp_high)
    day_factor = get_day_factor(db, tomorrow_str)
    hourly_consumption_map = build_hourly_consumption(daily_consumption, 1.0, temp_factor, day_factor)
    peak_consumption = sum(hourly_consumption_map.values())
    energy_deficit = max(0, peak_consumption - peak_solar)

    log.info(f"Peak consumption: {peak_consumption:.1f} kWh "
             f"(season={season}, base={daily_consumption:.1f}, "
             f"temp_factor={temp_factor:.2f}, day_factor={day_factor:.2f}), "
             f"peak solar: {peak_solar:.1f} kWh, "
             f"deficit: {energy_deficit:.1f} kWh")

    # Step 5: Binary search for minimum starting SOC
    safety_margin = get_param(db, "safety_margin_pct")
    reserve_target = BATTERY_RESERVE_PCT + safety_margin

    lo, hi = BATTERY_RESERVE_PCT, 100
    best_soc = hi
    best_sim = None

    for _ in range(15):
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

    candidate_soc = int(((best_soc + 4) // 5) * 5)
    sim = simulate_battery_hourly(candidate_soc, hourly_solar_map, hourly_consumption_map)

    soc_from_deficit = (energy_deficit / USABLE_CAPACITY_KWH) * 100 + safety_margin

    log.info(f"Simulation: min_soc={sim['min_soc']}% at {sim['min_soc_hour']}:00, "
             f"optimal start={best_soc:.0f}%, rounded={candidate_soc}%, "
             f"deficit-based={soc_from_deficit:.0f}%, "
             f"profile={sim['hourly_soc']}")

    min_soc = get_param(db, "min_overnight_soc")
    max_soc = get_param(db, "max_overnight_soc")
    overnight_soc = max(min_soc, min(max_soc, candidate_soc))
    overnight_soc = int(round(overnight_soc / 5) * 5)

    log.info(f"SOC target: sim-optimal={candidate_soc}%, "
             f"deficit-based={soc_from_deficit:.0f}%, "
             f"final={overnight_soc}%")

    slot_socs = [
        OUTAGE_RESERVE_PCT,
        overnight_soc,
        BATTERY_RESERVE_PCT,
        BATTERY_RESERVE_PCT,
        BATTERY_RESERVE_PCT,
        OUTAGE_RESERVE_PCT,
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
