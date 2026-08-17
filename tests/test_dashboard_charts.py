"""
T8 — Chart builder smoke tests.
Verifies that each chart builder:
  - returns "{}" for empty input
  - returns valid JSON for minimal valid input
"""

import json

import pytest

from app.dashboard import (
    _body_battery_chart,
    _bp_chart,
    _daily_bar_chart,
    _diverging_bar_chart,
    _dow_avg_chart,
    _polarization_chart,
    _running_economy_chart,
    _relationship_chart,
    _sleep_chart,
    _sparse_line_chart,
    _trend_chart,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _is_valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


# ── _trend_chart ──────────────────────────────────────────────────────────────

def test_trend_chart_empty():
    assert _trend_chart([], "hrv", "hrv_7d_avg", "HRV") == "{}"


def test_trend_chart_valid():
    rows = [{"date": "2025-01-01", "hrv": 55, "hrv_7d_avg": 52}]
    result = _trend_chart(rows, "hrv", "hrv_7d_avg", "HRV")
    assert _is_valid_json(result)
    assert result != "{}"



def test_relationship_chart_requires_three_pairs():
    spec, stats = _relationship_chart([{"input": 1.0, "output": 2.0}], "Input", "Outcome")
    assert spec == "{}"
    assert stats["n"] == 1


def test_relationship_chart_valid_with_correlation():
    rows = [
        {"input": 1.0, "output": 2.0, "date": "2025-01-01", "outcome_date": "2025-01-02"},
        {"input": 2.0, "output": 4.0, "date": "2025-01-02", "outcome_date": "2025-01-03"},
        {"input": 3.0, "output": 6.0, "date": "2025-01-03", "outcome_date": "2025-01-04"},
    ]
    spec, stats = _relationship_chart(rows, "Input", "Outcome")
    assert _is_valid_json(spec)
    assert stats["r"] == 1.0

# ── _sleep_chart ──────────────────────────────────────────────────────────────

def test_sleep_chart_empty():
    assert _sleep_chart([], []) == "{}"


def test_sleep_chart_score_only():
    score_rows = [{"date": "2025-01-01", "sleep_score": 80}]
    result = _sleep_chart(score_rows, [])
    assert _is_valid_json(result)
    assert result != "{}"


def test_sleep_chart_score_and_stages():
    score_rows = [{"date": "2025-01-01", "sleep_score": 80}]
    stage_rows = [
        {"date": "2025-01-01", "stage": "Deep", "minutes": 45},
        {"date": "2025-01-01", "stage": "REM", "minutes": 60},
        {"date": "2025-01-01", "stage": "Light", "minutes": 120},
    ]
    result = _sleep_chart(score_rows, stage_rows)
    assert _is_valid_json(result)
    assert result != "{}"


# ── _diverging_bar_chart ──────────────────────────────────────────────────────

def test_diverging_bar_chart_empty():
    assert _diverging_bar_chart([], "hrv_delta", "HRV delta") == "{}"


def test_diverging_bar_chart_valid():
    rows = [{"date": "2025-01-01", "hrv_delta": 5.0}]
    result = _diverging_bar_chart(rows, "hrv_delta", "HRV delta")
    assert _is_valid_json(result)
    assert result != "{}"


def test_diverging_bar_chart_negative():
    rows = [{"date": "2025-01-01", "hrv_delta": -3.5}]
    result = _diverging_bar_chart(rows, "hrv_delta", "HRV delta")
    assert _is_valid_json(result)
    assert result != "{}"


def test_diverging_bar_chart_invert_color_valid():
    rows = [{"date": "2025-01-01", "decoupling_pct": 6.0}]
    result = _diverging_bar_chart(rows, "decoupling_pct", "Aerobic decoupling (%)", invert_color=True)
    assert _is_valid_json(result)
    assert result != "{}"


# ── _daily_bar_chart ──────────────────────────────────────────────────────────

def test_daily_bar_chart_empty():
    assert _daily_bar_chart([], "alcohol_units", "Alcohol (units)") == "{}"


def test_daily_bar_chart_valid():
    rows = [{"date": "2025-01-01", "alcohol_units": 1.0}]
    result = _daily_bar_chart(rows, "alcohol_units", "Alcohol (units)")
    assert _is_valid_json(result)
    assert result != "{}"


# ── _dow_avg_chart ────────────────────────────────────────────────────────────

def test_dow_avg_chart_empty():
    assert _dow_avg_chart([], "Avg alcohol", "#e05c5c") == "{}"


def test_dow_avg_chart_valid():
    rows = [{"dow_name": "Mon", "avg_val": 1.5}]
    result = _dow_avg_chart(rows, "Avg alcohol", "#e05c5c")
    assert _is_valid_json(result)
    assert result != "{}"


# ── _body_battery_chart ───────────────────────────────────────────────────────

def test_body_battery_chart_empty():
    assert _body_battery_chart([]) == "{}"


def test_body_battery_chart_valid():
    rows = [{"date": "2025-01-01", "body_battery_high": 80, "body_battery_low": 20}]
    result = _body_battery_chart(rows)
    assert _is_valid_json(result)
    assert result != "{}"


# ── _sparse_line_chart ────────────────────────────────────────────────────────

def test_sparse_line_chart_empty():
    assert _sparse_line_chart([], "weight_kg", "Weight (kg)") == "{}"


def test_sparse_line_chart_valid():
    rows = [{"date": "2025-01-01", "weight_kg": 82.5}]
    result = _sparse_line_chart(rows, "weight_kg", "Weight (kg)")
    assert _is_valid_json(result)
    assert result != "{}"


# ── _bp_chart ─────────────────────────────────────────────────────────────────

def test_bp_chart_empty():
    assert _bp_chart([]) == "{}"


def test_bp_chart_no_data_in_rows():
    # Rows with no bp fields → internal long-form is empty → returns "{}"
    rows = [{"date": "2025-01-01"}]
    assert _bp_chart(rows) == "{}"


def test_bp_chart_valid():
    rows = [{"date": "2025-01-01", "bp_systolic": 120, "bp_diastolic": 78}]
    result = _bp_chart(rows)
    assert _is_valid_json(result)
    assert result != "{}"


# ── _running_economy_chart ────────────────────────────────────────────────────

def test_running_economy_chart_empty():
    assert _running_economy_chart([]) == "{}"


def test_running_economy_chart_too_few_rows():
    rows = [
        {"date": "2025-01-01", "pace_min_km": 5.5, "avg_hr": 140},
        {"date": "2025-01-02", "pace_min_km": 5.4, "avg_hr": 138},
    ]
    assert _running_economy_chart(rows) == "{}"


def test_running_economy_chart_valid():
    rows = [
        {"date": "2025-01-01", "pace_min_km": 5.5, "avg_hr": 145},
        {"date": "2025-01-08", "pace_min_km": 5.45, "avg_hr": 142},
        {"date": "2025-01-15", "pace_min_km": 5.5, "avg_hr": 140},
        {"date": "2025-01-22", "pace_min_km": 5.48, "avg_hr": 138},
    ]
    result = _running_economy_chart(rows)
    assert _is_valid_json(result)
    assert result != "{}"


def test_running_economy_chart_band_excludes_outlier_paces():
    # One run far outside the pace band should be excluded, dropping below
    # the 3-row minimum even though 4 rows were passed in.
    rows = [
        {"date": "2025-01-01", "pace_min_km": 5.5, "avg_hr": 145},
        {"date": "2025-01-08", "pace_min_km": 5.5, "avg_hr": 142},
        {"date": "2025-01-15", "pace_min_km": 9.0, "avg_hr": 160},
    ]
    assert _running_economy_chart(rows) == "{}"


# ── _polarization_chart ────────────────────────────────────────────────────────

def test_polarization_chart_empty():
    assert _polarization_chart([]) == "{}"


def test_polarization_chart_valid():
    rows = [
        {"week": "2025-01-06", "easy_pct": 70.0, "moderate_pct": 10.0, "hard_pct": 20.0},
        {"week": "2025-01-13", "easy_pct": 50.0, "moderate_pct": 40.0, "hard_pct": 10.0},
    ]
    result = _polarization_chart(rows)
    assert _is_valid_json(result)
    assert result != "{}"
