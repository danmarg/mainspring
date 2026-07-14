"""
MCP server — data layer only.

Tools:
  log_meal       log_caffeine    log_alcohol
  amend_log      delete_log
  get_logs       get_daily_metrics
  get_suggested_workout
  get_source_config   set_source_preference

Mounted at /mcp by main.py. Auth is handled by OAuth 2.1 via FastMCP's
built-in auth machinery (MainspringOAuthProvider in mcp_oauth.py).
The login page lives at /mcp-auth/login on the main FastAPI app.
"""

import json
import os
from datetime import date, datetime, timezone
from typing import Optional

from mcp.server import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

from app.db import db, utc_now

_base_url = os.getenv("APP_BASE_URL", "https://your-app.fly.dev")

from app.mcp_oauth import MainspringOAuthProvider

mcp = FastMCP(
    "mainspring",
    host="0.0.0.0",
    streamable_http_path="/",
    auth_server_provider=MainspringOAuthProvider(base_url=_base_url),
    auth=AuthSettings(
        issuer_url=f"{_base_url}/mcp",
        resource_server_url=f"{_base_url}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
    ),
)


# ── helpers ─────────────────────────────────────────────────────────────────

def _ts_or_now(ts: Optional[str]) -> str:
    if ts:
        return ts
    return datetime.now(timezone.utc).isoformat()


def _date_or_today(d: Optional[str]) -> str:
    return d or date.today().isoformat()


# ── log tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def log_meal(
    description: str,
    ts: Optional[str] = None,
    estimated_calories: Optional[int] = None,
    estimated_macros: Optional[dict] = None,
    confidence: Optional[str] = None,
) -> str:
    """Log a meal. ts is UTC ISO-8601 (defaults to now). estimated_macros is
    {protein_g, carbs_g, fat_g}. confidence: 'photo_estimate' | 'user_confirmed'."""
    event_ts = _ts_or_now(ts)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO manual_logs
              (ts, type, description, estimated_calories, estimated_macros_json, confidence, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                event_ts,
                "meal",
                description,
                estimated_calories,
                json.dumps(estimated_macros) if estimated_macros else None,
                confidence,
                utc_now(),
            ),
        )
    return f"Logged meal at {event_ts}: {description}"


@mcp.tool()
def log_caffeine(
    description: str,
    ts: Optional[str] = None,
    amount_mg: Optional[float] = None,
) -> str:
    """Log a caffeine intake. amount_mg is milligrams."""
    event_ts = _ts_or_now(ts)
    with db() as conn:
        conn.execute(
            "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
            (event_ts, "caffeine", description, amount_mg, "mg", utc_now()),
        )
    return f"Logged caffeine at {event_ts}: {description}" + (f" ({amount_mg}mg)" if amount_mg else "")


@mcp.tool()
def log_alcohol(
    description: str,
    ts: Optional[str] = None,
    units: Optional[float] = None,
) -> str:
    """Log alcohol consumption. units is standard UK units (1 unit = 8g ethanol)."""
    event_ts = _ts_or_now(ts)
    with db() as conn:
        conn.execute(
            "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
            (event_ts, "alcohol", description, units, "units", utc_now()),
        )
    return f"Logged alcohol at {event_ts}: {description}" + (f" ({units} units)" if units else "")


# ── amend / delete tools ────────────────────────────────────────────────────

