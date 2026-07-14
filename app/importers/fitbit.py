"""
Fitbit importer — pulls a rolling N-day window and writes:
  raw_import_payloads  (verbatim, before any parsing)
  raw_daily_metrics    (upsert)
  fitbit_activities    (upsert)

OAuth2 tokens are stored in the fitbit_oauth table and refreshed automatically.
FITBIT_CLIENT_ID and FITBIT_CLIENT_SECRET must be set as env vars / Fly secrets.
"""

import base64
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

SOURCE = "fitbit"
WINDOW_DAYS = 7
API_BASE = "https://api.fitbit.com"

_ACTIVITY_TYPE_MAP = {
    "run": "running",
    "walk": "walking",
    "bike": "cycling",
    "outdoor bike": "cycling",
    "swim": "swimming",
    "hike": "hiking",
    "yoga": "yoga",
    "weights": "strength_training",
    "circuit training": "strength_training",
    "sport": "sport",
    "elliptical": "elliptical",
    "rowing": "rowing",
}


def _normalize_activity_type(name: str) -> str:
    return _ACTIVITY_TYPE_MAP.get(name.lower(), name.lower())


# ── OAuth helpers ────────────────────────────────────────────────────────────

def _configured(conn) -> bool:
    if not (os.getenv("FITBIT_CLIENT_ID") and os.getenv("FITBIT_CLIENT_SECRET")):
        return False
    row = conn.execute("SELECT id FROM fitbit_oauth LIMIT 1").fetchone()
    return row is not None


def _get_tokens(conn) -> dict | None:
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at FROM fitbit_oauth WHERE id=1"
    ).fetchone()
    if not row:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}


