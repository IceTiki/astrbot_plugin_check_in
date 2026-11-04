import asyncio
from datetime import date

import pytest
from test_regressions import Event, plugin, resolve
from astrbot_plugin_daka.command_feedback import width
from astrbot_plugin_daka.weighted_calendar import weighted_text


@pytest.mark.parametrize("args,at,message", [
    ("任务   时间 午餐 25:00 60", "25:00", "00:00～23:59"),
    ("任务时间 午餐 12:00 60", "时间", "缺少空格"),
    ("任务 时间 午餐", "", "缺少输入"),
    ("任务 时间 午餐 关闭 60", "60", "多余"),
])
def test_rejected_command(plugin, args, at, message):
    event = Event()
    event.message_str = "/打卡 " + args
    scope = plugin.store.person_scope(resolve(plugin.store))
    snapshot = plugin.store.daily(scope, "self", plugin._today())
    asyncio.run(plugin.daka_command(event))
    lines = event.sent[-1].parts[0][1].splitlines()
    assert lines[0] == event.message_str
    position = lines[0].index(at) if at else len(lines[0])
    assert lines[1].index("^") == width(lines[0][:position])
    assert message in lines[2]
    assert all(line.startswith("格式：") for line in lines[3:-1])
    assert lines[-1].startswith("示例：/打卡")
    assert snapshot == plugin.store.daily(scope, "self", plugin._today())


def test_task_named_like_joined_command(plugin):
    for args in ("任务 目标 任务时间 1", "任务时间"):
        event = Event(args)
        asyncio.run(plugin.daka_command(event))
    scope = plugin.store.person_scope(resolve(plugin.store))
    assert any(i["name"] == "任务时间" and i["amount"] == 1 for i in plugin.store.daily(scope, "self", plugin._today()))


@pytest.mark.parametrize("earned,weight,grade", [(22.7,32,"B"),(1,1,"OCD"),(0,1,"E"),(0,0,"--")])
def test_month_grade(earned, weight, grade):
    stats = dict(earned=earned, weight=weight, ratio=earned/weight if weight else None)
    for chosen in (None, "午餐"):
        assert "月综合评级：" + grade in weighted_text(2026,11,{},stats,date(2026,11,4),chosen)
