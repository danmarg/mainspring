"""
Readiness score and energy curve — shared between dashboard and MCP server.

━━━ Readiness score ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Weighted composite of six signals (weights in compute_readiness):

HRV / 7-day baseline (35%)
  The ratio of today's HRV to its own 7-day rolling mean is a better fatigue
  signal than the raw value, because individual baselines vary so much.
  Reference: Plews, D.J. et al. (2012). "Heart rate variability in elite
  triathletes, is variation in variability the key to effective training?
  A case comparison." Int J Sports Physiol Perform, 7(4), 327-336.

  Folded into this component: the day-to-day *variability* of HRV, measured
  as the standard deviation of ln(RMSSD) across the same trailing 7 nights.
  A rising CV is itself a marker of autonomic stress/overreaching independent
  of the mean — two athletes with the same 7-day average HRV can be in very
  different states depending on how noisy the nightly values are. Applied as
  a penalty on top of the ratio score once CV exceeds a typical healthy
  range (~8%), capped so it can't zero out the component on its own.
  Reference: Plews, D.J. et al. (2013). "Evaluating training adaptation with
  heart-rate measures: a methods comparison." Int J Sports Physiol Perform,
  8(6), 688-691.
  Reference: Buchheit, M. (2014). "Monitoring training status with HR
  measures: do all roads lead to Rome?" Front Physiol, 5, 73.

Sleep debt (25%)
  Rather than reading last night's score in isolation, this is an
  exponentially-weighted average of sleep scores over the trailing ~10
  nights (half-life 2.5 nights), so a run of poor nights still weighs on
  today's readiness even after a single good one — cumulative sleep debt
  dissipates gradually rather than resetting to zero at each good night.
  Reference: Fullagar, H.H.K. et al. (2015). "Sleep and athletic
  performance: the effects of sleep loss on exercise performance, and
  physiological and cognitive responses to exercise." Sports Med, 45(2),
  161-186.
  Per-night scores are passed as-is from Garmin/Fitbit sleep staging
  algorithms, calibrated against polysomnography. When no vendor score is
  available for a day, normalize.py synthesizes one from
  duration/efficiency/deep%/rem%, calibrated against 171 days of
  overlapping Garmin+Google Health data (~5.5 MAE, see
  synthesize_sleep_score in app/normalize.py).

Resting HR vs baseline (15%)
  Elevated morning resting HR is an established marker of incomplete recovery
  and autonomic stress. Inverted from the HRV ratio so high = bad.
  Reference: Coote, J.H. (2010). "Recovery of heart rate following intense
  dynamic exercise." Exp Physiol, 95(3), 431-440.

Training Stress Balance / form (10%)
  TSB = CTL − ATL (chronic minus acute training load). Positive TSB = freshness,
  negative TSB = accumulated fatigue. Garmin provides CTL/ATL directly.
  Reference: Banister, E.W. (1991). "Modeling elite athletic performance."
  Physiological Testing of Elite Athletes, 403-424.

Acute:Chronic Workload Ratio (15%)
  ATL/CTL ratio (the same two Garmin-provided numbers behind TSB, read as a
  ratio instead of a difference). Sweet spot 0.8-1.3; below it risks
  detraining, above it is the most replicated injury-risk signal in the
  load-monitoring literature, penalized more steeply than the low side.
  Reference: Gabbett, T.J. (2016). "The training-injury prevention paradox:
  should athletes be training smarter and harder?" Br J Sports Med, 50(5),
  273-280.

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

import json
import math
import sqlite3
from datetime import date


def _ln_rmssd_cv(daily_hrv_values: list[float]) -> float | None:
    """
    Day-to-day HRV variability: SD of ln-transformed daily RMSSD values across
    a trailing window, expressed as a fraction (0.08 = 8%). ln-transforming
    first normalizes RMSSD's right-skewed distribution so the SD approximates
    a coefficient of variation (Plews et al. 2013; Buchheit 2014).

    Needs >=4 nights to be meaningful; returns None otherwise.
    """
    values = [v for v in daily_hrv_values if v and v > 0]
    if len(values) < 4:
        return None
    logs = [math.log(v) for v in values]
    mean = sum(logs) / len(logs)
    variance = sum((x - mean) ** 2 for x in logs) / len(logs)
    return math.sqrt(variance)


def _sleep_debt_score(nights: list[tuple[str, float]], half_life_days: float = 2.5) -> float | None:
    """
    Exponentially-weighted average of sleep scores over recent nights, most
    recent night weighted heaviest. A run of poor nights still drags this
    down even after one good night, unlike reading last night's score alone
    (Fullagar et al. 2015 on cumulative sleep debt).

    Args:
        nights: list of (date_str, score) sorted ascending by date (oldest first).
        half_life_days: days for a night's influence to decay by half.
    """
    scored = [(d, s) for d, s in nights if s is not None]
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    most_recent = date.fromisoformat(scored[-1][0])

    total_weight = 0.0
    weighted_sum = 0.0
    for d_str, score in scored:
        days_ago = (most_recent - date.fromisoformat(d_str)).days
        weight = 0.5 ** (days_ago / half_life_days)
        weighted_sum += score * weight
        total_weight += weight

    return weighted_sum / total_weight if total_weight > 0 else None


def compute_readiness(
    hrv: float | None,
    hrv_7d: float | None,
    hrv_cv: float | None,
    sleep_debt_score: float | None,
    rhr: float | None,
    rhr_7d: float | None,
    atl: float | None,
    ctl: float | None,
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

    # HRV vs 7-day rolling mean, with a variability penalty (weight 0.35)
    # Plews et al. 2012: current/rolling-mean ratio is the key signal.
    # Plews et al. 2013 / Buchheit 2014: elevated night-to-night CV is an
    # independent overreaching signal, so it discounts the ratio score.
    if hrv is not None and hrv_7d and hrv_7d > 0:
        ratio = hrv / hrv_7d
        # 50 at baseline, +/- 2.5 points per percent deviation; capped 0-100
        hrv_score = min(100.0, max(0.0, 50.0 + (ratio - 1.0) * 250.0))
        sign = "+" if ratio >= 1 else ""
        detail = f"{hrv:.0f}ms vs {hrv_7d:.0f}ms avg ({sign}{(ratio-1)*100:.0f}%)"

        if hrv_cv is not None:
            # Healthy trailing-week CV is roughly <=8%; penalize above that,
            # ramping to a 30pt cap by ~15% (capped so CV alone can't zero the score).
            cv_penalty = min(30.0, max(0.0, (hrv_cv - 0.08) / (0.15 - 0.08) * 30.0))
            hrv_score = max(0.0, hrv_score - cv_penalty)
            detail += f", CV {hrv_cv*100:.0f}%"

        components.append({
            "name": "HRV",
            "score": hrv_score,
            "weight": 0.35,
            "detail": detail,
        })

    # Sleep debt: EWMA over trailing nights (weight 0.25)
    if sleep_debt_score is not None:
        components.append({
            "name": "Sleep",
            "score": float(sleep_debt_score),
            "weight": 0.25,
            "detail": f"Debt-adjusted {sleep_debt_score:.0f}/100",
        })

    # RHR vs 7-day rolling mean, inverted (weight 0.15)
    if rhr is not None and rhr_7d and rhr_7d > 0:
        ratio = rhr / rhr_7d
        # 50 at baseline; elevated RHR (ratio > 1) drives score down
        rhr_score = min(100.0, max(0.0, 50.0 - (ratio - 1.0) * 500.0))
        components.append({
            "name": "Resting HR",
            "score": rhr_score,
            "weight": 0.15,
            "detail": f"{rhr:.0f}bpm vs {rhr_7d:.0f}bpm avg",
        })

    # Training Stress Balance / form (weight 0.10 when available)
    if atl is not None and ctl is not None:
        tsb = ctl - atl
        # 50 at TSB=0; +2pts per unit of TSB (100 at +25, 0 at -25)
        tsb_score = min(100.0, max(0.0, 50.0 + tsb * 2.0))
        sign = "+" if tsb >= 0 else ""
        components.append({
            "name": "Form (TSB)",
            "score": tsb_score,
            "weight": 0.10,
            "detail": f"TSB {sign}{tsb:.0f}",
        })

    # Acute:Chronic Workload Ratio (weight 0.15 when available)
    # Gabbett 2016: sweet spot 0.8-1.3; penalize high side more steeply
    # than low side, matching the asymmetric injury-risk curve.
    if atl is not None and ctl is not None and ctl > 0:
        acwr = atl / ctl
        if 0.8 <= acwr <= 1.3:
            acwr_score = 100.0
        elif acwr < 0.8:
            acwr_score = max(0.0, 100.0 - (0.8 - acwr) * 150.0)
        else:
            acwr_score = max(0.0, 100.0 - (acwr - 1.3) * 220.0)
        components.append({
            "name": "ACWR",
            "score": acwr_score,
            "weight": 0.15,
            "detail": f"ATL/CTL {acwr:.2f}",
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


def _source_flag(source_flags_json: str | None, metric: str) -> str | None:
    if not source_flags_json:
        return None
    try:
        flag = json.loads(source_flags_json).get(metric)
    except (ValueError, TypeError):
        return None
    return flag if isinstance(flag, str) else None


def _is_computed_rhr_flag(source_flags_json: str | None) -> bool:
    """True if that day's resting_hr came from _resting_hr_from_intraday (flagged
    "<source>_intraday" in normalize.py) rather than a vendor's own RHR figure."""
    flag = _source_flag(source_flags_json, "resting_hr")
    return flag is not None and flag.endswith("_intraday")


