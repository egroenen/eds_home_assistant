"""Historical backtesting and profile tuning for the solar optimizer."""

from .config import (
    BATTERY_CAPACITY_KWH,
    BATTERY_RESERVE_PCT,
    PROFILE_SELECTION_COST_TOLERANCE,
    PEAK_END_HOUR,
    PEAK_START_HOUR,
    PEAK_VALUE_RATE,
)
from .engines import build_engine_hourly_solar
from .models import (
    build_hourly_consumption,
    get_day_factor,
    get_seasonal_consumption,
    get_temp_factor,
    simulate_battery_hourly,
)
from .profiles import ensure_original_profile, get_active_engine_name, get_current_params, save_profile


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def fit_radiation_params(db, percentile=50, scale=1.0):
    """Fit per-hour shortwave efficiency from tracked actual-vs-radiation history."""
    rows = db.execute(
        """
        SELECT hour, shortwave_wm2, actual_pv_wh
        FROM forecast_tracking
        WHERE shortwave_wm2 > 50
          AND actual_pv_wh IS NOT NULL
        ORDER BY date DESC, hour DESC
        """
    ).fetchall()

    by_hour = {}
    for row in rows:
        ratio = (row["actual_pv_wh"] / 1000.0) / row["shortwave_wm2"]
        by_hour.setdefault(row["hour"], []).append(ratio)

    fitted = {}
    for hour, ratios in by_hour.items():
        fitted[f"sw_efficiency_{hour}"] = round(
            max(0.0, min(0.08, (_percentile(ratios, percentile) or 0.0) * scale)),
            4,
        )
    return fitted


def _build_hourly_forecast(db, day):
    rows = db.execute(
        """
        SELECT hour, weather_condition, cloud_pct, temperature, shortwave_wm2
        FROM forecast_tracking
        WHERE date=?
        ORDER BY hour
        """,
        (day,),
    ).fetchall()
    if len(rows) < 6:
        return None
    return [
        {
            "hour": row["hour"],
            "condition": row["weather_condition"] or "cloudy",
            "cloud_coverage": row["cloud_pct"] if row["cloud_pct"] is not None else 50,
            "temperature": row["temperature"],
            "shortwave_wm2": row["shortwave_wm2"],
        }
        for row in rows
    ]


def _build_actual_hourly_maps(db, day):
    rows = db.execute(
        """
        SELECT hour, load_consumption_kwh, pv_production_kwh
        FROM hourly_log
        WHERE date=?
        ORDER BY hour
        """,
        (day,),
    ).fetchall()
    by_hour = {row["hour"]: row for row in rows}
    if not all(hour in by_hour for hour in range(PEAK_START_HOUR, PEAK_END_HOUR + 1)):
        return None, None

    load_map = {}
    solar_map = {}
    for hour in range(PEAK_START_HOUR, PEAK_END_HOUR):
        current = by_hour[hour]
        next_row = by_hour[hour + 1]
        if (
            current["load_consumption_kwh"] is None
            or next_row["load_consumption_kwh"] is None
            or current["pv_production_kwh"] is None
            or next_row["pv_production_kwh"] is None
        ):
            return None, None
        load_map[hour] = max(0.0, next_row["load_consumption_kwh"] - current["load_consumption_kwh"])
        solar_map[hour] = max(0.0, next_row["pv_production_kwh"] - current["pv_production_kwh"])
    return load_map, solar_map


