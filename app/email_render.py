"""Render the daily-workout email from structured fields.

The routine model supplies only its own decisions and prose (session, headline, why
bullets, ...). Everything derived from data (readiness, sleep, HRV, illness risk, week
volume) is read from the workout context, and all markup, colors and text contrast are
decided here, so the email can't come out with a stray hex value, a missing fill or
unreadable text. Rendering never raises on missing data: a metric with nothing to show
is simply left out.

Email-client rules baked in: inline styles only (Gmail strips <style>), table layout
for the pill rows, no SVG/images/scripts, and every filled element sets both a `bgcolor`
attribute and `background-color` plus an explicit text color. Text on a fill is always
black or white, whichever contrasts more; with pure black that is >= 4.5:1 for *any*
fill, which is why black wins on the mid-tone status colors (white would fail WCAG AA).
"""
from __future__ import annotations

import html as _html
import re

# Fixed palette for anything that doesn't carry its own color in the data.
GOOD = "#2ecc71"
CAUTION = "#f39c12"
STOP = "#e74c3c"
NEUTRAL = "#bdc3c7"
NO_DATA = "#888888"
QUALITY = "#e67e22"
HEADS_UP_BG = "#fff4d6"

BLACK = "#000000"
WHITE = "#ffffff"

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# session_type -> (emoji, accent color)
SESSION_TYPES = {
    "easy": ("🏃", GOOD),
    "easy_strides": ("🏃", GOOD),
    "long": ("🛣️", GOOD),
    "quality": ("⚡", QUALITY),
    "strength": ("🏋️", GOOD),
    "rest": ("😴", STOP),
}

_WHY_EMOJI = {
    "phase": "🗓️",
    "recovery": "💤",
    "quality": "📈",
    "week": "📈",
    "weather": "🌦️",
}

_READINESS_SQUARES = ((80, "🟩"), (65, "🟦"), (50, "🟨"), (35, "🟧"))


def _luminance(hex_color: str) -> float:
    def chan(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def text_on(fill: str) -> str:
    """Whichever of black/white reads better on `fill` (always >= 4.5:1 with pure black)."""
    return BLACK if contrast_ratio(fill, BLACK) >= contrast_ratio(fill, WHITE) else WHITE


def _color(value, fallback: str = NO_DATA) -> str:
    return value if isinstance(value, str) and _HEX.match(value) else fallback


def _esc(s) -> str:
    return _html.escape(str(s), quote=True)


def _fmt_hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60}h{m % 60:02d}"


# ── status pills ─────────────────────────────────────────────────────────────

def _pills(ctx: dict) -> list[tuple[str, str]]:
    """[(text, fill)] for each status pill that has data, in display order."""
    today = ctx.get("today") or {}
    pills: list[tuple[str, str]] = []

    readiness = today.get("readiness") or {}
    if readiness.get("score") is not None:
        pills.append((f"🔋 Readiness {readiness['score']} · {readiness.get('label') or ''}".rstrip(" ·"),
                      _color(readiness.get("color"))))

    dur = today.get("sleep_duration_min")
    if dur:
        need = today.get("sleep_need_min") or today.get("sleep_need_baseline_min")
        if need:
            ratio = dur / need
            fill = GOOD if ratio >= 0.95 else CAUTION if ratio >= 0.8 else STOP
        else:
            fill = NEUTRAL
        pills.append((f"😴 Sleep {_fmt_hm(dur)}", fill))

    hrv = today.get("hrv")
    if hrv is not None:
        trend = today.get("hrv_trend")
        arrow, word, fill = {
            "improving": ("▲", "rising", GOOD),
            "stable": ("▬", "steady", GOOD),
            "declining": ("▼", "falling", CAUTION),
        }.get(trend, ("", "", NEUTRAL))
        pills.append((f"❤️ HRV {round(hrv)} ms {arrow} {word}".rstrip(), fill))

    illness = today.get("illness_risk") or {}
    if illness.get("level"):
        fallback = {"green": GOOD, "yellow": CAUTION, "red": STOP}.get(illness["level"], NO_DATA)
        pills.append((f"🩺 Illness risk {illness['level']}", _color(illness.get("color"), fallback)))

    return pills


def _pill_cell(text: str, fill: str) -> str:
    return (
        f'<td bgcolor="{fill}" style="background-color:{fill};color:{text_on(fill)};'
        f'padding:6px 12px;border-radius:14px;font-weight:bold;font-size:13px;'
        f'white-space:nowrap">{_esc(text)}</td>'
    )


def _pill_table(pills: list[tuple[str, str]]) -> str:
    # Two per row so four pills wrap cleanly at phone width.
    rows = []
    for i in range(0, len(pills), 2):
        cells = "".join(_pill_cell(t, f) for t, f in pills[i:i + 2])
        rows.append(f"<tr>{cells}</tr>")
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="border-collapse:separate;border-spacing:6px">' + "".join(rows) + "</table>"
    )


# ── week line ────────────────────────────────────────────────────────────────

def _bar(done: float, target: float, slots: int = 6) -> str:
    filled = max(0, min(slots, round(done / target * slots))) if target else 0
    return "▰" * filled + "▱" * (slots - filled)


