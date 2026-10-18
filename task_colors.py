"""Stable Win8/Metro colors shared by daily and monthly views."""
from __future__ import annotations

from hashlib import sha256
import colorsys

# Fixed colors for the built-in habits. Keep these values stable across releases.
TASK_COLORS = {
    "\u7761\u89c9": "#6A5ACD",
    "\u8d77\u5e8a": "#00A4EF",
    "\u65e9\u9910": "#7FBA00",
    "\u5348\u9910": "#FFB900",
    "\u665a\u9910": "#F25022",
    "\u8fd0\u52a8": "#00B294",
    "\u5de5\u4f5c\u4e00\u5c0f\u65f6": "#8E44AD",
}

TASK_ALIASES = {
    "\u65e9\u7761": "\u7761\u89c9",
    "\u65e9\u8d77": "\u8d77\u5e8a",
}

# Custom tasks receive a deterministic hue based on their name, so colors stay
# stable across ordering, restart, and calendar month without a small palette
# causing frequent collisions when users add many custom tasks.

ENGLISH_LABELS = {
    "\u7761\u89c9": "Sleep",
    "\u65e9\u7761": "Sleep",
    "\u8d77\u5e8a": "Wake up",
    "\u65e9\u8d77": "Wake up",
    "\u65e9\u9910": "Breakfast",
    "\u5348\u9910": "Lunch",
    "\u665a\u9910": "Dinner",
    "\u8fd0\u52a8": "Exercise",
    "\u5de5\u4f5c\u4e00\u5c0f\u65f6": "Work hour",
    "\u559d\u6c34": "Water",
}


def task_color(name: str, index: int = 0) -> str:
    key = TASK_ALIASES.get(str(name), str(name))
    if key in TASK_COLORS:
        return TASK_COLORS[key]
    digest = sha256(str(name).encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535.0
    saturation = 0.62 + digest[2] / 255.0 * 0.18
    value = 0.76 + digest[3] / 255.0 * 0.18
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return "#{:02X}{:02X}{:02X}".format(round(red * 255), round(green * 255), round(blue * 255))


def task_label(name: str, index: int = 0, chinese: bool = True) -> str:
    value = str(name)
    if chinese:
        return value
    if value in ENGLISH_LABELS:
        return ENGLISH_LABELS[value]
    if value.isascii():
        return value
    return f"TASK {index + 1}"