def _calculate_target_soc(plan_row, hourly_forecast, params, engine_name):
    raw_solar = plan_row["solar_forecast_kwh"]
    if raw_solar is None or raw_solar <= 0:
        raw_solar = 0.0
        engine_name = "radiation"

    solar_result = build_engine_hourly_solar(
        raw_solar, hourly_forecast, params, plan_row["date"], engine_name
    )
    hourly_solar_map = solar_result["hourly_solar"]
    if sum(hourly_solar_map.values()) <= 0:
        return None, solar_result

    daily_consumption = get_seasonal_consumption(params, plan_row["date"])
    temps = [h["temperature"] for h in hourly_forecast if h.get("temperature") is not None]
    avg_temp = sum(temps) / len(temps) if temps else None
    temp_factor = get_temp_factor(params, avg_temp)
    day_factor = get_day_factor(params, plan_row["date"])
    hourly_consumption_map = build_hourly_consumption(
        daily_consumption, 1.0, temp_factor, day_factor
    )

    safety_margin = params.get("safety_margin_pct", 10.0)
    reserve_target = BATTERY_RESERVE_PCT + safety_margin

    lo, hi = BATTERY_RESERVE_PCT, 100.0
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
    max_soc = params.get("max_overnight_soc", 100.0)
    revised = max(reserve_target, min(max_soc, revised))
    revised = int(round(revised / 5) * 5)
    return revised, solar_result


def _simulate_actual_peak_grid(start_soc_pct, actual_solar_map, actual_load_map):
    reserve_kwh = BATTERY_CAPACITY_KWH * BATTERY_RESERVE_PCT / 100.0
    battery_kwh = BATTERY_CAPACITY_KWH * start_soc_pct / 100.0
    peak_grid_kwh = 0.0
    min_soc_pct = start_soc_pct

    for hour in range(PEAK_START_HOUR, PEAK_END_HOUR):
        load_kwh = actual_load_map.get(hour, 0.0)
        solar_kwh = actual_solar_map.get(hour, 0.0)
        net_load = load_kwh - solar_kwh

        if net_load <= 0:
            battery_kwh = min(BATTERY_CAPACITY_KWH, battery_kwh + abs(net_load))
        else:
            available = max(0.0, battery_kwh - reserve_kwh)
            battery_used = min(available, net_load)
            battery_kwh -= battery_used
            peak_grid_kwh += max(0.0, net_load - battery_used)

        min_soc_pct = min(min_soc_pct, (battery_kwh / BATTERY_CAPACITY_KWH) * 100.0)

    return {
        "peak_grid_kwh": round(peak_grid_kwh, 3),
        "end_soc_pct": round((battery_kwh / BATTERY_CAPACITY_KWH) * 100.0, 1),
        "min_soc_pct": round(min_soc_pct, 1),
    }


def evaluate_candidate(db, params, engine_name, peak_rate=PEAK_VALUE_RATE):
    rows = db.execute(
        """
        SELECT date, solar_forecast_kwh
        FROM daily_plan
        ORDER BY date
        """
    ).fetchall()

    day_results = []
    for row in rows:
        hourly_forecast = _build_hourly_forecast(db, row["date"])
        actual_load_map, actual_solar_map = _build_actual_hourly_maps(db, row["date"])
        if not hourly_forecast or not actual_load_map or not actual_solar_map:
            continue

        target_soc, solar_result = _calculate_target_soc(row, hourly_forecast, params, engine_name)
        if target_soc is None:
            continue

        actual_sim = _simulate_actual_peak_grid(target_soc, actual_solar_map, actual_load_map)
        actual_solar_total = sum(actual_solar_map.values())
        solar_error = solar_result["active_total"] - actual_solar_total
        day_results.append(
            {
                "date": row["date"],
                "target_soc": target_soc,
                "peak_grid_kwh": actual_sim["peak_grid_kwh"],
                "peak_cost": round(actual_sim["peak_grid_kwh"] * peak_rate, 3),
                "end_soc_pct": actual_sim["end_soc_pct"],
                "min_soc_pct": actual_sim["min_soc_pct"],
                "engine_total_kwh": round(solar_result["active_total"], 3),
                "actual_solar_kwh": round(actual_solar_total, 3),
                "solar_error_kwh": round(solar_error, 3),
            }
        )

    total_peak_grid = sum(day["peak_grid_kwh"] for day in day_results)
    total_cost = sum(day["peak_cost"] for day in day_results)
    avg_target_soc = (
        sum(day["target_soc"] for day in day_results) / len(day_results)
        if day_results else None
    )
    avg_solar_mae = (
        sum(abs(day["solar_error_kwh"]) for day in day_results) / len(day_results)
        if day_results else None
    )
    avg_solar_bias = (
        sum(day["solar_error_kwh"] for day in day_results) / len(day_results)
        if day_results else None
    )
    return {
        "engine_name": engine_name,
        "days": len(day_results),
        "total_peak_grid_kwh": round(total_peak_grid, 3),
        "total_cost": round(total_cost, 3),
        "avg_peak_grid_kwh": round(total_peak_grid / len(day_results), 3) if day_results else None,
        "avg_target_soc": round(avg_target_soc, 1) if avg_target_soc is not None else None,
        "avg_solar_mae_kwh": round(avg_solar_mae, 2) if avg_solar_mae is not None else None,
        "avg_solar_bias_kwh": round(avg_solar_bias, 2) if avg_solar_bias is not None else None,
        "day_results": day_results,
    }


