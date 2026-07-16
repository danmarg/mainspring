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
from datetime import date, datetime, timedelta

import altair as alt
from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import db
from app.readiness import alertness_curve, readiness_from_db

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
    mapping = {
        "7": "-7 days", "30": "-30 days", "90": "-90 days",
        "180": "-180 days", "360": "-360 days", "all": "-9999 days",
    }
    return mapping.get(str(days), "-30 days")


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
                ), legend=alt.Legend(orient="bottom", labelColor="#aaa", titleColor="#aaa")),
                tooltip=["date:T", "stage:N", "minutes:Q"],
            )
            .properties(width="container", height=120)
        )
        combined = (
            alt.vconcat(score_chart, stages_chart, spacing=8)
            .resolve_scale(x="shared")
        )
    else:
        combined = score_chart
    return (
        combined
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


def _zone_load_chart(rows: list[dict]) -> str:
    """Stacked area: monthly zone load trend (aerobic low/high, anaerobic)."""
    if not rows:
        return "{}"
    # Unpivot to long form
    long = []
    for r in rows:
        for zone, key in [
            ("Aerobic low",  "monthly_load_aerobic_low"),
            ("Aerobic high", "monthly_load_aerobic_high"),
            ("Anaerobic",    "monthly_load_anaerobic"),
        ]:
            if r.get(key) is not None:
                long.append({"date": r["date"], "zone": zone, "load": r[key]})
    if not long:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=long))
        .mark_area(opacity=0.8)
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("load:Q", title="Monthly zone load", stack="zero"),
            color=alt.Color("zone:N", scale=alt.Scale(
                domain=["Aerobic low", "Aerobic high", "Anaerobic"],
                range=["#4e9af1", "#f4a261", "#e05c5c"],
            ), title="Zone"),
            tooltip=["date:T", "zone:N", alt.Tooltip("load:Q", format=".0f")],
        )
        .properties(width="container", height=200)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
    )
    return chart.to_json()


