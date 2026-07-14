"""
Tests for MCP tool logic.
Tests call the underlying functions directly (not via HTTP/MCP protocol),
patching app.db.DB_PATH to use a temp database.
"""

import json
import sqlite3
import tempfile
import pathlib
from datetime import date

import pytest

import app.db as db_module
from app.db import init_db, upsert_raw_metric, utc_now


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    yield path


# ── log_meal ─────────────────────────────────────────────────────────────────

def test_log_meal_basic():
    from app.mcp_server import log_meal
    result = log_meal(description="chicken salad", estimated_calories=400, confidence="user_confirmed")
    assert "chicken salad" in result

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute("SELECT type, description, estimated_calories, confidence FROM manual_logs").fetchone()
    conn.close()
    assert row[0] == "meal"
    assert row[1] == "chicken salad"
    assert row[2] == 400
    assert row[3] == "user_confirmed"


def test_log_meal_with_macros():
    from app.mcp_server import log_meal
    macros = {"protein_g": 40, "carbs_g": 30, "fat_g": 15}
    log_meal(description="steak", estimated_macros=macros, ts="2025-01-01T12:00:00+00:00")

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute("SELECT estimated_macros_json FROM manual_logs").fetchone()
    conn.close()
    assert json.loads(row[0]) == macros


def test_log_meal_ts_defaults_to_now():
    from app.mcp_server import log_meal
    log_meal(description="oats")

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute("SELECT ts FROM manual_logs").fetchone()
    conn.close()
    assert row[0] is not None and len(row[0]) > 0


# ── log_caffeine ──────────────────────────────────────────────────────────────

def test_log_caffeine():
    from app.mcp_server import log_caffeine
    result = log_caffeine(description="espresso", amount_mg=65.0, ts="2025-01-01T07:30:00+00:00")
    assert "65.0mg" in result

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute("SELECT type, quantity, unit FROM manual_logs").fetchone()
    conn.close()
    assert row[0] == "caffeine"
    assert row[1] == 65.0
    assert row[2] == "mg"


# ── log_alcohol ───────────────────────────────────────────────────────────────

def test_log_alcohol():
    from app.mcp_server import log_alcohol
    result = log_alcohol(description="glass of wine", units=1.5, ts="2025-01-01T20:00:00+00:00")
    assert "1.5 units" in result

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute("SELECT type, quantity, unit FROM manual_logs").fetchone()
    conn.close()
    assert row[0] == "alcohol"
    assert row[1] == 1.5


# ── get_logs ──────────────────────────────────────────────────────────────────

def test_get_logs_returns_entries():
    from app.mcp_server import log_caffeine, log_alcohol, get_logs
    log_caffeine(description="coffee", amount_mg=80, ts="2025-01-01T08:00:00+00:00")
    log_alcohol(description="beer", units=1.0, ts="2025-01-01T20:00:00+00:00")

    logs = get_logs("2025-01-01", "2025-01-01")
    assert len(logs) == 2
    types = {l["type"] for l in logs}
    assert types == {"caffeine", "alcohol"}


def test_get_logs_type_filter():
    from app.mcp_server import log_caffeine, log_meal, get_logs
    log_caffeine(description="tea", amount_mg=30, ts="2025-01-01T09:00:00+00:00")
    log_meal(description="toast", ts="2025-01-01T08:00:00+00:00")

    logs = get_logs("2025-01-01", "2025-01-01", type="caffeine")
    assert len(logs) == 1
    assert logs[0]["type"] == "caffeine"


def test_get_logs_date_range():
    from app.mcp_server import log_caffeine, get_logs
    log_caffeine(description="morning coffee", amount_mg=80, ts="2025-01-01T08:00:00+00:00")
    log_caffeine(description="afternoon coffee", amount_mg=80, ts="2025-01-03T14:00:00+00:00")

    logs = get_logs("2025-01-01", "2025-01-02")
    assert len(logs) == 1


# ── get_daily_metrics ─────────────────────────────────────────────────────────

def test_get_daily_metrics():
    now = utc_now()
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        "INSERT INTO daily_metrics(date, hrv, resting_hr, sleep_score, caffeine_mg, alcohol_units, source_flags_json) "
        "VALUES (?,?,?,?,?,?,?)",
        ("2025-01-01", 55.0, 52.0, 78.0, 200.0, 1.5, json.dumps({"hrv": "garmin"})),
    )
    conn.commit()
    conn.close()

    from app.mcp_server import get_daily_metrics
    rows = get_daily_metrics("2025-01-01", "2025-01-01")
    assert len(rows) == 1
    assert rows[0]["hrv"] == 55.0
    assert rows[0]["resting_hr"] == 52.0
    assert rows[0]["caffeine_mg"] == 200.0
    assert rows[0]["sources"] == {"hrv": "garmin"}


