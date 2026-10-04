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
