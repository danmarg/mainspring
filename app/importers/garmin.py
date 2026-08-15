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
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import upsert_raw_metric, upsert_raw_payload, utc_now
from app.importers import _build_date_range

log = logging.getLogger(__name__)

SOURCE = "garmin"
WINDOW_DAYS = 7
CLIENT_CACHE_SECONDS = 30 * 60
PUSH_RETRIES = 3
PUSH_RETRY_BACKOFF_SECONDS = 2

_cached_client = None
_cached_client_at = 0.0


def _configured() -> bool:
    """True if any auth credentials are present."""
    return bool(
        os.getenv("GARMINTOKENS")
        or (os.getenv("GARMIN_EMAIL") and os.getenv("GARMIN_PASSWORD"))
    )


def _login() -> Any:
    """Perform a fresh Garmin login (the actual network round-trip).

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


def _client(force_refresh: bool = False) -> Any:
    """Return an authenticated Garmin client, reusing a cached session for
    CLIENT_CACHE_SECONDS. Garmin's unofficial auth endpoint is the flakiest
    part of this integration, so avoiding a fresh login() on every single
    manual push (log_hydration/log_weight/log_blood_pressure) meaningfully
    cuts failure exposure — most calls just reuse the in-process session."""
    global _cached_client, _cached_client_at
    now = time.monotonic()
    if (
        force_refresh
        or _cached_client is None
        or (now - _cached_client_at) > CLIENT_CACHE_SECONDS
    ):
        _cached_client = _login()
        _cached_client_at = now
    return _cached_client


def _with_retry(fn, label: str) -> bool:
    """Run a Garmin write (fn takes a client), retrying transient failures
    with backoff. On failure, drops the cached client so the next attempt
    re-authenticates rather than retrying against a possibly-stale session."""
    global _cached_client
    last_exc: Exception | None = None
    for attempt in range(1, PUSH_RETRIES + 1):
        try:
            client = _client(force_refresh=(attempt > 1))
            fn(client)
            return True
        except Exception as exc:
            last_exc = exc
            _cached_client = None
            if attempt < PUSH_RETRIES:
                log.warning(
                    "garmin %s failed (attempt %d/%d), retrying: %s",
                    label, attempt, PUSH_RETRIES, exc,
                )
                time.sleep(PUSH_RETRY_BACKOFF_SECONDS * attempt)
    log.warning("garmin %s failed after %d attempts: %s", label, PUSH_RETRIES, last_exc)
    return False


def _safe(fn, label: str) -> Any | None:
    try:
        return fn()
    except Exception as exc:
        log.warning("garmin endpoint %s failed: %s", label, exc)
        return None


def _as_utc_iso(ts: str | None) -> str:
    """Normalize a possibly-naive ISO timestamp to an explicit UTC offset,
    since garminconnect's write endpoints interpret a naive timestamp as
    local server time, not UTC."""
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def push_weight(kg: float, ts: str | None = None) -> bool:
    """Write a weigh-in to Garmin Connect (e.g. from a manual MCP log_weight
    call), so it shows up alongside device-synced weigh-ins. Best-effort:
    returns False (without raising) if Garmin isn't configured or the push
    fails, so callers can log locally regardless of Garmin's availability."""
    if not _configured():
        return False
    return _with_retry(
        lambda client: client.add_weigh_in(weight=kg, unitKey="kg", timestamp=_as_utc_iso(ts)),
        "push_weight",
    )


def push_blood_pressure(
    systolic: int, diastolic: int, pulse: int, ts: str | None = None, notes: str = ""
) -> bool:
    """Write a blood pressure reading to Garmin Connect. Best-effort, same
    contract as push_weight. Garmin's set_blood_pressure requires pulse, so
    callers without a pulse reading should skip calling this."""
    if not _configured():
        return False
    return _with_retry(
        lambda client: client.set_blood_pressure(
            systolic=systolic, diastolic=diastolic, pulse=pulse,
            timestamp=_as_utc_iso(ts), notes=notes,
        ),
        "push_blood_pressure",
    )


