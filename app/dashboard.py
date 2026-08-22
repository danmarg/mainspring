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
import statistics
from datetime import date, datetime, timedelta, timezone

import altair as alt
from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import db, DEFAULT_SOURCE_PRIORITY
from app.readiness import alertness_curve, average_wake_hour_from_db, illness_risk_from_db, readiness_from_db, sleep_regularity_from_db, trimp_from_hr_samples

log = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard")

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates", "dashboard")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# ── auth ─────────────────────────────────────────────────────────────────────

COOKIE_NAME = "ms_dash_auth"
COOKIE_TTL = 30 * 86400  # 30 days, matching the MCP refresh token lifetime


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


def _x_domain(days: str) -> list[str]:
    """[start, today] ISO date bounds so charts' x-axis always extends to today,
    even when the most recent data point (e.g. delayed sync) is a day or more old."""
    days_int = int(days) if str(days).isdigit() else 30
    today = date.today()
    return [(today - timedelta(days=days_int)).isoformat(), today.isoformat()]


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _scalar(conn, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _import_status_summary() -> list[dict]:
    """Most recent import_runs row per active source, for the nav status strip."""
    placeholders = ",".join("?" * len(DEFAULT_SOURCE_PRIORITY))
    with db() as conn:
        rows = _rows(conn, f"""
            SELECT source, finished_at, started_at, status
            FROM import_runs
            WHERE source IN ({placeholders})
              AND id IN (SELECT MAX(id) FROM import_runs GROUP BY source)
            ORDER BY source
        """, tuple(DEFAULT_SOURCE_PRIORITY))
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        ts_str = r["finished_at"] or r["started_at"]
        ago_min = None
        if ts_str:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ago_min = int((now - ts).total_seconds() // 60)
        out.append({
            "source": r["source"],
            "status": r["status"],
            "ago_min": ago_min,
            "stale": r["status"] == "error" or ago_min is None or ago_min > 90,
        })
    return out


templates.env.globals["import_status"] = _import_status_summary


# ── chart builders ────────────────────────────────────────────────────────────

def _dark(chart: alt.Chart, title: bool = False) -> alt.Chart:
    """Apply standard dark-theme configure to a chart."""
    c = (chart
         .configure_axis(grid=True, gridColor="#333", labelColor="#aaa", titleColor="#aaa")
         .configure_view(strokeWidth=0, fill="#1a1a1a"))
    return c.configure_title(color="#ccc") if title else c

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
                 height: int = 200, x_domain: list[str] | None = None) -> str:
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    base = alt.Chart(alt.Data(values=rows))
    line = base.mark_line(color=color, strokeWidth=1.5).encode(
        x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
        y=alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip(f"{field}:Q", title=title)],
    )
    avg = base.mark_line(color=avg_color, strokeDash=[4, 4], opacity=0.85, strokeWidth=1.5).encode(
        x=alt.X("date:T", scale=x_scale),
        y=f"{avg_field}:Q",
        tooltip=[alt.Tooltip(f"{avg_field}:Q", title="7d avg")],
    )
    return _dark(
        alt.layer(line, avg).properties(width="container", height=height)
    ).to_json()


def _sleep_chart(rows_score: list[dict], rows_stages: list[dict],
                 x_domain: list[str] | None = None) -> str:
    if not rows_score:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    score_chart = (
        alt.Chart(alt.Data(values=rows_score))
        .mark_line(color="#7ec8e3", strokeWidth=1.5)
        .encode(
            x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
            y=alt.Y("sleep_score:Q", title="Sleep score", scale=alt.Scale(domain=[0, 100])),
            tooltip=["date:T", "sleep_score:Q"],
        )
        .properties(height=140)
    )
    if rows_stages:
        stages_chart = (
            alt.Chart(alt.Data(values=rows_stages))
            .mark_area()
            .encode(
                x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
                y=alt.Y("minutes:Q", title="Stage (min)", stack="zero"),
                color=alt.Color("stage:N", scale=alt.Scale(
                    domain=["Deep", "REM", "Light"],
                    range=["#1a6b9e", "#7b4fa6", "#2d8a6d"],
                ), legend=alt.Legend(orient="bottom", labelColor="#aaa", titleColor="#aaa")),
                tooltip=["date:T", "stage:N", "minutes:Q"],
            )
            .properties(height=120)
        )
        combined = (
            alt.vconcat(score_chart, stages_chart, spacing=8)
            .resolve_scale(x="shared")
        )
    else:
        combined = score_chart.properties(width="container")
    spec = json.loads(_dark(combined).to_json())
    # width="container" must be on each sub-chart individually; setting it at the
    # vconcat top level or via autosize leaves sub-charts at their ~300px default.
    for sub in spec.get("vconcat", []):
        sub["width"] = "container"
    return json.dumps(spec)


def _zone_load_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Stacked area: monthly zone load trend (aerobic low/high, anaerobic)."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
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
            x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
            y=alt.Y("load:Q", title="Monthly zone load", stack="zero"),
            color=alt.Color("zone:N", scale=alt.Scale(
                domain=["Aerobic low", "Aerobic high", "Anaerobic"],
                range=["#4e9af1", "#f4a261", "#e05c5c"],
            ), title="Zone"),
            tooltip=["date:T", "zone:N", alt.Tooltip("load:Q", format=".0f")],
        )
        .properties(width="container", height=200)
    )
    return _dark(chart).to_json()


def _polarization_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Stacked bar: weekly training-time polarization (easy/moderate/hard %),
    from real per-activity HR time-in-zone (activity_hr_zones) rather than
    whole-session avg HR — the latter drags an interval session's recovery
    jogs into "moderate" and hides exactly the grey-zone drift this chart is
    meant to catch. A polarized week reads as thick blue+red, thin orange;
    a week dominated by orange has drifted into unproductive grey-zone training."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    long = []
    for r in rows:
        for zone, key in [("Easy", "easy_pct"), ("Moderate", "moderate_pct"), ("Hard", "hard_pct")]:
            if r.get(key) is not None:
                long.append({"week": r["week"], "zone": zone, "pct": r[key]})
    if not long:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=long))
        .mark_bar()
        .encode(
            x=alt.X("week:T", timeUnit="yearweek", title=None, scale=x_scale, axis=alt.Axis(format="%b %d")),
            y=alt.Y("pct:Q", title="% of training time", stack="zero", scale=alt.Scale(domain=[0, 100])),
            color=alt.Color("zone:N", scale=alt.Scale(
                domain=["Easy", "Moderate", "Hard"],
                range=["#4e9af1", "#f4a261", "#e05c5c"],
            ), title="Intensity"),
            tooltip=["week:T", "zone:N", alt.Tooltip("pct:Q", format=".0f")],
        )
        .properties(width="container", height=200)
    )
    return _dark(chart).to_json()


def _hr_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Dual line: resting HR (blue) + daily max HR from activities (red) on one chart."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    rhr_rows = [r for r in rows if r.get("resting_hr") is not None]
    max_rows = [r for r in rows if r.get("max_hr") is not None]
    if not rhr_rows and not max_rows:
        return "{}"
    layers = []
    if rhr_rows:
        base_rhr = alt.Chart(alt.Data(values=rhr_rows))
        layers.append(base_rhr.mark_line(color="#4e9af1", strokeWidth=1.5).encode(
            x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
            y=alt.Y("resting_hr:Q", title="HR (bpm)", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("resting_hr:Q", title="Resting HR")],
        ))
        layers.append(base_rhr.mark_line(color="#4e9af1", strokeDash=[4, 4], opacity=0.6, strokeWidth=1.2).encode(
            x=alt.X("date:T", scale=x_scale), y="rhr_7d_avg:Q",
        ))
    if max_rows:
        base_max = alt.Chart(alt.Data(values=max_rows))
        layers.append(base_max.mark_line(color="#e05c5c", strokeWidth=1.5, opacity=0.7).encode(
            x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
            y=alt.Y("max_hr:Q", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("max_hr:Q", title="Max HR (activity)")],
        ))
    return _dark(
        alt.layer(*layers).properties(width="container", height=200)
    ).to_json()


