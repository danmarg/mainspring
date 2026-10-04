"""Concurrent-import safety: cheap prune, bounded dedupe, single-flight, serialized normalization."""

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException

import app.db as db_module
from app import admin_routes
from app.db import get_connection, init_db, upsert_raw_payload, utc_now
from app.normalize import prune_raw_payloads


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    conn = get_connection(path)
    admin_routes._running_imports.clear()
    yield conn
    conn.close()
    admin_routes._running_imports.clear()


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _seed(conn, ages):
    for i, age in enumerate(ages):
        conn.execute(
            "INSERT INTO raw_import_payloads(source, endpoint, date, payload_json, fetched_at) VALUES (?,?,?,?,?)",
            ("garmin", "get_stats", f"d{i}", "{}", _iso(age)),
        )
    conn.commit()


# ── prune ────────────────────────────────────────────────────────────────────

def test_prune_deletes_only_expired_rows(tmp_db):
    _seed(tmp_db, [400, 300, 200, 181, 179, 90, 1, 0])  # append-only → fetched_at rises with id
    assert prune_raw_payloads(tmp_db, retention_days=180) == 4
    assert [r[0] for r in tmp_db.execute("SELECT date FROM raw_import_payloads ORDER BY id")] == ["d4", "d5", "d6", "d7"]


def test_prune_is_a_noop_without_scanning_when_nothing_is_due(tmp_db):
    _seed(tmp_db, [100, 50, 1])
    statements = []
    tmp_db.set_trace_callback(statements.append)
    assert prune_raw_payloads(tmp_db, retention_days=180) == 0
    assert not [s for s in statements if s.lstrip().upper().startswith("DELETE")]
    # and it never filters on fetched_at across the whole table
    assert not [s for s in statements if "fetched_at <" in s and s.lstrip().upper().startswith("SELECT")]


def test_prune_runs_in_batches_and_commits_between_them(tmp_db, monkeypatch):
    import app.normalize as norm
    monkeypatch.setattr(norm, "PRUNE_BATCH_ROWS", 3)
    _seed(tmp_db, [500] * 10 + [1] * 2)
    statements = []
    tmp_db.set_trace_callback(statements.append)
    assert prune_raw_payloads(tmp_db, retention_days=180) == 10
    assert sum(s.strip().upper() == "COMMIT" for s in statements) >= 4  # one per batch
    assert tmp_db.execute("SELECT COUNT(*) FROM raw_import_payloads").fetchone()[0] == 2


def test_prune_time_budget_bounds_one_call_and_resumes_next_time(tmp_db, monkeypatch):
    import app.normalize as norm
    monkeypatch.setattr(norm, "PRUNE_BATCH_ROWS", 2)
    _seed(tmp_db, [500] * 8 + [1])
    first = prune_raw_payloads(tmp_db, retention_days=180, time_budget_s=0.0)
    assert first == 0  # budget exhausted before the first batch
    assert prune_raw_payloads(tmp_db, retention_days=180) == 8


def test_prune_never_deletes_a_fresh_row_even_if_fetched_at_is_out_of_order(tmp_db):
    _seed(tmp_db, [400, 1, 300, 0])  # id order is not time order
    prune_raw_payloads(tmp_db, retention_days=180)
    left = {r[0] for r in tmp_db.execute("SELECT date FROM raw_import_payloads")}
    assert {"d1", "d3"} <= left  # the fresh rows survive


# ── dedupe is bounded to recent rows ─────────────────────────────────────────

def test_dedupe_only_searches_recent_rows(tmp_db, monkeypatch):
    monkeypatch.setattr(db_module, "_DEDUPE_WINDOW_ROWS", 3)
    upsert_raw_payload(tmp_db, "garmin", "activity", '{"a":1}', "2025-06-01")
    upsert_raw_payload(tmp_db, "garmin", "activity", '{"a":1}', "2025-06-01")  # recent copy → skipped
    assert tmp_db.execute("SELECT COUNT(*) FROM raw_import_payloads").fetchone()[0] == 1
    for i in range(5):  # push the original out of the window
        upsert_raw_payload(tmp_db, "garmin", "get_stats", f'{{"n":{i}}}', f"2025-06-0{i}")
    upsert_raw_payload(tmp_db, "garmin", "activity", '{"a":1}', "2025-06-01")
    assert tmp_db.execute("SELECT COUNT(*) FROM raw_import_payloads WHERE endpoint='activity'").fetchone()[0] == 2


