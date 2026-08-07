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
    _resting_hr_from_intraday,
    compute_hr_zones,
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


def test_hrv_recomputed_from_intraday_when_google_daily_rollup_used(tmp_db):
    """Google's daily-heart-rate-variability rollup is all-day, not overnight —
    when it's the only source, recompute from the overnight intraday_hrv samples.
    No wake-time data seeded, so this falls back to the 01:00-06:00 local
    (Europe/Berlin, UTC+1 in January) core-night window."""
    upsert_raw_metric(tmp_db, "2025-01-15", "google_health", "hrv", 60.0, utc_now())
    from datetime import datetime, timedelta
    start = datetime.fromisoformat("2025-01-15T00:30:00+00:00")
    for i in range(30):
        ts = (start + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp_db.execute(
            "INSERT INTO intraday_hrv(ts, source, rmssd) VALUES (?,?,?)",
            (ts, "google_health", 50.0),
        )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-01-15"})
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT hrv, source_flags_json FROM daily_metrics WHERE date='2025-01-15'"
    ).fetchone()
    assert row[0] == 50.0  # overnight-computed, not the 60.0 all-day rollup
    assert json.loads(row[1])["hrv"] == "google_health_intraday"


def test_hrv_recomputed_using_actual_sleep_window_not_wide_window(tmp_db):
    """With real wake/duration data, only samples inside the actual bed-to-wake
    span should count — evening-awake samples outside it (which the old
    20:00-10:00 window would have included) must not pull the average down."""
    upsert_raw_metric(tmp_db, "2025-01-15", "google_health", "hrv", 60.0, utc_now())
    upsert_raw_metric(tmp_db, "2025-01-15", "google_health", "sleep_wake_hour", 7.0, utc_now())
    upsert_raw_metric(tmp_db, "2025-01-15", "google_health", "sleep_duration_min", 420.0, utc_now())  # 7h -> bedtime 00:00 CET

    from datetime import datetime, timedelta
    # In-window: 2025-01-14T23:00 CET (22:00 UTC) through 2025-01-15T06:00 CET (05:00 UTC)
    in_window_start = datetime.fromisoformat("2025-01-14T23:30:00+00:00")  # 00:30 CET, inside bed 00:00 -> wake 07:00
    for i in range(30):
        ts = (in_window_start + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp_db.execute(
            "INSERT INTO intraday_hrv(ts, source, rmssd) VALUES (?,?,?)",
            (ts, "google_health", 50.0),
        )
    # Out-of-window: early evening, well before bedtime — would bias a wide window down
    evening_start = datetime.fromisoformat("2025-01-14T18:00:00+00:00")
    for i in range(30):
        ts = (evening_start + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp_db.execute(
            "INSERT INTO intraday_hrv(ts, source, rmssd) VALUES (?,?,?)",
            (ts, "google_health", 20.0),
        )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-01-15"})
    tmp_db.commit()
    row = tmp_db.execute("SELECT hrv FROM daily_metrics WHERE date='2025-01-15'").fetchone()
    assert row[0] == 50.0  # only the in-window samples counted, not the evening 20.0s


def test_hrv_garmin_untouched_when_already_overnight(tmp_db):
    """Garmin's hrv is already overnight-only (hrvSummary.lastNight) — don't touch it
    even if intraday_hrv rows happen to exist."""
    upsert_raw_metric(tmp_db, "2025-01-15", "garmin", "hrv", 55.0, utc_now())
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-01-15"})
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT hrv, source_flags_json FROM daily_metrics WHERE date='2025-01-15'"
    ).fetchone()
    assert row[0] == 55.0
    assert json.loads(row[1])["hrv"] == "garmin"


def test_hrv_recomputed_from_intraday_when_no_daily_rollup_yet(tmp_db):
    """Google's daily-heart-rate-variability rollup can lag a day behind the
    intraday RMSSD samples syncing — hrv should still resolve from intraday
    even when there's no rollup row to recompute from at all."""
    from datetime import datetime, timedelta
    start = datetime.fromisoformat("2025-01-15T00:30:00+00:00")
    for i in range(30):
        ts = (start + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp_db.execute(
            "INSERT INTO intraday_hrv(ts, source, rmssd) VALUES (?,?,?)",
            (ts, "google_health", 50.0),
        )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-01-15"})
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT hrv, source_flags_json FROM daily_metrics WHERE date='2025-01-15'"
    ).fetchone()
    assert row[0] == 50.0
    assert json.loads(row[1])["hrv"] == "google_health_intraday"


def test_hrv_falls_back_to_daily_rollup_without_enough_intraday_samples(tmp_db):
    upsert_raw_metric(tmp_db, "2025-01-15", "google_health", "hrv", 60.0, utc_now())
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-01-15"})
    tmp_db.commit()
    row = tmp_db.execute("SELECT hrv FROM daily_metrics WHERE date='2025-01-15'").fetchone()
    assert row[0] == 60.0


def test_resting_hr_from_intraday_uses_lowest_5pct(tmp_db):
    """Default HOME_TZ is Europe/Berlin; 2025-01-15 is CET (UTC+1), so the
    overnight window is 2025-01-14T19:00Z through 2025-01-15T09:00Z."""
    from datetime import datetime, timedelta
    start = datetime.fromisoformat("2025-01-14T19:00:00+00:00")
    for i in range(100):
        ts = (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bpm = 50.0 if i < 5 else 70.0
        tmp_db.execute(
            "INSERT INTO intraday_hr(ts, source, bpm) VALUES (?,?,?)",
            (ts, "garmin", bpm),
        )
    tmp_db.commit()

    rhr, src = _resting_hr_from_intraday(tmp_db, "2025-01-15")
    assert rhr == 50.0
    assert src == "garmin_intraday"


def test_resting_hr_from_intraday_none_below_min_samples(tmp_db):
    for i in range(10):  # below RESTING_HR_MIN_SAMPLES
        tmp_db.execute(
            "INSERT INTO intraday_hr(ts, source, bpm) VALUES (?,?,?)",
            (f"2025-01-14T2{i}:00:00Z", "garmin", 55.0),
        )
    tmp_db.commit()
    rhr, src = _resting_hr_from_intraday(tmp_db, "2025-01-15")
    assert rhr is None
    assert src is None


def test_rebuild_one_day_prefers_computed_rhr_over_vendor(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-01-15", "garmin", "resting_hr", 62.0, now)
    from datetime import datetime, timedelta
    start = datetime.fromisoformat("2025-01-14T19:00:00+00:00")
    for i in range(100):
        ts = (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bpm = 48.0 if i < 5 else 65.0
        tmp_db.execute(
            "INSERT INTO intraday_hr(ts, source, bpm) VALUES (?,?,?)",
            (ts, "garmin", bpm),
        )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-01-15"})
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT resting_hr FROM daily_metrics WHERE date='2025-01-15'"
    ).fetchone()
    assert row[0] == 48.0  # computed, not the vendor's 62.0


def test_hydration_manual_wins_before_pushback(tmp_db):
    """No raw Garmin value yet (push hasn't round-tripped) — manual log should count."""
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "resting_hr", 55.0, utc_now())  # unrelated row, keeps date live
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
        ("2025-06-01T09:00:00", "hydration", "500ml", 500.0, "ml", utc_now()),
    )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-06-01"})
    tmp_db.commit()
    row = tmp_db.execute("SELECT hydration_ml FROM daily_metrics WHERE date='2025-06-01'").fetchone()
    assert row[0] == 500.0


def test_hydration_raw_wins_after_pushback_without_double_counting(tmp_db):
    """Garmin's total already reflects the pushed manual value — don't add again."""
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "hydration_ml", 1700.0, utc_now())
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
        ("2025-06-01T09:00:00", "hydration", "500ml", 500.0, "ml", utc_now()),
    )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-06-01"})
    tmp_db.commit()
    row = tmp_db.execute("SELECT hydration_ml FROM daily_metrics WHERE date='2025-06-01'").fetchone()
    assert row[0] == 1700.0