def _body_battery_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_area(opacity=0.6, color="#f4a261")
        .encode(
            x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
            y=alt.Y("body_battery_low:Q", title="Body battery", scale=alt.Scale(domain=[0, 100])),
            y2="body_battery_high:Q",
            tooltip=["date:T", "body_battery_high:Q", "body_battery_low:Q"],
        )
        .properties(width="container", height=160)
    )
    return _dark(chart).to_json()


RELATIONSHIP_INPUTS = {
    "alcohol_units": "Alcohol (units)",
    "caffeine_mg": "Caffeine (mg)",
    "calories_estimated": "Calories (kcal)",
    "rpe": "Workout RPE",
    "acute_training_load": "Acute training load",
}
RELATIONSHIP_OUTPUTS = {
    "hrv": "HRV (ms)",
    "sleep_score": "Sleep score",
    "resting_hr": "Resting HR (bpm)",
    "body_battery_high": "Body battery high",
    "stress_avg": "Stress average",
    "training_readiness": "Training readiness",
}


def _relationship_rows(conn, input_field: str, output_field: str, lag_days: int, days: int) -> list[dict]:
    """Pair an input day with an outcome measured lag_days later."""
    if input_field not in RELATIONSHIP_INPUTS or output_field not in RELATIONSHIP_OUTPUTS:
        return []
    start = date.today() - timedelta(days=max(1, days) - 1)
    end = date.today()
    rows = conn.execute(
        f"SELECT date, {input_field}, {output_field} FROM daily_metrics "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        (start.isoformat(), (end + timedelta(days=lag_days)).isoformat()),
    ).fetchall()
    by_date = {row[0]: {"input": row[1], "output": row[2]} for row in rows}
    pairs = []
    for d_str, values in by_date.items():
        try:
            outcome_date = (date.fromisoformat(d_str) + timedelta(days=lag_days)).isoformat()
        except ValueError:
            continue
        outcome = by_date.get(outcome_date, {}).get("output")
        if values["input"] is None or outcome is None:
            continue
        pairs.append({
            "input": float(values["input"]),
            "output": float(outcome),
            "date": d_str,
            "outcome_date": outcome_date,
        })
    return pairs


def _relationship_chart(rows: list[dict], input_label: str, output_label: str) -> tuple[str, dict]:
    """Scatter + regression line and compact correlation diagnostics."""
    if len(rows) < 3:
        return "{}", {"n": len(rows), "r": None}
    xs = [row["input"] for row in rows]
    ys = [row["output"] for row in rows]
    try:
        r = statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        r = None
    base = alt.Chart(alt.Data(values=rows))
    points = base.mark_circle(size=65, opacity=0.75, color="#4e9af1").encode(
        x=alt.X("input:Q", title=input_label),
        y=alt.Y("output:Q", title=output_label),
        tooltip=[
            alt.Tooltip("date:T", title="Input date"),
            alt.Tooltip("outcome_date:T", title="Outcome date"),
            alt.Tooltip("input:Q", title=input_label, format=".1f"),
            alt.Tooltip("output:Q", title=output_label, format=".1f"),
        ],
    )
    regression = base.transform_regression("input", "output").mark_line(color="#f4a261", strokeWidth=2).encode(
        x="input:Q", y="output:Q"
    )
    chart = _dark((points + regression).properties(width="container", height=190)).to_json()
    return chart, {"n": len(rows), "r": round(r, 2) if r is not None else None}