def push_hydration(ml: float, ts: str | None = None) -> bool:
    """Write a hydration reading to Garmin Connect (e.g. from a manual MCP
    log_hydration call). Best-effort, same contract as push_weight."""
    if not _configured():
        return False
    return _with_retry(
        lambda client: client.add_hydration_data(value_in_ml=ml, timestamp=_as_utc_iso(ts)),
        "push_hydration",
    )


def push_pending_manual_logs(conn) -> int:
    """Push manual_logs rows not yet synced to Garmin (hydration/weight/
    blood_pressure logged via MCP). Run at the start of each import so
    pushes piggyback on that run's login instead of blocking the MCP tool
    call — a failed push here just gets retried on the next import cycle.
    Returns the number of rows successfully pushed."""
    if not _configured():
        return 0
    rows = conn.execute(
        "SELECT id, type, ts, quantity, estimated_macros_json FROM manual_logs "
        "WHERE garmin_synced_at IS NULL AND type IN ('hydration', 'weight', 'blood_pressure')"
    ).fetchall()
    pushed = 0
    for log_id, log_type, ts, quantity, macros_json in rows:
        if log_type == "hydration":
            ok = push_hydration(quantity, ts)
        elif log_type == "weight":
            ok = push_weight(quantity, ts)
        else:  # blood_pressure
            macros = json.loads(macros_json) if macros_json else {}
            pulse = macros.get("pulse")
            if pulse is None:
                # No pulse reading (legacy rows from before log_blood_pressure skipped
                # queuing these) — Garmin's endpoint requires pulse, so this can never
                # succeed. Mark synced so it stops retrying every import forever.
                conn.execute(
                    "UPDATE manual_logs SET garmin_synced_at=? WHERE id=?", (utc_now(), log_id)
                )
                continue
            ok = push_blood_pressure(macros["systolic"], macros["diastolic"], pulse, ts)
        if ok:
            conn.execute(
                "UPDATE manual_logs SET garmin_synced_at=? WHERE id=?", (utc_now(), log_id)
            )
            pushed += 1
    if pushed:
        conn.commit()
    return pushed


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
        # Day's observed peak HR, used as a rolling proxy for max_hr (HR-zone calc)
        # rather than a fixed profile setting — it drifts with fitness/effort, same
        # tradeoff sports scientists accept with field-test-derived max HR generally.
        "maxHeartRate": "max_hr",
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
        # Wake time: sleepEndTimestampLocal is epoch ms in local time
        wake_ts_ms = score_obj.get("sleepEndTimestampLocal")
        if wake_ts_ms is not None:
            try:
                from datetime import timezone as _tz
                wake_dt = datetime.fromtimestamp(int(wake_ts_ms) / 1000, tz=_tz.utc)
                wake_hour = wake_dt.hour + wake_dt.minute / 60
                upsert_raw_metric(conn, date_str, SOURCE, "sleep_wake_hour", wake_hour, now)
                rows += 1
            except (TypeError, ValueError, OSError):
                pass
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

        # Sleep-specific respiration, distinct from the all-day average that
        # _parse_respiration writes to "breathing_rate" — this one only covers the
        # sleep window and is the more useful trend for illness/recovery disruption.
        sleep_resp = score_obj.get("avgSleepRespirationValue") or score_obj.get("averageRespirationValue")
        if sleep_resp is not None:
            upsert_raw_metric(conn, date_str, SOURCE, "sleep_breathing_rate", float(sleep_resp), now)
            rows += 1

        # Skin temperature deviation (deg C from personal baseline) — only present
        # for compatible devices (Venu 3, epix Pro, fenix 7 Pro and newer). Field
        # names are best-effort against an undocumented endpoint; check
        # raw_import_payloads.get_sleep_data if this never populates.
        skin_temp_list = data.get("skinTempDataDTOList") or score_obj.get("skinTempDataDTOList")
        if skin_temp_list:
            readings = [
                r.get("skinTempCelsius") or r.get("deviation") or r.get("celsius")
                for r in skin_temp_list if isinstance(r, dict)
            ]
            readings = [r for r in readings if r is not None]
            if readings:
                avg_deviation = sum(readings) / len(readings)
                # Sanity guard: skin_temp_deviation means degrees from personal
                # baseline (expected within a couple degrees C either way). If the
                # field actually held an absolute skin temperature (~30-35C) rather
                # than a delta — plausible given the field-name uncertainty above —
                # writing it as-is would read as a wild illness signal every day.
                # Skip rather than risk that; check raw_import_payloads to fix the
                # field mapping if this is silently never populating.
                if abs(avg_deviation) < 10:
                    upsert_raw_metric(conn, date_str, SOURCE, "skin_temp_deviation", float(avg_deviation), now)
                    rows += 1
    return rows


