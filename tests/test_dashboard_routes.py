"""Render every dashboard page end-to-end against a seeded DB.

The chart-builder tests never exercise the route handlers, which is how a
closed-connection bug in `overview` reached production."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db_module
from app.db import get_connection, init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    monkeypatch.setenv("HOME_TZ", "UTC")
    init_db(path)

    conn = get_connection(path)
    now = datetime.now(timezone.utc)
    for i in range(14):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO daily_metrics(date, resting_hr, max_hr, hrv, sleep_score, sleep_duration_min, steps, weight_kg) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (d, 52 + i % 3, 170 + i % 5, 45 + i, 70 + i, 420, 8000, 80.0),
        )
    today = now.strftime("%Y-%m-%d")
    start = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    conn.execute(
        "INSERT INTO activities(date, start_time, type, duration_s, avg_hr, max_hr, canonical_source) "
        "VALUES (?,?,?,?,?,?,?)", (today, start, "running", 2700, 150, 172, "garmin"),
    )
    conn.commit()
    conn.close()

    from app.dashboard import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, headers={"Authorization": "Bearer pw"}, follow_redirects=False)


@pytest.mark.parametrize("page", ["", "/trends", "/behavior", "/nutrition", "/activities", "/vitals"])
def test_dashboard_page_renders(client, page):
    r = client.get(f"/dashboard{page}")
    assert r.status_code == 200, r.text[-400:]


def test_dashboard_requires_auth(client):
    r = TestClient(client.app, follow_redirects=False).get("/dashboard")
    assert r.status_code in (302, 307)
