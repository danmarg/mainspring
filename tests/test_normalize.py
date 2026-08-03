"""
Tests for the normalization job.
Covers: day_timezone derivation, daily_metrics rebuild, activity dedup.
"""

import json
import sqlite3
import tempfile
import pathlib

import pytest

import app.db as db_module
from app.db import init_db, upsert_raw_metric, upsert_raw_payload, utc_now
from app.normalize import (
    rebuild_day_timezone,
    rebuild_daily_metrics,
    rebuild_activities,
    run_normalization,
)


@pytest.fixture
def tmp_db(tmp_path):
    path = tmp_path / "test.db"
    orig = db_module.DB_PATH
    db_module.DB_PATH = path
    init_db(path)
    from app.db import get_connection
    conn = get_connection(path)
    yield conn
    conn.close()
    db_module.DB_PATH = orig


# ── day_timezone ─────────────────────────────────────────────────────────────

def test_day_timezone_home_default(tmp_db):
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "hrv", 50.0, utc_now())
    tmp_db.commit()
    rebuild_day_timezone(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT tz, source FROM day_timezone WHERE date='2025-01-01'"
    ).fetchone()
    assert row is not None
    assert row[1] == "home_default"
    from app.db import HOME_TZ
    assert row[0] == HOME_TZ


def test_day_timezone_from_activity_payload(tmp_db):
    # activity payload has startTimeLocal and startTimeGMT — diff gives offset
    activity = {
        "activityId": "1",
        "startTimeLocal": "2025-06-15T08:00:00",
        "startTimeGMT": "2025-06-15T12:00:00",  # UTC-4 (EDT)
        "activityType": {"typeKey": "running"},
    }
    upsert_raw_payload(tmp_db, "garmin", "activity", json.dumps(activity), date="2025-06-15")
    tmp_db.commit()
    rebuild_day_timezone(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT tz, source FROM day_timezone WHERE date='2025-06-15'"
    ).fetchone()
    assert row is not None
    assert "garmin_activity" in row[1]


# ── daily_metrics ─────────────────────────────────────────────────────────────

def test_daily_metrics_single_source(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "hrv", 55.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "resting_hr", 52.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "sleep_score", 78.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "steps", 9000.0, now)
    tmp_db.commit()

    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT hrv, resting_hr, sleep_score, steps FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    assert row[0] == 55.0
    assert row[1] == 52.0
    assert row[2] == 78.0
    assert row[3] == 9000.0


def test_daily_metrics_source_flags(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "hrv", 55.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT source_flags_json FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    flags = json.loads(row[0])
    assert flags.get("hrv") == "garmin"


def test_daily_metrics_source_config_override(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "hrv", 55.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "hrv", 62.0, now)
    # override: prefer google_health for hrv
    tmp_db.execute(
        "INSERT INTO source_config(metric, canonical_source) VALUES ('hrv', 'google_health')"
    )
    tmp_db.commit()

    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT hrv, source_flags_json FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    assert row[0] == 62.0
    flags = json.loads(row[1])
    assert flags["hrv"] == "google_health"


def test_daily_metrics_fallback_when_preferred_absent(tmp_db):
    now = utc_now()
    # only google_health has hrv, but no source_config (garmin is default priority)
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "hrv", 60.0, now)
    tmp_db.commit()

    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT hrv FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    assert row[0] == 60.0


def test_daily_metrics_synthesizes_sleep_score_when_no_vendor_score(tmp_db):
    now = utc_now()
    # google_health-only day: stage breakdown but no vendor sleep_score
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "sleep_duration_min", 420.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "sleep_deep_min", 90.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "sleep_rem_min", 80.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "sleep_awake_min", 10.0, now)
    tmp_db.commit()

    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT sleep_score, source_flags_json FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    assert row[0] is not None
    assert 0 <= row[0] <= 100
    assert json.loads(row[1])["sleep_score"] == "synthetic"


def test_daily_metrics_prefers_real_sleep_score_over_synthetic(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "sleep_score", 78.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "sleep_duration_min", 200.0, now)
    upsert_raw_metric(tmp_db, "2025-01-01", "google_health", "sleep_deep_min", 5.0, now)
    tmp_db.commit()

    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT sleep_score, source_flags_json FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    assert row[0] == 78.0
    assert json.loads(row[1])["sleep_score"] == "garmin"