def _hr_chart(rows: list[dict]) -> str:
    """Dual line: resting HR (blue) + daily max HR from activities (red) on one chart."""
    if not rows:
        return "{}"
    rhr_rows = [r for r in rows if r.get("resting_hr") is not None]
    max_rows = [r for r in rows if r.get("max_hr") is not None]
    if not rhr_rows and not max_rows:
        return "{}"
    layers = []
    if rhr_rows:
        base_rhr = alt.Chart(alt.Data(values=rhr_rows))
        layers.append(base_rhr.mark_line(color="#4e9af1", strokeWidth=1.5).encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("resting_hr:Q", title="HR (bpm)", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("resting_hr:Q", title="Resting HR")],
        ))
        layers.append(base_rhr.mark_line(color="#4e9af1", strokeDash=[4, 4], opacity=0.6, strokeWidth=1.2).encode(
            x="date:T", y="rhr_7d_avg:Q",
        ))
    if max_rows:
        base_max = alt.Chart(alt.Data(values=max_rows))
        layers.append(base_max.mark_line(color="#e05c5c", strokeWidth=1.5, opacity=0.7).encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("max_hr:Q", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("max_hr:Q", title="Max HR (activity)")],
        ))
    return (
        alt.layer(*layers)
        .properties(width="container", height=200)
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


def _pmc_chart(rows: list[dict]) -> str:
    """Performance Management Chart: CTL (fitness), ATL (fatigue), TSB (form) bars."""
    if not rows:
        return "{}"
    # Compute TSB = CTL - ATL in Python
    for r in rows:
        if r.get("ctl") is not None and r.get("atl") is not None:
            r["tsb"] = round(r["ctl"] - r["atl"], 1)
        else:
            r["tsb"] = None
    tsb_rows = [r for r in rows if r.get("tsb") is not None]
    ctl_rows = [r for r in rows if r.get("ctl") is not None]
    atl_rows = [r for r in rows if r.get("atl") is not None]
    if not ctl_rows and not atl_rows:
        return "{}"

    layers = []

    # TSB bars (green = fresh/positive, red = tired/negative)
    if tsb_rows:
        tsb_bars = (
            alt.Chart(alt.Data(values=tsb_rows))
            .mark_bar(opacity=0.4)
            .encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("tsb:Q", title="Load / TSB", scale=alt.Scale(zero=True)),
                color=alt.condition(
                    alt.datum.tsb >= 0,
                    alt.value("#5cb85c"),
                    alt.value("#e05c5c"),
                ),
                tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("tsb:Q", title="TSB (form)")],
            )
        )
        layers.append(tsb_bars)

    if ctl_rows:
        layers.append(
            alt.Chart(alt.Data(values=ctl_rows))
            .mark_line(color="#4e9af1", strokeWidth=2)
            .encode(
                x="date:T",
                y=alt.Y("ctl:Q", scale=alt.Scale(zero=True)),
                tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("ctl:Q", title="CTL (fitness)")],
            )
        )
    if atl_rows:
        layers.append(
            alt.Chart(alt.Data(values=atl_rows))
            .mark_line(color="#f4a261", strokeWidth=2)
            .encode(
                x="date:T",
                y=alt.Y("atl:Q", scale=alt.Scale(zero=True)),
                tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("atl:Q", title="ATL (fatigue)")],
            )
        )

    return (
        alt.layer(*layers)
        .resolve_scale(y="shared")
        .properties(width="container", height=220)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


def _energy_chart(wake_hour: float | None, sleep_score: float | None,
                  caffeine_doses: list[tuple[float, float]] | None = None) -> str:
    """
    Energy forecast chart using two-process alertness model (Borbély 1982).
    Anchored to today's wake time from Garmin sleep data.
    The "now" rule is injected client-side so timezone is always accurate.
    """
    if wake_hour is None:
        return "{}"

    FORECAST_HOURS = 18  # slightly past midnight for late sleepers
    curve = alertness_curve(wake_hour, sleep_score or 75.0, FORECAST_HOURS, caffeine_doses=caffeine_doses)

    # Use hours-since-wake on X axis to avoid midnight-wrapping issues
    curve_hw = [
        {**r, "hw": i / 4.0, "label": f"{int(r['hour']):02d}:{int((r['hour'] % 1) * 60):02d}"}
        for i, r in enumerate(curve)
    ]
    peak = max(curve_hw, key=lambda r: r["alertness"])

    # X-axis: tick every 2h from wake, labeled as local clock time
    tick_vals = list(range(0, FORECAST_HOURS + 1, 2))
    tick_labels = {h: f"{int((wake_hour + h) % 24):02d}:00" for h in tick_vals}
    label_expr = "{" + ",".join(f'"{k}":"{v}"' for k, v in tick_labels.items()) + "}[datum.value]"

    base = alt.Chart(alt.Data(values=curve_hw))
    area = base.mark_area(opacity=0.25, color="#4e9af1").encode(
        x=alt.X("hw:Q", title=None, scale=alt.Scale(domain=[0, FORECAST_HOURS]),
                axis=alt.Axis(values=tick_vals, labelExpr=label_expr)),
        y=alt.Y("alertness:Q", title="Alertness", scale=alt.Scale(domain=[0, 100], zero=True)),
    )
    line = base.mark_line(color="#4e9af1", strokeWidth=2).encode(
        x="hw:Q", y="alertness:Q",
        tooltip=[alt.Tooltip("label:N", title="Time"),
                 alt.Tooltip("alertness:Q", title="Alertness", format=".0f")],
    )
    peak_dot = (
        alt.Chart(alt.Data(values=[peak]))
        .mark_point(color="#2ecc71", size=80, shape="triangle-up")
        .encode(x="hw:Q", y="alertness:Q",
                tooltip=[alt.Tooltip("label:N", title="Peak time"),
                         alt.Tooltip("alertness:Q", title="Peak", format=".0f")])
    )
    return (
        alt.layer(area, line, peak_dot)
        .properties(width="container", height=160)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


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


def _daily_bar_chart(rows: list[dict], field: str, title: str,
                     color: str = "#4e9af1", ref_line: float | None = None,
                     x_domain: list[str] | None = None) -> str:
    """Bar chart of a daily value over time, with optional horizontal reference line."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    bars = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar(color=color, opacity=0.8)
        .encode(
            x=alt.X("date:T", title=None, scale=x_scale),
            y=alt.Y(f"{field}:Q", title=title),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip(f"{field}:Q", title=title, format=".0f")],
        )
    )
    layers: list = [bars]
    if ref_line is not None:
        rule = (
            alt.Chart(alt.Data(values=[{"ref": ref_line}]))
            .mark_rule(color="#5cb85c", strokeDash=[4, 4], strokeWidth=1.5)
            .encode(y="ref:Q")
        )
        layers.append(rule)
    return (
        alt.layer(*layers)
        .properties(width="container", height=200)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


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


def _sparse_line_chart(rows: list[dict], field: str, title: str,
                       color: str = "#4e9af1", ref_bands: list | None = None) -> str:
    """Dots + connecting line for infrequently sampled metrics (weight, BP, VO2max)."""
    if not rows:
        return "{}"
    base = alt.Chart(alt.Data(values=rows))
    line = base.mark_line(color=color, opacity=0.4, strokeWidth=1).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=False)),
    )
    dots = base.mark_point(color=color, size=60, opacity=0.9).encode(
        x="date:T",
        y=f"{field}:Q",
        tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip(f"{field}:Q", title=title, format=".1f")],
    )
    layers = [line, dots]
    if ref_bands:
        band_data = [{"y1": b[0], "y2": b[1], "label": b[2]} for b in ref_bands]
        bands = (
            alt.Chart(alt.Data(values=band_data))
            .mark_rect(opacity=0.08, color="#5cb85c")
            .encode(y="y1:Q", y2="y2:Q")
        )
        layers = [bands] + layers
    return (
        alt.layer(*layers)
        .properties(width="container", height=180)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


def _bp_chart(rows: list[dict]) -> str:
    """Dual line: systolic + diastolic with normal-range reference band."""
    if not rows:
        return "{}"
    long = []
    for r in rows:
        if r.get("bp_systolic"):
            long.append({"date": r["date"], "reading": "Systolic", "mmhg": r["bp_systolic"]})
        if r.get("bp_diastolic"):
            long.append({"date": r["date"], "reading": "Diastolic", "mmhg": r["bp_diastolic"]})
    if not long:
        return "{}"
    ref_data = [
        {"y1": 90, "y2": 120, "label": "Normal systolic"},
        {"y1": 60, "y2": 80, "label": "Normal diastolic"},
    ]
    bands = (
        alt.Chart(alt.Data(values=ref_data))
        .mark_rect(opacity=0.07, color="#5cb85c")
        .encode(y="y1:Q", y2="y2:Q")
    )
    base = alt.Chart(alt.Data(values=long))
    lines = base.mark_line(strokeWidth=1.5, opacity=0.5).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("mmhg:Q", title="mmHg", scale=alt.Scale(domain=[50, 160])),
        color=alt.Color("reading:N", scale=alt.Scale(
            domain=["Systolic", "Diastolic"], range=["#e05c5c", "#4e9af1"]
        )),
    )
    dots = base.mark_point(size=60, opacity=0.9).encode(
        x="date:T", y="mmhg:Q",
        color=alt.Color("reading:N", legend=None),
        tooltip=["date:T", "reading:N", alt.Tooltip("mmhg:Q", title="mmHg")],
    )
    return (
        alt.layer(bands, lines, dots)
        .properties(width="container", height=200)
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .to_json()
    )


def _pace_trend_chart(rows: list[dict]) -> str:
    """Running pace (min/km) over time with 4-week rolling avg."""
    if not rows:
        return "{}"
    base = alt.Chart(alt.Data(values=rows))
    dots = base.mark_point(size=40, opacity=0.6, color="#4e9af1").encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("pace_min_km:Q", title="Pace (min/km)", scale=alt.Scale(zero=False),
                axis=alt.Axis(labelExpr="floor(datum.value) + ':' + (datum.value % 1 * 60 < 10 ? '0' : '') + floor(datum.value % 1 * 60)")),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("pace_min_km:Q", title="Pace (min/km)", format=".2f"),
            alt.Tooltip("distance_km:Q", title="Distance (km)", format=".1f"),
        ],
    )
    avg = base.mark_line(color="#f4a261", strokeWidth=2, strokeDash=[4, 4]).encode(
        x="date:T",
        y="pace_4w_avg:Q",
    )
    return (
        alt.layer(dots, avg)
        .properties(width="container", height=200, title="Running pace trend")
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .configure_title(color="#ccc")
        .to_json()
    )


def _hr_efficiency_chart(rows: list[dict]) -> str:
    """Scatter: avg HR vs pace for running. HR dropping at same pace = fitness gain."""
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_point(size=50, opacity=0.7)
        .encode(
            x=alt.X("pace_min_km:Q", title="Pace (min/km)", scale=alt.Scale(zero=False)),
            y=alt.Y("avg_hr:Q", title="Avg HR (bpm)", scale=alt.Scale(zero=False)),
            color=alt.Color("date:T", scale=alt.Scale(scheme="viridis"),
                            title="Date", legend=alt.Legend(orient="right")),
            tooltip=[
                alt.Tooltip("date:T", title="Date"),
                alt.Tooltip("pace_min_km:Q", title="Pace (min/km)", format=".2f"),
                alt.Tooltip("avg_hr:Q", title="Avg HR"),
                alt.Tooltip("distance_km:Q", title="Distance (km)", format=".1f"),
            ],
        )
        .properties(width="container", height=240, title="HR efficiency (pace vs avg HR) — color = recency, darker = older")
        .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
        .configure_view(strokeWidth=0, fill="#1a1a1a")
        .configure_title(color="#ccc")
    )
    return chart.to_json()


# ── login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, token: str = Form(...)):
    expected = (os.getenv("DATASETTE_TOKEN") or "").strip()
    if not expected:
        return templates.TemplateResponse(request, "login.html", {"error": "DATASETTE_TOKEN not configured"})
    if token.strip() != expected:
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid token"})
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        _token_hash(expected),
        max_age=COOKIE_TTL,
        httponly=True,
        samesite="lax",
    )
    return resp


# ── timezone helper ───────────────────────────────────────────────────────────

def _get_tz(browser_tz: str | None = None):
    """
    Return a ZoneInfo for the best available timezone source.
    Priority: browser cookie > HOME_TZ env > UTC.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    for name in (browser_tz, os.getenv("HOME_TZ"), "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, Exception):
            continue
    from zoneinfo import ZoneInfo
    return ZoneInfo("UTC")


def _local_now(browser_tz: str | None = None) -> datetime:
    """Current datetime in the best available local timezone."""
    from datetime import timezone
    return datetime.now(timezone.utc).astimezone(_get_tz(browser_tz))


def _utc_to_local_hour(ts_str: str, browser_tz: str | None = None) -> float | None:
    """Convert UTC ISO timestamp string to local hour-of-day float."""
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_get_tz(browser_tz))
        return local.hour + local.minute / 60
    except Exception:
        return None


# ── overview ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def overview(request: Request,
                   ms_dash_auth: str | None = Cookie(default=None),
                   ms_tz: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    local_now = _local_now(ms_tz)
    today = local_now.strftime("%Y-%m-%d")
    yesterday = (local_now - timedelta(days=1)).strftime("%Y-%m-%d")

    with db() as conn:
        # If HOME_TZ isn't set and it's early UTC (e.g. 1-6am), today might have
        # no data yet. Fall back to yesterday when that happens.
        has_today = _scalar(conn, "SELECT 1 FROM daily_metrics WHERE date=? LIMIT 1", (today,))
        if not has_today:
            has_yesterday = _scalar(conn, "SELECT 1 FROM daily_metrics WHERE date=? LIMIT 1", (yesterday,))
            if has_yesterday:
                today = yesterday
                yesterday = (local_now - timedelta(days=2)).strftime("%Y-%m-%d")

        # Readiness (shared module — same formula MCP server uses)
        readiness = readiness_from_db(conn, today)

        # Today's core metrics for the info strip
        today_row = conn.execute(
            """SELECT hrv, sleep_score, resting_hr, body_battery_high,
                      weight_kg, sleep_duration_min, stress_avg
               FROM daily_metrics WHERE date=?""",
            (today,),
        ).fetchone()

        # Wake time and sleep score for energy curve
        wake_row = conn.execute(
            """SELECT value FROM raw_daily_metrics
               WHERE date=? AND source='garmin' AND metric='sleep_wake_hour'""",
            (today,),
        ).fetchone()
        wake_hour: float | None = wake_row[0] if wake_row else None

        # If today's sleep data not yet imported, fall back to yesterday
        if wake_hour is None:
            wake_row_y = conn.execute(
                """SELECT value FROM raw_daily_metrics
                   WHERE date=? AND source='garmin' AND metric='sleep_wake_hour'""",
                (yesterday,),
            ).fetchone()
            wake_hour = wake_row_y[0] if wake_row_y else None

        # Today's caffeine logs for energy boost component
        caffeine_logs = _rows(conn,
            """SELECT ts, quantity FROM manual_logs
               WHERE type='caffeine' AND DATE(ts)=?
                 AND quantity IS NOT NULL AND quantity > 0
               ORDER BY ts""",
            (today,))

        # Today's nutrition totals
        today_nutrition_row = conn.execute(
            """SELECT SUM(estimated_calories),
                      GROUP_CONCAT(estimated_macros_json, char(31))
               FROM manual_logs
               WHERE type='meal' AND DATE(ts)=?""",
            (today,),
        ).fetchone()
        today_calories = today_nutrition_row[0] if today_nutrition_row else None
        today_macros = _parse_macros(today_nutrition_row[1]) if today_nutrition_row else {}
        today_meal_count = _scalar(conn,
            "SELECT COUNT(*) FROM manual_logs WHERE type='meal' AND DATE(ts)=?",
            (today,))

        # Nutrition targets
        protein_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='protein_g_daily'"
        ).fetchone()
        protein_target = float(protein_target_row[0]) if protein_target_row else 160.0

        calorie_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='calories_daily'"
        ).fetchone()
        calorie_target = float(calorie_target_row[0]) if calorie_target_row else None

    # Build caffeine doses list in local hours for the energy curve
    caffeine_doses: list[tuple[float, float]] = []
    for cl in caffeine_logs:
        local_h = _utc_to_local_hour(cl["ts"], ms_tz)
        if local_h is not None:
            try:
                caffeine_doses.append((float(cl["quantity"]), local_h))
            except (TypeError, ValueError):
                pass

    # Sleep score from today's metrics (for energy curve quality)
    sleep_score_for_curve = today_row[1] if today_row and today_row[1] else None

    # Today info strip (compact, not card grid)
    today_info: dict = {}
    if today_row:
        tr = today_row
        today_info = {
            "hrv": tr[0],
            "sleep_score": tr[1],
            "rhr": tr[2],
            "body_battery": tr[3],
            "weight_kg": tr[4],
            "sleep_h": round(tr[5] / 60, 1) if tr[5] else None,
            "stress": tr[6],
        }

    return templates.TemplateResponse(request, "overview.html", {
        "today": today,
        "readiness": readiness,
        "today_info": today_info,
        "today_calories": today_calories,
        "today_protein": today_macros.get("protein_g"),
        "today_carbs": today_macros.get("carbs_g"),
        "today_fat": today_macros.get("fat_g"),
        "today_meal_count": today_meal_count or 0,
        "protein_target": protein_target,
        "calorie_target": calorie_target,
        "energy_spec": _energy_chart(wake_hour, sleep_score_for_curve, caffeine_doses or None),
        "wake_hour": wake_hour,
    })


