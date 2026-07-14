"""
Normalization job — runs after each import.

Rebuilds (in order):
  1. day_timezone   — from device offsets in raw payloads, HOME_TZ fallback
  2. daily_metrics  — wide table from raw_daily_metrics + manual_logs
  3. activities     — deduped from garmin_activities / fitbit_activities
"""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db import HOME_TZ, DEFAULT_SOURCE_PRIORITY, resolve_metric, utc_now

log = logging.getLogger(__name__)

# metrics that go into the wide daily_metrics table, in column order
DAILY_METRIC_COLUMNS = [
    "resting_hr",
    "hrv",
    "sleep_score",
    "sleep_duration_min",
    "sleep_deep_min",
    "sleep_rem_min",
    "sleep_light_min",
    "body_battery_high",
    "body_battery_low",
    "stress_avg",
    "training_readiness",
    "readiness_score",
    "active_zone_minutes",
    "spo2_avg",
    "breathing_rate",
    "vo2max",
    "steps",
]

# ── timezone resolution ─────────────────────────────────────────────────────

def _tz_from_garmin_payload(payload_json: str, endpoint: str) -> str | None:
    """
    Extract a timezone name from a raw Garmin payload.
    Garmin embeds the local UTC offset in several places; we convert the
    fixed offset to the closest IANA zone name for storage.
    """
    try:
        data = json.loads(payload_json)
    except Exception:
        return None

    offset_seconds = None

    if endpoint == "get_sleep_data":
        dto = data.get("dailySleepDTO") or {}
        offset_seconds = dto.get("sleepStartTimestampGMT") and None  # no direct offset here
        tz_str = dto.get("deviceRemSleepData", {}) and None
        # Garmin sometimes includes timezoneOffset in seconds
        offset_seconds = dto.get("timezoneOffset")

    elif endpoint in ("get_stats", "get_training_status", "get_training_readiness"):
        offset_seconds = (
            data.get("timeOffsetSleepData")
            or data.get("timezoneOffset")
        )

    elif endpoint == "activity":
        # activities carry startTimeLocal and startTimeGMT — diff gives offset
        local_str = data.get("startTimeLocal")
        gmt_str = data.get("startTimeGMT")
        if local_str and gmt_str:
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                local_dt = datetime.strptime(local_str.replace("T", " "), fmt)
                gmt_dt = datetime.strptime(gmt_str.replace("T", " "), fmt)
                delta = local_dt - gmt_dt
                offset_seconds = int(delta.total_seconds())
            except Exception:
                pass

    if offset_seconds is None:
        return None

    # Convert fixed offset to an IANA zone name if possible, else fixed offset string
    hours = offset_seconds / 3600
    sign = "+" if hours >= 0 else "-"
    h = int(abs(hours))
    m = int((abs(hours) - h) * 60)
    fixed = f"Etc/GMT{'-' if hours >= 0 else '+'}{h}" if m == 0 else None
    if fixed:
        try:
            ZoneInfo(fixed)
            return fixed
        except ZoneInfoNotFoundError:
            pass
    return f"UTC{sign}{h:02d}:{m:02d}"


def rebuild_day_timezone(conn) -> int:
    """
    For every date in raw_import_payloads, try to derive the local timezone
    from the payload content. Falls back to HOME_TZ when nothing is found.
    Returns number of rows written.
    """
    dates = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT date FROM raw_import_payloads WHERE date IS NOT NULL"
        ).fetchall()
    }
    # also cover dates from raw_daily_metrics
    dates |= {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT date FROM raw_daily_metrics"
        ).fetchall()
    }

    written = 0
    for date_str in sorted(dates):
        tz, source = _resolve_tz_for_date(conn, date_str)
        conn.execute(
            """
            INSERT INTO day_timezone(date, tz, source)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET tz=excluded.tz, source=excluded.source
            """,
            (date_str, tz, source),
        )
        written += 1

    return written


