"""Tests for the daily-email renderer and the render_daily_email MCP tool."""

import re
import sqlite3

import pytest

import app.db as db_module
from app.db import init_db
from app.email_render import (
    BLACK, CAUTION, GOOD, HEADS_UP_BG, NEUTRAL, NO_DATA, QUALITY, SESSION_TYPES, STOP, WHITE,
    contrast_ratio, readiness_squares, render_daily_email, text_on,
)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    init_db(path)
    yield path


# ── contrast ─────────────────────────────────────────────────────────────────

PALETTE = [GOOD, CAUTION, STOP, NEUTRAL, NO_DATA, QUALITY, "#3498db", "#e67e22", "#ffffff", "#000000"]


@pytest.mark.parametrize("fill", PALETTE)
def test_text_on_palette_meets_aa(fill):
    assert contrast_ratio(fill, text_on(fill)) >= 4.5


def test_text_on_meets_aa_for_any_fill():
    # Pure black as the dark option guarantees >= 4.5:1 for every sRGB color.
    steps = range(0, 256, 15)
    for r in steps:
        for g in steps:
            for b in steps:
                fill = f"#{r:02x}{g:02x}{b:02x}"
                assert contrast_ratio(fill, text_on(fill)) >= 4.5, fill


def test_mid_tone_status_fills_get_dark_text():
    for fill in (GOOD, CAUTION, "#3498db", NO_DATA):
        assert text_on(fill) == BLACK, fill


# ── rendering ────────────────────────────────────────────────────────────────

CTX = {
    "today": {
        "readiness": {"score": 78, "label": "Ready", "color": "#3498db"},
        "sleep_duration_min": 460, "sleep_need_min": 480,
        "hrv": 62.4, "hrv_trend": "improving",
        "illness_risk": {"level": "green", "color": "#2ecc71"},
    },
    "week_progress": {"volume_km": 24.0, "target_km": 40.0},
}


def _render(ctx=CTX, **kw):
    kw.setdefault("session_type", "easy")
    kw.setdefault("headline", "10 km easy")
    return render_daily_email(ctx, **kw)


def _fills(html):
    """(bgcolor attr, style background-color, style color) for every filled element."""
    out = []
    for tag in re.findall(r"<(?:td|div)[^>]*>", html):
        bg = re.search(r'bgcolor="(#[0-9a-fA-F]{6})"', tag)
        if not bg:
            continue
        style_bg = re.search(r"background-color:(#[0-9a-fA-F]{6})", tag)
        fg = re.search(r"(?<![-\w])color:(#[0-9a-fA-F]{6})", tag)
        out.append((bg.group(1), style_bg and style_bg.group(1), fg and fg.group(1)))
    return out


def test_every_fill_has_bgcolor_style_and_readable_text():
    r = _render(heads_up=["x"])
    fills = _fills(r["html"])
    assert len(fills) >= 5  # 4 pills + heads-up block
    for bg, style_bg, fg in fills:
        assert style_bg == bg, "bgcolor attr and style background must agree"
        assert fg is not None, "every filled element sets an explicit text color"
        assert contrast_ratio(bg, fg) >= 4.5, (bg, fg)
    assert not any(fg.lower() == WHITE for _, _, fg in fills)  # the white-on-light bug
    assert HEADS_UP_BG in r["html"]


def test_email_safe_markup():
    html = _render(heads_up=["x"], why=["Phase: build"])["html"]
    for banned in ("<style", "<svg", "<img", "<script", "display:flex", "display:grid"):
        assert banned not in html


