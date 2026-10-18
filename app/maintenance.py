"""
Database maintenance: size report and a guarded in-place VACUUM.

SQLite never gives freed pages back to the filesystem, so after payload compaction
the file stays at its high-water mark until a VACUUM rewrites it. VACUUM needs spare
room (a temp copy of the live data plus the same again in the WAL), takes the write
lock for its duration, and must not run while there isn't enough disk — hence the
checks, and why it's an explicit admin action rather than automatic.
"""

import logging
import os
import sqlite3
import tempfile
import time

import app.db as dbm

log = logging.getLogger(__name__)

SAFETY_FACTOR = 1.3
MIN_RECLAIMABLE_MB = 50  # below this a VACUUM (full lock, full rewrite) isn't worth it


def _free_bytes(path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def db_stats() -> dict:
    path = dbm.DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()
    mb = lambda b: round(b / 1048576, 1)
    live = (pages - free) * page_size
    return {
        "file_mb": mb(pages * page_size),
        "live_mb": mb(live),
        "reclaimable_mb": mb(free * page_size),
        "volume_free_mb": mb(_free_bytes(path.parent)),
        "temp_free_mb": mb(_free_bytes(tempfile.gettempdir())),
        "vacuum_needs_mb": mb(live * SAFETY_FACTOR),
    }


def vacuum_blockers() -> list[str]:
    """Reasons a VACUUM must not start right now (empty = safe)."""
    stats = db_stats()
    problems = []
    if stats["reclaimable_mb"] < MIN_RECLAIMABLE_MB:
        problems.append(f"only {stats['reclaimable_mb']}MB reclaimable — nothing worth vacuuming")
    need = stats["vacuum_needs_mb"]
    if stats["volume_free_mb"] < need:
        problems.append(f"volume has {stats['volume_free_mb']}MB free, need ~{need}MB (WAL copy of the live data)")
    if stats["temp_free_mb"] < need:
        problems.append(f"temp dir has {stats['temp_free_mb']}MB free, need ~{need}MB")
    return problems


def vacuum_db() -> dict:
    """VACUUM in place, then checkpoint so the file actually shrinks. Holds the write
    lock throughout; other writers wait (busy_timeout) rather than fail."""
    before = db_stats()
    started = time.monotonic()
    conn = dbm.get_connection()
    conn.isolation_level = None  # VACUUM cannot run inside a transaction
    try:
        log.warning("VACUUM starting: %s", before)
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    after = db_stats()
    log.warning("VACUUM finished in %.1fs: %s", time.monotonic() - started, after)
    return {"before": before, "after": after, "seconds": round(time.monotonic() - started, 1)}


def reparse_garmin_hrv(conn) -> dict:
    """Re-derive Garmin's stored HRV from the raw payloads we kept.

    Before the lastNightAvg fix, the importer read a `lastNight` key that real payloads
    don't have and fell back to the *weekly* average, so most history holds a smoothed
    weekly figure instead of the night's value. The verbatim payload is still stored for
    every one of those days, so nothing needs to be re-fetched: re-run the (fixed)
    parser over the newest payload per date. A day whose payload has no nightly figure
    gets its wrong stored value removed rather than left as a weekly average.

    Returns counts plus the affected dates; the caller re-normalizes those dates."""
    from app.importers.garmin import _parse_hrv  # single source of truth for the parse
    import json

    latest: dict[str, int] = {}
    # id/date sit before the payload column, so this scan doesn't read payload overflow pages
    for rid, day in conn.execute(
        "SELECT id, date FROM raw_import_payloads WHERE endpoint='get_hrv_data' AND date IS NOT NULL"
    ):
        if rid > latest.get(day, -1):
            latest[day] = rid

    changed: list[str] = []
    counts = {"checked": 0, "corrected": 0, "removed": 0, "unchanged": 0}
    for day in sorted(latest):
        counts["checked"] += 1
        payload = conn.execute("SELECT payload_json FROM raw_import_payloads WHERE id=?", (latest[day],)).fetchone()[0]
        before = conn.execute(
            "SELECT value FROM raw_daily_metrics WHERE date=? AND source='garmin' AND metric='hrv'", (day,)
        ).fetchone()
        stored = before[0] if before else None
        if _parse_hrv(conn, day, json.loads(payload)):
            after = conn.execute(
                "SELECT value FROM raw_daily_metrics WHERE date=? AND source='garmin' AND metric='hrv'", (day,)
            ).fetchone()[0]
            if stored is None or abs(after - stored) > 0.001:
                counts["corrected"] += 1
                changed.append(day)
            else:
                counts["unchanged"] += 1
        elif stored is not None:
            conn.execute("DELETE FROM raw_daily_metrics WHERE date=? AND source='garmin' AND metric='hrv'", (day,))
            counts["removed"] += 1
            changed.append(day)
        else:
            counts["unchanged"] += 1
        if (counts["checked"] % 100) == 0:
            conn.commit()
    conn.commit()
    return {**counts, "dates": changed}


def reparse_garmin_hrv_and_renormalize() -> dict:
    """reparse_garmin_hrv, then refresh daily_metrics for just the dates that changed."""
    from app.normalize import rebuild_daily_metrics

    with dbm.db() as conn:
        result = reparse_garmin_hrv(conn)
    dates = result.pop("dates")
    for i in range(0, len(dates), 50):  # small committed chunks: never a long write lock
        with dbm.db() as conn:
            rebuild_daily_metrics(conn, set(dates[i:i + 50]))
    log.warning("HRV reparse: %s; re-normalized %d dates", result, len(dates))
    return {**result, "renormalized_dates": len(dates)}



def reparse_garmin_sleep(conn: sqlite3.Connection) -> dict:
    """Re-run the current sleep parser over the newest retained get_sleep_data payload for
    each date, so fields added to _parse_sleep later (episode start/end, Garmin sleep need)
    cover history, not just days imported since. Upserts only; idempotent."""
    from app.importers.garmin import _parse_sleep
    import json

    latest: dict[str, int] = {}
    for rid, day in conn.execute(
        "SELECT id, date FROM raw_import_payloads "
        "WHERE source='garmin' AND endpoint='get_sleep_data' AND date IS NOT NULL"
    ):
        if rid > latest.get(day, -1):
            latest[day] = rid

    dates: list[str] = []
    for i, day in enumerate(sorted(latest), 1):
        payload = conn.execute("SELECT payload_json FROM raw_import_payloads WHERE id=?", (latest[day],)).fetchone()[0]
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if isinstance(data, dict) and _parse_sleep(conn, day, data):
            dates.append(day)
        if i % 100 == 0:
            conn.commit()
    conn.commit()
    return {"reparsed": len(dates), "dates": dates}


def reparse_garmin_sleep_and_renormalize() -> dict:
    """reparse_garmin_sleep, then refresh daily_metrics for the dates it touched."""
    from app.normalize import rebuild_daily_metrics

    with dbm.db() as conn:
        result = reparse_garmin_sleep(conn)
    dates = result.pop("dates")
    for i in range(0, len(dates), 50):  # small committed chunks: never a long write lock
        with dbm.db() as conn:
            rebuild_daily_metrics(conn, set(dates[i:i + 50]))
    log.warning("sleep reparse: %s; re-normalized %d dates", result, len(dates))
    return {**result, "renormalized_dates": len(dates)}
