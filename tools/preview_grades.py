"""Generate reproducible grade prototypes; all records are synthetic.

Run: python tools/preview_grades.py --output previews/grades
No AstrBot installation, network, database, or live account is used.
"""
from __future__ import annotations

import argparse
import base64
import importlib
import sys
import types
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
# Load leaf modules without executing the AstrBot plugin's __init__.
PACKAGE = "_grade_preview_plugin"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package

daily = importlib.import_module(f"{PACKAGE}.daily_image")
monthly = importlib.import_module(f"{PACKAGE}.weighted_calendar")
scoring = importlib.import_module(f"{PACKAGE}.scoring")
art = importlib.import_module(f"{PACKAGE}.grade_badge")
calendar_image = importlib.import_module(f"{PACKAGE}.calendar_image")

NAMES = ("睡觉", "起床", "早餐", "午餐", "晚餐", "运动", "规划待办", "完成待办")
STYLE_LABELS = {"arcade": "01 街机大字", "metal": "02 金属徽记", "neon": "03 霓虹描边"}
TZ = ZoneInfo("Asia/Shanghai")
TODAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 22, tzinfo=TZ)


def sample_items(ratio=1., count=8):
    return [dict(name=NAMES[i] if i < 8 else f"额外任务{i + 1}", weight=1.,
                 earned=ratio, score=ratio, progress=1. if ratio else 0.,
                 amount=1 if ratio else 0, target=1, complete=ratio > 0,
                 optional=False, penalized=0 < ratio < 1,
                 last_at="2026-10-28T21:30:00+08:00") for i in range(count)]


def sample_month():
    ratios = (0., .15, .25, .49, .50, .69, .70, .849, .85, .949, .95, .99999, 1., 1.)
    metrics = {}
    for n in range(15, 29):
        stats = scoring.summarize(sample_items(ratios[n - 15]))
        stats["display_ratio"] = stats["ratio"]
        metrics[n] = stats
    return metrics, scoring.month_summary(metrics)