def test_dedupe_queries_use_a_rowid_range_not_a_table_scan(tmp_db):
    statements = []
    tmp_db.set_trace_callback(statements.append)
    upsert_raw_payload(tmp_db, "garmin", "activity", '{"a":1}', "2025-06-01")
    upsert_raw_payload(tmp_db, "garmin", "get_stats", '{"a":1}', "2025-06-01")
    selects = [s for s in statements if "FROM raw_import_payloads" in s and "MAX(id)" not in s and "SELECT" in s.upper()]
    assert selects and all("id >" in s for s in selects)


# ── single-flight ────────────────────────────────────────────────────────────

def _start(source="garmin", **kw):
    return admin_routes._start_import(source, BackgroundTasks(), lambda *a, **k: {}, 7, kw.get("start_date"), kw.get("end_date"))


def test_second_rolling_import_for_same_source_is_a_noop(tmp_db):
    first = _start()
    second = _start()
    assert first["status"] == "started"
    assert second == {"run_id": first["run_id"], "status": "already_running"}
    assert tmp_db.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 1


def test_different_sources_run_independently(tmp_db):
    assert _start("garmin")["status"] == "started"
    assert _start("google_health")["status"] == "started"


def test_explicit_range_backfill_gets_409_while_running(tmp_db):
    _start()
    with pytest.raises(HTTPException) as exc:
        _start(start_date="2025-01-01", end_date="2025-01-07")
    assert exc.value.status_code == 409


def test_slot_is_released_when_the_run_finishes_even_on_failure(tmp_db):
    run = _start()
    def boom(conn, **kw):
        raise RuntimeError("upstream down")
    admin_routes._run_import_bg("garmin", run["run_id"], boom, {})
    assert "garmin" not in admin_routes._running_imports
    status, err = tmp_db.execute("SELECT status, error FROM import_runs WHERE id=?", (run["run_id"],)).fetchone()
    assert status == "error" and "upstream down" in err
    assert _start()["status"] == "started"


def test_hung_run_stops_blocking_after_max_age(tmp_db, monkeypatch):
    first = _start()
    monkeypatch.setattr(admin_routes, "IMPORT_MAX_AGE_S", 0)
    second = _start()
    assert second["status"] == "started" and second["run_id"] != first["run_id"]


def test_failed_run_row_insert_frees_the_slot(tmp_db, monkeypatch):
    def broken_db():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(admin_routes, "db", broken_db)
    with pytest.raises(sqlite3.OperationalError):
        _start()
    assert "garmin" not in admin_routes._running_imports


def test_startup_marks_leftover_running_rows_as_interrupted(tmp_db):
    tmp_db.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('garmin', ?, 'running')", (utc_now(),))
    tmp_db.execute("INSERT INTO import_runs(source, started_at, finished_at, status) VALUES ('garmin', ?, ?, 'ok')", (utc_now(), utc_now()))
    tmp_db.commit()
    assert admin_routes.mark_interrupted_imports() == 1
    rows = tmp_db.execute("SELECT status, error FROM import_runs ORDER BY id").fetchall()
    assert rows[0][0] == "error" and "interrupted" in rows[0][1]
    assert rows[1][0] == "ok"


# ── normalization is serialized across concurrent imports ────────────────────

def test_concurrent_imports_never_normalize_at_the_same_time(tmp_db, monkeypatch):
    import app.normalize as norm
    inside, peak, lock = [0], [0], threading.Lock()

    def fake_normalization(conn, dates=None):
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        time.sleep(0.15)
        with lock:
            inside[0] -= 1
        return {}

    monkeypatch.setattr(norm, "run_normalization", fake_normalization)
    monkeypatch.setattr(admin_routes, "_fire_morning_webhook", lambda: True)
    runs = [_start("garmin"), _start("google_health")]

    def fake_import(conn, **kw):
        return {"skipped": False, "rows_upserted": 1, "dates": ["2025-06-01"]}

    threads = [threading.Thread(target=admin_routes._run_import_bg, args=(s, r["run_id"], fake_import, {}))
               for s, r in zip(("garmin", "google_health"), runs)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert peak[0] == 1
