"""Seven-column weighted-score calendars. Uses the installed font, not a bundle."""
from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

from .calendar_image import Image, ImageDraw, _font, _supports_chinese, _save
from .scoring import fmt_number, fmt_percent


def color_for(ratio):
    if ratio is None:
        return "#F4F6FA", "#B0B8C5", "#E4E8EF"
    if ratio >= 1 - 1e-10:
        return "#E0F5EC", "#138261", "#C1E5D4"
    if ratio >= .75:
        return "#E5EEFC", "#336FCC", "#CADBF2"
    if ratio >= .5:
        return "#FFF3D7", "#AD770D", "#F0DEB3"
    if ratio > 0:
        return "#FFF0E6", "#BA6635", "#F0D7C5"
    return "#EEF1F6", "#7D899C", "#DFE5EE"


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
    lines = [f"\U0001f5d3\ufe0f {year}-{month:02d} \u00b7 {title}",
             f"\u52a0\u6743\u5b8c\u6210\u5ea6 {fmt_percent(stats['ratio'])} \uff5c "
             f"{fmt_number(stats['earned'])}/{fmt_number(stats['weight'])} \u5206",
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
    lines.append("\U0001f7e9100% \U0001f7e675\uff5e99% \U0001f7e850\uff5e74% \U0001f7e70\uff5e49% \u2b1c0% \u00b7\u4e0d\u8ba1\u5206")
    if today.year == year and today.month == month and today.day in metrics:
        d = metrics[today.day]
        lines.append(f"\u4eca\u5929 {today.day} \u65e5\uff1a{fmt_percent(d.get('display_ratio', d['ratio']))}"
                     f" \uff5c {fmt_number(d['earned'])}/{fmt_number(d['weight'])} \u5206")
    lines.append("04:00 \u6362\u65e5\uff1b\u672a\u542f\u7528\u65e5\u4e0e\u672a\u6765\u65e5\u4e0d\u8ba1\u5165\u3002")
    return "\n".join(lines)


def render_weighted(out_path: Path, year: int, month: int, metrics: dict,
                    stats: dict, today: date, member_name: str, chosen=None):
    if Image is None:
        return None
    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    width, height = 1080, 400 + 122 * len(weeks)
    image = Image.new("RGB", (width, height), "#F3F5F9")
    d = ImageDraw.Draw(image)
    cn = _supports_chinese()
    def text(x, y, value, size=22, color="#25324A", anchor=None):
        d.text((x, y), value, font=_font(size), fill=color, anchor=anchor)
    def fit(value, size, limit):
        while value and d.textbbox((0, 0), value, font=_font(size))[2] > limit:
            value = value[:-1]
        return value
    def card(box, color="#FFFFFF", radius=18, outline=None, line=1):
        d.rounded_rectangle(box, radius=radius, fill=color, outline=outline, width=line)
    card((20, 20, width-20, height-20), radius=26)
    text(56, 48, f"{year} / {month:02d}", 42)
    subject = chosen or ("\u5168\u90e8\u4efb\u52a1" if cn else "ALL TASKS")
    subtitle = f"{member_name} \u00b7 {subject}" if cn else "WEIGHTED HABIT COMPLETION"
    text(56, 112, fit(subtitle, 23, 730), 23, "#6B7891")
    text(1020, 74, "04:00 \u6362\u65e5" if cn else "04:00 RESET", 19, "#6B7891", "rt")
    summary_cards = [
        (56, "\u672c\u6708\u5b8c\u6210\u5ea6" if cn else "MONTH SCORE", fmt_percent(stats["ratio"])),
        (386, "\u5f97\u5206 / \u53ef\u5f97\u5206" if cn else "POINTS / POSSIBLE", f"{fmt_number(stats['earned'])} / {fmt_number(stats['weight'])}"),
        (716, "\u6ee1\u5206\u65e5 / \u8ba1\u5206\u65e5" if cn else "PERFECT / TRACKED DAYS", f"{stats['full_days']} / {stats['days']}")]
    for x, label, value in summary_cards:
        card((x, 161, x+308, 253), "#F2F6FC", 16)
        text(x+20, 176, label, 17, "#6B7891")
        size = 28
        while size > 15 and d.textbbox((0, 0), value, font=_font(size))[2] > 268:
            size -= 1
        text(x+20, 206, value, size, "#254B7B")
    weekdays = ("\u5468\u4e00", "\u5468\u4e8c", "\u5468\u4e09", "\u5468\u56db", "\u5468\u4e94", "\u5468\u516d", "\u5468\u65e5") if cn else ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
    cellw, cellh, xstep, ystep = 128, 106, 141, 122
    x0, y0 = 52, 307
    for col, weekday in enumerate(weekdays):
        text(x0+col*xstep+64, 275, weekday, 18, "#6B7891", "mt")
    for row, week in enumerate(weeks):
        for col, n in enumerate(week):
            if not n:
                continue
            x, y = x0+col*xstep, y0+row*ystep
            m = metrics.get(n)
            ratio = m.get("display_ratio", m.get("ratio")) if m else None
            bg, ink, stroke = color_for(ratio)
            istoday = date(year, month, n) == today
            card((x, y, x+cellw, y+cellh), bg, 14, "#386CCA" if istoday else stroke, 3 if istoday else 1)
            text(x+12, y+7, str(n), 20, "#25324A")
            if istoday:
                text(x+116, y+10, "\u4eca" if cn else "NOW", 13, "#386CCA", "rt")
            text(x+12, y+34, fmt_percent(ratio), 24, ink)
            detail = f"{fmt_number(m['earned'])}/{fmt_number(m['weight'])}" if m and m["weight"] else "--"
            if m and not m["weight"] and chosen and m["items"]:
                i = m["items"][0]
                detail = f"{i['amount']}/{i['target']}" + (" \u53ef\u9009" if cn else " opt")
            text(x+12, y+72, detail, 15, ink)
            d.rounded_rectangle((x+12, y+96, x+116, y+99), radius=1, fill=stroke)
            if ratio is not None and ratio > 0:
                d.rounded_rectangle((x+12, y+96, x+12+104*ratio, y+99), radius=1, fill=ink)
    footer = y0 + len(weeks)*ystep + 7
    note = ("\u683c\u5185\uff1a\u52a0\u6743\u5b8c\u6210\u5ea6 \u00b7 \u5f97\u5206/\u53ef\u5f97\u5206\uff08\u5df2\u8ba1\u5165\u8fdf\u5230\u6263\u5206\uff09" if cn
            else "Each day: weighted completion and earned / possible points, including lateness.")
    text(56, footer, note, 17, "#6B7891")
    text(56, footer+27, "\u672a\u542f\u7528\u3001\u672a\u6765\u3001\u5168\u90e8\u96f6\u6743\u91cd\u65e5\u4e0d\u8ba1\u5165\u6708\u5ea6\u5206\u6bcd\u3002" if cn
         else "Inactive, future and zero-weight days are excluded from the monthly denominator.", 16, "#8A95A8")
    return _save(image, out_path)
