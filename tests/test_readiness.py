"""Tests for the composite readiness score (app/readiness.py)."""

import json
import sqlite3

import pytest

import app.db as db_module
from app.db import init_db, upsert_raw_metric, utc_now
from app.readiness import (
    _circular_sd_hours,
    _ln_rmssd_cv,
    _sleep_debt_score,
    compute_illness_risk,
    compute_readiness,
    illness_risk_from_db,
    readiness_from_db,
    sleep_regularity,
    sleep_regularity_from_db,
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

def test_readiness_from_db_rhr_baseline_excludes_mismatched_methodology():
    """7 days of vendor-sourced resting_hr (55, flagged 'garmin') followed by one
    day of intraday-computed resting_hr (48, flagged 'garmin_intraday') shouldn't
    read as a huge recovery improvement — that's a methodology change, not signal.
    The RHR component should either be dropped (no matching-methodology baseline)
    or computed against a same-methodology baseline, never pinned at the 100 cap."""
    conn = sqlite3.connect(str(db_module.DB_PATH))
    now = utc_now()
    for i, d in enumerate([
        "2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04",
        "2025-04-05", "2025-04-06", "2025-04-07",
    ]):
        conn.execute(
            "INSERT INTO daily_metrics(date, resting_hr, source_flags_json) VALUES (?,?,?)",
            (d, 55.0, json.dumps({"resting_hr": "garmin"})),
        )
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, source_flags_json) VALUES (?,?,?)",
        ("2025-04-08", 48.0, json.dumps({"resting_hr": "garmin_intraday"})),
    )
    conn.commit()

    result = readiness_from_db(conn, "2025-04-08")
    conn.close()

    rhr_component = next((c for c in result["components"] if c["name"] == "Resting HR"), None)
    assert rhr_component is None or rhr_component["score"] < 100.0


def test_readiness_from_db_hrv_baseline_excludes_mismatched_methodology():
    """7 days of Google's all-day HRV rollup (60, flagged 'google_health') followed
    by one day of overnight-computed HRV (50, flagged 'google_health_intraday')
    shouldn't read as a big HRV drop — that's a methodology change, not signal."""
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for d in ["2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04",
              "2025-05-05", "2025-05-06", "2025-05-07"]:
        conn.execute(
            "INSERT INTO daily_metrics(date, hrv, source_flags_json) VALUES (?,?,?)",
            (d, 60.0, json.dumps({"hrv": "google_health"})),
        )
    conn.execute(
        "INSERT INTO daily_metrics(date, hrv, source_flags_json) VALUES (?,?,?)",
        ("2025-05-08", 50.0, json.dumps({"hrv": "google_health_intraday"})),
    )
    conn.commit()

    result = readiness_from_db(conn, "2025-05-08")
    conn.close()

    hrv_component = next((c for c in result["components"] if c["name"] == "HRV"), None)
    # With no matching-methodology baseline, the component should drop out rather
    # than score against Google's incomparable all-day average.
    assert hrv_component is None


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


# ── compute_illness_risk ────────────────────────────────────────────────────

def _day(d, rhr=50.0, rhr_base=50.0, hrv=60.0, hrv_base=60.0, skin_temp=0.0):
    return {
        "date": d, "rhr": rhr, "rhr_baseline": rhr_base,
        "hrv": hrv, "hrv_baseline": hrv_base, "skin_temp_deviation": skin_temp,
    }


def test_compute_illness_risk_green_when_all_near_baseline():
    result = compute_illness_risk([_day("2026-01-01")])
    assert result["level"] == "green"
    assert result["signals"] == []


def test_compute_illness_risk_yellow_for_single_signal():
    result = compute_illness_risk([_day("2026-01-01", rhr=54.0)])  # +4bpm
    assert result["level"] == "yellow"
    assert len(result["signals"]) == 1


def test_compute_illness_risk_red_requires_two_signals_same_day():
    result = compute_illness_risk([_day("2026-01-01", rhr=54.0, hrv=50.0)])  # +4bpm, 83% baseline
    assert result["level"] == "red"
    assert len(result["signals"]) == 2


def test_compute_illness_risk_red_for_cascade_across_days():
    """RHR elevated day 1, skin temp elevated day 2, HRV suppressed day 3 — none
    concordant on a single day, but the trailing window should still catch it."""
    days = [
        _day("2026-01-01", rhr=54.0),
        _day("2026-01-02", skin_temp=0.5),
        _day("2026-01-03", hrv=50.0),
    ]
    result = compute_illness_risk(days)
    assert result["level"] == "red"
    assert len(result["signals"]) == 3


