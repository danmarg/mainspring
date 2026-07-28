"""
Tests for the Garmin importer — uses a mock client so no real credentials needed.
Verifies: raw payload storage, metric parsing, activity upsert, graceful no-op.
"""

import json
import os
import sqlite3
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

import app.db as db_module
from app.db import init_db, utc_now
from app.importers.garmin import (
    _compute_decoupling,
    _parse_body_battery,
    _parse_hrv,
    _parse_sleep,
    _parse_splits,
    _parse_stats,
    _parse_stress,
    _parse_training_readiness,
    _parse_training_status,
    _upsert_activity,
    backfill_decoupling,
    run_import,
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


# ── unit tests for parsers ──────────────────────────────────────────────────

def test_parse_stats(tmp_db):
    data = {"restingHeartRate": 52, "totalSteps": 9000, "totalKilocalories": 2100}
    rows = _parse_stats(tmp_db, "2025-01-01", data)
    assert rows == 3
    row = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='resting_hr'"
    ).fetchone()
    assert row[0] == 52.0


def test_parse_hrv(tmp_db):
    data = {"hrvSummary": {"lastNight": 48, "status": "BALANCED"}}
    rows = _parse_hrv(tmp_db, "2025-01-01", data)
    assert rows >= 1
    row = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='hrv'"
    ).fetchone()
    assert row[0] == 48.0


def test_parse_sleep(tmp_db):
    # sleepEndTimestampLocal: datetime(2025,1,1,7,30,0) treated as UTC epoch
    # = 1735716600 seconds = 1735716600000 ms → wake_hour = 7.5
    WAKE_TS_MS = 1735716600000
    data = {
        "dailySleepDTO": {
            "sleepScores": {"overall": {"value": 75}},
            "sleepTimeSeconds": 27000,
            "deepSleepSeconds": 5400,
            "remSleepSeconds": 7200,
            "lightSleepSeconds": 14400,
            "sleepEndTimestampLocal": WAKE_TS_MS,
        }
    }
    rows = _parse_sleep(tmp_db, "2025-01-01", data)
    assert rows >= 5
    score = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='sleep_score'"
    ).fetchone()
    assert score[0] == 75.0
    dur = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='sleep_duration_min'"
    ).fetchone()
    assert abs(dur[0] - 450.0) < 0.01
    wake = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='sleep_wake_hour'"
    ).fetchone()
    assert wake is not None
    assert abs(wake[0] - 7.5) < 0.01


def test_parse_stress(tmp_db):
    data = {"avgStressLevel": 35, "maxStressLevel": 72}
    rows = _parse_stress(tmp_db, "2025-01-01", data)
    assert rows == 2


def test_parse_body_battery(tmp_db):
    data = [{"charged": 85, "drained": 20, "date": "2025-01-01"}]
    rows = _parse_body_battery(tmp_db, "2025-01-01", data)
    assert rows == 2
    high = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='body_battery_high'"
    ).fetchone()
    assert high[0] == 85.0


def test_parse_training_readiness(tmp_db):
    data = [{"score": 68, "trainingReadinessScore": 68}]
    rows = _parse_training_readiness(tmp_db, "2025-01-01", data)
    assert rows == 1


def test_parse_training_status_vo2max(tmp_db):
    data = {
        "mostRecentVO2Max": {
            "generic": {
                "vo2MaxPreciseValue": 52.3,
                "vo2MaxValue": 52.0,
            }
        },
        "mostRecentTrainingLoadBalance": {
            "metricsTrainingLoadBalanceDTOMap": {
                "123": {
                    "primaryTrainingDevice": True,
                    "monthlyLoadAerobicLow": 800.0,
                    "monthlyLoadAerobicHigh": 400.0,
                    "monthlyLoadAnaerobic": 100.0,
                }
            }
        },
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "123": {
                    "primaryTrainingDevice": True,
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 450,
                        "dailyTrainingLoadChronic": 520,
                    },
                }
            }
        },
    }
    rows = _parse_training_status(tmp_db, "2025-01-01", data)
    assert rows >= 6
    vo2 = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='vo2max'"
    ).fetchone()
    assert vo2[0] == 52.3
    load = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='monthly_load_aerobic_low'"
    ).fetchone()
    assert load[0] == 800.0
    atl = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='atl'"
    ).fetchone()
    assert atl[0] == 450.0
    ctl = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='ctl'"
    ).fetchone()
    assert ctl[0] == 520.0


