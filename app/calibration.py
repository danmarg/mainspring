"""Weekly, suggestion-only calibration for the alertness exercise-strain decay."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import HOME_TZ, db, resolve_metric, utc_now
from app.readiness import trimp_from_hr_samples

MODEL = "exercise_strain_tau"
INTERVAL = timedelta(days=7)
MIN_LABELS = 20
TAU_CANDIDATES = tuple(4.0 + i * 0.5 for i in range(9))  # 4–8 hours


def _parse_ts(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _local_context(conn: sqlite3.Connection, ts: str) -> tuple[datetime, str]:
    instant = _parse_ts(ts)
    tentative_date = instant.astimezone(ZoneInfo(HOME_TZ)).date().isoformat()
    row = conn.execute("SELECT tz FROM day_timezone WHERE date=?", (tentative_date,)).fetchone()
    try:
        tz = ZoneInfo(row[0] if row else HOME_TZ)
    except Exception:
        tz = ZoneInfo(HOME_TZ)
    local = instant.astimezone(tz)
    return local, local.date().isoformat()


def classify_energy_log(conn: sqlite3.Connection, ts: str) -> str | None:
    """Classify at the instant the rating was made; only intraday ratings train tau."""
    local, local_date = _local_context(conn, ts)
    wake_hour, _ = resolve_metric(conn, local_date, "sleep_wake_hour")
    if wake_hour is None:
        return None
    hour = local.hour + local.minute / 60 + local.second / 3600
    offset = hour - wake_hour
    if offset < -6:
        offset += 24
    if offset > 18:
        offset -= 24
    if 0 <= offset <= 2:
        return "morning"
    if 2 < offset <= 18:
        return "intraday"
    return None


def _activity_end(activity: sqlite3.Row) -> datetime | None:
    if not activity["start_time"] or not activity["duration_s"]:
        return None
    try:
        return _parse_ts(activity["start_time"]) + timedelta(seconds=float(activity["duration_s"]))
    except (TypeError, ValueError):
        return None


def _strain_at(conn: sqlite3.Connection, rating_ts: str, tau_hours: float) -> float:
    rating = _parse_ts(rating_ts)
    local, local_date = _local_context(conn, rating_ts)
    dates = ((local.date() - timedelta(days=1)).isoformat(), local.date().isoformat())
    activities = conn.execute(
        "SELECT start_time, duration_s, avg_hr FROM activities WHERE date IN (?,?) AND start_time IS NOT NULL",
        dates,
    ).fetchall()
    resting_hr, _ = resolve_metric(conn, local_date, "resting_hr")
    debt = 0.0
    for activity in activities:
        end = _activity_end(activity)
        if end is None or end > rating:
            continue
        hours_since_end = (rating - end).total_seconds() / 3600
        if hours_since_end > 24:
            continue
        start = _parse_ts(activity["start_time"])
        samples = [r[0] for r in conn.execute(
            "SELECT COALESCE(MAX(CASE WHEN source='garmin' THEN bpm END), MAX(bpm)) "
            "FROM intraday_hr WHERE ts >= ? AND ts < ? GROUP BY ts ORDER BY ts",
            ((start - timedelta(minutes=15)).isoformat(), (end + timedelta(minutes=15)).isoformat()),
        ).fetchall()]
        trimp = trimp_from_hr_samples(samples, float(activity["duration_s"]) / 60, resting_hr or 55.0)
        if trimp is None and activity["avg_hr"]:
            # Sparse HR coverage is common; use the same duration/HR proxy rather than inventing load.
            trimp = trimp_from_hr_samples([activity["avg_hr"]] * 10, float(activity["duration_s"]) / 60, resting_hr or 55.0)
        if trimp is not None:
            debt += min(0.18, trimp / 1000.0) * math.exp(-hours_since_end / tau_hours)
    return min(0.18, debt) * 100


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1 + exp_x)


def evaluate_tau_samples(samples: list[tuple[float, int]]) -> dict | None:
    """Fit a one-feature logistic check and return discrimination diagnostics."""
    if len(samples) < MIN_LABELS or len({y for _, y in samples}) < 2:
        return None
    xs = [x for x, _ in samples]
    mean = sum(xs) / len(xs)
    scale = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs))
    if scale < 0.01:
        return None
    normalized = [(x - mean) / scale for x in xs]
    intercept = slope = 0.0
    for _ in range(30):
        probabilities = [_sigmoid(intercept + slope * x) for x in normalized]
        w = [max(1e-5, p * (1 - p)) for p in probabilities]
        g0 = sum(y - p for (_, y), p in zip(samples, probabilities))
        g1 = sum((y - p) * x for (_, y), p, x in zip(samples, probabilities, normalized))
        h00 = sum(w) + 1e-4
        h01 = sum(weight * x for weight, x in zip(w, normalized))
        h11 = sum(weight * x * x for weight, x in zip(w, normalized)) + 1e-4
        determinant = h00 * h11 - h01 * h01
        if determinant <= 1e-10:
            return None
        d0 = (g0 * h11 - g1 * h01) / determinant
        d1 = (g1 * h00 - g0 * h01) / determinant
        intercept += d0
        slope += d1
        if max(abs(d0), abs(d1)) < 1e-6:
            break
    scores = [intercept + slope * x for x in normalized]
    ll = sum(y * math.log(max(_sigmoid(score), 1e-12)) + (1-y) * math.log(max(1-_sigmoid(score), 1e-12)) for score, (_, y) in zip(scores, samples))
    pos = [score for score, (_, y) in zip(scores, samples) if y]
    neg = [score for score, (_, y) in zip(scores, samples) if not y]
    auc = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
    return {"log_likelihood": ll, "auc": auc, "slope": slope}


def run_energy_tau_calibration(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT ts, quantity FROM manual_logs WHERE type='energy' AND quantity BETWEEN 1 AND 5 ORDER BY ts"
    ).fetchall()
    labels = [(r["ts"], 1 if r["quantity"] >= 4 else 0) for r in rows
              if r["quantity"] <= 2 or r["quantity"] >= 4]
    labels = [(ts, label) for ts, label in labels if classify_energy_log(conn, ts) == "intraday"]
    if len(labels) < MIN_LABELS or sum(y for _, y in labels) < 5 or sum(1-y for _, y in labels) < 5:
        return {"status": "insufficient", "n_labels": len(labels), "reason": "Need 20 intraday ratings, including at least five low and five high ratings."}
    candidates = []
    for tau in TAU_CANDIDATES:
        fitted = evaluate_tau_samples([(_strain_at(conn, ts, tau), label) for ts, label in labels])
        if fitted:
            candidates.append({"tau_hours": tau, **fitted})
    if not candidates:
        return {"status": "insufficient", "n_labels": len(labels), "reason": "Ratings do not yet span enough exercise strain to estimate a decay time."}
    best = max(candidates, key=lambda candidate: (candidate["log_likelihood"], candidate["auc"]))
    return {"status": "suggested", "n_labels": len(labels), "best_tau_hours": best["tau_hours"], "log_likelihood": best["log_likelihood"], "auc": best["auc"], "candidates": candidates}


def maybe_run_energy_calibration() -> dict:
    """Claim a weekly run atomically; imports can call this concurrently."""
    now = datetime.now(timezone.utc)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute("SELECT started_at FROM model_calibration_runs WHERE model=? ORDER BY id DESC LIMIT 1", (MODEL,)).fetchone()
        if latest and now - _parse_ts(latest[0]) < INTERVAL:
            return {"status": "not_due"}
        cur = conn.execute("INSERT INTO model_calibration_runs(model, started_at, status) VALUES (?,?,?)", (MODEL, utc_now(), "running"))
        run_id = cur.lastrowid
    try:
        with db() as conn:
            result = run_energy_tau_calibration(conn)
            conn.execute(
                "UPDATE model_calibration_runs SET finished_at=?, status=?, n_labels=?, best_tau_hours=?, log_likelihood=?, auc=?, details_json=? WHERE id=?",
                (utc_now(), result["status"], result.get("n_labels"), result.get("best_tau_hours"), result.get("log_likelihood"), result.get("auc"), json.dumps(result), run_id),
            )
        return result
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE model_calibration_runs SET finished_at=?, status=?, error=? WHERE id=?", (utc_now(), "error", str(exc), run_id))
        raise


def latest_energy_calibration() -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT started_at, finished_at, status, n_labels, best_tau_hours, log_likelihood, auc, details_json, error FROM model_calibration_runs WHERE model=? ORDER BY id DESC LIMIT 1", (MODEL,)).fetchone()
    if not row:
        return None
    return {"started_at": row[0], "finished_at": row[1], "status": row[2], "n_labels": row[3], "best_tau_hours": row[4], "log_likelihood": row[5], "auc": row[6], "details": json.loads(row[7]) if row[7] else None, "error": row[8]}
