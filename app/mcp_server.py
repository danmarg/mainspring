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
    Includes HRV, sleep, stress, body battery, training readiness, caffeine, alcohol."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT date, resting_hr, hrv,
                   sleep_score, sleep_duration_min,
                   body_battery_high, body_battery_low,
                   stress_avg, training_readiness, vo2max, steps,
                   caffeine_mg, alcohol_units, calories_estimated,
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
            "body_battery_high": r[5],
            "body_battery_low": r[6],
            "stress_avg": r[7],
            "training_readiness": r[8],
            "vo2max": r[9],
            "steps": r[10],
            "caffeine_mg": r[11],
            "alcohol_units": r[12],
            "calories_estimated": r[13],
            "sources": json.loads(r[14]) if r[14] else {},
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
    """Set the canonical source for a metric. source must be 'garmin' or 'fitbit'.
    Use 'activities' as the metric to set the preference for activity dedup."""
    valid_sources = ("garmin", "fitbit")
    if source not in valid_sources:
        return f"Error: source must be one of {valid_sources}"
    with db() as conn:
        conn.execute(
            "INSERT INTO source_config(metric, canonical_source) VALUES (?,?) "
            "ON CONFLICT(metric) DO UPDATE SET canonical_source=excluded.canonical_source",
            (metric, source),
        )
    return f"Set {metric} → {source}"


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