def _diverging_bar_chart(rows: list[dict], field: str, title: str,
                         x_domain: list[str] | None = None, invert_color: bool = False) -> str:
    """Bar chart with green bars above zero and red below — for HRV delta etc.
    Pass invert_color=True when positive values are the unfavorable direction
    (e.g. aerobic decoupling, where higher = more fatigue drift)."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    good_color, bad_color = ("#e05c5c", "#2ecc71") if invert_color else ("#2ecc71", "#e05c5c")
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar()
        .encode(
            x=alt.X("date:T", timeUnit="yearmonthdate", title=None, scale=x_scale,
                    axis=alt.Axis(format="%b %d")),
            y=alt.Y(f"{field}:Q", title=title),
            color=alt.condition(
                alt.datum[field] >= 0,
                alt.value(good_color),
                alt.value(bad_color),
            ),
            tooltip=[alt.Tooltip("date:T", title="Date"),
                     alt.Tooltip(f"{field}:Q", title=title, format=".1f")],
        )
        .properties(width="container", height=160)
    )
    return _dark(chart).to_json()


def _dow_avg_chart(rows: list[dict], title: str, color: str) -> str:
    """Bar chart of average value by day of week. rows: [{dow_name, avg_val}]."""
    if not rows:
        return "{}"
    DOW_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar(color=color, opacity=0.85)
        .encode(
            x=alt.X("dow_name:N", title=None, sort=DOW_ORDER,
                    scale=alt.Scale(domain=DOW_ORDER)),
            y=alt.Y("avg_val:Q", title=title),
            tooltip=["dow_name:N", alt.Tooltip("avg_val:Q", title=title, format=".1f")],
        )
        .properties(width="container", height=120)
    )
    return _dark(chart).to_json()


def _compute_dow_avgs(rows: list[dict], field: str) -> list[dict]:
    """Aggregate a field by day of week; rows must have 'dow' (strftime %w) key."""
    DOW_MAP = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
               "4": "Thu", "5": "Fri", "6": "Sat"}
    accum: dict[str, list] = {}
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        dow = str(r.get("dow", "0"))
        accum.setdefault(dow, []).append(v)
    return [
        {"dow_name": DOW_MAP.get(dow, dow), "avg_val": round(sum(vals) / len(vals), 2)}
        for dow, vals in sorted(accum.items())
    ]


def _heatmap_to_json(chart: alt.Chart) -> str:
    spec = json.loads(chart.to_json())
    spec["autosize"] = {"type": "fit-x", "contains": "padding"}
    return json.dumps(spec)


def _calendar_heatmap(rows: list[dict], field: str, title: str,
                      scheme: str = "reds", zero_color: str = "#1a1a1a") -> str:
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
    return _heatmap_to_json(chart)


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
    return _heatmap_to_json(chart)


def _pmc_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Performance Management Chart: CTL (fitness), ATL (fatigue), TSB (form) bars."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
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
                x=alt.X("date:T", timeUnit="yearmonthdate", title=None, scale=x_scale,
                        axis=alt.Axis(format="%b %d")),
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
                x=alt.X("date:T", scale=x_scale),
                y=alt.Y("ctl:Q", scale=alt.Scale(zero=True)),
                tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("ctl:Q", title="CTL (fitness)")],
            )
        )
    if atl_rows:
        layers.append(
            alt.Chart(alt.Data(values=atl_rows))
            .mark_line(color="#f4a261", strokeWidth=2)
            .encode(
                x=alt.X("date:T", scale=x_scale),
                y=alt.Y("atl:Q", scale=alt.Scale(zero=True)),
                tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("atl:Q", title="ATL (fatigue)")],
            )
        )

    return _dark(
        alt.layer(*layers).resolve_scale(y="shared").properties(width="container", height=220)
    ).to_json()


def _activity_end_local_hour(start_time: str, duration_s: int, browser_tz: str | None) -> float | None:
    """Return local clock hour when the activity ended; handles TZ-aware and naive start_times."""
    try:
        dt = datetime.fromisoformat(start_time)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_get_tz(browser_tz))
        # TZ-naive (Garmin startTimeLocal) → already local time
        end_dt = dt + timedelta(seconds=duration_s)
        return end_dt.hour + end_dt.minute / 60 + end_dt.second / 3600
    except Exception:
        return None


def _energy_chart(wake_hour: float | None, sleep_score: float | None,
                  caffeine_doses: list[tuple[float, float]] | None = None,
                  activity_boosts: list[tuple[float, float]] | None = None,
                  strain_events: list[tuple[float, float]] | None = None) -> str:
    """
    Energy forecast chart using two-process alertness model (Borbély 1982).
    Anchored to today's wake time from Garmin sleep data.
    The "now" rule is injected client-side so timezone is always accurate.
    """
    if wake_hour is None:
        return "{}"

    FORECAST_HOURS = 18  # slightly past midnight for late sleepers
    curve = alertness_curve(wake_hour, sleep_score or 75.0, FORECAST_HOURS,
                            caffeine_doses=caffeine_doses, activity_boosts=activity_boosts,
                            strain_events=strain_events)

    # Use hours-since-wake on X axis to avoid midnight-wrapping issues
    curve_hw = [
        {**r, "hw": i / 4.0, "label": f"{int(r['hour']):02d}:{int((r['hour'] % 1) * 60):02d}"}
        for i, r in enumerate(curve)
    ]
    peak = max(curve_hw, key=lambda r: r["alertness"])
    peak_threshold = peak["alertness"] * 0.85
    peak_idx = curve_hw.index(peak)
    start_idx = peak_idx
    end_idx = peak_idx
    while start_idx > 0 and curve_hw[start_idx - 1]["alertness"] >= peak_threshold:
        start_idx -= 1
    while end_idx < len(curve_hw) - 1 and curve_hw[end_idx + 1]["alertness"] >= peak_threshold:
        end_idx += 1
    peak_range = [{"start": curve_hw[start_idx]["hw"],
                   "end": min(FORECAST_HOURS, curve_hw[end_idx]["hw"] + 0.25),
                   "alertness": peak["alertness"], "zero": 0,
                   "label": "Peak alertness range"}]

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
                 alt.Tooltip("alertness:Q", title="Alertness", format=".0f"),
                 alt.Tooltip("exercise_strain:Q", title="Exercise strain", format=".0f")],
    )
    peak_bar = (
        alt.Chart(alt.Data(values=peak_range))
        .mark_rule(color="#2ecc71", strokeWidth=7)
        .encode(x="start:Q", x2="end:Q", y="zero:Q",
                tooltip=[alt.Tooltip("label:N", title="Window"),
                         alt.Tooltip("alertness:Q", title="Peak", format=".0f")])
    )
    return _dark(
        alt.layer(area, line, peak_bar).properties(width="container", height=160)
    ).to_json()


def _live_curve_json(wake_hour: float | None, sleep_score: float | None,
                     caffeine_doses: list[tuple[float, float]] | None = None,
                     activity_boosts: list[tuple[float, float]] | None = None,
                     strain_events: list[tuple[float, float]] | None = None) -> str:
    """Raw alertness curve (hours-since-wake, alertness) for the client-side
    live status widget, which recomputes 'good window to train' / bedtime
    text every minute without a page reload."""
    if wake_hour is None:
        return "[]"
    FORECAST_HOURS = 18
    curve = alertness_curve(wake_hour, sleep_score or 75.0, FORECAST_HOURS,
                            caffeine_doses=caffeine_doses, activity_boosts=activity_boosts,
                            strain_events=strain_events)
    return json.dumps([{"h": i / 4.0, "a": r["alertness"]} for i, r in enumerate(curve)])


def _sleep_debt_minutes(recent_sleep_rows: list[dict], target_min: float,
                        lookback_nights: int = 3, cap_total_min: float = 180.0) -> float:
    """
    Sum of (target - actual) sleep duration over the most recent lookback_nights
    nights with data. Capped at cap_total_min (3h default) so a long gap in data
    or an extended bad stretch doesn't produce an absurd bedtime recommendation —
    practical sleep-debt guidance treats debt beyond a couple nights as something
    to repay gradually rather than in one sitting anyway.
    """
    deficits = [max(0.0, target_min - r["sleep_duration_min"])
                for r in recent_sleep_rows[-lookback_nights:]]
    return min(sum(deficits), cap_total_min)


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
    )
    return _dark(chart).to_json()


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
    )
    return _dark(chart).to_json()


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
            x=alt.X("date:T", timeUnit="yearmonthdate", title=None, scale=x_scale,
                    axis=alt.Axis(format="%b %e")),
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
    return _dark(
        alt.layer(*layers).properties(width="container", height=200)
    ).to_json()


def _macro_dow_chart(rows: list[dict]) -> str:
    if not rows:
        return "{}"
    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar()
        .encode(
            x=alt.X("dow_name:N", title=None,
                    sort=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                    scale=alt.Scale(domain=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])),
            y=alt.Y("avg_g:Q", title="Avg grams", stack="zero"),
            color=alt.Color("macro:N", scale=alt.Scale(
                domain=["protein_g", "carbs_g", "fat_g"],
                range=["#e05c5c", "#f4a261", "#4e9af1"],
            ), title="Macro"),
            tooltip=["dow_name:N", "macro:N", alt.Tooltip("avg_g:Q", title="Avg g", format=".1f")],
        )
        .properties(width="container", height=220, title="Average macros by day of week")
    )
    return _dark(chart, title=True).to_json()


def _sparse_line_chart(rows: list[dict], field: str, title: str,
                       color: str = "#4e9af1", ref_bands: list | None = None,
                       x_domain: list[str] | None = None) -> str:
    """Dots + connecting line for infrequently sampled metrics (weight, BP, VO2max)."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    base = alt.Chart(alt.Data(values=rows))
    line = base.mark_line(color=color, opacity=0.4, strokeWidth=1).encode(
        x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
        y=alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=False)),
    )
    dots = base.mark_point(color=color, size=60, opacity=0.9).encode(
        x=alt.X("date:T", scale=x_scale),
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
    return _dark(
        alt.layer(*layers).properties(width="container", height=180)
    ).to_json()


def _bp_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Systolic + diastolic + pulse, with a normal-range reference band for BP."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    long = []
    for r in rows:
        if r.get("bp_systolic"):
            long.append({"date": r["date"], "reading": "Systolic", "value": r["bp_systolic"]})
        if r.get("bp_diastolic"):
            long.append({"date": r["date"], "reading": "Diastolic", "value": r["bp_diastolic"]})
        if r.get("bp_pulse"):
            long.append({"date": r["date"], "reading": "Pulse", "value": r["bp_pulse"]})
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
        x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
        y=alt.Y("value:Q", title="mmHg / bpm", scale=alt.Scale(domain=[50, 160])),
        color=alt.Color("reading:N", scale=alt.Scale(
            domain=["Systolic", "Diastolic", "Pulse"], range=["#e05c5c", "#4e9af1", "#f4a261"]
        )),
    )
    dots = base.mark_point(size=60, opacity=0.9).encode(
        x=alt.X("date:T", scale=x_scale), y="value:Q",
        color=alt.Color("reading:N", legend=None),
        tooltip=["date:T", "reading:N", alt.Tooltip("value:Q", title="Value")],
    )
    return _dark(
        alt.layer(bands, lines, dots).properties(width="container", height=200)
    ).to_json()


