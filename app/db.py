import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("/data/health.db")
SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"

HOME_TZ = os.getenv("HOME_TZ", "Europe/Berlin")

DEFAULT_SOURCE_PRIORITY = ["garmin", "google_health"]


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=300000")  # retry for up to 5min before raising
    conn.row_factory = sqlite3.Row


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH))
    _configure(conn)
    return conn


@contextmanager
def db(path: Path | None = None):
    conn = get_connection(path or DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _drop_legacy_columns(conn: sqlite3.Connection) -> None:
    """Drop columns removed from schema.sql that may exist in older DBs."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    for col in ("readiness_score",):
        if col in existing:
            conn.execute(f"ALTER TABLE daily_metrics DROP COLUMN {col}")
    conn.commit()


# Columns added to daily_metrics after the table's initial CREATE TABLE IF NOT EXISTS.
# executescript is a no-op for a column added to a CREATE TABLE statement once the table
# already exists, so new columns must be ALTER'd in explicitly (SQLite has no
# "ADD COLUMN IF NOT EXISTS", hence the manual existence check).
_ADDED_DAILY_METRICS_COLUMNS = [
    ("skin_temp_deviation", "REAL"),
    ("hydration_ml", "REAL"),
    ("max_hr", "REAL"),
    ("lactate_threshold_hr", "REAL"),
    ("lactate_threshold_pace_min_per_km", "REAL"),
    ("ftp_watts", "REAL"),
    ("sleep_breathing_rate", "REAL"),
    ("recovery_hours", "REAL"),
]


_ADDED_MANUAL_LOGS_COLUMNS = [
    ("garmin_synced_at", "TEXT"),
]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    for col, col_type in _ADDED_DAILY_METRICS_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE daily_metrics ADD COLUMN {col} {col_type}")

    existing = {row[1] for row in conn.execute("PRAGMA table_info(manual_logs)")}
    for col, col_type in _ADDED_MANUAL_LOGS_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE manual_logs ADD COLUMN {col} {col_type}")

    conn.commit()


def init_db(path: Path | None = None) -> None:
    """Apply schema.sql idempotently, then reconcile any columns added since."""
    with get_connection(path or DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        _drop_legacy_columns(conn)
        _add_missing_columns(conn)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def health_date(ts: str, date_override: str | None = None) -> str:
    """
    Convert a UTC ISO-8601 instant to its local health date string (YYYY-MM-DD).
    Looks up day_timezone for the approximate date; falls back to HOME_TZ.
    date_override bypasses the lookup (use for sleep, where provider assigns the date).
    """
    if date_override:
        return date_override

    dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    tz_name = HOME_TZ  # will be refined once day_timezone is populated

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(HOME_TZ)

    return dt.astimezone(tz).strftime("%Y-%m-%d")


def upsert_raw_metric(
    conn: sqlite3.Connection,
    date: str,
    source: str,
    metric: str,
    value: float | None,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO raw_daily_metrics(date, source, metric, value, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date, source, metric) DO UPDATE SET
          value      = excluded.value,
          fetched_at = excluded.fetched_at
        """,
        (date, source, metric, value, fetched_at),
    )


def upsert_raw_payload(
    conn: sqlite3.Connection,
    source: str,
    endpoint: str,
    payload_json: str,
    date: str | None = None,
    fetched_at: str | None = None,
) -> None:
    """Insert the raw payload, skipping it if byte-identical to the most recent
    stored payload for this (source, endpoint, date) — rolling-window imports
    re-fetch the same days repeatedly, so most re-fetches have unchanged
    content and would otherwise duplicate storage forever."""
    row = conn.execute(
        """
        SELECT payload_json FROM raw_import_payloads
        WHERE source=? AND endpoint=? AND date IS ?
        ORDER BY id DESC LIMIT 1
        """,
        (source, endpoint, date),
    ).fetchone()
    if row and row[0] == payload_json:
        return
    conn.execute(
        """
        INSERT INTO raw_import_payloads(source, endpoint, date, payload_json, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source, endpoint, date, payload_json, fetched_at or utc_now()),
    )


def resolve_metric(
    conn: sqlite3.Connection,
    date: str,
    metric: str,
) -> tuple[float | None, str | None]:
    """
    Return (value, source) for the canonical source of a metric on a given date.
    Consults source_config first; falls back to DEFAULT_SOURCE_PRIORITY.
    Returns (None, None) if no source has data.
    """
    row = conn.execute(
        "SELECT canonical_source FROM source_config WHERE metric = ?", (metric,)
    ).fetchone()

    priority = [row[0]] if row else DEFAULT_SOURCE_PRIORITY

    for source in priority:
        row = conn.execute(
            "SELECT value FROM raw_daily_metrics WHERE date=? AND source=? AND metric=?",
            (date, source, metric),
        ).fetchone()
        if row and row[0] is not None:
            return row[0], source

    # fall back to any source that has it
    row = conn.execute(
        "SELECT value, source FROM raw_daily_metrics "
        "WHERE date=? AND metric=? AND value IS NOT NULL LIMIT 1",
        (date, metric),
    ).fetchone()
    if row:
        return row[0], row[1]

    return None, None