def test_daily_metrics_no_sleep_score_when_no_sleep_data_at_all(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "hrv", 55.0, now)
    tmp_db.commit()

    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT sleep_score FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    assert row[0] is None


def test_daily_metrics_aggregates_manual_logs(tmp_db):
    now = utc_now()
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, quantity, unit, created_at) VALUES (?,?,?,?,?)",
        ("2025-01-01T08:00:00", "caffeine", 200.0, "mg", now),
    )
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, quantity, unit, created_at) VALUES (?,?,?,?,?)",
        ("2025-01-01T14:00:00", "caffeine", 100.0, "mg", now),
    )
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, quantity, unit, created_at) VALUES (?,?,?,?,?)",
        ("2025-01-01T20:00:00", "alcohol", 2.0, "units", now),
    )
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, estimated_calories, created_at) VALUES (?,?,?,?)",
        ("2025-01-01T12:00:00", "meal", 650, now),
    )
    tmp_db.commit()

    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT caffeine_mg, alcohol_units, calories_estimated FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()
    assert row[0] == 300.0
    assert row[1] == 2.0
    assert row[2] == 650.0


def test_daily_metrics_idempotent(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "hrv", 55.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    count = tmp_db.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE date='2025-01-01'"
    ).fetchone()[0]
    assert count == 1


# ── activity dedup ────────────────────────────────────────────────────────────

def _insert_garmin_activity(conn, activity_id, date, start_time, act_type, avg_hr=140):
    conn.execute(
        "INSERT INTO garmin_activities(activity_id, date, start_time, type, duration_s, avg_hr, fetched_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (activity_id, date, start_time, act_type, 3600, avg_hr, utc_now()),
    )


def _insert_gh_activity(conn, activity_id, date, start_time, act_type, avg_hr=138):
    conn.execute(
        "INSERT INTO google_health_activities(activity_id, date, start_time, type, duration_s, avg_hr, fetched_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (activity_id, date, start_time, act_type, 3600, avg_hr, utc_now()),
    )


def test_activities_garmin_only(tmp_db):
    _insert_garmin_activity(tmp_db, "g1", "2025-01-01", "2025-01-01T07:00:00", "running")
    tmp_db.commit()
    rebuild_activities(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT canonical_source, garmin_activity_id, google_health_activity_id FROM activities"
    ).fetchone()
    assert row[0] == "garmin"
    assert row[1] == "g1"
    assert row[2] is None


def test_activities_dedup_match(tmp_db):
    # Garmin at 07:00, Google Health at 07:08 — within ±15 min window, same date+type
    _insert_garmin_activity(tmp_db, "g1", "2025-01-01", "2025-01-01T07:00:00", "running", avg_hr=145)
    _insert_gh_activity(tmp_db, "gh1", "2025-01-01", "2025-01-01T07:08:00", "running", avg_hr=143)
    tmp_db.commit()

    rebuild_activities(tmp_db)
    tmp_db.commit()

    rows = tmp_db.execute("SELECT canonical_source, garmin_activity_id, google_health_activity_id FROM activities").fetchall()
    assert len(rows) == 1  # deduped to one
    assert rows[0][1] == "g1"
    assert rows[0][2] == "gh1"


def test_activities_no_dedup_outside_window(tmp_db):
    # 20 min apart — should be two separate rows
    _insert_garmin_activity(tmp_db, "g1", "2025-01-01", "2025-01-01T07:00:00", "running")
    _insert_gh_activity(tmp_db, "gh1", "2025-01-01", "2025-01-01T07:20:00", "running")
    tmp_db.commit()

    rebuild_activities(tmp_db)
    tmp_db.commit()

    count = tmp_db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    assert count == 2


def test_activities_no_dedup_different_type(tmp_db):
    _insert_garmin_activity(tmp_db, "g1", "2025-01-01", "2025-01-01T07:00:00", "running")
    _insert_gh_activity(tmp_db, "gh1", "2025-01-01", "2025-01-01T07:05:00", "cycling")
    tmp_db.commit()

    rebuild_activities(tmp_db)
    tmp_db.commit()

    count = tmp_db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    assert count == 2