def _hr_zones_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Stacked area of personalized HR zone boundaries over time. rows:
    [{date, zone, min_bpm, max_bpm}, ...]. Each date's stack starts at that
    day's zone-1 floor (not zero) so the band tops line up with real bpm
    values — the top edge of each layer is exactly that zone's ceiling."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    zone_colors = ["#2ecc71", "#a8d95e", "#f4a261", "#e76f51", "#e05c5c"]
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    long = []
    for d, zones in sorted(by_date.items()):
        zones = sorted(zones, key=lambda z: z["zone"])
        if not zones:
            continue
        running = zones[0]["min_bpm"]
        for z in zones:
            top = running + (z["max_bpm"] - z["min_bpm"])
            long.append({
                "date": d, "zone": f"Z{z['zone']}", "y1": running, "y2": top,
                "min_bpm": z["min_bpm"], "max_bpm": z["max_bpm"],
            })
            running = top
    if not long:
        return "{}"

    chart = (
        alt.Chart(alt.Data(values=long))
        .mark_area(opacity=0.85, line={"strokeWidth": 0.5, "color": "#1a1a1a"})
        .encode(
            x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
            y=alt.Y("y1:Q", title="Zone (bpm)"),
            y2="y2:Q",
            color=alt.Color("zone:N", scale=alt.Scale(
                domain=["Z1", "Z2", "Z3", "Z4", "Z5"], range=zone_colors,
            ), title="Zone", legend=alt.Legend(orient="bottom", labelColor="#aaa", titleColor="#aaa")),
            tooltip=[
                alt.Tooltip("date:T", title="Date"), "zone:N",
                alt.Tooltip("min_bpm:Q", title="Min bpm"), alt.Tooltip("max_bpm:Q", title="Max bpm"),
            ],
        )
        .properties(width="container", height=200)
    )
    return _dark(chart).to_json()


def _running_economy_chart(rows: list[dict], band_pct: float = 0.07) -> str:
    """HR trend at a consistent effort: filters runs to within band_pct of the
    median pace, then plots avg HR over time with a linear trend line — same
    pace, lower HR over time = improving running economy."""
    if len(rows) < 3:
        return "{}"
    paces = sorted(r["pace_min_km"] for r in rows)
    median_pace = paces[len(paces) // 2]
    lo, hi = median_pace * (1 - band_pct), median_pace * (1 + band_pct)
    band_rows = [r for r in rows if lo <= r["pace_min_km"] <= hi]
    if len(band_rows) < 3:
        return "{}"

    xs = [datetime.fromisoformat(r["date"]).toordinal() for r in band_rows]
    ys = [r["avg_hr"] for r in band_rows]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs) or 1
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    trend_rows = [
        {"date": r["date"], "trend_hr": slope * datetime.fromisoformat(r["date"]).toordinal() + intercept}
        for r in band_rows
    ]

    base = alt.Chart(alt.Data(values=band_rows))
    dots = base.mark_point(size=60, opacity=0.7, color="#4e9af1").encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(tickCount="day", format="%b %d")),
        y=alt.Y("avg_hr:Q", title="Avg HR (bpm)", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("pace_min_km:Q", title="Pace (min/km)", format=".2f"),
            alt.Tooltip("avg_hr:Q", title="Avg HR"),
        ],
    )
    trend = alt.Chart(alt.Data(values=trend_rows)).mark_line(
        color="#f4a261", strokeWidth=2, strokeDash=[4, 4]
    ).encode(x="date:T", y="trend_hr:Q")

    return _dark(
        alt.layer(dots, trend).properties(
            width="container", height=220,
            title=f"Running economy — HR at ~{median_pace:.2f} min/km pace (±{int(band_pct * 100)}%)",
        ),
        title=True,
    ).to_json()


def _pace_trend_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Running pace (min/km) over time with 4-week rolling avg."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    base = alt.Chart(alt.Data(values=rows))
    dots = base.mark_point(size=40, opacity=0.6, color="#4e9af1").encode(
        x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
        y=alt.Y("pace_min_km:Q", title="Pace (min/km)", scale=alt.Scale(zero=False),
                axis=alt.Axis(labelExpr="floor(datum.value) + ':' + (datum.value % 1 * 60 < 10 ? '0' : '') + floor(datum.value % 1 * 60)")),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("pace_min_km:Q", title="Pace (min/km)", format=".2f"),
            alt.Tooltip("distance_km:Q", title="Distance (km)", format=".1f"),
        ],
    )
    avg = base.mark_line(color="#f4a261", strokeWidth=2, strokeDash=[4, 4]).encode(
        x=alt.X("date:T", scale=x_scale),
        y="pace_4w_avg:Q",
    )
    return _dark(
        alt.layer(dots, avg).properties(width="container", height=200, title="Running pace trend"),
        title=True,
    ).to_json()


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
    )
    return _dark(chart, title=True).to_json()


def _ef_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Efficiency Factor (speed per bpm) over time with 4-week rolling avg.
    Rising EF = more speed for the same HR cost = improving aerobic fitness,
    across all runs rather than just those near the median pace."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    base = alt.Chart(alt.Data(values=rows))
    dots = base.mark_point(size=40, opacity=0.6, color="#4e9af1").encode(
        x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
        y=alt.Y("ef:Q", title="Efficiency factor (m/min per bpm)", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("ef:Q", title="EF", format=".3f"),
            alt.Tooltip("pace_min_km:Q", title="Pace (min/km)", format=".2f"),
            alt.Tooltip("avg_hr:Q", title="Avg HR"),
        ],
    )
    avg = base.mark_line(color="#f4a261", strokeWidth=2, strokeDash=[4, 4]).encode(
        x=alt.X("date:T", scale=x_scale),
        y="ef_4w_avg:Q",
    )
    return _dark(
        alt.layer(dots, avg).properties(width="container", height=200, title="Efficiency factor trend"),
        title=True,
    ).to_json()


def _training_effect_chart(rows: list[dict], x_domain: list[str] | None = None) -> str:
    """Aerobic vs anaerobic training effect per session (Garmin's 0-5 scale) —
    shows whether recent training is building base fitness or high-intensity
    stimulus, and whether the balance matches intent."""
    if not rows:
        return "{}"
    x_scale = alt.Scale(domain=x_domain) if x_domain else alt.Undefined
    base = alt.Chart(alt.Data(values=rows))
    aerobic = base.mark_point(size=50, opacity=0.8, color="#4e9af1", filled=True).encode(
        x=alt.X("date:T", title=None, scale=x_scale, axis=alt.Axis(tickCount="day", format="%b %d")),
        y=alt.Y("training_effect_aerobic:Q", title="Training effect (0-5)",
                scale=alt.Scale(domain=[0, 5])),
        tooltip=[alt.Tooltip("date:T", title="Date"),
                 alt.Tooltip("training_effect_aerobic:Q", title="Aerobic", format=".1f")],
    )
    anaerobic = base.mark_point(size=50, opacity=0.8, color="#e76f51", filled=True).encode(
        x=alt.X("date:T", scale=x_scale),
        y=alt.Y("training_effect_anaerobic:Q", scale=alt.Scale(domain=[0, 5])),
        tooltip=[alt.Tooltip("date:T", title="Date"),
                 alt.Tooltip("training_effect_anaerobic:Q", title="Anaerobic", format=".1f")],
    )
    return _dark(
        alt.layer(aerobic, anaerobic).properties(
            width="container", height=220,
            title="Training effect — blue = aerobic, orange = anaerobic",
        ),
        title=True,
    ).to_json()


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
    return ZoneInfo("UTC")


