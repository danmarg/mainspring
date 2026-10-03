"""
Regression tests for the "server times out during imports / tool calls" bug.

Two failure modes, both from one single-process, single-SQLite-file design:
  1. FastMCP runs sync tool functions inline on the event loop, so a slow call —
     or a write waiting up to busy_timeout (5min) behind an import's write
     lock — froze /healthz and every other request.
  2. sqlite3's implicit BEGIN holds the write lock from the first INSERT until
     commit, so importers that wrote and then made an HTTP call kept every other
     writer (MCP logging tools) blocked for the duration of the network call.
"""

import asyncio
import inspect
import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import app.db as db_module
from app.db import db, get_connection, init_db, utc_now


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    conn = get_connection(path)
    yield conn
    conn.close()


def _write_lock_is_free() -> bool:
    """True if another connection could start a write right now (no waiting)."""
    other = sqlite3.connect(str(db_module.DB_PATH), timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


def _hold_write_lock() -> sqlite3.Connection:
    blocker = sqlite3.connect(str(db_module.DB_PATH), isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def _release_after(seconds, blocker, stmt):
    t = threading.Timer(seconds, lambda: blocker.execute(stmt))
    t.daemon = True
    t.start()
    return t


# ── event loop stays responsive ──────────────────────────────────────────────

def test_tool_blocked_on_write_lock_does_not_stall_event_loop(tmp_db):
    from app.mcp_server import mcp

    async def scenario():
        blocker = _hold_write_lock()
        gaps = []

        async def ticker():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        # Released from a plain thread, not an asyncio task: if the loop were
        # blocked, a task could never run and the test would deadlock for the full
        # busy_timeout instead of failing on the assertions below.
        _release_after(0.6, blocker, "COMMIT")

        tick = asyncio.create_task(ticker())
        start = time.monotonic()
        await mcp.call_tool("log_note", {"description": "waits for the lock"})
        elapsed = time.monotonic() - start
        tick.cancel()
        return elapsed, max(gaps)

    elapsed, max_gap = asyncio.run(scenario())
    assert elapsed >= 0.5  # really did wait on the lock...
    assert max_gap < 0.4   # ...without freezing the loop (inline would be ~0.6s+)
    row = tmp_db.execute("SELECT description FROM manual_logs").fetchone()
    assert row[0] == "waits for the lock"


def test_read_tool_is_not_delayed_by_log_write_under_lock(tmp_db):
    """The tool_call_log insert uses a short busy timeout and is best-effort, so
    a read-only tool still returns promptly while an import holds the lock."""
    from app.mcp_server import mcp

    blocker = _hold_write_lock()
    # safety net: a regression to the 5min default timeout fails the assertion
    # after ~6s instead of hanging
    _release_after(6, blocker, "ROLLBACK")
    start = time.monotonic()
    asyncio.run(mcp.call_tool("get_source_config", {}))
    assert time.monotonic() - start < 4  # ~1s log timeout, never the 5min default


def test_tool_call_log_write_gives_up_quickly_when_locked(tmp_db):
    from datetime import datetime, timezone
    from app.mcp_server import _write_tool_call_log

    blocker = _hold_write_lock()
    try:
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            _write_tool_call_log(
                datetime.now(timezone.utc), "t", {}, 1.0, "ok", None, None
            )
        assert time.monotonic() - start < 4
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_every_mcp_tool_runs_off_the_event_loop():
    from app.mcp_server import mcp

    tools = mcp._tool_manager._tools
    assert tools and all(t.is_async for t in tools.values())


def test_db_touching_routes_are_sync_so_starlette_threadpools_them():
    from app import admin_routes, dashboard

    for fn in (
        admin_routes.import_garmin, admin_routes.import_google_health,
        admin_routes.import_status, admin_routes.google_health_init_tokens,
        admin_routes.energy_calibration_status, admin_routes.export_db,
        dashboard.overview, dashboard.trends, dashboard.behavior,
        dashboard.nutrition, dashboard.activities, dashboard.vitals,
    ):
        assert not inspect.iscoroutinefunction(fn), f"{fn.__name__} would block the loop"
    assert inspect.iscoroutinefunction(
        __import__("app.main", fromlist=["healthz"]).healthz
    )


# ── importers don't hold the write lock across network calls ─────────────────

def test_garmin_run_import_releases_write_lock_before_every_fetch(tmp_db, monkeypatch):
    import app.importers.garmin as garmin

    monkeypatch.setenv("GARMINTOKENS", "fake")
    for name in [n for n in dir(garmin) if n.startswith("_parse_")]:
        monkeypatch.setattr(garmin, name, lambda *a, **k: 0)
    monkeypatch.setattr(garmin, "_upsert_activity", lambda *a, **k: False)

    checks = []

    class Client:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                checks.append((name, _write_lock_is_free()))
                return {"payload": name}  # truthy → _store_raw writes before the next call
            return call

    with patch.object(garmin, "_client", return_value=Client()):
        garmin.run_import(tmp_db, days=2)

    assert len(checks) > 20
    locked = [name for name, free in checks if not free]
    assert not locked, f"write lock held during fetch: {locked}"


def test_garmin_push_pending_manual_logs_releases_lock_between_pushes(tmp_db, monkeypatch):
    import app.importers.garmin as garmin

    monkeypatch.setenv("GARMINTOKENS", "fake")
    for _ in range(3):
        tmp_db.execute(
            "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) "
            "VALUES ('2025-06-01T09:00:00+00:00', 'hydration', '500ml', 500.0, 'ml', ?)",
            (utc_now(),),
        )
    tmp_db.commit()

    free_at_push = []
    client = MagicMock()
    client.add_hydration_data.side_effect = lambda *a, **k: free_at_push.append(_write_lock_is_free())

    with patch.object(garmin, "_client", return_value=client):
        assert garmin.push_pending_manual_logs(tmp_db) == 3

    assert free_at_push == [True, True, True]


def test_garmin_hr_zone_fetch_releases_lock_after_activity_upsert(tmp_db, monkeypatch):
    import app.importers.garmin as garmin

    free = []
    client = MagicMock()
    client.get_activity_hr_in_timezones.side_effect = (
        lambda *a, **k: free.append(_write_lock_is_free()) or []
    )
    tmp_db.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('x','x','x')")  # open write txn
    garmin._fetch_and_store_hr_zones(tmp_db, client, "123", "running", 3600)
    assert free == [True]


@pytest.mark.parametrize("fn_name,args", [("_get", ({},)), ("_post", ({"range": {}},))])
def test_google_health_releases_lock_before_http(tmp_db, monkeypatch, fn_name, args):
    import app.importers.google_health as gh

    free = []

    class Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, *a, **k):
        free.append(_write_lock_is_free())
        return Resp()

    tmp_db.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('x','x','x')")  # open write txn
    tokens = {"access_token": "a", "refresh_token": "r", "expires_at": "x"}
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        getattr(gh, fn_name)(tmp_db, "/p", *args, tokens)

    assert free == [True]


# ── manual logging tools stay cheap ──────────────────────────────────────────

def test_log_hydration_refreshes_that_days_metrics_without_full_normalization(tmp_db):
    """Logging hydration must update the day's daily_metrics immediately, but must
    not run the global job (activities rebuild + raw-payload prune) — that made
    every log call slow and lock-prone."""
    from app.mcp_server import log_hydration

    tmp_db.execute(
        "INSERT INTO activities(date, type, start_time, canonical_source) VALUES ('2020-01-01','run','2020-01-01T08:00:00','garmin')"
    )
    tmp_db.execute(
        "INSERT INTO raw_import_payloads(source, endpoint, payload_json, date, fetched_at) "
        "VALUES ('garmin','get_stats','{}','2000-01-01','2000-01-01T00:00:00+00:00')"
    )
    tmp_db.commit()

    log_hydration(ml=500.0, ts="2025-06-01T09:00:00+00:00")

    row = tmp_db.execute("SELECT hydration_ml FROM daily_metrics WHERE date='2025-06-01'").fetchone()
    assert row[0] == 500.0
    # untouched: a full run_normalization would delete the activity row (no
    # garmin_activities source row backs it) and prune the ancient raw payload
    assert tmp_db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM raw_import_payloads").fetchone()[0] == 1


def test_log_hydration_never_calls_full_normalization(tmp_db):
    from app.mcp_server import log_hydration

    with patch("app.normalize.run_normalization") as full:
        log_hydration(ml=250.0, ts="2025-06-01T09:00:00+00:00")
    full.assert_not_called()


def test_renormalize_failure_is_logged_not_raised(tmp_db, caplog):
    from app.mcp_server import log_hydration

    with patch("app.normalize.rebuild_daily_metrics", side_effect=RuntimeError("boom")):
        result = log_hydration(ml=250.0, ts="2025-06-01T09:00:00+00:00")

    assert "Logged hydration" in result  # the log itself still succeeds
    assert any("renormalize after manual log failed" in r.message for r in caplog.records)
    assert tmp_db.execute("SELECT COUNT(*) FROM manual_logs").fetchone()[0] == 1


def test_log_hydration_releases_write_lock_quickly(tmp_db):
    from app.mcp_server import log_hydration

    log_hydration(ml=250.0, ts="2025-06-01T09:00:00+00:00")
    assert _write_lock_is_free()


def test_non_utc_log_timestamp_refreshes_the_utc_day(tmp_db):
    """23:30 at UTC-5 is 04:30 UTC the next day; manual logs aggregate by SQL
    DATE(ts) (UTC), so that's the day that must be refreshed."""
    from app.mcp_server import log_hydration

    log_hydration(ml=300.0, ts="2025-06-01T23:30:00-05:00")
    row = tmp_db.execute("SELECT hydration_ml FROM daily_metrics WHERE date='2025-06-02'").fetchone()
    assert row and row[0] == 300.0


def test_tool_pool_is_not_exhausted_by_lock_waiters(tmp_db):
    """More lock-blocked write tools than the default executor has workers must
    not stop an unrelated read tool from answering."""
    from app.mcp_server import mcp

    async def scenario():
        blocker = _hold_write_lock()
        _release_after(2.0, blocker, "COMMIT")
        writers = [
            asyncio.create_task(mcp.call_tool("log_note", {"description": f"w{i}"}))
            for i in range(12)
        ]
        await asyncio.sleep(0.2)
        start = time.monotonic()
        await mcp.call_tool("get_source_config", {})
        read_elapsed = time.monotonic() - start
        await asyncio.gather(*writers)
        return read_elapsed

    assert asyncio.run(scenario()) < 1.0


# ── write-lock diagnostics ───────────────────────────────────────────────────

def test_long_write_transaction_is_logged_with_its_holder(tmp_db, monkeypatch, caplog):
    monkeypatch.setattr(db_module, "LOCK_HOLD_WARN_S", 0.05)
    with caplog.at_level("WARNING", logger="app.db"):
        with db_module.db() as conn:
            conn.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('x','x','x')")
            time.sleep(0.12)
    assert any(
        "write transaction held" in r.message and "test_event_loop_blocking" in r.message
        for r in caplog.records
    )


def test_short_write_transaction_is_not_logged(tmp_db, caplog):
    with caplog.at_level("WARNING", logger="app.db"):
        with db_module.db() as conn:
            conn.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('x','x','x')")
    assert not [r for r in caplog.records if "write transaction held" in r.message]


def test_locked_error_names_the_open_write_transactions(tmp_db, caplog):
    holder = db_module.get_connection()
    holder.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('held','x','x')")  # lock held
    try:
        with caplog.at_level("ERROR", logger="app.db"):
            with pytest.raises(sqlite3.OperationalError):
                with db_module.db() as conn:
                    conn.execute("PRAGMA busy_timeout=50")
                    conn.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('y','x','x')")
    finally:
        holder.rollback()
        holder.close()
    msg = next(r.message for r in caplog.records if "database is locked" in r.message)
    assert "test_event_loop_blocking" in msg and "open" in msg  # the holder is identified


def test_open_transaction_registry_clears_on_commit(tmp_db):
    conn = db_module.get_connection()
    conn.execute("INSERT INTO import_runs(source, started_at, status) VALUES ('x','x','x')")
    assert db_module.open_write_transactions()
    conn.commit()
    assert db_module.open_write_transactions() == []
    conn.close()
