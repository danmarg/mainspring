"""
Readiness score and energy curve — shared between dashboard and MCP server.

━━━ Readiness score ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Weighted composite of four signals (weights in compute_readiness):

HRV / 7-day baseline (40%)
  The ratio of today's HRV to its own 7-day rolling mean is a better fatigue
  signal than the raw value, because individual baselines vary so much.
  Reference: Plews, D.J. et al. (2012). "Heart rate variability in elite
  triathletes, is variation in variability the key to effective training?
  A case comparison." Int J Sports Physiol Perform, 7(4), 327-336.

Sleep score (30%)
  Passed through as-is from Garmin/Fitbit sleep staging algorithms, which
  are themselves calibrated against polysomnography.

Resting HR vs baseline (20%)
  Elevated morning resting HR is an established marker of incomplete recovery
  and autonomic stress. Inverted from the HRV ratio so high = bad.
  Reference: Coote, J.H. (2010). "Recovery of heart rate following intense
  dynamic exercise." Exp Physiol, 95(3), 431-440.

Training Stress Balance / form (10%)
  TSB = CTL − ATL (chronic minus acute training load). Positive TSB = freshness,
  negative TSB = accumulated fatigue. Garmin provides CTL/ATL directly.
  Reference: Banister, E.W. (1991). "Modeling elite athletic performance."
  Physiological Testing of Elite Athletes, 403-424.

━━━ Energy / alertness curve ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Two-process model: alertness(t) = f(C(t), S(t)) where:

  C(t) — circadian process: the ~24h oscillation driven by the suprachiasmatic
          nucleus (SCN). Modeled as a cosine peaking at 15:00 (typical
          chronotype). Shifts the alert zone into the afternoon and creates the
          characteristic post-lunch dip and evening wake-maintenance zone.
          Reference: Daan, S., Beersma, D.C.M. & Borbély, A.A. (1984).
          "Timing of human sleep: recovery process gated by a circadian
          pacemaker." Am J Physiol, 246(2), R161-R183.

  S(t) — homeostatic sleep pressure: adenosine accumulates in the brain during
          waking and is cleared during sleep. Modeled as exponential rise toward
          saturation, with time constant 16h (Achermann & Borbély 2003).
          Starting level s0 is raised when sleep quality is poor (worse sleep
          = more residual pressure at wake).
          Reference: Borbély, A.A. (1982). "A two process model of sleep
          regulation." Hum Neurobiol, 1(3), 195-204.
          Reference: Achermann, P. & Borbély, A.A. (2003). "Mathematical
          models of sleep regulation." Front Biosci, 8, s683-693.

  Caffeine — delays adenosine clearance by competitively binding A1/A2A
             receptors. Effect modeled as a reduction in S proportional to
             dose (reference dose 200mg), ramping over 45 min and decaying with
             5h half-life (matching caffeine plasma half-life in most adults).
             Reference: Nehlig, A., Daval, J.L. & Debry, G. (1992). "Caffeine
             and the central nervous system: mechanisms of action, biochemical,
             metabolic and psychostimulant effects." Brain Res Rev, 17(2),
             139-170.
             Reference: Lovallo, W.R. et al. (2006). "Caffeine stimulation of
             cortisol secretion across the waking hours in relation to caffeine
             intake levels." Psychosom Med, 68(5), 734-739.

  Exercise — post-workout sympathetic arousal (catecholamine release,
             elevated core temperature) suppresses perceived sleepiness for
             1-3h after session end. Effect scales with intensity (RPE or
             avg-HR proxy) and decays with 90-min half-life. Morning workouts
             therefore provide a sustained alertness boost into mid-morning;
             evening workouts delay the evening decline.
             Reference: Youngstedt, S.D. (2005). "Effects of exercise on
             sleep." Clin Sports Med, 24(2), 355-365.
             Reference: Kline, C.E. et al. (2010). "The effect of exercise
             intensity on sleep in young women." Eur J Appl Physiol,
             110(2), 389-397.
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
    activity_boosts: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """
    Simplified two-process alertness model (Borbély 1982; Daan, Beersma, Borbély 1984).

    Combines:
      - Circadian signal C(t): cosine with peak ~9pm (wake-maintenance zone)
      - Homeostatic pressure S(t): exponential rise over the waking day
      - Sleep-quality factor: low score shifts starting S upward
      - Caffeine boost: blocks adenosine, suppressing S with ~5h half-life
        (Nehlig et al. 1992; Lovallo et al. 2006)
      - Exercise boost: post-workout sympathetic arousal suppresses sleep pressure
        for ~2-3h after the session; effect scales with intensity (RPE or avg HR proxy)

    Args:
        wake_hour: local hour of waking (e.g. 6.5 = 6:30am)
        sleep_score: 0-100 sleep quality (affects depth of post-wake restoration)
        hours: how many hours forward to forecast
        caffeine_doses: list of (dose_mg, local_hour_of_dose) tuples for today
        activity_boosts: list of (intensity_0_to_1, local_end_hour) tuples for today

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

        # Exercise: post-workout sympathetic arousal suppresses perceived sleep pressure.
        # Effect peaks immediately after session end, decays with 90-min half-life.
        # Intensity 1.0 (hard effort) reduces S by up to 0.20 at peak.
        exercise_reduction = 0.0
        if activity_boosts:
            for intensity, end_local_hour in activity_boosts:
                hrs_since = (hour - end_local_hour) % 24
                if 0 < hrs_since <= 4:
                    exercise_reduction += intensity * 0.20 * math.exp(-0.693 * hrs_since / 1.5)

        S_effective = max(0.0, S - caffeine_reduction - exercise_reduction)

        # Alertness ∝ C (circadian drive) minus S_effective (sleep pressure)
        raw = C * 0.65 + (1 - S_effective) * 0.35
        alertness = round(max(5.0, min(100.0, raw * quality * 100)), 1)

        result.append({"hour": round(hour, 3), "alertness": alertness})

    return result
