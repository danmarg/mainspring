import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("/data/health.db")
SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

HOME_TZ = os.getenv("HOME_TZ", "Europe/Berlin")

DEFAULT_SOURCE_PRIORITY = ["garmin", "google_health"]


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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


def init_db(path: Path | None = None) -> None:
    """Apply schema.sql then any pending migrations, idempotently."""
    with get_connection(path or DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _schema_migrations "
        "(filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()

    applied = {
        row[0]
        for row in conn.execute("SELECT filename FROM _schema_migrations").fetchall()
    }

    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.name in applied:
            continue
        conn.executescript(sql_file.read_text())
        conn.execute(
            "INSERT INTO _schema_migrations(filename, applied_at) VALUES (?, ?)",
            (sql_file.name, utc_now()),
        )
        conn.commit()


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
