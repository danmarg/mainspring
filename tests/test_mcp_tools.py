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
    assert "65mg" in result

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


# ── amend_log / delete_log ──────────────────────────────────────────────────

def test_amend_log_updates_fields():
    from app.mcp_server import log_caffeine, amend_log, get_logs
    log_caffeine(description="espresso", amount_mg=65.0, ts="2025-01-01T07:30:00+00:00")
    log_id = get_logs(start_date="2025-01-01", end_date="2025-01-01")[0]["id"]

    result = amend_log(log_id=log_id, description="double espresso", quantity=130.0)
    assert f"Amended log {log_id}" in result

    logs = get_logs(start_date="2025-01-01", end_date="2025-01-01")
    assert logs[0]["description"] == "double espresso"
    assert logs[0]["quantity"] == 130.0
    assert logs[0]["unit"] == "mg"  # untouched field preserved


def test_amend_log_estimated_macros():
    from app.mcp_server import log_meal, amend_log, get_logs
    log_meal(description="steak")
    log_id = get_logs(start_date=str(date.today()), end_date=str(date.today()))[0]["id"]

    macros = {"protein_g": 40, "carbs_g": 0, "fat_g": 20}
    amend_log(log_id=log_id, estimated_macros=macros)

    logs = get_logs(start_date=str(date.today()), end_date=str(date.today()))
    assert logs[0]["estimated_macros"] == macros


def test_amend_log_no_fields():
    from app.mcp_server import log_meal, amend_log, get_logs
    log_meal(description="oats")
    log_id = get_logs(start_date=str(date.today()), end_date=str(date.today()))[0]["id"]

    result = amend_log(log_id=log_id)
    assert "No fields" in result


def test_amend_log_missing_id():
    from app.mcp_server import amend_log
    result = amend_log(log_id=9999, description="x")
    assert "Error" in result


def test_delete_log_removes_entry():
    from app.mcp_server import log_alcohol, delete_log, get_logs
    log_alcohol(description="glass of wine", units=1.5, ts="2025-01-01T20:00:00+00:00")
    log_id = get_logs(start_date="2025-01-01", end_date="2025-01-01")[0]["id"]

    result = delete_log(log_id=log_id)
    assert f"Deleted log {log_id}" in result
    assert get_logs(start_date="2025-01-01", end_date="2025-01-01") == []


def test_delete_log_missing_id():
    from app.mcp_server import delete_log
    result = delete_log(log_id=9999)
    assert "Error" in result


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
        ("2025-01-01", 55.0, 52.0, 78.0, 200.0, 1.5,
         json.dumps({"hrv": "garmin", "resting_hr": "google_health"})),
    )
    conn.commit()
    conn.close()

    from app.mcp_server import get_daily_metrics
    rows = get_daily_metrics("2025-01-01", "2025-01-01")
    assert len(rows) == 1
    assert rows[0]["hrv"] == 55.0
    assert rows[0]["resting_hr"] == 52.0
    assert rows[0]["caffeine_mg"] == 200.0
    # 'garmin' is the default priority and is suppressed; only the deviation is surfaced
    assert rows[0]["sources"] == {"resting_hr": "google_health"}


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
        "INSERT INTO daily_metrics(date, training_readiness, hrv, stress_avg, acute_training_load, source_flags_json) VALUES (?,?,?,?,?,?)",
        ("2025-01-01", 68.0, 54.0, 32.0, 280.0, "{}"),
    )
    conn.commit()
    conn.close()

    from app.mcp_server import get_suggested_workout
    result = get_suggested_workout("2025-01-01")
    assert result is not None
    assert result["workout_type"] == "recovery"
    assert result["training_context"]["training_readiness"] == 68.0
    assert result["training_context"]["acute_training_load"] == 280.0
    assert "readiness" in result["training_context"]
    assert "label" in result["training_context"]["readiness"]


# ── source config ─────────────────────────────────────────────────────────────

def test_get_source_config_empty():
    from app.mcp_server import get_source_config
    result = get_source_config()
    assert "default_priority" in result
    assert result["overrides"] == {}


def test_set_source_preference():
    from app.mcp_server import set_source_preference, get_source_config
    result = set_source_preference("hrv", "google_health")
    assert "hrv" in result and "google_health" in result

    config = get_source_config()
    assert config["overrides"]["hrv"] == "google_health"


def test_set_source_preference_idempotent():
    from app.mcp_server import set_source_preference, get_source_config
    set_source_preference("hrv", "garmin")
    set_source_preference("hrv", "google_health")
    config = get_source_config()
    assert config["overrides"]["hrv"] == "google_health"


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
    from app.datasette_mount import _TokenMiddleware

    allowed = []
    async def inner(scope, receive, send):
        allowed.append(True)

    mw = _TokenMiddleware(inner, token="secret123")
    responses = asyncio.run(_call_middleware(mw, token="secret123"))
    assert allowed == [True]
    assert responses == []


def test_bearer_middleware_rejects_wrong_token():
    from app.datasette_mount import _TokenMiddleware

    async def inner(scope, receive, send): pass

    mw = _TokenMiddleware(inner, token="secret123")
    responses = asyncio.run(_call_middleware(mw, token="wrongtoken"))
    statuses = [r["status"] for r in responses if "status" in r]
    assert 401 in statuses


def test_bearer_middleware_rejects_missing_token():
    from app.datasette_mount import _TokenMiddleware

    async def inner(scope, receive, send): pass

    mw = _TokenMiddleware(inner, token="secret123")
    responses = asyncio.run(_call_middleware(mw, token=None))
    statuses = [r["status"] for r in responses if "status" in r]
    assert 401 in statuses


