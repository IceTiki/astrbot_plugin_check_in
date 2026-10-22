"""Metro-style full command reference rendered on demand with Pillow.

Intentionally uses the same HELP_SECTIONS as the text fallback. If there is no
Chinese-capable font, return None instead of producing unreadable tofu glyphs.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .calendar_image import Image, ImageDraw, ImageFont, _save, FONT_CANDIDATES, _supports_chinese
from .help_content import HELP_SECTIONS

W = 1080
PAD = 42
ROW_H = 77
CARD_HEAD = 72
CARD_FOOT = 15
CARD_GAP = 21
HEADER = 326
FOOTER = 154

@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False):
    paths = []
    for path in FONT_CANDIDATES[:-1]:
        if Path(path).is_file():
            paths.append(path)
            if "NotoSansCJK-Regular.ttc" in path and bold:
                alternative = path.replace("Regular.ttc", "Bold.ttc")
                if Path(alternative).is_file():
                    paths.insert(0, alternative)
    if not paths:
        return None
    filename = paths[0]
    # The Noto CJK font collection's index 2 is simplified Chinese.
    index = 2 if "NotoSansCJK" in filename and filename.endswith(".ttc") else 0
    try:
        return ImageFont.truetype(filename, size, index=index)
    except (OSError, ValueError):
        return ImageFont.truetype(filename, size)

def _fit(draw, label: str, max_width: int, size: int, bold: bool = True):
    while size > 19:
        f = _font(size, bold)
        if draw.textbbox((0, 0), label, font=f)[2] <= max_width:
            return f
        size -= 1
    return _font(size, bold)

def render_help_image(out_path: Path) -> str | None:
    """Render all documented examples in a readable, tall Metro help poster."""
    if Image is None or not _supports_chinese() or _font(30) is None:
        return None
    height = HEADER + sum(CARD_HEAD + len(rows) * ROW_H + CARD_FOOT + CARD_GAP
                          for _, _, _, _, rows in HELP_SECTIONS) + FOOTER
    img = Image.new("RGB", (W, height), "#F1F4F8")
    d = ImageDraw.Draw(img)

    d.rectangle((0, 0, W, HEADER), fill="#142B43")
    for rect, color in (
        ((826,0,900,74),"#0F93BF"), ((906,0,980,74),"#14B29A"),
        ((986,0,1080,74),"#E5A04B"), ((906,80,1080,155),"#785BC1"),
        ((996,161,1080,250),"#1676B5"),
    ):
        d.rectangle(rect, fill=color)
    d.rectangle((PAD,49,PAD+11,108), fill="#1BA9BA")
    d.text((PAD+28,45), "打卡命令大全", font=_font(65,True), fill="#FFFFFF")
    d.text((PAD+4,151), "每日目标  /  记录  /  视图  /  好友  /  账户",
           font=_font(28), fill="#CBD9E6")
    d.rounded_rectangle((PAD,227,PAD+305,279),radius=6,fill="#255174")
    d.text((PAD+17,233), "v1.9.5 · 完整命令", font=_font(26,True), fill="#F6FBFF")
    d.text((PAD+335,239), "全部正式命令 · 单次打卡只增加 1 次",
           font=_font(23), fill="#D7E4EF")

    y = HEADER + 9
    card_width = W - 2 * PAD
    for number, heading, subtitle, color, rows in HELP_SECTIONS:
        section_height = CARD_HEAD + len(rows) * ROW_H + CARD_FOOT
        d.rounded_rectangle((PAD,y,PAD+card_width,y+section_height),
                            radius=16, fill="#FFFFFF")
        d.rectangle((PAD,y+17,PAD+10,y+section_height-16), fill=color)
        d.rounded_rectangle((PAD+28,y+16,PAD+100,y+59),radius=7,fill=color)
        d.text((PAD+43,y+20),number,font=_font(25,True),fill="#FFFFFF")
        d.text((PAD+118,y+15),heading,font=_font(35,True),fill="#193249")
        sub_font=_fit(d,subtitle,350,23,False)
        sub_width=d.textbbox((0,0),subtitle,font=sub_font)[2]
        d.text((PAD+card_width-24-sub_width,y+31),
               subtitle,font=sub_font,fill="#7B8997")
        d.line((PAD+29,y+CARD_HEAD-1,PAD+card_width-25,y+CARD_HEAD-1),
               fill="#E7EBF0",width=2)
        for j,(command,desc) in enumerate(rows):
            row_y = y + CARD_HEAD + j * ROW_H
            cmdfont=_fit(d,command,card_width-77,32,True)
            d.text((PAD+39,row_y+5),command,font=cmdfont,fill="#203549")
            descfont=_fit(d,desc,card_width-81,23,False)
            d.text((PAD+41,row_y+43),desc,font=descfont,fill="#7B8A9A")
            if j<len(rows)-1:
                d.line((PAD+34,row_y+ROW_H-1,PAD+card_width-29,row_y+ROW_H-1),
                       fill="#EDF0F3",width=2)
        y += section_height + CARD_GAP

    footer_y = y + 3
    d.rectangle((0,footer_y-2,W,height), fill="#E5EBF0")
    d.rectangle((PAD,footer_y+24,PAD+10,footer_y+99), fill="#149BBA")
    d.text((PAD+26,footer_y+20),"说明",font=_font(30,True),fill="#193249")
    d.text((PAD+126,footer_y+24),
           "习惯名称、日期为示例，可换成自己的内容。",font=_font(23),fill="#556A7E")
    d.text((PAD+26,footer_y+71),
           "凌晨 04:00 换日 · 视图设置仅影响自己的默认显示。",font=_font(22),fill="#556A7E")
    return _save(img,out_path)
