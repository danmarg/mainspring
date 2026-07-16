"""
Readiness score and energy curve — shared between dashboard and MCP server.

Readiness algorithm draws on:
  - HRV vs 7-day baseline: Plews et al. (2012, Int J Sports Physiol Perform)
  - Training Stress Balance / TSB: Banister (1991) impulse-response model
  - Sleep score and resting HR as established recovery markers

Energy curve uses a simplified two-process model of alertness:
  - Borbély (1982) / Daan, Beersma, Borbély (1984)
  - Circadian component C(t) and homeostatic pressure S(t) combined
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date


def compute_readiness(
    hrv: float | None,
    hrv_7d: float | None,
    sleep_score: float | None,
    rhr: float | None,
    rhr_7d: float | None,
    tsb: float | None,
) -> dict:
    """
    Compute a composite readiness score 0-100.

    Returns:
        score: int 0-100, or None if no data at all
        label: "Primed" / "Ready" / "Moderate" / "Low" / "Rest"
        color: CSS hex color
        components: list of dicts {name, score, weight, detail}
    """
    components: list[dict] = []

    # HRV vs 7-day rolling mean (weight 0.40)
    # Plews et al. 2012: current/rolling-mean ratio is the key signal
    if hrv is not None and hrv_7d and hrv_7d > 0:
        ratio = hrv / hrv_7d
        # 50 at baseline, +/- 2.5 points per percent deviation; capped 0-100
        hrv_score = min(100.0, max(0.0, 50.0 + (ratio - 1.0) * 250.0))
        sign = "+" if ratio >= 1 else ""
        components.append({
            "name": "HRV",
            "score": hrv_score,
            "weight": 0.40,
            "detail": f"{hrv:.0f}ms vs {hrv_7d:.0f}ms avg ({sign}{(ratio-1)*100:.0f}%)",
        })

    # Sleep score (weight 0.30)
    if sleep_score is not None:
        components.append({
            "name": "Sleep",
            "score": float(sleep_score),
            "weight": 0.30,
            "detail": f"Score {sleep_score:.0f}/100",
        })

    # RHR vs 7-day rolling mean, inverted (weight 0.20)
    if rhr is not None and rhr_7d and rhr_7d > 0:
        ratio = rhr / rhr_7d
        # 50 at baseline; elevated RHR (ratio > 1) drives score down
        rhr_score = min(100.0, max(0.0, 50.0 - (ratio - 1.0) * 500.0))
        components.append({
            "name": "Resting HR",
            "score": rhr_score,
            "weight": 0.20,
            "detail": f"{rhr:.0f}bpm vs {rhr_7d:.0f}bpm avg",
        })

    # Training Stress Balance / form (weight 0.10 when available)
    if tsb is not None:
        # 50 at TSB=0; +2pts per unit of TSB (100 at +25, 0 at -25)
        tsb_score = min(100.0, max(0.0, 50.0 + tsb * 2.0))
        sign = "+" if tsb >= 0 else ""
        components.append({
            "name": "Form (TSB)",
            "score": tsb_score,
            "weight": 0.10,
            "detail": f"TSB {sign}{tsb:.0f}",
        })

    if not components:
        return {"score": None, "label": "No data", "color": "#888", "components": []}

    total_weight = sum(c["weight"] for c in components)
    weighted_score = sum(c["score"] * c["weight"] for c in components) / total_weight
    score = round(weighted_score)

    if score >= 80:
        label, color = "Primed", "#2ecc71"
    elif score >= 65:
        label, color = "Ready", "#3498db"
    elif score >= 50:
        label, color = "Moderate", "#f39c12"
    elif score >= 35:
        label, color = "Low", "#e67e22"
    else:
        label, color = "Rest", "#e74c3c"

    return {"score": score, "label": label, "color": color, "components": components}


def readiness_from_db(conn: sqlite3.Connection, date_str: str | None = None) -> dict:
    """Query today's metrics and compute readiness. date_str defaults to today."""
    if date_str is None:
        date_str = date.today().isoformat()

    row = conn.execute(
        "SELECT hrv, sleep_score, resting_hr FROM daily_metrics WHERE date=?",
        (date_str,),
    ).fetchone()

    hrv = sleep_score = rhr = None
    if row:
        hrv, sleep_score, rhr = row

    avg = conn.execute(
        """SELECT AVG(hrv), AVG(resting_hr) FROM daily_metrics
           WHERE date >= date(?, '-7 days') AND date < ?""",
        (date_str, date_str),
    ).fetchone()
    hrv_7d = avg[0] if avg else None
    rhr_7d = avg[1] if avg else None

    tsb_row = conn.execute(
        """SELECT
               MAX(CASE WHEN metric='ctl' THEN value END),
               MAX(CASE WHEN metric='atl' THEN value END)
           FROM raw_daily_metrics
           WHERE date=? AND metric IN ('ctl', 'atl')""",
        (date_str,),
    ).fetchone()
    tsb = None
    if tsb_row and tsb_row[0] is not None and tsb_row[1] is not None:
        tsb = tsb_row[0] - tsb_row[1]

    return compute_readiness(hrv, hrv_7d, sleep_score, rhr, rhr_7d, tsb)