@mcp.tool()
def amend_log(
    log_id: int,
    ts: Optional[str] = None,
    description: Optional[str] = None,
    quantity: Optional[float] = None,
    unit: Optional[str] = None,
    estimated_calories: Optional[int] = None,
    estimated_macros: Optional[dict] = None,
    confidence: Optional[str] = None,
) -> str:
    """Amend an existing manual log entry (meal/caffeine/alcohol/note) by id.
    Only the fields provided are updated; omitted fields are left unchanged.
    Use get_logs to find the id."""
    fields = {
        "ts": ts,
        "description": description,
        "quantity": quantity,
        "unit": unit,
        "estimated_calories": estimated_calories,
        "confidence": confidence,
    }
    if estimated_macros is not None:
        fields["estimated_macros_json"] = json.dumps(estimated_macros)
    fields = {k: v for k, v in fields.items() if v is not None}

    if not fields:
        return "No fields provided to amend."

    with db() as conn:
        existing = conn.execute("SELECT id FROM manual_logs WHERE id=?", (log_id,)).fetchone()
        if not existing:
            return f"Error: no log with id {log_id}"
        set_clause = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE manual_logs SET {set_clause} WHERE id=?",
            (*fields.values(), log_id),
        )
    return f"Amended log {log_id}: " + ", ".join(f"{k}={v}" for k, v in fields.items())


@mcp.tool()
def delete_log(log_id: int) -> str:
    """Delete a manual log entry (meal/caffeine/alcohol/note) by id.
    Use get_logs to find the id."""
    with db() as conn:
        existing = conn.execute("SELECT id FROM manual_logs WHERE id=?", (log_id,)).fetchone()
        if not existing:
            return f"Error: no log with id {log_id}"
        conn.execute("DELETE FROM manual_logs WHERE id=?", (log_id,))
    return f"Deleted log {log_id}"


