"""
Tests for the Garmin importer — uses a mock client so no real credentials needed.
Verifies: raw payload storage, metric parsing, activity upsert, graceful no-op.
"""

import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import app.db as db_module
from app.db import init_db, utc_now
from app.importers.garmin import (
    _parse_body_battery,
    _parse_hrv,
    _parse_sleep,
    _parse_stats,
    _parse_stress,
    _parse_training_readiness,
    _parse_training_status,
    _upsert_activity,
    run_import,
)


@pytest.fixture
def tmp_db(tmp_path):
    path = tmp_path / "test.db"
    orig = db_module.DB_PATH
    db_module.DB_PATH = path
    init_db(path)
    conn = sqlite3.connect(str(path))
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
    data = {
        "dailySleepDTO": {
            "sleepScores": {"overall": {"value": 75}},
            "sleepTimeSeconds": 27000,
            "deepSleepSeconds": 5400,
            "remSleepSeconds": 7200,
            "lightSleepSeconds": 14400,
        }
    }
    rows = _parse_sleep(tmp_db, "2025-01-01", data)
    assert rows >= 4
    score = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='sleep_score'"
    ).fetchone()
    assert score[0] == 75.0
    dur = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='sleep_duration_min'"
    ).fetchone()
    assert abs(dur[0] - 450.0) < 0.01


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
    data = {"latestVO2Max": 52.3, "trainingLoadBalance": {"acuteLoad": 300, "chronicLoad": 280}}
    rows = _parse_training_status(tmp_db, "2025-01-01", data)
    assert rows >= 3
    vo2 = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='vo2max'"
    ).fetchone()
    assert vo2[0] == 52.3


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
    return client


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
