"""TOU register writing and plan storage."""

import logging
import time
from datetime import datetime

from .config import TOU_REGS, SLOT_TIMES, SLOT_POWERS, SLOT_ENABLES

log = logging.getLogger("solar_optimizer")


def encode_time(hour, minute):
    """Encode time as Deye register value: hour*100 + minute (decimal packed)."""
    return hour * 100 + minute


def write_tou_config(ha, plan):
    """Write all TOU registers to the inverter."""
    slot_socs = plan["slot_socs"]
    writes = []

    for i, (h, m) in enumerate(SLOT_TIMES):
        writes.append((TOU_REGS["time"][i], encode_time(h, m)))

    for i, soc in enumerate(slot_socs):
        writes.append((TOU_REGS["soc"][i], soc))

    for i, power in enumerate(SLOT_POWERS):
        writes.append((TOU_REGS["power"][i], power))

    for i, enable in enumerate(SLOT_ENABLES):
        writes.append((TOU_REGS["enable"][i], enable))

    writes.append((TOU_REGS["tou_master"], 1))

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