# ── query tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def get_logs(
    start_date: str,
    end_date: str,
    type: Optional[str] = None,
) -> list[dict]:
    """Return manual logs between start_date and end_date (YYYY-MM-DD).
    type filters to 'meal' | 'caffeine' | 'alcohol' | 'note'."""
    with db() as conn:
        if type:
            rows = conn.execute(
                "SELECT id, ts, type, description, quantity, unit, "
                "estimated_calories, estimated_macros_json, confidence, created_at "
                "FROM manual_logs "
                "WHERE DATE(ts) BETWEEN ? AND ? AND type=? ORDER BY ts",
                (start_date, end_date, type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, type, description, quantity, unit, "
                "estimated_calories, estimated_macros_json, confidence, created_at "
                "FROM manual_logs "
                "WHERE DATE(ts) BETWEEN ? AND ? ORDER BY ts",
                (start_date, end_date),
            ).fetchall()

    return [
        {
            "id": r[0],
            "ts": r[1],
            "type": r[2],
            "description": r[3],
            "quantity": r[4],
            "unit": r[5],
            "estimated_calories": r[6],
            "estimated_macros": json.loads(r[7]) if r[7] else None,
            "confidence": r[8],
            "created_at": r[9],
        }
        for r in rows
    ]


@mcp.tool()
def get_daily_metrics(
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Return normalized daily health metrics between start_date and end_date (YYYY-MM-DD).
    Includes HRV, sleep, stress, body battery, training readiness, training load,
    weight, blood pressure, caffeine, and alcohol."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT date, resting_hr, hrv,
                   sleep_score, sleep_duration_min,
                   sleep_deep_min, sleep_rem_min, sleep_light_min,
                   body_battery_high, body_battery_low,
                   stress_avg, training_readiness,
                   active_zone_minutes, spo2_avg, breathing_rate,
                   vo2max, steps,
                   acute_training_load, chronic_training_load, training_load_ratio,
                   caffeine_mg, alcohol_units, calories_estimated,
                   weight_kg, bp_systolic, bp_diastolic, bp_pulse,
                   source_flags_json
            FROM daily_metrics
            WHERE date BETWEEN ? AND ?
            ORDER BY date
            """,
            (start_date, end_date),
        ).fetchall()

    return [
        {
            "date": r[0],
            "resting_hr": r[1],
            "hrv": r[2],
            "sleep_score": r[3],
            "sleep_duration_min": r[4],
            "sleep_deep_min": r[5],
            "sleep_rem_min": r[6],
            "sleep_light_min": r[7],
            "body_battery_high": r[8],
            "body_battery_low": r[9],
            "stress_avg": r[10],
            "training_readiness": r[11],
            "active_zone_minutes": r[12],
            "spo2_avg": r[13],
            "breathing_rate": r[14],
            "vo2max": r[15],
            "steps": r[16],
            "acute_training_load": r[17],
            "chronic_training_load": r[18],
            "training_load_ratio": r[19],
            "caffeine_mg": r[20],
            "alcohol_units": r[21],
            "calories_estimated": r[22],
            "weight_kg": r[23],
            "bp_systolic": r[24],
            "bp_diastolic": r[25],
            "bp_pulse": r[26],
            "sources": json.loads(r[27]) if r[27] else {},
        }
        for r in rows
    ]


@mcp.tool()
def get_suggested_workout(date: Optional[str] = None) -> dict | None:
    """Return Garmin's suggested workout for a given date (YYYY-MM-DD, defaults to today),
    plus the training-load context (readiness, acute/chronic load) behind it."""
    d = _date_or_today(date)
    with db() as conn:
        sw = conn.execute(
            "SELECT workout_type, description, target_duration_min, target_intensity, raw_json "
            "FROM suggested_workouts WHERE date=? ORDER BY source LIMIT 1",
            (d,),
        ).fetchone()

        metrics = conn.execute(
            "SELECT training_readiness, hrv, stress_avg FROM daily_metrics WHERE date=?",
            (d,),
        ).fetchone()

        load = conn.execute(
            """
            SELECT metric, value FROM raw_daily_metrics
            WHERE date=? AND source='garmin'
              AND metric IN ('acute_training_load','chronic_training_load','training_load_ratio')
            """,
            (d,),
        ).fetchall()

    if not sw:
        return None

    context = {}
    if metrics:
        context = {
            "training_readiness": metrics[0],
            "hrv": metrics[1],
            "stress_avg": metrics[2],
        }
    for row in load:
        context[row[0]] = row[1]

    return {
        "date": d,
        "workout_type": sw[0],
        "description": sw[1],
        "target_duration_min": sw[2],
        "target_intensity": sw[3],
        "training_context": context,
    }


# ── source preference tools ──────────────────────────────────────────────────

@mcp.tool()
def get_source_config() -> dict:
    """Return current per-metric source preferences and the default priority order."""
    from app.db import DEFAULT_SOURCE_PRIORITY
    with db() as conn:
        rows = conn.execute(
            "SELECT metric, canonical_source FROM source_config ORDER BY metric"
        ).fetchall()
    return {
        "default_priority": DEFAULT_SOURCE_PRIORITY,
        "overrides": {r[0]: r[1] for r in rows},
    }


@mcp.tool()
def set_source_preference(metric: str, source: str) -> str:
    """Set the canonical source for a metric. source must be 'garmin' or 'google_health'.
    Use 'activities' as the metric to set the preference for activity dedup."""
    valid_sources = ("garmin", "google_health")
    if source not in valid_sources:
        return f"Error: source must be one of {valid_sources}"
    with db() as conn:
        conn.execute(
            "INSERT INTO source_config(metric, canonical_source) VALUES (?,?) "
            "ON CONFLICT(metric) DO UPDATE SET canonical_source=excluded.canonical_source",
            (metric, source),
        )
    return f"Set {metric} → {source}"


@mcp.tool()
def log_note(description: str, ts: Optional[str] = None) -> str:
    """Log a free-text note. ts is UTC ISO-8601 (defaults to now)."""
    event_ts = _ts_or_now(ts)
    with db() as conn:
        conn.execute(
            "INSERT INTO manual_logs(ts, type, description, created_at) VALUES (?,?,?,?)",
            (event_ts, "note", description, utc_now()),
        )
    return f"Logged note at {event_ts}: {description}"


@mcp.tool()
def log_weight(kg: float, ts: Optional[str] = None) -> str:
    """Log a weight measurement in kilograms. ts is UTC ISO-8601 (defaults to now)."""
    event_ts = _ts_or_now(ts)
    with db() as conn:
        conn.execute(
            "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
            (event_ts, "weight", f"{kg}kg", kg, "kg", utc_now()),
        )
    return f"Logged weight at {event_ts}: {kg}kg"


@mcp.tool()
def log_blood_pressure(
    systolic: int,
    diastolic: int,
    pulse: Optional[int] = None,
    ts: Optional[str] = None,
) -> str:
    """Log a blood pressure reading in mmHg. pulse is beats per minute (optional).
    ts is UTC ISO-8601 (defaults to now)."""
    event_ts = _ts_or_now(ts)
    desc = f"{systolic}/{diastolic}" + (f" pulse {pulse}" if pulse else "")
    with db() as conn:
        conn.execute(
            "INSERT INTO manual_logs(ts, type, description, quantity, unit, estimated_macros_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                event_ts, "blood_pressure", desc,
                systolic, "mmHg",
                json.dumps({"systolic": systolic, "diastolic": diastolic, "pulse": pulse}),
                utc_now(),
            ),
        )
    return f"Logged BP at {event_ts}: {desc}"


@mcp.tool()
def get_correlations(
    days: int = 90,
    lags: Optional[list] = None,
    inputs: Optional[list] = None,
    outputs: Optional[list] = None,
    min_pairs: int = 14,
    method: str = "pearson",
) -> dict:
    """Compute lag-shifted correlations between behavior inputs and recovery outputs.
    lag=1 means output measured 1 day after input (e.g. last night's alcohol → this morning's HRV).
    method: 'pearson' or 'spearman'.
    Returns correlations sorted by absolute strength plus a plain-text top_findings summary."""
    from app.analysis import compute_correlations
    with db() as conn:
        return compute_correlations(
            conn,
            inputs=inputs,
            outputs=outputs,
            lags=lags or [0, 1, 2],
            days=days,
            min_pairs=min_pairs,
            method=method,
        )


@mcp.tool()
def get_workout_context(date: Optional[str] = None, hrv_window: int = 7) -> dict:
    """Rich context for workout planning: today's metrics, HRV trend, training load
    status, Garmin's suggested workout, recent activities, and personalized insights
    based on historical correlations (e.g. 'you had 2 drinks yesterday; historically
    that reduces your HRV by ~8ms the next morning')."""
    from app.analysis import compute_correlations

    d = _date_or_today(date)

    with db() as conn:
        # Today's metrics
        today_row = conn.execute(
            """
            SELECT hrv, sleep_score, sleep_duration_min, body_battery_high,
                   stress_avg, training_readiness,
                   acute_training_load, chronic_training_load, training_load_ratio,
                   weight_kg, resting_hr
            FROM daily_metrics WHERE date=?
            """,
            (d,),
        ).fetchone()

        # HRV window for trend
        hrv_rows = conn.execute(
            "SELECT date, hrv FROM daily_metrics WHERE date <= ? AND hrv IS NOT NULL "
            "ORDER BY date DESC LIMIT ?",
            (d, hrv_window),
        ).fetchall()

        # Yesterday behavior
        from datetime import date as date_cls, timedelta
        yesterday = (date_cls.fromisoformat(d) - timedelta(days=1)).isoformat()
        yest_row = conn.execute(
            "SELECT alcohol_units, caffeine_mg, calories_estimated FROM daily_metrics WHERE date=?",
            (yesterday,),
        ).fetchone()

        # Garmin suggestion
        sw_row = conn.execute(
            "SELECT workout_type, description, target_duration_min, target_intensity "
            "FROM suggested_workouts WHERE date=? ORDER BY source LIMIT 1",
            (d,),
        ).fetchone()

        # Recent activities (last 7)
        act_rows = conn.execute(
            "SELECT date, type, duration_s, avg_hr FROM activities "
            "WHERE date <= ? ORDER BY date DESC, start_time DESC LIMIT 7",
            (d,),
        ).fetchall()

        # Correlations for personalized insights
        try:
            corr_result = compute_correlations(
                conn,
                inputs=["alcohol_units", "caffeine_mg"],
                outputs=["hrv", "sleep_score", "stress_avg"],
                lags=[1],
                days=90,
                min_pairs=10,
            )
            correlations = corr_result.get("correlations", [])
        except Exception:
            correlations = []

    # Build today dict
    today: dict = {}
    if today_row:
        hrv_val = today_row[0]
        today = {
            "hrv": hrv_val,
            "sleep_score": today_row[1],
            "sleep_duration_min": today_row[2],
            "body_battery_high": today_row[3],
            "stress_avg": today_row[4],
            "training_readiness": today_row[5],
            "acute_training_load": today_row[6],
            "chronic_training_load": today_row[7],
            "training_load_ratio": today_row[8],
            "weight_kg": today_row[9],
            "resting_hr": today_row[10],
        }

        # HRV trend
        hrv_vals = [r[1] for r in hrv_rows if r[1] is not None]
        if len(hrv_vals) >= 4:
            recent_avg = sum(hrv_vals[:3]) / 3
            older_avg = sum(hrv_vals[3:]) / len(hrv_vals[3:])
            if older_avg > 0:
                pct_change = (recent_avg - older_avg) / older_avg
                if pct_change > 0.03:
                    trend = "improving"
                elif pct_change < -0.03:
                    trend = "declining"
                else:
                    trend = "stable"
            else:
                trend = "stable"
            today["hrv_trend"] = trend
            today["hrv_recent_avg"] = round(recent_avg, 1)
        elif hrv_vals:
            today["hrv_trend"] = "insufficient_data"
            today["hrv_recent_avg"] = round(sum(hrv_vals) / len(hrv_vals), 1)

        # Load status
        ratio = today_row[8]
        if ratio is not None:
            if ratio < 0.8:
                load_status = "detraining"
            elif ratio < 1.0:
                load_status = "maintenance"
            elif ratio < 1.3:
                load_status = "productive"
            elif ratio < 1.5:
                load_status = "slight_overreaching"
            else:
                load_status = "overreaching"
            today["load_status"] = load_status

    # Yesterday behavior
    yesterday_behavior: dict = {}
    if yest_row:
        yesterday_behavior = {
            "alcohol_units": yest_row[0],
            "caffeine_mg": yest_row[1],
            "calories_estimated": yest_row[2],
        }

    # Personalized insights
    insights = []
    for c in correlations:
        if abs(c["r"]) < 0.15:
            continue
        inp = c["input"]
        out = c["output"]
        yval = yesterday_behavior.get(inp)
        if not yval:
            continue
        direction = "lower" if c["r"] < 0 else "higher"
        insights.append(
            f"You had {yval} {_unit_for(inp)} yesterday. "
            f"Based on {c['n_pairs']} days of history, this is associated with "
            f"{direction} {out} today (r={c['r']})."
        )

    # Garmin suggestion
    garmin_suggestion = None
    if sw_row:
        garmin_suggestion = {
            "workout_type": sw_row[0],
            "description": sw_row[1],
            "target_duration_min": sw_row[2],
            "target_intensity": sw_row[3],
        }

    # Recent activities
    recent_activities = [
        {
            "date": r[0],
            "type": r[1],
            "duration_min": round(r[2] / 60) if r[2] else None,
            "avg_hr": r[3],
        }
        for r in act_rows
    ]

    return {
        "date": d,
        "today": today,
        "yesterday_behavior": yesterday_behavior,
        "garmin_suggestion": garmin_suggestion,
        "personalized_insights": insights,
        "recent_activities": recent_activities,
    }


def _unit_for(metric: str) -> str:
    units = {"alcohol_units": "units of alcohol", "caffeine_mg": "mg of caffeine"}
    return units.get(metric, metric)


# ── ASGI app ─────────────────────────────────────────────────────────────────

def build_mcp_app():
    """
    Return an ASGI app suitable for mounting at /mcp.
    If MCP_TOKEN is not set, returns None (caller skips the mount).
    MCP_TOKEN is reused as the login PIN for the OAuth authorization page.
    """
    if not os.getenv("MCP_TOKEN"):
        return None
    return mcp.streamable_http_app()
