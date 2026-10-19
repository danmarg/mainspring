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
    decoupling_trend_from_db,
    illness_risk_from_db,
    intensity_distribution_from_db,
    readiness_from_db,
    sleep_regularity,
    sleep_regularity_from_db,
    training_monotony_strain,
    alertness_curve,
    trimp_from_hr_samples,
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

def test_compute_readiness_acwr_neutral_when_building_peaks_when_fresh():
    def acwr_score(atl):
        result = compute_readiness(
            hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
            rhr=None, rhr_7d=None, atl=atl, ctl=100.0,
        )
        return next(c for c in result["components"] if c["name"] == "ACWR")["score"]
    assert acwr_score(100.0) == pytest.approx(75.0)   # steady building: ordinary, not primed
    assert acwr_score(90.0) == pytest.approx(88.0)    # slightly fresh: best
    assert acwr_score(140.0) < acwr_score(100.0)


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


def test_compute_readiness_scores_load_once_scale_free():
    # Form (CTL-ATL)/CTL is 1 - ACWR, so it's reported, not scored separately; and
    # Garmin-scale loads (hundreds) must score the same as the same ratio at any scale.
    garmin = compute_readiness(
        hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=560.0, ctl=671.0,
    )
    small = compute_readiness(
        hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=56.0, ctl=67.1,
    )
    names = [c["name"] for c in garmin["components"]]
    assert names == ["ACWR"]
    assert garmin["components"][0]["score"] == pytest.approx(small["components"][0]["score"])
    assert "form +17%" in garmin["components"][0]["detail"]


def test_hrv_band_score_shape():
    from app.readiness import _hrv_band_score
    assert _hrv_band_score(0.0) == pytest.approx(66.0)
    assert _hrv_band_score(-2.0) < _hrv_band_score(-0.5) < _hrv_band_score(0.0)
    # Well above normal eases off rather than rising, but never below 60.
    assert _hrv_band_score(3.0) < _hrv_band_score(1.0)
    assert _hrv_band_score(10.0) == 60.0


def test_compute_readiness_prefers_hrv_z_over_ratio():
    result = compute_readiness(
        hrv=40.0, hrv_7d=50.0, hrv_cv=None, sleep_debt_score=None,
        rhr=None, rhr_7d=None, atl=None, ctl=None, hrv_z=0.0,
    )
    hrv_component = result["components"][0]
    assert hrv_component["score"] == pytest.approx(66.0)
    assert "SD vs your 60-day normal" in hrv_component["detail"]


def test_ordinary_day_composes_to_ready():
    # Everything exactly at personal normal, decent sleep, steady building load:
    # "Ready", not "Primed" — Primed is reserved for genuinely above-normal days.
    result = compute_readiness(
        hrv=50.0, hrv_7d=50.0, hrv_cv=0.04, sleep_debt_score=75.0,
        rhr=50.0, rhr_7d=50.0, atl=100.0, ctl=100.0, hrv_z=0.0, rhr_sd=1.5,
    )
    assert 65 <= result["score"] < 80
    assert result["label"] == "Ready"


def test_compute_readiness_rhr_scaled_by_personal_sd():
    # +1 bpm is a big move for someone with 0.5 bpm noise, small for someone with 3 bpm.
    tight = compute_readiness(hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
                              rhr=51.0, rhr_7d=50.0, atl=None, ctl=None, rhr_sd=0.5)
    loose = compute_readiness(hrv=None, hrv_7d=None, hrv_cv=None, sleep_debt_score=None,
                              rhr=51.0, rhr_7d=50.0, atl=None, ctl=None, rhr_sd=3.0)
    assert tight["components"][0]["score"] == pytest.approx(29.0)
    assert loose["components"][0]["score"] == pytest.approx(59.0)