def _local_now(browser_tz: str | None = None) -> datetime:
    """Current datetime in the best available local timezone."""
    return datetime.now(timezone.utc).astimezone(_get_tz(browser_tz))


def _local_day_utc_bounds(local_date_str: str, browser_tz: str | None = None) -> tuple[str, str]:
    """
    Return (utc_start, utc_end) ISO strings bracketing a local calendar day.
    Used for ts >= utc_start AND ts < utc_end queries on manual_logs.ts (stored UTC).
    """
    tz = _get_tz(browser_tz)
    naive = datetime.strptime(local_date_str, "%Y-%m-%d")
    start = naive.replace(tzinfo=tz).astimezone(timezone.utc)
    end = (naive + timedelta(days=1)).replace(tzinfo=tz).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def _local_window_utc_start(days_int: int, browser_tz: str | None = None) -> str:
    """
    Return UTC ISO string for local midnight N days ago — the correct lower bound
    for range queries on manual_logs.ts when the user wants the last N local days.
    """
    tz = _get_tz(browser_tz)
    local_now = datetime.now(timezone.utc).astimezone(tz)
    local_start = (local_now - timedelta(days=days_int)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc).isoformat()


def _utc_to_local_hour(ts_str: str, browser_tz: str | None = None) -> float | None:
    """Convert UTC ISO timestamp string to local hour-of-day float."""
    try:
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
        # For readiness/metrics, use the most recent date with actual biometric data.
        # Garmin doesn't return daily summaries for the current UTC day until the next
        # morning import, so today's row is often all NULLs until ~6am local.
        # Include resting_hr and sleep_duration_min so Fitbit-only days (no Garmin HRV/score)
        # still resolve to the correct date rather than falling back to the last Garmin date.
        metrics_date = _scalar(conn,
            """SELECT date FROM daily_metrics
               WHERE hrv IS NOT NULL OR sleep_score IS NOT NULL
                  OR resting_hr IS NOT NULL OR sleep_duration_min IS NOT NULL
               ORDER BY date DESC LIMIT 1""")
        if not metrics_date:
            metrics_date = today

        # Readiness (shared module — same formula MCP server uses)
        readiness = readiness_from_db(conn, metrics_date)

        # Illness/stress risk — trailing-window RHR/HRV/skin-temp concordance check
        illness_risk = illness_risk_from_db(conn, metrics_date)

        # Biometric stat cards and energy curve use metrics_date (most recent with data)
        today_row = conn.execute(
            """SELECT hrv, sleep_score, resting_hr, body_battery_high,
                      weight_kg, sleep_duration_min, stress_avg
               FROM daily_metrics WHERE date=?""",
            (metrics_date,),
        ).fetchone()

        # Wake time for energy curve — prefer garmin, fall back to any available source.
        # Google Health (Fitbit) also provides sleep_wake_hour so the chart works on
        # Fitbit-only days when Garmin isn't worn.
        wake_row = conn.execute(
            """SELECT value FROM raw_daily_metrics
               WHERE date=? AND metric='sleep_wake_hour'
               ORDER BY CASE source WHEN 'garmin' THEN 0 WHEN 'google_health' THEN 1 ELSE 2 END
               LIMIT 1""",
            (metrics_date,),
        ).fetchone()
        wake_hour: float | None = wake_row[0] if wake_row else None

        # Trailing average wake time, for anchoring the bedtime recommendation instead
        # of today's single (possibly anomalous) wake — see average_wake_hour_from_db.
        bedtime_wake_hour = average_wake_hour_from_db(conn, metrics_date)
        if bedtime_wake_hour is None:
            bedtime_wake_hour = wake_hour

        # UTC bounds for today's local date — used for all manual_logs queries so that
        # logs made late in the evening (after UTC midnight) land on the right local day.
        today_utc_start, today_utc_end = _local_day_utc_bounds(today, ms_tz)

        # Today's caffeine logs for energy boost component
        caffeine_logs = _rows(conn,
            """SELECT ts, quantity FROM manual_logs
               WHERE type='caffeine' AND ts >= ? AND ts < ?
                 AND quantity IS NOT NULL AND quantity > 0
               ORDER BY ts""",
            (today_utc_start, today_utc_end))

        # Today's activities for exercise adjustment to the energy curve.
        # Queries the normalized activities table (source-agnostic).
        activity_rows = _rows(conn,
            """SELECT start_time, duration_s, avg_hr FROM activities
               WHERE date=? AND duration_s >= 600 AND start_time IS NOT NULL""",
            (today,))
        activity_hr_samples: list[list[float]] = []
        for act in activity_rows:
            try:
                start = datetime.fromisoformat(act["start_time"].replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                start = start.astimezone(timezone.utc) - timedelta(minutes=15)
                end = start + timedelta(seconds=(act["duration_s"] or 0) + 30 * 60)
                rows = conn.execute(
                    """SELECT COALESCE(MAX(CASE WHEN source='garmin' THEN bpm END), MAX(bpm))
                       FROM intraday_hr WHERE ts >= ? AND ts < ? GROUP BY ts ORDER BY ts""",
                    (start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")),
                ).fetchall()
                activity_hr_samples.append([r[0] for r in rows if r[0] is not None])
            except (TypeError, ValueError):
                activity_hr_samples.append([])

        # Today's RPE log — if the user logged it, prefer it over avg_hr as intensity proxy
        rpe_row = conn.execute(
            """SELECT quantity FROM manual_logs
               WHERE type='rpe' AND ts >= ? AND ts < ? AND quantity IS NOT NULL
               ORDER BY ts DESC LIMIT 1""",
            (today_utc_start, today_utc_end),
        ).fetchone()
        today_rpe = float(rpe_row[0]) if rpe_row else None

        # Today's nutrition totals
        today_nutrition_row = conn.execute(
            """SELECT SUM(estimated_calories),
                      GROUP_CONCAT(estimated_macros_json, char(31))
               FROM manual_logs
               WHERE type='meal' AND ts >= ? AND ts < ?""",
            (today_utc_start, today_utc_end),
        ).fetchone()
        today_calories = today_nutrition_row[0] if today_nutrition_row else None
        today_macros = _parse_macros(today_nutrition_row[1]) if today_nutrition_row else {}
        today_meal_count = _scalar(conn,
            "SELECT COUNT(*) FROM manual_logs WHERE type='meal' AND ts >= ? AND ts < ?",
            (today_utc_start, today_utc_end))

        # Today's hydration total — summed from manual_logs (same as nutrition) rather
        # than daily_metrics.hydration_ml, since that column isn't populated until the
        # next normalization run.
        today_hydration = _scalar(conn,
            """SELECT SUM(quantity) FROM manual_logs
               WHERE type='hydration' AND ts >= ? AND ts < ? AND quantity IS NOT NULL""",
            (today_utc_start, today_utc_end))

        # Nutrition targets — no default; only shown once the user sets one via
        # set_nutrition_goal.
        protein_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='protein_g_daily'"
        ).fetchone()
        protein_target = float(protein_target_row[0]) if protein_target_row else None

        calorie_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='calories_daily'"
        ).fetchone()
        calorie_target = float(calorie_target_row[0]) if calorie_target_row else None

        hydration_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='hydration_ml_daily'"
        ).fetchone()
        hydration_target = float(hydration_target_row[0]) if hydration_target_row else None

        # Latest tau calibration is suggestion-only; keep the current 6h model
        # parameter untouched until a user explicitly reviews a suggestion.
        calibration_row = conn.execute(
            "SELECT status, n_labels, best_tau_hours, auc FROM model_calibration_runs "
            "WHERE model='exercise_strain_tau' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        calibration_status = None
        if calibration_row:
            calibration_status = {
                "status": calibration_row[0],
                "n_labels": calibration_row[1],
                "best_tau_hours": calibration_row[2],
                "auc": calibration_row[3],
            }

        # Sleep debt — recent nights' shortfall vs target, for the bedtime recommendation
        sleep_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='sleep_target_hours'"
        ).fetchone()
        sleep_target_hours = float(sleep_target_row[0]) if sleep_target_row else 8.0
        sleep_target_min = sleep_target_hours * 60.0

        recent_sleep_rows = _rows(conn, """
            SELECT date, sleep_duration_min FROM daily_metrics
            WHERE date <= ? AND date > date(?, '-4 days')
              AND sleep_duration_min IS NOT NULL
            ORDER BY date
        """, (metrics_date, metrics_date))

        next_event_row = conn.execute(
            "SELECT date, type, description, goal_description FROM training_events "
            "WHERE status='upcoming' AND date >= ? ORDER BY date LIMIT 1",
            (today,),
        ).fetchone()

    next_event = None
    if next_event_row:
        days_away = (date.fromisoformat(next_event_row[0]) - date.fromisoformat(today)).days
        next_event = {
            "date": next_event_row[0],
            "type": next_event_row[1],
            "description": next_event_row[2],
            "goal": next_event_row[3],
            "days_away": days_away,
        }

    # Sleep debt over the last 3 nights, repaid at up to 1h/night so a large
    # deficit spreads over several nights rather than producing an absurd bedtime.
    sleep_debt_min = _sleep_debt_minutes(recent_sleep_rows, sleep_target_min)
    repay_min = min(sleep_debt_min, 60.0)
    recommended_sleep_hours = (sleep_target_min + repay_min) / 60.0

    # Build caffeine doses list in local hours for the energy curve
    caffeine_doses: list[tuple[float, float]] = []
    for cl in caffeine_logs:
        local_h = _utc_to_local_hour(cl["ts"], ms_tz)
        if local_h is not None:
            try:
                caffeine_doses.append((float(cl["quantity"]), local_h))
            except (TypeError, ValueError):
                pass

    # Exercise boosts: (intensity 0-1, local end hour) per activity today.
    # Intensity = RPE/10 if logged, else (avg_hr - resting_hr) / (190 - resting_hr).
    resting_hr_for_calc = float(today_row[2]) if today_row and today_row[2] else 55.0
    activity_boosts: list[tuple[float, float]] = []
    strain_events: list[tuple[float, float]] = []
    for index, act in enumerate(activity_rows):
        end_h = _activity_end_local_hour(act["start_time"], act["duration_s"] or 0, ms_tz)
        if end_h is None:
            continue
        if today_rpe is not None:
            intensity = min(1.0, today_rpe / 10.0)
        elif act["avg_hr"] and act["avg_hr"] > 0:
            denom = max(1.0, 190.0 - resting_hr_for_calc)
            intensity = max(0.0, min(1.0, (act["avg_hr"] - resting_hr_for_calc) / denom))
        else:
            intensity = 0.4  # moderate default when no HR data
        if intensity >= 0.1:
            activity_boosts.append((intensity, end_h))
        trimp = trimp_from_hr_samples(activity_hr_samples[index], (act["duration_s"] or 0) / 60,
                                      resting_hr_for_calc)
        if trimp is not None and wake_hour is not None:
            strain_events.append((trimp, (end_h - wake_hour) % 24))

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
        "metrics_date": metrics_date,
        "readiness": readiness,
        "illness_risk": illness_risk,
        "today_info": today_info,
        "next_event": next_event,
        "today_calories": today_calories,
        "today_protein": today_macros.get("protein_g"),
        "today_carbs": today_macros.get("carbs_g"),
        "today_fat": today_macros.get("fat_g"),
        "today_meal_count": today_meal_count or 0,
        "today_hydration": today_hydration,
        "protein_target": protein_target,
        "calorie_target": calorie_target,
        "hydration_target": hydration_target,
        "calibration_status": calibration_status,
        "energy_spec": _energy_chart(wake_hour, sleep_score_for_curve, caffeine_doses or None, activity_boosts or None, strain_events or None),
        "wake_hour": wake_hour,
        "bedtime_wake_hour": bedtime_wake_hour,
        "live_curve_json": _live_curve_json(wake_hour, sleep_score_for_curve, caffeine_doses or None, activity_boosts or None, strain_events or None),
        "recommended_sleep_hours": round(recommended_sleep_hours, 2),
        "sleep_debt_min": round(sleep_debt_min),
    })


