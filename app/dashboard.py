"""
Analytics dashboard — /dashboard/*

Auth: same DATASETTE_TOKEN as Datasette, via cookie (ms_dash_auth) or Bearer header.
Charts: Altair 5/6 → Vega-Lite JSON → rendered client-side via vega-embed CDN.
Data: SQLite queries with window functions; no pandas.
"""

import hashlib
import json
import logging
import os
from datetime import date, timedelta

import altair as alt
from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard")

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates", "dashboard")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# ── auth ─────────────────────────────────────────────────────────────────────

COOKIE_NAME = "ms_dash_auth"
COOKIE_TTL = 86400  # 24h


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_authed(request: Request, ms_dash_auth: str | None) -> bool:
    token = (os.getenv("DATASETTE_TOKEN") or "").strip()
    if not token:
        return False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == token:
        return True
    return ms_dash_auth == _token_hash(token)


def _auth_redirect():
    return RedirectResponse("/dashboard/login", status_code=302)


# ── helpers ───────────────────────────────────────────────────────────────────

def _days_clause(days: str) -> str:
    """Return SQL date lower-bound string for WHERE date >= date('now', ?)."""
    mapping = {"30": "-30 days", "90": "-90 days", "180": "-180 days", "all": "-9999 days"}
    return mapping.get(str(days), "-90 days")


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _scalar(conn, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ── chart builders ────────────────────────────────────────────────────────────

def _sparkline(rows: list[dict], field: str, color: str = "#4e9af1") -> str:
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_line(color=color, strokeWidth=1.5)
        .encode(
            x=alt.X("date:T", axis=None),
            y=alt.Y(f"{field}:Q", axis=None, scale=alt.Scale(zero=False)),
        )
        .properties(width="container", height=60)
        .configure_view(strokeWidth=0)
    )
    return chart.to_json()