def test_hrv_rolling_z_from_db():
    from app.readiness import _hrv_rolling_z
    conn = sqlite3.connect(str(db_module.DB_PATH))
    from datetime import date as _d, timedelta as _td
    end = _d(2025, 6, 30)
    for i in range(67):
        d = (end - _td(days=i)).isoformat()
        hrv = 40.0 if i < 7 else (50.0 if i % 2 else 52.0)  # last week well below normal
        conn.execute("INSERT INTO daily_metrics(date, hrv, source_flags_json) VALUES (?,?,?)",
                     (d, hrv, "{}"))
    conn.commit()
    z = _hrv_rolling_z(conn, end.isoformat(), False)
    conn.close()
    assert z is not None and z < -5


def test_trimp_coefficient_follows_athlete_sex(monkeypatch):
    from app.readiness import trimp_from_hr_samples
    male = trimp_from_hr_samples([150.0] * 30, 30, 50, 190)
    monkeypatch.setenv("ATHLETE_SEX", "female")
    female = trimp_from_hr_samples([150.0] * 30, 30, 50, 190)
    assert female < male


def test_self_computed_chronic_load_excludes_acute_week():
    from datetime import date as _d
    from app.readiness import _ewma_load
    as_of = _d(2025, 6, 30)
    # Training only in the last 3 days: coupled chronic sees it, uncoupled doesn't.
    trimp = {"2025-06-30": 100.0, "2025-06-29": 100.0, "2025-06-28": 100.0}
    assert _ewma_load(trimp, as_of, 42, 180) > 0
    assert _ewma_load(trimp, as_of, 42, 180, skip_days=7) == 0


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
    assert "Form (TSB)" not in names

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


def test_compute_illness_risk_cascade_across_days_stays_yellow():
    """RHR elevated day 1, skin temp elevated day 2, HRV suppressed day 3 — none
    concordant on a single day. This used to escalate to red on total count
    alone, but that over-alerted in practice (3 independent noisy metrics hit
    "2+ somewhere in 3 days" often by chance) — it should surface as yellow,
    with all 3 signals visible for context, not read as the stronger same-night
    claim "red" is reserved for."""
    days = [
        _day("2026-01-01", rhr=54.0),
        _day("2026-01-02", skin_temp=0.5),
        _day("2026-01-03", hrv=50.0),
    ]
    result = compute_illness_risk(days)
    assert result["level"] == "yellow"
    assert len(result["signals"]) == 3


def test_compute_illness_risk_no_data_when_no_baselines():
    days = [{"date": "2026-01-01", "rhr": None, "rhr_baseline": None,
             "hrv": None, "hrv_baseline": None, "skin_temp_deviation": None}]
    result = compute_illness_risk(days)
    assert result["level"] is None


def test_compute_illness_risk_noisy_baseline_not_flagged_by_small_delta():
    """Someone with naturally noisy RHR (sd=4bpm) shouldn't trip the fixed
    +3bpm cutoff — that's exactly the over-alerting the SD-relative
    threshold (1.5x trailing SD) exists to fix."""
    day = _day("2026-01-01", rhr=54.0)  # +4bpm, would trigger under the old flat rule
    day["rhr_sd"] = 4.0  # 1.5 * 4.0 = 6bpm threshold, so +4bpm shouldn't trigger
    result = compute_illness_risk([day])
    assert result["level"] == "green"
    assert result["signals"] == []


def test_compute_illness_risk_stable_baseline_flagged_by_small_delta():
    """Someone with a very stable RHR (sd=1bpm) should be flagged by a
    smaller absolute delta than the flat +3bpm default would require."""
    day = _day("2026-01-01", rhr=52.0)  # +2bpm, below the old flat +3bpm cutoff
    day["rhr_sd"] = 1.0  # 1.5 * 1.0 = 1.5bpm threshold, so +2bpm should trigger
    result = compute_illness_risk([day])
    assert result["level"] == "yellow"
    assert len(result["signals"]) == 1


def test_compute_illness_risk_hard_training_day_not_flagged_by_rhr_alone():
    """A single elevated RHR from hard training shouldn't read as 'possible
    illness' on its own — that's exactly why red requires 2+ concordant signals."""
    result = compute_illness_risk([_day("2026-01-01", rhr=54.0)])
    assert result["level"] != "red"


