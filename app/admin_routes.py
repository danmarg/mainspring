import logging
import os
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.auth import ADMIN, EXPORT, require_bearer
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.db import HOME_TZ, SECRET_TABLES, db, resolve_metric, utc_now

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

_import_auth = require_bearer(ADMIN)
_export_auth = require_bearer(EXPORT)

MORNING_WEBHOOK_EARLIEST_HOUR = int(os.getenv("MORNING_WEBHOOK_EARLIEST_HOUR", "5"))
WAKE_CONFIRM_WINDOW_MIN = int(os.getenv("MORNING_WEBHOOK_CONFIRM_WINDOW_MIN", "20"))
WAKE_CONFIRM_HR_MARGIN = float(os.getenv("MORNING_WEBHOOK_CONFIRM_HR_MARGIN", "8"))


def _recently_active(conn, today: str) -> bool | None:
    """Whether recent intraday HR shows the user is actually up and moving,
    vs. still resting — distinguishes a real wake-up from a brief nighttime
    wake that made Garmin finalize sleepEndTimestampLocal early (e.g. a
    bathroom trip at 5am) before going back to sleep. Returns None when there
    isn't enough recent data to judge (e.g. import ran right as they woke,
    before the watch has synced fresh samples, or resting_hr hasn't resolved
    yet) — callers should not block firing on a None, only on an explicit
    False, since missing data is common and shouldn't suppress a real wake.
    """
    resting_hr, _ = resolve_metric(conn, today, "resting_hr")
    if resting_hr is None:
        return None

    window_start = (datetime.now(timezone.utc) - timedelta(minutes=WAKE_CONFIRM_WINDOW_MIN)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    rows = conn.execute(
        "SELECT bpm FROM intraday_hr WHERE ts >= ? ORDER BY ts", (window_start,)
    ).fetchall()
    if not rows:
        return None

    avg_bpm = sum(r[0] for r in rows) / len(rows)
    return avg_bpm >= resting_hr + WAKE_CONFIRM_HR_MARGIN


def _is_morning_locally(conn, today: str) -> bool:
    """True once the user is actually likely to be awake.

    Gate on the detected sleep_wake_hour (from Garmin's sleepEndTimestampLocal
    or Google Health's last sleep sample — whichever resolve_metric picks) once
    it's landed for today, since a fixed clock time can't tell "already awake"
    from "still asleep, but data happened to sync early" (e.g. a phone-only
    night with no watch worn). MORNING_WEBHOOK_EARLIEST_HOUR still acts as an
    absolute floor — never fire before it even if a detected wake_hour is
    implausibly early (bad sample, timezone glitch, etc) — and as the fallback
    heuristic when no wake_hour has resolved yet for today.

    A detected wake_hour on its own still isn't enough: Garmin finalizes
    sleepEndTimestampLocal on the first sustained movement, which a brief
    nighttime wake (bathroom trip, checking the time) can trigger even though
    the user goes right back to sleep for another hour or two. So once
    wake_hour has passed, cross-check against recent intraday HR via
    _recently_active — if it clearly shows them still at resting HR right
    now, treat wake_hour as a false start rather than firing. Absence of
    recent HR data (None) doesn't block firing, since that's the common case
    right after a real wake, before fresh samples have synced.
    """
    row = conn.execute("SELECT tz FROM day_timezone WHERE date=?", (today,)).fetchone()
    tz_name = row[0] if row else HOME_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(HOME_TZ)
    local_now = datetime.now(timezone.utc).astimezone(tz)
    local_now_hour = local_now.hour + local_now.minute / 60

    if local_now_hour < MORNING_WEBHOOK_EARLIEST_HOUR:
        return False

    wake_hour, _ = resolve_metric(conn, today, "sleep_wake_hour")
    if wake_hour is not None:
        if local_now_hour < wake_hour:
            return False
        return _recently_active(conn, today) is not False

    return True


def _fire_morning_webhook() -> bool:
    """Fire the morning webhook. Returns True if the request succeeded."""
    url = os.getenv("MORNING_WEBHOOK_URL", "").strip()
    if not url:
        return False
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    secret = os.getenv("MORNING_WEBHOOK_SECRET", "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("morning webhook fired → %s", resp.status)
            return True
    except Exception as exc:
        log.warning("morning webhook failed: %s", exc)
        return False


# ── single-flight imports ─────────────────────────────────────────────────────
#
# Imports take minutes (and far longer under contention) while triggers arrive every
# 20 minutes from more than one caller, so without a guard runs pile up — ~20
# google_health imports started in one burst after an outage — all fetching from the
# same upstream and fighting over the one SQLite write lock. One import per source at
# a time; the process is the only runner (single Machine), so an in-memory registry
# is authoritative and resets on restart.

IMPORT_MAX_AGE_S = 3600  # a run older than this is presumed hung and no longer blocks a new one
_running_imports: dict[str, tuple[int, float]] = {}  # source -> (run_id, started monotonic)
_running_lock = threading.Lock()
_normalization_lock = threading.Lock()  # normalization is global (activities rebuild); never run two at once


def mark_interrupted_imports() -> int:
    """At startup nothing can legitimately still be running: any import_runs row left
    'running' belongs to a process that was restarted/killed mid-import."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE import_runs SET status='error', finished_at=?, error='interrupted (process restarted)' "
            "WHERE status='running'", (utc_now(),),
        )
        return cur.rowcount


def _start_import(source: str, background_tasks, import_fn, days, start_date, end_date) -> dict:
    explicit_range = start_date is not None or end_date is not None
    with _running_lock:
        current = _running_imports.get(source)
        if current and time.monotonic() - current[1] < IMPORT_MAX_AGE_S:
            if explicit_range:
                # a backfill must not silently turn into "someone else's import"
                raise HTTPException(status_code=409, detail=f"{source} import already running (run {current[0]})")
            return {"run_id": current[0], "status": "already_running"}
        if current:
            log.warning("%s import run %d exceeded %ds; allowing a new run", source, current[0], IMPORT_MAX_AGE_S)

        _running_imports[source] = (0, time.monotonic())  # reserve the slot; fill in run_id below

    try:  # the insert can wait on the write lock — don't hold _running_lock meanwhile
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO import_runs(source, started_at, status) VALUES (?,?,?)",
                (source, utc_now(), "running"),
            )
            run_id = cur.lastrowid
    except Exception:
        with _running_lock:
            _running_imports.pop(source, None)
        raise
    with _running_lock:
        _running_imports[source] = (run_id, _running_imports[source][1])

    background_tasks.add_task(
        _run_import_bg, source, run_id, import_fn,
        {"days": days, "start_date": start_date, "end_date": end_date},
    )
    return {"run_id": run_id, "status": "started"}


def _run_import_bg(source: str, run_id: int, import_fn, import_kwargs: dict):
    """Run an import synchronously in a background thread and update import_runs."""
    try:
        _run_import_bg_inner(source, run_id, import_fn, import_kwargs)
    finally:
        with _running_lock:
            if _running_imports.get(source, (None,))[0] == run_id:
                del _running_imports[source]


def _run_import_bg_inner(source: str, run_id: int, import_fn, import_kwargs: dict):
    try:
        today = date.today().isoformat()
        t_start = time.monotonic()

        with db() as conn:
            result = import_fn(conn, **import_kwargs)
        t_imported = time.monotonic()

        imported_dates = set(result.get("dates") or [])

        if not result.get("skipped"):
            from app.normalize import run_normalization
            with _normalization_lock, db() as conn:
                run_normalization(conn, imported_dates or None)
            log.info(
                "%s import run_id=%d timing: fetch+parse %.1fs, normalization %.1fs (%d dates)",
                source, run_id, t_imported - t_start, time.monotonic() - t_imported,
                len(imported_dates) or -1,
            )

        status = "skipped" if result.get("skipped") else "ok"
        rows = result.get("rows_upserted", 0)

        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, rows_upserted=? WHERE id=?",
                (utc_now(), status, rows, run_id),
            )
        log.info("%s import run_id=%d finished: %s (%d rows)", source, run_id, status, rows)

        # Calibration is deliberately suggestion-only. The database claim inside
        # maybe_run_energy_calibration makes this safe when both importers finish
        # together, while keeping the scheduler itself simple (hourly imports).
        try:
            from app.calibration import maybe_run_energy_calibration
            calibration = maybe_run_energy_calibration()
            if calibration.get("status") != "not_due":
                log.info("energy calibration after %s import: %s", source, calibration["status"])
        except Exception:
            log.exception("energy calibration check after %s import failed", source)

        # Fire morning webhook once sleep_score has landed for today AND it's
        # actually morning locally (see _is_morning_locally — a brief nighttime
        # wake can make Garmin finalize sleep_score hours before real wake-up).
        # Claim the date via INSERT OR IGNORE (date is PRIMARY KEY) *before*
        # firing — a plain SELECT-then-fire check races when garmin and
        # google_health imports run in separate background threads close
        # together, letting both pass the check and double-fire the webhook.
        # Only the thread whose INSERT actually wins fires it; on webhook
        # failure the claim is released so a later run retries (Fly auto-stop
        # race condition — machine killed before the webhook could fire).
        if today in imported_dates:
            with db() as conn:
                sleep_now = conn.execute(
                    "SELECT sleep_score FROM daily_metrics WHERE date=?", (today,)
                ).fetchone()
                is_morning = _is_morning_locally(conn, today)
            if sleep_now and sleep_now[0] is not None and is_morning:
                with db() as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO morning_webhooks(date, sent_at) VALUES (?,?)",
                        (today, utc_now()),
                    )
                    claimed = cur.rowcount == 1
                if claimed and not _fire_morning_webhook():
                    with db() as conn:
                        conn.execute("DELETE FROM morning_webhooks WHERE date=?", (today,))

    except Exception as exc:
        log.exception("%s import run_id=%d failed", source, run_id)
        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, error=? WHERE id=?",
                (utc_now(), "error", str(exc), run_id),
            )


@router.post("/import/garmin", dependencies=[Depends(_import_auth)])
def import_garmin(
    background_tasks: BackgroundTasks,
    days: int = Query(default=7, ge=1, le=3650),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    from app.importers.garmin import run_import
    return _start_import("garmin", background_tasks, run_import, days, start_date, end_date)



@router.get("/import/status/{run_id}", dependencies=[Depends(_import_auth)])
def import_status(run_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT source, started_at, finished_at, status, rows_upserted, error "
            "FROM import_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "run_id": run_id,
        "source": row[0],
        "started_at": row[1],
        "finished_at": row[2],
        "status": row[3],
        "rows_upserted": row[4],
        "error": row[5],
    }



@router.post("/google_health/init_tokens", dependencies=[Depends(_import_auth)])
def google_health_init_tokens(body: dict):
    """Store initial Google Health OAuth tokens from google_health_get_tokens.py output."""
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    expires_at = body.get("expires_at")
    if not (access_token and refresh_token and expires_at):
        raise HTTPException(status_code=422, detail="access_token, refresh_token, expires_at required")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO google_health_oauth(id, access_token, refresh_token, expires_at, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at,
                updated_at=excluded.updated_at
            """,
            (access_token, refresh_token, expires_at, utc_now()),
        )
    return {"stored": True}


@router.post("/import/google_health", dependencies=[Depends(_import_auth)])
def import_google_health(
    background_tasks: BackgroundTasks,
    days: int = Query(default=7, ge=1, le=3650),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    from app.importers.google_health import run_import
    return _start_import("google_health", background_tasks, run_import, days, start_date, end_date)


@router.get("/calibration/energy", dependencies=[Depends(_import_auth)])
def energy_calibration_status():
    """Return the latest suggestion; this endpoint never changes the live model."""
    from app.calibration import latest_energy_calibration
    return latest_energy_calibration() or {"status": "not_run"}


@router.get("/export/db", dependencies=[Depends(_export_auth)])
def export_db():
    import sqlite3
    import tempfile
    from pathlib import Path
    from starlette.background import BackgroundTask

    from app.db import DB_PATH

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(f"VACUUM INTO '{tmp_path}'")
        finally:
            conn.close()

        # Credentials never leave the server: scrub them from the snapshot and
        # VACUUM again so the deleted rows aren't recoverable from free pages.
        snap = sqlite3.connect(str(tmp_path), isolation_level=None)
        try:
            snap.execute("PRAGMA secure_delete=ON")
            for table in sorted(SECRET_TABLES):
                snap.execute(f"DELETE FROM {table}")
            snap.execute("VACUUM")
        finally:
            snap.close()
    except Exception:
        # an unscrubbed (or partial) snapshot must never be left in /tmp
        tmp_path.unlink(missing_ok=True)
        raise

    def cleanup():
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    return FileResponse(
        str(tmp_path),
        media_type="application/octet-stream",
        filename="health.db",
        background=BackgroundTask(cleanup),
    )
