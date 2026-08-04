"""
Normalization job — runs after each import.

Rebuilds (in order):
  1. day_timezone   — from device offsets in raw payloads, HOME_TZ fallback
  2. daily_metrics  — wide table from raw_daily_metrics + manual_logs
  3. activities     — deduped from garmin_activities / google_health_activities
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db import HOME_TZ, DEFAULT_SOURCE_PRIORITY, resolve_metric, utc_now

log = logging.getLogger(__name__)

# raw_import_payloads is append-only and upsert_raw_payload() already dedupes
# identical re-fetches, but genuine revisions (e.g. Garmin correcting a day's
# data over several re-fetches before it exits the rolling window) still
# accumulate. Prune anything older than this as a growth backstop.
RAW_PAYLOAD_RETENTION_DAYS = 180

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
    "skin_temp_deviation",
    "hydration_ml",
    "max_hr",
    "lactate_threshold_hr",
    "lactate_threshold_pace_min_per_km",
    "ftp_watts",
    "sleep_breathing_rate",
    "recovery_hours",
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


def rebuild_day_timezone(conn, dates: set[str] | None = None) -> int:
    if dates is None:
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


# ── synthetic sleep score (fallback when no vendor score is available) ─────────
#
# Backtested against 171 days where both Garmin's real sleep_score and Google
# Health's stage breakdown were present: raw composite of duration-adequacy,
# sleep efficiency, deep%, and rem% correlates ~0.51 with Garmin's score.
# A linear recalibration (fit against that history) brings held-out MAE to
# ~5.5 points, against a Garmin score stdev of ~8 — close enough to land in
# the right readiness band most days. Slope/intercept below are from that fit;
# re-run the backtest (see scratchpad) if the formula's inputs change.
_SYNTH_SLOPE = 0.374
_SYNTH_INTERCEPT = 41.8


def synthesize_sleep_score(
    duration_min: float | None,
    deep_min: float | None,
    rem_min: float | None,
    awake_min: float | None,
) -> float | None:
    """Estimate a sleep score from duration + stage composition, calibrated
    against Garmin's real sleep_score on days where both were available."""
    if not duration_min or duration_min <= 0:
        return None

    awake_min = awake_min or 0.0
    deep_min = deep_min or 0.0
    rem_min = rem_min or 0.0

    time_in_bed = duration_min + awake_min
    efficiency = duration_min / time_in_bed if time_in_bed > 0 else 0.85
    eff_score = max(0.0, min(1.0, (efficiency - 0.70) / 0.25))

    hours = duration_min / 60.0
    if hours <= 7.5:
        dur_score = max(0.0, min(1.0, (hours - 4.0) / 3.5))
    else:
        dur_score = max(0.0, min(1.0, 1.0 - (hours - 7.5) / 3.0))

    deep_score = max(0.0, min(1.0, (deep_min / duration_min) / 0.20))
    rem_score = max(0.0, min(1.0, (rem_min / duration_min) / 0.22))

    composite = 0.35 * dur_score + 0.30 * eff_score + 0.20 * deep_score + 0.15 * rem_score
    raw = composite * 100
    return max(0.0, min(100.0, _SYNTH_SLOPE * raw + _SYNTH_INTERCEPT))


# ── resting HR from raw intraday samples (source-agnostic) ──────────────────
#
# Vendors don't agree on what "resting heart rate" means: Garmin computes an
# overnight minimum, while Google Health/Health Connect's dailyRestingHeartRate
# is closer to waking RHR and reads ~10bpm higher for the same person on the
# same night — so picking whichever source is "canonical" per source_config
# still produces a day-to-day discontinuity if the canonical source ever
# changes, or an apples-to-oranges comparison across a Garmin-then-Fitbit
# history. Raw 1-min HR samples in intraday_hr don't have that problem — both
# importers write the same kind of measurement — so compute RHR ourselves as
# the mean of the lowest 5% of samples in the overnight window, instead of
# trusting either vendor's own summarization.
RESTING_HR_MIN_SAMPLES = 60  # ~1h of 1-min samples; below this, distrust the estimate