# ── trends ────────────────────────────────────────────────────────────────────

@router.get("/trends", response_class=HTMLResponse)
async def trends(request: Request, days: str = "30",
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

        # Zone load + ATL/CTL from raw_daily_metrics (pivot from EAV)
        load_raw = _rows(conn, """
            SELECT date, metric, value FROM raw_daily_metrics
            WHERE metric IN ('monthly_load_aerobic_low','monthly_load_aerobic_high',
                             'monthly_load_anaerobic','atl','ctl')
              AND date >= date('now', ?)
            ORDER BY date
        """, (clause,))
        load_by_date: dict[str, dict] = {}
        for r in load_raw:
            load_by_date.setdefault(r["date"], {"date": r["date"]})[r["metric"]] = r["value"]
        all_load_rows = sorted(load_by_date.values(), key=lambda x: x["date"])
        load_rows = all_load_rows  # zone load chart uses aerobic_low/high/anaerobic
        pmc_rows = all_load_rows   # PMC chart uses atl/ctl

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

        # Daily max HR from activities
        max_hr_rows = _rows(conn, """
            SELECT DATE(start_time) AS date, MAX(max_hr) AS max_hr
            FROM garmin_activities
            WHERE DATE(start_time) >= date('now', ?) AND max_hr IS NOT NULL
            GROUP BY DATE(start_time)
            ORDER BY date
        """, (clause,))

    # Merge rhr + max_hr by date for combined HR chart
    max_hr_by_date = {r["date"]: r["max_hr"] for r in max_hr_rows}
    hr_rows = []
    all_dates = sorted(set(r["date"] for r in rhr_rows) | set(max_hr_by_date.keys()))
    rhr_by_date = {r["date"]: r for r in rhr_rows}
    for d in all_dates:
        rr = rhr_by_date.get(d, {})
        hr_rows.append({
            "date": d,
            "resting_hr": rr.get("resting_hr"),
            "rhr_7d_avg": rr.get("rhr_7d_avg"),
            "max_hr": max_hr_by_date.get(d),
        })

    return templates.TemplateResponse(request, "trends.html", {
        "days": days,
        "hrv_spec": _trend_chart(hrv_rows, "hrv", "hrv_7d_avg", "HRV (ms)"),
        "sleep_spec": _sleep_chart(score_rows, stage_rows),
        "load_spec": _zone_load_chart(load_rows),
        "pmc_spec": _pmc_chart(pmc_rows),
        "battery_spec": _body_battery_chart(battery_rows),
        "hr_spec": _hr_chart(hr_rows),
    })


# ── behavior ──────────────────────────────────────────────────────────────────

@router.get("/behavior", response_class=HTMLResponse)
async def behavior(request: Request, days: str = "30",
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
              sleep_score,
              ROUND(sleep_duration_min / 60.0, 2) AS sleep_hours
            FROM daily_metrics
            WHERE date >= date('now', ?)
            ORDER BY date
        """, (clause,))

    alcohol_rows = [r for r in rows if r.get("alcohol_units") is not None]
    caffeine_rows = [r for r in rows if r.get("caffeine_mg") is not None]
    sleep_score_rows = [r for r in rows if r.get("sleep_score") is not None]
    sleep_dur_rows = [r for r in rows if r.get("sleep_hours") is not None]

    return templates.TemplateResponse(request, "behavior.html", {
        "days": days,
        "alcohol_spec": _calendar_heatmap(alcohol_rows, "alcohol_units", "Alcohol (units)", scheme="reds"),
        "caffeine_spec": _calendar_heatmap(caffeine_rows, "caffeine_mg", "Caffeine (mg)", scheme="purples"),
        "sleep_score_spec": _calendar_heatmap(sleep_score_rows, "sleep_score", "Sleep score", scheme="blues", zero_color="#111"),
        "sleep_dur_spec": _calendar_heatmap(sleep_dur_rows, "sleep_hours", "Sleep (hours)", scheme="greens", zero_color="#111"),
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
async def nutrition(request: Request, days: str = "30",
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

    # Add protein grams to cal_rows for time-series chart
    for r in cal_rows:
        m = macro_by_date.get(r["date"], {})
        protein = m.get("protein_g")
        r["protein_g"] = round(protein, 1) if protein is not None else None

    # Only days with macro data logged
    protein_rows = [r for r in cal_rows if r.get("protein_g") is not None]
    # Only days with calories logged
    cal_logged_rows = [r for r in cal_rows if r.get("calories")]

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

    from datetime import date as _date, timedelta
    days_int = int(days) if str(days).isdigit() else 30
    x_domain = [
        (_date.today() - timedelta(days=days_int)).isoformat(),
        _date.today().isoformat(),
    ]

    return templates.TemplateResponse(request, "nutrition.html", {
        "days": days,
        "protein_target": protein_target,
        "calories_spec": _daily_bar_chart(cal_logged_rows, "calories", "Calories (kcal)", color="#f4a261", x_domain=x_domain),
        "protein_spec": _daily_bar_chart(protein_rows, "protein_g", "Protein (g)", color="#e05c5c", ref_line=protein_target, x_domain=x_domain),
        "macro_dow_spec": _macro_dow_chart(dow_macro_rows),
    })


# ── activities ────────────────────────────────────────────────────────────────

@router.get("/activities", response_class=HTMLResponse)
async def activities(request: Request, days: str = "30",
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

        # Pace data: running only, need distance > 0 and duration > 0
        pace_raw = _rows(conn, """
            SELECT date,
              ROUND(distance_m / 1000.0, 2) AS distance_km,
              ROUND(duration_s / 60.0, 1) AS duration_min,
              avg_hr
            FROM activities
            WHERE date >= date('now', ?)
              AND LOWER(type) IN ('running','trail_running','treadmill_running','run')
              AND distance_m > 100 AND duration_s > 60
            ORDER BY date
        """, (clause,))

    # Compute pace (min/km) and rolling 4-week avg in Python
    pace_rows = []
    for r in pace_raw:
        if r["distance_km"] and r["duration_min"]:
            pace = r["duration_min"] / r["distance_km"]
            pace_rows.append({**r, "pace_min_km": round(pace, 3)})

    # 4-week rolling avg (last 4 entries as simple approximation)
    window = 4
    for i, r in enumerate(pace_rows):
        window_vals = [p["pace_min_km"] for p in pace_rows[max(0, i - window + 1): i + 1]]
        r["pace_4w_avg"] = round(sum(window_vals) / len(window_vals), 3)

    # HR efficiency: runs with both avg_hr and pace
    hr_eff_rows = [r for r in pace_rows if r.get("avg_hr")]

    with db() as conn:
        vo2max_rows = _rows(conn, """
            SELECT date, vo2max FROM daily_metrics
            WHERE date >= date('now', ?) AND vo2max IS NOT NULL
            ORDER BY date
        """, (clause,))

    return templates.TemplateResponse(request, "activities.html", {
        "days": days,
        "table_rows": table_rows,
        "count_spec": _activity_bar(count_rows, "count", "Activities"),
        "volume_spec": _weekly_volume_chart(weekly_rows),
        "pace_spec": _pace_trend_chart(pace_rows),
        "hr_eff_spec": _hr_efficiency_chart(hr_eff_rows),
        "vo2max_spec": _sparse_line_chart(vo2max_rows, "vo2max", "VO2 Max (mL/kg/min)", color="#5cb85c"),
    })


# ── vitals ────────────────────────────────────────────────────────────────────

@router.get("/vitals", response_class=HTMLResponse)
async def vitals(request: Request, days: str = "30",
                 ms_dash_auth: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    clause = _days_clause(days)

    with db() as conn:
        spo2_rows = _rows(conn, """
            SELECT date, spo2_avg FROM daily_metrics
            WHERE date >= date('now', ?) AND spo2_avg IS NOT NULL
            ORDER BY date
        """, (clause,))

        weight_rows = _rows(conn, """
            SELECT date, weight_kg FROM daily_metrics
            WHERE date >= date('now', ?) AND weight_kg IS NOT NULL
            ORDER BY date
        """, (clause,))

        bp_rows = _rows(conn, """
            SELECT date, bp_systolic, bp_diastolic FROM daily_metrics
            WHERE date >= date('now', ?)
              AND (bp_systolic IS NOT NULL OR bp_diastolic IS NOT NULL)
            ORDER BY date
        """, (clause,))

    return templates.TemplateResponse(request, "vitals.html", {
        "days": days,
        "weight_spec": _sparse_line_chart(weight_rows, "weight_kg", "Weight (kg)", color="#f4a261"),
        "spo2_spec": _sparse_line_chart(spo2_rows, "spo2_avg", "SpO2 (%)", color="#7ec8e3"),
        "bp_spec": _bp_chart(bp_rows),
    })
