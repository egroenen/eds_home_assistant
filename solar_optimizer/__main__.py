"""Solar Battery Optimizer for Deye Hybrid Inverter.

Self-learning system that optimizes Time-of-Use battery charging based on
solar production forecasts and weather conditions. Controls the Deye inverter
via Home Assistant's Solarman integration.

Usage:
    python3 -m solar_optimizer optimize     # Calculate + write tomorrow's TOU settings
    python3 -m solar_optimizer dry-run      # Like optimize but print-only
    python3 -m solar_optimizer record       # Record today's outcomes + run learning
    python3 -m solar_optimizer poll         # Record hourly snapshot (for cron)
    python3 -m solar_optimizer status       # Show current state + learning params
    python3 -m solar_optimizer history [N]  # Show last N days (default 14)
    python3 -m solar_optimizer set-param KEY VALUE  # Manually set a learning parameter
    python3 -m solar_optimizer reset        # Reset learning parameters to defaults

Environment:
    HASS_SERVER  - Home Assistant URL (e.g. http://localhost:8123)
    HASS_TOKEN   - Long-lived access token
"""

import logging
import os
import sys
from datetime import datetime

from . import config
from .config import SCRIPT_DIR, DEFAULT_PARAMS, SLOT_TIMES, SLOT_POWERS, SLOT_ENABLES
from .ha_api import HomeAssistantAPI
from .db import get_db, get_param, set_param
from .planner import calculate_plan, make_failsafe_plan
from .registers import write_tou_config, store_plan
from .polling import poll_snapshot
from .learning import record_outcome, backup_db
from .dashboard import write_dashboard_status, show_status, show_history

log = logging.getLogger("solar_optimizer")


def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")

    log_path = SCRIPT_DIR / "solar_optimizer.log"
    fh = logging.FileHandler(log_path)
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)


def load_env():
    """Load .env file if it exists, populate os.environ."""
    if config.ENV_PATH.exists():
        for line in config.ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    server = os.environ.get("HASS_SERVER")
    token = os.environ.get("HASS_TOKEN")
    if not server or not token:
        log.error("HASS_SERVER and HASS_TOKEN must be set")
        sys.exit(1)

    config.METOCEAN_API_KEY = os.environ.get("METOCEAN_API_KEY")
    if not config.METOCEAN_API_KEY:
        log.warning("METOCEAN_API_KEY not set — MetOcean forecasts will be unavailable")

    return server, token


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
            write_dashboard_status(ha, db)
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
            backup_db()
            record_outcome(ha, db)
            write_dashboard_status(ha, db)

        elif mode == "poll":
            poll_snapshot(ha, db)
            write_dashboard_status(ha, db)

        elif mode == "status":
            show_status(ha, db)

        elif mode == "history":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 14
            show_history(db, days)

        elif mode == "set-param":
            if len(sys.argv) < 4:
                print("Usage: solar_optimizer set-param KEY VALUE")
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
