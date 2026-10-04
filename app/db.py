import logging
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DB_PATH = Path("/data/health.db")
SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"

HOME_TZ = os.getenv("HOME_TZ", "Europe/Berlin")

# Tables holding credentials (OAuth tokens/codes/clients, Google refresh token).
# Never exposed through Datasette or the /export/db snapshot — a single leaked
# read credential must not be convertible into a long-lived MCP/Google token.
SECRET_TABLES = frozenset({
    "mcp_oauth_clients", "mcp_pending_auth", "mcp_auth_codes",
    "mcp_access_tokens", "mcp_refresh_tokens", "google_health_oauth",
})


def deny_secret_reads(action, arg1, arg2, dbname, source):
    """sqlite3 authorizer: reading a secret-table column yields NULL (IGNORE, not
    DENY, so Datasette's table introspection and counts still work)."""
    if action == sqlite3.SQLITE_READ and arg1 in SECRET_TABLES:
        return sqlite3.SQLITE_IGNORE
    return sqlite3.SQLITE_OK


DEFAULT_SOURCE_PRIORITY = ["garmin", "google_health"]


# ── write-lock diagnostics ────────────────────────────────────────────────────
#
# SQLite has one writer. sqlite3's implicit BEGIN opens the write lock at the first
# INSERT/UPDATE/DELETE and holds it until COMMIT/ROLLBACK, so a long-lived writer
# makes every other writer wait up to busy_timeout (5min) and then fail with
# "database is locked". When that happened in production nothing recorded *who*
# held the lock. Each connection now traces its own transactions: a held-too-long
# warning names the holder, and a "locked" error dumps every write transaction
# open at that moment.

LOCK_HOLD_WARN_S = float(os.getenv("LOCK_HOLD_WARN_S", "2"))

_open_txns: dict[int, tuple[str, str, float]] = {}  # id -> (label, thread, begin)
_open_txns_lock = threading.RLock()  # re-entrant: a GC-triggered connection teardown can call back into the tracer on the same thread


def _caller_label() -> str:
    """module:function:line of the first caller outside this file."""
    frame = sys._getframe(1)
    while frame and (frame.f_code.co_filename == __file__ or frame.f_globals.get("__name__") == "contextlib"):
        frame = frame.f_back
    if frame is None:
        return "unknown"
    return f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_name}:{frame.f_lineno}"


def open_write_transactions() -> list[str]:
    now = time.monotonic()
    with _open_txns_lock:
        held = sorted(_open_txns.values(), key=lambda t: t[2])
    return [f"{label} [{thread}] open {now - begin:.1f}s" for label, thread, begin in held]


class _TracedConnection(sqlite3.Connection):
    """Closing with an open transaction (an implicit rollback) emits no COMMIT/
    ROLLBACK statement, so end the trace here or the registry would keep a ghost."""

    _tracer: "_TxnTracer | None" = None

    def close(self):
        if self._tracer is not None:
            self._tracer.finish()
        super().close()


class _TxnTracer:
    """sqlite3 trace callback: times each write transaction (implicit BEGIN → COMMIT)."""

    def __init__(self, label: str):
        self.label = label
        self.thread = threading.current_thread().name
        self.begin: float | None = None

    def __call__(self, statement: str) -> None:
        head = statement[:8].lstrip().upper()
        if head.startswith("BEGIN"):
            self.begin = time.monotonic()
            with _open_txns_lock:
                _open_txns[id(self)] = (self.label, self.thread, self.begin)
        elif head.startswith(("COMMIT", "ROLLBACK", "END")):
            self.finish()

    def finish(self) -> None:
        if self.begin is None:
            return
        held = time.monotonic() - self.begin
        self.begin = None
        with _open_txns_lock:
            _open_txns.pop(id(self), None)
        if held >= LOCK_HOLD_WARN_S:
            log.warning("write transaction held %.1fs by %s [%s]", held, self.label, self.thread)


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=300000")  # retry for up to 5min before raising
    conn.row_factory = sqlite3.Row


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH), factory=_TracedConnection)
    _configure(conn)
    conn._tracer = _TxnTracer(_caller_label())
    conn.set_trace_callback(conn._tracer)
    return conn