def test_compute_illness_risk_no_data_when_no_baselines():
    days = [{"date": "2026-01-01", "rhr": None, "rhr_baseline": None,
             "hrv": None, "hrv_baseline": None, "skin_temp_deviation": None}]
    result = compute_illness_risk(days)
    assert result["level"] is None


def test_compute_illness_risk_hard_training_day_not_flagged_by_rhr_alone():
    """A single elevated RHR from hard training shouldn't read as 'possible
    illness' on its own — that's exactly why red requires 2+ concordant signals."""
    result = compute_illness_risk([_day("2026-01-01", rhr=54.0)])
    assert result["level"] != "red"


# ── illness_risk_from_db integration ────────────────────────────────────────

def test_illness_risk_from_db_flags_cascade_within_window():
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for d in ["2025-06-01", "2025-06-02", "2025-06-03", "2025-06-04",
              "2025-06-05", "2025-06-06", "2025-06-07"]:
        conn.execute(
            "INSERT INTO daily_metrics(date, resting_hr, hrv, source_flags_json) VALUES (?,?,?,?)",
            (d, 50.0, 60.0, "{}"),
        )
    # Trailing 3-day window: RHR up on day 1, skin temp up on day 2, HRV down on day 3.
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-08", 54.0, 60.0, None, "{}"))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-09", 50.0, 60.0, 0.5, "{}"))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-10", 50.0, 50.0, None, "{}"))
    conn.commit()

    result = illness_risk_from_db(conn, "2025-06-10")
    conn.close()

    assert result["level"] == "red"
    assert len(result["signals"]) == 3


def test_illness_risk_from_db_no_data_returns_none_level():
    conn = sqlite3.connect(str(db_module.DB_PATH))
    result = illness_risk_from_db(conn, "2025-06-10")
    conn.close()
    assert result["level"] is None


# ── sleep regularity ──────────────────────────────────────────────────────────

def test_circular_sd_hours_none_with_too_few_nights():
    assert _circular_sd_hours([7.0, 7.5]) is None


def test_circular_sd_hours_low_for_consistent_wake_times():
    sd = _circular_sd_hours([7.0, 7.1, 6.9, 7.0, 7.2])
    assert sd is not None
    assert sd < 0.3


def test_circular_sd_hours_handles_midnight_wraparound():
    """Bedtimes clustered around midnight (23.5, 0.5) shouldn't read as wildly
    variable just because they're numerically far apart."""
    naive_sd = (sum((h - 12) ** 2 for h in [23.5, 0.5, 23.8, 0.2]) / 4) ** 0.5
    circular_sd = _circular_sd_hours([23.5, 0.5, 23.8, 0.2])
    assert circular_sd is not None
    assert circular_sd < naive_sd


def test_circular_sd_hours_high_for_erratic_times():
    consistent = _circular_sd_hours([7.0, 7.1, 6.9, 7.0])
    erratic = _circular_sd_hours([5.0, 9.0, 6.0, 10.0])
    assert erratic > consistent


def test_sleep_regularity_score_high_for_consistent_nights():
    nights = [(7.0, 480.0), (7.1, 470.0), (6.9, 475.0), (7.0, 480.0)]
    result = sleep_regularity(nights)
    assert result["score"] is not None
    assert result["score"] > 80


def test_sleep_regularity_score_low_for_erratic_nights():
    nights = [(5.0, 300.0), (9.0, 600.0), (6.0, 400.0), (10.0, 350.0)]
    result = sleep_regularity(nights)
    assert result["score"] is not None
    assert result["score"] < 60


def test_sleep_regularity_none_with_no_nights():
    result = sleep_regularity([])
    assert result["score"] is None


def test_sleep_regularity_from_db(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    now = utc_now()
    dates = ["2025-03-01", "2025-03-02", "2025-03-03", "2025-03-04"]
    for d, wake, dur in zip(dates, [7.0, 7.1, 6.9, 7.0], [480.0, 470.0, 475.0, 480.0]):
        conn.execute(
            "INSERT INTO daily_metrics(date, sleep_duration_min, source_flags_json) VALUES (?,?,?)",
            (d, dur, "{}"),
        )
        upsert_raw_metric(conn, d, "garmin", "sleep_wake_hour", wake, now)
    conn.commit()

    result = sleep_regularity_from_db(conn, "2025-03-04")
    conn.close()
    assert result["score"] is not None
    assert result["score"] > 80