def test_compute_illness_risk_downgrades_when_two_signals_reverted():
    """RHR and skin temp both crossed threshold on the same past day, but
    neither is active on the most recent day — this is a resolved one-day
    blip, not an ongoing state, so it shouldn't read as 'red' anymore even
    though 2 signals technically fired somewhere in the window."""
    days = [
        _day("2026-01-01", rhr=54.0, skin_temp=0.5),  # both trigger, in the past
        _day("2026-01-02"),  # today: everything back to baseline
    ]
    result = compute_illness_risk(days)
    assert result["level"] == "yellow"
    assert result["label"] == "Recent deviation (resolved)"
    assert len(result["signals"]) == 2  # still visible for context


def test_compute_illness_risk_active_cascade_still_not_red():
    """A cascade (RHR day 1, skin temp day 2, HRV day 3) with today's HRV signal
    still active is NOT red — cascades never concordant on a single night
    don't meet the same-night-concordance bar red requires, regardless of
    whether the most recent trigger is still active."""
    days = [
        _day("2026-01-01", rhr=54.0),
        _day("2026-01-02", skin_temp=0.5),
        _day("2026-01-03", hrv=50.0),  # today — still an active trigger
    ]
    result = compute_illness_risk(days)
    assert result["level"] == "yellow"


# ── illness_risk_from_db integration ────────────────────────────────────────

def test_illness_risk_from_db_cascade_within_window_stays_yellow():
    """RHR up on day 1, skin temp up on day 2, HRV down on day 3 — no two
    signals concordant on the same night, so this stays yellow (informational)
    rather than escalating to red; all 3 signals still surface for context."""
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

    assert result["level"] == "yellow"
    assert len(result["signals"]) == 3


def test_illness_risk_from_db_red_when_same_night_concordant_and_active():
    """RHR and skin temp both cross threshold on the same night, and that
    night is today — this is the same-night concordance red requires."""
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for d in ["2025-06-01", "2025-06-02", "2025-06-03", "2025-06-04",
              "2025-06-05", "2025-06-06", "2025-06-07"]:
        conn.execute(
            "INSERT INTO daily_metrics(date, resting_hr, hrv, source_flags_json) VALUES (?,?,?,?)",
            (d, 50.0, 60.0, "{}"),
        )
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-08", 50.0, 60.0, 0.0, "{}"))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-09", 50.0, 60.0, 0.0, "{}"))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-10", 54.0, 60.0, 0.5, "{}"))  # today: both trigger
    conn.commit()

    result = illness_risk_from_db(conn, "2025-06-10")
    conn.close()

    assert result["level"] == "red"
    assert len(result["signals"]) == 2


def test_illness_risk_from_db_downgrades_reverted_single_day_double_trigger():
    """Regression test: RHR and skin temp both crossed threshold on the same
    day (2025-06-09), two days before the query date, but both are back to
    baseline by the most recent day — this must not read as an active red
    state."""
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for d in ["2025-06-01", "2025-06-02", "2025-06-03", "2025-06-04",
              "2025-06-05", "2025-06-06", "2025-06-07"]:
        conn.execute(
            "INSERT INTO daily_metrics(date, resting_hr, hrv, source_flags_json) VALUES (?,?,?,?)",
            (d, 50.0, 60.0, "{}"),
        )
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-08", 50.0, 60.0, 0.0, "{}"))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-09", 54.0, 60.0, 0.5, "{}"))  # both trigger here
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, hrv, skin_temp_deviation, source_flags_json) "
        "VALUES (?,?,?,?,?)", ("2025-06-10", 50.0, 60.0, 0.0, "{}"))  # today: reverted
    conn.commit()

    result = illness_risk_from_db(conn, "2025-06-10")
    conn.close()

    assert result["level"] == "yellow"
    assert result["label"] == "Recent deviation (resolved)"


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


def test_trimp_requires_sufficient_hr_coverage():
    assert trimp_from_hr_samples([150.0] * 9, 30, 50) is None


def test_trimp_increases_with_intensity():
    easy = trimp_from_hr_samples([110.0] * 20, 30, 50)
    hard = trimp_from_hr_samples([170.0] * 20, 30, 50)
    assert easy is not None and hard is not None
    assert hard > easy > 0