def alertness_curve(
    wake_hour: float,
    sleep_score: float = 75.0,
    hours: int = 17,
    caffeine_doses: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """
    Simplified two-process alertness model (Borbély 1982; Daan, Beersma, Borbély 1984).

    Combines:
      - Circadian signal C(t): cosine with peak ~9pm (wake-maintenance zone)
      - Homeostatic pressure S(t): exponential rise over the waking day
      - Sleep-quality factor: low score shifts starting S upward
      - Caffeine boost: blocks adenosine, suppressing S with ~5h half-life
        (Nehlig et al. 1992; Lovallo et al. 2006)

    Args:
        wake_hour: local hour of waking (e.g. 6.5 = 6:30am)
        sleep_score: 0-100 sleep quality (affects depth of post-wake restoration)
        hours: how many hours forward to forecast
        caffeine_doses: list of (dose_mg, local_hour_of_dose) tuples for today

    Returns:
        List of {hour, alertness} dicts at 15-min resolution.
    """
    quality = 0.6 + 0.4 * max(0.0, min(sleep_score / 100.0, 1.0))
    s0 = 1.0 - quality  # residual sleep pressure at wake; worse sleep = higher S

    result = []
    for i in range(hours * 4):  # 15-min steps
        t = i / 4.0  # hours since waking
        hour = (wake_hour + t) % 24

        # Circadian component: cosine peak at 3pm (15h), typical chronotype.
        # This shifts the alert phase to afternoon and produces the expected
        # morning-rise → afternoon-peak → evening-decline pattern.
        C = (math.cos(2 * math.pi * (hour - 15.0) / 24) + 1) / 2

        # Homeostatic pressure: rises from s0 toward 1 over the waking period
        S = s0 + (1 - s0) * (1 - math.exp(-t / 16.0))

        # Caffeine: each dose reduces effective S via adenosine-receptor blockade
        # Effect peaks ~45min post-dose, decays with 5h half-life
        caffeine_reduction = 0.0
        if caffeine_doses:
            for dose_mg, dose_local_hour in caffeine_doses:
                hours_since = (hour - dose_local_hour) % 24
                if 0 < hours_since <= 12:
                    ramp = min(1.0, hours_since / 0.75)  # 0 → peak over 45 min
                    decay = math.exp(-0.693 * max(0.0, hours_since - 0.75) / 5.0)
                    caffeine_reduction += (dose_mg / 200.0) * 0.25 * ramp * decay

        S_effective = max(0.0, S - caffeine_reduction)

        # Alertness ∝ C (circadian drive) minus S_effective (sleep pressure)
        raw = C * 0.65 + (1 - S_effective) * 0.35
        alertness = round(max(5.0, min(100.0, raw * quality * 100)), 1)

        result.append({"hour": round(hour, 3), "alertness": alertness})

    return result
