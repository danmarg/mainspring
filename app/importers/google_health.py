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
from app.importers import _build_date_range

log = logging.getLogger(__name__)

SOURCE = "google_health"
WINDOW_DAYS = 7
API_BASE = "https://health.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"
HTTP_TIMEOUT = 30

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
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                # Mutate in place (not `tokens = _refresh(...)`) so the caller's
                # dict — shared across every endpoint call in run_import's loop —
                # sees the refreshed access token too, instead of re-refreshing
                # from a stale token on every subsequent call this run.
                tokens.update(_refresh(conn, tokens))
            elif e.code == 429:
                log.warning("google_health: rate limited on %s", path)
                return None
            elif e.code == 404:
                log.debug("google_health: no data at %s", path)
                return None
            else:
                body_text = e.read().decode(errors="replace")
                try:
                    err_msg = json.loads(body_text)["error"]["message"]
                except Exception:
                    err_msg = body_text[:300]
                log.warning("google_health: POST %s → %s: %s", path, e.code, err_msg)
                return None
        except Exception as exc:
            log.warning("google_health: %s failed: %s", path, exc)
            return None
    return None


def _get(conn, path: str, params: dict, tokens: dict) -> Any | None:
    """GET from Google Health API, auto-refreshing on 401."""
    for attempt in range(2):
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{API_BASE}{path}?{query}",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                # Mutate in place (not `tokens = _refresh(...)`) so the caller's
                # dict — shared across every endpoint call in run_import's loop —
                # sees the refreshed access token too, instead of re-refreshing
                # from a stale token on every subsequent call this run.
                tokens.update(_refresh(conn, tokens))
            elif e.code == 429:
                log.warning("google_health: rate limited on GET %s", path)
                return None
            elif e.code == 404:
                log.debug("google_health: no data at GET %s", path)
                return None
            else:
                body_text = e.read().decode(errors="replace")
                try:
                    err_msg = json.loads(body_text)["error"]["message"]
                except Exception:
                    err_msg = body_text[:300]
                log.warning("google_health: GET %s → %s: %s", path, e.code, err_msg)
                return None
        except Exception as exc:
            log.warning("google_health: GET %s failed: %s", path, exc)
            return None
    return None


def _civil_time_range(d: date) -> dict:
    """Build a CivilTimeInterval covering a single calendar day."""
    next_day = d + timedelta(days=1)
    return {
        "start": {"date": {"year": d.year,        "month": d.month,        "day": d.day}},
        "end":   {"date": {"year": next_day.year, "month": next_day.month, "day": next_day.day}},
    }


def _daily_rollup(conn, data_type: str, d: date, tokens: dict) -> Any | None:
    path = f"/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
    return _post(conn, path, {"range": _civil_time_range(d)}, tokens)


def _list_datapoints(conn, data_type: str, d: date, tokens: dict) -> Any | None:
    # dataPoints.list is GET with AIP-160 filter.
    # Filter field name = snake_case of the data type (not the kebab-case URL segment).
    # Each kind has different filterable fields:
    #   daily-*    → {type}.date >= / <
    #   sleep      → sleep.interval.civil_end_time (only end_time is filterable for sleep)
    #   exercise   → exercise.interval.civil_start_time
    # heart-rate isn't handled here — see _fetch_intraday_hr_for_date, which
    # needs its own pagination loop rather than this function's single GET.
    path = f"/users/me/dataTypes/{data_type}/dataPoints"
    field = data_type.replace("-", "_")
    next_day = d + timedelta(days=1)
    if data_type.startswith("daily-"):
        f = f'{field}.date >= "{d.isoformat()}" AND {field}.date < "{next_day.isoformat()}"'
    elif data_type == "sleep":
        f = (
            f'sleep.interval.civil_end_time >= "{d.isoformat()}T00:00:00" '
            f'AND sleep.interval.civil_end_time < "{next_day.isoformat()}T00:00:00"'
        )
    else:
        # exercise and any future session types: filter by civil_start_time
        f = (
            f'{field}.interval.civil_start_time >= "{d.isoformat()}T00:00:00" '
            f'AND {field}.interval.civil_start_time < "{next_day.isoformat()}T00:00:00"'
        )
    return _get(conn, path, {"filter": f}, tokens)