def _save_tokens(conn, access_token: str, refresh_token: str, expires_at: str) -> None:
    conn.execute(
        """
        INSERT INTO fitbit_oauth(id, access_token, refresh_token, expires_at, updated_at)
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
    client_id = os.getenv("FITBIT_CLIENT_ID", "")
    client_secret = os.getenv("FITBIT_CLIENT_SECRET", "")
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }).encode()

    req = urllib.request.Request(
        f"{API_BASE}/oauth2/token",
        data=body,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
    ).isoformat()
    new_tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": expires_at,
    }
    _save_tokens(conn, new_tokens["access_token"], new_tokens["refresh_token"], expires_at)
    log.info("fitbit: tokens refreshed")
    return new_tokens


def _get(conn, path: str, tokens: dict) -> Any | None:
    """GET from Fitbit API, auto-refreshing on 401."""
    for attempt in range(2):
        req = urllib.request.Request(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                tokens = _refresh(conn, tokens)
            elif e.code == 429:
                log.warning("fitbit: rate limited on %s", path)
                return None
            else:
                log.warning("fitbit: %s returned %s", path, e.code)
                return None
        except Exception as exc:
            log.warning("fitbit: %s failed: %s", path, exc)
            return None
    return None


# ── parsers ──────────────────────────────────────────────────────────────────

def _parse_activities(conn, date_str: str, data: dict) -> int:
    summary = data.get("summary", {})
    now = utc_now()
    rows = 0
    mapping = {
        "steps": ("steps", 1),
        "caloriesOut": ("total_calories", 1),
        "activeScore": ("active_score", 1),
    }
    for api_key, (metric, scale) in mapping.items():
        val = summary.get(api_key)
        if val is not None:
            upsert_raw_metric(conn, date_str, SOURCE, metric, float(val) * scale, now)
            rows += 1

    return rows


def _parse_heart(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    # response: {"activities-heart": [{"value": {"restingHeartRate": N, ...}, "dateTime": "..."}]}
    entries = data.get("activities-heart", [])
    if not entries:
        return 0
    rhr = entries[0].get("value", {}).get("restingHeartRate")
    if rhr is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "resting_hr", float(rhr), now)
        return 1
    return 0


def _parse_azm(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    # response: {"activities-active-zone-minutes": [{"value": {"activeZoneMinutes": N}, "dateTime": "..."}]}
    entries = data.get("activities-active-zone-minutes", [])
    if not entries:
        return 0
    azm = entries[0].get("value", {}).get("activeZoneMinutes")
    if azm is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "active_zone_minutes", float(azm), now)
        return 1
    return 0


def _parse_sleep(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    rows = 0
    summary = data.get("summary", {})
    stages = summary.get("stages", {})

    total_min = summary.get("totalMinutesAsleep")
    if total_min is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "sleep_duration_min", float(total_min), now)
        rows += 1

    for stage_key, metric in [("deep", "sleep_deep_min"), ("rem", "sleep_rem_min"), ("light", "sleep_light_min")]:
        val = stages.get(stage_key)
        if val is not None:
            upsert_raw_metric(conn, date_str, SOURCE, metric, float(val), now)
            rows += 1

    # Sleep score: use efficiency from the main sleep entry if available
    sleeps = data.get("sleep", [])
    main_sleep = next((s for s in sleeps if s.get("isMainSleep")), sleeps[0] if sleeps else None)
    if main_sleep:
        eff = main_sleep.get("efficiency")
        if eff is not None:
            upsert_raw_metric(conn, date_str, SOURCE, "sleep_score", float(eff), now)
            rows += 1

    return rows


def _parse_hrv(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    entries = data.get("hrv", [])
    if not entries:
        return 0
    val = entries[0].get("value", {}).get("dailyRmssd") or entries[0].get("value", {}).get("deepRmssd")
    if val is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "hrv", float(val), now)
        return 1
    return 0



def _parse_spo2(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    # response is either {"value": {"avg": N}} or {"minutes": [...]}
    avg = None
    if "value" in data:
        avg = data["value"].get("avg")
    elif "minutes" in data:
        vals = [m["value"] for m in data["minutes"] if "value" in m]
        avg = sum(vals) / len(vals) if vals else None
    if avg is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "spo2_avg", float(avg), now)
        return 1
    return 0


def _parse_breathing_rate(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    entries = data.get("br", [])
    if not entries:
        return 0
    rate = entries[0].get("value", {}).get("breathingRate")
    if rate is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "breathing_rate", float(rate), now)
        return 1
    return 0


def _parse_cardioscore(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    entries = data.get("cardioScore", [])
    if not entries:
        return 0
    vo2 = entries[0].get("value", {}).get("vo2Max")
    if vo2 is None:
        return 0
    # Fitbit returns a range string like "42-46"; take the lower bound
    if isinstance(vo2, str) and "-" in vo2:
        vo2 = float(vo2.split("-")[0])
    try:
        upsert_raw_metric(conn, date_str, SOURCE, "vo2max", float(vo2), now)
        return 1
    except (ValueError, TypeError):
        return 0


def _upsert_activity(conn, activity: dict, date_str: str) -> bool:
    log_id = str(activity.get("logId", ""))
    if not log_id:
        return False

    activity_name = activity.get("activityName") or activity.get("name") or ""
    act_type = _normalize_activity_type(activity_name)

    start_time = activity.get("startTime") or activity.get("originalStartTime")
    duration_ms = activity.get("duration") or activity.get("activeDuration")
    duration_s = int(duration_ms / 1000) if duration_ms else None

    # distance: Fitbit returns in user's unit system; store raw (km or miles)
    # convert to meters: check activityLevel or distanceUnit header isn't available here,
    # so store as-is with a note; analysis layer can normalise
    distance = activity.get("distance")
    distance_m = float(distance) * 1000 if distance is not None else None  # assume km

    conn.execute(
        """
        INSERT INTO fitbit_activities (
            activity_id, date, start_time, type,
            duration_s, distance_m, avg_hr, max_hr, calories,
            raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET
            date=excluded.date, start_time=excluded.start_time, type=excluded.type,
            duration_s=excluded.duration_s, distance_m=excluded.distance_m,
            avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,
            calories=excluded.calories,
            raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
        """,
        (
            log_id, date_str, start_time, act_type,
            duration_s, distance_m,
            activity.get("averageHeartRate"),
            activity.get("maxHeartRate"),
            activity.get("calories"),
            json.dumps(activity),
            utc_now(),
        ),
    )
    return True


# ── main entry point ─────────────────────────────────────────────────────────

def run_import(conn, days: int = WINDOW_DAYS, start_date=None, end_date=None) -> dict:
    if not _configured(conn):
        log.info("Fitbit not configured (missing credentials or tokens) — skipping")
        return {"skipped": True, "reason": "credentials not configured"}

    tokens = _get_tokens(conn)
    if not tokens:
        return {"skipped": True, "reason": "no tokens stored — call /admin/fitbit/init_tokens"}

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

    log.info("fitbit import: %s → %s (%d days)", dates[0], dates[-1], len(dates))
    rows_upserted = 0

    for d in dates:
        ds = d.isoformat()

        endpoints = [
            (f"/1/user/-/activities/date/{ds}.json", "activities", _parse_activities),
            (f"/1/user/-/activities/heart/date/{ds}/1d.json", "heart", _parse_heart),
            (f"/1/user/-/activities/active-zone-minutes/date/{ds}/1d.json", "azm", _parse_azm),
            (f"/1/user/-/sleep/date/{ds}.json", "sleep", _parse_sleep),
            (f"/1/user/-/hrv/date/{ds}.json", "hrv", _parse_hrv),
            (f"/1/user/-/spo2/date/{ds}.json", "spo2", _parse_spo2),
            (f"/1/user/-/breathing-rate/date/{ds}.json", "breathing_rate", _parse_breathing_rate),
            (f"/1/user/-/cardioscore/date/{ds}.json", "cardioscore", _parse_cardioscore),
        ]

        for path, endpoint_name, parser in endpoints:
            data = _get(conn, path, tokens)
            if data:
                upsert_raw_payload(conn, SOURCE, endpoint_name, json.dumps(data), ds)
                rows_upserted += parser(conn, ds, data)

        # activities from the daily summary
        acts_data = _get(conn, f"/1/user/-/activities/date/{ds}.json", tokens)
        if acts_data:
            for activity in acts_data.get("activities", []):
                upsert_raw_payload(conn, SOURCE, "activity", json.dumps(activity), ds)
                if _upsert_activity(conn, activity, ds):
                    rows_upserted += 1

    conn.commit()
    return {
        "skipped": False,
        "rows_upserted": rows_upserted,
        "dates": [d.isoformat() for d in dates],
    }
