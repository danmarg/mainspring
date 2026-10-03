"""Single-password auth, login hardening, and credential-table isolation."""

import asyncio
import sqlite3

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import app.db as db_module
from app import auth
from app.db import get_connection, init_db


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("MAINSPRING_PASSWORD", "ADMIN_TOKEN", "EXPORT_TOKEN", "DATASETTE_TOKEN", "MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    auth.limiter._failures.clear()
    yield
    auth.limiter._failures.clear()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    return path


# ── verify / legacy fallback ─────────────────────────────────────────────────

def test_password_unlocks_every_surface(monkeypatch):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    for legacy in (auth.ADMIN, auth.EXPORT, auth.DATASETTE, auth.MCP):
        assert auth.verify("pw", legacy)
        assert not auth.verify("nope", legacy)


def test_unconfigured_rejects_everything():
    assert not auth.configured(auth.ADMIN)
    assert not auth.verify("", auth.ADMIN)
    assert not auth.verify("anything", auth.ADMIN)


def test_legacy_token_only_works_for_its_own_surface(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "old-admin")
    assert auth.verify("old-admin", auth.ADMIN)
    assert not auth.verify("old-admin", auth.EXPORT)


def test_session_cookie_is_per_surface_and_not_the_secret(monkeypatch):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    cookie = auth.primary_session_value("dashboard")
    assert "pw" not in cookie
    assert auth.verify_session(cookie, "dashboard")
    assert not auth.verify_session(cookie, "datasette")


# ── bearer dependency + rate limiting ────────────────────────────────────────

def _client():
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(auth.require_bearer(auth.ADMIN))])
    def x():
        return {"ok": True}

    return TestClient(app)


def test_bearer_dependency(monkeypatch):
    c = _client()
    assert c.get("/x").status_code == 503  # nothing configured
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    assert c.get("/x").status_code == 401
    assert c.get("/x", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/x", headers={"Authorization": "Bearer pw"}).status_code == 200


def test_bearer_failures_lock_out_then_block_even_correct_password(monkeypatch):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    c = _client()
    for _ in range(auth.limiter.max_failures):
        assert c.get("/x", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/x", headers={"Authorization": "Bearer pw"}).status_code == 429


# ── MCP OAuth login page ─────────────────────────────────────────────────────

def _oauth_client(tmp_db):
    from app.mcp_oauth import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, follow_redirects=False)


def test_login_page_escapes_error_and_session(tmp_db):
    c = _oauth_client(tmp_db)
    r = c.get("/mcp-auth/login", params={"session": '"><script>alert(1)</script>', "error": "<script>x</script>"})
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


def _seed_pending(tmp_db, redirect_uri="https://claude.ai/cb", state=None):
    from mcp.server.auth.provider import AuthorizationParams
    params = AuthorizationParams(
        state=state, scopes=[], code_challenge="c" * 43,
        redirect_uri=redirect_uri, redirect_uri_provided_explicitly=True,
    )
    import time
    conn = get_connection(tmp_db)
    conn.execute(
        "INSERT INTO mcp_pending_auth(session_id, client_id, params_json, expires_at, created_at) "
        "VALUES ('s1','cid',?,?,'now')", (params.model_dump_json(), time.time() + 600),
    )
    conn.commit()
    conn.close()


def test_login_wrong_password_redirects_and_rate_limits(monkeypatch, tmp_db):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    c = _oauth_client(tmp_db)
    for _ in range(auth.limiter.max_failures):
        r = c.post("/mcp-auth/login", data={"session": "s1", "pin": "bad"})
        assert r.status_code == 303
    assert c.post("/mcp-auth/login", data={"session": "s1", "pin": "pw"}).status_code == 429


def test_login_callback_encodes_state_and_escapes_markup(monkeypatch, tmp_db):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    _seed_pending(tmp_db, state='"><script>alert(1)</script>&x=1#')
    c = _oauth_client(tmp_db)
    r = c.post("/mcp-auth/login", data={"session": "s1", "pin": "pw"})
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text
    assert "&x=1#" not in r.text  # state is URL-encoded, not appended raw


def test_login_rejects_javascript_redirect_uri(monkeypatch, tmp_db):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    _seed_pending(tmp_db, redirect_uri="javascript:alert(1)")
    c = _oauth_client(tmp_db)
    assert c.post("/mcp-auth/login", data={"session": "s1", "pin": "pw"}).status_code == 400


# ── dashboard login ──────────────────────────────────────────────────────────

def test_dashboard_login_sets_secure_session_cookie(monkeypatch, tmp_db):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    from app.dashboard import router
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app, follow_redirects=False)
    bad = c.post("/dashboard/login", data={"token": "nope"})
    assert "set-cookie" not in bad.headers
    ok = c.post("/dashboard/login", data={"token": "pw"})
    cookie = ok.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "pw;" not in cookie