def _ts_from_epoch_ms(ms: int) -> str:
    """Convert Garmin epoch-ms (true UTC) to ISO-8601 second-precision UTC string."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_intraday_hr(conn, data: dict) -> int:
    """Aggregate 2-min Garmin HR samples into 1-min buckets and upsert to intraday_hr."""
    values = data.get("heartRateValues") or []
    if not values:
        return 0

    buckets: dict[int, list[float]] = {}
    for entry in values:
        ts_ms, bpm = entry
        if bpm is None or bpm <= 0:
            continue
        minute_ms = (ts_ms // 60000) * 60000
        buckets.setdefault(minute_ms, []).append(float(bpm))

    rows = 0
    for minute_ms, bpms in buckets.items():
        conn.execute(
            """
            INSERT INTO intraday_hr(ts, source, bpm)
            VALUES (?, ?, ?)
            ON CONFLICT(ts, source) DO UPDATE SET bpm = excluded.bpm
            """,
            (_ts_from_epoch_ms(minute_ms), SOURCE, round(sum(bpms) / len(bpms), 1)),
        )
        rows += 1
    return rows


def _parse_intraday_stress_ts(conn, data: dict) -> int:
    """Write stressValuesArray to intraday_stress, filtering out unmeasured/activity markers."""
    values = data.get("stressValuesArray") or []
    rows = 0
    for entry in values:
        ts_ms, stress = entry
        if stress < 0:  # -1 = unmeasured, -2 = during activity
            continue
        conn.execute(
            """
            INSERT INTO intraday_stress(ts, source, stress)
            VALUES (?, ?, ?)
            ON CONFLICT(ts, source) DO UPDATE SET stress = excluded.stress
            """,
            (_ts_from_epoch_ms(ts_ms), SOURCE, float(stress)),
        )
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
    rows += _parse_intraday_stress_ts(conn, data)
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
        # Recovery time is one of the named factors behind the readiness score
        # (alongside sleep/HRV/training-load history) and rides along in the same
        # response item; field name is best-effort (undocumented API) — check
        # raw_import_payloads.get_training_readiness if this doesn't populate.
        recovery = item.get("recoveryTime") or item.get("recoveryTimeHours")
        if recovery is not None:
            upsert_raw_metric(conn, date_str, SOURCE, "recovery_hours", float(recovery), now)
            rows += 1
        if score is not None:
            break
    return rows


def _parse_training_status(conn, date_str: str, data: dict) -> int:
    now = utc_now()
    rows = 0
    # VO2max — API returns mostRecentVO2Max as a nested object, not a scalar:
    #   {"generic": {"vo2MaxPreciseValue": 55.8, "vo2MaxValue": 56, ...}, ...}
    vo2_raw = data.get("mostRecentVO2Max")
    if isinstance(vo2_raw, dict):
        generic = vo2_raw.get("generic") or {}
        vo2 = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
    else:
        # flat response format (some firmware versions)
        vo2 = vo2_raw or data.get("latestVO2Max") or data.get("vo2MaxPreciseValue")
    if vo2 is not None:
        try:
            upsert_raw_metric(conn, date_str, SOURCE, "vo2max", float(vo2), now)
            rows += 1
        except (TypeError, ValueError):
            pass

    # Monthly zone load from mostRecentTrainingLoadBalance
    mlb = data.get("mostRecentTrainingLoadBalance") or {}
    dmap = mlb.get("metricsTrainingLoadBalanceDTOMap") or {}
    entry: dict | None = None
    for v in dmap.values():
        if isinstance(v, dict) and (v.get("primaryTrainingDevice") or entry is None):
            entry = v
    if entry:
        for metric, key in [
            ("monthly_load_aerobic_low",  "monthlyLoadAerobicLow"),
            ("monthly_load_aerobic_high", "monthlyLoadAerobicHigh"),
            ("monthly_load_anaerobic",    "monthlyLoadAnaerobic"),
        ]:
            val = entry.get(key)
            if val is not None:
                upsert_raw_metric(conn, date_str, SOURCE, metric, float(val), now)
                rows += 1

    # ATL/CTL from mostRecentTrainingStatus → latestTrainingStatusData[device].acuteTrainingLoadDTO
    mts = data.get("mostRecentTrainingStatus") or {}
    ts_dmap = mts.get("latestTrainingStatusData") or {}
    ts_entry: dict | None = None
    for v in ts_dmap.values():
        if isinstance(v, dict) and (v.get("primaryTrainingDevice") or ts_entry is None):
            ts_entry = v
    if ts_entry:
        atl_dto = ts_entry.get("acuteTrainingLoadDTO") or {}
        atl = atl_dto.get("dailyTrainingLoadAcute")
        ctl = atl_dto.get("dailyTrainingLoadChronic")
        if atl is not None:
            upsert_raw_metric(conn, date_str, SOURCE, "atl", float(atl), now)
            rows += 1
        if ctl is not None:
            upsert_raw_metric(conn, date_str, SOURCE, "ctl", float(ctl), now)
            rows += 1

    return rows


def _parse_spo2(conn, date_str: str, data: dict | list) -> int:
    now = utc_now()
    rows = 0
    # get_spo2_data returns either a dict with averages or a list of readings
    if isinstance(data, list):
        vals = [
            item.get("averageSpO2") or item.get("spo2Reading")
            for item in data
            if isinstance(item, dict)
        ]
        vals = [v for v in vals if v is not None]
        val = sum(vals) / len(vals) if vals else None
    else:
        val = (
            data.get("averageSpO2")
            or data.get("avgSpO2")
            or data.get("averageSpO2Value")
        )
    if val is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "spo2_avg", float(val), now)
        rows += 1
    return rows


def _parse_respiration(conn, date_str: str, data: dict | list) -> int:
    now = utc_now()
    rows = 0
    if isinstance(data, list):
        vals = [
            item.get("avgBreathingRate") or item.get("breathingRate")
            for item in data
            if isinstance(item, dict)
        ]
        vals = [v for v in vals if v is not None]
        val = sum(vals) / len(vals) if vals else None
    else:
        val = (
            data.get("avgBreathingRate")
            or data.get("averageBreathingRate")
            or data.get("breathingRate")
        )
    if val is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "breathing_rate", float(val), now)
        rows += 1
    return rows


def _parse_hydration(conn, date_str: str, data: dict) -> int:
    """add_hydration_data's write body uses key 'valueInML'; get_hydration_data
    is presumed symmetric (unverified — undocumented endpoint)."""
    now = utc_now()
    val = data.get("valueInML") or data.get("totalValueInML")
    if val is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "hydration_ml", float(val), now)
        return 1
    return 0


def _parse_lactate_threshold(conn, date_str: str, data: dict) -> int:
    """get_lactate_threshold(latest=True) returns a confirmed shape (see library
    source): {"speed_and_heart_rate": {"speed": m/s, "heartRate": bpm, ...}, "power": {...}}.
    This is a point-in-time snapshot (Garmin updates it occasionally, not daily),
    so it's written once per import run at the most recent date, not per-day."""
    now = utc_now()
    rows = 0
    shr = data.get("speed_and_heart_rate") or {}
    hr = shr.get("heartRate")
    if hr is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "lactate_threshold_hr", float(hr), now)
        rows += 1
    speed = shr.get("speed")
    if speed:
        pace_min_per_km = 1000 / speed / 60
        upsert_raw_metric(conn, date_str, SOURCE, "lactate_threshold_pace_min_per_km",
                          round(pace_min_per_km, 2), now)
        rows += 1
    return rows


