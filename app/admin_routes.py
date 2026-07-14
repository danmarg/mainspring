import logging
import os
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import db, utc_now

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

_bearer = HTTPBearer()


def _require_token(env_var: str):
    def dependency(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
        expected = os.getenv(env_var)
        if not expected:
            raise HTTPException(status_code=503, detail=f"{env_var} not configured")
        if creds.credentials != expected:
            raise HTTPException(status_code=401, detail="invalid token")
    return dependency


_import_auth = _require_token("ADMIN_TOKEN")
_export_auth = _require_token("EXPORT_TOKEN")


def _run_import_bg(source: str, run_id: int, import_fn, import_kwargs: dict):
    """Run an import synchronously in a background thread and update import_runs."""
    try:
        with db() as conn:
            result = import_fn(conn, **import_kwargs)

        if not result.get("skipped"):
            from app.normalize import run_normalization
            with db() as conn:
                run_normalization(conn)

        status = "skipped" if result.get("skipped") else "ok"
        rows = result.get("rows_upserted", 0)

        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, rows_upserted=? WHERE id=?",
                (utc_now(), status, rows, run_id),
            )
        log.info("%s import run_id=%d finished: %s (%d rows)", source, run_id, status, rows)

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
