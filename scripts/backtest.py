"""Replay Mainspring's coaching signals over a date range and diff them against Garmin.

Run against a snapshot (GET /export/db), never the live volume:

    python scripts/backtest.py mainspring.db --start 2026-07-01 --end 2026-10-25 \
        [--csv out.csv]

For each day it recomputes, with the *current* code, what get_workout_context would
have shown: readiness score/label, HR zones and their source, the three-zone intensity
split, Garmin's load focus verdict, and the consistency checks. It prints the days
where Mainspring and Garmin disagree plus a summary, so a change to readiness,
zones or the intensity model can be checked against real history before it ships.
The summer-2026 regression (weeks of "grey zone" while Garmin reported a high-aerobic
shortage) shows up here as a run of intensity_vs_garmin_load_focus conflicts.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.normalize import resolve_hr_zones
from app.readiness import (
    _hrv_method,
    consistency_checks,
    garmin_load_focus_from_db,
    intensity_distribution_from_db,
    readiness_from_db,
    usual_hrv_method,
)

FIELDS = [
    "date", "readiness_score", "readiness_label", "garmin_training_readiness",
    "zones_source", "z2_min", "z2_max", "easy_pct", "moderate_pct", "hard_pct",
    "grey_zone_flag", "garmin_load_feedback", "checks",
]


def day_row(conn: sqlite3.Connection, d: str) -> dict:
    readiness = readiness_from_db(conn, d)
    metrics = conn.execute(
        "SELECT training_readiness, hrv, source_flags_json FROM daily_metrics WHERE date=?", (d,)
    ).fetchone()
    garmin_tr, hrv, flags = metrics if metrics else (None, None, None)
    resolved = resolve_hr_zones(conn, d)
    zones_source, zones = resolved if resolved else (None, [])
    z2 = next((z for z in zones if z["zone"] == 2), {})
    intensity = intensity_distribution_from_db(conn, d)
    load_focus = garmin_load_focus_from_db(conn, d)
    checks = consistency_checks(
        readiness, garmin_tr, intensity, load_focus, zones_source,
        hrv_method_today=_hrv_method(flags) if hrv is not None else None,
        hrv_method_usual=usual_hrv_method(conn, d),
    )
    return {
        "date": d,
        "readiness_score": readiness.get("score"),
        "readiness_label": readiness.get("label"),
        "garmin_training_readiness": garmin_tr,
        "zones_source": zones_source,
        "z2_min": z2.get("min_bpm"),
        "z2_max": z2.get("max_bpm"),
        "easy_pct": intensity.get("easy_pct"),
        "moderate_pct": intensity.get("moderate_pct"),
        "hard_pct": intensity.get("hard_pct"),
        "grey_zone_flag": intensity.get("grey_zone_flag"),
        "garmin_load_feedback": (load_focus or {}).get("feedback"),
        "checks": ";".join(f"{c['check']}:{c['status']}" for c in checks),
    }


def backtest(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    rows = []
    d = start
    while d <= end:
        rows.append(day_row(conn, d.isoformat()))
        d += timedelta(days=1)
    return rows


def summarize(rows: list[dict]) -> str:
    counts = Counter()
    for r in rows:
        for c in filter(None, r["checks"].split(";")):
            counts[c] += 1
    labels = Counter(r["readiness_label"] for r in rows)
    lines = [f"{len(rows)} days", "readiness labels: " + ", ".join(f"{k}={v}" for k, v in labels.most_common())]
    lines += [f"  {k}: {v} days" for k, v in counts.most_common()] or ["  no consistency issues"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", type=Path, help="SQLite snapshot from /export/db")
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--csv", type=Path, help="write every day's row here")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = backtest(conn, args.start, args.end)
    conn.close()

    if args.csv:
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
    for r in rows:
        if r["checks"]:
            print(f"{r['date']}  readiness {r['readiness_score']} ({r['readiness_label']})  "
                  f"Z2 {r['z2_min']}-{r['z2_max']} [{r['zones_source']}]  {r['checks']}")
    print(summarize(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