def _week_line(ctx: dict, trend_squares: str | None) -> str | None:
    wp = ctx.get("week_progress") or {}
    parts = []
    target_km = wp.get("target_km")
    if target_km and wp.get("volume_km") is not None:
        parts.append(f"📈 {_bar(wp['volume_km'], target_km)} {wp['volume_km']:g} / {target_km:g} km")
    elif wp.get("volume_km"):
        parts.append(f"📈 {wp['volume_km']:g} km this week")
    if trend_squares:
        parts.append(trend_squares)
    return " · ".join(parts) or None


def readiness_squares(scores: list[int | None]) -> str:
    """One emoji square per day, oldest first, matching the readiness bands."""
    out = []
    for s in scores:
        if s is None:
            out.append("⬜")
            continue
        out.append(next((e for lo, e in _READINESS_SQUARES if s >= lo), "🟥"))
    return "".join(out)


# ── why bullets ──────────────────────────────────────────────────────────────

def _split_label(item: str) -> tuple[str, str]:
    label, sep, rest = item.partition(": ")
    if sep and len(label) <= 24:
        return label.strip(), rest.strip()
    return "", item.strip()


def _why_items(why: list[str]) -> list[tuple[str, str, str]]:
    out = []
    for item in why:
        label, text = _split_label(item)
        out.append((_WHY_EMOJI.get(label.lower(), "•"), label, text))
    return out


# ── main entry ───────────────────────────────────────────────────────────────

def render_daily_email(
    ctx: dict,
    *,
    session_type: str,
    headline: str = "",
    distance_line: str | None = None,
    targets: str | None = None,
    cue: str | None = None,
    why: list[str] | None = None,
    heads_up: list[str] | None = None,
    calendar_line: str | None = None,
    weather_line: str | None = None,
    trend_squares: str | None = None,
) -> dict:
    emoji, accent = SESSION_TYPES[session_type]
    why = [w for w in (why or []) if w and w.strip()]
    heads_up = [h for h in (heads_up or []) if h and h.strip()]
    pills = _pills(ctx)
    week = _week_line(ctx, trend_squares)

    if session_type == "rest":
        title_html, title_txt = "<b>Take a rest day</b>", "Take a rest day"
    else:
        title_html = f"<b>Target workout:</b> {_esc(headline)}"
        title_txt = f"Target workout: {headline}"

    # ── html ──
    h = ['<div style="max-width:600px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
         'font-size:15px;line-height:1.45">']
    if pills:
        h.append(_pill_table(pills))
    card = [f'<div style="font-size:16px">{emoji} {title_html}</div>']
    if distance_line and session_type != "rest":
        card.append(f'<div style="font-size:22px;font-weight:bold;margin:4px 0">{_esc(distance_line)}</div>')
    if targets and session_type != "rest":
        card.append(f'<div>{_esc(targets)}</div>')
    if cue:
        card.append(f'<div style="margin-top:4px">{_esc(cue)}</div>')
    h.append(f'<div style="border-left:4px solid {accent};padding:8px 12px;margin:12px 0">'
             + "".join(card) + "</div>")
    if week:
        h.append(f'<p style="margin:8px 0">{_esc(week)}</p>')
    if why:
        items = "".join(
            f"<li>{e} <b>{_esc(label)}:</b> {_esc(text)}</li>" if label else f"<li>{_esc(text)}</li>"
            for e, label, text in _why_items(why)
        )
        h.append(f'<p style="margin:12px 0 4px"><b>Why</b></p><ul style="margin:0 0 8px">{items}</ul>')
    if heads_up:
        items = "".join(f"<li>{_esc(x)}</li>" for x in heads_up)
        h.append(
            f'<div bgcolor="{HEADS_UP_BG}" style="background-color:{HEADS_UP_BG};color:{BLACK};'
            f'padding:8px 12px;margin:8px 0;border-radius:6px">'
            f'⚠️ <b>Heads-up</b><ul style="margin:4px 0 0">{items}</ul></div>'
        )
    if calendar_line:
        h.append(f'<p style="margin:8px 0">⌚ {_esc(calendar_line)}</p>')
    if weather_line:
        h.append(f'<p style="margin:8px 0">{_esc(weather_line)}</p>')
    h.append("</div>")

    # ── plain text ──
    t = []
    if pills:
        t.append(" | ".join(p for p, _ in pills))
        t.append("")
    t.append(f"{emoji} {title_txt}")
    if distance_line and session_type != "rest":
        t.append(distance_line)
    if targets and session_type != "rest":
        t.append(targets)
    if cue:
        t.append(cue)
    if week:
        t += ["", week]
    if why:
        t += ["", "Why"]
        t += [f"- {e} {label}: {text}" if label else f"- {text}" for e, label, text in _why_items(why)]
    if heads_up:
        t += ["", "⚠️ Heads-up"] + [f"- {x}" for x in heads_up]
    if calendar_line:
        t += ["", f"⌚ {calendar_line}"]
    if weather_line:
        t += ["", weather_line]

    return {"subject": "Daily Workout", "html": "".join(h), "text": "\n".join(t)}