def _trend_chart(rows: list[dict], field: str, avg_field: str, title: str,
                 color: str = "#4e9af1", avg_color: str = "#f4a261",
                 height: int = 200) -> str:
    if not rows:
        return "{}"
    base = alt.Chart(alt.Data(values=rows))
    line = base.mark_line(color=color, strokeWidth=1.5).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip(f"{field}:Q", title=title)],
    )
    avg = base.mark_line(color=avg_color, strokeDash=[4, 4], opacity=0.85, strokeWidth=1.5).encode(
        x="date:T",
        y=f"{avg_field}:Q",
        tooltip=[alt.Tooltip(f"{avg_field}:Q", title="7d avg")],
    )
    return (
        alt.layer(line, avg)
        .properties(width="container", height=height)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


def _sleep_chart(rows_score: list[dict], rows_stages: list[dict]) -> str:
    if not rows_score:
        return "{}"
    score_chart = (
        alt.Chart(alt.Data(values=rows_score))
        .mark_line(color="#7ec8e3", strokeWidth=1.5)
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("sleep_score:Q", title="Sleep score", scale=alt.Scale(domain=[0, 100])),
            tooltip=["date:T", "sleep_score:Q"],
        )
        .properties(width="container", height=140)
    )
    if rows_stages:
        stages_chart = (
            alt.Chart(alt.Data(values=rows_stages))
            .mark_area()
            .encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("minutes:Q", title="Stage (min)", stack="zero"),
                color=alt.Color("stage:N", scale=alt.Scale(
                    domain=["Deep", "REM", "Light"],
                    range=["#1a6b9e", "#7b4fa6", "#2d8a6d"],
                )),
                tooltip=["date:T", "stage:N", "minutes:Q"],
            )
            .properties(width="container", height=120)
        )
        combined = alt.vconcat(score_chart, stages_chart, spacing=8)
    else:
        combined = score_chart
    return (
        combined
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


def _load_chart(rows: list[dict]) -> str:
    if not rows:
        return "{}"
    base = alt.Chart(alt.Data(values=rows))
    acute = base.mark_line(color="#e05c5c", strokeWidth=1.5).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("acute_training_load:Q", title="Load", scale=alt.Scale(zero=False)),
        tooltip=["date:T", "acute_training_load:Q"],
    )
    chronic = base.mark_line(color="#4e9af1", strokeWidth=1.5, strokeDash=[3, 3]).encode(
        x="date:T",
        y="chronic_training_load:Q",
        tooltip=["date:T", "chronic_training_load:Q"],
    )
    lines = alt.layer(acute, chronic).properties(width="container", height=160)

    # ratio bands
    ratio_rows = [r for r in rows if r.get("training_load_ratio") is not None]
    if ratio_rows:
        ratio_base = alt.Chart(alt.Data(values=ratio_rows))
        ratio_line = ratio_base.mark_line(color="#a8d8a8", strokeWidth=1.5).encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("training_load_ratio:Q", title="ATL/CTL ratio", scale=alt.Scale(domain=[0, 2])),
            tooltip=["date:T", "training_load_ratio:Q"],
        )
        zone_data = [
            {"y1": 0, "y2": 0.8, "zone": "Detraining"},
            {"y1": 0.8, "y2": 1.0, "zone": "Maintenance"},
            {"y1": 1.0, "y2": 1.3, "zone": "Productive"},
            {"y1": 1.3, "y2": 1.5, "zone": "Overreaching"},
            {"y1": 1.5, "y2": 2.5, "zone": "High risk"},
        ]
        bands = (
            alt.Chart(alt.Data(values=zone_data))
            .mark_rect(opacity=0.12)
            .encode(
                y=alt.Y("y1:Q", scale=alt.Scale(domain=[0, 2])),
                y2="y2:Q",
                color=alt.Color("zone:N", scale=alt.Scale(
                    domain=["Detraining", "Maintenance", "Productive", "Overreaching", "High risk"],
                    range=["#888888", "#4e9af1", "#5cb85c", "#f0ad4e", "#e05c5c"],
                ), legend=None),
            )
        )
        ratio_chart = alt.layer(bands, ratio_line).properties(width="container", height=120)
        combined = alt.vconcat(lines, ratio_chart, spacing=8)
    else:
        combined = lines

    return (
        combined
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


def _body_battery_chart(rows: list[dict]) -> str:
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_area(opacity=0.6, color="#f4a261")
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("body_battery_low:Q", title="Body battery", scale=alt.Scale(domain=[0, 100])),
            y2="body_battery_high:Q",
            tooltip=["date:T", "body_battery_high:Q", "body_battery_low:Q"],
        )
        .properties(width="container", height=160)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
    )
    return chart.to_json()


def _calendar_heatmap(rows: list[dict], field: str, title: str,
                      scheme: str = "reds", zero_color: str = "#1a1a1a") -> str:
    if not rows:
        return "{}"
    # Filter to non-null for color, include zeros as a separate condition
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_rect(cornerRadius=2)
        .encode(
            x=alt.X("week:O", title="Week", axis=alt.Axis(labels=False, ticks=False)),
            y=alt.Y("dow:O", title=None,
                    sort=["1", "2", "3", "4", "5", "6", "0"],
                    axis=alt.Axis(
                        labelExpr="{'0':'Sun','1':'Mon','2':'Tue','3':'Wed','4':'Thu','5':'Fri','6':'Sat'}[datum.label]"
                    )),
            color=alt.condition(
                alt.datum[field] == 0,
                alt.value(zero_color),
                alt.Color(f"{field}:Q", scale=alt.Scale(scheme=scheme), title=title),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="Date"),
                alt.Tooltip(f"{field}:Q", title=title),
            ],
        )
        .properties(width="container", height=140, title=title)
        .configure_axis(labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#111")
        .configure_title(color="#ccc")
    )
    return chart.to_json()


