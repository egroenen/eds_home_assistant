"""Dashboard status writing and display functions."""

import json
import logging
from datetime import date, datetime, timedelta

from .config import SENSORS, SCRIPT_DIR, PEAK_START_HOUR
from .db import get_param
from .models import (
    build_hourly_solar, build_hourly_solar_radiation,
    build_hourly_consumption, get_season, get_seasonal_consumption,
    get_temp_factor, get_day_factor, simulate_battery_hourly,
    get_sw_efficiency_map,
)
from .metocean import get_metocean_hourly

log = logging.getLogger("solar_optimizer")


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
            row = db.execute("SELECT * FROM daily_plan WHERE date=?", (tomorrow,)).fetchone()
            if not row:
                row = db.execute("SELECT * FROM daily_plan WHERE date=?", (today,)).fetchone()
        else:
            row = db.execute("SELECT * FROM daily_plan WHERE date=?", (today,)).fetchone()
            if not row:
                row = db.execute("SELECT * FROM daily_plan WHERE date=?", (tomorrow,)).fetchone()
        if row:
            p = dict(row)
    if p:
        slots = []
        for i in range(6):
            n = i + 1
            slot_time = ha.get_state(f"sensor.inverter_time_of_use_time_{n}").get("state", "??:??")
            slot_soc = ha.get_sensor_float(f"sensor.inverter_time_of_use_soc_{n}")
            slot_power = ha.get_sensor_float(f"sensor.inverter_time_of_use_power_{n}")
            slot_enable = ha.get_sensor_float(f"sensor.inverter_time_of_use_enable_{n}")
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

    days = db.execute("SELECT COUNT(*) FROM daily_outcome").fetchone()[0]
    status["days_of_data"] = days

    pref = get_param(db, "preferred_solar_model") or 0.0
    status["learning"]["model_pref"] = round(pref, 2)

    # Per-hour shortwave efficiency
    sw_eff = {}
    for h in range(7, 21):
        v = get_param(db, f"sw_efficiency_{h}")
        sw_eff[h] = round(v, 4) if v else 0.018
    status["learning"]["sw_efficiency"] = sw_eff
    status["learning"]["active_model"] = "radiation"

    # Detailed hourly forecast for the detail view
    #
    # Before peak hours (7am), we recalculate the forecast each poll so it
    # reflects the latest weather data and SOC target.  At 7am (or the first
    # poll after), we freeze the forecast into the daily_plan row so that
    # daytime tracking compares actuals against a stable baseline.
    try:
        plan_date = p.get("date", today) if p else today
        now_hour = datetime.now().hour
        frozen = None

        # During peak hours, try to use the frozen forecast
        if now_hour >= PEAK_START_HOUR and plan_date == today:
            frozen_row = db.execute(
                "SELECT frozen_detail FROM daily_plan WHERE date=?",
                (plan_date,)
            ).fetchone()
            if frozen_row and frozen_row["frozen_detail"]:
                frozen = json.loads(frozen_row["frozen_detail"])

        if frozen:
            # Use frozen forecast, but update actuals from hourly_log + forecast_tracking
            actual_soc_rows = db.execute(
                "SELECT hour, battery_soc FROM hourly_log WHERE date=? ORDER BY hour",
                (plan_date,)
            ).fetchall()
            actual_soc_by_hour = {r["hour"]: r["battery_soc"] for r in actual_soc_rows}

            actual_pv_rows = db.execute(
                "SELECT hour, actual_pv_wh FROM forecast_tracking WHERE date=? ORDER BY hour",
                (plan_date,)
            ).fetchall()
            actual_pv_by_hour = {r["hour"]: r["actual_pv_wh"] for r in actual_pv_rows}

            for h_entry in frozen["hours"]:
                h = h_entry["hour"]
                actual_soc = actual_soc_by_hour.get(h)
                h_entry["actual_soc"] = actual_soc
                forecast_soc = h_entry.get("battery_soc")
                if forecast_soc is not None and actual_soc is not None:
                    h_entry["soc_diff"] = round(actual_soc - forecast_soc, 1)
                else:
                    h_entry["soc_diff"] = None
                actual_pv = actual_pv_by_hour.get(h)
                h_entry["actual_pv_kwh"] = round(actual_pv / 1000, 2) if actual_pv is not None else None

            status["detail"] = frozen
        else:
            # Recalculate forecast (pre-7am or no frozen data yet)
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
                sw_eff_map = get_sw_efficiency_map(db)
                solar_rad = build_hourly_solar_radiation(raw_solar, hourly, sw_eff_map)

                daily_consumption = get_seasonal_consumption(db, plan_date)
                season = get_season(plan_date)
                temps = [h["temperature"] for h in hourly if h.get("temperature") is not None]
                avg_temp = sum(temps) / len(temps) if temps else None
                temp_factor = get_temp_factor(db, avg_temp)
                day_factor = get_day_factor(db, plan_date)
                hourly_consumption_map = build_hourly_consumption(daily_consumption, 1.0, temp_factor, day_factor)

                active_solar = solar_rad if (solar_rad and sum(solar_rad.values()) > 0) else solar_cloud
                soc_target = p.get("overnight_soc_target", 70) if p else 70
                sim = simulate_battery_hourly(soc_target, active_solar, hourly_consumption_map)

                actual_soc_rows = db.execute(
                    "SELECT hour, battery_soc FROM hourly_log WHERE date=? ORDER BY hour",
                    (plan_date,)
                ).fetchall()
                actual_soc_by_hour = {r["hour"]: r["battery_soc"] for r in actual_soc_rows}

                actual_pv_rows = db.execute(
                    "SELECT hour, actual_pv_wh FROM forecast_tracking WHERE date=? ORDER BY hour",
                    (plan_date,)
                ).fetchall()
                actual_pv_by_hour = {r["hour"]: r["actual_pv_wh"] for r in actual_pv_rows}

                sim_soc_map = dict(sim["hourly_soc"])
                detail_hours = []
                for hfc in hourly:
                    h = hfc["hour"]
                    forecast_soc = sim_soc_map.get(h)
                    actual_soc = actual_soc_by_hour.get(h)
                    actual_pv = actual_pv_by_hour.get(h)
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
                        "actual_pv_kwh": round(actual_pv / 1000, 2) if actual_pv is not None else None,
                        "consumption": round(hourly_consumption_map.get(h, 0), 2),
                        "battery_soc": forecast_soc,
                        "actual_soc": actual_soc,
                        "soc_diff": diff,
                    })

                detail = {
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
                status["detail"] = detail

                # Freeze the forecast at 7am for daytime tracking
                if now_hour >= PEAK_START_HOUR and plan_date == today:
                    frozen_row = db.execute(
                        "SELECT frozen_detail FROM daily_plan WHERE date=?",
                        (plan_date,)
                    ).fetchone()
                    if frozen_row and not frozen_row["frozen_detail"]:
                        db.execute(
                            "UPDATE daily_plan SET frozen_detail=? WHERE date=?",
                            (json.dumps(detail), plan_date)
                        )
                        db.commit()
                        log.info(f"Frozen forecast detail for {plan_date}")

        if "detail" in status:
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


def write_history_status(db):
    """Write historical plan-vs-actual data to a JSON file for the dashboard."""
    rows = db.execute("""
        SELECT p.date, p.weather_condition AS plan_weather,
               p.solar_forecast_kwh AS forecast_kwh,
               p.adjusted_solar_kwh AS adjusted_kwh,
               p.overnight_soc_target AS soc_target,
               p.energy_deficit_kwh AS deficit_kwh,
               p.correction_factor AS correction,
               o.actual_production_kwh AS production_kwh,
               o.actual_consumption_kwh AS consumption_kwh,
               o.grid_bought_kwh,
               o.grid_sold_kwh,
               o.peak_grid_kwh,
               o.peak_grid_used,
               o.forecast_accuracy,
               o.weather_condition AS actual_weather,
               o.temperature_high AS temp_high
        FROM daily_plan p
        LEFT JOIN daily_outcome o ON p.date = o.date
        WHERE o.date IS NOT NULL
        ORDER BY p.date DESC
        LIMIT 30
    """).fetchall()

    history = []
    for r in rows:
        production = r["production_kwh"]
        consumption = r["consumption_kwh"]
        forecast = r["forecast_kwh"]
        grid_bought = r["grid_bought_kwh"]
        peak_grid = r["peak_grid_kwh"]
        grid_sold = r["grid_sold_kwh"]
        adjusted = r["adjusted_kwh"]

        # Off-peak grid = total grid bought minus peak grid bought
        off_peak_grid = round(grid_bought - peak_grid, 1) if (
            grid_bought is not None and peak_grid is not None) else None

        # Solar delta: actual production vs forecast
        solar_delta = round(production - forecast, 1) if (
            production is not None and forecast is not None) else None

        # Net balance: production - consumption
        net_balance = round(production - consumption, 1) if (
            production is not None and consumption is not None) else None

        history.append({
            "date": r["date"],
            "plan_weather": r["plan_weather"],
            "actual_weather": r["actual_weather"],
            "forecast_kwh": round(forecast, 1) if forecast else None,
            "adjusted_kwh": round(adjusted, 1) if adjusted else None,
            "production_kwh": round(production, 1) if production else None,
            "solar_delta": solar_delta,
            "consumption_kwh": round(consumption, 1) if consumption else None,
            "net_balance": net_balance,
            "grid_bought_kwh": round(grid_bought, 1) if grid_bought else None,
            "off_peak_grid_kwh": off_peak_grid,
            "peak_grid_kwh": round(peak_grid, 1) if peak_grid is not None else None,
            "grid_sold_kwh": round(grid_sold, 1) if grid_sold else None,
            "soc_target": r["soc_target"],
            "deficit_kwh": round(r["deficit_kwh"], 1) if r["deficit_kwh"] else None,
            "correction": round(r["correction"], 2) if r["correction"] else None,
            "forecast_accuracy": round(r["forecast_accuracy"], 2) if r["forecast_accuracy"] else None,
            "peak_grid_used": bool(r["peak_grid_used"]),
            "temp_high": round(r["temp_high"], 1) if r["temp_high"] is not None else None,
        })

    status = {
        "updated_at": datetime.now().strftime("%H:%M %d %b"),
        "days": history,
    }

    history_path = SCRIPT_DIR / "solar_optimizer_history.json"
    history_path.write_text(json.dumps(status, indent=2))
    log.info(f"History status written to {history_path}")


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