def _is_all_day_hrv_flag(source_flags_json: str | None) -> bool:
    """True only for Google Health's raw daily rollup (a 24h aggregate) — Garmin's
    hrv and the "google_health_intraday" recompute are both overnight-only and
    comparable to each other; the raw google_health rollup isn't comparable to
    either (see _hrv_overnight_from_intraday in normalize.py)."""
    return _source_flag(source_flags_json, "hrv") == "google_health"


def readiness_from_db(conn: sqlite3.Connection, date_str: str | None = None) -> dict:
    """Query today's metrics and compute readiness. date_str defaults to today."""
    if date_str is None:
        date_str = date.today().isoformat()

    row = conn.execute(
        "SELECT hrv, sleep_score, resting_hr, source_flags_json FROM daily_metrics WHERE date=?",
        (date_str,),
    ).fetchone()

    hrv = sleep_score = rhr = None
    today_rhr_computed = today_hrv_all_day = False
    if row:
        hrv, sleep_score, rhr, today_flags = row
        today_rhr_computed = _is_computed_rhr_flag(today_flags)
        today_hrv_all_day = _is_all_day_hrv_flag(today_flags)

    trailing = conn.execute(
        """SELECT hrv, resting_hr, source_flags_json FROM daily_metrics
           WHERE date >= date(?, '-7 days') AND date < ?""",
        (date_str, date_str),
    ).fetchall()
    # Both baselines must be like-for-like: mixing methodologies (vendor RHR vs our
    # intraday-computed RHR; Google's all-day HRV rollup vs overnight-only HRV)
    # through the readiness ratios' large multipliers produces a false floor/ceiling
    # on methodology-transition days, not real signal.
    hrv_values_7d = [
        r[0] for r in trailing
        if r[0] is not None and _is_all_day_hrv_flag(r[2]) == today_hrv_all_day
    ]
    rhr_values_7d = [
        r[1] for r in trailing
        if r[1] is not None and _is_computed_rhr_flag(r[2]) == today_rhr_computed
    ]
    hrv_7d = sum(hrv_values_7d) / len(hrv_values_7d) if hrv_values_7d else None
    rhr_7d = sum(rhr_values_7d) / len(rhr_values_7d) if rhr_values_7d else None
    hrv_cv = _ln_rmssd_cv(hrv_values_7d + ([hrv] if hrv is not None else []))

    sleep_rows = conn.execute(
        """SELECT date, sleep_score FROM daily_metrics
           WHERE date <= ? AND date > date(?, '-10 days') AND sleep_score IS NOT NULL""",
        (date_str, date_str),
    ).fetchall()
    sleep_debt = _sleep_debt_score(sleep_rows) if sleep_rows else sleep_score

    load_row = conn.execute(
        """SELECT
               MAX(CASE WHEN metric='ctl' THEN value END),
               MAX(CASE WHEN metric='atl' THEN value END)
           FROM raw_daily_metrics
           WHERE date=? AND metric IN ('ctl', 'atl')""",
        (date_str,),
    ).fetchone()
    ctl = load_row[0] if load_row else None
    atl = load_row[1] if load_row else None

    return compute_readiness(hrv, hrv_7d, hrv_cv, sleep_debt, rhr, rhr_7d, atl, ctl)


