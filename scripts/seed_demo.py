"""Seed a database with synthetic demo data — for README screenshots and local UI work.

    python scripts/seed_demo.py /tmp/demo.db [--days 45] [--end 2026-11-01]

Everything here is generated (seeded RNG, smooth trends plus noise): no real person's
data. Writes raw metrics, Garmin-style activities, manual logs, goals and an event,
then runs the normal normalization pipeline so the dashboard reads it like real data.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_connection, init_db, upsert_raw_metric, utc_now
from app.normalize import run_normalization

SOURCE = "garmin"
MEALS = [
    ("Oatmeal with berries and yogurt", 520, 22, 78, 12),
    ("Lentil soup with bread", 610, 28, 85, 14),
    ("Pasta with tomato sauce and parmesan", 780, 30, 112, 20),
    ("Rice bowl with tofu and vegetables", 690, 32, 95, 18),
    ("Bagel with cream cheese", 410, 14, 58, 12),
    ("Banana and peanut butter", 330, 9, 38, 16),
    ("Greek salad with chickpeas", 540, 22, 40, 30),
    ("Protein shake", 230, 30, 12, 6),
]


def _wave(i: int, period: float, amp: float) -> float:
    return amp * math.sin(2 * math.pi * i / period)


def seed(path: Path, days: int, end: date, rng: random.Random) -> None:
    init_db(path)
    conn = get_connection(path)
    now = utc_now()
    start = end - timedelta(days=days - 1)
    ctl, atl = 480.0, 470.0
    for i in range(days):
        d = start + timedelta(days=i)
        ds = d.isoformat()
        weekday = d.weekday()
        run_day = weekday != 0  # Mondays off
        long_run = weekday == 6
        quality = weekday in (1, 3)

        # Activities
        load = 0.0
        if run_day:
            minutes = 120 if long_run else (55 if quality else 45) + rng.randint(-5, 10)
            avg_hr = 150 if quality else (135 if long_run else 128) + rng.randint(-3, 3)
            dist = minutes * 60 / (300 if quality else 345)
            start_t = datetime(d.year, d.month, d.day, 7, 30, tzinfo=UTC)
            label = "LACTATE_THRESHOLD" if quality else "AEROBIC_BASE"
            conn.execute(
                """INSERT OR REPLACE INTO garmin_activities(activity_id, date, start_time, type,
                   duration_s, distance_m, avg_hr, max_hr, training_effect_aerobic, calories,
                   decoupling_pct, raw_json, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"demo-{ds}", ds, start_t.isoformat().replace("+00:00", "Z"), "running",
                 minutes * 60, dist * 1000, avg_hr, avg_hr + 15, 3.4 if quality else 2.8,
                 minutes * 11, round(rng.uniform(1, 6), 1),
                 json.dumps({"trainingEffectLabel": label}), now),
            )
            floors = {1: 98, 2: 115, 3: 131, 4: 147, 5: 165}
            split = {1: 0.08, 2: 0.55, 3: 0.27, 4: 0.08, 5: 0.02} if not quality else \
                    {1: 0.05, 2: 0.35, 3: 0.2, 4: 0.35, 5: 0.05}
            for z, frac in split.items():
                conn.execute(
                    "INSERT OR REPLACE INTO activity_hr_zones(activity_id, zone, seconds, low_bpm) VALUES (?,?,?,?)",
                    (f"demo-{ds}", z, minutes * 60 * frac, floors[z]),
                )
            load = minutes * (2.6 if quality else 1.9)
        atl = atl + (load * 7 - atl) / 7
        ctl = ctl + (load * 7 - ctl) / 42

        drank = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM manual_logs WHERE type='alcohol' AND ts LIKE ?",
            ((d - timedelta(days=1)).isoformat() + "%",),
        ).fetchone()[0]
        hrv = 46 + _wave(i, 21, 3) - (2.5 if quality else 0) - 1.6 * drank + rng.gauss(0, 1.8)
        rhr = 50 - _wave(i, 21, 1.2) + (1 if quality else 0) + rng.gauss(0, 0.8)
        sleep_min = 440 + _wave(i, 9, 25) + rng.gauss(0, 20)
        metrics = {
            "hrv": round(hrv), "resting_hr": round(rhr), "sleep_score": round(78 + _wave(i, 9, 6) + rng.gauss(0, 4)),
            "sleep_duration_min": round(sleep_min), "sleep_deep_min": round(sleep_min * 0.18),
            "sleep_rem_min": round(sleep_min * 0.22), "sleep_light_min": round(sleep_min * 0.6),
            "body_battery_high": round(70 + _wave(i, 9, 10) + rng.gauss(0, 5)),
            "body_battery_low": round(18 + rng.gauss(0, 5)), "stress_avg": round(26 + rng.gauss(0, 4)),
            "steps": round(9000 + (6000 if run_day else 0) + rng.gauss(0, 2500)),
            "active_zone_minutes": round(40 + (60 if run_day else 0) + rng.gauss(0, 15)),
            "max_hr": 150 + (15 if quality else 0) + rng.randint(-4, 4),
            "lactate_threshold_hr": 168, "vo2max": 54, "skin_temp_deviation": round(rng.gauss(0, 0.15), 2),
            "sleep_breathing_rate": round(14.5 + rng.gauss(0, 0.3), 1), "atl": round(atl), "ctl": round(ctl),
        }
        for metric, value in metrics.items():
            upsert_raw_metric(conn, ds, SOURCE, metric, float(value), now)

        # Manual logs: meals, caffeine, occasional alcohol
        for h, (desc, kcal, p, c, f) in zip((7, 12, 16, 19), rng.sample(MEALS, 4)):
            ts = datetime(d.year, d.month, d.day, h, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO manual_logs(ts, type, description, estimated_calories, estimated_macros_json, "
                "confidence, created_at) VALUES (?,?,?,?,?,?,?)",
                (ts, "meal", desc, kcal, json.dumps({"protein_g": p, "carbs_g": c, "fat_g": f}),
                 "photo_estimate", now),
            )
        for h in (7, 13, 16):
            if h == 16 and rng.random() < 0.6 or h == 13 and weekday >= 5:
                continue
            desc, mg = rng.choice([("Espresso", 80), ("Flat white", 130), ("Filter coffee", 95), ("Black tea", 45)])
            ts = datetime(d.year, d.month, d.day, h, 30, tzinfo=UTC).isoformat().replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
                (ts, "caffeine", desc, mg, "mg", now),
            )
        if rng.random() < (0.6 if weekday in (4, 5) else 0.12):
            ts = datetime(d.year, d.month, d.day, 19, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO manual_logs(ts, type, description, quantity, unit, created_at) VALUES (?,?,?,?,?,?)",
                (ts, "alcohol", *rng.choice([("Beer", 1.5), ("Glass of wine", 2.0), ("Two glasses of wine", 4.0), ("Cocktail", 2.5)]), "units", now),
            )

    for metric, value, unit in (("weekly_volume_km", 55, "km"), ("weekly_runs", 6, None),
                                ("weekly_strength_sessions", 2, None), ("daily_protein_g", 120, "g")):
        conn.execute("INSERT OR REPLACE INTO training_goals(metric, value, unit, updated_at) VALUES (?,?,?,?)",
                     (metric, value, unit, now))
    conn.execute(
        "INSERT INTO training_events(date, type, description, goal_description, created_at) VALUES (?,?,?,?,?)",
        ((end + timedelta(days=70)).isoformat(), "marathon", "Demo City Marathon", "sub-3:30", now),
    )
    conn.commit()
    run_normalization(conn)
    conn.commit()
    conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", type=Path)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    if args.db.exists():
        ap.error(f"{args.db} exists; pass a new path")
    seed(args.db, args.days, args.end, random.Random(args.seed))
    print(f"seeded {args.days} days ending {args.end} into {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