def _overnight_utc_bounds(date_str: str) -> tuple[str, str]:
    """UTC bounds for previous-evening-8pm through this-morning-10am, local HOME_TZ.
    Shared by the RHR and HRV intraday-recompute helpers below."""
    try:
        tz = ZoneInfo(HOME_TZ)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    d = date.fromisoformat(date_str)
    window_start = datetime(d.year, d.month, d.day, 20, 0, tzinfo=tz) - timedelta(days=1)
    window_end = datetime(d.year, d.month, d.day, 10, 0, tzinfo=tz)
    return (
        window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _resting_hr_from_intraday(conn, date_str: str) -> tuple[float | None, str | None]:
    """Falls back to (None, None) — most historical dates won't have intraday HR,
    since both importers only pull it for the last ~2 days on rolling runs."""
    utc_start, utc_end = _overnight_utc_bounds(date_str)

    # Pick whichever source has the most coverage in the window, not just the
    # first source in DEFAULT_SOURCE_PRIORITY to clear the sample threshold —
    # a full night is ~300-400 1-min buckets, and an hour spent at a desk
    # shouldn't win over a full night's data from a lower-priority source.
    best: tuple[str, list[float]] | None = None
    for source in DEFAULT_SOURCE_PRIORITY:
        rows = conn.execute(
            "SELECT bpm FROM intraday_hr WHERE source=? AND ts >= ? AND ts < ? ORDER BY bpm",
            (source, utc_start, utc_end),
        ).fetchall()
        bpms = [r[0] for r in rows]
        if len(bpms) >= RESTING_HR_MIN_SAMPLES and (best is None or len(bpms) > len(best[1])):
            best = (source, bpms)

    if best is None:
        return None, None

    source, bpms = best
    lowest_n = max(1, len(bpms) // 20)  # lowest 5%
    avg = sum(bpms[:lowest_n]) / lowest_n
    return round(avg, 1), f"{source}_intraday"


# ── HRV from raw intraday samples (Google Health's daily rollup is all-day) ──
#
# Garmin's "hrv" is already overnight-only (hrvSummary.lastNight — see
# _parse_hrv in garmin.py), but Google Health/Health Connect's
# daily-heart-rate-variability rollup is a 24h aggregate, not sleep-window-only.
# Google Health separately provides real per-sample overnight RMSSD via
# _fetch_and_store_hrv into intraday_hrv, so when Google Health's daily rollup
# is what resolve_metric picked, recompute from those samples instead — same
# fix as _resting_hr_from_intraday, applied to whichever side actually needs it
# (Garmin's own figure is left untouched; it's already correct).
HRV_MIN_SAMPLES = 20  # ~100min at Google's ~5min sample spacing


def _hrv_overnight_from_intraday(conn, date_str: str, source: str) -> float | None:
    utc_start, utc_end = _overnight_utc_bounds(date_str)
    rows = conn.execute(
        "SELECT rmssd FROM intraday_hrv WHERE source=? AND ts >= ? AND ts < ?",
        (source, utc_start, utc_end),
    ).fetchall()
    values = [r[0] for r in rows]
    if len(values) < HRV_MIN_SAMPLES:
        return None
    return round(sum(values) / len(values), 1)


# ── HR zones (derived from max_hr, source-agnostic) ─────────────────────────
#
# Standard 5-zone %HRmax model (Zone 1 recovery through Zone 5 max/VO2max),
# rather than any single vendor's proprietary zone-boundary endpoint — this
# works from a max_hr value regardless of which source produced it.
_HR_ZONE_BANDS = [
    (1, 0.50, 0.60),
    (2, 0.60, 0.70),
    (3, 0.70, 0.80),
    (4, 0.80, 0.90),
    (5, 0.90, 1.00),
]


def compute_hr_zones(max_hr: float) -> list[dict]:
    return [
        {"zone": zone, "min_bpm": round(max_hr * lo), "max_bpm": round(max_hr * hi)}
        for zone, lo, hi in _HR_ZONE_BANDS
    ]


MAX_HR_ROLLING_WINDOW_DAYS = 90


def _rolling_max_hr(conn, date_str: str) -> float | None:
    """max_hr is the day's *observed* peak (see _parse_stats in garmin.py) — on a
    rest day that's ~100-115bpm, not a meaningful ceiling for zone bands. Roll
    forward the highest observed value over a trailing window instead, same
    principle as field-test-derived max HR: it should only ever ratchet up
    within the window, not reflect a single easy day."""
    row = conn.execute(
        """SELECT MAX(max_hr) FROM daily_metrics
           WHERE date <= ? AND date > date(?, ?) AND max_hr IS NOT NULL""",
        (date_str, date_str, f"-{MAX_HR_ROLLING_WINDOW_DAYS} days"),
    ).fetchone()
    return row[0] if row else None


def _rebuild_hr_zones(conn, date_str: str, todays_max_hr: float | None) -> None:
    # _rebuild_hr_zones runs before today's own row is committed to daily_metrics,
    # so the rolling query can't see it yet — fold it in explicitly so a new high
    # observed today still ratchets the zones up immediately, not next run.
    rolling = _rolling_max_hr(conn, date_str)
    candidates = [v for v in (rolling, todays_max_hr) if v]
    max_hr = max(candidates) if candidates else None
    if not max_hr or max_hr <= 0:
        return
    for band in compute_hr_zones(max_hr):
        conn.execute(
            """
            INSERT INTO hr_zones(date, source, zone, min_bpm, max_bpm)
            VALUES (?, 'derived', ?, ?, ?)
            ON CONFLICT(date, source, zone) DO UPDATE SET
                min_bpm=excluded.min_bpm, max_bpm=excluded.max_bpm
            """,
            (date_str, band["zone"], band["min_bpm"], band["max_bpm"]),
        )


# ── daily_metrics rebuild ────────────────────────────────────────────────────

def rebuild_daily_metrics(conn, dates: set[str] | None = None) -> int:
    if dates is None:
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
        if written % 50 == 0:
            conn.commit()

    return written


def _rebuild_one_day(conn, date_str: str) -> None:
    values: dict[str, float | None] = {}
    source_flags: dict[str, str] = {}

    for metric in DAILY_METRIC_COLUMNS:
        val, src = resolve_metric(conn, date_str, metric)
        values[metric] = val
        if src:
            source_flags[metric] = src

    # Prefer resting_hr computed from raw intraday samples over either vendor's own
    # summarization (see _resting_hr_from_intraday) — falls back to whatever
    # resolve_metric already found above when too few intraday samples exist.
    computed_rhr, computed_rhr_src = _resting_hr_from_intraday(conn, date_str)
    if computed_rhr is not None:
        values["resting_hr"] = computed_rhr
        source_flags["resting_hr"] = computed_rhr_src

    # If Google Health's all-day HRV rollup is what got picked above, recompute
    # from overnight-only intraday samples instead (see _hrv_overnight_from_intraday).
    # Garmin's own hrv is already overnight-only and left as resolved.
    if source_flags.get("hrv") == "google_health":
        overnight_hrv = _hrv_overnight_from_intraday(conn, date_str, "google_health")
        if overnight_hrv is not None:
            values["hrv"] = overnight_hrv
            source_flags["hrv"] = "google_health_intraday"

    # Acute/chronic training load: Garmin writes these as raw metrics "atl"/"ctl"
    # (dailyTrainingLoadAcute/Chronic), not under the daily_metrics column names.
    # training_load_ratio (ACWR) isn't provided directly — derive it from the two.
    atl_val, atl_src = resolve_metric(conn, date_str, "atl")
    ctl_val, ctl_src = resolve_metric(conn, date_str, "ctl")
    values["acute_training_load"] = atl_val
    values["chronic_training_load"] = ctl_val
    if atl_src:
        source_flags["acute_training_load"] = atl_src
    if ctl_src:
        source_flags["chronic_training_load"] = ctl_src
    if atl_val is not None and ctl_val is not None and ctl_val > 0:
        values["training_load_ratio"] = round(atl_val / ctl_val, 3)
        source_flags["training_load_ratio"] = "derived"
    else:
        values["training_load_ratio"] = None

    _rebuild_hr_zones(conn, date_str, values.get("max_hr"))

    if values.get("sleep_score") is None:
        awake_min, _ = resolve_metric(conn, date_str, "sleep_awake_min")
        synthetic = synthesize_sleep_score(
            values.get("sleep_duration_min"),
            values.get("sleep_deep_min"),
            values.get("sleep_rem_min"),
            awake_min,
        )
        if synthetic is not None:
            values["sleep_score"] = round(synthetic, 1)
            source_flags["sleep_score"] = "synthetic"

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

    # Manual hydration logs are pushed back to Garmin Connect (push_hydration), so
    # Garmin's own total will include them again on the next import. Take the max
    # of the two rather than either summing (double-counts once the re-import
    # lands) or overriding (throws away Garmin's total once it does): before
    # push-back the manual value is the larger/more current one and wins; after
    # push-back Garmin's total already includes it and is >= the manual sum.
    manual_hydration = conn.execute(
        "SELECT SUM(quantity) FROM manual_logs WHERE type='hydration' AND DATE(ts)=?",
        (date_str,),
    ).fetchone()[0]
    if manual_hydration is not None:
        raw_hydration = values.get("hydration_ml")
        if raw_hydration is None or manual_hydration > raw_hydration:
            values["hydration_ml"] = manual_hydration
            source_flags["hydration_ml"] = "manual"
        # else: raw value already reflects (or exceeds) the manual log; keep it
        # and its existing source flag

    weight_row = conn.execute(
        "SELECT quantity FROM manual_logs WHERE type='weight' AND DATE(ts)=? ORDER BY ts DESC LIMIT 1",
        (date_str,),
    ).fetchone()
    weight_kg = weight_row[0] if weight_row else None
    if weight_kg is None:
        weight_kg, weight_src = resolve_metric(conn, date_str, "weight_kg")
        if weight_src:
            source_flags["weight_kg"] = weight_src
    else:
        source_flags["weight_kg"] = "manual"

    rpe_row = conn.execute(
        "SELECT quantity FROM manual_logs WHERE type='rpe' AND DATE(ts)=? ORDER BY ts DESC LIMIT 1",
        (date_str,),
    ).fetchone()
    rpe = rpe_row[0] if rpe_row else None

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
    if bp_systolic is None:
        # all-three-or-nothing: a device reading is one measurement event,
        # so don't mix a manual systolic with a device diastolic/pulse.
        bp_sys_val, bp_src = resolve_metric(conn, date_str, "bp_systolic")
        bp_dia_val, _ = resolve_metric(conn, date_str, "bp_diastolic")
        bp_pulse_val, _ = resolve_metric(conn, date_str, "bp_pulse")
        if bp_sys_val is not None and bp_dia_val is not None and bp_pulse_val is not None:
            bp_systolic, bp_diastolic, bp_pulse = bp_sys_val, bp_dia_val, bp_pulse_val
            source_flags["bp"] = bp_src
    else:
        source_flags["bp"] = "manual"

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
            rpe,
            skin_temp_deviation, hydration_ml, max_hr,
            lactate_threshold_hr, lactate_threshold_pace_min_per_km, ftp_watts,
            sleep_breathing_rate, recovery_hours,
            source_flags_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            rpe=excluded.rpe,
            skin_temp_deviation=excluded.skin_temp_deviation,
            hydration_ml=excluded.hydration_ml,
            max_hr=excluded.max_hr,
            lactate_threshold_hr=excluded.lactate_threshold_hr,
            lactate_threshold_pace_min_per_km=excluded.lactate_threshold_pace_min_per_km,
            ftp_watts=excluded.ftp_watts,
            sleep_breathing_rate=excluded.sleep_breathing_rate,
            recovery_hours=excluded.recovery_hours,
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
            rpe,
            values.get("skin_temp_deviation"),
            values.get("hydration_ml"),
            values.get("max_hr"),
            values.get("lactate_threshold_hr"),
            values.get("lactate_threshold_pace_min_per_km"),
            values.get("ftp_watts"),
            values.get("sleep_breathing_rate"),
            values.get("recovery_hours"),
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
        "avg_hr, max_hr, calories, decoupling_pct FROM garmin_activities"
    ).fetchall()

    gh_rows = conn.execute(
        "SELECT activity_id, date, start_time, type, duration_s, distance_m, "
        "avg_hr, NULL as max_hr, calories, NULL as decoupling_pct FROM google_health_activities"
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
    _, date_str, start_time, act_type, dur, dist, ahr, mhr, cal, decoupling_pct = row
    conn.execute(
        """
        INSERT INTO activities (
            date, start_time, type, duration_s, distance_m,
            avg_hr, max_hr, calories, decoupling_pct,
            canonical_source, garmin_activity_id, google_health_activity_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (date_str, start_time, act_type, dur, dist, ahr, mhr, cal, decoupling_pct,
         source, garmin_id, gh_id),
    )


def prune_raw_payloads(conn, retention_days: int = RAW_PAYLOAD_RETENTION_DAYS) -> int:
    """Delete raw_import_payloads rows older than the retention window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cur = conn.execute("DELETE FROM raw_import_payloads WHERE fetched_at < ?", (cutoff,))
    return cur.rowcount


# ── entry point ──────────────────────────────────────────────────────────────

def run_normalization(conn, dates: set[str] | None = None) -> dict:
    tz_rows = rebuild_day_timezone(conn, dates)
    metric_rows = rebuild_daily_metrics(conn, dates)
    activity_rows = rebuild_activities(conn)
    pruned_rows = prune_raw_payloads(conn)
    conn.commit()
    return {
        "day_timezone_rows": tz_rows,
        "daily_metric_dates": metric_rows,
        "activity_rows": activity_rows,
        "pruned_raw_payload_rows": pruned_rows,
    }