def _hrv_delta_heatmap(rows: list[dict]) -> str:
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_rect(cornerRadius=2)
        .encode(
            x=alt.X("week:O", title="Week", axis=alt.Axis(labels=False, ticks=False)),
            y=alt.Y("dow:O", title=None,
                    sort=["1", "2", "3", "4", "5", "6", "0"],
                    axis=alt.Axis(
                        labelExpr="{'0':'Sun','1':'Mon','2':'Tue','3':'Wed','4':'Thu','5':'Fri','6':'Sat'}[datum.label]"
                    )),
            color=alt.Color(
                "hrv_delta:Q",
                scale=alt.Scale(scheme="redblue", domainMid=0),
                title="HRV delta (ms)",
            ),
            tooltip=[
                alt.Tooltip("date:T", title="Date"),
                alt.Tooltip("hrv:Q", title="HRV"),
                alt.Tooltip("hrv_delta:Q", title="Delta from 7d avg"),
            ],
        )
        .properties(width="container", height=140, title="HRV deviation from 7-day avg")
        .configure_axis(labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#111")
        .configure_title(color="#ccc")
    )
    return chart.to_json()


def _activity_bar(rows: list[dict], field: str, y_title: str) -> str:
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar()
        .encode(
            x=alt.X("type:N", title="Activity type", sort="-y"),
            y=alt.Y(f"{field}:Q", title=y_title),
            color=alt.Color("type:N", legend=None),
            tooltip=["type:N", alt.Tooltip(f"{field}:Q", title=y_title)],
        )
        .properties(width="container", height=200)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
    )
    return chart.to_json()


def _weekly_volume_chart(rows: list[dict]) -> str:
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar()
        .encode(
            x=alt.X("week:O", title="Week", axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("duration_min:Q", title="Duration (min)", stack="zero"),
            color=alt.Color("type:N", title="Type"),
            tooltip=["week:O", "type:N", alt.Tooltip("duration_min:Q", title="Min")],
        )
        .properties(width="container", height=220)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
    )
    return chart.to_json()


def _macro_dow_chart(rows: list[dict]) -> str:
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar()
        .encode(
            x=alt.X("dow_name:N", title=None,
                    sort=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
            y=alt.Y("avg_g:Q", title="Avg grams", stack="zero"),
            color=alt.Color("macro:N", scale=alt.Scale(
                domain=["protein_g", "carbs_g", "fat_g"],
                range=["#e05c5c", "#f4a261", "#4e9af1"],
            ), title="Macro"),
            tooltip=["dow_name:N", "macro:N", alt.Tooltip("avg_g:Q", title="Avg g", format=".1f")],
        )
        .properties(width="container", height=220, title="Average macros by day of week")
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .configure_title(color="#ccc")
    )
    return chart.to_json()


# ── login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, token: str = Form(...)):
    expected = (os.getenv("DATASETTE_TOKEN") or "").strip()
    if not expected:
        return templates.TemplateResponse("login.html", {"request": request, "error": "DATASETTE_TOKEN not configured"})
    if token.strip() != expected:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid token"})
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        _token_hash(expected),
        max_age=COOKIE_TTL,
        httponly=True,
        samesite="lax",
    )
    return resp