def render_brush_previews(out):
    out.mkdir(parents=True, exist_ok=True)
    metrics, stats = sample_month()
    daily.render_daily_panel(out / "daily.png", sample_items(), "模拟数据",
                             TODAY, NOW, TZ, grade_style="brush")
    monthly.render_weighted(out / "calendar.png", 2026, 10, metrics, stats,
                            TODAY, "模拟数据", grade_style="brush")
    Image, ImageDraw = calendar_image.Image, calendar_image.ImageDraw
    sheet = Image.new("RGB", (1120, 230), "#1F1F1F")
    d = ImageDraw.Draw(sheet)
    d.text((20, 8), "纯色硬笔刷 · 七档评级 · 模拟数据", font=calendar_image._font(24), fill="#F3F3F3")
    for col, ratio in enumerate((1., .97, .9, .8, .6, .3, .1)):
        x = col * 160
        d.rounded_rectangle((x + 5, 50, x + 155, 220), radius=12, fill="#303236")
        art.draw_grade(sheet, (x + 10, 56, x + 150, 160), ratio, "brush")
        box = (x + 85, 170, x + 155, 211) if ratio == 1 else (x + 109, 170, x + 150, 213)
        art.draw_grade(sheet, box, ratio, "brush")
        d.text((x + 16, 183), scoring.fmt_percent(ratio), font=calendar_image._font(18), fill="#F3F3F3")
    sheet.save(out / "all_grades.png")
    contact = Image.new("RGB", (1060, 610), "#1F1F1F")
    ImageDraw.Draw(contact).text((20, 8), "纯色硬笔刷 · 每日与月历半宽预览 · 模拟数据",
                                 font=calendar_image._font(24), fill="#F3F3F3")
    for filename, x in (("daily", 0), ("calendar", 520)):
        im = Image.open(out / f"{filename}.png")
        im = im.resize((im.width // 2, im.height // 2), Image.Resampling.LANCZOS)
        im.save(out / f"{filename}_half.png")
        contact.paste(im, (x, 50))
    contact.save(out / "comparison.png")


def export_readme_svgs(source, target):
    """Wrap exact Pillow renders in self-contained SVGs, with no UI recreation."""
    from xml.sax.saxutils import escape
    target.mkdir(parents=True, exist_ok=True)
    for stem, name, title in (
        ("daily", "daily-preview.svg", "每日打卡 · 笔刷评级 · 模拟数据"),
        ("calendar", "monthly-preview.svg", "月历 · OCD评级与条内百分比 · 模拟数据"),
    ):
        png = source / f"{stem}.png"
        with calendar_image.Image.open(png) as im:
            width, height = im.size
        data = base64.b64encode(png.read_bytes()).decode("ascii")
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
               f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">\n'
               f'  <title>{escape(title)}</title>\n'
               '  <desc>由当前 Pillow 渲染代码生成，显式启用 brush 样式。全部数据为模拟记录。</desc>\n'
               f'  <image width="{width}" height="{height}" xlink:href="data:image/png;base64,{data}"/>\n'
               '</svg>\n')
        (target / name).write_text(svg, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "previews" / "grades")
    parser.add_argument("--brush-only", action="store_true", help="Only write the new brush/ previews")
    parser.add_argument("--readme-svg", action="store_true", help="Also update docs/images SVGs from current brush renders")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    render_brush_previews(out / "brush")
    if args.readme_svg:
        export_readme_svgs(out / "brush", ROOT / "docs" / "images")
    if args.brush_only:
        print(f"Generated brush previews in {out / 'brush'}")
        return
    metrics, stats = sample_month()
    for style in STYLE_LABELS:
        daily.render_daily_panel(out / f"{style}_daily.png", sample_items(),
                                 "模拟数据", TODAY, NOW, TZ, grade_style=style)
        monthly.render_weighted(out / f"{style}_calendar.png", 2026, 10,
                                metrics, stats, TODAY, "模拟数据", grade_style=style)
    daily.render_daily_panel(out / "baseline_daily.png", sample_items(), "模拟数据", TODAY, NOW, TZ)
    monthly.render_weighted(out / "baseline_calendar.png", 2026, 10, metrics, stats, TODAY, "模拟数据")
    Image, ImageDraw = calendar_image.Image, calendar_image.ImageDraw
    # Same full-size renderer on a compact comparison sheet, with every grade.
    sheet = Image.new("RGB", (1440, 580), "#1F1F1F")
    d = ImageDraw.Draw(sheet)
    font = calendar_image._font
    d.text((28, 18), "三版评级对照 · 全部为模拟数据", font=font(28), fill="#F3F3F3")
    values = (1., .97, .9, .8, .6, .3, .1)
    for row, style in enumerate(STYLE_LABELS):
        y = 75 + row * 165
        d.text((28, y), STYLE_LABELS[style], font=font(22), fill="#B8B8B8")
        for col, ratio in enumerate(values):
            x = 220 + col * 172
            d.rounded_rectangle((x, y - 6, x + 160, y + 143), radius=12, fill="#303236")
            art.draw_grade(sheet, (x + 6, y, x + 154, y + 100), ratio, style)
            # Compact cell counterpart alongside each percentage.
            art.draw_grade(sheet, (x + 121, y + 97, x + 151, y + 142), ratio, style, compact=True)
            d.text((x + 12, y + 111), scoring.fmt_percent(ratio), font=font(18), fill="#B8B8B8")
    sheet.save(out / "all_grades.png")
    # Single contact sheet makes actual full-card layouts easy to compare.
    contact = Image.new("RGB", (1620, 1015), "#1F1F1F")
    cd = ImageDraw.Draw(contact)
    for col, style in enumerate(STYLE_LABELS):
        x = col * 540
        cd.text((x + 20, 12), STYLE_LABELS[style] + " · 模拟数据", font=font(22), fill="#F3F3F3")
        for filename, y in (("daily", 55), ("calendar", 450)):
            im = Image.open(out / f"{style}_{filename}.png")
            im.thumbnail((540, 550), Image.Resampling.LANCZOS)
            contact.paste(im, (x, y))
    contact.save(out / "comparison.png")
    print(f"Generated previews in {out}")


if __name__ == "__main__":
    main()
