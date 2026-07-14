"""
Garmin importer — pulls a rolling N-day window and writes:
  raw_import_payloads  (verbatim, before any parsing)
  raw_daily_metrics    (upsert)
  garmin_activities    (upsert)
  suggested_workouts   (upsert)

Each API call is wrapped so a single endpoint failure doesn't abort the run.
"""

import json
import logging
import os
from datetime import date, timedelta
from typing import Any

from app.db import upsert_raw_metric, upsert_raw_payload, utc_now

log = logging.getLogger(__name__)

SOURCE = "garmin"
WINDOW_DAYS = 7


def _configured() -> bool:
    """True if any auth credentials are present."""
    return bool(
        os.getenv("GARMINTOKENS")
        or (os.getenv("GARMIN_EMAIL") and os.getenv("GARMIN_PASSWORD"))
    )


def _client():
    """
    Return an authenticated Garmin client.

    Prefers GARMINTOKENS (serialized session, no MFA needed).
    Falls back to GARMIN_EMAIL + GARMIN_PASSWORD (triggers SSO/MFA on first
    run; subsequent runs should use stored tokens).

    GARMINTOKENS is read automatically by garminconnect.login() from the env,
    so we only need to pass email/password when tokens are absent.
    """
    from garminconnect import Garmin

    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")
    client = Garmin(email=email, password=password)
    client.login()  # uses GARMINTOKENS env var automatically if set
    return client


def _safe(fn, label: str) -> Any | None:
    try:
        return fn()
    except Exception as exc:
        log.warning("garmin endpoint %s failed: %s", label, exc)
        return None


def _store_raw(conn, endpoint: str, data: Any, date_str: str | None = None) -> None:
    upsert_raw_payload(
        conn,
        source=SOURCE,
        endpoint=endpoint,
        payload_json=json.dumps(data),
        date=date_str,
        fetched_at=utc_now(),
    )


def _parse_stats(conn, date_str: str, data: dict) -> int:
    rows = 0
    now = utc_now()
    mapping = {
        "restingHeartRate": "resting_hr",
        "totalSteps": "steps",
        "totalKilocalories": "total_calories",
        "activeKilocalories": "active_calories",
        "floorsAscended": "floors_ascended",
        "floorsDescended": "floors_descended",
    }
    for api_key, metric in mapping.items():
        val = data.get(api_key)
        if val is not None:
            upsert_raw_metric(conn, date_str, SOURCE, metric, float(val), now)
            rows += 1
    return rows


def _parse_hrv(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    rows = 0
    # lastNight is the primary summary; weekly avg also available
    last_night = data.get("hrvSummary") or data.get("lastNight") or {}
    val = last_night.get("lastNight") or last_night.get("weeklyAvg")
    if val is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "hrv", float(val), now)
        rows += 1
    status = last_night.get("status")
    if status:
        # store as a string metric — normalization job can interpret
        upsert_raw_metric(conn, date_str, SOURCE, "hrv_status",
                          None, now)  # status is categorical; skip numeric store
    return rows