def test_compute_hr_zones_bands():
    zones = compute_hr_zones(180.0)
    assert len(zones) == 5
    assert zones[0]["zone"] == 1
    assert zones[0]["min_bpm"] == 90  # 50% of 180
    assert zones[4]["max_bpm"] == 180  # 100% of 180
    # zones should be contiguous and increasing
    for i in range(1, 5):
        assert zones[i]["min_bpm"] == zones[i - 1]["max_bpm"]


def test_new_metrics_in_daily_metrics(tmp_db):
    now = utc_now()
    for metric, val in [
        ("skin_temp_deviation", 0.3),
        ("hydration_ml", 1800.0),
        ("max_hr", 182.0),
        ("lactate_threshold_hr", 168.0),
        ("lactate_threshold_pace_min_per_km", 4.76),
        ("ftp_watts", 245.0),
        ("sleep_breathing_rate", 13.8),
        ("recovery_hours", 22.0),
    ]:
        upsert_raw_metric(tmp_db, "2025-07-01", "garmin", metric, val, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT skin_temp_deviation, hydration_ml, max_hr, lactate_threshold_hr, "
        "lactate_threshold_pace_min_per_km, ftp_watts, sleep_breathing_rate, recovery_hours "
        "FROM daily_metrics WHERE date='2025-07-01'"
    ).fetchone()
    assert tuple(row) == (0.3, 1800.0, 182.0, 168.0, 4.76, 245.0, 13.8, 22.0)


def test_hr_zones_use_rolling_max_not_todays_low_reading(tmp_db):
    """A rest-day max_hr of 108 shouldn't collapse zones down from a true peak
    of 190 seen earlier in the rolling window."""
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-06-01", "garmin", "max_hr", 190.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-06-01"})
    tmp_db.commit()

    upsert_raw_metric(tmp_db, "2025-06-15", "garmin", "max_hr", 108.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db, {"2025-06-15"})
    tmp_db.commit()

    rows = tmp_db.execute(
        "SELECT zone, min_bpm, max_bpm FROM hr_zones WHERE date='2025-06-15' ORDER BY zone"
    ).fetchall()
    assert tuple(rows[4]) == (5, 171, 190)  # zone 5 still based on the 190 peak


def test_hr_zones_derived_from_max_hr(tmp_db):
    now = utc_now()
    upsert_raw_metric(tmp_db, "2025-07-01", "garmin", "max_hr", 180.0, now)
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    tmp_db.commit()
    rows = tmp_db.execute(
        "SELECT zone, min_bpm, max_bpm FROM hr_zones WHERE date='2025-07-01' ORDER BY zone"
    ).fetchall()
    assert len(rows) == 5
    assert tuple(rows[0]) == (1, 90, 108)
    assert tuple(rows[4]) == (5, 162, 180)


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
