"""Compact Win8/Metro daily status card rendered locally with Pillow."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar_image import Image, ImageDraw, _font, _supports_chinese, _save
from .scoring import business_day, fmt_number, fmt_percent, summarize
from .task_colors import task_color, task_label
from .grade_badge import draw_grade

BG = "#1F1F1F"
CARD = "#2D2D30"
TILE = "#333438"
INK = "#F3F3F3"
MUTED = "#B8B8B8"
FAINT = "#85878C"
BLUE = "#2D89EF"
BAR_BG = "#45474D"


def _text(draw, point, value, *, size: int, color=INK, anchor=None):
    draw.text(point, value, fill=color, font=_font(size), anchor=anchor)


def _fit(draw, value: str, size: int, width: int) -> str:
    font = _font(size)
    original = str(value)
    value = original
    while value and draw.textbbox((0, 0), value, font=font)[2] > width:
        value = value[:-1]
    return value + ("..." if value != original else "")


def _safe_stamp(raw: str | None, tz: ZoneInfo) -> str:
    if not raw:
        return ""
    try:
        stamp = datetime.fromisoformat(raw)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=tz)
        return stamp.astimezone(tz).strftime("%H:%M")
    except (ValueError, TypeError, OverflowError):
        return ""


def _status(item: dict, tz: ZoneInfo, chinese: bool) -> str:
    amount = int(item.get("amount", 0))
    target = max(1, int(item.get("target", 1)))
    stamp = _safe_stamp(item.get("last_at"), tz)
    if item.get("complete"):
        text = "\u5df2\u5b8c\u6210" if chinese else "DONE"
        if stamp:
            text += " · " + stamp
        if item.get("penalized"):
            text += " · " + ("\u6298\u5206" if chinese else "LATE")
    elif amount:
        text = (f"\u8fdb\u884c\u4e2d {amount}/{target}" if chinese
                else f"IN PROGRESS {amount}/{target}")
    else:
        text = "\u672a\u5b8c\u6210" if chinese else "NOT DONE"
    if item.get("optional"):
        text += " · " + ("\u53ef\u9009" if chinese else "OPTIONAL")
    elif item.get("time_unknown") and amount:
        text += " · " + ("\u65f6\u95f4\u672a\u77e5" if chinese else "TIME UNKNOWN")
    return text


def _draw_task(draw, box, item: dict, index: int, tz: ZoneInfo, chinese: bool):
    x0, y0, x1, y1 = box
    color = task_color(item.get("name", ""), index)
    draw.rounded_rectangle(box, radius=12, fill=TILE)
    draw.rounded_rectangle((x0, y0, x0 + 7, y1), radius=3, fill=color)
    name = task_label(item.get("name", ""), index, chinese)
    _text(draw, (x0 + 20, y0 + 10), name, size=21, color=INK)
    if item.get("optional"):
        points = "\u53ef\u9009" if chinese else "OPTIONAL"
    else:
        points = f"{fmt_number(item.get('earned', 0))}/{fmt_number(item.get('weight', 0))}"
    _text(draw, (x1 - 16, y0 + 14), points, size=17, color=color, anchor="rt")
    status = _status(item, tz, chinese)
    max_status_width = max(60, x1 - x0 - 42)
    font = _font(15)
    while status and draw.textbbox((0, 0), status, font=font)[2] > max_status_width:
        status = status[:-1]
    _text(draw, (x0 + 20, y0 + 42), status, size=15, color=MUTED)
    bar_x0, bar_y = x0 + 20, y1 - 17
    bar_x1 = x1 - 17
    draw.rounded_rectangle((bar_x0, bar_y, bar_x1, bar_y + 5), radius=2, fill=BAR_BG)
    ratio = float(item.get("score", 0) or 0)
    if item.get("optional"):
        ratio = float(item.get("progress", 0) or 0)
    ratio = min(1.0, max(0.0, ratio))
    if ratio > 0:
        fill_x = bar_x0 + max(3, round((bar_x1 - bar_x0) * ratio))
        draw.rounded_rectangle((bar_x0, bar_y, min(bar_x1, fill_x), bar_y + 5),
                               radius=2, fill=color)


def render_daily_panel(
    out_path: Path,
    items: list[dict],
    nick: str,
    day: date,
    now: datetime,
    tz: ZoneInfo,
    grade_style: str | None = None,
) -> str | None:
    """Render a daily scorecard. Returns None if Pillow is unavailable."""
    if Image is None:
        return None

    items = list(items[:64])
    stats = summarize(items)
    chinese = _supports_chinese()
    width = 1040
    margin = 26
    gap_x = 14
    columns = 2 if len(items) > 1 else 1
    tile_width = ((width - margin * 2 - gap_x) // 2 if columns == 2 else width - margin * 2)
    tile_height = 96
    gap_y = 12
    rows = max(1, math.ceil(len(items) / columns))
    header_height = 224
    footer_height = 74
    height = margin * 2 + header_height + rows * tile_height + max(0, rows - 1) * gap_y + footer_height

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, width - 14, height - 14), radius=20, fill=CARD)

    if chinese:
        title = f"{day:%m-%d} · {str(nick).replace(chr(10), ' ').replace(chr(13), ' ')[:18]} · \u4eca\u65e5\u6253\u5361"
        cutoff_label = f"\u622a\u6b62 {day + timedelta(days=1):%m-%d} 04:00 · {tz.key}"
    else:
        title = f"{day:%Y-%m-%d} · DAILY CHECK-IN"
        cutoff_label = f"RESET {day + timedelta(days=1):%m-%d} 04:00 · {tz.key}"
    title = _fit(draw, title, 25, width - 2 * (margin + 12))
    _text(draw, (margin + 12, 38), title, size=25, color=INK)

    ratio_text = fmt_percent(stats["ratio"])
    _text(draw, (margin + 10, 78), ratio_text, size=58, color=BLUE)
    score_text = f"{fmt_number(stats['earned'])} / {fmt_number(stats['weight'])}"
    _text(draw, (margin + 292, 94), score_text, size=27, color=INK)
    if chinese:
        completed = f"\u5df2\u5b8c\u6210 {stats['required_finished']}/{stats['required_total']} \u9879"
    else:
        completed = f"{stats['required_finished']}/{stats['required_total']} DONE"
    _text(draw, (margin + 294, 132), completed, size=17, color=MUTED)

    bar_x0, bar_y, bar_x1 = margin + 12, 168, width - margin - 12
    draw.rounded_rectangle((bar_x0, bar_y, bar_x1, bar_y + 9), radius=4, fill=BAR_BG)
    ratio = float(stats["ratio"] or 0)
    if ratio > 0:
        draw.rounded_rectangle((bar_x0, bar_y, bar_x0 + max(4, round((bar_x1 - bar_x0) * ratio)), bar_y + 9),
                               radius=4, fill=BLUE)
    _text(draw, (margin + 12, 190), cutoff_label, size=16, color=MUTED)

    start_y = margin + header_height
    if not items:
        label = "\u6682\u65e0\u542f\u7528\u4efb\u52a1" if chinese else "NO ACTIVE TASKS"
        draw.rounded_rectangle((margin, start_y, width - margin, start_y + tile_height), radius=12, fill=TILE)
        _text(draw, (margin + 20, start_y + 30), label, size=20, color=MUTED)
    else:
        for index, item in enumerate(items):
            row, col = divmod(index, columns)
            x = margin + col * (tile_width + gap_x)
            y = start_y + row * (tile_height + gap_y)
            _draw_task(draw, (x, y, x + tile_width, y + tile_height), item, index, tz, chinese)

    unfinished = [item for item in items if not item.get("complete") and not item.get("optional")]
    footer_y = start_y + rows * tile_height + max(0, rows - 1) * gap_y + 13
    if chinese:
        if unfinished:
            names = [task_label(i.get("name", ""), index, chinese)
                     for index, i in enumerate(unfinished[:4])]
            remaining = len(unfinished)
            pending = f"\u5f85\u5b8c\u6210 {remaining} \u9879: " + " / ".join(names)
            if remaining > 4:
                pending += f" +{remaining - 4}"
        else:
            pending = "\u8ba1\u5206\u4efb\u52a1\u5df2\u5168\u90e8\u5b8c\u6210"
        hint = "\u6587\u5b57\u7248 /\u6253\u5361 \u89c6\u56fe \u6587\u5b57 · \u89c4\u5219 /\u6253\u5361 \u8bbe\u7f6e"
    else:
        pending = f"{len(unfinished)} TASKS LEFT" if unfinished else "ALL REQUIRED TASKS DONE"
        hint = "TEXT MODE: use the check-in view command; rules in settings"
    _text(draw, (margin + 12, footer_y), pending, size=16, color=INK)
    _text(draw, (margin + 12, footer_y + 26), hint, size=13, color=FAINT)
    rating = stats["ratio"] if day <= business_day(now.astimezone(tz)) else None
    draw_grade(image, (600, 35, 994, 158), rating, grade_style)
    return _save(image, out_path)
