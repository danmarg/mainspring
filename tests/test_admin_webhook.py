"""
Regression test for the morning-webhook double-fire race: garmin and
google_health imports run in separate background threads, and both used to
pass a plain "already sent?" SELECT check before either had committed its
INSERT, firing the webhook twice on mornings both imports landed close
together. The fix claims the date via INSERT OR IGNORE (PRIMARY KEY) before
firing, so only one of two concurrent-ish calls can win.
"""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

import app.db as db_module
from app.db import init_db, get_connection, utc_now
from app.admin_routes import _is_morning_locally, _run_import_bg


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

    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire, \
         patch("app.admin_routes._is_morning_locally", return_value=True):
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

    with patch("app.admin_routes._fire_morning_webhook", return_value=False) as mock_fire, \
         patch("app.admin_routes._is_morning_locally", return_value=True):
        _run_import_bg("garmin", 1, _fake_import_fn(today), {})

    assert mock_fire.call_count == 1
    # Claim was released after the failed send, so a later run can retry.
    row = tmp_db.execute("SELECT COUNT(*) FROM morning_webhooks WHERE date=?", (today,)).fetchone()
    assert row[0] == 0

    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire2, \
         patch("app.admin_routes._is_morning_locally", return_value=True):
        _run_import_bg("google_health", 2, _fake_import_fn(today), {})

    assert mock_fire2.call_count == 1
    row = tmp_db.execute("SELECT COUNT(*) FROM morning_webhooks WHERE date=?", (today,)).fetchone()
    assert row[0] == 1


def test_no_sleep_score_does_not_fire(tmp_db):
    today = date.today().isoformat()
    _insert_run_row(tmp_db, 1, "garmin")

    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire, \
         patch("app.admin_routes._is_morning_locally", return_value=True):
        _run_import_bg("garmin", 1, _fake_import_fn(today), {})

    assert mock_fire.call_count == 0


def test_early_sleep_score_does_not_fire_before_morning(tmp_db):
    """A brief nighttime wake can make Garmin finalize sleep_score at 2am;
    the webhook must not fire until it's actually morning locally, and the
    claim must not be consumed so a later run can still fire it."""
    today = date.today().isoformat()
    _seed_today_sleep_score(tmp_db, today)
    _insert_run_row(tmp_db, 1, "garmin")

    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire, \
         patch("app.admin_routes._is_morning_locally", return_value=False):
        _run_import_bg("garmin", 1, _fake_import_fn(today), {})

    assert mock_fire.call_count == 0
    row = tmp_db.execute("SELECT COUNT(*) FROM morning_webhooks WHERE date=?", (today,)).fetchone()
    assert row[0] == 0

    # A later run, once it's actually morning, fires it.
    _insert_run_row(tmp_db, 2, "garmin")
    with patch("app.admin_routes._fire_morning_webhook", return_value=True) as mock_fire2, \
         patch("app.admin_routes._is_morning_locally", return_value=True):
        _run_import_bg("garmin", 2, _fake_import_fn(today), {})

    assert mock_fire2.call_count == 1
    row = tmp_db.execute("SELECT COUNT(*) FROM morning_webhooks WHERE date=?", (today,)).fetchone()
    assert row[0] == 1


"""
Regression coverage for _is_morning_locally itself: it used to be a pure
clock-time check (fire once local wall-clock passed a fixed hour), which
fires even when the user is still asleep but data happened to sync early
(e.g. a phone-only night with no watch worn). It now gates on the detected
sleep_wake_hour when one has resolved for the day, only falling back to the
clock-time heuristic when no wake_hour is available yet. The clock-time
floor (MORNING_WEBHOOK_EARLIEST_HOUR) still applies as an absolute minimum
either way.
"""


def _set_day_tz(conn, date_str: str, tz: str = "UTC"):
    conn.execute(
        "INSERT OR REPLACE INTO day_timezone(date, tz, source) VALUES (?,?,?)",
        (date_str, tz, "test"),
    )
    conn.commit()


def _seed_wake_hour(conn, date_str: str, wake_hour: float, source: str = "garmin"):
    conn.execute(
        "INSERT INTO raw_daily_metrics(date, source, metric, value, fetched_at) VALUES (?,?,?,?,?)",
        (date_str, source, "sleep_wake_hour", wake_hour, utc_now()),
    )
    conn.commit()


def _frozen_utc(hour: int, minute: int = 0):
    return datetime(2026, 8, 8, hour, minute, tzinfo=timezone.utc)


def test_before_floor_hour_never_fires_even_with_early_wake(tmp_db):
    today = "2026-08-08"
    _set_day_tz(tmp_db, today)
    _seed_wake_hour(tmp_db, today, 4.0)  # detected wake at 4am

    with patch("app.admin_routes.datetime") as mock_dt:
        mock_dt.now.return_value = _frozen_utc(4, 30)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert _is_morning_locally(tmp_db, today) is False


def test_waits_for_detected_wake_hour_past_floor(tmp_db):
    today = "2026-08-08"
    _set_day_tz(tmp_db, today)
    _seed_wake_hour(tmp_db, today, 8.0)  # user actually woke at 8am

    with patch("app.admin_routes.datetime") as mock_dt:
        mock_dt.now.return_value = _frozen_utc(6, 0)  # past the 5am floor, before wake
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert _is_morning_locally(tmp_db, today) is False

    with patch("app.admin_routes.datetime") as mock_dt:
        mock_dt.now.return_value = _frozen_utc(8, 0)  # at detected wake time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert _is_morning_locally(tmp_db, today) is True


def test_no_detected_wake_hour_falls_back_to_floor(tmp_db):
    today = "2026-08-08"
    _set_day_tz(tmp_db, today)
    # No sleep_wake_hour row seeded at all.

    with patch("app.admin_routes.datetime") as mock_dt:
        mock_dt.now.return_value = _frozen_utc(4, 30)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert _is_morning_locally(tmp_db, today) is False

    with patch("app.admin_routes.datetime") as mock_dt:
        mock_dt.now.return_value = _frozen_utc(6, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert _is_morning_locally(tmp_db, today) is True
