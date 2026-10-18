"""Seven-column weighted calendars with Metro task markers and text fallback."""
from __future__ import annotations

import calendar
import math
from datetime import date
from pathlib import Path

from .calendar_image import Image, ImageDraw, _font, _supports_chinese, _save
from .scoring import fmt_number, fmt_percent
from .task_colors import task_color, task_label

BG = "#1F1F1F"
CARD = "#2D2D30"
CELL = "#303236"
INK = "#F3F3F3"
MUTED = "#B8B8B8"
FAINT = "#85878C"
BLUE = "#2D89EF"
BAR_BG = "#45474D"


def color_for(ratio):
    """Return neutral Metro colors for the score bar, not task identity."""
    if ratio is None:
        return BAR_BG, FAINT, "#4A4C52"
    if ratio >= 1 - 1e-10:
        return "#1D553C", "#69D69C", "#2D7B58"
    if ratio >= .75:
        return "#173B61", "#72B8FF", "#2D89EF"
    if ratio >= .5:
        return "#55451A", "#FFD15C", "#B88B21"
    if ratio > 0:
        return "#583222", "#FF9A62", "#B75C2B"
    return CELL, MUTED, BAR_BG


def emoji_for(ratio):
    if ratio is None:
        return "\u00b7"
    if ratio >= 1 - 1e-10:
        return "\U0001f7e9"
    if ratio >= .75:
        return "\U0001f7e6"
    if ratio >= .5:
        return "\U0001f7e8"
    if ratio > 0:
        return "\U0001f7e7"
    return "\u2b1c"


def weighted_text(year, month, metrics, stats, today, chosen=None):
    title = chosen or "\u5168\u90e8\u4efb\u52a1"
    lines = [f"{year}-{month:02d} · {title}",
             f"{fmt_percent(stats['ratio'])} · {fmt_number(stats['earned'])}/{fmt_number(stats['weight'])} \u5206",
             "\u4e00\uff5c\u4e8c\uff5c\u4e09\uff5c\u56db\uff5c\u4e94\uff5c\u516d\uff5c\u65e5"]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        cells = []
        for n in week:
            if not n:
                cells.append("--\u00b7")
            else:
                metric = metrics.get(n)
                ratio = metric.get("display_ratio", metric.get("ratio")) if metric else None
                cells.append(f"{n:02d}{emoji_for(ratio)}")
        lines.append("\uff5c".join(cells))
    lines.append("\U0001f7e9100% \U0001f7e675+% \U0001f7e850+% \U0001f7e7>0% \u2b1c0% \u00b7 \u4e0d\u8ba1\u5206")
    if today.year == year and today.month == month and today.day in metrics:
        d = metrics[today.day]
        lines.append(f"\u4eca\u5929 {today.day} \u65e5: {fmt_percent(d.get('display_ratio', d['ratio']))} · "
                     f"{fmt_number(d['earned'])}/{fmt_number(d['weight'])} \u5206")
    lines.append("04:00 \u6362\u65e5 · \u672a\u6765\u65e5\u4e0d\u8ba1\u5206\u6bcd")
    return "\n".join(lines)


def _text(draw, point, value, size=22, color=INK, anchor=None):
    draw.text(point, str(value), font=_font(size), fill=color, anchor=anchor)


def _fit(draw, value, size, limit):
    value = str(value)
    font = _font(size)
    while value and draw.textbbox((0, 0), value, font=font)[2] > limit:
        value = value[:-1]
    return value


def _task_names(metrics):
    names = []
    for day in sorted(metrics):
        for item in metrics[day].get("items", []):
            name = str(item.get("name", ""))
            if name and name not in names:
                names.append(name)
    return names


def _draw_task_marker(draw, box, item, name, index, future):
    x0, y0, x1, y1 = box
    color = task_color(name, index)
    if future or item is None:
        draw.rounded_rectangle(box, radius=2, fill="#36383D", outline="#4B4E55", width=1)
        return
    amount = max(0, int(item.get("amount", 0)))
    target = max(1, int(item.get("target", 1)))
    if amount >= target:
        draw.rounded_rectangle(box, radius=2, fill=color)
    elif amount > 0:
        draw.rounded_rectangle(box, radius=2, fill="#3B3D42", outline=color, width=1)
        width = max(1, round((x1 - x0) * min(1.0, amount / target)))
        draw.rectangle((x0, y0 + 1, min(x1 - 1, x0 + width), y1 - 1), fill=color)
    else:
        draw.rounded_rectangle(box, radius=2, fill="#3B3D42", outline="#55585E", width=1)