# ── T3: log_weight and log_blood_pressure ────────────────────────────────────

def test_log_weight():
    from app.mcp_server import log_weight
    result = log_weight(kg=82.5, ts="2025-06-01T09:00:00+00:00")
    assert "82.5" in result

    # _renormalize_date runs synchronously, so daily_metrics should be updated
    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute("SELECT weight_kg FROM daily_metrics WHERE date='2025-06-01'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 82.5


def test_log_blood_pressure():
    from app.mcp_server import log_blood_pressure
    result = log_blood_pressure(systolic=125, diastolic=82, pulse=68, ts="2025-06-01T08:00:00+00:00")
    assert "125" in result
    assert "82" in result
    assert "68" in result

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute(
        "SELECT bp_systolic, bp_diastolic FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 125
    assert row[1] == 82


def test_log_rpe_validates_range():
    from app.mcp_server import log_rpe
    assert "Error" in log_rpe(rpe=11)
    assert "Error" in log_rpe(rpe=0)
    result = log_rpe(rpe=7, activity_type="running")
    assert "Error" not in result
    assert "7" in result


def test_log_hydration():
    from app.mcp_server import log_hydration
    result = log_hydration(ml=500.0, ts="2025-06-01T09:00:00+00:00")
    assert "500" in result

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute("SELECT hydration_ml FROM daily_metrics WHERE date='2025-06-01'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 500.0


def test_log_soreness_validates_range():
    from app.mcp_server import log_soreness
    assert "Error" in log_soreness(body_part="left calf", severity=11)
    assert "Error" in log_soreness(body_part="left calf", severity=0)
    result = log_soreness(body_part="left calf", severity=6, notes="tight", date="2025-06-01")
    assert "Error" not in result
    assert "left calf" in result
    assert "6" in result

    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute(
        "SELECT type, quantity, unit, description FROM manual_logs WHERE type='soreness'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == 6.0
    assert row[2] == "left calf"
    assert "tight" in row[3]


# ── T9: TZ boundary test for get_logs ────────────────────────────────────────

def test_get_logs_tz_boundary():
    """A log at 23:30 UTC on Jan 1 is on Jan 2 in Europe/Berlin (+1h in winter)."""
    from app.mcp_server import log_caffeine, get_logs
    # 2025-01-01T23:30 UTC = 2025-01-02T00:30 Europe/Berlin (UTC+1 in January)
    log_caffeine(description="late coffee", amount_mg=80, ts="2025-01-01T23:30:00+00:00")

    # In Europe/Berlin it is Jan 2, so Jan 1 query should be empty
    results_berlin_jan1 = get_logs("2025-01-01", "2025-01-01", tz="Europe/Berlin")
    assert len(results_berlin_jan1) == 0

    # In Europe/Berlin it is Jan 2
    results_berlin_jan2 = get_logs("2025-01-02", "2025-01-02", tz="Europe/Berlin")
    assert len(results_berlin_jan2) == 1

    # In UTC it is still Jan 1
    results_utc_jan1 = get_logs("2025-01-01", "2025-01-01", tz="UTC")
    assert len(results_utc_jan1) == 1


# ── T1: get_workout_context smoke test ───────────────────────────────────────

def test_get_workout_context():
    from app.mcp_server import get_workout_context
    from app.db import utc_now

    target_date = "2025-01-07"
    # Seed one week of daily_metrics
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for i, d in enumerate([
        "2025-01-01", "2025-01-02", "2025-01-03",
        "2025-01-04", "2025-01-05", "2025-01-06", "2025-01-07",
    ]):
        conn.execute(
            """INSERT INTO daily_metrics(
                date, hrv, sleep_score, training_readiness,
                acute_training_load, chronic_training_load, source_flags_json
            ) VALUES (?,?,?,?,?,?,?)""",
            (d, 55.0 + i, 78.0, 68.0, 280.0, 310.0, "{}"),
        )
    # Add a suggested workout for the target date
    conn.execute(
        "INSERT INTO suggested_workouts(date, source, workout_type, description, "
        "target_duration_min, target_intensity, fetched_at) VALUES (?,?,?,?,?,?,?)",
        (target_date, "garmin", "base", "Easy 45 min jog", 45.0, "low", utc_now()),
    )
    conn.execute(
        "INSERT INTO hr_zones(date, source, zone, min_bpm, max_bpm) VALUES (?,?,?,?,?)",
        (target_date, "derived", 3, 126, 144),
    )
    conn.execute(
        "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
        (f"{target_date}T08:00:00+00:00", "soreness", "left calf: 6/10", 6.0, "left calf", utc_now()),
    )
    conn.commit()
    conn.close()

    result = get_workout_context(date=target_date)
    assert result is not None
    assert "today" in result
    assert "week_progress" in result
    # training_readiness lives inside "today"
    assert result["today"]["training_readiness"] is not None
    assert isinstance(result["today"]["hrv"], (int, float))
    assert "readiness" in result["today"]
    assert result["today"]["readiness"]["score"] is not None
    assert result["today"]["hr_zones"] == [{"zone": 3, "min_bpm": 126, "max_bpm": 144}]
    assert len(result["recent_soreness"]) == 1
    assert result["recent_soreness"][0]["body_part"] == "left calf"
    assert result["recent_soreness"][0]["severity"] == 6.0
