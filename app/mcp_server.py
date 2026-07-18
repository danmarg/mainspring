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
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

def _local_date_utc_bounds(date_str: str, tz_name: str | None) -> tuple[str, str]:
    """Return (utc_start, utc_end_exclusive) for a local calendar date."""
    try:
        tz = ZoneInfo(tz_name or os.getenv("HOME_TZ", "UTC"))
    except (ZoneInfoNotFoundError, TypeError):
        tz = ZoneInfo("UTC")
    d = date.fromisoformat(date_str)
    local_start = datetime(d.year, d.month, d.day, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc).isoformat(), local_end.astimezone(timezone.utc).isoformat()


@mcp.tool()
def get_logs(
    start_date: str,
    end_date: str,
    type: Optional[str] = None,
    tz: Optional[str] = None,
) -> list[dict]:
    """Return manual logs between start_date and end_date (YYYY-MM-DD, inclusive).
    type filters to 'meal' | 'caffeine' | 'alcohol' | 'note'.
    tz: IANA timezone name for the user's local date (e.g. 'America/New_York').
    Always pass tz when you know the user's timezone — logs are stored in UTC and
    date boundaries differ by up to a day without it."""
    utc_start, _ = _local_date_utc_bounds(start_date, tz)
    _, utc_end = _local_date_utc_bounds(end_date, tz)
    with db() as conn:
        if type:
            rows = conn.execute(
                "SELECT id, ts, type, description, quantity, unit, "
                "estimated_calories, estimated_macros_json, confidence, created_at "
                "FROM manual_logs "
                "WHERE ts >= ? AND ts < ? AND type=? ORDER BY ts",
                (utc_start, utc_end, type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, type, description, quantity, unit, "
                "estimated_calories, estimated_macros_json, confidence, created_at "
                "FROM manual_logs "
                "WHERE ts >= ? AND ts < ? ORDER BY ts",
                (utc_start, utc_end),
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
                   rpe,
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
            "rpe": r[27],
            "sources": json.loads(r[28]) if r[28] else {},
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
def log_rpe(
    rpe: int,
    activity_type: Optional[str] = None,
    notes: Optional[str] = None,
    date: Optional[str] = None,
) -> str:
    """Log perceived exertion (RPE 1–10) for today's workout.
    activity_type: e.g. 'running', 'cycling', 'strength'. date defaults to today (YYYY-MM-DD).
    Use after a workout to capture subjective effort — feeds correlation analysis and
    workout context (e.g. 'yesterday was RPE 8; HRV historically drops after hard efforts')."""
    if not 1 <= rpe <= 10:
        return "Error: rpe must be between 1 and 10"
    event_date = date or _date_or_today(None)
    event_ts = f"{event_date}T23:59:00+00:00"
    desc = f"RPE {rpe}/10" + (f" — {activity_type}" if activity_type else "") + (f": {notes}" if notes else "")
    with db() as conn:
        conn.execute(
            "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
            (event_ts, "rpe", desc, float(rpe), "/10", utc_now()),
        )
    return f"Logged RPE {rpe}/10 for {event_date}" + (f" ({activity_type})" if activity_type else "")


@mcp.tool()
def set_training_goal(metric: str, value: float, unit: Optional[str] = None) -> str:
    """Set a steady-state weekly training target.
    Common metrics: weekly_runs, weekly_volume_km, weekly_strength_sessions.
    Examples: set_training_goal('weekly_runs', 3), set_training_goal('weekly_volume_km', 50, 'km')."""
    with db() as conn:
        conn.execute(
            "INSERT INTO training_goals(metric, value, unit, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(metric) DO UPDATE SET value=excluded.value, unit=excluded.unit, updated_at=excluded.updated_at",
            (metric, value, unit, utc_now()),
        )
    return f"Set training goal: {metric} = {value}" + (f" {unit}" if unit else "")


@mcp.tool()
def delete_training_goal(metric: str) -> str:
    """Delete a training goal by metric name (e.g. 'weekly_runs', 'weekly_volume_km')."""
    with db() as conn:
        cur = conn.execute("DELETE FROM training_goals WHERE metric=?", (metric,))
    if cur.rowcount == 0:
        return f"No training goal found for '{metric}'"
    return f"Deleted training goal: {metric}"


@mcp.tool()
def get_training_goals() -> dict:
    """Return current weekly training targets and upcoming goal events."""
    with db() as conn:
        goal_rows = conn.execute(
            "SELECT metric, value, unit FROM training_goals ORDER BY metric"
        ).fetchall()
        event_rows = conn.execute(
            "SELECT id, date, type, description, goal_description FROM training_events "
            "WHERE status='upcoming' ORDER BY date"
        ).fetchall()
    return {
        "weekly_targets": {r[0]: {"value": r[1], "unit": r[2]} for r in goal_rows},
        "upcoming_events": [
            {"id": r[0], "date": r[1], "type": r[2], "description": r[3], "goal": r[4]}
            for r in event_rows
        ],
    }


@mcp.tool()
def add_training_event(
    date: str,
    type: str,
    description: str,
    goal_description: Optional[str] = None,
) -> str:
    """Add a goal race or target event.
    type: 'marathon' | 'half_marathon' | '10k' | '5k' | 'triathlon' | 'cycling_event' | 'other'.
    goal_description: e.g. 'sub-4h', 'finish', 'PR'. date is YYYY-MM-DD."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO training_events(date, type, description, goal_description, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (date, type, description, goal_description, "upcoming", utc_now()),
        )
        event_id = cur.lastrowid
    return f"Added event #{event_id}: {description} on {date}" + (f" (goal: {goal_description})" if goal_description else "")


@mcp.tool()
def list_training_events(status: Optional[str] = None) -> list:
    """List training events. status: 'upcoming' | 'completed' | 'cancelled' | None for all."""
    with db() as conn:
        if status:
            rows = conn.execute(
                "SELECT id, date, type, description, goal_description, status, result "
                "FROM training_events WHERE status=? ORDER BY date",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, date, type, description, goal_description, status, result "
                "FROM training_events ORDER BY date"
            ).fetchall()
    return [
        {"id": r[0], "date": r[1], "type": r[2], "description": r[3],
         "goal": r[4], "status": r[5], "result": r[6]}
        for r in rows
    ]


@mcp.tool()
def complete_training_event(event_id: int, result: Optional[str] = None) -> str:
    """Mark a training event as completed.
    result: actual outcome, e.g. '3:52:14', 'DNS', 'finished top-10'."""
    with db() as conn:
        existing = conn.execute(
            "SELECT description FROM training_events WHERE id=?", (event_id,)
        ).fetchone()
        if not existing:
            return f"Error: no event with id {event_id}"
        conn.execute(
            "UPDATE training_events SET status='completed', result=? WHERE id=?",
            (result, event_id),
        )
    return f"Marked event #{event_id} ({existing[0]}) as completed" + (f": {result}" if result else "")


@mcp.tool()
def delete_training_event(event_id: int) -> str:
    """Delete a training event by id."""
    with db() as conn:
        existing = conn.execute(
            "SELECT description FROM training_events WHERE id=?", (event_id,)
        ).fetchone()
        if not existing:
            return f"No training event found with id {event_id}"
        conn.execute("DELETE FROM training_events WHERE id=?", (event_id,))
    return f"Deleted training event #{event_id} ({existing[0]})"


@mcp.tool()
def get_workout_context(date: Optional[str] = None, hrv_window: int = 7) -> dict:
    """Rich context for workout planning: today's metrics, HRV trend, TSB (form),
    training load status, week progress vs targets, next goal event, yesterday's RPE,
    aerobic efficiency trend, Garmin's suggested workout, recent activities, and
    personalized insights from historical correlations."""
    from app.analysis import compute_correlations
    from datetime import date as date_cls, timedelta

    d = _date_or_today(date)
    today_obj = date_cls.fromisoformat(d)
    yesterday = (today_obj - timedelta(days=1)).isoformat()
    week_start = (today_obj - timedelta(days=today_obj.weekday())).isoformat()  # Monday

    with db() as conn:
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

        hrv_rows = conn.execute(
            "SELECT date, hrv FROM daily_metrics WHERE date <= ? AND hrv IS NOT NULL "
            "ORDER BY date DESC LIMIT ?",
            (d, hrv_window),
        ).fetchall()

        yest_row = conn.execute(
            "SELECT alcohol_units, caffeine_mg, calories_estimated FROM daily_metrics WHERE date=?",
            (yesterday,),
        ).fetchone()

        yest_rpe_row = conn.execute(
            "SELECT quantity, description FROM manual_logs "
            "WHERE type='rpe' AND DATE(ts)=? ORDER BY ts DESC LIMIT 1",
            (yesterday,),
        ).fetchone()

        sw_row = conn.execute(
            "SELECT workout_type, description, target_duration_min, target_intensity "
            "FROM suggested_workouts WHERE date=? ORDER BY source LIMIT 1",
            (d,),
        ).fetchone()

        act_rows = conn.execute(
            "SELECT date, type, duration_s, avg_hr, distance_m FROM activities "
            "WHERE date <= ? ORDER BY date DESC, start_time DESC LIMIT 7",
            (d,),
        ).fetchall()

        # Week progress
        week_acts = conn.execute(
            "SELECT type, duration_s, distance_m FROM activities WHERE date BETWEEN ? AND ?",
            (week_start, d),
        ).fetchall()

        # Training goals
        goal_rows = conn.execute(
            "SELECT metric, value FROM training_goals"
        ).fetchall()
        goals = {r[0]: r[1] for r in goal_rows}

        # Next upcoming event
        next_event_row = conn.execute(
            "SELECT id, date, type, description, goal_description FROM training_events "
            "WHERE status='upcoming' AND date >= ? ORDER BY date LIMIT 1",
            (d,),
        ).fetchone()

        # Aerobic efficiency: running acts last 56 days (8wk) with HR data
        eff_rows = conn.execute(
            "SELECT date, duration_s, distance_m, avg_hr FROM activities "
            "WHERE type LIKE '%run%' AND distance_m > 1000 AND duration_s > 0 AND avg_hr > 0 "
            "AND date > ? AND date <= ? ORDER BY date DESC",
            ((today_obj - timedelta(days=56)).isoformat(), d),
        ).fetchall()

        try:
            corr_result = compute_correlations(
                conn,
                inputs=["alcohol_units", "caffeine_mg", "rpe"],
                outputs=["hrv", "sleep_score", "stress_avg"],
                lags=[1],
                days=90,
                min_pairs=10,
            )
            correlations = corr_result.get("correlations", [])
        except Exception:
            correlations = []

    # ── today ────────────────────────────────────────────────────────────────
    today: dict = {}
    if today_row:
        atl = today_row[6]
        ctl = today_row[7]
        ratio = today_row[8]
        today = {
            "hrv": today_row[0],
            "sleep_score": today_row[1],
            "sleep_duration_min": today_row[2],
            "body_battery_high": today_row[3],
            "stress_avg": today_row[4],
            "training_readiness": today_row[5],
            "acute_training_load": atl,
            "chronic_training_load": ctl,
            "training_load_ratio": ratio,
            "weight_kg": today_row[9],
            "resting_hr": today_row[10],
        }

        if ctl is not None and atl is not None:
            today["tsb"] = round(ctl - atl, 1)

        hrv_vals = [r[1] for r in hrv_rows if r[1] is not None]
        if len(hrv_vals) >= 4:
            recent_avg = sum(hrv_vals[:3]) / 3
            older_avg = sum(hrv_vals[3:]) / len(hrv_vals[3:])
            pct = (recent_avg - older_avg) / older_avg if older_avg else 0
            today["hrv_trend"] = "improving" if pct > 0.03 else "declining" if pct < -0.03 else "stable"
            today["hrv_recent_avg"] = round(recent_avg, 1)
        elif hrv_vals:
            today["hrv_trend"] = "insufficient_data"
            today["hrv_recent_avg"] = round(sum(hrv_vals) / len(hrv_vals), 1)

        if ratio is not None:
            today["load_status"] = (
                "detraining" if ratio < 0.8 else
                "maintenance" if ratio < 1.0 else
                "productive" if ratio < 1.3 else
                "slight_overreaching" if ratio < 1.5 else
                "overreaching"
            )

    # ── yesterday ────────────────────────────────────────────────────────────
    yesterday_behavior: dict = {}
    if yest_row:
        yesterday_behavior = {
            "alcohol_units": yest_row[0],
            "caffeine_mg": yest_row[1],
            "calories_estimated": yest_row[2],
        }

    yesterday_rpe = None
    if yest_rpe_row:
        yesterday_rpe = {"rpe": yest_rpe_row[0], "notes": yest_rpe_row[1]}
        yesterday_behavior["rpe"] = yest_rpe_row[0]

    # ── week progress ─────────────────────────────────────────────────────────
    run_types = {"running", "run", "trail_running", "treadmill_running"}
    strength_types = {"strength_training", "strength", "weight_training", "gym"}
    runs = sum(1 for a in week_acts if (a[0] or "").lower().replace(" ", "_") in run_types
               or "run" in (a[0] or "").lower())
    volume_km = sum((a[2] or 0) / 1000 for a in week_acts
                    if "run" in (a[0] or "").lower())
    strength = sum(1 for a in week_acts if any(s in (a[0] or "").lower()
                   for s in ("strength", "weight", "gym")))

    week_progress: dict = {"runs": runs, "volume_km": round(volume_km, 1), "strength_sessions": strength}
    if "weekly_runs" in goals:
        week_progress["target_runs"] = goals["weekly_runs"]
    if "weekly_volume_km" in goals:
        week_progress["target_km"] = goals["weekly_volume_km"]
    if "weekly_strength_sessions" in goals:
        week_progress["target_strength"] = goals["weekly_strength_sessions"]

    # ── next event ───────────────────────────────────────────────────────────
    next_event = None
    if next_event_row:
        event_date = date_cls.fromisoformat(next_event_row[1])
        days_away = (event_date - today_obj).days
        next_event = {
            "id": next_event_row[0],
            "date": next_event_row[1],
            "type": next_event_row[2],
            "description": next_event_row[3],
            "goal": next_event_row[4],
            "weeks_away": days_away // 7,
            "days_away": days_away,
        }

    # ── aerobic efficiency trend ──────────────────────────────────────────────
    aerobic_efficiency = None
    if eff_rows:
        cutoff = (today_obj - timedelta(days=28)).isoformat()
        def _eff(r):
            speed_kmh = (r[2] / 1000) / (r[1] / 3600)
            return speed_kmh / r[3] * 100  # km/h per bpm × 100; higher = more efficient

        recent_effs = [_eff(r) for r in eff_rows if r[0] >= cutoff]
        older_effs = [_eff(r) for r in eff_rows if r[0] < cutoff]
        if recent_effs:
            aerobic_efficiency = {
                "recent_4wk_avg": round(sum(recent_effs) / len(recent_effs), 3),
                "n_activities": len(eff_rows),
            }
            if older_effs:
                prior = sum(older_effs) / len(older_effs)
                aerobic_efficiency["prior_4wk_avg"] = round(prior, 3)
                change = (aerobic_efficiency["recent_4wk_avg"] - prior) / prior if prior else 0
                aerobic_efficiency["trend"] = (
                    "improving" if change > 0.02 else
                    "declining" if change < -0.02 else
                    "stable"
                )
                aerobic_efficiency["change_pct"] = round(change * 100, 1)

    # ── personalized insights ─────────────────────────────────────────────────
    insights = []
    for c in correlations:
        if abs(c["r"]) < 0.15:
            continue
        inp, out = c["input"], c["output"]
        yval = yesterday_behavior.get(inp)
        if not yval:
            continue
        direction = "lower" if c["r"] < 0 else "higher"
        insights.append(
            f"You had {yval} {_unit_for(inp)} yesterday. "
            f"Based on {c['n_pairs']} days of history, this is associated with "
            f"{direction} {out} today (r={c['r']})."
        )

    garmin_suggestion = None
    if sw_row:
        garmin_suggestion = {
            "workout_type": sw_row[0],
            "description": sw_row[1],
            "target_duration_min": sw_row[2],
            "target_intensity": sw_row[3],
        }

    recent_activities = [
        {
            "date": r[0],
            "type": r[1],
            "duration_min": round(r[2] / 60) if r[2] else None,
            "avg_hr": r[3],
            "distance_km": round(r[4] / 1000, 2) if r[4] else None,
        }
        for r in act_rows
    ]

    return {
        "date": d,
        "today": today,
        "yesterday_behavior": yesterday_behavior,
        "yesterday_rpe": yesterday_rpe,
        "week_progress": week_progress,
        "next_event": next_event,
        "aerobic_efficiency": aerobic_efficiency,
        "garmin_suggestion": garmin_suggestion,
        "personalized_insights": insights,
        "recent_activities": recent_activities,
    }


def _unit_for(metric: str) -> str:
    units = {
        "alcohol_units": "units of alcohol",
        "caffeine_mg": "mg of caffeine",
        "rpe": "RPE",
    }
    return units.get(metric, metric)


# ── guide resource ────────────────────────────────────────────────────────────

@mcp.resource("mainspring://guide")
def guide() -> str:
    """Mainspring usage guide — call this at the start of any health or workout conversation."""
    return """# Mainspring — personal health data server

Mainspring stores your Garmin and Google Health data locally, plus manual logs you
add via these tools. It is a **data layer only** — you (Claude) do the reasoning,
planning, and recommendations. The server never fetches weather or sends messages.

---

## Morning planning workflow

1. Call `get_workout_context()` — one call returns everything needed:
   - today's HRV, sleep, stress, body battery, training readiness
   - HRV trend (improving/stable/declining over 7 days)
   - TSB (Training Stress Balance = CTL − ATL; positive = fresh, negative = fatigued/building)
   - load_status derived from ATL/CTL ratio
   - yesterday's alcohol, caffeine, calories, and RPE
   - week progress vs weekly targets (runs, km, strength sessions)
   - next goal event with weeks_away
   - aerobic efficiency trend (pace/HR ratio for running, 4-week vs prior 4-week)
   - Garmin's suggested workout for today
   - personalized insights from historical correlations
   - last 7 activities

2. Use `get_correlations()` periodically (weekly / when patterns are unclear) to surface
   which behaviours most affect recovery. Results feed the insights in `get_workout_context`.

3. Recommend a workout. Consider in order:
   a. TSB: positive → fine to go hard; deeply negative → favour recovery
   b. HRV trend: declining → back off intensity; improving → good to build
   c. load_status: overreaching → rest or easy; detraining → safe to add load
   d. next_event weeks_away: taper starts ~2–3 weeks out; peak week ~4–6 weeks out
   e. aerobic_efficiency trend: improving = adaptation working; declining = too much stress
   f. yesterday_rpe: 8–10 → likely need easy day regardless of other signals
   g. Garmin suggestion as a secondary input, not the primary driver

---

## Key metrics — how to interpret them

| Metric | Good | Caution | Act |
|---|---|---|---|
| hrv | above personal avg | 5–10% below avg | >10% below → easy/rest |
| tsb | 0 to +20 | -10 to 0 | < -20 → high fatigue, rest |
| training_load_ratio | 0.8–1.3 | 1.3–1.5 | >1.5 overreaching |
| sleep_score | >75 | 60–75 | <60 compounds other signals |
| training_readiness | >70 | 50–70 | <50 favour recovery |
| body_battery_high | >70 | 40–70 | <40 suggests poor recovery |

TSB interpretation: think of it as "form". Positive = rested/sharp (good for racing or
hard efforts). Negative = accumulated fatigue (fine during a build block, bad near a race).
A TSB of -30 with a marathon in 2 weeks = needs to taper urgently.

---

## Logging tools

| Tool | When to use |
|---|---|
| `log_meal` | any food; include photo estimates with confidence='photo_estimate' |
| `log_caffeine` | coffee, tea, pre-workout; amount_mg optional |
| `log_alcohol` | UK units (1 unit = 8g ethanol; pint ~2.3 units, glass wine ~2 units) |
| `log_weight` | kg; normalizer surfaces into daily_metrics and correlations |
| `log_blood_pressure` | systolic/diastolic/pulse in mmHg |
| `log_rpe` | 1–10 after a workout; feeds next-day correlation insights |
| `log_note` | anything else — sleep quality, illness, stress events |
| `amend_log` / `delete_log` | corrections; use get_logs to find the id |

Log things at the time they happen. Past entries can use the `ts` parameter (UTC ISO-8601)
or `date` parameter (for log_rpe). RPE should be logged same day as the workout.

**Timezone note**: logs are stored in UTC. Always pass `tz` (e.g. `'America/New_York'`)
to `get_logs` so date boundaries are computed in local time, not UTC. Without it, meals
after ~8pm local time appear on the wrong day.

---

## Training goals and events

Set once, update when training focus changes:
```
set_training_goal('weekly_runs', 3)
set_training_goal('weekly_volume_km', 50)
set_training_goal('weekly_strength_sessions', 1)
add_training_event('2026-11-15', 'marathon', 'Berlin Marathon', 'sub-4h')
```

`get_workout_context` automatically picks up the next upcoming event and shows
weeks_away so you can reason about periodization without hard-coded phase rules.

When an event is done: `complete_training_event(id, result='3:52:14')`.

---

## Correlation analysis

`get_correlations()` computes Pearson/Spearman correlations between behaviour inputs
(alcohol, caffeine, calories, weight, RPE) and recovery outputs (HRV, sleep score,
resting HR, stress, body battery) at lag 0, 1, and 2 days.

- **lag=1** is the most clinically relevant: last night's behaviour → this morning's recovery
- Requires `min_pairs=14` (default) — results with fewer data points are suppressed
- `r` ranges −1 to +1; |r| > 0.3 is worth acting on, |r| > 0.5 is strong
- Use `method='spearman'` if you suspect non-linear relationships

Run this when: onboarding to a new conversation, weekly reviews, or when the user asks
"why is my HRV low?" and you want data rather than guesses.

---

## Data sources

- **Garmin** (primary): HRV, sleep, stress, body battery, training readiness, training load,
  VO2max, SpO2, breathing rate, activities, suggested workouts — imported hourly
- **Google Health** (secondary): activities, step counts — imported hourly
- **Manual logs**: meals, caffeine, alcohol, weight, BP, RPE, notes — via these tools
- Normalization runs after each import; `daily_metrics` is rebuilt from raw sources

If a metric shows None, the device hasn't synced yet for that day or the API returned
no data. Garmin data often arrives 1–2 hours after waking/syncing.

---

## Source preferences

`get_source_config()` shows which source wins per metric. Override with
`set_source_preference('hrv', 'garmin')`. Default priority: garmin → google_health.
"""


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