# ── overview ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def overview(request: Request, ms_dash_auth: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    with db() as conn:
        today_row = conn.execute(
            """SELECT hrv, sleep_score, training_readiness, body_battery_high,
                      resting_hr, weight_kg, stress_avg, sleep_duration_min
               FROM daily_metrics WHERE date=?""",
            (today,),
        ).fetchone()

        avg_rows = _rows(conn,
            """SELECT AVG(hrv) as hrv, AVG(sleep_score) as sleep_score,
                      AVG(training_readiness) as training_readiness,
                      AVG(body_battery_high) as body_battery_high,
                      AVG(resting_hr) as resting_hr
               FROM daily_metrics
               WHERE date >= date('now','-7 days') AND date < date('now')""")
        avgs = avg_rows[0] if avg_rows else {}

        yesterday_row = conn.execute(
            "SELECT alcohol_units, caffeine_mg, calories_estimated FROM daily_metrics WHERE date=?",
            (yesterday,),
        ).fetchone()

        # Sparklines: last 14 days
        spark_rows = _rows(conn,
            """SELECT date, hrv, sleep_score, resting_hr FROM daily_metrics
               WHERE date >= date('now','-14 days') ORDER BY date""")

    def _delta(key: int, avg_key: str):
        if today_row and today_row[key] is not None and avgs.get(avg_key):
            d = today_row[key] - avgs[avg_key]
            return f"{'+' if d >= 0 else ''}{d:.1f}"
        return None

    cards = []
    if today_row:
        tr = today_row
        def card(label, val, avg_key, fmt=".0f", unit=""):
            raw = val
            if raw is None:
                return {"label": label, "value": "—", "delta": None, "unit": unit}
            avg = avgs.get(avg_key)
            delta = None
            if avg:
                d = raw - avg
                delta = {"text": f"{'+' if d >= 0 else ''}{d:{fmt}}", "pos": d >= 0}
            return {"label": label, "value": f"{raw:{fmt}}", "delta": delta, "unit": unit}

        cards = [
            card("HRV", tr[0], "hrv", ".0f", "ms"),
            card("Sleep score", tr[1], "sleep_score", ".0f", ""),
            card("Readiness", tr[2], "training_readiness", ".0f", ""),
            card("Body battery", tr[3], "body_battery_high", ".0f", ""),
            card("Resting HR", tr[4], "resting_hr", ".0f", "bpm"),
            {"label": "Weight", "value": f"{tr[5]:.1f}" if tr[5] else "—", "delta": None, "unit": "kg"},
        ]

    # Build sparkline specs
    hrv_spark = _sparkline([r for r in spark_rows if r.get("hrv")], "hrv", "#4e9af1")
    sleep_spark = _sparkline([r for r in spark_rows if r.get("sleep_score")], "sleep_score", "#7ec8e3")
    hr_spark = _sparkline([r for r in spark_rows if r.get("resting_hr")], "resting_hr", "#e05c5c")

    yesterday_behavior = None
    if yesterday_row:
        yesterday_behavior = {
            "alcohol_units": yesterday_row[0],
            "caffeine_mg": yesterday_row[1],
            "calories_estimated": yesterday_row[2],
        }

    return templates.TemplateResponse("overview.html", {
        "request": request,
        "today": today,
        "cards": cards,
        "hrv_spark": hrv_spark,
        "sleep_spark": sleep_spark,
        "hr_spark": hr_spark,
        "yesterday_behavior": yesterday_behavior,
    })


# ── trends ────────────────────────────────────────────────────────────────────

