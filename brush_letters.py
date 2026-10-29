"""Original polygon brush lettering for the seven score symbols.

Coordinates are glyph-local vector outlines, not a dependency on installed fonts.
The small cuts are deliberate dry-brush marks; no randomness is used.
"""
from .calendar_image import Image, ImageDraw

# Each glyph contains a solid outline followed by counter-shapes to subtract.
GLYPHS = {
    "O": (
        [(22, 4), (76, 0), (98, 22), (96, 91), (73, 117), (17, 120), (0, 96), (2, 29)],
        [(29, 29), (64, 25), (72, 37), (70, 81), (59, 92), (28, 94), (24, 81), (25, 41)],
    ),
    "C": (
        [(23, 4), (79, 0), (99, 24), (93, 43), (70, 41), (65, 26), (31, 30),
         (25, 42), (24, 83), (31, 94), (63, 90), (73, 73), (99, 77), (88, 103),
         (72, 117), (18, 120), (0, 97), (2, 29)],
    ),
    "D": (
        [(3, 5), (65, 0), (98, 28), (96, 89), (64, 116), (0, 120)],
        [(29, 29), (54, 26), (72, 43), (70, 76), (53, 91), (27, 94)],
    ),
    "S": (
        [(23, 4), (85, 0), (99, 22), (89, 38), (66, 26), (31, 29), (25, 43),
         (80, 51), (98, 69), (94, 98), (73, 117), (9, 120), (0, 94),
         (21, 82), (31, 96), (65, 92), (72, 78), (16, 68), (1, 49), (6, 24)],
    ),
    "A": (
        [(31, 4), (68, 0), (100, 118), (73, 115), (66, 91), (30, 94),
         (23, 117), (0, 120)],
        [(48, 32), (37, 69), (59, 66)],
    ),
    "B": (
        [(3, 5), (72, 0), (98, 21), (93, 48), (80, 57), (98, 71),
         (94, 99), (71, 116), (0, 120)],
        [(29, 28), (61, 25), (71, 34), (65, 46), (28, 49)],
        [(28, 72), (62, 69), (71, 79), (64, 92), (27, 95)],
    ),
    "E": (
        [(3, 5), (100, 0), (92, 27), (29, 31), (28, 48), (83, 44),
         (78, 71), (28, 74), (27, 94), (98, 88), (89, 116), (0, 120)],
    ),
}


def brush_layer(width, height, text, rgb, *, perfect=False, compact=False):
    """Build a high-resolution, uniformly colored brush badge with a small shadow."""
    scale = 3
    W, H = width * scale, height * scale
    margin = 3 * scale
    ray_height = (6 if compact else 10) * scale if perfect else 0
    natural_w = len(text) * 124 - 6
    factor = min((W - margin * 2 - 2 * scale) / natural_w,
                 (H - margin * 2 - ray_height - 2 * scale) / 120)
    factor = max(.01, factor)
    left = (W - natural_w * factor) / 2
    top = (H - 120 * factor + ray_height) / 2 - scale
    mask = Image.new("L", (W, H))
    d = ImageDraw.Draw(mask)
    for i, letter in enumerate(text):
        def points(outline):
            return [(round(left + (i * 124 + x + .15 * (120 - y)) * factor),
                     round(top + y * factor)) for x, y in outline]
        outlines = GLYPHS[letter]
        for j, outline in enumerate(outlines):
            d.polygon(points(outline), fill=255 if j == 0 else 0)
        # Fine tapered cuts only; the silhouette stays heavy and readable.
        d.polygon(points([(12, 10), (38, 8), (13, 12)]), fill=0)
        d.polygon(points([(6, 103), (22, 99), (8, 105)]), fill=0)
    layer = Image.new("RGBA", (W, H))
    shadow_mask = Image.new("L", (W, H))
    shadow_mask.paste(mask, (scale, scale))
    shadow = Image.new("RGBA", (W, H), (9, 12, 19, 0))
    shadow.putalpha(shadow_mask.point(lambda v: round(v * .8)))
    layer = Image.alpha_composite(layer, shadow)
    ink = Image.new("RGBA", (W, H), (*rgb, 0))
    ink.putalpha(mask)
    layer = Image.alpha_composite(layer, ink)
    if perfect:
        d = ImageDraw.Draw(layer)
        cx, cy = W // 2, max(2 * scale, round(top) - 2 * scale)
        for dx in (-7, 0, 7):
            d.line((cx + dx * scale, cy - (4 if dx else 6) * scale,
                    cx + dx * scale, cy), fill=(*rgb, 255), width=scale)
    return layer.resize((width, height), Image.Resampling.LANCZOS)