def render_weighted(out_path: Path, year: int, month: int, metrics: dict,
                    stats: dict, today: date, member_name: str, chosen=None):
    """Render the month score and a fixed-color status marker for each task/day."""
    if Image is None:
        return None
    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    cn = _supports_chinese()
    names = _task_names(metrics)
    marker_cols = min(8, max(1, len(names)))
    marker_size = 10 if len(names) <= 8 else 7
    marker_gap = 3 if len(names) <= 8 else 2
    marker_rows = max(1, math.ceil(len(names) / marker_cols))
    cell_height = max(110, 50 + marker_rows * (marker_size + marker_gap) + 24)
    cell_width, x_step, y_step = 128, 141, cell_height + 14
    width, x0, y0 = 1080, 52, 302

    task_legend_count = min(14, len(names))
    task_legend_rows = math.ceil(task_legend_count / 7) if task_legend_count else 0
    footer_y = y0 + (len(weeks) - 1) * y_step + cell_height + 12
    status_y = footer_y + max(1, task_legend_rows) * 27 + (15 if len(names) > 14 else 0)
    height = status_y + 61

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, width - 14, height - 14), radius=20, fill=CARD)

    _text(draw, (56, 43), f"{year} / {month:02d}", 42)
    subject = chosen or ("\u5168\u90e8\u4efb\u52a1" if cn else "ALL TASKS")
    subtitle = (f"{member_name[:14]} · {subject}" if cn else "MONTHLY HABIT SCORE")
    _text(draw, (56, 105), _fit(draw, subtitle, 22, 610), 22, MUTED)
    _text(draw, (1020, 57), "04:00 \u6362\u65e5" if cn else "04:00 RESET", 17, MUTED, "rt")

    summary_cards = [
        (56, "\u672c\u6708\u5b8c\u6210\u5ea6" if cn else "MONTH SCORE", fmt_percent(stats["ratio"])),
        (386, "\u5f97\u5206 / \u53ef\u5f97\u5206" if cn else "POINTS / POSSIBLE",
         f"{fmt_number(stats['earned'])} / {fmt_number(stats['weight'])}"),
        (716, "\u6ee1\u5206\u65e5 / \u8ba1\u5206\u65e5" if cn else "FULL / TRACKED DAYS",
         f"{stats['full_days']} / {stats['days']}")]
    for x, label, value in summary_cards:
        draw.rounded_rectangle((x, 154, x + 308, 244), radius=12, fill="#34363B")
        _text(draw, (x + 18, 166), label, 15, MUTED)
        size = 27
        while size > 15 and draw.textbbox((0, 0), value, font=_font(size))[2] > 270:
            size -= 1
        _text(draw, (x + 18, 198), value, size, BLUE if x == 56 else INK)

    weekdays = ("\u5468\u4e00", "\u5468\u4e8c", "\u5468\u4e09", "\u5468\u56db", "\u5468\u4e94", "\u5468\u516d", "\u5468\u65e5") if cn else ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
    for col, weekday in enumerate(weekdays):
        _text(draw, (x0 + col * x_step + cell_width // 2, 269), weekday, 16, MUTED, "mt")

    for row, week in enumerate(weeks):
        for col, number in enumerate(week):
            if not number:
                continue
            x, y = x0 + col * x_step, y0 + row * y_step
            future = date(year, month, number) > today
            metric = metrics.get(number)
            ratio = metric.get("display_ratio", metric.get("ratio")) if metric else None
            fill, score_color, stroke = color_for(ratio)
            is_today = date(year, month, number) == today
            draw.rounded_rectangle((x, y, x + cell_width, y + cell_height), radius=10,
                                   fill=CELL, outline=BLUE if is_today else "#44474D",
                                   width=2 if is_today else 1)
            _text(draw, (x + 9, y + 5), str(number), 18, BLUE if is_today else INK)
            _text(draw, (x + cell_width - 8, y + 6), "--" if future else fmt_percent(ratio),
                  16, score_color if not future else FAINT, "rt")

            detail = "--"
            if metric and not future:
                if chosen and metric.get("items"):
                    item = metric["items"][0]
                    detail = (f"{fmt_number(item['earned'])}/{fmt_number(item['weight'])}"
                              if item["weight"] else f"{item['amount']}/{item['target']}")
                elif metric["weight"]:
                    detail = f"{fmt_number(metric['earned'])}/{fmt_number(metric['weight'])}"
            _text(draw, (x + 9, y + 29), detail, 12, MUTED)

            by_name = {str(item.get("name", "")): item for item in metric.get("items", [])} if metric else {}
            total_marker_width = marker_cols * marker_size + max(0, marker_cols - 1) * marker_gap
            marker_x0 = x + (cell_width - total_marker_width) // 2
            marker_y0 = y + 51
            for index, name in enumerate(names):
                marker_row, marker_col = divmod(index, marker_cols)
                mx = marker_x0 + marker_col * (marker_size + marker_gap)
                my = marker_y0 + marker_row * (marker_size + marker_gap)
                _draw_task_marker(draw, (mx, my, mx + marker_size, my + marker_size),
                                 by_name.get(name), name, index, future)

            bar_y = y + cell_height - 10
            bar_x0, bar_x1 = x + 10, x + cell_width - 10
            draw.rounded_rectangle((bar_x0, bar_y, bar_x1, bar_y + 3), radius=1, fill=BAR_BG)
            if ratio is not None and not future and ratio > 0:
                draw.rounded_rectangle((bar_x0, bar_y,
                                        min(bar_x1, bar_x0 + max(2, round((bar_x1 - bar_x0) * ratio))),
                                        bar_y + 3), radius=1, fill=BLUE)

    if task_legend_count:
        for index, name in enumerate(names[:14]):
            row, col = divmod(index, 7)
            lx = 56 + col * 143
            ly = footer_y + row * 27
            draw.rounded_rectangle((lx, ly + 2, lx + 12, ly + 14), radius=2,
                                   fill=task_color(name, index))
            label = task_label(name, index, cn)
            _text(draw, (lx + 19, ly), _fit(draw, label, 13, 118), 13, MUTED)
    else:
        _text(draw, (56, footer_y + 2), "\u6682\u65e0\u4efb\u52a1" if cn else "NO TASKS", 13, MUTED)

    if len(names) > 14:
        more = f"+{len(names) - 14} " + ("\u9879\u4efb\u52a1\uff0c\u987a\u5e8f\u89c1\u6e05\u5355" if cn else "MORE TASKS; ORDER IN TASK LIST")
        _text(draw, (56, status_y - 12), _fit(draw, more, 13, 960), 13, FAINT)

    status_entries = [
        ("\u5b9e\u5fc3=\u5b8c\u6210" if cn else "SOLID = DONE", 0),
        ("\u534a\u586b=\u90e8\u5206" if cn else "PARTIAL FILL", 1),
        ("\u7a7a\u6846=\u672a\u5b8c\u6210" if cn else "EMPTY = NOT DONE", 2),
        ("\u6de1\u6846=\u672a\u542f\u7528/\u672a\u6765" if cn else "FAINT = INACTIVE/FUTURE", 3),
    ]
    for index, (label, state) in enumerate(status_entries):
        sx = 56 + index * 241
        sy = status_y + 5
        sample = (sx, sy, sx + 13, sy + 13)
        if state == 0:
            draw.rounded_rectangle(sample, radius=2, fill="#B8B8B8")
        elif state == 1:
            draw.rounded_rectangle(sample, radius=2, fill="#3B3D42", outline="#8E44AD", width=1)
            draw.rectangle((sx + 1, sy + 2, sx + 6, sy + 11), fill="#8E44AD")
        elif state == 2:
            draw.rounded_rectangle(sample, radius=2, fill="#3B3D42", outline="#55585E", width=1)
        else:
            draw.rounded_rectangle(sample, radius=2, fill="#36383D", outline="#4B4E55", width=1)
        _text(draw, (sx + 20, sy - 1), label, 13, MUTED)
    note = "\u8272\u5757\u987a\u5e8f=\u4efb\u52a1\u5217\u8868" if cn else "MARKER ORDER = TASK LIST"
    _text(draw, (width - 56, status_y + 28), note, 11, FAINT, "rt")
    return _save(image, out_path)