def _resolve_tz_for_date(conn, date_str: str) -> tuple[str, str]:
    # prefer activity payloads (they have both local and GMT timestamps)
    rows = conn.execute(
        "SELECT payload_json, endpoint FROM raw_import_payloads "
        "WHERE date=? AND source='garmin' ORDER BY id DESC",
        (date_str,),
    ).fetchall()

    for payload_json, endpoint in rows:
        tz = _tz_from_garmin_payload(payload_json, endpoint)
        if tz:
            return tz, f"garmin_{endpoint}"

    return HOME_TZ, "home_default"


# ── daily_metrics rebuild ────────────────────────────────────────────────────

def rebuild_daily_metrics(conn) -> int:
    """
    For every date with raw metric data, resolve each column via source_config /
    DEFAULT_SOURCE_PRIORITY, then aggregate manual_logs for caffeine/alcohol/calories.
    """
    dates = {
        row[0]
        for row in conn.execute("SELECT DISTINCT date FROM raw_daily_metrics").fetchall()
    }
    # also include dates that only have manual logs
    dates |= {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT DATE(ts) FROM manual_logs"
        ).fetchall()
        if row[0]
    }

    written = 0
    for date_str in sorted(dates):
        _rebuild_one_day(conn, date_str)
        written += 1

    return written


def _rebuild_one_day(conn, date_str: str) -> None:
    values: dict[str, float | None] = {}
    source_flags: dict[str, str] = {}

    for metric in DAILY_METRIC_COLUMNS:
        val, src = resolve_metric(conn, date_str, metric)
        values[metric] = val
        if src:
            source_flags[metric] = src

    # aggregate manual_logs for the health date
    # use UTC date as approximation (day_timezone lookup is a future refinement)
    caffeine = conn.execute(
        "SELECT SUM(quantity) FROM manual_logs "
        "WHERE type='caffeine' AND DATE(ts)=?",
        (date_str,),
    ).fetchone()[0]

    alcohol = conn.execute(
        "SELECT SUM(quantity) FROM manual_logs "
        "WHERE type='alcohol' AND DATE(ts)=?",
        (date_str,),
    ).fetchone()[0]

    calories = conn.execute(
        "SELECT SUM(estimated_calories) FROM manual_logs "
        "WHERE type='meal' AND DATE(ts)=?",
        (date_str,),
    ).fetchone()[0]

    conn.execute(
        """
        INSERT INTO daily_metrics (
            date,
            resting_hr, hrv,
            sleep_score, sleep_duration_min,
            sleep_deep_min, sleep_rem_min, sleep_light_min,
            body_battery_high, body_battery_low,
            stress_avg, training_readiness,
            readiness_score, active_zone_minutes, spo2_avg, breathing_rate,
            vo2max, steps,
            caffeine_mg, alcohol_units, calories_estimated,
            source_flags_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
            resting_hr=excluded.resting_hr,
            hrv=excluded.hrv,
            sleep_score=excluded.sleep_score,
            sleep_duration_min=excluded.sleep_duration_min,
            sleep_deep_min=excluded.sleep_deep_min,
            sleep_rem_min=excluded.sleep_rem_min,
            sleep_light_min=excluded.sleep_light_min,
            body_battery_high=excluded.body_battery_high,
            body_battery_low=excluded.body_battery_low,
            stress_avg=excluded.stress_avg,
            training_readiness=excluded.training_readiness,
            readiness_score=excluded.readiness_score,
            active_zone_minutes=excluded.active_zone_minutes,
            spo2_avg=excluded.spo2_avg,
            breathing_rate=excluded.breathing_rate,
            vo2max=excluded.vo2max,
            steps=excluded.steps,
            caffeine_mg=excluded.caffeine_mg,
            alcohol_units=excluded.alcohol_units,
            calories_estimated=excluded.calories_estimated,
            source_flags_json=excluded.source_flags_json
        """,
        (
            date_str,
            values.get("resting_hr"),
            values.get("hrv"),
            values.get("sleep_score"),
            values.get("sleep_duration_min"),
            values.get("sleep_deep_min"),
            values.get("sleep_rem_min"),
            values.get("sleep_light_min"),
            values.get("body_battery_high"),
            values.get("body_battery_low"),
            values.get("stress_avg"),
            values.get("training_readiness"),
            values.get("readiness_score"),
            values.get("active_zone_minutes"),
            values.get("spo2_avg"),
            values.get("breathing_rate"),
            values.get("vo2max"),
            values.get("steps"),
            caffeine,
            alcohol,
            calories,
            json.dumps(source_flags),
        ),
    )


