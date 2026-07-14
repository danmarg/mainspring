import sqlite3
from pathlib import Path

import pytest

from app.db import init_db, upsert_raw_metric, upsert_raw_payload, resolve_metric, utc_now


@pytest.fixture
def tmp_db(tmp_path):
    path = tmp_path / "test.db"
    # point schema loader at project root
    import app.db as db_module
    orig = db_module.DB_PATH
    db_module.DB_PATH = path
    init_db(path)
    yield path
    db_module.DB_PATH = orig


def test_schema_applies(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "raw_import_payloads" in tables
    assert "raw_daily_metrics" in tables
    assert "daily_metrics" in tables
    assert "manual_logs" in tables


def test_upsert_raw_metric(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    now = utc_now()
    upsert_raw_metric(conn, "2025-01-01", "garmin", "hrv", 55.0, now)
    conn.commit()
    row = conn.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND source='garmin' AND metric='hrv'"
    ).fetchone()
    assert row[0] == 55.0

    # upsert updates value
    upsert_raw_metric(conn, "2025-01-01", "garmin", "hrv", 60.0, now)
    conn.commit()
    row = conn.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND source='garmin' AND metric='hrv'"
    ).fetchone()
    assert row[0] == 60.0
    conn.close()


def test_resolve_metric_default_priority(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    now = utc_now()
    upsert_raw_metric(conn, "2025-01-01", "garmin", "hrv", 55.0, now)
    conn.commit()

    value, source = resolve_metric(conn, "2025-01-01", "hrv")
    assert value == 55.0
    assert source == "garmin"
    conn.close()


def test_resolve_metric_missing(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    value, source = resolve_metric(conn, "2025-01-01", "hrv")
    assert value is None
    assert source is None
    conn.close()


def test_upsert_raw_payload(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    upsert_raw_payload(conn, "garmin", "daily_summary", '{"test": 1}', date="2025-01-01")
    conn.commit()
    row = conn.execute("SELECT source, endpoint FROM raw_import_payloads").fetchone()
    assert row[0] == "garmin"
    assert row[1] == "daily_summary"
    conn.close()