# ── illness / physiological-stress risk ──────────────────────────────────────
#
# Heuristic, not a validated diagnostic score — no consumer vendor (Garmin,
# Oura, WHOOP) has published peer-reviewed validation of their composite
# illness-risk weighting, and neither has this. What the literature does
# support:
#
#   - Multi-signal beats any single signal. A systematic review of wearable-
#     based SARS-CoV-2 detection found resting HR, HRV, and respiratory rate
#     each carry independent signal, while skin temperature alone was the
#     least reliable metric in isolation — it adds value only in combination.
#     Reference: Kamišalić, A. et al. (2022). "The role of wearable devices
#     in detecting SARS-CoV-2: A systematic review." IJERPH / PMC9020803.
#
#   - The signals don't move together — they cascade over a few days rather
#     than all shifting on the same day (RHR first, skin temp next, HRV
#     later, symptoms after). That pattern is well-documented in consumer
#     wearable illness-detection write-ups but not itself peer-reviewed, so
#     it's treated here as a design constraint rather than a cited finding:
#     requiring same-day concordance across all three signals would miss it,
#     so this looks for signals crossing threshold at any point within a
#     trailing window instead of on a single day.
#
# Each signal is evaluated against the athlete's own trailing baseline (same
# 7-day HRV/RHR baselines as compute_readiness, methodology-matched via the
# source_flags_json checks) rather than an absolute threshold, since normal
# ranges vary hugely between people.