# ── credential tables stay private ───────────────────────────────────────────

def _seed_secrets(path):
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO mcp_refresh_tokens(token, token_json, created_at) VALUES ('rt-secret','{\"t\":1}','now')")
    conn.execute(
        "INSERT INTO google_health_oauth(id, access_token, refresh_token, expires_at, updated_at) "
        "VALUES (1,'at-secret','gh-refresh-secret','x','now')"
    )
    conn.commit()
    conn.close()


def test_authorizer_nulls_secret_columns(tmp_db):
    _seed_secrets(tmp_db)
    conn = sqlite3.connect(str(tmp_db))
    conn.set_authorizer(db_module.deny_secret_reads)
    assert conn.execute("SELECT token FROM mcp_refresh_tokens").fetchone() == (None,)
    assert conn.execute("SELECT COUNT(*) FROM mcp_refresh_tokens").fetchone() == (1,)
    conn.close()


def test_datasette_sql_cannot_read_credentials(tmp_db):
    _seed_secrets(tmp_db)
    from app.datasette_mount import make_datasette

    async def run():
        ds = make_datasette()
        await ds.invoke_startup()
        out = []
        for sql in ("select * from google_health_oauth", "select token, token_json from mcp_refresh_tokens"):
            r = await ds.client.get("/health.json", params={"sql": sql, "_shape": "array"})
            out.append(r.text)
        return out

    for text in asyncio.run(run()):
        assert "secret" not in text


def test_export_snapshot_scrubs_credentials(monkeypatch, tmp_db):
    _seed_secrets(tmp_db)
    from app import admin_routes

    resp = admin_routes.export_db()
    snap = sqlite3.connect(resp.path)
    try:
        for table in db_module.SECRET_TABLES:
            assert snap.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        snap.close()
    raw = open(resp.path, "rb").read()
    assert b"rt-secret" not in raw and b"gh-refresh-secret" not in raw
    # the live DB is untouched
    live = sqlite3.connect(str(tmp_db))
    assert live.execute("SELECT COUNT(*) FROM google_health_oauth").fetchone()[0] == 1
    live.close()


# ── review follow-ups ────────────────────────────────────────────────────────

def test_non_ascii_cookie_is_rejected_not_a_500(monkeypatch):
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    assert auth.verify_session("é", "dashboard") is False
    assert auth.verify_session("\udcff", "dashboard") is False


def test_fly_client_ip_only_trusted_on_fly(monkeypatch):
    headers = {"fly-client-ip": "1.2.3.4"}
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    assert auth.client_ip(headers, "10.0.0.9") == "10.0.0.9"  # spoofable off-Fly → ignored
    monkeypatch.setenv("FLY_APP_NAME", "mainspring")
    assert auth.client_ip(headers, "10.0.0.9") == "1.2.3.4"


def test_limiter_memory_is_bounded(monkeypatch):
    lim = auth.RateLimiter(max_failures=3, window_s=0)  # everything expires immediately
    for i in range(10_500):
        lim.fail(f"ip{i}")
    assert len(lim._failures) < 10_000


def _call_datasette(mw, header=None, ip="9.9.9.9"):
    out = []
    scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
             "client": (ip, 1234), "headers": [(b"authorization", header)] if header else []}

    async def receive(): pass
    async def send(msg): out.append(msg)
    asyncio.run(mw(scope, receive, send))
    return [m.get("status") for m in out if "status" in m]


def test_datasette_failed_credentials_are_rate_limited(monkeypatch):
    from app.datasette_mount import _TokenMiddleware
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    reached = []

    async def inner(s, r, send): reached.append(True)

    mw = _TokenMiddleware(inner)
    for _ in range(auth.limiter.max_failures):
        assert _call_datasette(mw, b"Bearer wrong") == [401]
    assert _call_datasette(mw, b"Bearer pw") == [429]
    assert not reached
    assert _call_datasette(mw, b"Bearer pw", ip="8.8.8.8") == [] and reached  # other IPs unaffected


def test_export_failure_leaves_no_snapshot_behind(monkeypatch, tmp_db, tmp_path):
    import glob, tempfile
    from app import admin_routes
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(snap_dir))
    _seed_secrets(tmp_db)
    monkeypatch.setattr(db_module, "SECRET_TABLES", frozenset({"table_that_does_not_exist"}))
    monkeypatch.setattr(admin_routes, "SECRET_TABLES", frozenset({"table_that_does_not_exist"}))
    with pytest.raises(sqlite3.OperationalError):
        admin_routes.export_db()
    assert glob.glob(str(snap_dir / "*.db")) == []
