"""Monthly calendar PNG generation for chat applications.

All images are created locally; no web access, AI or bundled font is needed.
If no Chinese font is installed, only the PNG labels switch to English/numeric.
Chinese habit names are always provided in accompanying text messages.
"""

from __future__ import annotations

import calendar
import hashlib
import uuid
from functools import lru_cache
from datetime import date
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Optional: text-only fallback when Pillow cannot be installed.
    Image = ImageDraw = ImageFont = None


FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
BG = "#F6F8FC"
INK = "#243048"
MUTED = "#71809C"
GREEN = "#49B88B"
AMBER = "#F1B760"
LIGHT = "#E7ECF3"
FUTURE = "#F2F4F8"


@lru_cache(maxsize=16)
def _font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _supports_chinese() -> bool:
    return any(Path(path).is_file() for path in FONT_CANDIDATES[:-1])


def _text(draw, point, value, *, size: int, color=INK, anchor=None):
    draw.text(point, value, fill=color, font=_font(size), anchor=anchor)


def _card(draw, bounds, fill="#FFFFFF", radius=18):
    draw.rounded_rectangle(bounds, radius=radius, fill=fill)


def _save(image, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return str(output)


def calendar_text(year: int, month: int, records: dict[int, int], target: int, today: date) -> str:
    """A real, compact seven-column text calendar (not a stats-only summary)."""
    lines = ["一｜二｜三｜四｜五｜六｜日"]
    for week in calendar.monthcalendar(year, month):
        cells = []
        for num in week:
            if not num:
                cells.append("--·")
            elif date(year, month, num) > today:
                cells.append(f"{num:02d}▫️")
            else:
                amount = int(records.get(num, 0))
                symbol = "✅" if amount >= target else ("🟨" if amount else "⬜")
                cells.append(f"{num:02d}{symbol}")
        lines.append("｜".join(cells))
    lines.append("✅达标  🟨部分  ⬜未打卡  ▫️未来")
    return "\n".join(lines)


def render_single(
    out_path: Path,
    year: int,
    month: int,
    target: int,
    records: dict[int, int],
    today: date,
    habit_name: str,
    member_name: str,
) -> str | None:
    if Image is None:
        return None
    width, height = 900, 740
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    _card(d, (25, 25, width - 25, height - 25))
    title = f"{year} / {month:02d}"
    _text(d, (58, 61), title, size=40)
    if _supports_chinese():
        _text(d, (58, 117), f"{member_name[:16]}  ·  {habit_name}", size=24, color=MUTED)
    else:
        _text(d, (58, 117), "HABIT CHECK-IN CALENDAR", size=21, color=MUTED)
    days_in_month = calendar.monthrange(year, month)[1]
    month_complete = sum(
        records.get(day, 0) >= target and date(year, month, day) <= today
        for day in range(1, days_in_month + 1)
    )
    _card(d, (670, 62, 836, 134), "#E2F5EB", 14)
    _text(d, (753, 81), f"{month_complete} / {days_in_month}", size=26, color="#168568", anchor="mt")
    _text(d, (753, 111), "days done", size=16, color="#478876", anchor="mt")
    x0, y0, step_x, step_y, size = 70, 235, 112, 71, 61
    for col, weekday in enumerate(("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")):
        _text(d, (x0 + col * step_x + size // 2, 193), weekday, size=17, color=MUTED, anchor="mt")
    for row, week in enumerate(calendar.monthcalendar(year, month)):
        for col, num in enumerate(week):
            if not num:
                continue
            x, y = x0 + col * step_x, y0 + row * step_y
            amount = records.get(num, 0)
            is_future = date(year, month, num) > today
            fill = FUTURE if is_future else (GREEN if amount >= target else AMBER if amount else LIGHT)
            _card(d, (x, y, x + size, y + size), fill, radius=15)
            font_color = "#FFFFFF" if amount >= target and not is_future else INK
            _text(d, (x + size // 2, y + 14), str(num), size=24, color=font_color, anchor="mt")
            if amount and target > 1 and not is_future:
                _text(d, (x + size // 2, y + 44), f"{amount}/{target}", size=12, color=font_color, anchor="mt")
    legend_y = 680
    for x, label, color in ((72, "COMPLETED", GREEN), (325, "PARTIAL", AMBER), (528, "MISSING", LIGHT)):
        _card(d, (x, legend_y, x + 22, legend_y + 22), color, radius=6)
        _text(d, (x + 32, legend_y + 2), label, size=16, color=MUTED)
    return _save(img, out_path)


def overview_day_stats(habits: list[dict], records: dict[str, dict[int, int]], day: int) -> tuple[int, int]:
    """Per-day completion counts, each habit counting once at its target."""
    total = len(habits)
    completed = sum(
        int(records.get(h["name"], {}).get(day, 0)) >= int(h["target"])
        for h in habits
    )
    return completed, total


def progress_emoji(done: int, total: int, future: bool = False) -> str:
    """Visual graded daily completion used in image-failure-safe text output."""
    if future:
        return "▫️"
    if total <= 0 or done <= 0:
        return "⬜"
    if done >= total:
        return "🟩"
    ratio = done / total
    return "🟦" if ratio >= .75 else "🟨" if ratio >= .5 else "🟧"


def calendar_overview_text(
    year: int, month: int, habits: list[dict], records: dict[str, dict[int, int]], today: date
) -> str:
    """Always available seven-column month calendar, even if QQ rejects PNGs."""
    lines = ["一｜二｜三｜四｜五｜六｜日"]
    for week in calendar.monthcalendar(year, month):
        cells = []
        for number in week:
            if not number:
                cells.append("--·")
                continue
            future = date(year, month, number) > today
            completed, total = overview_day_stats(habits, records, number)
            cells.append(f"{number:02d}{progress_emoji(completed, total, future)}")
        lines.append("｜".join(cells))
    lines.append("🟩100% 🟦75–99% 🟨50–74% 🟧1–49% ⬜0% ▫️未来")
    if (year, month) == (today.year, today.month):
        done, total = overview_day_stats(habits, records, today.day)
        lines.append(f"今天 {today.day} 日：{done}/{total} 项达标")
    lines.append("单项月历：/打卡 月历 早起")
    return "\n".join(lines)


def render_overview(
    out_path: Path,
    year: int,
    month: int,
    habits: list[dict],
    records: dict[str, dict[int, int]],
    today: date,
    member_name: str,
) -> str | None:
    """Render a real 7-column month calendar; every day is done/total habits."""
    if Image is None:
        return None
    habits = habits[:64]
    weeks = calendar.monthcalendar(year, month)
    width, height = 1050, 365 + len(weeks) * 106
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    _card(draw, (20, 20, width - 20, height - 20), "#FFFFFF", 24)
    _text(draw, (62, 58), f"{year} / {month:02d}", size=42)
    if _supports_chinese():
        _text(draw, (62, 117), f"{member_name[:14]}  ·  全部习惯完成度", size=22, color=MUTED)
        weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    else:
        _text(draw, (62, 117), "MONTHLY HABIT COMPLETION", size=20, color=MUTED)
        weekdays = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

    # Summary uses elapsed days only; future days are not counted as failures.
    valid = [number for number in range(1, calendar.monthrange(year, month)[1] + 1)
             if date(year, month, number) <= today]
    totals = [overview_day_stats(habits, records, n) for n in valid]
    all_done = sum(total > 0 and done == total for done, total in totals)
    average = round(sum(done / total if total else 0 for done, total in totals) * 100 / len(totals)) if totals else 0
    _card(draw, (702, 57, 979, 145), "#E8F6EE", 16)
    _text(draw, (730, 68), f"{average}%", size=34, color="#167A5A")
    _text(draw, (730, 113), f"{all_done} fully-done days", size=17, color="#527F6D")

    x0, y0 = 60, 240
    cell_width, cell_height, x_step, y_step = 121, 88, 141, 106
    for col, dayname in enumerate(weekdays):
        _text(draw, (x0 + col * x_step + cell_width // 2, 197),
              dayname, size=19, color=MUTED, anchor="mt")
    for row, week in enumerate(weeks):
        for col, number in enumerate(week):
            if not number:
                continue
            x, y = x0 + col * x_step, y0 + row * y_step
            future = date(year, month, number) > today
            completed, total = overview_day_stats(habits, records, number)
            ratio = completed / total if total else 0
            if future:
                fill, stroke = "#F4F6FA", "#E1E7EF"
            elif total > 0 and completed == total:
                fill, stroke = "#D8F2E4", "#A4D9B8"
            elif completed > 0:
                fill, stroke = ("#FFF2CE", "#F2D687") if ratio >= .5 else ("#FFEAD8", "#F2C6A1")
            else:
                fill, stroke = "#EEF2F6", "#DDE3EC"
            draw.rounded_rectangle((x, y, x + cell_width, y + cell_height), radius=15,
                                   fill=fill, outline=stroke, width=2)
            _text(draw, (x + 13, y + 9), str(number), size=24, color=INK)
            if not future:
                _text(draw, (x + cell_width - 12, y + 17), f"{completed}/{total}",
                      size=20, color="#1F6D51" if ratio == 1 and total else INK, anchor="rt")
                bar_x0, bar_y0, bar_w = x + 12, y + 65, cell_width - 24
                draw.rounded_rectangle((bar_x0, bar_y0, bar_x0 + bar_w, bar_y0 + 7),
                                       radius=3, fill="#D6DCE6")
                if ratio:
                    draw.rounded_rectangle((bar_x0, bar_y0, bar_x0 + max(5, round(bar_w * ratio)), bar_y0 + 7),
                                           radius=3, fill=GREEN if ratio == 1 else AMBER)
            else:
                _text(draw, (x + cell_width - 12, y + 19), "–", size=21, color=MUTED, anchor="rt")
    footer_y = 270 + len(weeks) * y_step
    legend = (("ALL DONE", "#D8F2E4"), ("PARTIAL", "#FFF2CE"),
              ("NOT DONE", "#EEF2F6"), ("FUTURE", "#F4F6FA"))
    for i, (label, fill) in enumerate(legend):
        x = 70 + i * 241
        _card(draw, (x, footer_y, x + 22, footer_y + 22), fill, 5)
        _text(draw, (x + 31, footer_y + 3), label, size=15, color=MUTED)
    return _save(image, out_path)


def cache_file(cache_dir: Path, *parts) -> Path:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:20]
    return cache_dir / f"calendar-{digest}-{uuid.uuid4().hex[:8]}.png"