def _parse_ftp(conn, date_str: str, data: dict | list) -> int:
    """get_cycling_ftp() shape is unconfirmed (no source docstring detail beyond
    dict|list); tries the field names used by its sibling lactate-threshold power
    endpoint plus generic fallbacks. Snapshot, written once per run like LT above."""
    now = utc_now()
    entry = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
    val = (
        entry.get("functionalThresholdPower")
        or entry.get("value")
        or entry.get("ftp")
    )
    if val is not None:
        upsert_raw_metric(conn, date_str, SOURCE, "ftp_watts", float(val), now)
        return 1
    return 0


def _parse_intensity_minutes(conn, date_str: str, data: dict | list) -> int:
    now = utc_now()
    rows = 0
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    moderate = data.get("moderateIntensityMinutes") or data.get("moderateMinutes") or 0
    vigorous = data.get("vigorousIntensityMinutes") or data.get("vigorousMinutes") or 0
    # WHO/Garmin formula: vigorous counts double
    total = float(moderate) + 2 * float(vigorous)
    if total > 0:
        upsert_raw_metric(conn, date_str, SOURCE, "active_zone_minutes", total, now)
        rows += 1
    return rows


def _parse_body_composition(conn, date_str: str, data: dict) -> int:
    """Extract same-day weigh-in weight from get_daily_weigh_ins(ds).
    Response shape (confirmed via garminconnect's own delete_weigh_ins, which
    reads dateWeightList[].samplePk): {"dateWeightList": [{"weight": ..., ...}]}.
    The API's weight units aren't documented; Garmin's raw values are grams,
    so branch on magnitude rather than assume, to avoid a silent 1000x error."""
    now = utc_now()
    entries = data.get("dateWeightList") or []
    if not entries:
        return 0
    try:
        raw = entries[0].get("weight")
        if raw is None:
            return 0
        raw = float(raw)
    except (TypeError, ValueError):
        return 0
    kg = raw / 1000.0 if raw > 500 else raw
    upsert_raw_metric(conn, date_str, SOURCE, "weight_kg", kg, now)
    return 1


