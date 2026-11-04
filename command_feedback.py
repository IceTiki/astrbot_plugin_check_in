"""Explain rejected input using the original token spans. Never execute a suggestion."""
import re
import unicodedata
from datetime import date

from .scoring import RENAMES, validate_clock, validate_delay, validate_weight
from .store import HabitError, validate_month

# Each row is a complete accepted form; alternatives within a slot are literals.
FORMS = """
帮助
[任务]
任务
任务 [任务]
任务 目标 [名称] [次数]
任务 删除 [任务]
任务 时间 [任务] [时间]
任务 时间 [任务] [时间] [衰减分钟]
任务 时间 [任务] 关闭
任务 权重 [任务] [权重]
任务 规则
任务 排序
任务 排序 [任务] [位置]
任务 排序 [任务] [更多任务…]
月历
月历 [月历任务]
月历 [月份]
月历 [月历任务] [月份]
月历 [月份] [月历任务]
记录
记录 [任务]
记录 [查询日期]
记录 [任务] [查询日期]
记录 [查询日期] [任务]
记录 补签 [任务] [日期]
记录 补签 [任务] [日期] [完成时间]
记录 清除 [任务]
记录 清除 [任务] [日期]
视图
视图 重置|诊断
视图 今日|月历|帮助 图片|文字
视图 操作 自动|按钮|文字链接|关闭
好友 排行
好友 排行 [月份]
好友 隐私 公开|隐藏
好友 月历 [昵称]
好友 月历 [昵称] [任务名]
好友 月历 [昵称] [月份]
好友 月历 [昵称] [任务名] [月份]
好友 月历 [昵称] [月份] [任务名]
账户
账户 绑定
账户 绑定 [绑定码]
账户 解绑
群设置 提醒 [时间]
群设置 提醒 关闭
调试
调试 用户列表
调试 用户列表 [页码]
调试 删除用户 [账户ID]
调试 删除用户 [账户ID] 确认 [确认码]
""".strip().splitlines()
for prefix in ("调试", "调试 用户 [账户ID]"):
    for op in ("更名 [旧名称] [名称]", "彻底删除 [旧名称]"):
        FORMS.extend((f"{prefix} {op}", f"{prefix} {op} 确认 [确认码]"))

SAMPLES = dict(名称="阅读", 时间="12:00", 衰减分钟="60", 次数="1", 位置="1",
               权重="1", 日期="昨天", 查询日期="今天", 完成时间="12:00", 昵称="小明",
               页码="1", 账户ID="账户列表中的完整ID", 绑定码="私聊收到的绑定码", 确认码="预览中的确认码")
EXPECT = dict(时间="时间 HH:MM（00:00～23:59）", 衰减分钟="衰减分钟（1～1440 的整数）",
              权重="权重（0～1000 的有限数值）", 次数="次数（1～100 的整数）",
              月份="月份 YYYY-MM", 日期="今天、昨天或 YYYY-MM-DD", 查询日期="今天、昨天、前天或 YYYY-MM-DD",
              完成时间="完成时间 HH:MM 或 HH:MM:SS", 位置="从 1 开始的排序位置")


def accepts(slot, word, names, today):
    if not slot.startswith("["):
        return word in slot.split("|")
    key = slot[1:-1]
    if key in ("任务", "月历任务", "更多任务…"):
        return (word in names or RENAMES.get(word) in names
                or key == "月历任务" and word in ("全部", "总览"))
    try:
        if key in ("时间", "衰减分钟", "权重"):
            {"时间": validate_clock, "衰减分钟": validate_delay, "权重": validate_weight}[key](word)
        elif key == "月份":
            validate_month(word, today)
        elif key in ("日期", "查询日期"):
            if word not in (("今天", "昨天", "前天") if key == "查询日期" else ("今天", "昨天")):
                return date.fromisoformat(word) <= today
        elif key == "完成时间":
            validate_clock(":".join(word.split(":")[:2]))
            return bool(re.fullmatch(r"\d{1,2}:\d{2}(:[0-5]\d)?", word))
        elif key in ("次数", "位置", "页码"):
            return 1 <= int(word) <= (100 if key == "次数" else len(names) if key == "位置" else 9999)
        elif key == "账户ID":
            return bool(re.fullmatch("[0-9a-fA-F]{32}", word))
    except (ValueError, HabitError):
        return False
    return True


