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