def test_activities_canonical_source_google_health(tmp_db):
    _insert_garmin_activity(tmp_db, "g1", "2025-01-01", "2025-01-01T07:00:00", "running", avg_hr=145)
    _insert_gh_activity(tmp_db, "gh1", "2025-01-01", "2025-01-01T07:05:00", "running", avg_hr=143)
    tmp_db.execute(
        "INSERT INTO source_config(metric, canonical_source) VALUES ('activities', 'google_health')"
    )
    tmp_db.commit()

    rebuild_activities(tmp_db)
    tmp_db.commit()

    row = tmp_db.execute("SELECT canonical_source, avg_hr FROM activities").fetchone()
    assert row[0] == "google_health"
    assert row[1] == 143


def test_activities_google_health_only(tmp_db):
    _insert_gh_activity(tmp_db, "gh1", "2025-01-01", "2025-01-01T07:00:00", "yoga")
    tmp_db.commit()
    rebuild_activities(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT canonical_source, garmin_activity_id FROM activities"
    ).fetchone()
    assert row[0] == "google_health"
    assert row[1] is None


# ── full run_normalization ────────────────────────────────────────────────────

def test_run_normalization_returns_counts(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-01", "garmin", "hrv", 55.0, now)
    upsert_raw_metric(tmp_db, "2025-01-02", "garmin", "hrv", 58.0, now)
    _insert_garmin_activity(tmp_db, "g1", "2025-01-01", "2025-01-01T07:00:00", "running")
    tmp_db.commit()

    result = run_normalization(tmp_db)
    assert result["daily_metric_dates"] == 2
    assert result["activity_rows"] == 1
    assert result["day_timezone_rows"] >= 2


# ── weight and BP in daily_metrics ──────────────────────────────────────────

def test_weight_in_daily_metrics(tmp_db):
    now = utc_now()
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("2025-06-01T09:00:00", "weight", "78.5kg", 78.5, "kg", now),
    )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT weight_kg FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    assert row is not None
    assert row[0] == 78.5


def test_training_load_in_daily_metrics(tmp_db):
    """Garmin writes acute/chronic load as raw metrics 'atl'/'ctl' — normalize.py
    must map those into the daily_metrics columns and derive the ratio (ACWR)."""
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "atl", 320.0, now)
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "ctl", 290.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT acute_training_load, chronic_training_load, training_load_ratio "
        "FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    assert row is not None
    assert row[0] == 320.0
    assert row[1] == 290.0
    assert abs(row[2] - (320.0 / 290.0)) < 0.001


def test_training_load_ratio_none_without_both_atl_and_ctl(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-06-02", "garmin", "atl", 320.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT acute_training_load, chronic_training_load, training_load_ratio "
        "FROM daily_metrics WHERE date='2025-06-02'"
    ).fetchone()
    assert row is not None
    assert row[0] == 320.0
    assert row[1] is None
    assert row[2] is None


def test_blood_pressure_in_daily_metrics(tmp_db):
    import json as _json
    now = utc_now()
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, description, quantity, unit, estimated_macros_json, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "2025-06-01T08:00:00", "blood_pressure", "120/80 pulse 65",
            120, "mmHg",
            _json.dumps({"systolic": 120, "diastolic": 80, "pulse": 65}),
            now,
        ),
    )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT bp_systolic, bp_diastolic, bp_pulse FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    assert row is not None
    assert row[0] == 120
    assert row[1] == 80
    assert row[2] == 65


def test_weight_falls_back_to_garmin_when_no_manual_log(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "weight_kg", 74.2, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT weight_kg, source_flags_json FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    assert row is not None
    assert row[0] == 74.2
    assert json.loads(row[1])["weight_kg"] == "garmin"


def test_manual_weight_overrides_garmin(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "weight_kg", 74.2, now)
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("2025-06-01T09:00:00", "weight", "78.5kg", 78.5, "kg", now),
    )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT weight_kg, source_flags_json FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    assert row[0] == 78.5
    assert json.loads(row[1])["weight_kg"] == "manual"


def test_bp_falls_back_to_garmin_when_no_manual_log(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "bp_systolic", 118.0, now)
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "bp_diastolic", 76.0, now)
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "bp_pulse", 62.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT bp_systolic, bp_diastolic, bp_pulse, source_flags_json "
        "FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    assert row[0] == 118.0
    assert row[1] == 76.0
    assert row[2] == 62.0
    assert json.loads(row[3])["bp"] == "garmin"


def test_bp_partial_garmin_data_is_not_used(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "bp_systolic", 118.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT bp_systolic, bp_diastolic, bp_pulse FROM daily_metrics WHERE date='2025-06-01'"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
