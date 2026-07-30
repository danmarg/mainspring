"""
Regression test for the morning-webhook double-fire race: garmin and
google_health imports run in separate background threads, and both used to
pass a plain "already sent?" SELECT check before either had committed its
INSERT, firing the webhook twice on mornings both imports landed close
together. The fix claims the date via INSERT OR IGNORE (PRIMARY KEY) before
firing, so only one of two concurrent-ish calls can win.
"""

from datetime import date
from unittest.mock import patch

import pytest

import app.db as db_module
from app.db import init_db, get_connection, utc_now
from app.admin_routes import _run_import_bg


@pytest.fixture
def tmp_db(tmp_path):
    path = tmp_path / "test.db"
    orig = db_module.DB_PATH
    db_module.DB_PATH = path
    init_db(path)
    conn = get_connection(path)
    yield conn
    conn.close()
    db_module.DB_PATH = orig


def _seed_today_sleep_score(conn, today: str):
    conn.execute(
        "INSERT INTO raw_daily_metrics(date, source, metric, value, fetched_at) VALUES (?,?,?,?,?)",
        (today, "garmin", "sleep_score", 80.0, utc_now()),
    )
    conn.commit()


def _insert_run_row(conn, run_id: int, source: str):
    conn.execute(
        "INSERT INTO import_runs(id, source, started_at, status) VALUES (?,?,?,?)",
        (run_id, source, utc_now(), "running"),
    )
    conn.commit()


def _fake_import_fn(today: str):
    def fn(conn, **kwargs):
        return {"skipped": False, "rows_upserted": 1, "dates": [today]}
    return fn


def test_two_concurrent_imports_fire_webhook_only_once(tmp_db):
    today = date.today().isoformat()
    _seed_today_sleep_score(tmp_db, today)
    _insert_run_row(tmp_db, 1, "garmin")
    _insert_run_row(tmp_db, 2, "google_health")

    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire:
        # Simulates garmin and google_health import threads both reaching the
        # webhook-claim step after sleep_score has landed.
        _run_import_bg("garmin", 1, _fake_import_fn(today), {})
        _run_import_bg("google_health", 2, _fake_import_fn(today), {})

    assert mock_fire.call_count == 1
    rows = tmp_db.execute("SELECT COUNT(*) FROM morning_webhooks WHERE date=?", (today,)).fetchone()
    assert rows[0] == 1


def test_webhook_failure_releases_claim_for_retry(tmp_db):
    today = date.today().isoformat()
    _seed_today_sleep_score(tmp_db, today)
    _insert_run_row(tmp_db, 1, "garmin")
    _insert_run_row(tmp_db, 2, "google_health")

    with patch("app.admin_routes._fire_morning_webhook", return_value=False) as mock_fire:
        _run_import_bg("garmin", 1, _fake_import_fn(today), {})

    assert mock_fire.call_count == 1
    # Claim was released after the failed send, so a later run can retry.
    row = tmp_db.execute("SELECT COUNT(*) FROM morning_webhooks WHERE date=?", (today,)).fetchone()
    assert row[0] == 0

    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire2:
        _run_import_bg("google_health", 2, _fake_import_fn(today), {})

    assert mock_fire2.call_count == 1
    row = tmp_db.execute("SELECT COUNT(*) FROM morning_webhooks WHERE date=?", (today,)).fetchone()
    assert row[0] == 1


def test_no_sleep_score_does_not_fire(tmp_db):
    today = date.today().isoformat()
    _insert_run_row(tmp_db, 1, "garmin")

    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire:
        _run_import_bg("garmin", 1, _fake_import_fn(today), {})

    assert mock_fire.call_count == 0
