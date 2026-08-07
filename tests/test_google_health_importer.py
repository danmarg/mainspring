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
from app.importers.google_health import (
    _fetch_intraday_hr_for_date,
    _get,
    _parse_intraday_hr,
    _parse_skin_temp,
    _post,
)


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
            {"dailySleepTemperatureDerivations": {
                "nightlyTemperatureCelsius": 34.3, "baselineTemperatureCelsius": 34.0,
            }}
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


def test_parse_intraday_hr_reads_sample_time_and_bpm(tmp_db):
    """HeartRate data points nest the timestamp under sampleTime.physicalTime
    (ObservationSampleTime), not a bare startTime (regression test for the 400
    INVALID_DATA_POINT_FILTER this shape mismatch caused, which meant
    google_health intraday HR was never actually ingested). beatsPerMinute
    comes back as a string, not a number."""
    data = {
        "dataPoints": [
            {"heartRate": {
                "sampleTime": {"physicalTime": "2025-01-01T03:00:00Z"},
                "beatsPerMinute": "52",
            }},
            {"heartRate": {
                "sampleTime": {"physicalTime": "2025-01-01T03:00:30Z"},
                "beatsPerMinute": "54",
            }},
        ]
    }
    rows = _parse_intraday_hr(tmp_db, data)
    assert rows == 1  # both samples fall in the same 1-min bucket
    row = tmp_db.execute(
        "SELECT bpm FROM intraday_hr WHERE ts='2025-01-01T03:00:00Z' AND source='google_health'"
    ).fetchone()
    assert row[0] == 53.0


def test_fetch_intraday_hr_filters_on_sample_time_not_start_time(tmp_db):
    """Regression test for the 400 INVALID_DATA_POINT_FILTER this shape mismatch
    caused, which meant google_health intraday HR was never actually ingested."""
    from datetime import date
    captured = {}

    def fake_get(conn, path, params, tokens):
        captured["filter"] = params["filter"]
        captured["pageSize"] = params.get("pageSize")
        return None

    import app.importers.google_health as gh
    orig_get = gh._get
    gh._get = fake_get
    try:
        _fetch_intraday_hr_for_date(tmp_db, date(2025, 1, 15), {})
    finally:
        gh._get = orig_get

    assert "heart_rate.sample_time.physical_time" in captured["filter"]
    assert "heart_rate.startTime" not in captured["filter"]
    assert captured["pageSize"] == gh.HR_PAGE_SIZE


def test_fetch_intraday_hr_pages_until_token_exhausted(tmp_db):
    """A single default-sized page (50 rows) covers under two minutes of a
    night at Fitbit's ~2s passive sampling rate — this must keep following
    nextPageToken rather than stopping after page one."""
    from datetime import date

    import app.importers.google_health as gh

    def make_page(hour: int, has_next: bool):
        return {
            "dataPoints": [{"heartRate": {
                "sampleTime": {"physicalTime": f"2025-01-15T{hour:02d}:00:00Z"},
                "beatsPerMinute": "60",
            }}],
            **({"nextPageToken": f"tok-{hour}"} if has_next else {}),
        }

    pages = [make_page(3, True), make_page(2, True), make_page(1, False)]
    calls = []

    def fake_get(conn, path, params, tokens):
        calls.append(params.get("pageToken"))
        return pages[len(calls) - 1]

    orig_get = gh._get
    gh._get = fake_get
    try:
        rows = _fetch_intraday_hr_for_date(tmp_db, date(2025, 1, 15), {})
    finally:
        gh._get = orig_get

    assert len(calls) == 3
    assert calls[0] is None  # first request has no pageToken
    assert calls[1] == "tok-3"
    assert calls[2] == "tok-2"
    assert rows == 3
    stored = tmp_db.execute("SELECT count(*) FROM intraday_hr WHERE source='google_health'").fetchone()[0]
    assert stored == 3
