"""Smoke test for scripts/backtest.py against a small synthetic snapshot."""

import json
import sys
from pathlib import Path

import app.db as db_module

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backtest


def _snapshot(tmp_path):
    path = tmp_path / "snap.db"
    db_module.init_db(path)
    conn = db_module.get_connection(path)
    for i in range(1, 15):
        d = f"2026-08-{i:02d}"
        conn.execute("INSERT INTO daily_metrics(date, hrv, resting_hr, max_hr, source_flags_json) "
                     "VALUES (?,?,?,?,?)", (d, 33.0, 49.0, 165.0, json.dumps({"hrv": "garmin"})))
    conn.execute("INSERT INTO activities(date, start_time, type, garmin_activity_id) "
                 "VALUES ('2026-08-10','2026-08-10T08:00:00Z','running','a1')")
    for z, lo in {1: 95, 2: 111, 3: 127, 4: 143, 5: 161}.items():
        conn.execute("INSERT INTO activity_hr_zones(activity_id, zone, seconds, low_bpm) VALUES ('a1',?,600,?)",
                     (z, lo))
    conn.commit()
    conn.close()
    return path


def test_backtest_runs_and_reports_garmin_zones(tmp_path, capsys):
    path = _snapshot(tmp_path)
    out_csv = tmp_path / "out.csv"
    assert backtest.main([str(path), "--start", "2026-08-11", "--end", "2026-08-14", "--csv", str(out_csv)]) == 0
    lines = out_csv.read_text().splitlines()
    assert lines[0].split(",") == backtest.FIELDS
    assert len(lines) == 5
    assert ",garmin,111,126," in lines[1]
    assert "4 days" in capsys.readouterr().out


def test_backtest_uses_the_days_own_max_hr_and_lthr(tmp_path):
    # No zone-tagged activities, so zones come from LTHR. The day's LTHR changed to 170;
    # a replay that ignored it would use the earlier 150 and report different bands.
    path = tmp_path / "snap.db"
    db_module.init_db(path)
    conn = db_module.get_connection(path)
    conn.execute("INSERT INTO daily_metrics(date, max_hr, lactate_threshold_hr) VALUES ('2026-08-01', 170, 150)")
    conn.execute("INSERT INTO daily_metrics(date, max_hr, lactate_threshold_hr) VALUES ('2026-08-02', 180, 170)")
    conn.commit()
    row = backtest.day_row(conn, "2026-08-02")
    conn.close()
    assert row["zones_source"] == "lthr"
    assert row["z2_min"] == round(170 * 0.685)  # from 170, not yesterday's 150


def test_backtest_reports_unflagged_hrv_days(tmp_path):
    path = _snapshot(tmp_path)
    conn = db_module.get_connection(path)
    conn.execute("INSERT INTO daily_metrics(date, hrv, source_flags_json) VALUES ('2026-07-01', 40, '{}')")
    conn.commit()
    assert backtest.unflagged_hrv_days(conn) == ["2026-07-01"]
    conn.close()