def compute_illness_risk(
    days: list[dict],
) -> dict:
    """
    days: list of {date, rhr, rhr_baseline, hrv, hrv_baseline, skin_temp_deviation},
    most recent last. Missing values are fine (per-field None).

    Returns {level: "green"|"yellow"|"red"|None, label, color, detail, signals}
    where signals is a list of the individual triggers found in the window.

    The window is scanned for triggers on any day, and severity (red needs 2+
    distinct signals, yellow needs 1) is based on the total found anywhere in
    the window — that's what makes a genuine cascade (RHR up day 1, skin temp
    up day 2, HRV down day 3, none concordant on a single day) still read red.
    But if *none* of those triggers are still active on the most recent day —
    everything that crossed threshold has since reverted — the level is capped
    at yellow with a distinct "resolved" label instead of red/yellow, so a
    two-signal blip on one past day doesn't read as an active state days after
    it passed. A past-only trigger still surfaces in `detail`/`signals` either way.
    """
    RHR_DELTA_BPM = 3.0       # bpm above baseline
    HRV_RATIO = 0.90          # fraction of baseline
    SKIN_TEMP_C = 0.3         # degrees C above personal baseline

    triggers: dict[str, dict] = {}  # signal name -> first/strongest trigger found
    most_recent_date = days[-1].get("date") if days else None
    active_today: set[str] = set()  # signal names currently crossed on most_recent_date

    def _recency(d: str | None) -> str:
        if not d or not most_recent_date:
            return d or ""
        try:
            n = (date.fromisoformat(most_recent_date) - date.fromisoformat(d)).days
        except ValueError:
            return d
        if n <= 0:
            return "today"
        if n == 1:
            return "yesterday"
        return f"{n} days ago"

    for day in days:
        d = day.get("date")
        is_today = d == most_recent_date

        rhr, rhr_base = day.get("rhr"), day.get("rhr_baseline")
        if rhr is not None and rhr_base:
            delta = rhr - rhr_base
            if delta >= RHR_DELTA_BPM:
                if "Resting HR" not in triggers:
                    triggers["Resting HR"] = {"date": d, "detail": f"Resting HR +{delta:.0f}bpm vs baseline ({_recency(d)})"}
                if is_today:
                    active_today.add("Resting HR")

        hrv, hrv_base = day.get("hrv"), day.get("hrv_baseline")
        if hrv is not None and hrv_base:
            ratio = hrv / hrv_base
            if ratio <= HRV_RATIO:
                if "HRV" not in triggers:
                    triggers["HRV"] = {"date": d, "detail": f"HRV {ratio*100:.0f}% of baseline ({_recency(d)})"}
                if is_today:
                    active_today.add("HRV")

        skin_temp = day.get("skin_temp_deviation")
        if skin_temp is not None and skin_temp >= SKIN_TEMP_C:
            if "Skin temp" not in triggers:
                triggers["Skin temp"] = {"date": d, "detail": f"Skin temp +{skin_temp:.1f}°C vs baseline ({_recency(d)})"}
            if is_today:
                active_today.add("Skin temp")

    signals = [t["detail"] for t in triggers.values()]
    n = len(triggers)
    n_today = len(active_today)

    have_any_data = any(
        day.get("rhr_baseline") or day.get("hrv_baseline") or day.get("skin_temp_deviation") is not None
        for day in days
    )
    if not have_any_data:
        return {"level": None, "label": "No data", "color": "#888", "detail": "", "signals": []}

    if n >= 1 and n_today == 0:
        # Everything that crossed threshold in the window has since reverted —
        # don't read as an active state days after it passed.
        level, label, color = "yellow", "Recent deviation (resolved)", "#f39c12"
    elif n >= 2:
        level, label, color = "red", "Possible stress/illness signal", "#e74c3c"
    elif n == 1:
        level, label, color = "yellow", "Mild deviation", "#f39c12"
    else:
        level, label, color = "green", "Normal range", "#2ecc71"

    detail = "; ".join(signals) if signals else "RHR, HRV, and skin temp all within normal range."
    return {"level": level, "label": label, "color": color, "detail": detail, "signals": signals}