def _fetch_and_store_hrv(conn, target_dates: set, tokens: dict, max_pages: int = 20) -> int:
    """
    Page through heart-rate-variability dataPoints newest-first, writing RMSSD values
    to intraday_hrv as each page arrives (no accumulation in memory, no raw payload blob).
    Stops as soon as all points on a page predate the oldest target date.

    max_pages=20 covers ~10-20 recent nights (nightly runs).
    Raise for backfills: each month of history needs ~50-100 pages.
    """
    path = "/users/me/dataTypes/heart-rate-variability/dataPoints"
    cutoff = min(target_dates) - timedelta(days=1)
    cutoff_str = cutoff.isoformat() + "T00:00:00Z"

    rows = 0
    page_token: str | None = None

    for _ in range(max_pages):
        params: dict = {}
        if page_token:
            params["pageToken"] = page_token
        data = _get(conn, path, params, tokens)
        if not data:
            break

        past_window = False
        for pt in (data.get("dataPoints") or []):
            hrv = pt.get("heartRateVariability", {})
            ts_str = hrv.get("sampleTime", {}).get("physicalTime", "")
            if not ts_str:
                continue
            if ts_str < cutoff_str:
                past_window = True
                break
            rmssd = hrv.get("rootMeanSquareOfSuccessiveDifferencesMilliseconds")
            if rmssd is None or rmssd <= 0:
                continue
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                if date(dt.year, dt.month, dt.day) not in target_dates:
                    continue
                ts_key = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                continue
            conn.execute(
                """
                INSERT INTO intraday_hrv(ts, source, rmssd)
                VALUES (?, ?, ?)
                ON CONFLICT(ts, source) DO UPDATE SET rmssd = excluded.rmssd
                """,
                (ts_key, SOURCE, round(rmssd, 2)),
            )
            rows += 1

        # Commit each page rather than holding one long transaction across up to
        # 500 pages (backfills) — otherwise the write lock stays held for however
        # long that pagination takes, and every other writer (MCP logging tools,
        # a concurrently-scheduled Garmin import) silently blocks on it for up to
        # busy_timeout (5min) instead of proceeding.
        conn.commit()

        if past_window:
            break
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return rows


HR_PAGE_SIZE = 5000  # server caps pageSize at 5000 for this type regardless of what's requested
HR_MAX_PAGES = 15    # 15 * 5000 = 75k samples/day — Fitbit's ~2s passive sampling is ~25-40k/day


