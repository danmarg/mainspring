"""Raw-payload compaction, DB stats, guarded VACUUM, and the maintenance endpoints."""

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db_module
import app.normalize as norm
from app import admin_routes, maintenance
from app.db import get_connection, init_db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    conn = get_connection(path)
    norm._compaction_state["last_finished"] = 0.0
    yield conn
    conn.close()


def day(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def put(conn, endpoint, date, payload="{}", source="garmin", age_days=None):
    age = age_days if age_days is not None else (0 if date is None else (datetime.now(timezone.utc).date() - datetime.fromisoformat(date).date()).days)
    conn.execute(
        "INSERT INTO raw_import_payloads(source, endpoint, date, payload_json, fetched_at) VALUES (?,?,?,?,?)",
        (source, endpoint, date, payload, (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()),
    )
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM raw_import_payloads").fetchone()[0]


def ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM raw_import_payloads ORDER BY id")]


# ── compaction ───────────────────────────────────────────────────────────────

def test_keeps_only_the_newest_version_per_settled_day(tmp_db):
    old = day(10)
    v1 = put(tmp_db, "get_heart_rates", old, '{"v":1}')
    v2 = put(tmp_db, "get_heart_rates", old, '{"v":2}')
    v3 = put(tmp_db, "get_heart_rates", old, '{"v":3}')
    other_endpoint = put(tmp_db, "get_stress_data", old, '{"s":1}')
    other_day = put(tmp_db, "get_heart_rates", day(11), '{"v":1}')
    other_source = put(tmp_db, "get_heart_rates", old, '{"v":9}', source="google_health")

    deleted, finished = norm.compact_raw_payloads(tmp_db)

    assert (deleted, finished) == (2, True)
    assert ids(tmp_db) == sorted([v3, other_endpoint, other_day, other_source])
    assert v1 not in ids(tmp_db) and v2 not in ids(tmp_db)


def test_recent_days_are_untouched(tmp_db):
    kept = [put(tmp_db, "get_heart_rates", day(d), f'{{"v":{i}}}') for d in (0, 1, 2) for i in range(3)]
    assert norm.compact_raw_payloads(tmp_db) == (0, True)  # today, yesterday and 2 days ago still change
    assert ids(tmp_db) == sorted(kept)
    put(tmp_db, "get_heart_rates", day(3), '{"v":1}')
    put(tmp_db, "get_heart_rates", day(3), '{"v":2}')
    assert norm.compact_raw_payloads(tmp_db)[0] == 1  # day 3 is settled


def test_multi_item_endpoints_are_never_compacted(tmp_db):
    kept = [put(tmp_db, ep, day(10), f'{{"id":{i}}}') for ep in ("scheduled_workout",) for i in range(3)]
    kept += [put(tmp_db, ep, None, f'{{"id":{i}}}') for ep in ("get_activity_splits", "get_activity_hr_in_timezones") for i in range(3)]
    assert norm.compact_raw_payloads(tmp_db) == (0, True)
    assert ids(tmp_db) == sorted(kept)


def test_activity_payloads_keep_the_newest_copy_per_activity_id(tmp_db):
    import json
    old = day(10)
    a_old = put(tmp_db, "activity", old, json.dumps({"activityId": 111, "name": "v1"}))
    b = put(tmp_db, "activity", old, json.dumps({"activityId": 222}))
    a_new = put(tmp_db, "activity", old, json.dumps({"activityId": 111, "name": "v2"}))
    a_dup = put(tmp_db, "activity", old, json.dumps({"activityId": 111, "name": "v2"}))
    other_source = put(tmp_db, "activity", old, json.dumps({"activityId": 111}), source="google_health")
    recent_a = put(tmp_db, "activity", day(0), json.dumps({"activityId": 333, "n": 1}))
    recent_b = put(tmp_db, "activity", day(0), json.dumps({"activityId": 333, "n": 2}))
    no_id = put(tmp_db, "activity", old, json.dumps({"something": "else"}))

    deleted, finished = norm.compact_raw_payloads(tmp_db)

    assert finished and deleted == 2  # a_old and a_new are superseded by a_dup
    assert ids(tmp_db) == sorted([b, a_dup, other_source, recent_a, recent_b, no_id])
    assert a_old not in ids(tmp_db) and a_new not in ids(tmp_db)


def test_activity_compaction_pages_through_many_rows(tmp_db):
    import json
    for i in range(30):
        put(tmp_db, "activity", day(10), json.dumps({"activityId": i % 3, "v": i}))
    assert norm.compact_raw_payloads(tmp_db) == (27, True)
    assert len(ids(tmp_db)) == 3


def test_dateless_range_payloads_keep_only_the_newest_old_one(tmp_db):
    # rows are append-only, so fetched_at rises with id: seed oldest-first
    ftp = [put(tmp_db, "get_cycling_ftp", day(10), f'{{"w":{i}}}') for i in range(2)]  # dated: handled by key, not here
    old = [put(tmp_db, "get_body_battery", None, f'{{"v":{i}}}', age_days=10) for i in range(4)]
    recent = put(tmp_db, "get_body_battery", None, '{"v":"now"}', age_days=0)
    deleted, _ = norm.compact_raw_payloads(tmp_db)
    left = ids(tmp_db)
    assert recent in left and old[-1] in left
    assert not set(old[:-1]) & set(left)
    assert deleted == 3 + 1  # three old body-battery copies + one superseded ftp


def test_is_idempotent_and_budget_limited_runs_resume(tmp_db, monkeypatch):
    monkeypatch.setattr(norm, "COMPACT_BATCH_ROWS", 2)
    for i in range(7):
        put(tmp_db, "get_sleep_data", day(10), f'{{"v":{i}}}')
    assert norm.compact_raw_payloads(tmp_db, time_budget_s=0.0) == (0, False)  # budget spent before any batch
    assert norm.compact_raw_payloads(tmp_db) == (6, True)
    assert norm.compact_raw_payloads(tmp_db) == (0, True)
    assert len(ids(tmp_db)) == 1


def test_scan_never_reads_payload_or_fetched_at_columns(tmp_db):
    put(tmp_db, "get_heart_rates", day(10), "x" * 50_000)
    statements = []
    tmp_db.set_trace_callback(statements.append)
    norm.compact_raw_payloads(tmp_db)
    # the activity pass deliberately reads its (small) payloads to get activityId; every other scan must not
    scans = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "FROM raw_import_payloads" in s
             and "endpoint='activity'" not in s]
    assert scans
    # id/source/endpoint/date sit before payload_json in the row, so reading just them avoids
    # walking every payload's overflow pages; only the rowid-lookup helper touches fetched_at
    assert not [s for s in scans if "payload_json" in s]
    assert not [s for s in scans if "fetched_at" in s and "id >= ?" not in s and "id>=" not in s.replace(" ", "")]


def test_day_timezone_still_resolves_from_the_surviving_payload(tmp_db):
    import json
    d = day(10)
    put(tmp_db, "get_sleep_data", d, json.dumps({"dailySleepDTO": {"timezoneOffset": 3600}}))
    put(tmp_db, "get_sleep_data", d, json.dumps({"dailySleepDTO": {"timezoneOffset": 7200}}))
    norm.compact_raw_payloads(tmp_db)
    tz, _ = norm._resolve_tz_for_date(tmp_db, d)
    assert tz == "Etc/GMT-2"  # the newest version's offset


def test_maybe_compact_waits_between_passes_but_continues_when_unfinished(tmp_db, monkeypatch):
    calls = []
    results = iter([(5, False), (3, True), (0, True)])
    monkeypatch.setattr(norm, "compact_raw_payloads", lambda conn, **k: calls.append(1) or next(results))
    norm.maybe_compact_raw_payloads(tmp_db)   # unfinished → no cooldown
    norm.maybe_compact_raw_payloads(tmp_db)   # finishes → cooldown starts
    norm.maybe_compact_raw_payloads(tmp_db)   # within cooldown → skipped
    assert len(calls) == 2


def test_normalization_itself_does_not_compact(tmp_db, monkeypatch):
    """Compaction must not run inside the normalization lock/transaction."""
    calls = []
    monkeypatch.setattr(norm, "maybe_compact_raw_payloads", lambda conn: calls.append(1) or 0)
    norm.run_normalization(tmp_db, {day(0)})
    assert calls == []


def _finish_import(monkeypatch, source="garmin"):
    monkeypatch.setattr(admin_routes, "_fire_morning_webhook", lambda: True)
    run = admin_routes._start_import(source, __import__("fastapi").BackgroundTasks(), lambda *a, **k: {}, 7, None, None)
    admin_routes._run_import_bg(source, run["run_id"], lambda conn, **kw: {"skipped": False, "rows_upserted": 0, "dates": []}, {})
    return run


def test_import_triggers_compaction_after_it_is_recorded_and_outside_the_normalization_lock(tmp_db, monkeypatch):
    seen = {}

    def fake_compact(conn):
        seen["normalization_lock_held"] = admin_routes._normalization_lock.locked()
        seen["run_status"] = conn.execute("SELECT status FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        return 0

    monkeypatch.setattr(norm, "maybe_compact_raw_payloads", fake_compact)
    admin_routes._running_imports.clear()
    _finish_import(monkeypatch)
    assert seen == {"normalization_lock_held": False, "run_status": "ok"}


def test_compaction_failure_never_fails_the_import(tmp_db, monkeypatch):
    def boom(conn):
        raise RuntimeError("disk exploded")
    monkeypatch.setattr(norm, "maybe_compact_raw_payloads", boom)
    admin_routes._running_imports.clear()
    run = _finish_import(monkeypatch)
    assert tmp_db.execute("SELECT status FROM import_runs WHERE id=?", (run["run_id"],)).fetchone()[0] == "ok"


def test_compaction_is_skipped_while_a_maintenance_job_runs(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(norm, "maybe_compact_raw_payloads", lambda conn: calls.append(1) or 0)
    assert admin_routes._maintenance_lock.acquire(blocking=False)
    try:
        admin_routes._running_imports.clear()
        _finish_import(monkeypatch)
    finally:
        admin_routes._maintenance_lock.release()
    assert calls == []


def test_compaction_runs_daily_once_caught_up(monkeypatch):
    assert norm.COMPACT_INTERVAL_S == 24 * 3600 and norm.COMPACT_TIME_BUDGET_S == 10.0


# ── stats + vacuum ───────────────────────────────────────────────────────────

def _bloat_then_free(conn):
    for i in range(30):
        put(conn, "get_heart_rates", day(10), "y" * 200_000)  # ~6MB
    norm.compact_raw_payloads(conn)  # leaves 1 row, frees the rest


def test_db_stats_report_reclaimable_space_after_compaction(tmp_db):
    _bloat_then_free(tmp_db)
    stats = maintenance.db_stats()
    assert stats["reclaimable_mb"] > 4 and stats["file_mb"] > stats["live_mb"]
    assert stats["volume_free_mb"] > 0


def test_vacuum_shrinks_the_file(tmp_db):
    _bloat_then_free(tmp_db)
    tmp_db.close()
    before = os.path.getsize(db_module.DB_PATH)
    result = maintenance.vacuum_db()
    after = os.path.getsize(db_module.DB_PATH)
    assert after < before * 0.5
    assert result["after"]["reclaimable_mb"] < 1
    # database still intact and writable
    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) FROM raw_import_payloads").fetchone()[0] == 1
    conn.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('x','x','x')")
    conn.commit()
    conn.close()


def test_vacuum_is_refused_when_there_is_nothing_to_reclaim(tmp_db):
    put(tmp_db, "get_stats", day(10), "{}")
    assert any("nothing worth vacuuming" in b for b in maintenance.vacuum_blockers())


def test_vacuum_is_refused_without_enough_disk(tmp_db, monkeypatch):
    _bloat_then_free(tmp_db)
    monkeypatch.setattr(maintenance, "_free_bytes", lambda path: 1024)
    blockers = maintenance.vacuum_blockers()
    assert any("volume" in b for b in blockers) and any("temp" in b for b in blockers)


def test_vacuum_allowed_when_space_and_reclaimable_exist(tmp_db, monkeypatch):
    monkeypatch.setattr(maintenance, "MIN_RECLAIMABLE_MB", 1)
    _bloat_then_free(tmp_db)
    assert maintenance.vacuum_blockers() == []


# ── endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_db, monkeypatch):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    app = FastAPI()
    app.include_router(admin_routes.router)
    return TestClient(app, headers={"Authorization": "Bearer pw"})


def test_maintenance_endpoints_require_auth(tmp_db, monkeypatch):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    app = FastAPI()
    app.include_router(admin_routes.router)
    c = TestClient(app)
    assert c.get("/admin/maintenance/db").status_code in (401, 403)
    assert c.post("/admin/maintenance/compact").status_code in (401, 403)
    assert c.post("/admin/maintenance/vacuum").status_code in (401, 403)


def test_compact_endpoint_runs_to_completion_and_reports(tmp_db, client):
    for i in range(5):
        put(tmp_db, "get_heart_rates", day(10), f'{{"v":{i}}}')
    r = client.post("/admin/maintenance/compact")
    assert r.status_code == 200 and r.json()["status"] == "started"
    for _ in range(50):  # TestClient runs background tasks before returning; poll defensively
        info = client.get("/admin/maintenance/db").json()
        if info["running"] is None:
            break
    assert info["last_result"]["deleted"] == 4
    assert len(ids(tmp_db)) == 1


def test_second_maintenance_job_gets_409_while_one_runs(tmp_db, client):
    assert admin_routes._maintenance_lock.acquire(blocking=False)
    try:
        admin_routes._maintenance_job["name"] = "vacuum"
        assert client.post("/admin/maintenance/compact").status_code == 409
    finally:
        admin_routes._maintenance_job["name"] = None
        admin_routes._maintenance_lock.release()


def test_vacuum_endpoint_refuses_with_reasons(tmp_db, client):
    r = client.post("/admin/maintenance/vacuum")  # nothing reclaimable in a fresh DB
    assert r.status_code == 409 and r.json()["detail"]["refused"]