def illness_risk_from_db(conn: sqlite3.Connection, date_str: str | None = None, window_days: int = 3) -> dict:
    """Trailing-window illness/stress risk (see compute_illness_risk). Baseline is
    a single 7-day trailing average anchored on the window's most recent date —
    it doesn't need to move day-to-day since it represents "normal for you", not
    a rolling comparison point."""
    if date_str is None:
        date_str = date.today().isoformat()

    window_rows = conn.execute(
        """SELECT date, resting_hr, hrv, skin_temp_deviation, source_flags_json
           FROM daily_metrics
           WHERE date <= ? AND date > date(?, ?)
           ORDER BY date""",
        (date_str, date_str, f"-{window_days} days"),
    ).fetchall()
    if not window_rows:
        return {"level": None, "label": "No data", "color": "#888", "detail": "", "signals": []}

    today_flags = window_rows[-1][4]
    today_rhr_computed = _is_computed_rhr_flag(today_flags)
    today_hrv_all_day = _is_all_day_hrv_flag(today_flags)

    baseline_rows = conn.execute(
        """SELECT hrv, resting_hr, source_flags_json FROM daily_metrics
           WHERE date >= date(?, '-7 days') AND date < ?""",
        (date_str, date_str),
    ).fetchall()
    hrv_values = [
        r[0] for r in baseline_rows
        if r[0] is not None and _is_all_day_hrv_flag(r[2]) == today_hrv_all_day
    ]
    rhr_values = [
        r[1] for r in baseline_rows
        if r[1] is not None and _is_computed_rhr_flag(r[2]) == today_rhr_computed
    ]
    hrv_baseline = sum(hrv_values) / len(hrv_values) if hrv_values else None
    rhr_baseline = sum(rhr_values) / len(rhr_values) if rhr_values else None

    days = [
        {
            "date": r[0],
            "rhr": r[1],
            "rhr_baseline": rhr_baseline,
            "hrv": r[2],
            "hrv_baseline": hrv_baseline,
            "skin_temp_deviation": r[3],
        }
        for r in window_rows
    ]
    return compute_illness_risk(days)