def _with_sw_params(base_params, fitted_params, scale=1.0, safety_margin=None):
    params = dict(base_params)
    for key, value in fitted_params.items():
        params[key] = round(max(0.0, min(0.08, value * scale)), 4)
    if safety_margin is not None:
        params["safety_margin_pct"] = float(safety_margin)
    return params


def generate_candidate_specs(db):
    ensure_original_profile(db)

    base_params = get_current_params(db)
    current_engine = get_active_engine_name(db)
    fitted = fit_radiation_params(db, percentile=50, scale=1.0)
    fitted_p60 = fit_radiation_params(db, percentile=60, scale=1.0)
    current_margin = float(base_params.get("safety_margin_pct", 10.0))
    safe_margin = max(current_margin, 20.0)
    charge_bias_margin = max(current_margin, 35.0)

    return [
        {
            "name": "original",
            "engine_name": current_engine,
            "params": dict(base_params),
            "description": "Current working set snapshot.",
            "source": "snapshot",
        },
        {
            "name": "cloud-baseline",
            "engine_name": "cloud",
            "params": dict(base_params),
            "description": "Cloud-only baseline with the current parameter values.",
            "source": "backtest",
        },
        {
            "name": "radiation-fitted",
            "engine_name": "radiation",
            "params": _with_sw_params(base_params, fitted, scale=1.0, safety_margin=current_margin),
            "description": "Radiation engine with per-hour fitted shortwave efficiencies.",
            "source": "backtest",
        },
        {
            "name": "radiation-fitted-safe",
            "engine_name": "radiation",
            "params": _with_sw_params(base_params, fitted, scale=0.9, safety_margin=safe_margin),
            "description": "Radiation engine with fitted efficiencies scaled down and a larger safety margin.",
            "source": "backtest",
        },
        {
            "name": "radiation-accuracy-charge-biased",
            "engine_name": "radiation",
            "params": _with_sw_params(base_params, fitted_p60, scale=1.0, safety_margin=charge_bias_margin),
            "description": "Radiation engine fitted for generation accuracy with charge-biased reserve.",
            "source": "backtest",
        },
        {
            "name": "blended-fitted",
            "engine_name": "blended",
            "params": _with_sw_params(base_params, fitted, scale=1.0, safety_margin=current_margin),
            "description": "Blended cloud/radiation engine with fitted shortwave efficiencies.",
            "source": "backtest",
        },
        {
            "name": "blended-fitted-safe",
            "engine_name": "blended",
            "params": _with_sw_params(base_params, fitted, scale=0.9, safety_margin=safe_margin),
            "description": "Blended engine with conservative fitted efficiencies and extra reserve.",
            "source": "backtest",
        },
        {
            "name": "blended-charge-biased",
            "engine_name": "blended",
            "params": _with_sw_params(base_params, fitted, scale=0.85, safety_margin=charge_bias_margin),
            "description": "Blended engine biased toward extra off-peak charging when forecasts are uncertain.",
            "source": "backtest",
        },
        {
            "name": "capped-radiation-fitted",
            "engine_name": "capped_radiation",
            "params": _with_sw_params(base_params, fitted, scale=1.0, safety_margin=current_margin),
            "description": "Radiation engine capped by the cloud model to limit optimistic spikes.",
            "source": "backtest",
        },
        {
            "name": "capped-radiation-fitted-safe",
            "engine_name": "capped_radiation",
            "params": _with_sw_params(base_params, fitted, scale=0.9, safety_margin=safe_margin),
            "description": "Capped radiation engine with conservative fitted efficiencies and extra reserve.",
            "source": "backtest",
        },
        {
            "name": "capped-radiation-charge-biased",
            "engine_name": "capped_radiation",
            "params": _with_sw_params(base_params, fitted, scale=0.85, safety_margin=charge_bias_margin),
            "description": "Capped radiation engine biased toward extra off-peak charging when forecasts are uncertain.",
            "source": "backtest",
        },
    ]