def _parse_sleep(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    rows = 0
    daily = data.get("dailySleepDTO") or data.get("sleepMovement") or data
    score_obj = data.get("dailySleepDTO", {})
    if isinstance(score_obj, dict):
        score = score_obj.get("sleepScores", {})
        if isinstance(score, dict):
            overall = score.get("overall", {})
            val = overall.get("value") if isinstance(overall, dict) else overall
        else:
            val = score_obj.get("sleepScore") or score_obj.get("averageSpO2Value")
        if val is not None:
            upsert_raw_metric(conn, date_str, SOURCE, "sleep_score", float(val), now)
            rows += 1
        for api_key, metric in {
            "sleepTimeSeconds": ("sleep_duration_min", 1 / 60),
            "deepSleepSeconds": ("sleep_deep_min", 1 / 60),
            "lightSleepSeconds": ("sleep_light_min", 1 / 60),
            "remSleepSeconds": ("sleep_rem_min", 1 / 60),
            "awakeSleepSeconds": ("sleep_awake_min", 1 / 60),
        }.items():
            metric_name, scale = metric
            raw_val = score_obj.get(api_key)
            if raw_val is not None:
                upsert_raw_metric(conn, date_str, SOURCE, metric_name,
                                  float(raw_val) * scale, now)
                rows += 1
    return rows


def _parse_stress(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    rows = 0
    avg = data.get("avgStressLevel")
    mx = data.get("maxStressLevel")
    if avg is not None and avg >= 0:
        upsert_raw_metric(conn, date_str, SOURCE, "stress_avg", float(avg), now)
        rows += 1
    if mx is not None and mx >= 0:
        upsert_raw_metric(conn, date_str, SOURCE, "stress_max", float(mx), now)
        rows += 1
    return rows


def _parse_body_battery(conn, date_str: str, data: list) -> int:
    now = utc_now()
    rows = 0
    charged = [
        item.get("charged")
        for item in data
        if isinstance(item, dict) and item.get("charged") is not None
    ]
    drained = [
        item.get("drained")
        for item in data
        if isinstance(item, dict) and item.get("drained") is not None
    ]
    if charged:
        upsert_raw_metric(conn, date_str, SOURCE, "body_battery_high", float(max(charged)), now)
        rows += 1
    if drained:
        upsert_raw_metric(conn, date_str, SOURCE, "body_battery_low", float(min(drained)), now)
        rows += 1
    return rows


def _parse_training_readiness(conn, date_str: str, data: list | dict) -> int:
    now = utc_now()
    rows = 0
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        score = item.get("score") or item.get("trainingReadinessScore")
        if score is not None:
            upsert_raw_metric(conn, date_str, SOURCE, "training_readiness", float(score), now)
            rows += 1
            break
    return rows


def _parse_training_status(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    rows = 0
    # VO2max lives in the training status response
    vo2 = (
        data.get("latestVO2Max")
        or data.get("mostRecentVO2MaxRunning")
        or data.get("vo2MaxPreciseValue")
    )
    if vo2 is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "vo2max", float(vo2), now)
        rows += 1

    # Acute/chronic training load
    load_obj = data.get("trainingLoadBalance") or {}
    acute = load_obj.get("acuteLoad") or data.get("acuteTrainingLoad")
    chronic = load_obj.get("chronicLoad") or data.get("chronicTrainingLoad")
    ratio = load_obj.get("loadRatio") or data.get("trainingLoadRatio")
    for metric, val in [
        ("acute_training_load", acute),
        ("chronic_training_load", chronic),
        ("training_load_ratio", ratio),
    ]:
        if val is not None:
            upsert_raw_metric(conn, date_str, SOURCE, metric, float(val), now)
            rows += 1
    return rows


def _upsert_activity(conn, activity: dict) -> bool:
    activity_id = str(activity.get("activityId", ""))
    if not activity_id:
        return False

    start_time = activity.get("startTimeLocal") or activity.get("startTimeGMT")
    date_str = start_time[:10] if start_time else None

    conn.execute(
        """
        INSERT INTO garmin_activities (
            activity_id, date, start_time, type,
            duration_s, distance_m, avg_hr, max_hr,
            training_effect_aerobic, training_effect_anaerobic,
            calories, raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET
            date=excluded.date, start_time=excluded.start_time, type=excluded.type,
            duration_s=excluded.duration_s, distance_m=excluded.distance_m,
            avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,
            training_effect_aerobic=excluded.training_effect_aerobic,
            training_effect_anaerobic=excluded.training_effect_anaerobic,
            calories=excluded.calories,
            raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
        """,
        (
            activity_id,
            date_str,
            start_time,
            activity.get("activityType", {}).get("typeKey") if isinstance(activity.get("activityType"), dict) else activity.get("activityType"),
            int(activity.get("duration") or activity.get("elapsedDuration") or 0) or None,
            activity.get("distance"),
            activity.get("averageHR"),
            activity.get("maxHR"),
            activity.get("aerobicTrainingEffect"),
            activity.get("anaerobicTrainingEffect"),
            activity.get("calories"),
            json.dumps(activity),
            utc_now(),
        ),
    )
    return True


def _upsert_suggested_workout(conn, date_str: str, data: dict) -> bool:
    conn.execute(
        """
        INSERT INTO suggested_workouts (
            date, source, workout_type, description,
            target_duration_min, target_intensity, raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(date, source) DO UPDATE SET
            workout_type=excluded.workout_type,
            description=excluded.description,
            target_duration_min=excluded.target_duration_min,
            target_intensity=excluded.target_intensity,
            raw_json=excluded.raw_json,
            fetched_at=excluded.fetched_at
        """,
        (
            date_str,
            SOURCE,
            data.get("workoutType") or data.get("sportType"),
            data.get("workoutDescription") or data.get("description"),
            data.get("durationInSeconds", 0) / 60 or None,
            data.get("intensityType") or data.get("targetIntensity"),
            json.dumps(data),
            utc_now(),
        ),
    )
    return True


def run_import(conn, days: int = WINDOW_DAYS, start_date=None, end_date=None) -> dict:
    if not _configured():
        log.info("No Garmin credentials configured (GARMINTOKENS or GARMIN_EMAIL+PASSWORD) — skipping")
        return {"skipped": True, "reason": "credentials not configured"}

    client = _client()

    today = date.today()
    if start_date and end_date:
        # explicit range — convert to date objects if passed as strings
        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = date.fromisoformat(end_date)
        delta = (end_date - start_date).days + 1
        dates = [start_date + timedelta(days=i) for i in range(delta)]
    else:
        dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    start_str = dates[0].isoformat()
    end_str = dates[-1].isoformat()

    rows_upserted = 0
    log.info("garmin import: %s → %s (%d days)", start_str, end_str, len(dates))

    # ── per-day endpoints ───────────────────────────────────────────────────
    for d in dates:
        ds = d.isoformat()

        # daily stats (steps, resting HR, calories, floors)
        data = _safe(lambda: client.get_stats(ds), f"get_stats({ds})")
        if data:
            _store_raw(conn, "get_stats", data, ds)
            rows_upserted += _parse_stats(conn, ds, data)

        # HRV
        data = _safe(lambda: client.get_hrv_data(ds), f"get_hrv_data({ds})")
        if data:
            _store_raw(conn, "get_hrv_data", data, ds)
            rows_upserted += _parse_hrv(conn, ds, data)

        # sleep (provider-assigned night-of date; we trust it as-is)
        data = _safe(lambda: client.get_sleep_data(ds), f"get_sleep_data({ds})")
        if data:
            _store_raw(conn, "get_sleep_data", data, ds)
            rows_upserted += _parse_sleep(conn, ds, data)

        # stress
        data = _safe(lambda: client.get_stress_data(ds), f"get_stress_data({ds})")
        if data:
            _store_raw(conn, "get_stress_data", data, ds)
            rows_upserted += _parse_stress(conn, ds, data)

        # training readiness
        data = _safe(lambda: client.get_training_readiness(ds), f"get_training_readiness({ds})")
        if data:
            _store_raw(conn, "get_training_readiness", data, ds)
            rows_upserted += _parse_training_readiness(conn, ds, data)

        # training status (VO2max, training load)
        data = _safe(lambda: client.get_training_status(ds), f"get_training_status({ds})")
        if data:
            _store_raw(conn, "get_training_status", data, ds)
            rows_upserted += _parse_training_status(conn, ds, data)

    # ── range endpoints ─────────────────────────────────────────────────────

    # body battery (returns a list across the range)
    data = _safe(
        lambda: client.get_body_battery(start_str, end_str),
        f"get_body_battery({start_str},{end_str})",
    )
    if data:
        _store_raw(conn, "get_body_battery", data)
        # body battery response is a list of dicts, each with a date key
        for item in (data if isinstance(data, list) else [data]):
            item_date = item.get("date") or item.get("calendarDate")
            if item_date:
                _store_raw(conn, "get_body_battery_item", item, item_date)
                rows_upserted += _parse_body_battery(conn, item_date, [item])

    # activities
    data = _safe(
        lambda: client.get_activities_by_date(start_str, end_str),
        f"get_activities_by_date({start_str},{end_str})",
    )
    if data:
        activities = data if isinstance(data, list) else data.get("activityList", [])
        _store_raw(conn, "get_activities_by_date", activities)
        for activity in activities:
            _store_raw(conn, "activity", activity,
                       (activity.get("startTimeLocal") or "")[:10] or None)
            if _upsert_activity(conn, activity):
                rows_upserted += 1

    conn.commit()
    return {"skipped": False, "rows_upserted": rows_upserted, "dates": [d.isoformat() for d in dates]}
