"""Dynamic overnight charge adjustment."""

import logging
import time

from .config import (
    SENSORS, CHARGE_DEADLINE_HOUR, CHARGE_DEADLINE_MIN,
    PEAK_START_HOUR, BATTERY_RESERVE_PCT, USABLE_CAPACITY_KWH,
    OUTAGE_RESERVE_PCT, CHARGE_POWER_DEFAULT, CHARGE_POWER_MAX,
    TOU_REGS,
)
from .db import get_param, set_param
from .models import (
    build_hourly_solar, build_hourly_solar_radiation,
    build_hourly_consumption, get_seasonal_consumption,
    get_temp_factor, get_day_factor, simulate_battery_hourly,
    get_sw_efficiency_map,
)
from .metocean import get_metocean_hourly
from .registers import encode_time

log = logging.getLogger("solar_optimizer")


def adjust_overnight_charging(ha, db, now, current_soc):
    """Dynamically adjust charge start time and power to hit target by 06:30.

    Called every hour by the poll cron. Only acts during 00:00-06:30.
    Writes Slot 1/2 time, SOC, and power registers to delay charging as
    late as possible while still reaching the target SOC by the deadline.
    """
    deadline_hour = CHARGE_DEADLINE_HOUR
    deadline_min = CHARGE_DEADLINE_MIN
    deadline_minutes = deadline_hour * 60 + deadline_min

    now_minutes = now.hour * 60 + now.minute

    if now_minutes >= deadline_minutes or now.hour >= PEAK_START_HOUR:
        return

    if current_soc is None:
        log.warning("Cannot adjust charging: battery SOC unavailable")
        return

    today = now.date().isoformat()
    plan_row = db.execute(
        "SELECT overnight_soc_target FROM daily_plan WHERE date=?", (today,)
    ).fetchone()

    if not plan_row:
        log.info("No plan found for today, skipping charge adjustment")
        return

    target_soc = plan_row["overnight_soc_target"]

    if current_soc >= target_soc:
        log.info(f"SOC {current_soc}% >= target {target_soc}%, "
                 f"no charging needed")
        _write_charge_slots(ha, reserve_only=True)
        return

    soc_needed = target_soc - current_soc
    energy_kwh = (soc_needed / 100.0) * USABLE_CAPACITY_KWH

    minutes_left = deadline_minutes - now_minutes
    hours_left = minutes_left / 60.0

    charge_hours_at_default = energy_kwh / (CHARGE_POWER_DEFAULT / 1000.0)
    charge_hours_at_max = energy_kwh / (CHARGE_POWER_MAX / 1000.0)

    if charge_hours_at_max > hours_left:
        log.warning(f"Tight deadline! Need {energy_kwh:.1f}kWh in {hours_left:.1f}h, "
                    f"even {CHARGE_POWER_MAX}W needs {charge_hours_at_max:.1f}h. "
                    f"Starting immediately at max power.")
        charge_power = CHARGE_POWER_MAX
        start_hour = now.hour
        start_min = 0
    elif charge_hours_at_default <= hours_left:
        charge_power = CHARGE_POWER_DEFAULT
        buffer_minutes = 15
        start_minutes = deadline_minutes - int(charge_hours_at_default * 60) - buffer_minutes
        start_minutes = max(start_minutes, now_minutes)
        start_hour = start_minutes // 60
        start_min = start_minutes % 60
    else:
        min_power_kw = energy_kwh / hours_left
        charge_power_w = min_power_kw * 1000 * 1.2
        charge_power = min(CHARGE_POWER_MAX,
                           int((charge_power_w + 499) // 500) * 500)
        charge_hours = energy_kwh / (charge_power / 1000.0)
        buffer_minutes = 15
        start_minutes = deadline_minutes - int(charge_hours * 60) - buffer_minutes
        start_minutes = max(start_minutes, now_minutes)
        start_hour = start_minutes // 60
        start_min = start_minutes % 60

    start_min = (start_min // 15) * 15

    original_target = target_soc
    target_soc = _maybe_revise_target(ha, db, today, target_soc, current_soc)

    if target_soc != original_target:
        db.execute(
            "UPDATE daily_plan SET overnight_soc_target=?, slot1_soc=?, slot2_soc=? WHERE date=?",
            (target_soc, OUTAGE_RESERVE_PCT, target_soc, today))
        db.commit()

    soc_needed_revised = target_soc - current_soc
    if soc_needed_revised <= 0:
        log.info(f"Revised target {target_soc}% already met at SOC {current_soc}%")
        _write_charge_slots(ha, reserve_only=True)
        return
    if soc_needed_revised != soc_needed:
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
    """Revise SOC target using hourly battery simulation."""
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
        sw_eff_map = get_sw_efficiency_map(db)
        solar_rad = build_hourly_solar_radiation(raw_solar, hourly, sw_eff_map)

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

        max_soc = get_param(db, "max_overnight_soc")
        revised = max(reserve_target, min(max_soc, revised))
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
    """Write Slot 1 and Slot 2 registers to control overnight charging."""
    writes = []

    if reserve_only:
        writes.append((TOU_REGS["soc"][0], OUTAGE_RESERVE_PCT))
        writes.append((TOU_REGS["soc"][1], OUTAGE_RESERVE_PCT))
        writes.append((TOU_REGS["power"][0], CHARGE_POWER_DEFAULT))
        writes.append((TOU_REGS["power"][1], CHARGE_POWER_DEFAULT))
        writes.append((TOU_REGS["time"][0], encode_time(0, 0)))
        writes.append((TOU_REGS["time"][1], encode_time(4, 0)))
    else:
        writes.append((TOU_REGS["time"][0], encode_time(0, 0)))
        writes.append((TOU_REGS["soc"][0], OUTAGE_RESERVE_PCT))
        writes.append((TOU_REGS["power"][0], CHARGE_POWER_DEFAULT))

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
