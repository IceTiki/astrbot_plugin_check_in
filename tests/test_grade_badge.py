"""Score boundary and image integration checks, without live AstrBot calls."""
import importlib.util
import math
from pathlib import Path

import pytest
from PIL import Image, ImageChops

spec = importlib.util.spec_from_file_location("grade_preview", Path(__file__).resolve().parents[1] / "tools/preview_grades.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
art = preview.art


@pytest.mark.parametrize("ratio,expected", [
    (None, None), (float("nan"), None), (float("inf"), None), ("bad", None),
    (0., "E"), (.249999, "E"), (.25, "D"), (.499999, "D"),
    (.5, "C"), (.699999, "C"), (.7, "B"), (.849999, "B"),
    (.85, "A"), (.949999, "A"), (.95, "S"), (.99999, "S"),
    (1 - 2e-10, "S"), (1 - 1e-10, "OCD"), (1., "OCD"),
])
def test_grade_boundaries(ratio, expected):
    assert art.grade_for(ratio) == expected


@pytest.mark.parametrize("threshold,below,at", [
    (.25, "E", "D"), (.5, "D", "C"), (.7, "C", "B"),
    (.85, "B", "A"), (.95, "A", "S"),
])
def test_adjacent_float_thresholds(threshold, below, at):
    assert art.grade_for(math.nextafter(threshold, 0)) == below
    assert art.grade_for(threshold) == at
    assert art.grade_for(math.nextafter(threshold, 1)) == at


def test_daily_future_business_day_has_no_grade(tmp_path):
    from datetime import datetime
    # Before 04:00, the calendar date is still the next business day.
    now = datetime(2026, 10, 28, 3, tzinfo=preview.TZ)
    paths = [tmp_path / f"{n}.png" for n in (0, 1)]
    for path, style in zip(paths, (None, "arcade")):
        preview.daily.render_daily_panel(path, preview.sample_items(), "模拟",
                                         preview.TODAY, now, preview.TZ, style)
    assert ImageChops.difference(*(Image.open(p).convert("RGB") for p in paths)).getbbox() is None


@pytest.mark.parametrize("style", art.STYLES)
def test_badge_is_clipped_and_off_is_unchanged(style):
    original = Image.new("RGB", (200, 120), "#303236")
    image = original.copy()
    art.draw_grade(image, (50, 20, 150, 100), 1., style)
    bounds = ImageChops.difference(original, image).getbbox()
    assert bounds and bounds[0] >= 50 and bounds[1] >= 20
    assert bounds[2] <= 150 and bounds[3] <= 100
    image = original.copy()
    art.draw_grade(image, (50, 20, 150, 100), 1.)
    assert ImageChops.difference(original, image).getbbox() is None


@pytest.mark.parametrize("style", art.STYLES)
@pytest.mark.parametrize("count", [0, 1, 8, 32, 64])
def test_daily_changes_only_badge_region(tmp_path, style, count):
    items = preview.sample_items(.97, count)
    assert count == 0 or all(i["complete"] for i in items)
    stats = preview.scoring.summarize(items)
    assert art.grade_for(stats["ratio"]) == ("S" if count else None)
    paths = [tmp_path / f"{n}.png" for n in (0, 1)]
    for path, choice in zip(paths, (None, style)):
        preview.daily.render_daily_panel(path, items, "很长的昵称" * 20,
                                         preview.TODAY, preview.NOW, preview.TZ, choice)
    original, changed = [Image.open(p).convert("RGB") for p in paths]
    assert original.size == changed.size
    bounds = ImageChops.difference(original, changed).getbbox()
    if count:
        assert bounds and bounds[0] >= 600 and bounds[1] >= 35
        assert bounds[2] <= 994 and bounds[3] <= 158
    else:
        assert bounds is None


@pytest.mark.parametrize("style", [s for s in art.STYLES if s != "brush"])
def test_calendar_only_scored_nonfuture_cells_change(tmp_path, style):
    from datetime import date
    import calendar
    # August has six week rows. Include a future score and optional-only day.
    metrics = {n: preview.scoring.summarize(preview.sample_items(1.)) for n in (1, 2, 3, 31)}
    metrics[2].update(weight=0, ratio=None, display_ratio=1.)
    stats = preview.scoring.month_summary(metrics)
    paths = [tmp_path / f"{n}.png" for n in (0, 1)]
    for path, choice in zip(paths, (None, style)):
        preview.monthly.render_weighted(path, 2026, 8, metrics, stats, date(2026, 8, 3), "模拟", grade_style=choice)
    original, changed = [Image.open(p).convert("RGB") for p in paths]
    assert original.size == changed.size == (1080, 1231)
    diff = ImageChops.difference(original, changed)
    assert diff.getbbox()
    for row, week in enumerate(calendar.Calendar().monthdayscalendar(2026, 8)):
        for col, n in enumerate(week):
            if n:
                x, y = 52 + col * 141, 314 + row * 134
                cell = diff.crop((x, y, x + 129, y + 121))
                bounds = cell.getbbox()
                if n in (1, 3):
                    assert bounds and bounds[0] >= 102 and bounds[1] >= 38
                    assert bounds[2] <= 128 and bounds[3] <= 106
                else:
                    assert bounds is None


@pytest.mark.parametrize("count", [1, 8, 16, 32, 64])
def test_brush_calendar_preserves_markers_and_geometry(tmp_path, count):
    from datetime import date
    import calendar
    ratios = (0., .00001, .25, .5, .7, .85, .95, .99999, 1.)
    metrics = {n: preview.scoring.summarize(preview.sample_items(ratio, count))
               for n, ratio in enumerate(ratios, 1)}
    metrics[10] = dict(metrics[9], ratio=None, weight=0, display_ratio=None)
    stats = preview.scoring.month_summary(metrics)
    paths = [tmp_path / f"{n}.png" for n in (0, 1)]
    for path, choice in zip(paths, (None, "brush")):
        preview.monthly.render_weighted(path, 2026, 8, metrics, stats, date(2026, 8, 9),
                                       "模拟", grade_style=choice)
    original, changed = [Image.open(p).convert("RGB") for p in paths]
    assert original.size == changed.size
    size, gap = (15, 5) if count <= 8 else (13, 4) if count <= 16 else (10, 3) if count <= 32 else (8, 2)
    rows = math.ceil(count / min(4, count))
    height = max(120, 51 + rows * (size + gap) + 26)
    for row, week in enumerate(calendar.Calendar().monthdayscalendar(2026, 8)):
        for col, n in enumerate(week):
            if not n:
                continue
            x, y = 52 + col * 141, 314 + row * (height + 14)
            # Task markers and left-hand date/points are exactly unchanged.
            for box in ((x, y + 48, x + 129, y + height - 22),
                        (x, y, x + 45, y + 25), (x, y + 25, x + 45, y + 48)):
                assert ImageChops.difference(original.crop(box), changed.crop(box)).getbbox() is None
            # The old side badge area is empty, apart from the new bottom bar.
            assert changed.getpixel((x + 118, y + height - 23)) == (48, 50, 54)


def test_brush_glyphs_do_not_use_system_fonts(monkeypatch):
    monkeypatch.setattr(art, "_grade_font", lambda *_: pytest.fail("brush must use vector outlines"))
    for ratio in (1., .97, .9, .8, .6, .3, .1):
        image = Image.new("RGB", (300, 100))
        art.draw_grade(image, (0, 0, 300, 100), ratio, "brush")
        assert image.getbbox()


def test_brush_bar_labels_and_optional_awards(tmp_path, monkeypatch):
    from datetime import date
    from PIL import ImageDraw
    labels, awards = [], []
    original_text = ImageDraw.ImageDraw.text
    def capture_text(self, xy, text, *args, **kwargs):
        if kwargs.get("anchor") == "mm":
            labels.append((text, kwargs.get("fill"), kwargs.get("stroke_width")))
        return original_text(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    monkeypatch.setattr(preview.monthly, "draw_grade", lambda image, box, ratio, *a, **kw: awards.append(ratio))
    metrics = {n: preview.scoring.summarize(preview.sample_items(ratio))
               for n, ratio in enumerate((0., .00001, .99999, 1.), 1)}
    metrics[5] = dict(metrics[4], weight=0, ratio=None, display_ratio=1.)
    metrics[7] = metrics[4]  # Future score must not be awarded.
    preview.monthly.render_weighted(tmp_path / "labels.png", 2026, 10, metrics,
                                    preview.scoring.month_summary(metrics), date(2026, 10, 6),
                                    "模拟", grade_style="brush")
    assert [s for s, _, _ in labels[:7]] == ["0%", "<0.1%", "99.9%", "100%", "--", "--", "--"]
    assert all(color == preview.monthly.INK and stroke == 1 for _, color, stroke in labels)
    assert awards == [0., .00001, .99999, 1., None]


def test_brush_calendar_uses_full_ocd(tmp_path, monkeypatch):
    from datetime import date
    calls = []
    monkeypatch.setattr(preview.monthly, "draw_grade", lambda image, box, ratio, style, **kw:
                        calls.append((box, ratio, kw["compact"])))
    metrics = {1: preview.scoring.summarize(preview.sample_items())}
    preview.monthly.render_weighted(tmp_path / "full.png", 2026, 10, metrics,
                                    preview.scoring.month_summary(metrics), date(2026, 10, 1),
                                    "模拟", grade_style="brush")
    box, ratio, compact = calls[0]
    assert ratio == 1. and compact is False
    assert box[2] - box[0] == 78


def test_readme_svg_embeds_exact_render(tmp_path):
    import base64
    import xml.etree.ElementTree as ET
    source = tmp_path / "source"
    source.mkdir()
    for stem in ("daily", "calendar"):
        Image.new("RGB", (104, 77), "#FFD75E").save(source / f"{stem}.png")
    target = tmp_path / "docs"
    preview.export_readme_svgs(source, target)
    for stem, filename in (("daily", "daily-preview.svg"), ("calendar", "monthly-preview.svg")):
        root = ET.parse(target / filename).getroot()
        assert root.attrib["viewBox"] == "0 0 104 77"
        embedded = root.find("{http://www.w3.org/2000/svg}image")
        uri = embedded.attrib["{http://www.w3.org/1999/xlink}href"]
        assert base64.b64decode(uri.split(",", 1)[1]) == (source / f"{stem}.png").read_bytes()
