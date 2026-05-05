"""Named parameter profile storage for the solar optimizer."""

from datetime import datetime

from .config import DEFAULT_PARAMS


def get_meta(db, key, default=None):
    row = db.execute(
        "SELECT meta_value FROM optimizer_meta WHERE meta_key=?",
        (key,),
    ).fetchone()
    return row["meta_value"] if row else default


def set_meta(db, key, value):
    now = datetime.now().isoformat()
    db.execute(
        """
        INSERT OR REPLACE INTO optimizer_meta (meta_key, meta_value, updated_at)
        VALUES (?, ?, ?)
        """,
        (key, str(value), now),
    )
    db.commit()


def clear_meta(db, key):
    db.execute("DELETE FROM optimizer_meta WHERE meta_key=?", (key,))
    db.commit()


def get_active_profile_name(db):
    return get_meta(db, "active_profile")


def set_active_profile_name(db, profile_name):
    if profile_name:
        set_meta(db, "active_profile", profile_name)
    else:
        clear_meta(db, "active_profile")


def get_active_engine_name(db):
    return get_meta(db, "active_engine", "radiation")


def set_active_engine_name(db, engine_name):
    set_meta(db, "active_engine", engine_name)


def get_current_params(db):
    rows = db.execute(
        "SELECT param_key, param_value FROM learning_params ORDER BY param_key"
    ).fetchall()
    current = {r["param_key"]: r["param_value"] for r in rows}
    return {key: current.get(key, value) for key, value in DEFAULT_PARAMS.items()}


def get_profile_names(db):
    rows = db.execute(
        "SELECT name FROM parameter_profile ORDER BY created_at, name"
    ).fetchall()
    return [r["name"] for r in rows]


def get_profile(db, name):
    row = db.execute(
        "SELECT * FROM parameter_profile WHERE name=?",
        (name,),
    ).fetchone()
    if not row:
        return None
    values = db.execute(
        """
        SELECT param_key, param_value
        FROM parameter_profile_value
        WHERE profile_name=?
        ORDER BY param_key
        """,
        (name,),
    ).fetchall()
    profile = dict(row)
    profile["params"] = {r["param_key"]: r["param_value"] for r in values}
    return profile


def save_profile(
    db,
    name,
    params=None,
    engine_name=None,
    description=None,
    source="manual",
    score_peak_grid=None,
    score_cost=None,
):
    if params is None:
        params = get_current_params(db)
    if engine_name is None:
        engine_name = get_active_engine_name(db)

    now = datetime.now().isoformat()
    db.execute(
        """
        INSERT OR REPLACE INTO parameter_profile
        (name, created_at, description, engine_name, source, score_peak_grid, score_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, now, description, engine_name, source, score_peak_grid, score_cost),
    )
    db.execute("DELETE FROM parameter_profile_value WHERE profile_name=?", (name,))
    for key in sorted(DEFAULT_PARAMS):
        db.execute(
            """
            INSERT INTO parameter_profile_value (profile_name, param_key, param_value)
            VALUES (?, ?, ?)
            """,
            (name, key, float(params.get(key, DEFAULT_PARAMS[key]))),
        )
    db.commit()


def load_profile(db, name):
    profile = get_profile(db, name)
    if not profile:
        raise ValueError(f"Profile not found: {name}")

    now = datetime.now().isoformat()
    for key in sorted(DEFAULT_PARAMS):
        db.execute(
            """
            INSERT OR REPLACE INTO learning_params (param_key, param_value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, float(profile["params"].get(key, DEFAULT_PARAMS[key])), now),
        )
    db.commit()
    set_active_profile_name(db, name)
    set_active_engine_name(db, profile["engine_name"])
    return profile


def ensure_original_profile(db):
    if get_profile(db, "original"):
        return
    save_profile(
        db,
        "original",
        params=get_current_params(db),
        engine_name=get_active_engine_name(db),
        description="Snapshot of the working parameter set before profile tuning.",
        source="snapshot",
    )

