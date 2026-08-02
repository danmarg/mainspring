import logging
import os
import urllib.request
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import HOME_TZ, db, utc_now

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

_bearer = HTTPBearer()


def _require_token(env_var: str):
    def dependency(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
        expected = (os.getenv(env_var) or "").strip()
        if not expected:
            raise HTTPException(status_code=503, detail=f"{env_var} not configured")
        if creds.credentials != expected:
            raise HTTPException(status_code=401, detail="invalid token")
    return dependency


_import_auth = _require_token("ADMIN_TOKEN")
_export_auth = _require_token("EXPORT_TOKEN")

MORNING_WEBHOOK_EARLIEST_HOUR = int(os.getenv("MORNING_WEBHOOK_EARLIEST_HOUR", "5"))


def _is_morning_locally(conn, today: str) -> bool:
    """True once it's past MORNING_WEBHOOK_EARLIEST_HOUR in today's local (health-day) tz.

    Garmin finalizes a sleep_score as soon as it detects any wake — including a
    brief middle-of-the-night wake-up — so "sleep_score landed" alone fires too
    early. Gate on local wall-clock time instead.
    """
    row = conn.execute("SELECT tz FROM day_timezone WHERE date=?", (today,)).fetchone()
    tz_name = row[0] if row else HOME_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(HOME_TZ)
    local_now = datetime.now(timezone.utc).astimezone(tz)
    return local_now.hour >= MORNING_WEBHOOK_EARLIEST_HOUR


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


def _run_import_bg(source: str, run_id: int, import_fn, import_kwargs: dict):
    """Run an import synchronously in a background thread and update import_runs."""
    try:
        today = date.today().isoformat()

        with db() as conn:
            result = import_fn(conn, **import_kwargs)

        imported_dates = set(result.get("dates") or [])

        if not result.get("skipped"):
            from app.normalize import run_normalization
            with db() as conn:
                run_normalization(conn, imported_dates or None)

        status = "skipped" if result.get("skipped") else "ok"
        rows = result.get("rows_upserted", 0)

        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, rows_upserted=? WHERE id=?",
                (utc_now(), status, rows, run_id),
            )
        log.info("%s import run_id=%d finished: %s (%d rows)", source, run_id, status, rows)

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
async def import_garmin(
    background_tasks: BackgroundTasks,
    days: int = Query(default=7, ge=1, le=3650),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    from app.importers.garmin import run_import

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO import_runs(source, started_at, status) VALUES (?,?,?)",
            ("garmin", utc_now(), "running"),
        )
        run_id = cur.lastrowid

    background_tasks.add_task(
        _run_import_bg, "garmin", run_id, run_import,
        {"days": days, "start_date": start_date, "end_date": end_date},
    )
    return {"run_id": run_id, "status": "started"}



@router.get("/import/status/{run_id}", dependencies=[Depends(_import_auth)])
async def import_status(run_id: int):
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
async def google_health_init_tokens(body: dict):
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
async def import_google_health(
    background_tasks: BackgroundTasks,
    days: int = Query(default=7, ge=1, le=3650),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    from app.importers.google_health import run_import

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO import_runs(source, started_at, status) VALUES (?,?,?)",
            ("google_health", utc_now(), "running"),
        )
        run_id = cur.lastrowid

    background_tasks.add_task(
        _run_import_bg, "google_health", run_id, run_import,
        {"days": days, "start_date": start_date, "end_date": end_date},
    )
    return {"run_id": run_id, "status": "started"}


@router.get("/export/db", dependencies=[Depends(_export_auth)])
async def export_db():
    import sqlite3
    import tempfile
    from pathlib import Path
    from starlette.background import BackgroundTask

    from app.db import DB_PATH

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(f"VACUUM INTO '{tmp_path}'")
    finally:
        conn.close()

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
