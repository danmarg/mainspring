from datetime import datetime, timedelta, timezone

import pytest

import app.db as db_module
from app.calibration import classify_energy_log, evaluate_tau_samples, maybe_run_energy_calibration, run_energy_tau_calibration
from app.db import db, init_db, utc_now


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    yield path


def _seed_day(conn, day: str):
    conn.execute("INSERT INTO day_timezone(date, tz, source) VALUES (?,?,?)", (day, "UTC", "test"))
    conn.execute("INSERT INTO raw_daily_metrics(date, source, metric, value, fetched_at) VALUES (?,?,?,?,?)", (day, "garmin", "sleep_wake_hour", 7.0, utc_now()))
    conn.execute("INSERT INTO raw_daily_metrics(date, source, metric, value, fetched_at) VALUES (?,?,?,?,?)", (day, "garmin", "resting_hr", 50.0, utc_now()))


def test_energy_log_classification_uses_wake_relative_time():
    with db() as conn:
        _seed_day(conn, "2026-01-10")
        assert classify_energy_log(conn, "2026-01-10T08:00:00+00:00") == "morning"
        assert classify_energy_log(conn, "2026-01-10T14:00:00+00:00") == "intraday"


def test_calibration_requires_enough_labeled_ratings():
    with db() as conn:
        _seed_day(conn, "2026-01-10")
        conn.execute("INSERT INTO manual_logs(ts,type,quantity,created_at) VALUES (?,?,?,?)", ("2026-01-10T14:00:00+00:00", "energy", 2, utc_now()))
        result = run_energy_tau_calibration(conn)
    assert result["status"] == "insufficient"
    assert result["n_labels"] == 1


def test_tau_evaluation_finds_discriminating_samples():
    # Low ratings follow high strain, high ratings follow low strain.
    result = evaluate_tau_samples([(16.0, 0)] * 12 + [(2.0, 1)] * 12)
    assert result is not None
    assert result["auc"] == pytest.approx(1.0)
    assert result["slope"] < 0


def test_weekly_claim_prevents_duplicate_runs():
    first = maybe_run_energy_calibration()
    second = maybe_run_energy_calibration()
    assert first["status"] == "insufficient"
    assert second == {"status": "not_due"}
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM model_calibration_runs").fetchone()[0]
    assert count == 1
