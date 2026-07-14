"""Tests for app/analysis.py — lag-shifted correlation analysis."""

import sqlite3
from datetime import date, timedelta

import pytest

import app.db as db_module
from app.db import init_db
from app.analysis import compute_correlations, _rank_transform


@pytest.fixture
def tmp_db(tmp_path):
    path = tmp_path / "test.db"
    orig = db_module.DB_PATH
    db_module.DB_PATH = path
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
    db_module.DB_PATH = orig


def _insert_day(conn, d: str, **kwargs):
    cols = ["date"] + list(kwargs.keys())
    vals = [d] + list(kwargs.values())
    placeholders = ", ".join("?" for _ in vals)
    col_sql = ", ".join(cols)
    conn.execute(
        f"INSERT INTO daily_metrics ({col_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT(date) DO UPDATE SET " +
        ", ".join(f"{c}=excluded.{c}" for c in cols[1:]),
        vals,
    )
    conn.commit()


def _seed_alcohol_hrv(conn, n=30, effect=-5):
    """Seed n days where alcohol_units negatively correlates with next-day HRV."""
    # Seed ending at today so the default days= window includes the data
    end = date.today()
    for i in range(n + 1):
        d = (end - timedelta(days=n - i)).isoformat()
        alcohol = 2.0 if i % 2 == 0 else 0.0
        # HRV on day i is influenced by alcohol on day i-1
        prev_alcohol = 2.0 if (i - 1) % 2 == 0 else 0.0
        hrv = 50.0 + effect * prev_alcohol
        _insert_day(conn, d, alcohol_units=alcohol, hrv=hrv)


def test_compute_correlations_basic(tmp_db):
    _seed_alcohol_hrv(tmp_db, n=30, effect=-5)
    result = compute_correlations(
        tmp_db,
        inputs=["alcohol_units"],
        outputs=["hrv"],
        lags=[1],
        days=60,
        min_pairs=10,
    )
    assert result["n_correlations"] >= 1
    corrs = result["correlations"]
    assert len(corrs) >= 1
    assert corrs[0]["input"] == "alcohol_units"
    assert corrs[0]["output"] == "hrv"
    assert corrs[0]["lag_days"] == 1
    assert corrs[0]["r"] < 0, "Alcohol should negatively correlate with next-day HRV"


def test_compute_correlations_min_pairs_filter(tmp_db):
    # Only seed 5 days — below the min_pairs threshold of 14
    base = date(2026, 1, 1)
    for i in range(6):
        d = (base + timedelta(days=i)).isoformat()
        _insert_day(tmp_db, d, alcohol_units=float(i % 3), hrv=50.0 - i)

    result = compute_correlations(
        tmp_db,
        inputs=["alcohol_units"],
        outputs=["hrv"],
        lags=[1],
        days=60,
        min_pairs=14,
    )
    assert result["n_correlations"] == 0


def test_compute_correlations_spearman(tmp_db):
    _seed_alcohol_hrv(tmp_db, n=30, effect=-5)
    result = compute_correlations(
        tmp_db,
        inputs=["alcohol_units"],
        outputs=["hrv"],
        lags=[1],
        days=60,
        min_pairs=10,
        method="spearman",
    )
    assert result["n_correlations"] >= 1
    assert result["correlations"][0]["r"] < 0


def test_compute_correlations_no_data(tmp_db):
    result = compute_correlations(
        tmp_db,
        inputs=["alcohol_units"],
        outputs=["hrv"],
        lags=[0, 1, 2],
        days=90,
        min_pairs=14,
    )
    assert result["n_correlations"] == 0
    assert result["correlations"] == []
    assert result["top_findings"] == []


def test_compute_correlations_lag0(tmp_db):
    # Seed same-day correlation: caffeine → stress
    end = date.today()
    for i in range(30):
        d = (end - timedelta(days=29 - i)).isoformat()
        caffeine = 200.0 if i % 2 == 0 else 50.0
        stress = 40.0 if i % 2 == 0 else 20.0
        _insert_day(tmp_db, d, caffeine_mg=caffeine, stress_avg=stress)

    result = compute_correlations(
        tmp_db,
        inputs=["caffeine_mg"],
        outputs=["stress_avg"],
        lags=[0],
        days=60,
        min_pairs=10,
    )
    assert result["n_correlations"] >= 1
    assert result["correlations"][0]["r"] > 0


def test_rank_transform_no_ties():
    vals = [3.0, 1.0, 2.0]
    ranks = _rank_transform(vals)
    # 1.0 → rank 1, 2.0 → rank 2, 3.0 → rank 3
    assert ranks == [3.0, 1.0, 2.0]


def test_rank_transform_with_ties():
    vals = [1.0, 1.0, 3.0]
    ranks = _rank_transform(vals)
    # ties at 1.0 get average rank (1+2)/2 = 1.5; 3.0 gets rank 3
    assert ranks[0] == 1.5
    assert ranks[1] == 1.5
    assert ranks[2] == 3.0


def test_top_findings_populated(tmp_db):
    _seed_alcohol_hrv(tmp_db, n=40, effect=-8)
    result = compute_correlations(
        tmp_db,
        inputs=["alcohol_units"],
        outputs=["hrv"],
        lags=[1],
        days=60,
        min_pairs=10,
    )
    assert len(result["top_findings"]) >= 1
    assert "alcohol_units" in result["top_findings"][0]
    assert "hrv" in result["top_findings"][0]
    assert "negative" in result["top_findings"][0]
