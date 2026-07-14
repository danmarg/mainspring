import logging
import os
from datetime import datetime, timezone

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
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


@router.post("/import/garmin", dependencies=[Depends(_import_auth)])
async def import_garmin(
    days: int = Query(default=7, ge=1, le=3650),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    from app.importers.garmin import run_import

    run_id: int | None = None
    started_at = utc_now()

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO import_runs(source, started_at, status) VALUES (?,?,?)",
            ("garmin", started_at, "running"),
        )
        run_id = cur.lastrowid

    try:
        with db() as conn:
            result = run_import(conn, days=days, start_date=start_date, end_date=end_date)

        if not result.get("skipped"):
            from app.normalize import run_normalization
            with db() as conn:
                norm = run_normalization(conn)
            result["normalization"] = norm

        status = "skipped" if result.get("skipped") else "ok"
        rows = result.get("rows_upserted", 0)

        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, rows_upserted=? WHERE id=?",
                (utc_now(), status, rows, run_id),
            )

        return {"run_id": run_id, **result}

    except Exception as exc:
        log.exception("garmin import failed")
        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, error=? WHERE id=?",
                (utc_now(), "error", str(exc), run_id),
            )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/fitbit/init_tokens", dependencies=[Depends(_import_auth)])
async def fitbit_init_tokens(body: dict):
    """Store initial Fitbit OAuth tokens from fitbit_get_tokens.py output."""
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    expires_at = body.get("expires_at")
    if not (access_token and refresh_token and expires_at):
        raise HTTPException(status_code=422, detail="access_token, refresh_token, expires_at required")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO fitbit_oauth(id, access_token, refresh_token, expires_at, updated_at)
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


@router.post("/import/fitbit", dependencies=[Depends(_import_auth)])
async def import_fitbit(
    days: int = Query(default=7, ge=1, le=3650),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    from app.importers.fitbit import run_import

    run_id: int | None = None
    started_at = utc_now()

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO import_runs(source, started_at, status) VALUES (?,?,?)",
            ("fitbit", started_at, "running"),
        )
        run_id = cur.lastrowid

    try:
        with db() as conn:
            result = run_import(conn, days=days, start_date=start_date, end_date=end_date)

        if not result.get("skipped"):
            from app.normalize import run_normalization
            with db() as conn:
                norm = run_normalization(conn)
            result["normalization"] = norm

        status = "skipped" if result.get("skipped") else "ok"
        rows = result.get("rows_upserted", 0)

        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, rows_upserted=? WHERE id=?",
                (utc_now(), status, rows, run_id),
            )

        return {"run_id": run_id, **result}

    except Exception as exc:
        log.exception("fitbit import failed")
        with db() as conn:
            conn.execute(
                "UPDATE import_runs SET finished_at=?, status=?, error=? WHERE id=?",
                (utc_now(), "error", str(exc), run_id),
            )
        raise HTTPException(status_code=500, detail=str(exc))


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