# ── sleep regularity ─────────────────────────────────────────────────────────
#
# Night-to-night consistency of bed/wake times, independent of duration or
# score — increasingly shown to predict recovery on its own (Windred, D.P. et
# al. (2024). "Sleep regularity is a stronger predictor of mortality risk than
# sleep duration." Sleep, 47(1)). Reported separately rather than folded into
# the weighted readiness score above, since it's a slower-moving trend
# (multi-week pattern) rather than an acute daily input.
#
# Bed/wake hours wrap at midnight, so plain min/max SD would overstate
# variability for anyone who crosses it — circular standard deviation
# (Mardia, K.V. (1972). "Statistics of Directional Data") handles that
# correctly by treating each time-of-day as an angle around a 24h clock.

def _circular_sd_hours(hours: list[float]) -> float | None:
    if len(hours) < 3:
        return None
    angles = [h / 24 * 2 * math.pi for h in hours]
    n = len(angles)
    sin_sum = sum(math.sin(a) for a in angles)
    cos_sum = sum(math.cos(a) for a in angles)
    r = min(1.0, math.sqrt(sin_sum ** 2 + cos_sum ** 2) / n)
    if r <= 0:
        return 12.0  # maximally irregular
    circular_sd_rad = math.sqrt(-2 * math.log(r))
    return circular_sd_rad / (2 * math.pi) * 24


def sleep_regularity(nights: list[tuple[float, float]]) -> dict:
    """
    Args:
        nights: list of (wake_hour, sleep_duration_min) tuples, most recent
                14 or so nights.

    Returns:
        score: 0-100 (100 = perfectly consistent bed/wake times), or None if
               too few nights
        wake_sd_hours, bedtime_sd_hours: circular SD of each, in hours
    """
    wake_hours = [w for w, _ in nights if w is not None]
    bedtimes = [
        (w - d / 60) % 24
        for w, d in nights
        if w is not None and d is not None
    ]

    wake_sd = _circular_sd_hours(wake_hours)
    bedtime_sd = _circular_sd_hours(bedtimes)
    if wake_sd is None and bedtime_sd is None:
        return {"score": None, "wake_sd_hours": None, "bedtime_sd_hours": None}

    sds = [sd for sd in (wake_sd, bedtime_sd) if sd is not None]
    avg_sd = sum(sds) / len(sds)
    # SD of 0h -> 100; SD of 2.5h -> 0 (a "goes to bed within ~15min most
    # nights" athlete scores high, someone routinely off by 2h+ scores low)
    score = round(max(0.0, min(100.0, 100.0 - avg_sd * 40.0)))

    return {
        "score": score,
        "wake_sd_hours": round(wake_sd, 2) if wake_sd is not None else None,
        "bedtime_sd_hours": round(bedtime_sd, 2) if bedtime_sd is not None else None,
    }


def _wake_hours_from_db(conn: sqlite3.Connection, date_str: str, window_days: int) -> dict[str, float]:
    """sleep_wake_hour only lives in raw_daily_metrics (not the daily_metrics wide
    table), so resolve it per-date with the usual garmin-first source priority."""
    wake_rows = conn.execute(
        """SELECT date,
               MIN(CASE WHEN source='garmin' THEN value END) AS garmin_val,
               MIN(CASE WHEN source='google_health' THEN value END) AS gh_val
           FROM raw_daily_metrics
           WHERE metric='sleep_wake_hour' AND date <= ? AND date > date(?, ?)
           GROUP BY date""",
        (date_str, date_str, f"-{window_days} days"),
    ).fetchall()
    return {r[0]: r[1] if r[1] is not None else r[2] for r in wake_rows}