def test_alertness_curve_exercise_strain_decays():
    curve = alertness_curve(7.0, hours=8, strain_events=[(100.0, 0.0)])
    assert curve[0]["exercise_strain"] == pytest.approx(10.0)
    assert curve[-1]["exercise_strain"] < curve[0]["exercise_strain"]
    baseline = alertness_curve(7.0, hours=8)
    assert curve[0]["alertness"] < baseline[0]["alertness"]


# ── training_monotony_strain ──────────────────────────────────────────────────

def _insert_activity(conn, date_str, avg_hr=None, duration_s=None, decoupling_pct=None,
                      garmin_activity_id=None):
    conn.execute(
        "INSERT INTO activities(date, type, duration_s, avg_hr, decoupling_pct, garmin_activity_id) "
        "VALUES (?,?,?,?,?,?)",
        (date_str, "running", duration_s, avg_hr, decoupling_pct, garmin_activity_id),
    )


def test_monotony_strain_none_without_hr_data(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    result = training_monotony_strain(conn, "2025-04-07")
    conn.close()
    assert result == {"monotony": None, "strain": None, "band": None, "detail": "insufficient HR data"}


def test_monotony_strain_none_with_no_training(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, max_hr, source_flags_json) VALUES (?,?,?,?)",
        ("2025-04-01", 50.0, 190.0, "{}"),
    )
    conn.commit()
    result = training_monotony_strain(conn, "2025-04-07")
    conn.close()
    assert result == {"monotony": None, "strain": None, "band": None, "detail": "no training in window"}


def test_monotony_strain_elevated_for_identical_daily_load(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, max_hr, source_flags_json) VALUES (?,?,?,?)",
        ("2025-04-01", 50.0, 190.0, "{}"),
    )
    for d in ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04",
              "2025-04-05", "2025-04-06", "2025-04-07"]:
        _insert_activity(conn, d, avg_hr=140, duration_s=1800)
    conn.commit()

    result = training_monotony_strain(conn, "2025-04-07")
    conn.close()
    assert result["monotony"] is None
    assert result["band"] == "elevated"


def test_monotony_strain_normal_for_varied_daily_load(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, max_hr, source_flags_json) VALUES (?,?,?,?)",
        ("2025-04-01", 50.0, 190.0, "{}"),
    )
    # only 3 of 7 days have training -> high day-to-day variance -> low monotony
    for d in ["2025-04-01", "2025-04-03", "2025-04-05"]:
        _insert_activity(conn, d, avg_hr=160, duration_s=3600)
    conn.commit()

    result = training_monotony_strain(conn, "2025-04-07")
    conn.close()
    assert result["monotony"] is not None
    assert result["band"] == "normal"
    assert result["strain"] is not None


# ── decoupling_trend_from_db ──────────────────────────────────────────────────