def test_renders_expected_sections():
    r = _render(
        distance_line="10 km · ~60 min", targets="Z2 130-145", cue="Walk if HR drifts",
        why=["Phase: build, 8 weeks out", "Recovery: sleep fine"], heads_up=["Zones estimated"],
        calendar_line="Nothing pushed to the watch", weather_line="🌤️ 12°C",
        trend_squares="🟩🟦🟨",
    )
    html, text = r["html"], r["text"]
    assert r["subject"] == "Daily Workout"
    for needle in ("Readiness 78", "Sleep 7h40", "HRV 62 ms", "Illness risk green",
                   "Target workout:", "10 km · ~60 min", "Heads-up", "🌤️ 12°C", "▰▰▰▰▱▱ 24 / 40 km"):
        assert needle in html, needle
        assert needle in text, needle
    assert "<b>Phase:</b>" in html and "🗓️" in html
    assert "<" not in text


def test_rest_day_headline():
    r = _render(session_type="rest", distance_line="ignored", targets="ignored")
    assert "Take a rest day" in r["html"] and "Take a rest day" in r["text"]
    assert "Target workout" not in r["html"] and "ignored" not in r["html"]


def test_accent_follows_session_type():
    assert f"border-left:4px solid {QUALITY}" in _render(session_type="quality")["html"]
    assert f"border-left:4px solid {STOP}" in _render(session_type="rest")["html"]
    assert set(SESSION_TYPES) == {"easy", "easy_strides", "long", "quality", "strength", "rest"}


def test_model_text_is_escaped():
    evil = '<script>alert("x")</script> & \'q\''
    r = _render(
        headline=evil, distance_line=evil, targets=evil, cue=evil, why=[f"Phase: {evil}", evil],
        heads_up=[evil], calendar_line=evil, weather_line=evil,
    )
    assert "<script>" not in r["html"]
    assert "&lt;script&gt;" in r["html"] and "&amp;" in r["html"] and "&quot;" in r["html"]


def test_bad_color_from_data_falls_back():
    ctx = {"today": {"readiness": {"score": 60, "label": "Moderate", "color": "red; x:y"}}}
    html = _render(ctx)["html"]
    assert "red; x:y" not in html
    assert f'bgcolor="{NO_DATA}"' in html


@pytest.mark.parametrize("ctx", [{}, {"today": {}}, {"today": None, "week_progress": None}])
def test_no_data_renders_valid_email(ctx):
    r = _render(ctx, session_type="easy")
    assert r["html"].startswith("<div") and r["html"].endswith("</div>")
    assert "Target workout:" in r["text"]
    assert "Readiness" not in r["html"] and "Sleep" not in r["html"]
    assert "bgcolor" not in r["html"]  # no pills, no heads-up


def test_readiness_squares():
    assert readiness_squares([90, 70, 55, 40, 10, None]) == "🟩🟦🟨🟧🟥⬜"


# ── the MCP tool ─────────────────────────────────────────────────────────────

def test_tool_reads_data_from_db_and_renders():
    from app.mcp_server import render_daily_email as tool

    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        "INSERT INTO daily_metrics(date, hrv, resting_hr, sleep_score, sleep_duration_min, sleep_need_min) "
        "VALUES ('2026-11-09', 60, 50, 80, 450, 480)"
    )
    conn.commit()
    conn.close()
    r = tool(date="2026-11-09", session_type="easy", headline="8 km easy", why=["Phase: off-season"])
    assert "error" not in r
    assert "Sleep 7h30" in r["html"] and "HRV 60 ms" in r["html"]
    assert "8 km easy" in r["text"]


def test_tool_with_empty_database_still_renders():
    from app.mcp_server import render_daily_email as tool
    r = tool(date="2026-11-09", session_type="rest")
    assert "error" not in r
    assert r["html"] and r["text"] and "Take a rest day" in r["text"]


def test_tool_rejects_bad_inputs_without_raising():
    from app.mcp_server import render_daily_email as tool
    assert "error" in tool(date="tomorrow", session_type="easy")
    assert "error" in tool(date="", session_type="easy")
    r = tool(date="2026-11-09", session_type="sprint")
    assert "error" in r and "easy" in r["available"]


def test_tool_registered_and_date_required():
    from app.mcp_server import mcp
    tool = mcp._tool_manager._tools["render_daily_email"]
    assert "date" in tool.parameters["required"]
