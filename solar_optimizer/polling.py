"""Hourly polling and forecast model tracking."""

import logging
from datetime import datetime

from .config import SENSORS, PEAK_START_HOUR, PEAK_END_HOUR
from .db import get_param, set_param
from .models import (
    build_hourly_solar, build_hourly_solar_radiation,
    build_hourly_consumption, get_seasonal_consumption,
    get_sw_efficiency_map,
    get_temp_factor, get_day_factor, simulate_battery_hourly,
)
from .metocean import get_metocean_hourly
from .charging import adjust_overnight_charging

log = logging.getLogger("solar_optimizer")


def poll_snapshot(ha, db):
    """Record an hourly snapshot and dynamically adjust overnight charging."""
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
    load_consumption = ha.get_sensor_float(SENSORS["daily_consumption"])
    pv_production = ha.get_sensor_float(SENSORS["daily_production"])

    db.execute("""
        INSERT OR REPLACE INTO hourly_log
        (timestamp, date, hour, battery_soc, grid_power_w, pv_power_w,
         load_power_w, grid_bought_kwh, grid_sold_kwh,
         load_consumption_kwh, pv_production_kwh)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ts, day, hour, battery_soc, grid_power, pv_power,
          load_power, grid_bought, grid_sold,
          load_consumption, pv_production))
    db.commit()

    log.info(f"Poll: SOC={battery_soc}%, grid={grid_power}W, "
             f"PV={pv_power}W, load={load_power}W")

    if hour == PEAK_START_HOUR:
        peak_start_bought = ha.get_sensor_float(SENSORS["daily_grid_bought"])
        if peak_start_bought is not None:
            set_param(db, "peak_start_grid_bought", peak_start_bought)
            log.info(f"Recorded peak-start grid_bought: {peak_start_bought:.2f} kWh")

    if PEAK_START_HOUR <= hour < PEAK_END_HOUR:
        track_solar_models(ha, db, day, hour, pv_power, battery_soc, load_power)

    adjust_overnight_charging(ha, db, now, battery_soc)


def track_solar_models(ha, db, day, hour, actual_pv_w, battery_soc, load_power_w):
    """Track forecast models and consumption estimates against actuals."""
    try:
        raw_solar = ha.get_sensor_float(SENSORS["solar_forecast_today"])
        if raw_solar is None or raw_solar <= 0:
            return

        hourly = get_metocean_hourly(day)
        if not hourly:
            hourly = ha.get_hourly_forecast(day)
        if not hourly:
            return

        solar_cloud = build_hourly_solar(raw_solar, hourly, db)
        sw_eff_map = get_sw_efficiency_map(db)
        solar_rad = build_hourly_solar_radiation(raw_solar, hourly, sw_eff_map,
                                                       target_date=day)

        cloud_kwh = solar_cloud.get(hour, 0)
        rad_kwh = (solar_rad or {}).get(hour, 0)
        actual_pv_wh = actual_pv_w

        hour_fc = next((h for h in hourly if h["hour"] == hour), {})
        wx_condition = hour_fc.get("condition")
        wx_cloud = hour_fc.get("cloud_coverage")
        wx_temp = hour_fc.get("temperature")
        wx_sw = hour_fc.get("shortwave_wm2")

        daily_consumption = get_seasonal_consumption(db, day)
        temps = [h["temperature"] for h in hourly if h.get("temperature") is not None]
        avg_temp = sum(temps) / len(temps) if temps else None
        temp_factor = get_temp_factor(db, avg_temp)
        day_factor = get_day_factor(db, day)
        hourly_consumption_map = build_hourly_consumption(daily_consumption, 1.0, temp_factor, day_factor)
        est_consumption = hourly_consumption_map.get(hour, 0)

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