def width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def command_feedback(raw, command, reason, names, today):
    plain = "⚠️ " + reason
    if not command or any(s in reason for s in ("权限", "管理员", "确认码无效", "确认码已", "仅限", "绑定码无效", "仅支持群聊", "请在群聊", "请在私聊")):
        return plain
    raw = raw.expandtabs(4)
    tokens = list(re.finditer(r"\S+", raw))
    words = command.split()
    tokens = tokens[-len(words):]
    if [m[0] for m in tokens] != words:
        return plain
    forms = [line.split() for line in FORMS]
    roots = {f[0] for f in forms if not f[0].startswith("[")}
    if words[0] in roots:
        forms = [f for f in forms if f[0] == words[0]]
    scored = []
    for form in forms:
        slots = list(form)
        if slots[-1] == "[更多任务…]":
            slots.extend([slots[-1]] * max(0, len(words) - len(slots)))
        i = 0
        while i < min(len(slots), len(words)) and accepts(slots[i], words[i], names, today):
            i += 1
        scored.append((i, form, slots))
    index = max(i for i, _, _ in scored)
    choices = [(f, s) for i, f, s in scored if i == index]
    missing = index == len(words)
    if missing and any(len(s) == index for _, s in choices):
        # The form is valid. Keep runtime, access and confirmation errors intact.
        if not any(t in reason for t in ("用法", "未来", "日期", "补签过去", "不存在", "找不到", "格式")):
            return plain
        index -= 1
        missing = False
    pos = len(raw) if missing else tokens[index].start()
    word = "" if missing else words[index]
    splits = {}
    for form, slots in choices:
        if index + 1 >= len(slots):
            continue
        for k in range(1, len(word)):
            if accepts(slots[index], word[:k], names, today) and accepts(slots[index+1], word[k:], names, today):
                if slots[index].startswith("[") and word[:k] not in names:
                    continue
                splits.setdefault(k, []).append((form, slots))
    fixed = None
    if splits:
        k = next(iter(splits))
        choices = [pair for pairs in splits.values() for pair in pairs]
        pos += k
        message = "可能缺少空格，可写作 " + " 或 ".join("「"+word[:n]+" "+word[n:]+"」" for n in splits)
        fixed = raw[:pos] + " " + raw[pos:]
    else:
        slots_here = list(dict.fromkeys(s[index] for _, s in choices if index < len(s)))
        expected = []
        for slot in slots_here:
            key = slot.strip("[]")
            if key in ("任务", "月历任务", "更多任务…"):
                expected.append("任务名（" + ("、".join(names[:8]) or "发送 /打卡 任务 查看") + "）")
            else:
                expected.append(EXPECT.get(key, key.replace("|", "、")))
        message = ("缺少输入" if missing else f"「{word}」输入有误") + "，接受 " + " 或 ".join(expected) if expected else f"从「{word}」起有多余参数。"
        if reason and not reason.startswith(("用法", "🔧")):
            message += "。" + reason
    # Unknown root: list the valid root choices concisely rather than every sub-form.
    if index == 0 and not splits:
        choices = [(["[任务]"], ["[任务]"]), (["帮助"], ["帮助"])]
    forms_text = list(dict.fromkeys("/打卡 " + " ".join(f) for f, _ in choices))
    _, slots = min(choices, key=lambda pair: abs(len(pair[1]) - len(words)))
    sample = []
    for j, slot in enumerate(slots):
        key = slot.strip("[]")
        fallback = (today.strftime("%Y-%m") if key == "月份" else
                    SAMPLES.get(key, names[0] if names else "午餐"))
        keep = j < len(words) and (j < index or j > index and accepts(slot, words[j], names, today))
        example_word = fallback if slot.startswith("[") else slot.split("|")[0]
        sample.append(words[j] if keep else example_word)
    caret = " " * width(raw[:pos]) + "^" * (1 if missing or splits else max(1, width(word)))
    return "\n".join([raw, caret, "错误：" + message, *("格式：" + f for f in forms_text),
                      "示例：" + (fixed or "/打卡 " + " ".join(sample))])
