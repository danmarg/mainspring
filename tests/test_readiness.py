"""Tests for the composite readiness score (app/readiness.py)."""

import sqlite3

import pytest

import app.db as db_module
from app.db import init_db, upsert_raw_metric, utc_now
from app.readiness import (
    _ln_rmssd_cv,
    _sleep_debt_score,
    compute_readiness,
    readiness_from_db,
)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    yield path


# ── _ln_rmssd_cv ─────────────────────────────────────────────────────────────

def test_ln_rmssd_cv_none_with_too_few_nights():
    assert _ln_rmssd_cv([50.0, 52.0, 48.0]) is None


def test_ln_rmssd_cv_low_for_stable_values():
    stable = [50.0, 51.0, 49.0, 50.0, 52.0, 48.0, 50.0]
    cv = _ln_rmssd_cv(stable)
    assert cv is not None
    assert cv < 0.05


def test_ln_rmssd_cv_higher_for_noisy_values():
    noisy = [40.0, 65.0, 35.0, 70.0, 38.0, 60.0, 45.0]
    stable = [50.0, 51.0, 49.0, 50.0, 52.0, 48.0, 50.0]
    assert _ln_rmssd_cv(noisy) > _ln_rmssd_cv(stable)


# ── _sleep_debt_score ────────────────────────────────────────────────────────

def test_sleep_debt_score_none_when_empty():
    assert _sleep_debt_score([]) is None


def test_sleep_debt_score_single_night_equals_that_score():
    assert _sleep_debt_score([("2025-01-01", 80.0)]) == pytest.approx(80.0)


def test_sleep_debt_score_lags_after_bad_streak():
    """One good night after three bad ones should still read below the good
    night's raw score — debt shouldn't reset in a single night."""
    nights = [
        ("2025-01-01", 50.0),
        ("2025-01-02", 50.0),
        ("2025-01-03", 50.0),
        ("2025-01-04", 90.0),
    ]
    debt = _sleep_debt_score(nights)
    assert debt is not None
    assert debt < 90.0
    assert debt > 50.0


def test_sleep_debt_score_weights_recent_night_more():
    lagging = _sleep_debt_score([
        ("2025-01-01", 90.0), ("2025-01-02", 50.0),
    ])
    recent_good = _sleep_debt_score([
        ("2025-01-01", 50.0), ("2025-01-02", 90.0),
    ])
    assert recent_good > lagging


# ── compute_readiness: HRV + CV ──────────────────────────────────────────────

def test_compute_readiness_hrv_cv_penalizes_score():
    stable = compute_readiness(
        hrv=55.0, hrv_7d=55.0, hrv_cv=0.05,
        sleep_debt_score=None, rhr=None, rhr_7d=None, atl=None, ctl=None,
    )
    noisy = compute_readiness(
        hrv=55.0, hrv_7d=55.0, hrv_cv=0.20,
        sleep_debt_score=None, rhr=None, rhr_7d=None, atl=None, ctl=None,
    )
    hrv_stable = next(c for c in stable["components"] if c["name"] == "HRV")
    hrv_noisy = next(c for c in noisy["components"] if c["name"] == "HRV")
    assert hrv_noisy["score"] < hrv_stable["score"]


def test_compute_readiness_no_cv_data_unaffected():
    result = compute_readiness(
        hrv=55.0, hrv_7d=50.0, hrv_cv=None,
        sleep_debt_score=None, rhr=None, rhr_7d=None, atl=None, ctl=None,
    )
    hrv_component = next(c for c in result["components"] if c["name"] == "HRV")
    assert hrv_component["score"] > 50.0


# ── compute_readiness: ACWR ───────────────────────────────────────────────────

def test_compute_readiness_acwr_sweet_spot_scores_100():
    result = compute_readiness(
        hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=100.0, ctl=100.0,  # ratio = 1.0
    )
    acwr = next(c for c in result["components"] if c["name"] == "ACWR")
    assert acwr["score"] == 100.0


def test_compute_readiness_acwr_penalizes_high_ratio_more_than_low():
    low = compute_readiness(
        hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=60.0, ctl=100.0,  # ratio 0.6, 0.2 below sweet spot
    )
    high = compute_readiness(
        hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=150.0, ctl=100.0,  # ratio 1.5, 0.2 above sweet spot
    )
    low_score = next(c for c in low["components"] if c["name"] == "ACWR")["score"]
    high_score = next(c for c in high["components"] if c["name"] == "ACWR")["score"]
    assert high_score < low_score


def test_compute_readiness_tsb_still_present_alongside_acwr():
    result = compute_readiness(
        hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=80.0, ctl=100.0,
    )
    names = {c["name"] for c in result["components"]}
    assert "Form (TSB)" in names
    assert "ACWR" in names


# ── compute_readiness: no data ────────────────────────────────────────────────

def test_compute_readiness_no_data_returns_none_score():
    result = compute_readiness(
        hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=None, ctl=None,
    )
    assert result["score"] is None
    assert result["label"] == "No data"


# ── readiness_from_db integration ────────────────────────────────────────────

def test_readiness_from_db_uses_trailing_sleep_debt_and_acwr():
    conn = sqlite3.connect(str(db_module.DB_PATH))
    dates = ["2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
             "2025-02-05", "2025-02-06", "2025-02-07", "2025-02-08"]
    hrv_values = [50.0, 51.0, 49.0, 50.0, 52.0, 48.0, 50.0, 55.0]
    sleep_values = [50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 90.0]
    for d, hrv, sleep in zip(dates, hrv_values, sleep_values):
        conn.execute(
            "INSERT INTO daily_metrics(date, hrv, sleep_score, resting_hr, source_flags_json) "
            "VALUES (?,?,?,?,?)",
            (d, hrv, sleep, 55.0, "{}"),
        )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(db_module.DB_PATH))
    now = utc_now()
    upsert_raw_metric(conn, "2025-02-08", "garmin", "ctl", 100.0, now)
    upsert_raw_metric(conn, "2025-02-08", "garmin", "atl", 100.0, now)
    conn.commit()

    result = readiness_from_db(conn, "2025-02-08")
    conn.close()

    assert result["score"] is not None
    names = {c["name"] for c in result["components"]}
    assert "ACWR" in names
    assert "Form (TSB)" in names

    sleep_component = next(c for c in result["components"] if c["name"] == "Sleep")
    # Last night was 90 but preceded by a week of 50s — debt-adjusted score should
    # land well below the raw last-night value.
    assert sleep_component["score"] < 90.0