# ── activity dedup ───────────────────────────────────────────────────────────

DEDUP_WINDOW_SECONDS = 15 * 60  # ±15 min


def rebuild_activities(conn) -> int:
    """
    Merge garmin_activities and fitbit_activities into the normalized activities
    table. Matches on (date, type, start_time within ±15 min). Where both
    sources have a matching row, the canonical source per source_config wins.
    """
    conn.execute("DELETE FROM activities")

    garmin_rows = conn.execute(
        "SELECT activity_id, date, start_time, type, duration_s, distance_m, "
        "avg_hr, max_hr, calories FROM garmin_activities"
    ).fetchall()

    fitbit_rows = conn.execute(
        "SELECT activity_id, date, start_time, type, duration_s, distance_m, "
        "avg_hr, max_hr, calories FROM fitbit_activities"
    ).fetchall()

    canonical_source_row = conn.execute(
        "SELECT canonical_source FROM source_config WHERE metric='activities'"
    ).fetchone()
    canonical_source = canonical_source_row[0] if canonical_source_row else DEFAULT_SOURCE_PRIORITY[0]

    matched_fitbit_ids: set[str] = set()
    written = 0

    for g in garmin_rows:
        g_id, g_date, g_start, g_type, g_dur, g_dist, g_ahr, g_mhr, g_cal = g
        match_fid = _find_fitbit_match(g_date, g_type, g_start, fitbit_rows)

        if match_fid:
            matched_fitbit_ids.add(match_fid)
            # both sources have it — pick canonical
            if canonical_source == "fitbit":
                fb = next(r for r in fitbit_rows if r[0] == match_fid)
                _insert_activity(conn, fb, "fitbit", g_id, match_fid)
            else:
                _insert_activity(conn, g, "garmin", g_id, match_fid)
        else:
            _insert_activity(conn, g, "garmin", g_id, None)
        written += 1

    # fitbit rows with no Garmin match
    for fb in fitbit_rows:
        if fb[0] not in matched_fitbit_ids:
            _insert_activity(conn, fb, "fitbit", None, fb[0])
            written += 1

    return written


def _parse_start(start_time: str | None) -> datetime | None:
    if not start_time:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(start_time, fmt)
        except ValueError:
            continue
    return None


def _find_fitbit_match(
    date_str: str | None,
    activity_type: str | None,
    start_time: str | None,
    fitbit_rows: list,
) -> str | None:
    g_dt = _parse_start(start_time)

    for fb in fitbit_rows:
        fb_id, fb_date, fb_start, fb_type, *_ = fb
        if fb_date != date_str:
            continue
        if fb_type and activity_type and fb_type.lower() != activity_type.lower():
            continue
        if g_dt is None:
            # same date + same type is good enough when no timestamp
            return fb_id
        fb_dt = _parse_start(fb_start)
        if fb_dt and abs((g_dt - fb_dt).total_seconds()) <= DEDUP_WINDOW_SECONDS:
            return fb_id

    return None


def _insert_activity(conn, row: tuple, source: str, garmin_id: str | None, fitbit_id: str | None) -> None:
    _, date_str, start_time, act_type, dur, dist, ahr, mhr, cal = row
    conn.execute(
        """
        INSERT INTO activities (
            date, start_time, type, duration_s, distance_m,
            avg_hr, max_hr, calories,
            canonical_source, garmin_activity_id, fitbit_activity_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (date_str, start_time, act_type, dur, dist, ahr, mhr, cal,
         source, garmin_id, fitbit_id),
    )


# ── entry point ──────────────────────────────────────────────────────────────

def run_normalization(conn) -> dict:
    tz_rows = rebuild_day_timezone(conn)
    metric_rows = rebuild_daily_metrics(conn)
    activity_rows = rebuild_activities(conn)
    conn.commit()
    return {
        "day_timezone_rows": tz_rows,
        "daily_metric_dates": metric_rows,
        "activity_rows": activity_rows,
    }
