"""
Tests for Datasette mount and /export/db.
Datasette itself is not installed in this env, so we stub it for the
auth-middleware tests. The export test uses stdlib sqlite3 only.
"""

import asyncio
import os
import sqlite3
import tempfile
import pathlib

import app.db as db_module
from app.db import init_db, utc_now


# ── token middleware (bearer / basic / cookie / ?token=) ─────────────────────

from app.datasette_mount import _TokenMiddleware


async def _call(mw, auth_header=None, query_string=b""):
    responses = []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query_string,
        "headers": [(b"authorization", auth_header)] if auth_header else [],
    }
    async def receive(): pass
    async def send(msg): responses.append(msg)
    await mw(scope, receive, send)
    return responses


def test_datasette_bearer_allows_valid():
    passed = []
    async def inner(s, r, send): passed.append(True)
    mw = _TokenMiddleware(inner, token="ds-secret")
    asyncio.run(_call(mw, auth_header=b"Bearer ds-secret"))
    assert passed == [True]


def test_datasette_bearer_rejects_wrong():
    async def inner(s, r, send): pass
    mw = _TokenMiddleware(inner, token="ds-secret")
    resp = asyncio.run(_call(mw, auth_header=b"Bearer wrong"))
    assert any(r.get("status") == 401 for r in resp)


def test_datasette_bearer_rejects_missing():
    async def inner(s, r, send): pass
    mw = _TokenMiddleware(inner, token="ds-secret")
    resp = asyncio.run(_call(mw))
    assert any(r.get("status") == 401 for r in resp)


def test_datasette_bearer_www_authenticate_header():
    async def inner(s, r, send): pass
    mw = _TokenMiddleware(inner, token="ds-secret")
    resp = asyncio.run(_call(mw))
    start = next(r for r in resp if r.get("type") == "http.response.start")
    header_names = [k.lower() for k, v in start.get("headers", [])]
    assert b"www-authenticate" in header_names


def test_datasette_token_query_param():
    passed = []
    async def inner(s, r, send): passed.append(True)
    mw = _TokenMiddleware(inner, token="ds-secret")
    asyncio.run(_call(mw, query_string=b"token=ds-secret"))
    assert passed == [True]


# ── build_datasette_app returns None without token ────────────────────────────

def test_build_datasette_app_none_without_token(monkeypatch):
    monkeypatch.delenv("DATASETTE_TOKEN", raising=False)
    from app.datasette_mount import build_datasette_app
    assert build_datasette_app() is None


# ── export/db: VACUUM INTO produces a valid SQLite file ──────────────────────

def test_export_vacuum_into():
    """Smoke-test the VACUUM INTO mechanic used by the export endpoint."""
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / "source.db"
        dst = pathlib.Path(d) / "snapshot.db"

        # seed a small db
        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.execute(f"VACUUM INTO '{dst}'")
        conn.close()

        # snapshot is a valid, readable SQLite file
        snap = sqlite3.connect(str(dst))
        row = snap.execute("SELECT x FROM t").fetchone()
        snap.close()
        assert row[0] == 42
        # snapshot is independent of WAL (no -wal/-shm files needed)
        assert not (dst.parent / (dst.name + "-wal")).exists()


def test_export_snapshot_independent_of_source():
    """Modifying source after VACUUM INTO doesn't affect the snapshot."""
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / "source.db"
        dst = pathlib.Path(d) / "snapshot.db"

        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.execute(f"VACUUM INTO '{dst}'")
        # mutate source after snapshot
        conn.execute("INSERT INTO t VALUES (2)")
        conn.commit()
        conn.close()

        snap = sqlite3.connect(str(dst))
        rows = snap.execute("SELECT x FROM t").fetchall()
        snap.close()
        assert len(rows) == 1  # snapshot has only the original row
