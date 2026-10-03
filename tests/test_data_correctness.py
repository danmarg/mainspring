"""Regression tests for the timezone / start-time / HRV / TRIMP / raw-payload fixes."""

import json
import sqlite3

import pytest

import app.db as db_module
from app.db import (
    _migrate_garmin_start_times, get_connection, init_db, local_health_date,
    parse_instant, upsert_raw_payload, utc_now,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    conn = get_connection(path)
    yield conn
    conn.close()


def _set_tz(conn, date, tz):
    conn.execute("INSERT OR REPLACE INTO day_timezone(date, tz, source) VALUES (?,?,'test')", (date, tz))
    conn.commit()


# ── health date from the day's timezone ──────────────────────────────────────

def test_local_health_date_uses_day_timezone(tmp_db):
    _set_tz(tmp_db, "2025-06-01", "America/New_York")
    # 21:30 in New York is 01:30 UTC the next day
    assert local_health_date(tmp_db, "2025-06-02T01:30:00+00:00") == "2025-06-01"
    assert local_health_date(tmp_db, "2025-06-02T01:30:00Z") == "2025-06-01"


def test_local_health_date_falls_back_to_home_tz(tmp_db, monkeypatch):
    monkeypatch.setattr(db_module, "HOME_TZ", "Europe/Berlin")
    assert local_health_date(tmp_db, "2025-06-01T22:30:00+00:00") == "2025-06-02"


def test_local_health_date_handles_fixed_offset_zone_names(tmp_db):
    _set_tz(tmp_db, "2025-06-02", "UTC+05:30")
    assert local_health_date(tmp_db, "2025-06-01T20:00:00+00:00") == "2025-06-02"


def test_parse_instant_accepts_nanoseconds_offsets_and_naive():
    assert parse_instant("2025-06-01T10:00:00.123456789Z").hour == 10
    assert parse_instant("2025-06-01T10:00:00+02:00").hour == 8
    assert parse_instant("2025-06-01 10:00:00").hour == 10


# ── daily_metrics buckets instants by the day timezone ───────────────────────

def _metrics(conn, date):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM daily_metrics WHERE date=?", (date,)).fetchone()


def test_evening_drink_lands_on_local_day_not_utc_day(tmp_db):
    from app.mcp_server import log_alcohol
    _set_tz(tmp_db, "2025-06-01", "America/New_York")

    log_alcohol(description="beer", ts="2025-06-02T01:00:00+00:00", units=2.0)  # 21:00 local

    assert _metrics(tmp_db, "2025-06-01")["alcohol_units"] == 2.0
    assert _metrics(tmp_db, "2025-06-02") is None or _metrics(tmp_db, "2025-06-02")["alcohol_units"] is None


def test_rpe_stays_on_its_stated_date(tmp_db, monkeypatch):
    """rpe stores '<date>T23:59:00+00:00' — a date literal, not an instant. It must
    not be shifted into the next local day in zones east of UTC."""
    from app.mcp_server import log_rpe
    monkeypatch.setattr(db_module, "HOME_TZ", "Europe/Berlin")

    log_rpe(rpe=7, date="2025-06-01")

    assert _metrics(tmp_db, "2025-06-01")["rpe"] == 7.0


def test_full_rebuild_discovers_local_dates(tmp_db):
    from app.normalize import rebuild_daily_metrics
    _set_tz(tmp_db, "2025-06-01", "America/New_York")
    tmp_db.execute(
        "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) "
        "VALUES ('2025-06-02T01:00:00+00:00','caffeine','coffee',95,'mg',?)", (utc_now(),),
    )
    tmp_db.commit()
    rebuild_daily_metrics(tmp_db)
    assert _metrics(tmp_db, "2025-06-01")["caffeine_mg"] == 95.0


# ── every log tool refreshes daily_metrics; amend moves between days ─────────

def test_meal_and_caffeine_refresh_metrics_immediately(tmp_db):
    from app.mcp_server import log_caffeine, log_meal
    log_meal(description="oats", estimated_calories=300, ts="2025-06-01T08:00:00+00:00")
    log_caffeine(description="coffee", amount_mg=80, ts="2025-06-01T08:05:00+00:00")
    row = _metrics(tmp_db, "2025-06-01")
    assert row["calories_estimated"] == 300 and row["caffeine_mg"] == 80


def test_amend_log_refreshes_old_and_new_day(tmp_db):
    from app.mcp_server import amend_log, log_alcohol
    _set_tz(tmp_db, "2025-06-01", "UTC")
    _set_tz(tmp_db, "2025-06-02", "UTC")
    log_alcohol(description="wine", ts="2025-06-01T20:00:00+00:00", units=3.0)
    log_id = tmp_db.execute("SELECT id FROM manual_logs").fetchone()[0]

    amend_log(log_id=log_id, ts="2025-06-02T20:00:00+00:00")

    old = _metrics(tmp_db, "2025-06-01")
    assert old is None or old["alcohol_units"] is None
    assert _metrics(tmp_db, "2025-06-02")["alcohol_units"] == 3.0

    amend_log(log_id=log_id, quantity=5.0)
    assert _metrics(tmp_db, "2025-06-02")["alcohol_units"] == 5.0


# ── Garmin start_time is UTC; migration; cross-source dedupe ─────────────────

def _garmin_activity(**over):
    a = {
        "activityId": "42", "startTimeLocal": "2025-06-01 20:30:00", "startTimeGMT": "2025-06-01 18:30:00",
        "activityType": {"typeKey": "running"}, "duration": 3600,
    }
    a.update(over)
    return a


def test_garmin_start_time_is_utc_and_date_is_local(tmp_db):
    from app.importers.garmin import _upsert_activity
    _upsert_activity(tmp_db, _garmin_activity(startTimeLocal="2025-06-02 00:30:00", startTimeGMT="2025-06-01 22:30:00"))
    start, date = tmp_db.execute("SELECT start_time, date FROM garmin_activities").fetchone()
    assert start == "2025-06-01T22:30:00+00:00"
    assert date == "2025-06-02"  # local calendar day
    assert parse_instant(start).hour == 22


def test_migration_rewrites_legacy_local_start_times(tmp_db):
    from app.importers.garmin import _upsert_activity
    _upsert_activity(tmp_db, _garmin_activity())
    tmp_db.execute("UPDATE garmin_activities SET start_time='2025-06-01T20:30:00'")  # legacy naive-local
    tmp_db.commit()

    _migrate_garmin_start_times(tmp_db)
    _migrate_garmin_start_times(tmp_db)  # idempotent

    assert tmp_db.execute("SELECT start_time FROM garmin_activities").fetchone()[0] == "2025-06-01T18:30:00+00:00"


def test_dedupe_matches_garmin_utc_with_google_nanosecond_utc():
    from app.normalize import _find_match
    garmin_start = "2025-06-01T18:30:00+00:00"
    gh_rows = [("gh1", "2025-06-01", "2025-06-01T18:36:00.123456789Z", "running")]
    assert _find_match("2025-06-01", "running", garmin_start, gh_rows) == "gh1"
    far = [("gh2", "2025-06-01", "2025-06-01T20:30:00Z", "running")]  # the old local/UTC confusion
    assert _find_match("2025-06-01", "running", garmin_start, far) is None


# ── HRV: no weekly-average fallback ──────────────────────────────────────────

def test_parse_hrv_ignores_weekly_avg_when_last_night_missing(tmp_db):
    from app.importers.garmin import _parse_hrv
    assert _parse_hrv(tmp_db, "2025-06-01", {"hrvSummary": {"lastNight": None, "weeklyAvg": 52}}) == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM raw_daily_metrics WHERE metric='hrv'").fetchone()[0] == 0
    assert _parse_hrv(tmp_db, "2025-06-01", {"hrvSummary": {"lastNight": 47, "weeklyAvg": 52}}) == 1


# ── TRIMP ceiling ────────────────────────────────────────────────────────────

def test_hr_ceiling_is_rolling_peak_not_latest_day(tmp_db):
    from app.readiness import _hr_ceiling
    tmp_db.execute("INSERT INTO daily_metrics(date, max_hr) VALUES ('2025-05-01', 188)")
    tmp_db.execute("INSERT INTO daily_metrics(date, max_hr) VALUES ('2025-06-01', 108)")  # easy day
    tmp_db.commit()
    assert _hr_ceiling(tmp_db, "2025-06-01") == 188.0


def test_hr_ceiling_falls_back_when_no_credible_peak(tmp_db):
    from app.readiness import _hr_ceiling
    assert _hr_ceiling(tmp_db, "2025-06-01") == 190.0
    tmp_db.execute("INSERT INTO daily_metrics(date, max_hr) VALUES ('2025-06-01', 108)")
    tmp_db.commit()
    assert _hr_ceiling(tmp_db, "2025-06-01") == 190.0


# ── raw payload dedupe for multi-item endpoints ──────────────────────────────

def test_activity_payloads_dont_reinsert_when_alternating(tmp_db):
    a, b = json.dumps({"activityId": 1}), json.dumps({"activityId": 2})
    for _ in range(4):  # four hourly imports re-fetching the same two activities
        upsert_raw_payload(tmp_db, "garmin", "activity", a, "2025-06-01")
        upsert_raw_payload(tmp_db, "garmin", "activity", b, "2025-06-01")
    assert tmp_db.execute("SELECT COUNT(*) FROM raw_import_payloads").fetchone()[0] == 2


def test_single_item_endpoint_still_records_a_revert(tmp_db):
    """A->B->A on a one-payload-per-day endpoint must still end with A as latest
    (normalization reads the newest row), so those keep latest-only dedupe."""
    for payload in ('{"v":1}', '{"v":2}', '{"v":1}'):
        upsert_raw_payload(tmp_db, "garmin", "get_stats", payload, "2025-06-01")
    latest = tmp_db.execute("SELECT payload_json FROM raw_import_payloads ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert latest == '{"v":1}'


def test_parse_hrv_reads_real_last_night_avg_field(tmp_db):
    """python-garminconnect's HRV payload names the nightly figure lastNightAvg."""
    from app.importers.garmin import _parse_hrv
    assert _parse_hrv(tmp_db, "2025-06-01", {"hrvSummary": {"lastNightAvg": 44, "weeklyAvg": 60}}) == 1
    assert tmp_db.execute("SELECT value FROM raw_daily_metrics WHERE metric='hrv'").fetchone()[0] == 44.0
    # weekly average alone must still not be stored as tonight's HRV
    assert _parse_hrv(tmp_db, "2025-06-02", {"hrvSummary": {"lastNightAvg": None, "weeklyAvg": 60}}) == 0


def test_hr_ceiling_ignores_a_single_sensor_spike(tmp_db):
    from app.readiness import _hr_ceiling
    for i in range(40):
        tmp_db.execute("INSERT INTO daily_metrics(date, max_hr) VALUES (date('2025-06-01', ?), ?)", (f"-{i} days", 170))
    tmp_db.execute("INSERT INTO daily_metrics(date, max_hr) VALUES ('2025-04-15', 225)")  # optical glitch
    tmp_db.commit()
    assert _hr_ceiling(tmp_db, "2025-06-01") == 170.0


def test_dateless_payloads_dedupe_against_any_stored_copy(tmp_db):
    """Per-activity splits/zones are stored with date NULL — alternating A,B must not re-insert."""
    for _ in range(3):
        upsert_raw_payload(tmp_db, "garmin", "get_activity_splits", '{"a":1}', None)
        upsert_raw_payload(tmp_db, "garmin", "get_activity_splits", '{"b":2}', None)
    assert tmp_db.execute("SELECT COUNT(*) FROM raw_import_payloads").fetchone()[0] == 2


def test_migration_refreshes_normalized_activities(tmp_db):
    from app.importers.garmin import _upsert_activity
    from app.normalize import rebuild_activities
    _upsert_activity(tmp_db, _garmin_activity())
    tmp_db.execute("UPDATE garmin_activities SET start_time='2025-06-01T20:30:00'")
    tmp_db.commit()
    rebuild_activities(tmp_db)
    tmp_db.commit()
    assert tmp_db.execute("SELECT start_time FROM activities").fetchone()[0] == "2025-06-01T20:30:00"

    _migrate_garmin_start_times(tmp_db)

    assert tmp_db.execute("SELECT start_time FROM activities").fetchone()[0] == "2025-06-01T18:30:00+00:00"