def _circular_mean_hours(hours: list[float]) -> float | None:
    if not hours:
        return None
    angles = [h / 24 * 2 * math.pi for h in hours]
    sin_sum = sum(math.sin(a) for a in angles)
    cos_sum = sum(math.cos(a) for a in angles)
    if sin_sum == 0 and cos_sum == 0:
        return None
    return (math.atan2(sin_sum, cos_sum) / (2 * math.pi) * 24) % 24


def average_wake_hour_from_db(conn: sqlite3.Connection, date_str: str | None = None, window_days: int = 7) -> float | None:
    """
    Trailing circular-mean wake hour, for anchoring bedtime recommendations on a
    typical wake time rather than a single (possibly anomalous — early alarm, travel,
    bad night) day's actual wake. Falls back to None if no recent data, letting the
    caller fall back to today's actual wake_hour.
    """
    if date_str is None:
        date_str = date.today().isoformat()
    wake_by_date = _wake_hours_from_db(conn, date_str, window_days)
    return _circular_mean_hours([h for h in wake_by_date.values() if h is not None])


def sleep_regularity_from_db(conn: sqlite3.Connection, date_str: str | None = None, window_days: int = 14) -> dict:
    if date_str is None:
        date_str = date.today().isoformat()

    wake_by_date = _wake_hours_from_db(conn, date_str, window_days)
    if not wake_by_date:
        return sleep_regularity([])

    dur_rows = conn.execute(
        """SELECT date, sleep_duration_min FROM daily_metrics
           WHERE date <= ? AND date > date(?, ?)""",
        (date_str, date_str, f"-{window_days} days"),
    ).fetchall()
    dur_by_date = {r[0]: r[1] for r in dur_rows}

    nights = [
        (wake, dur_by_date.get(d))
        for d, wake in wake_by_date.items()
        if wake is not None
    ]
    return sleep_regularity(nights)


def alertness_curve(
    wake_hour: float,
    sleep_score: float = 75.0,
    hours: int = 17,
    caffeine_doses: list[tuple[float, float]] | None = None,
    activity_boosts: list[tuple[float, float]] | None = None,
    strain_events: list[tuple[float, float]] | None = None,
    strain_tau_hours: float = 6.0,
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
        strain_events: list of (TRIMP, hours_since_wake_when_activity_ended).
        strain_tau_hours: exponential decay time constant for exercise strain.

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

        # A TRIMP of 100 gives a ~10 point initial penalty, capped at 18
        # points. This describes intraday availability, not multi-day recovery.
        strain_debt = 0.0
        if strain_events:
            for trimp, end_hours_since_wake in strain_events:
                hours_since_end = t - end_hours_since_wake
                if hours_since_end >= 0:
                    initial_penalty = min(0.18, max(0.0, trimp) / 1000.0)
                    strain_debt += initial_penalty * math.exp(-hours_since_end / max(0.1, strain_tau_hours))
        strain_debt = min(0.18, strain_debt)

        raw = C * 0.65 + (1 - S_effective) * 0.35
        base_alertness = max(5.0, min(100.0, raw * quality * 100))
        alertness = round(max(5.0, base_alertness - strain_debt * 100), 1)

        result.append({"hour": round(hour, 3), "alertness": alertness,
                       "exercise_strain": round(strain_debt * 100, 1)})

    return result


def trimp_from_hr_samples(
    hr_samples: list[float], duration_minutes: float, resting_hr: float,
    max_hr: float = 190.0, min_samples: int = 10,
) -> float | None:
    """Return an HR-reserve-weighted TRIMP estimate; reject sparse coverage."""
    values = [float(v) for v in hr_samples if v is not None and v > 0]
    if len(values) < min_samples or duration_minutes <= 0 or max_hr <= resting_hr:
        return None
    reserve = max_hr - resting_hr
    weighted = []
    for bpm in values:
        hrr = min(1.0, max(0.0, (bpm - resting_hr) / reserve))
        weighted.append(hrr * math.exp(1.92 * hrr))
    return round(duration_minutes * sum(weighted) / len(weighted), 2)