def test_get_daily_metrics_date_range():
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for d, hrv in [("2025-01-01", 52.0), ("2025-01-02", 55.0), ("2025-01-03", 58.0)]:
        conn.execute(
            "INSERT INTO daily_metrics(date, hrv, source_flags_json) VALUES (?,?,?)",
            (d, hrv, "{}"),
        )
    conn.commit()
    conn.close()

    from app.mcp_server import get_daily_metrics
    rows = get_daily_metrics("2025-01-01", "2025-01-02")
    assert len(rows) == 2
    assert rows[0]["date"] == "2025-01-01"
    assert rows[1]["date"] == "2025-01-02"


# ── get_suggested_workout ─────────────────────────────────────────────────────

def test_get_suggested_workout_none_when_absent():
    from app.mcp_server import get_suggested_workout
    result = get_suggested_workout("2025-01-01")
    assert result is None


def test_get_suggested_workout_returns_with_context():
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        "INSERT INTO suggested_workouts(date, source, workout_type, description, target_duration_min, target_intensity, fetched_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("2025-01-01", "garmin", "recovery", "Easy 30 min jog", 30.0, "low", utc_now()),
    )
    conn.execute(
        "INSERT INTO daily_metrics(date, training_readiness, hrv, stress_avg, source_flags_json) VALUES (?,?,?,?,?)",
        ("2025-01-01", 68.0, 54.0, 32.0, "{}"),
    )
    upsert_raw_metric(conn, "2025-01-01", "garmin", "acute_training_load", 280.0, utc_now())
    conn.commit()
    conn.close()

    from app.mcp_server import get_suggested_workout
    result = get_suggested_workout("2025-01-01")
    assert result is not None
    assert result["workout_type"] == "recovery"
    assert result["training_context"]["training_readiness"] == 68.0
    assert result["training_context"]["acute_training_load"] == 280.0


# ── source config ─────────────────────────────────────────────────────────────

def test_get_source_config_empty():
    from app.mcp_server import get_source_config
    result = get_source_config()
    assert "default_priority" in result
    assert result["overrides"] == {}


def test_set_source_preference():
    from app.mcp_server import set_source_preference, get_source_config
    result = set_source_preference("hrv", "fitbit")
    assert "hrv" in result and "fitbit" in result

    config = get_source_config()
    assert config["overrides"]["hrv"] == "fitbit"


def test_set_source_preference_idempotent():
    from app.mcp_server import set_source_preference, get_source_config
    set_source_preference("hrv", "garmin")
    set_source_preference("hrv", "fitbit")
    config = get_source_config()
    assert config["overrides"]["hrv"] == "fitbit"


def test_set_source_preference_invalid_source():
    from app.mcp_server import set_source_preference
    result = set_source_preference("hrv", "oura")
    assert "Error" in result


# ── bearer token middleware ───────────────────────────────────────────────────

import asyncio

async def _call_middleware(middleware, path="/", token=None):
    responses = []
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"authorization", f"Bearer {token}".encode())] if token else [],
    }
    async def receive(): pass
    async def send(msg): responses.append(msg)
    await middleware(scope, receive, send)
    return responses


def test_bearer_middleware_allows_valid_token():
    from app.datasette_mount import _BearerTokenMiddleware

    allowed = []
    async def inner(scope, receive, send):
        allowed.append(True)

    mw = _BearerTokenMiddleware(inner, token="secret123")
    responses = asyncio.run(_call_middleware(mw, token="secret123"))
    assert allowed == [True]
    assert responses == []


def test_bearer_middleware_rejects_wrong_token():
    from app.datasette_mount import _BearerTokenMiddleware

    async def inner(scope, receive, send): pass

    mw = _BearerTokenMiddleware(inner, token="secret123")
    responses = asyncio.run(_call_middleware(mw, token="wrongtoken"))
    statuses = [r["status"] for r in responses if "status" in r]
    assert 401 in statuses


def test_bearer_middleware_rejects_missing_token():
    from app.datasette_mount import _BearerTokenMiddleware

    async def inner(scope, receive, send): pass

    mw = _BearerTokenMiddleware(inner, token="secret123")
    responses = asyncio.run(_call_middleware(mw, token=None))
    statuses = [r["status"] for r in responses if "status" in r]
    assert 401 in statuses
