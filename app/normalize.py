"""
Normalization job — runs after each import.

Rebuilds (in order):
  1. day_timezone   — from device offsets in raw payloads, HOME_TZ fallback
  2. daily_metrics  — wide table from raw_daily_metrics + manual_logs
  3. activities     — deduped from garmin_activities / google_health_activities
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
    "active_zone_minutes",
    "spo2_avg",
    "breathing_rate",
    "vo2max",
    "steps",
    "acute_training_load",
    "chronic_training_load",
    "training_load_ratio",
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
    dates = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT date FROM raw_import_payloads WHERE date IS NOT NULL"
        ).fetchall()
    }
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
    dates = {
        row[0]
        for row in conn.execute("SELECT DISTINCT date FROM raw_daily_metrics").fetchall()
    }
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

    caffeine = conn.execute(
        "SELECT SUM(quantity) FROM manual_logs WHERE type='caffeine' AND DATE(ts)=?",
        (date_str,),
    ).fetchone()[0]

    alcohol = conn.execute(
        "SELECT SUM(quantity) FROM manual_logs WHERE type='alcohol' AND DATE(ts)=?",
        (date_str,),
    ).fetchone()[0]

    calories = conn.execute(
        "SELECT SUM(estimated_calories) FROM manual_logs WHERE type='meal' AND DATE(ts)=?",
        (date_str,),
    ).fetchone()[0]

    weight_row = conn.execute(
        "SELECT quantity FROM manual_logs WHERE type='weight' AND DATE(ts)=? ORDER BY ts DESC LIMIT 1",
        (date_str,),
    ).fetchone()
    weight_kg = weight_row[0] if weight_row else None

    bp_row = conn.execute(
        "SELECT estimated_macros_json FROM manual_logs "
        "WHERE type='blood_pressure' AND DATE(ts)=? ORDER BY ts DESC LIMIT 1",
        (date_str,),
    ).fetchone()
    bp_systolic = bp_diastolic = bp_pulse = None
    if bp_row and bp_row[0]:
        try:
            bp = json.loads(bp_row[0])
            bp_systolic = bp.get("systolic")
            bp_diastolic = bp.get("diastolic")
            bp_pulse = bp.get("pulse")
        except Exception:
            pass

    conn.execute(
        """
        INSERT INTO daily_metrics (
            date,
            resting_hr, hrv,
            sleep_score, sleep_duration_min,
            sleep_deep_min, sleep_rem_min, sleep_light_min,
            body_battery_high, body_battery_low,
            stress_avg, training_readiness,
            active_zone_minutes, spo2_avg, breathing_rate,
            vo2max, steps,
            acute_training_load, chronic_training_load, training_load_ratio,
            caffeine_mg, alcohol_units, calories_estimated,
            weight_kg, bp_systolic, bp_diastolic, bp_pulse,
            source_flags_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            active_zone_minutes=excluded.active_zone_minutes,
            spo2_avg=excluded.spo2_avg,
            breathing_rate=excluded.breathing_rate,
            vo2max=excluded.vo2max,
            steps=excluded.steps,
            acute_training_load=excluded.acute_training_load,
            chronic_training_load=excluded.chronic_training_load,
            training_load_ratio=excluded.training_load_ratio,
            caffeine_mg=excluded.caffeine_mg,
            alcohol_units=excluded.alcohol_units,
            calories_estimated=excluded.calories_estimated,
            weight_kg=excluded.weight_kg,
            bp_systolic=excluded.bp_systolic,
            bp_diastolic=excluded.bp_diastolic,
            bp_pulse=excluded.bp_pulse,
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
            values.get("active_zone_minutes"),
            values.get("spo2_avg"),
            values.get("breathing_rate"),
            values.get("vo2max"),
            values.get("steps"),
            values.get("acute_training_load"),
            values.get("chronic_training_load"),
            values.get("training_load_ratio"),
            caffeine,
            alcohol,
            calories,
            weight_kg,
            bp_systolic,
            bp_diastolic,
            bp_pulse,
            json.dumps(source_flags),
        ),
    )


# ── activity dedup ───────────────────────────────────────────────────────────

DEDUP_WINDOW_SECONDS = 15 * 60  # ±15 min


def rebuild_activities(conn) -> int:
    """
    Merge garmin_activities and google_health_activities into the normalized
    activities table. Matches on (date, type, start_time within ±15 min).
    """
    conn.execute("DELETE FROM activities")

    garmin_rows = conn.execute(
        "SELECT activity_id, date, start_time, type, duration_s, distance_m, "
        "avg_hr, max_hr, calories FROM garmin_activities"
    ).fetchall()

    gh_rows = conn.execute(
        "SELECT activity_id, date, start_time, type, duration_s, distance_m, "
        "avg_hr, NULL as max_hr, calories FROM google_health_activities"
    ).fetchall()

    canonical_source_row = conn.execute(
        "SELECT canonical_source FROM source_config WHERE metric='activities'"
    ).fetchone()
    canonical_source = canonical_source_row[0] if canonical_source_row else DEFAULT_SOURCE_PRIORITY[0]

    matched_gh_ids: set[str] = set()
    written = 0

    for g in garmin_rows:
        g_id, g_date, g_start, g_type, *_ = g
        match_ghid = _find_match(g_date, g_type, g_start, gh_rows)

        if match_ghid:
            matched_gh_ids.add(match_ghid)
            if canonical_source == "google_health":
                gh = next(r for r in gh_rows if r[0] == match_ghid)
                _insert_activity(conn, gh, "google_health", g_id, match_ghid)
            else:
                _insert_activity(conn, g, "garmin", g_id, match_ghid)
        else:
            _insert_activity(conn, g, "garmin", g_id, None)
        written += 1

    for gh in gh_rows:
        if gh[0] not in matched_gh_ids:
            _insert_activity(conn, gh, "google_health", None, gh[0])
            written += 1

    return written


def _parse_start(start_time: str | None) -> datetime | None:
    if not start_time:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(start_time, fmt)
        except ValueError:
            continue
    return None


def _find_match(
    date_str: str | None,
    activity_type: str | None,
    start_time: str | None,
    other_rows: list,
) -> str | None:
    g_dt = _parse_start(start_time)
    for row in other_rows:
        r_id, r_date, r_start, r_type, *_ = row
        if r_date != date_str:
            continue
        if r_type and activity_type and r_type.lower() != activity_type.lower():
            continue
        if g_dt is None:
            return r_id
        r_dt = _parse_start(r_start)
        if r_dt and abs((g_dt - r_dt).total_seconds()) <= DEDUP_WINDOW_SECONDS:
            return r_id
    return None


def _insert_activity(conn, row: tuple, source: str, garmin_id: str | None, gh_id: str | None) -> None:
    _, date_str, start_time, act_type, dur, dist, ahr, mhr, cal = row
    conn.execute(
        """
        INSERT INTO activities (
            date, start_time, type, duration_s, distance_m,
            avg_hr, max_hr, calories,
            canonical_source, garmin_activity_id, google_health_activity_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (date_str, start_time, act_type, dur, dist, ahr, mhr, cal,
         source, garmin_id, gh_id),
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
