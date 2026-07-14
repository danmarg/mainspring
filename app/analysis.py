"""Lag-shifted correlation analysis between behavior inputs and recovery outputs."""

import statistics
from datetime import date, timedelta

DEFAULT_INPUTS = ["alcohol_units", "caffeine_mg", "calories_estimated", "weight_kg"]
DEFAULT_OUTPUTS = ["hrv", "sleep_score", "body_battery_high", "resting_hr", "stress_avg"]


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
) -> dict:
    """
    Compute lag-shifted Pearson or Spearman correlations between behavior
    inputs and recovery outputs over the last `days` days.

    lag=1 means output measured 1 day AFTER the input date (the classic
    "last night's alcohol → this morning's HRV" pattern).
    """
    inp_cols = inputs or DEFAULT_INPUTS
    out_cols = outputs or DEFAULT_OUTPUTS
    lag_list = lags if lags is not None else [0, 1, 2]

    end = date.today()
    start = end - timedelta(days=days - 1)

    # Deduplicated column list preserving order
    all_cols = list(dict.fromkeys(inp_cols + out_cols))
    col_sql = ", ".join(all_cols)

    rows = conn.execute(
        f"SELECT date, {col_sql} FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    data: dict[str, dict[str, float | None]] = {}
    for row in rows:
        data[row[0]] = {col: row[i + 1] for i, col in enumerate(all_cols)}

    all_dates = sorted(data.keys())
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
                    if d_lag not in data:
                        continue
                    y = data[d_lag].get(out)
                    if y is None:
                        continue
                    pairs.append((float(x), float(y)))

                if len(pairs) < min_pairs:
                    continue

                xs = [p[0] for p in pairs]
                ys = [p[1] for p in pairs]

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
                })

    correlations.sort(key=lambda c: abs(c["r"]), reverse=True)

    top = []
    for c in correlations[:10]:
        direction = "positive" if c["r"] > 0 else "negative"
        top.append(
            f"{c['input']} → {c['output']} (lag {c['lag_days']}d): "
            f"r={c['r']}, n={c['n_pairs']} [{direction}]"
        )

    return {
        "period_days": days,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "n_correlations": len(correlations),
        "correlations": correlations,
        "top_findings": top,
    }