def test_upsert_activity(tmp_db):
    activity = {
        "activityId": "123456789",
        "startTimeLocal": "2025-01-01T07:30:00",
        "activityType": {"typeKey": "running"},
        "duration": 3600,
        "distance": 10000.0,
        "averageHR": 148,
        "maxHR": 172,
        "aerobicTrainingEffect": 3.5,
        "calories": 650,
    }
    assert _upsert_activity(tmp_db, activity)
    tmp_db.commit()
    row = tmp_db.execute(
        "SELECT type, duration_s, avg_hr FROM garmin_activities WHERE activity_id='123456789'"
    ).fetchone()
    assert row[0] == "running"
    assert row[1] == 3600
    assert row[2] == 148


def test_upsert_activity_idempotent(tmp_db):
    activity = {
        "activityId": "111",
        "startTimeLocal": "2025-01-01T08:00:00",
        "activityType": {"typeKey": "cycling"},
        "duration": 1800,
        "distance": 30000.0,
        "averageHR": 155,
        "calories": 500,
    }
    _upsert_activity(tmp_db, activity)
    tmp_db.commit()
    activity["averageHR"] = 160
    _upsert_activity(tmp_db, activity)
    tmp_db.commit()
    rows = tmp_db.execute(
        "SELECT avg_hr FROM garmin_activities WHERE activity_id='111'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 160


# ── integration test: run_import with mock client ──────────────────────────

MOCK_STATS = {"restingHeartRate": 55, "totalSteps": 8500, "totalKilocalories": 2200}
MOCK_HRV = {"hrvSummary": {"lastNight": 52}}
MOCK_SLEEP = {
    "dailySleepDTO": {
        "sleepScores": {"overall": {"value": 80}},
        "sleepTimeSeconds": 28800,
        "deepSleepSeconds": 5400,
        "remSleepSeconds": 7200,
        "lightSleepSeconds": 16200,
    }
}
MOCK_STRESS = {"avgStressLevel": 28, "maxStressLevel": 65}
MOCK_READINESS = [{"score": 72}]
MOCK_TRAINING = {"latestVO2Max": 51.0}
MOCK_BODY_BATTERY = [{"date": "2025-01-01", "charged": 90, "drained": 15}]
MOCK_SPO2 = {"averageSpO2": 96.5}
MOCK_RESPIRATION = {"avgBreathingRate": 14.2}
MOCK_INTENSITY = {"moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 15}
MOCK_HEART_RATES = {"heartRateValues": [[1735714800000, 58], [1735714920000, 60]]}
MOCK_ACTIVITIES = [
    {
        "activityId": "999",
        "startTimeLocal": "2025-01-01T07:00:00",
        "activityType": {"typeKey": "running"},
        "duration": 2700,
        "distance": 7500.0,
        "averageHR": 142,
        "calories": 420,
    }
]


def _make_mock_client():
    client = MagicMock()
    client.get_stats.return_value = MOCK_STATS
    client.get_hrv_data.return_value = MOCK_HRV
    client.get_sleep_data.return_value = MOCK_SLEEP
    client.get_stress_data.return_value = MOCK_STRESS
    client.get_training_readiness.return_value = MOCK_READINESS
    client.get_training_status.return_value = MOCK_TRAINING
    client.get_body_battery.return_value = MOCK_BODY_BATTERY
    client.get_activities_by_date.return_value = MOCK_ACTIVITIES
    client.get_spo2_data.return_value = MOCK_SPO2
    client.get_respiration_data.return_value = MOCK_RESPIRATION
    client.get_intensity_minutes_data.return_value = MOCK_INTENSITY
    client.get_heart_rates.return_value = MOCK_HEART_RATES
    client.get_activity_splits.return_value = {
        "lapDTOs": [
            {"distance": 1000.0, "duration": 300, "averageHR": 135},
            {"distance": 1000.0, "duration": 300, "averageHR": 138},
            {"distance": 1000.0, "duration": 300, "averageHR": 140},
            {"distance": 1000.0, "duration": 300, "averageHR": 145},
            {"distance": 1000.0, "duration": 300, "averageHR": 148},
            {"distance": 1000.0, "duration": 300, "averageHR": 150},
        ]
    }
    client.get_scheduled_workouts.return_value = [
        {
            "date": "2025-01-01",
            "workoutType": "base",
            "workoutDescription": "Easy run",
            "durationInSeconds": 3600,
            "intensityType": "easy",
        }
    ]
    return client


# ── aerobic decoupling ───────────────────────────────────────────────────────

def test_parse_splits():
    data = {"lapDTOs": [{"distance": 1000.0, "duration": 300, "averageHR": 140}]}
    assert _parse_splits(data) == [{"distance_m": 1000.0, "duration_s": 300, "avg_hr": 140}]


def test_parse_splits_empty():
    assert _parse_splits({}) == []


def test_compute_decoupling_hr_drift_is_positive():
    # steady pace, HR climbs from 135 to 150 across 6 even laps -> HR:pace ratio rises
    splits = [
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 135},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 138},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 140},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 145},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 148},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 150},
    ]
    result = _compute_decoupling(splits)
    assert result is not None
    assert result > 0


