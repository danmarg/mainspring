"""
Google Health API importer — replaces Fitbit Web API (sunset Sep 2026).

Pulls a rolling N-day window and writes:
  raw_import_payloads     (verbatim, before any parsing)
  raw_daily_metrics       (upsert, source='google_health')
  google_health_activities (upsert)

OAuth2 tokens are stored in google_health_oauth and refreshed automatically.
GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set as Fly secrets.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import upsert_raw_metric, upsert_raw_payload, utc_now

log = logging.getLogger(__name__)

SOURCE = "google_health"
WINDOW_DAYS = 7
API_BASE = "https://health.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_EXERCISE_TYPE_MAP = {
    "running": "running",
    "walking": "walking",
    "cycling": "cycling",
    "swimming": "swimming",
    "hiking": "hiking",
    "yoga": "yoga",
    "strength_training": "strength_training",
    "elliptical": "elliptical",
    "rowing": "rowing",
    "high_intensity_interval_training": "hiit",
    "aerobics": "cardio",
    "dancing": "dancing",
    "skiing": "skiing",
    "snowboarding": "snowboarding",
    "soccer": "sport",
    "basketball": "sport",
    "tennis": "sport",
}


def _normalize_activity_type(name: str) -> str:
    return _EXERCISE_TYPE_MAP.get(name.lower().replace(" ", "_"), name.lower())


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def _configured(conn) -> bool:
    if not (os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")):
        return False
    row = conn.execute("SELECT id FROM google_health_oauth LIMIT 1").fetchone()
    return row is not None


def _get_tokens(conn) -> dict | None:
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at FROM google_health_oauth WHERE id=1"
    ).fetchone()
    if not row:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}


def _save_tokens(conn, access_token: str, refresh_token: str, expires_at: str) -> None:
    conn.execute(
        """
        INSERT INTO google_health_oauth(id, access_token, refresh_token, expires_at, updated_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at,
            updated_at=excluded.updated_at
        """,
        (access_token, refresh_token, expires_at, utc_now()),
    )
    conn.commit()


def _refresh(conn, tokens: dict) -> dict:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
    ).isoformat()
    # Google refresh tokens don't expire; keep the existing one if not re-issued
    new_refresh = data.get("refresh_token") or tokens["refresh_token"]
    new_tokens = {
        "access_token": data["access_token"],
        "refresh_token": new_refresh,
        "expires_at": expires_at,
    }
    _save_tokens(conn, new_tokens["access_token"], new_tokens["refresh_token"], expires_at)
    log.info("google_health: tokens refreshed")
    return new_tokens


def _post(conn, path: str, body: dict, tokens: dict) -> Any | None:
    """POST to Google Health API, auto-refreshing on 401."""
    for attempt in range(2):
        req = urllib.request.Request(
            f"{API_BASE}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                tokens = _refresh(conn, tokens)
            elif e.code == 429:
                log.warning("google_health: rate limited on %s", path)
                return None
            elif e.code == 404:
                log.debug("google_health: no data at %s", path)
                return None
            else:
                body_text = e.read().decode(errors="replace")
                log.warning("google_health: %s returned %s: %s", path, e.code, body_text[:200])
                return None
        except Exception as exc:
            log.warning("google_health: %s failed: %s", path, exc)
            return None
    return None


def _civil_time_range(d: date) -> dict:
    """Build an Interval covering a single calendar day (UTC)."""
    next_day = d + timedelta(days=1)
    return {
        "startTime": f"{d.isoformat()}T00:00:00Z",
        "endTime":   f"{next_day.isoformat()}T00:00:00Z",
    }


def _daily_rollup(conn, data_type: str, d: date, tokens: dict) -> Any | None:
    path = f"/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
    return _post(conn, path, {"range": _civil_time_range(d)}, tokens)


def _list_datapoints(conn, data_type: str, d: date, tokens: dict) -> Any | None:
    path = f"/users/me/dataTypes/{data_type}/dataPoints:list"
    next_day = d + timedelta(days=1)
    return _post(conn, path, {
        "startTime": f"{d.isoformat()}T00:00:00Z",
        "endTime":   f"{next_day.isoformat()}T00:00:00Z",
    }, tokens)


# ── parsers ───────────────────────────────────────────────────────────────────

def _first_rollup(data: dict | None) -> dict | None:
    if not data:
        return None
    pts = data.get("rollupDataPoints") or data.get("dataPoints", [])
    return pts[0] if pts else None


def _parse_steps(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    val = pt.get("steps", {}).get("count")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "steps", float(val), utc_now())
    return 1


def _parse_resting_hr(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    val = pt.get("dailyRestingHeartRate", {}).get("beatsPerMinute")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "resting_hr", float(val), utc_now())
    return 1


def _parse_hrv(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    hrv_data = pt.get("dailyHeartRateVariability", {})
    val = hrv_data.get("rmssd") or hrv_data.get("sdnn")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "hrv", float(val), utc_now())
    return 1


def _parse_spo2(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    val = pt.get("dailyOxygenSaturation", {}).get("percentage")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "spo2_avg", float(val), utc_now())
    return 1


def _parse_breathing_rate(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    val = pt.get("dailyRespiratoryRate", {}).get("breathsPerMinute")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "breathing_rate", float(val), utc_now())
    return 1


def _parse_vo2max(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    val = pt.get("dailyVo2Max", {}).get("vo2MaxMillilitersPerMinutePerKilogram")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "vo2max", float(val), utc_now())
    return 1


def _parse_active_minutes(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    val = pt.get("activeMinutes", {}).get("count")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "active_zone_minutes", float(val), utc_now())
    return 1


def _parse_calories(conn, date_str: str, data: dict) -> int:
    pt = _first_rollup(data)
    if not pt:
        return 0
    val = pt.get("activeEnergyBurned", {}).get("kilocalories")
    if val is None:
        return 0
    upsert_raw_metric(conn, date_str, SOURCE, "total_calories", float(val), utc_now())
    return 1


def _parse_sleep(conn, date_str: str, data: dict) -> int:
    """Sleep is a session type — list returns multiple sessions; aggregate the main one."""
    if not data:
        return 0
    sessions = data.get("dataPoints") or data.get("sessions", [])
    if not sessions:
        return 0

    # pick the longest sleep session as "main sleep"
    main = max(sessions, key=lambda s: s.get("durationMs", 0), default=None)
    if not main:
        return 0

    now = utc_now()
    rows = 0
    sleep_data = main.get("sleep", {})

    duration_ms = main.get("durationMs") or sleep_data.get("durationMs")
    if duration_ms:
        upsert_raw_metric(conn, date_str, SOURCE, "sleep_duration_min",
                          float(duration_ms) / 60000, now)
        rows += 1

    stages = sleep_data.get("stages", {})
    stage_map = [
        ("deepSleepDurationMs", "sleep_deep_min"),
        ("remSleepDurationMs",  "sleep_rem_min"),
        ("lightSleepDurationMs", "sleep_light_min"),
    ]
    for key, metric in stage_map:
        val = stages.get(key)
        if val is not None:
            upsert_raw_metric(conn, date_str, SOURCE, metric, float(val) / 60000, now)
            rows += 1

    score = sleep_data.get("sleepScore") or sleep_data.get("efficiency")
    if score is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "sleep_score", float(score), now)
        rows += 1

    return rows


def _upsert_activity(conn, session: dict, date_str: str) -> bool:
    session_id = session.get("sessionId") or session.get("id")
    if not session_id:
        return False

    activity_type = session.get("exercise", {}).get("exerciseType") or session.get("type", "")
    act_type = _normalize_activity_type(str(activity_type))

    start_time = session.get("startTime")
    duration_ms = session.get("durationMs")
    duration_s = int(duration_ms / 1000) if duration_ms else None

    exercise = session.get("exercise", {})
    distance_m = exercise.get("distance", {}).get("meters")
    avg_hr = exercise.get("averageHeartRate", {}).get("beatsPerMinute")
    calories = exercise.get("activeCaloriesBurned", {}).get("kilocalories")

    conn.execute(
        """
        INSERT INTO google_health_activities (
            activity_id, date, start_time, type,
            duration_s, distance_m, avg_hr, calories,
            raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET
            date=excluded.date, start_time=excluded.start_time, type=excluded.type,
            duration_s=excluded.duration_s, distance_m=excluded.distance_m,
            avg_hr=excluded.avg_hr, calories=excluded.calories,
            raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
        """,
        (
            session_id, date_str, start_time, act_type,
            duration_s, distance_m, avg_hr, calories,
            json.dumps(session), utc_now(),
        ),
    )
    return True


# ── main entry point ──────────────────────────────────────────────────────────

def run_import(conn, days: int = WINDOW_DAYS, start_date=None, end_date=None) -> dict:
    if not _configured(conn):
        log.info("Google Health not configured (missing credentials or tokens) — skipping")
        return {"skipped": True, "reason": "credentials not configured"}

    tokens = _get_tokens(conn)
    if not tokens:
        return {"skipped": True, "reason": "no tokens — call /admin/google_health/init_tokens"}

    today = date.today()
    if start_date and end_date:
        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = date.fromisoformat(end_date)
        delta = (end_date - start_date).days + 1
        dates = [start_date + timedelta(days=i) for i in range(delta)]
    else:
        dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    log.info("google_health import: %s → %s (%d days)", dates[0], dates[-1], len(dates))
    rows_upserted = 0

    for d in dates:
        ds = d.isoformat()

        # Daily aggregate data types (dailyRollUp)
        rollup_endpoints = [
            ("steps",                       "steps",          _parse_steps),
            ("daily-resting-heart-rate",    "resting_hr",     _parse_resting_hr),
            ("daily-heart-rate-variability","hrv",            _parse_hrv),
            ("daily-oxygen-saturation",     "spo2",           _parse_spo2),
            ("daily-respiratory-rate",      "breathing_rate", _parse_breathing_rate),
            ("daily-vo2-max",               "vo2max",         _parse_vo2max),
            ("active-minutes",              "active_minutes", _parse_active_minutes),
            ("active-energy-burned",        "calories",       _parse_calories),
        ]

        for data_type, endpoint_name, parser in rollup_endpoints:
            data = _daily_rollup(conn, data_type, d, tokens)
            if data:
                upsert_raw_payload(conn, SOURCE, endpoint_name, json.dumps(data), ds)
                rows_upserted += parser(conn, ds, data)

        # Sleep: session type, use list
        sleep_data = _list_datapoints(conn, "sleep", d, tokens)
        if sleep_data:
            upsert_raw_payload(conn, SOURCE, "sleep", json.dumps(sleep_data), ds)
            rows_upserted += _parse_sleep(conn, ds, sleep_data)

        # Activities: session type, use list
        exercise_data = _list_datapoints(conn, "exercise", d, tokens)
        if exercise_data:
            upsert_raw_payload(conn, SOURCE, "exercise", json.dumps(exercise_data), ds)
            for session in (exercise_data.get("dataPoints") or exercise_data.get("sessions", [])):
                if _upsert_activity(conn, session, ds):
                    rows_upserted += 1

    conn.commit()
    return {
        "skipped": False,
        "rows_upserted": rows_upserted,
        "dates": [d.isoformat() for d in dates],
    }