def _fetch_intraday_hr_for_date(conn, d: date, tokens: dict) -> int:
    """
    Page through a single day's heart-rate dataPoints, writing 1-min bucket
    averages to intraday_hr as each page arrives (no raw payload stored — the
    blob would be huge, same reasoning as _fetch_and_store_hrv).

    Without an explicit pageSize the server defaults to 50 rows for this raw
    sample type — Fitbit's passive HR reporting is roughly 1 sample every 2s,
    so a single default-sized page covers under two minutes of a night, not
    the whole thing. The date filter already scopes the request to this one
    day, so unlike the HRV fetch this doesn't need a manual date cutoff — just
    page until nextPageToken runs out (or the safety cap is hit).
    """
    path = "/users/me/dataTypes/heart-rate/dataPoints"
    next_day = d + timedelta(days=1)
    f = (
        f'heart_rate.sample_time.physical_time >= "{d.isoformat()}T00:00:00Z" '
        f'AND heart_rate.sample_time.physical_time < "{next_day.isoformat()}T00:00:00Z"'
    )

    rows = 0
    page_token: str | None = None
    for _ in range(HR_MAX_PAGES):
        params: dict = {"filter": f, "pageSize": HR_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        data = _get(conn, path, params, tokens)
        if not data:
            break

        rows += _parse_intraday_hr(conn, data)
        conn.commit()

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return rows


def _parse_intraday_hr(conn, data: dict) -> int:
    """
    Parse individual heart-rate sample data points from Health Connect and write
    to intraday_hr at 1-minute resolution (averaging any multiple samples per minute).
    """
    points = data.get("dataPoints") or []
    if not points:
        return 0

    buckets: dict[str, list[float]] = {}
    for pt in points:
        heart_rate = pt.get("heartRate", {})
        ts_str = heart_rate.get("sampleTime", {}).get("physicalTime")
        # beatsPerMinute comes back as a string (e.g. "65"), not a number.
        raw_bpm = heart_rate.get("beatsPerMinute")
        try:
            bpm = float(raw_bpm) if raw_bpm is not None else None
        except (TypeError, ValueError):
            bpm = None
        if not ts_str or bpm is None or bpm <= 0:
            continue
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            # floor to minute
            minute_key = dt.strftime("%Y-%m-%dT%H:%M:00Z")
            buckets.setdefault(minute_key, []).append(float(bpm))
        except Exception:
            continue

    rows = 0
    for minute_key, bpms in buckets.items():
        conn.execute(
            """
            INSERT INTO intraday_hr(ts, source, bpm)
            VALUES (?, ?, ?)
            ON CONFLICT(ts, source) DO UPDATE SET bpm = excluded.bpm
            """,
            (minute_key, SOURCE, round(sum(bpms) / len(bpms), 1)),
        )
        rows += 1
    return rows


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
    val = pt.get("dailyOxygenSaturation", {}).get("averagePercentage")
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


def _parse_skin_temp(conn, date_str: str, data: dict) -> int:
    """dailyRollUp doesn't cover this type; dataPoints.list returns one point per
    night under dailySleepTemperatureDerivations, giving the night's absolute
    skin temp plus the device's own rolling baseline — deviation is the
    difference, not a field the API hands back directly."""
    if not data:
        return 0
    points = data.get("dataPoints") or []
    if not points:
        return 0

    deviations = []
    for pt in points:
        record = pt.get("dailySleepTemperatureDerivations")
        if not record:
            continue
        nightly = record.get("nightlyTemperatureCelsius")
        baseline = record.get("baselineTemperatureCelsius")
        if nightly is not None and baseline is not None:
            deviations.append(nightly - baseline)

    if not deviations:
        return 0
    avg_deviation = sum(deviations) / len(deviations)
    # Sanity guard: this column means degrees from personal baseline (a couple
    # degrees C at most), not an absolute skin temperature (~30-35C). Skip
    # rather than write a value that reads as a false illness signal every day.
    if abs(avg_deviation) < 10:
        upsert_raw_metric(conn, date_str, SOURCE, "skin_temp_deviation", float(avg_deviation), utc_now())
        return 1
    return 0


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


def _session_duration_ms(session: dict) -> float:
    """Compute session duration in ms from interval.startTime/endTime (actual API format)
    or durationMs fallback (older assumed format)."""
    sleep = session.get("sleep", {})
    interval = sleep.get("interval", {})
    try:
        s = datetime.fromisoformat(interval["startTime"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(interval["endTime"].replace("Z", "+00:00"))
        return max(0.0, (e - s).total_seconds() * 1000)
    except (KeyError, ValueError, TypeError):
        pass
    return float(session.get("durationMs") or sleep.get("durationMs") or 0)


def _parse_sleep(conn, date_str: str, data: dict) -> int:
    """Sleep is a session type — list returns multiple sessions; aggregate the main one."""
    if not data:
        return 0
    sessions = data.get("dataPoints") or data.get("sessions", [])
    if not sessions:
        return 0

    # pick the longest sleep session as "main sleep"
    main = max(sessions, key=_session_duration_ms, default=None)
    if not main:
        return 0

    now = utc_now()
    rows = 0
    sleep_data = main.get("sleep", {})
    dur_ms = _session_duration_ms(main)

    if dur_ms:
        upsert_raw_metric(conn, date_str, SOURCE, "sleep_duration_min",
                          dur_ms / 60000, now)
        rows += 1

    # Extract wake hour from interval.endTime
    interval = sleep_data.get("interval", {})
    end_str = interval.get("endTime")
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            wake_hour = end_dt.hour + end_dt.minute / 60
            upsert_raw_metric(conn, date_str, SOURCE, "sleep_wake_hour", wake_hour, now)
            rows += 1
        except (ValueError, TypeError):
            pass

    # stages: actual format is [{startTime, endTime, type}, ...]; durationMs is a fallback
    stages_raw = sleep_data.get("stages", [])
    if isinstance(stages_raw, dict):
        stage_totals = {k.upper(): float(v) for k, v in stages_raw.items()}
    else:
        stage_totals: dict[str, float] = {}
        for s in stages_raw:
            t = str(s.get("type", "")).upper()
            try:
                s_dt = datetime.fromisoformat(s["startTime"].replace("Z", "+00:00"))
                e_dt = datetime.fromisoformat(s["endTime"].replace("Z", "+00:00"))
                stage_ms = max(0.0, (e_dt - s_dt).total_seconds() * 1000)
            except (KeyError, ValueError, TypeError):
                stage_ms = float(s.get("durationMs") or 0)
            stage_totals[t] = stage_totals.get(t, 0) + stage_ms

    stage_map = [
        ("DEEP",  "sleep_deep_min"),
        ("REM",   "sleep_rem_min"),
        ("LIGHT", "sleep_light_min"),
        ("AWAKE", "sleep_awake_min"),
    ]
    for stage_key, metric in stage_map:
        val = stage_totals.get(stage_key)
        if val:
            upsert_raw_metric(conn, date_str, SOURCE, metric, val / 60000, now)
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

    dates = _build_date_range(days, start_date, end_date)

    log.info("google_health import: %s → %s (%d days)", dates[0], dates[-1], len(dates))
    rows_upserted = 0

    # Intraday HR: not subject to late corrections, so limit to last 2 days on
    # default rolling runs to reduce API load.
    hr_dates = set(dates) if (start_date or end_date) else set(dates[-2:])

    for d in dates:
        ds = d.isoformat()

        # Raw sample types — aggregate via dailyRollUp
        rollup_endpoints = [
            ("steps",              "steps",          _parse_steps),
            ("active-minutes",     "active_minutes", _parse_active_minutes),
            ("active-energy-burned","calories",      _parse_calories),
        ]
        for data_type, endpoint_name, parser in rollup_endpoints:
            data = _daily_rollup(conn, data_type, d, tokens)
            if data:
                upsert_raw_payload(conn, SOURCE, endpoint_name, json.dumps(data), ds)
                rows_upserted += parser(conn, ds, data)

        # daily-* types are already one record per day — use list, not dailyRollUp
        list_daily_endpoints = [
            ("daily-resting-heart-rate",     "resting_hr",     _parse_resting_hr),
            ("daily-heart-rate-variability", "hrv",            _parse_hrv),
            ("daily-oxygen-saturation",      "spo2",           _parse_spo2),
            ("daily-respiratory-rate",       "breathing_rate", _parse_breathing_rate),
            ("daily-vo2-max",                "vo2max",         _parse_vo2max),
        ]
        for data_type, endpoint_name, parser in list_daily_endpoints:
            data = _list_datapoints(conn, data_type, d, tokens)
            if data:
                upsert_raw_payload(conn, SOURCE, endpoint_name, json.dumps(data), ds)
                rows_upserted += parser(conn, ds, data)

        # Sleep: session type, use list
        sleep_data = _list_datapoints(conn, "sleep", d, tokens)
        if sleep_data:
            upsert_raw_payload(conn, SOURCE, "sleep", json.dumps(sleep_data), ds)
            rows_upserted += _parse_sleep(conn, ds, sleep_data)

        # Skin temperature: daily-list type (compatible devices only)
        skin_temp_data = _list_datapoints(conn, "daily-sleep-temperature-derivations", d, tokens)
        if skin_temp_data:
            upsert_raw_payload(conn, SOURCE, "skin_temperature", json.dumps(skin_temp_data), ds)
            rows_upserted += _parse_skin_temp(conn, ds, skin_temp_data)

        # Activities: session type, use list
        exercise_data = _list_datapoints(conn, "exercise", d, tokens)
        if exercise_data:
            upsert_raw_payload(conn, SOURCE, "exercise", json.dumps(exercise_data), ds)
            for session in (exercise_data.get("dataPoints") or exercise_data.get("sessions", [])):
                if _upsert_activity(conn, session, ds):
                    rows_upserted += 1

        # Intraday HR: individual heart-rate samples (Fitbit writes these to Health Connect).
        # Paginated separately from _list_datapoints — see _fetch_intraday_hr_for_date.
        if d in hr_dates:
            rows_upserted += _fetch_intraday_hr_for_date(conn, d, tokens)

        # Commit per day rather than holding one open write transaction across
        # the whole chunk (each day is several sequential blocking HTTP calls —
        # a backfill chunk can take minutes). See _fetch_and_store_hrv for the
        # same reasoning applied to its own page loop below.
        conn.commit()

    # Intraday HRV (RMSSD): heart-rate-variability doesn't support date filters, so we
    # page newest-first, writing to DB as each page arrives (no in-memory accumulation).
    # No raw payload stored — data is already in intraday_hrv and the blob would be huge.
    if hr_dates:
        hrv_max_pages = 500 if (start_date or end_date) else 20
        rows_upserted += _fetch_and_store_hrv(conn, hr_dates, tokens, max_pages=hrv_max_pages)

    conn.commit()
    return {
        "skipped": False,
        "rows_upserted": rows_upserted,
        "dates": [d.isoformat() for d in dates],
    }