@contextmanager
def db(path: Path | None = None):
    conn = get_connection(path or DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception as exc:
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
            log.error(
                "database is locked (caller %s [%s]); write transactions open now: %s",
                _caller_label(), threading.current_thread().name,
                open_write_transactions() or "none (lock held outside a traced txn)",  # oldest first
            )
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

    existing = {row[1] for row in conn.execute("PRAGMA table_info(activities)")}
    for col in ("training_effect_aerobic", "training_effect_anaerobic"):
        if col in existing:
            conn.execute(f"ALTER TABLE activities DROP COLUMN {col}")
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


def _migrate_garmin_start_times(conn: sqlite3.Connection) -> None:
    """One-off, idempotent: garmin_activities.start_time used to hold the naive
    *local* start (startTimeLocal) while every consumer reads naive as UTC. Rewrite
    to the tz-aware UTC start from the stored raw payload. `date` stays local."""
    cur = conn.execute(
        """
        UPDATE garmin_activities
        SET start_time = REPLACE(json_extract(raw_json, '$.startTimeGMT'), ' ', 'T') || '+00:00'
        WHERE json_valid(raw_json)
          AND json_extract(raw_json, '$.startTimeGMT') IS NOT NULL
          AND (start_time IS NULL OR start_time NOT LIKE '%+00:00')
        """
    )
    conn.commit()
    if cur.rowcount > 0:
        # the normalized activities table copied the old values; refresh it now so
        # nothing reads stale naive-local starts until the next import
        from app.normalize import rebuild_activities
        rebuild_activities(conn)
        conn.commit()


def init_db(path: Path | None = None) -> None:
    """Apply schema.sql idempotently, then reconcile any columns added since."""
    with get_connection(path or DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        _drop_legacy_columns(conn)
        _add_missing_columns(conn)
        _migrate_garmin_start_times(conn)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_FIXED_OFFSET = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")


def zone_from_name(name: str | None):
    """tzinfo for a day_timezone value: an IANA name, or the 'UTC+HH:MM' form the
    normalizer emits when an offset has no clean zone. Falls back to HOME_TZ."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            m = _FIXED_OFFSET.match(name)
            if m:
                delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
                return timezone(delta if m.group(1) == "+" else -delta)
    return ZoneInfo(HOME_TZ)


def parse_instant(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp (Z / offset / naive-as-UTC, any fraction length)
    to an aware UTC datetime."""
    value = re.sub(r"(\.\d{6})\d+", r"\1", ts.strip().replace("Z", "+00:00"))
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def local_health_date(conn: sqlite3.Connection, ts: str) -> str:
    """Health date (YYYY-MM-DD) of a UTC instant: the instant converted to that
    day's zone from day_timezone. The zone is keyed by *local* date, which is what
    we're solving for, so try the neighbouring days: the first day D whose recorded
    zone puts this instant on D itself is the answer. With no self-consistent row
    (no data for those days), fall back to HOME_TZ."""
    dt = parse_instant(ts)
    utc_day = dt.date()
    candidates = [(utc_day + timedelta(days=d)).isoformat() for d in (-1, 0, 1)]
    zones = dict(conn.execute(
        f"SELECT date, tz FROM day_timezone WHERE date IN ({','.join('?' * 3)})", candidates
    ).fetchall())
    for day in candidates:
        if day in zones and dt.astimezone(zone_from_name(zones[day])).date().isoformat() == day:
            return day
    return dt.astimezone(ZoneInfo(HOME_TZ)).date().isoformat()


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


_MULTI_ITEM_ENDPOINTS = frozenset({"activity", "scheduled_workout", "get_body_battery_item"})


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
    if endpoint in _MULTI_ITEM_ENDPOINTS or date is None:
        # Several distinct payloads share one (source, endpoint, date) — two
        # activities on a day, or per-activity splits/zones stored with date NULL — so "identical to the latest" never matches (A,B,A,B
        # alternate) and every run re-inserted them all. Dedupe against any stored
        # copy instead; ordering doesn't matter for these (derived tables are keyed
        # by the item's own id).
        if conn.execute(
            "SELECT 1 FROM raw_import_payloads WHERE source=? AND endpoint=? AND date IS ? "
            "AND payload_json=? LIMIT 1",
            (source, endpoint, date, payload_json),
        ).fetchone():
            return
    else:
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
