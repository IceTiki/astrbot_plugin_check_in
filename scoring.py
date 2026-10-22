"""Pure scoring rules. No AstrBot, network, database or wall-clock calls.

Business day: [04:00, next day 04:00), in the configured local timezone.
A time target before 04:00 belongs to the END of that business day.
For v1.9.5+ rules the time credit uses gentle/smooth decay:
    (1 + ln(2) * t / H) * 2 ** (-t / H).
Existing v1.7-v1.9.4 half-life snapshots and v1.6 weighted snapshots
retain their original formulas; historical rule revisions are not rewritten.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .store import HabitError

RESET_HOUR = 4
CURRENT_MODEL = "smooth_decay"
DEFAULT_RULES = (
    ("睡觉", 1, "23:30", 60),
    ("起床", 1, "08:00", 60),
    ("早餐", 1, "09:00", 60),
    ("午餐", 1, "12:00", 60),
    ("晚餐", 1, "18:00", 60),
    ("运动", 1, None, 60),
    ("规划待办", 1, "10:00", 60),
    ("完成待办", 1, "22:00", 60),
)
RENAMES = {"\u65e9\u7761": "\u7761\u89c9", "\u65e9\u8d77": "\u8d77\u5e8a"}


def business_day(when: datetime) -> date:
    if when.tzinfo is None or when.utcoffset() is None:
        raise HabitError("\u65f6\u95f4\u5fc5\u987b\u5305\u542b\u65f6\u533a\u3002")
    return (when - timedelta(hours=RESET_HOUR)).date()


def validate_clock(raw: str) -> str:
    if not re.fullmatch(r"\d{1,2}:\d{2}", raw):
        raise HabitError("\u65f6\u95f4\u683c\u5f0f\u4e3a HH:MM\uff0c\u4f8b\u5982 23:30\u3002")
    h, m = map(int, raw.split(":"))
    if not 0 <= h <= 23 or not 0 <= m <= 59:
        raise HabitError("\u65f6\u95f4\u8303\u56f4\u4e3a 00:00\uff5e23:59\u3002")
    return f"{h:02d}:{m:02d}"


def validate_delay(raw: str | int) -> int:
    try:
        value = int(raw)
    except (ValueError, TypeError) as exc:
        raise HabitError("衰减尺度 H 必须是整数分钟。") from exc
    if not 1 <= value <= 1440:
        raise HabitError("衰减尺度 H 需要在 1～1440 分钟之间。")
    return value


def validate_weight(raw: str | float) -> float:
    try:
        value = float(raw)
    except (ValueError, TypeError) as exc:
        raise HabitError("\u6743\u91cd\u5fc5\u987b\u662f\u6570\u5b57\uff0c\u53ef\u4e3a 0\u3002") from exc
    if not math.isfinite(value) or not 0 <= value <= 1000:
        raise HabitError("\u6743\u91cd\u9700\u8981\u5728 0\uff5e1000 \u4e4b\u95f4\uff0c\u4e0d\u80fd\u662f NaN \u6216\u65e0\u7a77\u5927\u3002")
    return value


def at_clock(day: date, clock: str, tz: ZoneInfo) -> datetime:
    h, m = map(int, validate_clock(clock).split(":"))
    anchor = day + timedelta(days=1 if h < RESET_HOUR else 0)
    return datetime.combine(anchor, time(h, m), tzinfo=tz)


def parse_backfill_time(day: date, raw: str, tz: ZoneInfo, now: datetime) -> datetime:
    if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", raw):
        raise HabitError("\u8865\u7b7e\u5b8c\u6210\u65f6\u95f4\u7528 HH:MM \u6216 HH:MM:SS\u3002")
    values = raw.split(":")
    when = at_clock(day, ":".join(values[:2]), tz)
    seconds = int(values[2]) if len(values) == 3 else 0
    if not 0 <= seconds <= 59:
        raise HabitError("\u79d2\u6570\u9700\u8981\u5728 00\uff5e59 \u4e4b\u95f4\u3002")
    when = when.replace(second=seconds)
    if when > now:
        raise HabitError("\u8865\u7b7e\u7684\u5b9e\u9645\u5b8c\u6210\u65f6\u95f4\u4e0d\u80fd\u5728\u672a\u6765\u3002")
    return when


def late_factor(day: date, best_time: str | None, delay_minutes: int,
                occurred_at: datetime | None, tz: ZoneInfo,
                model: str = CURRENT_MODEL) -> float:
    """Score a check-in with the rule model saved for its business date.

    ``delay_minutes`` is retained in storage and command aliases, but under
    ``smooth_decay`` it is the *scale* H (not the actual half-life).
    ``half_life`` preserves the earlier 2**(-t/H) model for history.
    ``weighted`` preserves the still older interval/step model.
    """
    if best_time is None or occurred_at is None:
        return 1.0
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=tz)
    best = at_clock(day, best_time, tz)
    seconds = (occurred_at.astimezone(tz) - best).total_seconds()
    if seconds <= 0:
        return 1.0
    scale_seconds = validate_delay(delay_minutes) * 60
    if model == "weighted":  # v1.6 snapshots: first window full, then steps
        steps = max(0, math.ceil(seconds / scale_seconds) - 1)
        return math.ldexp(1.0, -min(steps, 1075))
    x = seconds / scale_seconds
    if model == CURRENT_MODEL:
        # f(0)=1, f(H)=(1+ln(2))/2=84.66%; strictly decreasing for t>0.
        return (1.0 + x * math.log(2.0)) * math.exp2(-x)
    # For old rule snapshots, do not change already-ended business dates.
    return math.exp2(-x)


def _parse_stamp(raw, tz):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    except (TypeError, ValueError, OverflowError):
        return None


def score_item(rule: dict, amount: int, events: list[dict], day: date,
               tz: ZoneInfo, legacy_at: str | None = None) -> dict:
    """Cap total contribution at one weight, even for multi-count tasks.

    Existing unknown/backfilled timestamps are never guessed. Legacy scoring
    versions retain their pre-upgrade all-or-nothing count-based semantics.
    For modern time rules, missing historic timestamps have no retrospective
    penalty, and the uncertainty is explicit in the output.
    """
    target = max(1, int(rule["target"]))
    credited = min(max(0, int(amount)), target)
    events = sorted(events, key=lambda e: (e.get("checked_at") or "", e.get("id", 0)))
    remaining = credited
    contributions = []
    actuals = []
    unknown = False
    for e in events:
        take = min(max(0, int(e["delta"])), remaining)
        if not take:
            continue
        raw = e.get("occurred_at")
        if not raw and e.get("kind", "normal") == "normal":
            raw = e.get("checked_at")
        occurred = _parse_stamp(raw, tz)
        if occurred is None and rule.get("best_time"):
            unknown = True
        factor = late_factor(day, rule.get("best_time"), rule["delay_minutes"], occurred, tz,
                             model=rule.get("model", CURRENT_MODEL))
        contributions.append(take * factor)
        if occurred is not None:
            actuals.append(occurred)
        remaining -= take
    if remaining:
        contributions.append(float(remaining))
        unknown = bool(rule.get("best_time")) or unknown
    progress = credited / target
    score = (float(credited >= target) if rule.get("model") == "legacy"
             else min(1.0, math.fsum(contributions) / target))
    weight = float(rule["weight"])
    item = dict(rule, amount=int(amount), progress=progress, score=score,
                earned=score * weight, complete=credited >= target,
                optional=weight == 0, time_unknown=unknown,
                first_at=min(actuals).isoformat() if actuals else None,
                last_at=max(actuals).isoformat() if actuals else None,
                legacy_at=legacy_at if credited and not actuals else None)
    item["penalized"] = rule.get("model") != "legacy" and score + 1e-10 < progress
    return item


def summarize(items: list[dict]) -> dict:
    weight = math.fsum(i["weight"] for i in items if i["weight"] > 0)
    earned = math.fsum(i["earned"] for i in items if i["weight"] > 0)
    ratio = min(1.0, earned / weight) if weight else None
    required = [i for i in items if i["weight"] > 0]
    return {"weight": weight, "earned": earned, "ratio": ratio,
            "finished": sum(i["complete"] for i in items), "total": len(items),
            "required_finished": sum(i["complete"] for i in required),
            "required_total": len(required), "optional_total": len(items) - len(required),
            "penalized": sum(i["penalized"] for i in required),
            "full_score": ratio is not None and ratio >= 1 - 1e-10,
            "items": items}


def month_summary(days: dict[int, dict]) -> dict:
    eligible = [v for v in days.values() if v["weight"] > 0]
    weight = math.fsum(v["weight"] for v in eligible)
    earned = math.fsum(v["earned"] for v in eligible)
    return {"weight": weight, "earned": earned,
            "ratio": min(1.0, earned / weight) if weight else None,
            "days": len(eligible), "full_days": sum(v["full_score"] for v in eligible)}


def fmt_number(value: float) -> str:
    if value == 0:
        return "0"
    if 0 < abs(value) < 0.01:
        return "<0.01"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def fmt_percent(ratio: float | None) -> str:
    if ratio is None:
        return "--"
    if 0 < ratio < .001:
        return "<0.1%"
    if ratio < 1 - 1e-10 and round(ratio * 100, 1) >= 100:
        return "99.9%"
    return f"{ratio * 100:.1f}".rstrip("0").rstrip(".") + "%"
