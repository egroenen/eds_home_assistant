"""Database initialization and parameter access."""

import logging
import sqlite3
from datetime import datetime

from .config import DB_PATH, DEFAULT_PARAMS

log = logging.getLogger("solar_optimizer")


def get_db():
    db = sqlite3.connect(str(DB_PATH), timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.row_factory = sqlite3.Row
    init_db(db)
    return db


def init_db(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS daily_plan (
            date               TEXT PRIMARY KEY,
            created_at         TEXT NOT NULL,
            solar_forecast_kwh REAL,
            weather_condition  TEXT,
            cloud_coverage_pct REAL,
            precipitation_mm   REAL,
            temperature_high   REAL,
            adjusted_solar_kwh REAL,
            overnight_soc_target INTEGER,
            energy_deficit_kwh REAL,
            correction_factor  REAL,
            slot1_soc INTEGER, slot2_soc INTEGER, slot3_soc INTEGER,
            slot4_soc INTEGER, slot5_soc INTEGER, slot6_soc INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily_outcome (
            date                   TEXT PRIMARY KEY,
            recorded_at            TEXT NOT NULL,
            actual_production_kwh  REAL,
            actual_consumption_kwh REAL,
            grid_bought_kwh        REAL,
            grid_sold_kwh          REAL,
            battery_charge_kwh     REAL,
            battery_discharge_kwh  REAL,
            battery_soc_at_record  REAL,
            peak_grid_used         INTEGER DEFAULT 0,
            peak_grid_kwh          REAL DEFAULT 0,
            forecast_accuracy      REAL,
            weather_condition      TEXT
        );

        CREATE TABLE IF NOT EXISTS hourly_log (
            timestamp       TEXT PRIMARY KEY,
            date            TEXT NOT NULL,
            hour            INTEGER NOT NULL,
            battery_soc     REAL,
            grid_power_w    REAL,
            pv_power_w      REAL,
            load_power_w    REAL,
            grid_bought_kwh REAL,
            grid_sold_kwh   REAL
        );

        CREATE TABLE IF NOT EXISTS learning_params (
            param_key   TEXT PRIMARY KEY,
            param_value REAL NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_hourly_date ON hourly_log(date);

        CREATE TABLE IF NOT EXISTS forecast_tracking (
            date              TEXT NOT NULL,
            hour              INTEGER NOT NULL,
            model_cloud_kwh   REAL,
            model_rad_kwh     REAL,
            actual_pv_wh      REAL,
            est_consumption_kwh REAL,
            est_battery_soc   REAL,
            actual_consumption_wh REAL,
            actual_battery_soc REAL,
            weather_condition TEXT,
            cloud_pct         REAL,
            temperature       REAL,
            shortwave_wm2     REAL,
            PRIMARY KEY (date, hour)
        );
    """)
    db.commit()

    # Add temperature_high column to daily_outcome if missing (migration)
    try:
        db.execute("SELECT temperature_high FROM daily_outcome LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE daily_outcome ADD COLUMN temperature_high REAL")
        db.commit()

    # Add forecast_tracking columns if missing (migration)
    for col, coltype in [("est_consumption_kwh", "REAL"),
                         ("est_battery_soc", "REAL"),
                         ("actual_consumption_wh", "REAL"),
                         ("actual_battery_soc", "REAL"),
                         ("weather_condition", "TEXT"),
                         ("cloud_pct", "REAL"),
                         ("temperature", "REAL"),
                         ("shortwave_wm2", "REAL")]:
        try:
            db.execute(f"SELECT {col} FROM forecast_tracking LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE forecast_tracking ADD COLUMN {col} {coltype}")
            db.commit()

    # Add frozen_detail column to daily_plan if missing (migration)
    try:
        db.execute("SELECT frozen_detail FROM daily_plan LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE daily_plan ADD COLUMN frozen_detail TEXT")
        db.commit()

    # Seed default params if empty
    cursor = db.execute("SELECT COUNT(*) FROM learning_params")
    if cursor.fetchone()[0] == 0:
        now = datetime.now().isoformat()
        for key, val in DEFAULT_PARAMS.items():
            db.execute(
                "INSERT INTO learning_params (param_key, param_value, updated_at) VALUES (?, ?, ?)",
                (key, val, now),
            )
        db.commit()
        log.info("Initialized default learning parameters")


def get_param(db, key):
    row = db.execute("SELECT param_value FROM learning_params WHERE param_key=?", (key,)).fetchone()
    if row:
        return row[0]
    return DEFAULT_PARAMS.get(key)


def set_param(db, key, value):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT OR REPLACE INTO learning_params (param_key, param_value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )
    db.commit()