def _parse_blood_pressure(conn, date_str: str, data: dict) -> int:
    """Extract systolic/diastolic/pulse from get_blood_pressure(ds).
    No consumer of this endpoint exists elsewhere in the installed
    garminconnect library to confirm the response shape, so this is
    defensive against several plausible key layouts; the raw payload is
    stored via _store_raw so this is a one-look fix if a real account
    returns something else. Written as all-three-or-nothing since a
    partial reading isn't a meaningful measurement."""
    now = utc_now()
    summaries = data.get("measurementSummaries") or []
    summary = summaries[0] if summaries else data

    def _pick(key: str, summary_key: str) -> float | None:
        val = summary.get(key)
        if val is None:
            nested = summary.get(summary_key)
            if isinstance(nested, dict):
                val = nested.get("average")
        return val

    systolic = _pick("systolic", "systolicSummary")
    diastolic = _pick("diastolic", "diastolicSummary")
    pulse = _pick("pulse", "pulseSummary")

    if systolic is None or diastolic is None or pulse is None:
        measurements = summary.get("measurements") or []
        if measurements:
            m = measurements[0]
            systolic = systolic if systolic is not None else m.get("systolic")
            diastolic = diastolic if diastolic is not None else m.get("diastolic")
            pulse = pulse if pulse is not None else m.get("pulse")

    if systolic is None or diastolic is None or pulse is None:
        return 0

    try:
        systolic, diastolic, pulse = float(systolic), float(diastolic), float(pulse)
    except (TypeError, ValueError):
        return 0

    upsert_raw_metric(conn, date_str, SOURCE, "bp_systolic", systolic, now)
    upsert_raw_metric(conn, date_str, SOURCE, "bp_diastolic", diastolic, now)
    upsert_raw_metric(conn, date_str, SOURCE, "bp_pulse", pulse, now)
    return 3


# running-type activity types worth checking for aerobic decoupling
_RUNNING_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "street_running"}
_MIN_DECOUPLING_DURATION_S = 25 * 60


def _parse_splits(data: dict) -> list[dict]:
    """Defensive parse of Garmin's undocumented /activity/{id}/splits response.
    Expected shape: {"lapDTOs": [{"distance": m, "duration": s, "averageHR": bpm}, ...]}.
    Field names are from community reverse-engineering, not official docs —
    the raw payload is stored via _store_raw so this is a one-look fix if a
    real account returns a different shape."""
    laps = data.get("lapDTOs") or data.get("laps") or []
    out = []
    for lap in laps:
        out.append({
            "distance_m": lap.get("distance"),
            "duration_s": lap.get("duration") or lap.get("movingDuration"),
            "avg_hr": lap.get("averageHR") or lap.get("avgHr"),
        })
    return out