# ── trends ────────────────────────────────────────────────────────────────────

@router.get("/trends", response_class=HTMLResponse)
async def trends(request: Request, days: str = "30",
                 input: str = "alcohol_units", output: str = "hrv", lag: int = 1,
                 ms_dash_auth: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    clause = _days_clause(days)
    try:
        days_int = max(7, min(3650, int(days)))
    except (TypeError, ValueError):
        days_int = 30
    selected_input = input if input in RELATIONSHIP_INPUTS else "alcohol_units"
    selected_output = output if output in RELATIONSHIP_OUTPUTS else "hrv"
    selected_lag = lag if lag in (0, 1, 2) else 1

    with db() as conn:
        hrv_rows = _rows(conn, """
            SELECT date, hrv,
              AVG(hrv) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS hrv_7d_avg
            FROM daily_metrics
            WHERE date >= date('now', ?) AND hrv IS NOT NULL
            ORDER BY date
        """, (clause,))

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

        all_load_rows_acwr = _rows(conn, """
            SELECT date, training_load_ratio FROM daily_metrics
            WHERE date >= date('now', ?) AND training_load_ratio IS NOT NULL
            ORDER BY date
        """, (clause,))

        zone_time_rows = _rows(conn, """
            SELECT a.date, z.zone, SUM(z.seconds) AS seconds
            FROM activities a
            JOIN activity_hr_zones z ON z.activity_id = a.garmin_activity_id
            WHERE a.date >= date('now', ?)
            GROUP BY a.date, z.zone
        """, (clause,))

        steps_rows = _rows(conn, """
            SELECT date, steps FROM daily_metrics
            WHERE date >= date('now', ?) AND steps IS NOT NULL
            ORDER BY date
        """, (clause,))

        azm_rows = _rows(conn, """
            SELECT date, active_zone_minutes FROM daily_metrics
            WHERE date >= date('now', ?) AND active_zone_minutes IS NOT NULL
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

    x_domain = _x_domain(days)

    acwr_rows = [{"date": r["date"], "training_load_ratio": r["training_load_ratio"]}
                 for r in all_load_rows_acwr]

    # Weekly polarization: bucket per-activity zone-seconds by Monday-start week,
    # collapse zones 1-2/3/4-5 to easy/moderate/hard, convert to %.
    week_zone_seconds: dict[str, dict[int, float]] = {}
    for r in zone_time_rows:
        d = date.fromisoformat(r["date"])
        week = (d - timedelta(days=d.weekday())).isoformat()
        week_zone_seconds.setdefault(week, {}).setdefault(r["zone"], 0.0)
        week_zone_seconds[week][r["zone"]] += r["seconds"] or 0.0
    polarization_rows = []
    for week in sorted(week_zone_seconds):
        zs = week_zone_seconds[week]
        total = sum(zs.values())
        if total <= 0:
            continue
        easy = zs.get(1, 0) + zs.get(2, 0)
        moderate = zs.get(3, 0)
        hard = zs.get(4, 0) + zs.get(5, 0)
        polarization_rows.append({
            "week": week,
            "easy_pct": round(easy / total * 100, 1),
            "moderate_pct": round(moderate / total * 100, 1),
            "hard_pct": round(hard / total * 100, 1),
        })

    fixed_relationships = [
        ("Alcohol → next-day HRV", "alcohol_units", "hrv", 1),
        ("Caffeine → next-day sleep", "caffeine_mg", "sleep_score", 1),
        ("Training load → next-day resting HR", "acute_training_load", "resting_hr", 1),
    ]
    relationship_cards = []
    with db() as conn:
        for title, input_field, output_field, card_lag in fixed_relationships:
            card_rows = _relationship_rows(conn, input_field, output_field, card_lag, days_int)
            spec, stats = _relationship_chart(card_rows, RELATIONSHIP_INPUTS[input_field], RELATIONSHIP_OUTPUTS[output_field])
            relationship_cards.append({"title": title, "spec": spec, "stats": stats})
        selected_rows = _relationship_rows(conn, selected_input, selected_output, selected_lag, days_int)
    selected_spec, selected_stats = _relationship_chart(
        selected_rows, RELATIONSHIP_INPUTS[selected_input], RELATIONSHIP_OUTPUTS[selected_output]
    )

    return templates.TemplateResponse(request, "trends.html", {
        "days": days,
        "relationship_cards": relationship_cards,
        "relationship_spec": selected_spec,
        "relationship_stats": selected_stats,
        "selected_input": selected_input,
        "selected_output": selected_output,
        "selected_lag": selected_lag,
        "relationship_inputs": RELATIONSHIP_INPUTS,
        "relationship_outputs": RELATIONSHIP_OUTPUTS,
        "hrv_spec": _trend_chart(hrv_rows, "hrv", "hrv_7d_avg", "HRV (ms)", x_domain=x_domain),
        "load_spec": _zone_load_chart(load_rows, x_domain=x_domain),
        "polarization_spec": _polarization_chart(polarization_rows, x_domain=x_domain),
        "pmc_spec": _pmc_chart(pmc_rows, x_domain=x_domain),
        "acwr_spec": _sparse_line_chart(
            acwr_rows, "training_load_ratio", "ACWR (ATL/CTL)", color="#f4a261",
            ref_bands=[(0.8, 1.3, "sweet spot")], x_domain=x_domain,
        ),
        "battery_spec": _body_battery_chart(battery_rows, x_domain=x_domain),
        "hr_spec": _hr_chart(hr_rows, x_domain=x_domain),
        "steps_spec": _daily_bar_chart(steps_rows, "steps", "Steps", color="#4e9af1", x_domain=x_domain),
        "azm_spec": _daily_bar_chart(azm_rows, "active_zone_minutes", "Active zone min", color="#5cb85c", x_domain=x_domain),
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
              strftime('%w', date) AS dow,
              COALESCE(alcohol_units, 0) AS alcohol_units,
              COALESCE(caffeine_mg, 0) AS caffeine_mg,
              hrv,
              AVG(hrv) OVER (ORDER BY date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS hrv_7d_avg
            FROM daily_metrics
            WHERE date >= date('now', ?)
            ORDER BY date
        """, (clause,))

    for r in rows:
        if r.get("hrv") is not None and r.get("hrv_7d_avg") is not None:
            r["hrv_delta"] = round(r["hrv"] - r["hrv_7d_avg"], 1)
        else:
            r["hrv_delta"] = None

    x_domain = _x_domain(days)

    hrv_delta_rows = [r for r in rows if r.get("hrv_delta") is not None]

    return templates.TemplateResponse(request, "behavior.html", {
        "days": days,
        "alcohol_spec": _daily_bar_chart(rows, "alcohol_units", "Alcohol (units)", color="#e05c5c", x_domain=x_domain),
        "alcohol_dow_spec": _dow_avg_chart(_compute_dow_avgs(rows, "alcohol_units"), "Avg units", "#e05c5c"),
        "caffeine_spec": _daily_bar_chart(rows, "caffeine_mg", "Caffeine (mg)", color="#7b4fa6", x_domain=x_domain),
        "caffeine_dow_spec": _dow_avg_chart(_compute_dow_avgs(rows, "caffeine_mg"), "Avg mg", "#7b4fa6"),
        "hrv_delta_spec": _diverging_bar_chart(hrv_delta_rows, "hrv_delta", "HRV delta (ms)", x_domain=x_domain),
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
                    ms_dash_auth: str | None = Cookie(default=None),
                    ms_tz: str | None = Cookie(default=None)):
    if not _is_authed(request, ms_dash_auth):
        return _auth_redirect()

    clause = _days_clause(days)
    days_int = int(days) if str(days).isdigit() else 30
    utc_window_start = _local_window_utc_start(days_int, ms_tz)
    local_now = _local_now(ms_tz)
    local_today = local_now.strftime("%Y-%m-%d")
    local_window_start = (local_now - timedelta(days=days_int)).strftime("%Y-%m-%d")

    with db() as conn:
        # Pull calories from daily_metrics (date column is already local health date)
        cal_rows = _rows(conn, """
            SELECT date,
              strftime('%Y-%W', date) AS week,
              strftime('%w', date) AS dow,
              COALESCE(calories_estimated, 0) AS calories
            FROM daily_metrics
            WHERE date >= ?
            ORDER BY date
        """, (local_window_start,))

        # Fetch individual meal rows; group by local date in Python (server is UTC,
        # so SQLite's 'localtime' modifier won't match the browser's timezone).
        _raw_meal_rows = _rows(conn, """
            SELECT ts, estimated_calories, estimated_macros_json
            FROM manual_logs
            WHERE type='meal' AND ts >= ?
            ORDER BY ts
        """, (utc_window_start,))

        # Pull protein target from training_goals if set — no default
        protein_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='protein_g_daily'"
        ).fetchone()
        protein_target = float(protein_target_row[0]) if protein_target_row else None

        calorie_target_row = conn.execute(
            "SELECT value FROM training_goals WHERE metric='calories_daily'"
        ).fetchone()
        calorie_target = float(calorie_target_row[0]) if calorie_target_row else None

        hydration_rows = _rows(conn, """
            SELECT date, hydration_ml FROM daily_metrics
            WHERE date >= ? AND hydration_ml IS NOT NULL
            ORDER BY date
        """, (local_window_start,))

    # Assign each log to its local date in Python using browser tz
    tz = _get_tz(ms_tz)
    _meal_by_local_date: dict[str, dict] = {}
    for row in _raw_meal_rows:
        try:
            dt = datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue
        local_d = dt.strftime("%Y-%m-%d")
        # Convert Python weekday (0=Mon) to SQLite %w convention (0=Sun)
        sqlite_dow = str((dt.weekday() + 1) % 7)
        if local_d not in _meal_by_local_date:
            _meal_by_local_date[local_d] = {
                "date": local_d,
                "dow": sqlite_dow,
                "calories": 0,
                "macros_list": [],
            }
        _meal_by_local_date[local_d]["calories"] += row.get("estimated_calories") or 0
        if row.get("estimated_macros_json"):
            _meal_by_local_date[local_d]["macros_list"].append(row["estimated_macros_json"])
    meal_rows = []
    for v in sorted(_meal_by_local_date.values(), key=lambda x: x["date"]):
        meal_rows.append({**v, "macros_list": chr(31).join(v["macros_list"])})

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

    x_domain = [local_window_start, local_today]

    return templates.TemplateResponse(request, "nutrition.html", {
        "days": days,
        "protein_target": protein_target,
        "calorie_target": calorie_target,
        "calories_spec": _daily_bar_chart(cal_logged_rows, "calories", "Calories (kcal)", color="#f4a261", ref_line=calorie_target, x_domain=x_domain),
        "protein_spec": _daily_bar_chart(protein_rows, "protein_g", "Protein (g)", color="#e05c5c", ref_line=protein_target, x_domain=x_domain),
        "macro_dow_spec": _macro_dow_chart(dow_macro_rows),
        "hydration_spec": _daily_bar_chart(
            hydration_rows, "hydration_ml", "Hydration (mL)", color="#1a6b9e", x_domain=x_domain),
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

        vo2max_rows = _rows(conn, """
            SELECT date, vo2max FROM daily_metrics
            WHERE date >= date('now', ?) AND vo2max IS NOT NULL
            ORDER BY date
        """, (clause,))

        decoupling_rows = _rows(conn, """
            SELECT date, decoupling_pct FROM activities
            WHERE date >= date('now', ?) AND decoupling_pct IS NOT NULL
            ORDER BY date
        """, (clause,))

        # Personalized HR zones are rebuilt daily from that day's max_hr, so
        # they drift over a training block — worth trending, not just a snapshot.
        hr_zone_rows = _rows(conn, """
            SELECT date, zone, min_bpm, max_bpm FROM hr_zones
            WHERE date >= date('now', ?)
            ORDER BY date, zone
        """, (clause,))

        lt_hr_rows = _rows(conn, """
            SELECT date, lactate_threshold_hr FROM daily_metrics
            WHERE date >= date('now', ?) AND lactate_threshold_hr IS NOT NULL
            ORDER BY date
        """, (clause,))

        lt_pace_rows = _rows(conn, """
            SELECT date, lactate_threshold_pace_min_per_km FROM daily_metrics
            WHERE date >= date('now', ?) AND lactate_threshold_pace_min_per_km IS NOT NULL
            ORDER BY date
        """, (clause,))

        ftp_rows = _rows(conn, """
            SELECT date, ftp_watts FROM daily_metrics
            WHERE date >= date('now', ?) AND ftp_watts IS NOT NULL
            ORDER BY date
        """, (clause,))

        training_effect_rows = _rows(conn, """
            SELECT date, training_effect_aerobic, training_effect_anaerobic FROM activities
            WHERE date >= date('now', ?)
              AND (training_effect_aerobic IS NOT NULL OR training_effect_anaerobic IS NOT NULL)
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

    # Efficiency Factor: speed (m/min) per bpm, trended across all runs regardless
    # of pace — complements running-economy's band-filtered view, which only
    # covers near-median-pace days and so throws away most sessions.
    ef_rows = []
    for r in hr_eff_rows:
        speed_m_per_min = (r["distance_km"] * 1000) / r["duration_min"]
        ef_rows.append({**r, "ef": round(speed_m_per_min / r["avg_hr"], 3)})
    window = 4
    for i, r in enumerate(ef_rows):
        window_vals = [p["ef"] for p in ef_rows[max(0, i - window + 1): i + 1]]
        r["ef_4w_avg"] = round(sum(window_vals) / len(window_vals), 3)

    x_domain = _x_domain(days)

    return templates.TemplateResponse(request, "activities.html", {
        "days": days,
        "table_rows": table_rows,
        "count_spec": _activity_bar(count_rows, "count", "Activities"),
        "volume_spec": _weekly_volume_chart(weekly_rows),
        "pace_spec": _pace_trend_chart(pace_rows, x_domain=x_domain),
        "hr_eff_spec": _hr_efficiency_chart(hr_eff_rows),
        "econ_spec": _running_economy_chart(hr_eff_rows),
        "decoupling_spec": _diverging_bar_chart(
            decoupling_rows, "decoupling_pct", "Aerobic decoupling (%)", invert_color=True, x_domain=x_domain
        ),
        "vo2max_spec": _sparse_line_chart(vo2max_rows, "vo2max", "VO2 Max (mL/kg/min)", color="#5cb85c", x_domain=x_domain),
        "hr_zones_spec": _hr_zones_chart(hr_zone_rows, x_domain=x_domain),
        "lt_hr_spec": _sparse_line_chart(lt_hr_rows, "lactate_threshold_hr", "LT heart rate (bpm)", color="#e76f51", x_domain=x_domain),
        "lt_pace_spec": _sparse_line_chart(lt_pace_rows, "lactate_threshold_pace_min_per_km", "LT pace (min/km)", color="#4e9af1", x_domain=x_domain),
        "ftp_spec": _sparse_line_chart(ftp_rows, "ftp_watts", "FTP (W)", color="#f4a261", x_domain=x_domain),
        "ef_spec": _ef_chart(ef_rows, x_domain=x_domain),
        "training_effect_spec": _training_effect_chart(training_effect_rows, x_domain=x_domain),
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
            SELECT date, bp_systolic, bp_diastolic, bp_pulse FROM daily_metrics
            WHERE date >= date('now', ?)
              AND (bp_systolic IS NOT NULL OR bp_diastolic IS NOT NULL)
            ORDER BY date
        """, (clause,))

        breathing_rows = _rows(conn, """
            SELECT date, breathing_rate FROM daily_metrics
            WHERE date >= date('now', ?) AND breathing_rate IS NOT NULL
            ORDER BY date
        """, (clause,))

        skin_temp_rows = _rows(conn, """
            SELECT date, skin_temp_deviation FROM daily_metrics
            WHERE date >= date('now', ?) AND skin_temp_deviation IS NOT NULL
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

        sleep_regularity = sleep_regularity_from_db(conn)

    stage_rows = []
    for r in stage_rows_raw:
        for stage, col in [("Deep", "sleep_deep_min"), ("REM", "sleep_rem_min"), ("Light", "sleep_light_min")]:
            if r.get(col) is not None:
                stage_rows.append({"date": r["date"], "stage": stage, "minutes": r[col]})

    x_domain = _x_domain(days)

    return templates.TemplateResponse(request, "vitals.html", {
        "days": days,
        "sleep_spec": _sleep_chart(score_rows, stage_rows, x_domain=x_domain),
        "sleep_regularity": sleep_regularity,
        "weight_spec": _sparse_line_chart(weight_rows, "weight_kg", "Weight (kg)", color="#f4a261", x_domain=x_domain),
        "spo2_spec": _sparse_line_chart(spo2_rows, "spo2_avg", "SpO2 (%)", color="#7ec8e3", x_domain=x_domain),
        "breathing_spec": _sparse_line_chart(
            breathing_rows, "breathing_rate", "Breathing rate (br/min)", color="#5cb85c", x_domain=x_domain),
        "skin_temp_spec": _diverging_bar_chart(
            skin_temp_rows, "skin_temp_deviation", "Skin temp deviation (°C)", invert_color=True, x_domain=x_domain),
        "bp_spec": _bp_chart(bp_rows, x_domain=x_domain),
    })