def test_decoupling_trend_insufficient_runs(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for i, d in enumerate(["2025-04-01", "2025-04-03", "2025-04-05"]):
        _insert_activity(conn, d, decoupling_pct=float(i))
    conn.commit()

    result = decoupling_trend_from_db(conn, "2025-04-07")
    conn.close()
    assert result["direction"] is None
    assert result["detail"] == "fewer than 4 qualifying runs"


def test_decoupling_trend_worsening(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    # older (lower decoupling) then more recent (higher decoupling) runs
    for d, pct in [("2025-04-01", 1.0), ("2025-04-02", 1.5),
                   ("2025-04-03", 2.0), ("2025-04-04", 1.5),
                   ("2025-04-05", 6.0), ("2025-04-06", 6.5),
                   ("2025-04-07", 7.0), ("2025-04-08", 7.5)]:
        _insert_activity(conn, d, decoupling_pct=pct)
    conn.commit()

    result = decoupling_trend_from_db(conn, "2025-04-08")
    conn.close()
    assert result["direction"] == "worsening"
    assert result["recent_avg_pct"] > result["prior_avg_pct"]


def test_decoupling_trend_improving(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for d, pct in [("2025-04-01", 7.0), ("2025-04-02", 6.5),
                   ("2025-04-03", 6.0), ("2025-04-04", 6.5),
                   ("2025-04-05", 1.5), ("2025-04-06", 1.0),
                   ("2025-04-07", 1.5), ("2025-04-08", 1.0)]:
        _insert_activity(conn, d, decoupling_pct=pct)
    conn.commit()

    result = decoupling_trend_from_db(conn, "2025-04-08")
    conn.close()
    assert result["direction"] == "improving"


def test_decoupling_trend_stable_within_noise_floor(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for d in ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04"]:
        _insert_activity(conn, d, decoupling_pct=3.0)
    conn.commit()

    result = decoupling_trend_from_db(conn, "2025-04-04")
    conn.close()
    assert result["direction"] == "stable"


# ── intensity_distribution_from_db ────────────────────────────────────────────

def _insert_activity_hr_zones(conn, date_str, garmin_activity_id, seconds_by_zone):
    _insert_activity(conn, date_str, garmin_activity_id=garmin_activity_id)
    for zone, seconds in seconds_by_zone.items():
        conn.execute(
            "INSERT INTO activity_hr_zones(activity_id, zone, seconds) VALUES (?,?,?)",
            (garmin_activity_id, zone, seconds),
        )


def test_intensity_distribution_no_zone_data(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    result = intensity_distribution_from_db(conn, "2025-04-07")
    conn.close()
    assert result["easy_pct"] is None
    assert result["grey_zone_flag"] is None


def test_intensity_distribution_grey_zone_flag(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    _insert_activity_hr_zones(conn, "2025-04-05", "g1",
                               {1: 300, 2: 300, 3: 3000, 4: 300, 5: 100})
    conn.commit()

    result = intensity_distribution_from_db(conn, "2025-04-07")
    conn.close()
    assert result["grey_zone_flag"] is True
    assert result["moderate_pct"] > 30.0


def test_intensity_distribution_polarized_no_flag(tmp_db):
    conn = sqlite3.connect(str(db_module.DB_PATH))
    _insert_activity_hr_zones(conn, "2025-04-05", "g1",
                               {1: 2000, 2: 2000, 3: 200, 4: 1000, 5: 800})
    conn.commit()

    result = intensity_distribution_from_db(conn, "2025-04-07")
    conn.close()
    assert result["grey_zone_flag"] is False
    assert result["moderate_pct"] < 20.0


def test_compute_illness_risk_respiratory_rate_signal():
    day = _day("2026-01-01", rhr=54.0)  # RHR trigger
    day.update({"resp": 16.5, "resp_baseline": 15.0, "resp_sd": 0.3})
    result = compute_illness_risk([day])
    assert result["level"] == "red"  # concordant with RHR on the same night
    assert any("breathing" in s for s in result["signals"])


def test_illness_baseline_excludes_the_tested_window():
    # 28 normal days, then 3 days of RHR +6: with the window inside its own baseline the
    # elevation would partly mask itself; with it excluded, all 3 days trigger.
    from datetime import date as _d, timedelta as _td
    conn = sqlite3.connect(str(db_module.DB_PATH))
    end = _d(2025, 9, 30)
    for i in range(31):
        d = (end - _td(days=i)).isoformat()
        rhr = 56.0 if i < 3 else (50.0 if i % 2 else 51.0)
        conn.execute("INSERT INTO daily_metrics(date, resting_hr, source_flags_json) VALUES (?,?,?)",
                     (d, rhr, "{}"))
    conn.commit()
    result = illness_risk_from_db(conn, end.isoformat())
    conn.close()
    assert result["level"] == "yellow"
    assert "+6bpm" in result["detail"]


# ── Sleep Regularity Index / chronotype ──────────────────────────────────────

def _intervals(start_hours, dur_h=8.0, first="2025-03-01"):
    """{date: (start, end)}: sleep starting at start_hours[i] UTC — the previous evening
    for hours >= 12, after midnight otherwise."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    base = _dt.fromisoformat(first).replace(tzinfo=_tz.utc)
    out = {}
    for i, h in enumerate(start_hours):
        day = base + _td(days=i)
        start = day - _td(hours=24 - h) if h >= 12 else day + _td(hours=h)
        out[day.date().isoformat()] = (start.timestamp(), (start + _td(hours=dur_h)).timestamp())
    return out


def test_sri_perfect_and_irregular():
    from app.readiness import sleep_regularity_index
    assert sleep_regularity_index(_intervals([23.0] * 8)) == pytest.approx(100.0)
    # Alternating 2h shifts: each pair disagrees for 4h of 24 -> 100 - 200*240/1440
    assert sleep_regularity_index(_intervals([22.0, 0.0] * 4)) == pytest.approx(66.67, abs=0.1)
    assert sleep_regularity_index(_intervals([23.0] * 4)) is None  # too few pairs


def test_sleep_regularity_uses_sri_when_intervals_present():
    result = sleep_regularity([(7.0, 480.0)] * 8, _intervals([22.0, 0.0] * 4))
    assert result["method"] == "sri"
    assert result["score"] == 67


def test_nights_from_db_bedtime_includes_awake_time(tmp_db):
    from app.readiness import _nights_from_db
    conn = sqlite3.connect(str(db_module.DB_PATH))
    now = utc_now()
    upsert_raw_metric(conn, "2025-03-01", "garmin", "sleep_wake_hour", 7.0, now)
    upsert_raw_metric(conn, "2025-03-01", "garmin", "sleep_duration_min", 420.0, now)
    upsert_raw_metric(conn, "2025-03-01", "garmin", "sleep_awake_min", 45.0, now)
    conn.commit()
    night = _nights_from_db(conn, "2025-03-01", 7)["2025-03-01"]
    conn.close()
    assert night["time_in_bed_min"] == 465.0  # Garmin's duration excludes awake time


def test_circadian_peak_follows_weekend_mid_sleep(tmp_db):
    from datetime import date as _d, timedelta as _td
    from app.readiness import circadian_peak_hour_from_db
    conn = sqlite3.connect(str(db_module.DB_PATH))
    now = utc_now()
    end = _d(2025, 3, 30)  # a Sunday
    for i in range(28):
        d = end - _td(days=i)
        wake = 9.0 if d.weekday() >= 5 else 6.5  # late riser on free days
        upsert_raw_metric(conn, d.isoformat(), "garmin", "sleep_wake_hour", wake, now)
        upsert_raw_metric(conn, d.isoformat(), "garmin", "sleep_duration_min", 480.0, now)
    conn.commit()
    peak = circadian_peak_hour_from_db(conn, end.isoformat())
    conn.close()
    # Free-day mid-sleep 05:00 -> peak 16:00 (weekday 02:30 would give 13:30)
    assert peak == pytest.approx(16.0, abs=0.05)


def test_circadian_peak_default_without_data(tmp_db):
    from app.readiness import circadian_peak_hour_from_db
    conn = sqlite3.connect(str(db_module.DB_PATH))
    assert circadian_peak_hour_from_db(conn, "2025-03-30") == 15.0
    conn.close()


def test_alertness_curve_peak_shifts_with_chronotype():
    early = alertness_curve(7.0, hours=16, circadian_peak_hour=13.0)
    late = alertness_curve(7.0, hours=16, circadian_peak_hour=17.0)
    peak = lambda c: max(c, key=lambda r: r["alertness"])["hour"]
    assert peak(late) > peak(early)


# ── three-zone intensity model ───────────────────────────────────────────────

# The real Garmin response for a marathon-pace rehearsal (avg HR 142, LTHR 162)
_REHEARSAL = [(1, 9.0, 95.0), (2, 244.0, 111.0), (3, 1153.0, 127.0), (4, 2487.0, 143.0), (5, 1.0, 161.0)]


def test_three_zone_marathon_pace_is_moderate_not_hard():
    from app.readiness import three_zone_seconds
    easy, moderate, hard, model = three_zone_seconds(_REHEARSAL, 162.0, {})
    assert model == "lthr_3zone"
    # Garmin's 5-zone mapping called ~64% of this run "hard"; below LT2 it's moderate.
    assert hard < 0.01 * (easy + moderate + hard)
    assert moderate > easy


def test_three_zone_falls_back_without_lthr():
    from app.readiness import three_zone_seconds
    easy, moderate, hard, model = three_zone_seconds(_REHEARSAL, None, {})
    assert model == "garmin_5zone"
    assert (easy, moderate, hard) == (253.0, 1153.0, 2488.0)


def test_three_zone_uses_latest_bounds_for_rows_without_them():
    from app.readiness import three_zone_seconds
    rows = [(z, s, None) for z, s, _ in _REHEARSAL]
    bounds = {z: lo for z, _, lo in _REHEARSAL}
    assert three_zone_seconds(rows, 162.0, bounds) == three_zone_seconds(_REHEARSAL, 162.0, {})


def test_three_zone_uses_measured_lt1_when_set():
    from app.readiness import three_zone_seconds
    estimated = three_zone_seconds(_REHEARSAL, 162.0, {})
    measured = three_zone_seconds(_REHEARSAL, 162.0, {}, lt1=135.0)
    assert measured[1] > estimated[1]  # lower LT1 moves more time into moderate
    # An LT1 at/above LTHR is nonsense input: ignored, falls back to the estimate
    assert three_zone_seconds(_REHEARSAL, 162.0, {}, lt1=170.0) == estimated


def test_intensity_distribution_reports_lt1_source(tmp_db):
    from app.readiness import intensity_distribution_from_db
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute("INSERT INTO daily_metrics(date, lactate_threshold_hr) VALUES ('2025-06-30', 162)")
    conn.execute("INSERT INTO activities(date, garmin_activity_id, type, canonical_source) "
                 "VALUES ('2025-06-30', 'a1', 'running', 'garmin')")
    for z, s, lo in _REHEARSAL:
        conn.execute("INSERT INTO activity_hr_zones(activity_id, zone, seconds, low_bpm) VALUES ('a1',?,?,?)", (z, s, lo))
    conn.commit()
    est = intensity_distribution_from_db(conn, "2025-06-30")
    conn.execute("INSERT INTO training_goals(metric, value, updated_at) VALUES ('lt1_hr', 138, 'x')")
    conn.commit()
    measured = intensity_distribution_from_db(conn, "2025-06-30")
    conn.close()
    assert (est["lt1_source"], est["lt1_hr"]) == ("estimated", 144)
    assert (measured["lt1_source"], measured["lt1_hr"]) == ("setting", 138.0)
    assert measured["moderate_pct"] > est["moderate_pct"]


def test_session_kind_from_garmin_labels():
    from app.readiness import session_kind
    assert session_kind("VO2MAX") == "hard"
    assert session_kind("ANAEROBIC_CAPACITY") == "hard"
    assert session_kind("SPRINT") == "hard"
    assert session_kind("LACTATE_THRESHOLD") == "moderate"
    assert session_kind("TEMPO") == "moderate"
    assert session_kind("AEROBIC_BASE") == "easy"
    assert session_kind(None) == "easy"


# Real Garmin zones for a VO2max session (Oct 6): only 2 min in zone 5 by HR
_VO2 = [(1, 28.0, 95.0), (2, 334.0, 111.0), (3, 1143.0, 127.0), (4, 1128.0, 143.0), (5, 120.0, 161.0)]


def test_split_sessions_counts_interval_work_as_hard():
    from app.readiness import split_sessions, three_zone_seconds
    hr_only = three_zone_seconds(_VO2, 162.0, {})
    hybrid = split_sessions([("VO2MAX", _VO2)], 162.0, {})
    assert hr_only[2] < 0.05 * sum(hr_only[:3])           # HR alone: ~nothing hard
    assert hybrid["hard"] == pytest.approx(hr_only[1] + hr_only[2])
    assert hybrid["moderate"] == 0 and hybrid["easy"] == pytest.approx(hr_only[0])
    assert hybrid["sessions"] == {"easy": 0, "moderate": 0, "hard": 1}
    # A labelled-easy or threshold session keeps its HR split
    assert split_sessions([("TEMPO", _REHEARSAL)], 162.0, {})["moderate"] == pytest.approx(
        three_zone_seconds(_REHEARSAL, 162.0, {})[1])


def test_intensity_distribution_uses_session_labels(tmp_db):
    import json as _json
    from app.readiness import intensity_distribution_from_db
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute("INSERT INTO daily_metrics(date, lactate_threshold_hr) VALUES ('2025-06-30', 162)")
    for aid, label, zones in (("v", "VO2MAX", _VO2), ("e", "AEROBIC_BASE", _REHEARSAL)):
        conn.execute("INSERT INTO activities(date, garmin_activity_id, type, canonical_source) "
                     "VALUES ('2025-06-30', ?, 'running', 'garmin')", (aid,))
        conn.execute("INSERT INTO garmin_activities(activity_id, date, raw_json) VALUES (?, '2025-06-30', ?)",
                     (aid, _json.dumps({"trainingEffectLabel": label})))
        for z, s, lo in zones:
            conn.execute("INSERT INTO activity_hr_zones(activity_id, zone, seconds, low_bpm) VALUES (?,?,?,?)",
                         (aid, z, s, lo))
    conn.commit()
    result = intensity_distribution_from_db(conn, "2025-06-30")
    conn.close()
    assert result["sessions"] == {"easy": 1, "moderate": 0, "hard": 1}
    assert result["hard_pct"] > 15  # VO2 session's above-LT1 time, not its 2 min in zone 5


def test_garmin_load_focus_from_stored_payload(tmp_db):
    import json as _json
    from app.readiness import garmin_load_focus_from_db
    payload = {"mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {"123": {
        "primaryTrainingDevice": True,
        "monthlyLoadAerobicLow": 1638.2, "monthlyLoadAerobicHigh": 811.9, "monthlyLoadAnaerobic": 133.8,
        "monthlyLoadAnaerobicTargetMin": 200, "monthlyLoadAnaerobicTargetMax": 500,
        "trainingBalanceFeedbackPhrase": "ANAEROBIC_SHORTAGE"}}}}
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute("INSERT INTO raw_import_payloads(source, endpoint, date, payload_json, fetched_at) "
                 "VALUES ('garmin', 'get_training_status', '2026-10-18', ?, 'x')", (_json.dumps(payload),))
    conn.commit()
    focus = garmin_load_focus_from_db(conn, "2026-10-18")
    stale = garmin_load_focus_from_db(conn, "2026-11-30")
    conn.close()
    assert focus["feedback"] == "ANAEROBIC_SHORTAGE"
    assert focus["share_pct"] == {"low_aerobic": 63, "high_aerobic": 31, "anaerobic": 5}
    assert focus["targets"] == {"anaerobic": {"min": 200, "max": 500}}
    assert stale is None  # older than a week: not current


def test_latest_zone_bounds_merges_across_activities(tmp_db):
    # The most recent activity was easy (no zone 4/5 rows); bounds for 4/5 come from earlier.
    from app.readiness import _latest_zone_bounds
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for aid, zones in (("hard", _REHEARSAL), ("easy", _REHEARSAL[:3])):
        for z, s, lo in zones:
            conn.execute("INSERT INTO activity_hr_zones(activity_id, zone, seconds, low_bpm) VALUES (?,?,?,?)",
                         (aid, z, s, lo))
    conn.commit()
    bounds = _latest_zone_bounds(conn)
    conn.close()
    assert bounds == {1: 95.0, 2: 111.0, 3: 127.0, 4: 143.0, 5: 161.0}


def test_three_zone_fills_missing_own_bounds_from_fallback():
    # Own bounds only for zones 1-3 (zero-second zones weren't stored): fill 4-5, don't drop to 5-zone.
    from app.readiness import three_zone_seconds
    partial = [(z, s, lo if z <= 3 else None) for z, s, lo in _REHEARSAL]
    fallback = {z: lo for z, _, lo in _REHEARSAL}
    assert three_zone_seconds(partial, 162.0, fallback)[3] == "lthr_3zone"
