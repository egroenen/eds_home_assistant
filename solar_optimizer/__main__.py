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
    python3 -m solar_optimizer engines      # List available solar engines
    python3 -m solar_optimizer profile list
    python3 -m solar_optimizer profile save NAME
    python3 -m solar_optimizer profile load NAME
    python3 -m solar_optimizer profile show NAME
    python3 -m solar_optimizer backtest     # Backtest candidate profiles against history

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
from .planner import calculate_plan, make_failsafe_plan, is_overnight_charging_window
from .registers import write_tou_config, store_plan
from .polling import poll_snapshot
from .learning import record_outcome, backup_db
from .dashboard import write_dashboard_status, write_history_status, show_status, show_history
from .engines import list_engines
from .profiles import (
    clear_meta,
    ensure_original_profile,
    get_active_engine_name,
    get_active_profile_name,
    get_profile,
    get_profile_names,
    load_profile,
    save_profile,
)
from .backtest import format_backtest_report, run_backtest

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


ONLINE_MODES = {"optimize", "dry-run", "record", "poll", "status"}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1].lower()
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    setup_logging(verbose)
    db = get_db()
    ha = None

    if mode in ONLINE_MODES:
        load_env()
        server = os.environ["HASS_SERVER"]
        token = os.environ["HASS_TOKEN"]
        ha = HomeAssistantAPI(server, token)

    try:
        if mode == "optimize":
            plan = calculate_plan(ha, db)
            store_plan(db, plan)
            if is_overnight_charging_window():
                log.info("Overnight charging in progress — plan saved to DB, "
                         "skipping full register write (poll will pick up new target)")
                print(f"Optimization complete for {plan['date']} "
                      f"(registers deferred — overnight charging active):")
                success = True
            else:
                success = write_tou_config(ha, plan)
                print(f"Optimization complete for {plan['date']}:")
            write_dashboard_status(ha, db)
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
            write_history_status(db)

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
            clear_meta(db, "active_profile")
            print(f"Set {key}: {old} -> {value}")

        elif mode == "reset":
            now = datetime.now().isoformat()
            for key, val in DEFAULT_PARAMS.items():
                db.execute(
                    "INSERT OR REPLACE INTO learning_params (param_key, param_value, updated_at) VALUES (?, ?, ?)",
                    (key, val, now),
                )
            db.commit()
            clear_meta(db, "active_profile")
            print("Learning parameters reset to defaults")

        elif mode == "engines":
            print("\nAvailable engines:")
            for engine in list_engines():
                marker = " *" if engine["name"] == get_active_engine_name(db) else ""
                print(f"  {engine['name']:<18} {engine['description']}{marker}")
            print()

        elif mode == "profile":
            if len(sys.argv) < 3:
                print("Usage: solar_optimizer profile list|save|load|show NAME")
                sys.exit(1)
            action = sys.argv[2].lower()
            ensure_original_profile(db)

            if action == "list":
                active = get_active_profile_name(db)
                print("\nProfiles:")
                for name in get_profile_names(db):
                    profile = get_profile(db, name)
                    marker = " *" if name == active else ""
                    print(f"  {name:<30} engine={profile['engine_name']}{marker}")
                print()
            elif action == "save":
                if len(sys.argv) < 4:
                    print("Usage: solar_optimizer profile save NAME")
                    sys.exit(1)
                name = sys.argv[3]
                save_profile(db, name)
                print(f"Saved current parameters as profile '{name}'")
            elif action == "load":
                if len(sys.argv) < 4:
                    print("Usage: solar_optimizer profile load NAME")
                    sys.exit(1)
                name = sys.argv[3]
                profile = load_profile(db, name)
                print(f"Loaded profile '{name}' (engine={profile['engine_name']})")
            elif action == "show":
                if len(sys.argv) < 4:
                    print("Usage: solar_optimizer profile show NAME")
                    sys.exit(1)
                name = sys.argv[3]
                profile = get_profile(db, name)
                if not profile:
                    print(f"Profile not found: {name}")
                    sys.exit(1)
                print(f"\nProfile: {name}")
                print(f"  Engine: {profile['engine_name']}")
                print(f"  Source: {profile['source']}")
                print(f"  Description: {profile['description']}")
                if profile["score_peak_grid"] is not None:
                    print(f"  Backtest peak kWh: {profile['score_peak_grid']:.1f}")
                if profile["score_cost"] is not None:
                    print(f"  Backtest cost: {profile['score_cost']:.2f}")
                print("\n  Parameters:")
                for key in sorted(profile["params"]):
                    print(f"    {key:30s} = {profile['params'][key]}")
                print()
            else:
                print(f"Unknown profile action: {action}")
                sys.exit(1)

        elif mode == "backtest":
            ensure_original_profile(db)
            results = run_backtest(db)
            print()
            print(format_backtest_report(results))
            if results:
                best = results[0]
                print(
                    f"\nBest fit: {best['name']} "
                    f"(engine={best['engine_name']}, peak={best['total_peak_grid_kwh']:.1f}kWh, "
                    f"cost={best['total_cost']:.2f})\n"
                )

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
                store_plan(db, plan)
                if not is_overnight_charging_window():
                    write_tou_config(ha, plan)
            except Exception as e2:
                log.critical(f"Failsafe also failed: {e2}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
