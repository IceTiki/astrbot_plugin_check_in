"""Central validation for command/task name collisions."""
import re
from .store import HabitError, validate_name

RESERVED_COMMANDS = {
    "任务", "记录", "视图", "好友", "账户", "群设置", "月历", "帮助", "调试",
}
RETIRED_COMMANDS = {
    "今日", "今天", "进度", "清单", "习惯", "设置", "添加", "删除", "恢复",
    "目标", "权重", "时间", "时段", "最佳", "延迟", "半衰期", "规则",
    "撤销", "取消", "补签", "隐私", "排行", "排行榜", "文字月历",
    "交互", "显示", "月历视图", "月历样式", "月历展示", "身份", "绑定",
    "解绑", "绑定状态", "诊断", "按钮诊断", "按钮状态", "提醒", "关闭提醒",
    "历史时间", "help", "?",
}
AMBIGUOUS_NAMES = {
    "昨天", "前天", "确认", "用户", "用户列表", "删除用户", "更名",
    "彻底删除", "清除", "图片", "文字", "关闭", "公开", "隐藏",
    "重置", "操作", "列表", "SID", "账户ID",
}

def validate_task_name(raw: str) -> str:
    name = validate_name(raw)
    if (name in RESERVED_COMMANDS | RETIRED_COMMANDS | AMBIGUOUS_NAMES
            or name.isdecimal()
            or re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", name)):
        raise HabitError("任务名称与系统命令、日期或编号冲突，请换一个名称。")
    return name
