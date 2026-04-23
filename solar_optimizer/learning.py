"""Self-learning parameter adjustment and outcome recording."""

import logging
import math
import sqlite3
from datetime import date, datetime

from .config import (
    DB_PATH, SENSORS, NZ_SEASONS, TEMP_BANDS, WEEKEND_DAYS,
    BACKUP_DIR, BACKUP_KEEP,
)
from .db import get_param, set_param

log = logging.getLogger("solar_optimizer")


def backup_db():
    """Create a daily SQLite backup, keeping the last BACKUP_KEEP copies."""
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    today = date.today().isoformat()
    backup_path = BACKUP_DIR / f"solar_optimizer_{today}.db"
    if backup_path.exists():
        return

    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()
    log.info(f"Database backed up to {backup_path}")

    backups = sorted(BACKUP_DIR.glob("solar_optimizer_*.db"))
    for old in backups[:-BACKUP_KEEP]:
        old.unlink()
        log.info(f"Removed old backup: {old.name}")


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

    peak_start = get_param(db, "peak_start_grid_bought")
    if peak_start is not None and grid_bought is not None:
        peak_grid_kwh = max(0, grid_bought - peak_start)
        log.info(f"Peak grid (metered): {grid_bought:.2f} - {peak_start:.2f} = {peak_grid_kwh:.2f} kWh")
    else:
        peak_grid_kwh = 0
        log.warning("No peak-start grid_bought snapshot, peak grid usage unknown")

    peak_grid_used = 1 if (peak_grid_kwh or 0) > 0.5 else 0

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

    update_learning(db)


def update_sw_efficiency(db):
    """Learn per-hour shortwave-to-production efficiency from recent data.

    Because panels face different directions, the relationship between
    shortwave radiation and production varies by time of day (e.g., morning
    sun hits east-facing panels harder, afternoon favours west-facing).

    For each hour, calculates the median ratio of actual_pv_W / shortwave_wm2
    from recent forecast_tracking data, then blends into the stored per-hour
    efficiency parameter.  Only uses readings with meaningful radiation
    (>50 W/m²) to avoid noisy low-light data.  Zero-production hours ARE
    included so that shoulder periods (panels shaded despite ambient radiation)
    learn an efficiency near zero.

    The learning rate is adaptive: it decays exponentially with the size of the
    proposed change relative to the current value, with a floor of 10% to
    guarantee convergence even when initialisation is far off.  Small drift
    applies at ~30%, large deviations are dampened but always make progress.
    Formula: rate = max(0.10, 0.30 * exp(-2 * |delta/current|)).
    """
    rows = db.execute("""
        SELECT hour, shortwave_wm2, actual_pv_wh
        FROM forecast_tracking
        WHERE shortwave_wm2 > 50
          AND actual_pv_wh IS NOT NULL
        ORDER BY date DESC, hour DESC
        LIMIT 200
    """).fetchall()

    if len(rows) < 3:
        log.info(f"Not enough SW data for efficiency learning (have {len(rows)}, need 3+)")
        return

    # Group ratios by hour
    by_hour = {}
    for r in rows:
        h = r["hour"]
        ratio = r["actual_pv_wh"] / r["shortwave_wm2"]
        by_hour.setdefault(h, []).append(ratio)

    updated = []
    for hour, ratios in sorted(by_hour.items()):
        if len(ratios) < 2:
            continue

        # Median resists outliers from cloud transitions
        ratios.sort()
        n = len(ratios)
        if n % 2 == 0:
            median_ratio = (ratios[n // 2 - 1] + ratios[n // 2]) / 2
        else:
            median_ratio = ratios[n // 2]

        # Convert W/(W/m²) to kWh/(W/m²)
        new_eff = max(0.0, min(0.08, round(median_ratio / 1000.0, 4)))

        param_key = f"sw_efficiency_{hour}"
        current = get_param(db, param_key)
        if current is None:
            current = 0.018

        # Adaptive learning rate: small corrections apply at full speed,
        # large swings are dampened but still make progress.  A floor of
        # 10% ensures convergence even when initialisation is far off.
        delta = abs(new_eff - current) / current if current > 0 else 0
        rate = max(0.10, 0.30 * math.exp(-2.0 * delta))
        blended = current + rate * (new_eff - current)
        blended = round(blended, 4)

        if abs(blended - current) > 0.0002:
            set_param(db, param_key, blended)
            updated.append(f"h{hour}:{current:.4f}->{blended:.4f}")

    if updated:
        log.info(f"Learning: sw_efficiency updated {len(updated)} hours: "
                 f"{', '.join(updated)}")
    else:
        log.info(f"Learning: sw_efficiency unchanged across all hours "
                 f"(n={len(rows)} samples, {len(by_hour)} hours)")


def update_model_preference(db):
    """Compare both solar models' accuracy over recent days and update preference."""
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

    if total_rad_err < total_cloud_err:
        target_pref = 1.0
    else:
        target_pref = 0.0

    current_pref = get_param(db, "preferred_solar_model") or 0.0
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


def update_learning(db):
    """Adjust learning parameters based on recent outcomes."""
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
            new_val = current + 0.20 * (avg_accuracy - current)
            new_val = max(0.2, min(1.3, round(new_val, 3)))
            if abs(new_val - current) > 0.005:
                log.info(f"Learning: {condition} correction {current:.3f} -> {new_val:.3f} "
                         f"(avg accuracy: {avg_accuracy:.2f}, n={len(condition_days)})")
                set_param(db, param_key, new_val)

    # --- Adjustment 3: Daily consumption average (legacy) ---
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
            avg_consumption = sum(r["actual_consumption_kwh"] for r in band_days) / len(band_days)
            actual_ratio = avg_consumption / current_consumption_avg if current_consumption_avg > 0 else 1.0
            current_factor = get_param(db, param_key)
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

    # --- Adjustment 7: Shortwave efficiency factor ---
    update_sw_efficiency(db)