@router.get("/trends", response_class=HTMLResponse)
async def trends(request: Request, days: str = "90",
                 ms_dash_auth: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    clause = _days_clause(days)

    with db() as conn:
        hrv_rows = _rows(conn, """
            SELECT date, hrv,
              AVG(hrv) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS hrv_7d_avg
            FROM daily_metrics
            WHERE date >= date('now', ?) AND hrv IS NOT NULL
            ORDER BY date
        """, (clause,))

        score_rows = _rows(conn, """
            SELECT date, sleep_score FROM daily_metrics
            WHERE date >= date('now', ?) AND sleep_score IS NOT NULL ORDER BY date
        """, (clause,))

        stage_rows_raw = _rows(conn, """
            SELECT date, sleep_deep_min, sleep_rem_min, sleep_light_min FROM daily_metrics
            WHERE date >= date('now', ?)
              AND (sleep_deep_min IS NOT NULL OR sleep_rem_min IS NOT NULL OR sleep_light_min IS NOT NULL)
            ORDER BY date
        """, (clause,))
        # Unpivot to long form for Altair stacked area
        stage_rows = []
        for r in stage_rows_raw:
            for stage, col in [("Deep", "sleep_deep_min"), ("REM", "sleep_rem_min"), ("Light", "sleep_light_min")]:
                if r.get(col) is not None:
                    stage_rows.append({"date": r["date"], "stage": stage, "minutes": r[col]})

        load_rows = _rows(conn, """
            SELECT date, acute_training_load, chronic_training_load, training_load_ratio
            FROM daily_metrics
            WHERE date >= date('now', ?)
              AND acute_training_load IS NOT NULL
            ORDER BY date
        """, (clause,))

        battery_rows = _rows(conn, """
            SELECT date, body_battery_high, body_battery_low FROM daily_metrics
            WHERE date >= date('now', ?)
              AND body_battery_high IS NOT NULL AND body_battery_low IS NOT NULL
            ORDER BY date
        """, (clause,))

        rhr_rows = _rows(conn, """
            SELECT date, resting_hr,
              AVG(resting_hr) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rhr_7d_avg
            FROM daily_metrics
            WHERE date >= date('now', ?) AND resting_hr IS NOT NULL
            ORDER BY date
        """, (clause,))

    return templates.TemplateResponse("trends.html", {
        "request": request,
        "days": days,
        "hrv_spec": _trend_chart(hrv_rows, "hrv", "hrv_7d_avg", "HRV (ms)"),
        "sleep_spec": _sleep_chart(score_rows, stage_rows),
        "load_spec": _load_chart(load_rows),
        "battery_spec": _body_battery_chart(battery_rows),
        "rhr_spec": _trend_chart(rhr_rows, "resting_hr", "rhr_7d_avg", "Resting HR (bpm)", color="#e05c5c"),
    })


# ── behavior ──────────────────────────────────────────────────────────────────

@router.get("/behavior", response_class=HTMLResponse)
async def behavior(request: Request, days: str = "90",
                   ms_dash_auth: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    clause = _days_clause(days)

    with db() as conn:
        rows = _rows(conn, """
            SELECT date,
              strftime('%Y-%W', date) AS week,
              strftime('%w', date) AS dow,
              COALESCE(alcohol_units, 0) AS alcohol_units,
              COALESCE(caffeine_mg, 0) AS caffeine_mg,
              hrv,
              AVG(hrv) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS hrv_7d_avg
            FROM daily_metrics
            WHERE date >= date('now', ?)
            ORDER BY date
        """, (clause,))

    # Compute hrv_delta in Python
    for r in rows:
        if r.get("hrv") is not None and r.get("hrv_7d_avg") is not None:
            r["hrv_delta"] = round(r["hrv"] - r["hrv_7d_avg"], 1)
        else:
            r["hrv_delta"] = None

    alcohol_rows = [r for r in rows if r.get("alcohol_units") is not None]
    caffeine_rows = [r for r in rows if r.get("caffeine_mg") is not None]
    hrv_delta_rows = [r for r in rows if r.get("hrv_delta") is not None]

    return templates.TemplateResponse("behavior.html", {
        "request": request,
        "days": days,
        "alcohol_spec": _calendar_heatmap(alcohol_rows, "alcohol_units", "Alcohol (units)", scheme="reds"),
        "caffeine_spec": _calendar_heatmap(caffeine_rows, "caffeine_mg", "Caffeine (mg)", scheme="purples"),
        "hrv_delta_spec": _hrv_delta_heatmap(hrv_delta_rows),
    })


# ── nutrition ─────────────────────────────────────────────────────────────────

def _parse_macros(macros_list_str: str | None) -> dict[str, float]:
    if not macros_list_str:
        return {}
    totals: dict[str, float] = {}
    for chunk in macros_list_str.split("\x1f"):  # group_concat separator
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
            for key in ("protein_g", "carbs_g", "fat_g"):
                v = data.get(key)
                if v is not None:
                    try:
                        totals[key] = totals.get(key, 0.0) + float(v)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
    return totals


DOW_NAMES = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat"}


@router.get("/nutrition", response_class=HTMLResponse)
async def nutrition(request: Request, days: str = "90",
                    ms_dash_auth: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    clause = _days_clause(days)

    with db() as conn:
        # Pull calories from daily_metrics for heatmap
        cal_rows = _rows(conn, """
            SELECT date,
              strftime('%Y-%W', date) AS week,
              strftime('%w', date) AS dow,
              COALESCE(calories_estimated, 0) AS calories
            FROM daily_metrics
            WHERE date >= date('now', ?)
            ORDER BY date
        """, (clause,))

        # Pull individual meal logs for macro parsing
        meal_rows = _rows(conn, """
            SELECT DATE(ts) AS date,
              strftime('%w', ts) AS dow,
              SUM(estimated_calories) AS calories,
              GROUP_CONCAT(estimated_macros_json, char(31)) AS macros_list
            FROM manual_logs
            WHERE type='meal' AND DATE(ts) >= date('now', ?)
            GROUP BY DATE(ts)
            ORDER BY date
        """, (clause,))

        # Pull protein target from training_goals if set
        protein_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='protein_g_daily'"
        ).fetchone()
        protein_target = float(protein_target_row[0]) if protein_target_row else 150.0

    # Build per-day macro totals
    macro_by_date: dict[str, dict] = {}
    for mr in meal_rows:
        macros = _parse_macros(mr.get("macros_list"))
        macro_by_date[mr["date"]] = {
            "date": mr["date"],
            "dow": mr["dow"],
            "calories": mr.get("calories") or 0,
            **macros,
        }

    # Add calendar fields and protein compliance to cal_rows
    for r in cal_rows:
        m = macro_by_date.get(r["date"], {})
        protein = m.get("protein_g")
        if protein is None:
            r["protein_pct"] = None
        else:
            r["protein_pct"] = min(round(protein / protein_target * 100), 150)

    # Protein compliance heatmap — only days with macro data
    protein_rows = [r for r in cal_rows if r.get("protein_pct") is not None]

    # Day-of-week macro breakdown (long form)
    dow_macro_rows = []
    dow_accum: dict[str, dict[str, list]] = {}
    for md in macro_by_date.values():
        dow = md.get("dow", "0")
        if dow not in dow_accum:
            dow_accum[dow] = {"protein_g": [], "carbs_g": [], "fat_g": []}
        for macro in ("protein_g", "carbs_g", "fat_g"):
            v = md.get(macro)
            if v is not None:
                dow_accum[dow][macro].append(v)
    for dow, macros in sorted(dow_accum.items()):
        dow_name = DOW_NAMES.get(dow, dow)
        for macro, vals in macros.items():
            if vals:
                avg_g = sum(vals) / len(vals)
                dow_macro_rows.append({"dow_name": dow_name, "macro": macro, "avg_g": round(avg_g, 1)})

    return templates.TemplateResponse("nutrition.html", {
        "request": request,
        "days": days,
        "protein_target": protein_target,
        "calories_spec": _calendar_heatmap(cal_rows, "calories", "Calories", scheme="oranges"),
        "protein_spec": _calendar_heatmap(protein_rows, "protein_pct", "Protein % of target", scheme="greens"),
        "macro_dow_spec": _macro_dow_chart(dow_macro_rows),
    })


# ── activities ────────────────────────────────────────────────────────────────

@router.get("/activities", response_class=HTMLResponse)
async def activities(request: Request, days: str = "60",
                     ms_dash_auth: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    clause = _days_clause(days)

    with db() as conn:
        table_rows = _rows(conn, """
            SELECT date, type,
              ROUND(duration_s / 60.0, 0) AS duration_min,
              ROUND(distance_m / 1000.0, 2) AS distance_km,
              avg_hr, calories
            FROM activities
            WHERE date >= date('now', ?)
            ORDER BY date DESC, start_time DESC
        """, (clause,))

        count_rows = _rows(conn, """
            SELECT type, COUNT(*) AS count FROM activities
            WHERE date >= date('now', ?) GROUP BY type ORDER BY count DESC
        """, (clause,))

        weekly_rows = _rows(conn, """
            SELECT strftime('%Y-%W', date) AS week, type,
              ROUND(SUM(duration_s) / 60.0, 0) AS duration_min
            FROM activities
            WHERE date >= date('now', ?)
            GROUP BY week, type ORDER BY week, type
        """, (clause,))

    return templates.TemplateResponse("activities.html", {
        "request": request,
        "days": days,
        "table_rows": table_rows,
        "count_spec": _activity_bar(count_rows, "count", "Activities"),
        "volume_spec": _weekly_volume_chart(weekly_rows),
    })
