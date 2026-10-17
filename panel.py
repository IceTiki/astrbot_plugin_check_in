"""Compact native chat panel. Buttons/links are added by qq_transport, once."""
from datetime import timedelta, datetime
from .scoring import (summarize, fmt_number, fmt_percent, at_clock, late_factor)


def timing_line(item, day, now, tz):
    if not item.get("best_time"):
        return ""
    best = at_clock(day, item["best_time"], tz)
    until = best + timedelta(minutes=item["delay_minutes"])
    suffix = ("\u6b21\u65e5" if until.date() > day else "") + until.strftime("%H:%M")
    line = f"\u6700\u4f73 {item['best_time']} \u00b7 \u5ef6\u8fdf {item['delay_minutes']}\u5206\uff08\u81f3{suffix}\uff09"
    if not item["complete"]:
        factor = late_factor(day, item["best_time"], item["delay_minutes"], now, tz)
        if factor < 1:
            line += f" \u00b7 \u73b0\u5728\u5f97\u5206{fmt_percent(factor)}"
    return line


def daily_panel(items, nick, day, now, tz):
    stats = summarize(items)
    name = nick.replace("\n", " ").replace("\r", " ")[:24]
    lines = [f"\U0001f4c5 {day:%m-%d} \u00b7 {name} \u4eca\u65e5\u6253\u5361"]
    if stats["ratio"] is None:
        lines.append("\u5b8c\u6210\u5ea6 -- \u00b7 \u5f53\u524d\u6ca1\u6709\u8ba1\u5206\u4efb\u52a1")
    else:
        ratio = stats["ratio"]
        slots = min(10, int(ratio * 10 + 1e-9))
        bar = "\u25b0" * slots + "\u25b1" * (10-slots)
        lines.append(f"{bar}  {fmt_percent(ratio)}")
        lines.append(f"\u5f97\u5206 {fmt_number(stats['earned'])}/{fmt_number(stats['weight'])} "
                     f"\u00b7 \u5df2\u5b8c\u6210 {stats['required_finished']}/{stats['required_total']} \u9879")
    cutoff = day + timedelta(days=1)
    lines.append(f"\u6253\u5361\u65e5\u622a\u6b62 {cutoff:%m-%d} 04:00\uff08{tz.key}\uff09")
    lines.append("")
    for idx, item in enumerate(items[:20], 1):
        complete = item["complete"]
        icon = "\u2705" if complete else ("\U0001f7e1" if item["amount"] else "\u25ab\ufe0f")
        if complete and item["penalized"]:
            icon = "\U0001f7e0"
        count = f" {item['amount']}/{item['target']}" if item["target"] > 1 else ""
        points = ("\u53ef\u9009\u00b7\u4e0d\u8ba1\u5206" if item["optional"] else
                  f"{fmt_number(item['earned'])}/{fmt_number(item['weight'])}\u5206")
        stamp = ""
        if item.get("last_at"):
            when = datetime.fromisoformat(item["last_at"]).astimezone(tz)
            stamp = " \u00b7 " + ("\u6b21\u65e5" if when.date() > day else "") + when.strftime("%H:%M")
        flag = " \u8fdf\u5230\u6298\u5206" if item["penalized"] else ""
        lines.append(f"{icon} {idx}. {item['name']}{count} \u00b7 {points}{stamp}{flag}")
        timing = timing_line(item, day, now, tz)
        if timing:
            lines.append("    " + timing)
        if item["time_unknown"]:
            lines.append("    \u65e7\u8bb0\u5f55\u65f6\u95f4\u4e0d\u5168\uff0c\u4e0d\u8ffd\u6eaf\u6263\u5206")
    if len(items) > 20:
        lines.append(f"\u2026\u5176\u4f59 {len(items)-20} \u9879\u89c1 /\u6253\u5361 \u6e05\u5355")
    unfinished = [i for i in items if not i["complete"] and not i["optional"]]
    lines.append("")
    if unfinished:
        pending = "\u3001".join(i["name"] + (f"\uff08\u5dee{i['target']-i['amount']}\u6b21\uff09" if i["target"] > 1 else "")
                               for i in unfinished[:7])
        lines.append("\U0001f4cc \u5f85\u5b8c\u6210\uff1a" + pending + ("\u2026" if len(unfinished)>7 else ""))
    elif stats["required_total"]:
        lines.append("\u2705 \u8ba1\u5206\u4efb\u52a1\u5df2\u5168\u90e8\u6253\u5361" +
                     ("\uff1b\u5b8c\u6210\u5ea6\u5df2\u6263\u9664\u8fdf\u5230\u5206\u3002" if stats["penalized"] else "\u3002"))
    lines.append("\u8bbe\u7f6e /\u6253\u5361 \u8bbe\u7f6e \u00b7 \u65f6\u95f4\u8bb0\u5f55 /\u6253\u5361 \u8bb0\u5f55")
    return "\n".join(lines)
