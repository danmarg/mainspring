"""
Tests for the Google Health importer's OAuth token handling.

Regression test for a bug where a refreshed access token was rebound to a
local variable inside _get/_post and never propagated back to run_import's
loop, causing every subsequent API call in the same run to re-trigger a
token refresh against Google's OAuth endpoint.
"""

import json
import urllib.error
from unittest.mock import patch

import pytest

import app.db as db_module
from app.db import init_db, get_connection
from app.importers.google_health import _get, _parse_skin_temp, _post


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


def _http_error_401():
    return urllib.error.HTTPError(
        "https://health.googleapis.com/v4/x", 401, "Unauthorized", {}, None
    )


def _make_urlopen_401_then_ok(ok_body: dict):
    """First call raises 401, second call (the retried data request) succeeds."""
    calls = {"n": 0}

    def fake_urlopen(req, *args, **kwargs):
        calls["n"] += 1
        if req.full_url == "https://oauth2.googleapis.com/token":
            resp = _FakeResp({"access_token": "new-access-token", "expires_in": 3600})
            return resp
        if calls["n"] == 1:
            raise _http_error_401()
        return _FakeResp(ok_body)

    return fake_urlopen, calls


class _FakeResp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_propagates_refreshed_token_to_caller(tmp_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")

    fake_urlopen, calls = _make_urlopen_401_then_ok({"ok": True})
    tokens = {"access_token": "stale-token", "refresh_token": "r", "expires_at": "x"}

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        data = _get(tmp_db, "/some/path", {}, tokens)

    assert data == {"ok": True}
    # The caller's dict must reflect the refreshed token — not just a local
    # rebind inside _get — so the next call in run_import's loop reuses it
    # instead of refreshing again.
    assert tokens["access_token"] == "new-access-token"


def test_post_propagates_refreshed_token_to_caller(tmp_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")

    fake_urlopen, calls = _make_urlopen_401_then_ok({"ok": True})
    tokens = {"access_token": "stale-token", "refresh_token": "r", "expires_at": "x"}

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        data = _post(tmp_db, "/some/path", {"range": {}}, tokens)

    assert data == {"ok": True}
    assert tokens["access_token"] == "new-access-token"


def test_parse_skin_temp(tmp_db):
    data = {
        "dataPoints": [
            {"skinTemperature": {"deltas": [{"deltaCelsius": 0.2}, {"deltaCelsius": 0.4}]}}
        ]
    }
    rows = _parse_skin_temp(tmp_db, "2025-01-01", data)
    assert rows == 1
    row = tmp_db.execute(
        "SELECT value FROM raw_daily_metrics WHERE date='2025-01-01' AND metric='skin_temp_deviation'"
    ).fetchone()
    assert abs(row[0] - 0.3) < 0.01


def test_parse_skin_temp_no_data(tmp_db):
    assert _parse_skin_temp(tmp_db, "2025-01-01", {}) == 0
    assert _parse_skin_temp(tmp_db, "2025-01-01", {"dataPoints": []}) == 0