def _compute_decoupling(splits: list[dict]) -> float | None:
    """Aerobic decoupling: % change in HR:pace ratio from the first half of a
    run to the second half, using per-lap splits. Positive = HR drifted up
    relative to pace (aerobic fatigue); near 0 = steady effort."""
    valid = [
        s for s in splits
        if s.get("avg_hr") and s.get("duration_s") and s.get("distance_m")
        and s["avg_hr"] > 0 and s["duration_s"] > 0 and s["distance_m"] > 0
    ]
    if len(valid) < 4:
        return None

    def _hr_pace_ratio(group: list[dict]) -> float:
        total_duration = sum(s["duration_s"] for s in group)
        total_distance = sum(s["distance_m"] for s in group)
        avg_hr = sum(s["avg_hr"] * s["duration_s"] for s in group) / total_duration
        pace_s_per_m = total_duration / total_distance
        return avg_hr * pace_s_per_m

    mid = len(valid) // 2
    r1 = _hr_pace_ratio(valid[:mid])
    r2 = _hr_pace_ratio(valid[mid:])
    if r1 <= 0:
        return None
    return round((r2 - r1) / r1 * 100, 2)


def _fetch_and_store_decoupling(
    conn, client, activity_id: str, act_type: str | None, duration_s: int | None
) -> None:
    """Fetch lap splits and compute aerobic decoupling for a qualifying run.
    Only runs once per activity — skipped if already computed, since splits
    don't change on re-fetch within the rolling import window."""
    if not act_type or act_type.lower() not in _RUNNING_TYPES:
        return
    if not duration_s or duration_s < _MIN_DECOUPLING_DURATION_S:
        return
    existing = conn.execute(
        "SELECT decoupling_pct FROM garmin_activities WHERE activity_id=?", (activity_id,)
    ).fetchone()
    if existing and existing[0] is not None:
        return

    data = _safe(lambda: client.get_activity_splits(activity_id), f"get_activity_splits({activity_id})")
    if not data:
        return
    _store_raw(conn, "get_activity_splits", data, None)
    decoupling = _compute_decoupling(_parse_splits(data))
    if decoupling is not None:
        conn.execute(
            "UPDATE garmin_activities SET decoupling_pct=? WHERE activity_id=?",
            (decoupling, activity_id),
        )
    else:
        log.debug("garmin: could not compute decoupling for activity %s (insufficient/malformed splits)", activity_id)


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

    pending_pushed = push_pending_manual_logs(conn)

    dates = _build_date_range(days, start_date, end_date)
    start_str = dates[0].isoformat()
    end_str = dates[-1].isoformat()

    rows_upserted = 0
    log.info("garmin import: %s → %s (%d days)", start_str, end_str, len(dates))

    # For intraday HR: limit to last 2 days on default rolling runs (not subject to
    # late corrections like HRV/sleep, so full 7-day re-fetch is unnecessary load).
    # Explicit date range imports still fetch the full requested window.
    hr_dates = set(dates) if (start_date or end_date) else set(dates[-2:])

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

        # stress (daily aggregates + intraday timeseries)
        data = _safe(lambda: client.get_stress_data(ds), f"get_stress_data({ds})")
        if data:
            _store_raw(conn, "get_stress_data", data, ds)
            rows_upserted += _parse_stress(conn, ds, data)

        # intraday HR (limited to last 2 days on default runs — not late-corrected)
        if d in hr_dates:
            data = _safe(lambda: client.get_heart_rates(ds), f"get_heart_rates({ds})")
            if data:
                _store_raw(conn, "get_heart_rates", data, ds)
                rows_upserted += _parse_intraday_hr(conn, data)

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

        # SpO2
        data = _safe(lambda: client.get_spo2_data(ds), f"get_spo2_data({ds})")
        if data:
            _store_raw(conn, "get_spo2_data", data, ds)
            rows_upserted += _parse_spo2(conn, ds, data)

        # respiration / breathing rate
        data = _safe(lambda: client.get_respiration_data(ds), f"get_respiration_data({ds})")
        if data:
            _store_raw(conn, "get_respiration_data", data, ds)
            rows_upserted += _parse_respiration(conn, ds, data)

        # intensity minutes (active zone minutes)
        data = _safe(lambda: client.get_intensity_minutes_data(ds), f"get_intensity_minutes_data({ds})")
        if data:
            _store_raw(conn, "get_intensity_minutes_data", data, ds)
            rows_upserted += _parse_intensity_minutes(conn, ds, data)

        # weigh-ins (weight, from a synced smart scale)
        data = _safe(lambda: client.get_daily_weigh_ins(ds), f"get_daily_weigh_ins({ds})")
        if data:
            _store_raw(conn, "get_daily_weigh_ins", data, ds)
            rows_upserted += _parse_body_composition(conn, ds, data)

        # blood pressure (from a synced BP monitor)
        data = _safe(lambda: client.get_blood_pressure(ds), f"get_blood_pressure({ds})")
        if data:
            _store_raw(conn, "get_blood_pressure", data, ds)
            rows_upserted += _parse_blood_pressure(conn, ds, data)

        # hydration
        data = _safe(lambda: client.get_hydration_data(ds), f"get_hydration_data({ds})")
        if data:
            _store_raw(conn, "get_hydration_data", data, ds)
            rows_upserted += _parse_hydration(conn, ds, data)

        # Commit per day rather than holding one open write transaction across the
        # whole run — each day is a dozen-plus sequential blocking HTTP calls, so a
        # backfill chunk can take minutes. A long-held write lock means any other
        # writer (MCP logging tools, a concurrently-scheduled Google Health import)
        # silently blocks on it for up to busy_timeout (5min) rather than proceeding.
        conn.commit()

    # ── range endpoints ─────────────────────────────────────────────────────

    # lactate threshold / cycling FTP — point-in-time snapshots (Garmin recomputes
    # these occasionally, not daily), so fetched once per run and stored at the
    # most recent date in the window rather than per-day.
    data = _safe(lambda: client.get_lactate_threshold(latest=True), "get_lactate_threshold")
    if data:
        _store_raw(conn, "get_lactate_threshold", data, end_str)
        rows_upserted += _parse_lactate_threshold(conn, end_str, data)

    data = _safe(lambda: client.get_cycling_ftp(), "get_cycling_ftp")
    if data:
        _store_raw(conn, "get_cycling_ftp", data, end_str)
        rows_upserted += _parse_ftp(conn, end_str, data)

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
                activity_type = activity.get("activityType", {})
                type_key = activity_type.get("typeKey") if isinstance(activity_type, dict) else activity_type
                duration_s = int(activity.get("duration") or activity.get("elapsedDuration") or 0) or None
                _fetch_and_store_decoupling(
                    conn, client, str(activity.get("activityId", "")), type_key, duration_s
                )
        conn.commit()  # activities can trigger a per-activity decoupling fetch each

    # suggested/scheduled workouts — monthly endpoint, call once per unique month in window
    year_months = sorted({(d.year, d.month) for d in dates})
    for year, month in year_months:
        ym_label = f"{year}-{month:02d}"
        data = _safe(
            lambda y=year, m=month: client.get_scheduled_workouts(y, m),
            f"get_scheduled_workouts({ym_label})",
        )
        if not data:
            continue
        items = data if isinstance(data, list) else data.get("workouts", [data])
        _store_raw(conn, "get_scheduled_workouts", data)
        for item in items:
            if not isinstance(item, dict):
                continue
            # field name candidates vary by firmware/API version — try several
            item_date = (
                item.get("date")
                or item.get("scheduledDate")
                or item.get("calendarDate")
            )
            if not item_date:
                continue
            item_date = item_date[:10]  # trim to YYYY-MM-DD if datetime
            _store_raw(conn, "scheduled_workout", item, item_date)
            if _upsert_suggested_workout(conn, item_date, item):
                rows_upserted += 1

    conn.commit()
    return {
        "skipped": False,
        "rows_upserted": rows_upserted,
        "dates": [d.isoformat() for d in dates],
        "manual_logs_pushed": pending_pushed,
    }