def test_compute_decoupling_steady_effort_near_zero():
    splits = [{"distance_m": 1000.0, "duration_s": 300, "avg_hr": 140} for _ in range(6)]
    assert _compute_decoupling(splits) == 0.0


def test_compute_decoupling_too_few_splits():
    splits = [{"distance_m": 1000.0, "duration_s": 300, "avg_hr": 140} for _ in range(3)]
    assert _compute_decoupling(splits) is None


def test_compute_decoupling_ignores_malformed_laps():
    # one lap missing distance_m is filtered internally; 5 remaining valid
    # laps is still >= the 4-lap minimum, so this should still compute.
    splits = [
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 135},
        {"distance_m": None, "duration_s": 300, "avg_hr": 138},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 140},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 145},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 148},
        {"distance_m": 1000.0, "duration_s": 300, "avg_hr": 150},
    ]
    assert _compute_decoupling(splits) is not None


def test_backfill_decoupling_no_credentials(tmp_db, monkeypatch):
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    assert backfill_decoupling(tmp_db, days=90) == 0


def test_backfill_decoupling_computes_for_existing_activity(tmp_db, monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", "fake-token-store-value-that-is-long-enough-to-be-treated-as-token-data-not-a-path-xxxxxxxxx")

    activity_date = (date.today() - timedelta(days=10)).isoformat()
    tmp_db.execute(
        """
        INSERT INTO garmin_activities (activity_id, date, start_time, type, duration_s, distance_m, avg_hr, calories, fetched_at)
        VALUES ('555', ?, ?, 'running', 1800, 5000.0, 145, 400, ?)
        """,
        (activity_date, f"{activity_date}T07:00:00", f"{activity_date}T07:00:00+00:00"),
    )
    tmp_db.commit()

    mock_client = _make_mock_client()
    with patch("app.importers.garmin._client", return_value=mock_client):
        computed = backfill_decoupling(tmp_db, days=365)

    assert computed == 1
    row = tmp_db.execute("SELECT decoupling_pct FROM garmin_activities WHERE activity_id='555'").fetchone()
    assert row[0] is not None


def test_run_import_no_credentials(tmp_db, monkeypatch):
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    result = run_import(tmp_db, days=1)
    assert result["skipped"] is True


def test_run_import_with_mock_client(tmp_db, monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", "fake-token-store-value-that-is-long-enough-to-be-treated-as-token-data-not-a-path-xxxxxxxxx")

    mock_client = _make_mock_client()

    with patch("app.importers.garmin._client", return_value=mock_client):
        result = run_import(tmp_db, days=1)

    assert not result.get("skipped")
    assert result["rows_upserted"] > 0

    # raw payload was stored
    payload_count = tmp_db.execute(
        "SELECT COUNT(*) FROM raw_import_payloads"
    ).fetchone()[0]
    assert payload_count > 0

    # core metrics present
    hrv = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE metric='hrv' AND source='garmin'"
    ).fetchone()
    assert hrv is not None and hrv[0] == 52.0

    rhr = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE metric='resting_hr' AND source='garmin'"
    ).fetchone()
    assert rhr is not None and rhr[0] == 55.0

    # activity stored
    activity = tmp_db.execute(
        "SELECT type FROM garmin_activities WHERE activity_id='999'"
    ).fetchone()
    assert activity is not None and activity[0] == "running"
