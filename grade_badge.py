"""Shared, presentation-only score grades and clipped Pillow badge artwork."""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from .calendar_image import Image, ImageDraw

STYLES = ("arcade", "metal", "neon", "brush")
COLORS = {"OCD": "#FFE7A0", "S": "#CB9AFF", "A": "#60E4E8",
          "B": "#73ADFF", "C": "#DCC17C", "D": "#F49B6B", "E": "#A8ADB8"}


def grade_for(ratio):
    """Use raw weighted scores, with the scoring engine's full-score tolerance."""
    if ratio is None:
        return None
    try:
        ratio = float(ratio)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(ratio):
        return None
    if ratio >= 1 - 1e-10:
        return "OCD"
    for threshold, grade in ((.95, "S"), (.85, "A"), (.70, "B"),
                             (.50, "C"), (.25, "D")):
        if ratio >= threshold:
            return grade
    return "E"


@lru_cache(maxsize=64)
def _grade_font(size):
    from PIL import ImageFont
    for candidate in (
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def draw_grade(image, box, ratio, style=None, *, compact=False):
    """Composite a badge strictly inside box; compact mode uses O for OCD.

    None or 'off' keeps existing callers unchanged. Unknown styles fail early.
    The calendar caller supplies only a scored, non-future day's actual ratio.
    """
    if style in (None, "off"):
        return
    if style not in STYLES:
        raise ValueError(f"Unknown grade style: {style}")
    grade = grade_for(ratio)
    if grade is None or Image is None:
        return
    from PIL import ImageColor, ImageFilter, ImageChops

    x0, y0, x1, y1 = map(int, box)
    w, h = x1 - x0, y1 - y0
    if min(w, h) < 8:
        return
    if style == "brush":
        from .brush_letters import brush_layer
        rgb = ImageColor.getrgb("#FFD75E" if grade == "OCD" else COLORS[grade])
        layer = brush_layer(w, h, "O" if compact and grade == "OCD" else grade,
                            rgb, perfect=grade == "OCD", compact=compact)
        image.paste(layer, (x0, y0), layer)
        return
    # Draw at triple resolution so the small calendar letters remain crisp.
    scale = 3
    W, H = w * scale, h * scale
    layer = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(layer)
    rgb = ImageColor.getrgb(COLORS[grade])
    perfect = grade == "OCD"
    text = "O" if compact and perfect else grade
    pad = 3 * scale
    ornament = (10 if compact else 15) * scale if perfect else 0
    available_w = W - pad * 2 - (4 * scale if style == "arcade" else 0)
    available_h = H - pad * 2 - ornament
    size = max(8, int(available_h * 1.2))
    font = _grade_font(size)
    bounds = font.getbbox(text)
    while size > 8:
        font = _grade_font(size)
        bounds = font.getbbox(text)
        if bounds[2] - bounds[0] <= available_w and bounds[3] - bounds[1] <= available_h:
            break
        size -= 1
    tw, th = bounds[2] - bounds[0], bounds[3] - bounds[1]
    tx, ty = (W - tw) // 2, (H - th + ornament) // 2
    mask = Image.new("L", (W, H))
    ImageDraw.Draw(mask).text((tx - bounds[0], ty - bounds[1]), text, font=font, fill=255)
    if style == "arcade":
        # Shear the glyph mask, rather than depending on a platform italic font.
        mask = mask.transform((W, H), Image.Transform.AFFINE,
                              (1, .16, -.16 * H / 2, 0, 1, 0), Image.Resampling.BICUBIC)
        shadow = Image.new("RGBA", (W, H), (10, 12, 24, 0))
        shifted = Image.new("L", (W, H))
        shifted.paste(mask, (2 * scale, 2 * scale))
        shadow.putalpha(shifted.point(lambda v: round(v * .8)))
        layer = Image.alpha_composite(layer, shadow)
        gradient = Image.new("RGBA", (W, H))
        gd = ImageDraw.Draw(gradient)
        for y in range(H):
            t = min(1, max(0, (y - ty) / max(1, th)))
            c = tuple(round(255 * (1 - t) + v * t) for v in rgb)
            gd.line((0, y, W, y), fill=(*c, 235))
        gradient.putalpha(mask.point(lambda v: round(v * .86)))
        layer = Image.alpha_composite(layer, gradient)
    elif style == "metal":
        d.polygon([(pad, H // 3), (W // 2, pad), (W - pad, H // 3),
                   (W - pad, H * 3 // 4), (W // 2, H - pad), (pad, H * 3 // 4)],
                  fill=(24, 27, 35, 170), outline=(*rgb, 175), width=scale)
        gradient = Image.new("RGBA", (W, H))
        gd = ImageDraw.Draw(gradient)
        for y in range(H):
            t = (y - ty) / max(1, th)
            brightness = .65 if .43 < t < .56 else (1 if t < .43 else .82)
            gd.line((0, y, W, y), fill=tuple(round(c * brightness) for c in rgb) + (255,))
        gradient.putalpha(mask.point(lambda v: round(v * .92)))
        layer = Image.alpha_composite(layer, gradient)
    else:
        outer = mask.filter(ImageFilter.MaxFilter(2 * scale + 1))
        glow = outer.filter(ImageFilter.GaussianBlur(2 * scale))
        neon = Image.new("RGBA", (W, H), (*rgb, 0))
        neon.putalpha(glow.point(lambda v: round(v * .65)))
        layer = Image.alpha_composite(layer, neon)
        ring = ImageChops.subtract(outer, mask)
        neon.putalpha(ring.point(lambda v: round(v * .95)))
        layer = Image.alpha_composite(layer, neon)
        core = Image.new("RGBA", (W, H), (*rgb, 0))
        core.putalpha(mask.point(lambda v: round(v * .3)))
        layer = Image.alpha_composite(layer, core)
    if perfect:
        d = ImageDraw.Draw(layer)
        cx, cy = W // 2, max(5 * scale, ty - 5 * scale)
        if style == "metal":
            d.polygon([(cx - 8 * scale, cy - 3 * scale), (cx - 7 * scale, cy + 3 * scale),
                       (cx + 7 * scale, cy + 3 * scale), (cx + 8 * scale, cy - 3 * scale),
                       (cx + 3 * scale, cy), (cx, cy - 5 * scale), (cx - 3 * scale, cy)],
                      fill=(255, 226, 143, 245))
        elif style == "neon":
            d.ellipse((cx - 9 * scale, cy - 3 * scale, cx + 9 * scale, cy + 3 * scale),
                      outline=(255, 237, 181, 240), width=scale)
        else:
            for dx in (-7, 0, 7):
                d.line((cx + dx * scale, cy - 4 * scale, cx + dx * scale, cy),
                       fill=(255, 239, 183, 245), width=scale)
    layer = layer.resize((w, h), Image.Resampling.LANCZOS)
    image.paste(layer, (x0, y0), layer)