def _avg_soc_for_ranking(result):
    if result["avg_target_soc"] is None:
        return 0.0
    return result["avg_target_soc"]


def _solar_mae_for_ranking(result):
    if result.get("avg_solar_mae_kwh") is None:
        return 999.0
    return result["avg_solar_mae_kwh"]


def rank_results_charge_biased(results, cost_tolerance=PROFILE_SELECTION_COST_TOLERANCE):
    """Rank candidates, preferring charge first and solar accuracy second."""
    if not results:
        return []

    best_cost = min(result["total_cost"] for result in results)

    def key(result):
        within_tolerance = result["total_cost"] <= best_cost + cost_tolerance
        if within_tolerance:
            return (
                0,
                -_avg_soc_for_ranking(result),
                _solar_mae_for_ranking(result),
                result["total_cost"],
                result["total_peak_grid_kwh"],
                result["name"],
            )
        return (
            1,
            result["total_cost"],
            result["total_peak_grid_kwh"],
            -_avg_soc_for_ranking(result),
            _solar_mae_for_ranking(result),
            result["name"],
        )

    return sorted(results, key=key)


def run_backtest(db, peak_rate=PEAK_VALUE_RATE, save_candidates=True):
    results = []
    for spec in generate_candidate_specs(db):
        metrics = evaluate_candidate(db, spec["params"], spec["engine_name"], peak_rate=peak_rate)
        result = dict(spec)
        result.update(metrics)
        results.append(result)

        if save_candidates:
            save_profile(
                db,
                spec["name"],
                params=spec["params"],
                engine_name=spec["engine_name"],
                description=spec["description"],
                source=spec["source"],
                score_peak_grid=metrics["total_peak_grid_kwh"],
                score_cost=metrics["total_cost"],
            )

    ranked = rank_results_charge_biased(results)
    if ranked and save_candidates:
        best = ranked[0]
        save_profile(
            db,
            "best-fit",
            params=best["params"],
            engine_name=best["engine_name"],
            description=(
                f"Best historical fit by peak-cost backtest at {peak_rate}/kWh, "
                "charge-biased for near-ties."
            ),
            source="backtest-best",
            score_peak_grid=best["total_peak_grid_kwh"],
            score_cost=best["total_cost"],
        )
    return ranked


def format_backtest_report(results, peak_rate=PEAK_VALUE_RATE):
    lines = []
    lines.append(f"Backtest results (peak rate {peak_rate}/kWh):")
    lines.append(
        f"Charge-biased near-tie tolerance: {PROFILE_SELECTION_COST_TOLERANCE:.2f}"
    )
    lines.append("")
    lines.append(f"{'Profile':<34} {'Engine':<18} {'Days':>4} {'Peak kWh':>10} {'Cost':>10} {'Avg SOC':>8} {'Solar MAE':>10}")
    lines.append("-" * 108)
    for result in results:
        lines.append(
            f"{result['name']:<34} {result['engine_name']:<18} "
            f"{result['days']:>4} {result['total_peak_grid_kwh']:>10.1f} "
            f"{result['total_cost']:>10.2f} {result['avg_target_soc']:>8} "
            f"{result.get('avg_solar_mae_kwh'):>10}"
        )
    return "\n".join(lines)
