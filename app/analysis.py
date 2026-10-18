"""Lag-shifted correlation analysis between behavior inputs and recovery outputs.

Daily health series are not independent samples, so a plain correlation over-claims:
  - Shared slow trends (fitness, season) make unrelated series correlate. Outputs are
    detrended against their own trailing 7-day mean, so the question becomes "was HRV
    lower than *my recent normal* after drinking", not "was HRV low in a low month".
  - Consecutive days are autocorrelated, so n days carry fewer than n independent
    observations. p-values use an effective sample size from the lag-1
    autocorrelations (Bretherton et al. 1999, J Climate 12:1990).
  - A sweep runs dozens of tests and ranks by |r|, so some strong-looking results are
    chance. Benjamini-Hochberg q-values control the false discovery rate across the sweep.
  - Behaviour logs are sparse: a day with no alcohol log is "0 drinks" only if the user
    was logging that day at all. On logging days (any meal/caffeine/alcohol logged)
    missing caffeine/alcohol counts as 0; otherwise it's unknown. Without this the
    analysis only ever compared drinking days with other drinking days.
"""

import math
import statistics
from datetime import date, timedelta

DEFAULT_INPUTS = ["alcohol_units", "caffeine_mg", "calories_estimated", "weight_kg", "rpe"]
DEFAULT_OUTPUTS = ["hrv", "sleep_score", "body_battery_high", "resting_hr", "stress_avg"]

# Inputs where "nothing logged on a day the user was logging" means zero.
ZERO_FILL_INPUTS = {"alcohol_units", "caffeine_mg"}
LOGGING_DAY_COLUMNS = ["calories_estimated", "caffeine_mg", "alcohol_units"]
DETREND_DAYS = 7
DETREND_MIN_DAYS = 4
SIGNIFICANT_Q = 0.10


def _lag1_autocorr(vals: list[float]) -> float:
    if len(vals) < 3:
        return 0.0
    try:
        return statistics.correlation(vals[:-1], vals[1:])
    except statistics.StatisticsError:
        return 0.0


def _p_value(r: float, n_eff: float) -> float:
    """Two-sided p for a correlation via Fisher's z with the effective sample size."""
    if n_eff <= 3:
        return 1.0
    z = math.atanh(max(-0.9999, min(0.9999, r))) * math.sqrt(n_eff - 3)
    return 2 * (1 - statistics.NormalDist().cdf(abs(z)))


def _bh_q_values(ps: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (q), in the input order."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    q = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, ps[i] * m / rank)
        q[i] = running
    return q


def _rank_transform(vals: list[float]) -> list[float]:
    """Average-rank transform for Spearman; handles ties."""
    indexed = sorted(enumerate(vals), key=lambda x: x[1])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) - 1 and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def compute_correlations(
    conn,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    lags: list[int] | None = None,
    days: int = 90,
    min_pairs: int = 14,
    method: str = "pearson",
    max_results: int = 20,
    detrend: bool = True,
) -> dict:
    """
    Compute lag-shifted Pearson or Spearman correlations between behavior
    inputs and recovery outputs over the last `days` days.

    lag=1 means output measured 1 day AFTER the input date (the classic
    "last night's alcohol → this morning's HRV" pattern).

    `correlations` is capped at `max_results` (strongest |r| first) so a
    broad inputs/outputs sweep doesn't dump dozens of weak, sub-threshold
    rows on the caller; `n_correlations` still reports the true total found.

    Each row carries n_eff (autocorrelation-adjusted sample size), p, q (FDR-adjusted
    across every test in this call) and significant (q < SIGNIFICANT_Q). See the
    module docstring for the detrending and zero-fill rules.
    """
    inp_cols = inputs or DEFAULT_INPUTS
    out_cols = outputs or DEFAULT_OUTPUTS
    lag_list = lags if lags is not None else [0, 1, 2]

    end = date.today()
    start = end - timedelta(days=days - 1)

    # Deduplicated column list preserving order
    all_cols = list(dict.fromkeys(inp_cols + out_cols + LOGGING_DAY_COLUMNS))
    col_sql = ", ".join(all_cols)

    # Extra leading days so the first in-window outputs have a detrending baseline
    rows = conn.execute(
        f"SELECT date, {col_sql} FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date",
        ((start - timedelta(days=DETREND_DAYS)).isoformat(), end.isoformat()),
    ).fetchall()

    data: dict[str, dict[str, float | None]] = {}
    for row in rows:
        data[row[0]] = {col: row[i + 1] for i, col in enumerate(all_cols)}

    for vals in data.values():
        if any(vals.get(c) is not None for c in LOGGING_DAY_COLUMNS):
            for col in ZERO_FILL_INPUTS:
                if vals.get(col) is None:
                    vals[col] = 0.0

    def _output(col: str, d_str: str) -> float | None:
        y = data.get(d_str, {}).get(col)
        if y is None or not detrend:
            return y
        day = date.fromisoformat(d_str)
        prior = [data[p][col] for p in
                 ((day - timedelta(days=k)).isoformat() for k in range(1, DETREND_DAYS + 1))
                 if p in data and data[p].get(col) is not None]
        if len(prior) < DETREND_MIN_DAYS:
            return None
        return y - sum(prior) / len(prior)

    all_dates = sorted(d for d in data if d >= start.isoformat())
    correlations = []

    for inp in inp_cols:
        for out in out_cols:
            if inp == out:
                continue
            for lag in lag_list:
                pairs: list[tuple[float, float]] = []
                for d_str in all_dates:
                    x = data[d_str].get(inp)
                    if x is None:
                        continue
                    d_lag = (date.fromisoformat(d_str) + timedelta(days=lag)).isoformat()
                    y = _output(out, d_lag)
                    if y is None:
                        continue
                    pairs.append((float(x), float(y)))

                if len(pairs) < min_pairs:
                    continue

                xs = [p[0] for p in pairs]
                ys = [p[1] for p in pairs]
                # Effective n for two autocorrelated series (Bretherton 1999)
                rho = _lag1_autocorr(xs) * _lag1_autocorr(ys)
                n_eff = max(2.0, min(len(pairs), len(pairs) * (1 - rho) / (1 + rho))) if rho > -1 else len(pairs)

                try:
                    if method == "spearman":
                        xs = _rank_transform(xs)
                        ys = _rank_transform(ys)
                    r = statistics.correlation(xs, ys)
                except statistics.StatisticsError:
                    continue

                correlations.append({
                    "input": inp,
                    "output": out,
                    "lag_days": lag,
                    "r": round(r, 4),
                    "n_pairs": len(pairs),
                    "n_eff": round(n_eff, 1),
                    "p": _p_value(r, n_eff),
                })

    for c, q in zip(correlations, _bh_q_values([c["p"] for c in correlations])):
        c["p"] = round(c["p"], 4)
        c["q"] = round(q, 4)
        c["significant"] = q < SIGNIFICANT_Q

    correlations.sort(key=lambda c: abs(c["r"]), reverse=True)
    n_correlations = len(correlations)
    correlations = correlations[:max_results]

    top = []
    for c in correlations[:10]:
        direction = "positive" if c["r"] > 0 else "negative"
        top.append(
            f"{c['input']} → {c['output']} (lag {c['lag_days']}d): "
            f"r={c['r']}, n={c['n_pairs']}, q={c['q']} [{direction}"
            + ("" if c["significant"] else ", not significant") + "]"
        )

    return {
        "period_days": days,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "n_correlations": n_correlations,
        "correlations": correlations,
        "top_findings": top,
    }
