"""AstrBot check-in plugin: group chats, private chats, and daily status.

All commands are registered natively and do not require an LLM.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .calendar_image import cache_file
from .store import HabitError, validate_day, validate_month
from .accounts import identity_aliases
from .scored_store import ScoredStore
from .scoring import (business_day, RENAMES, at_clock, validate_clock, validate_delay,
                      validate_weight, parse_backfill_time, fmt_number, fmt_percent, summarize)
from .panel import daily_panel
from .weighted_calendar import render_weighted, weighted_text
from .qq_transport import (QQTransport, app_id_of, platform_name, send_status, safe_error, OFFICIAL)


COMMAND_TOKEN = re.compile(r"(?:^|\s)[/／]?打卡(?=\s|$)")
HELP = """\U0001f331 \u4e60\u60ef\u6253\u5361 v1.6
/\u6253\u5361                  \u4eca\u65e5\u72b6\u6001\u3001\u5f97\u5206\u4e0e\u4ea4\u4e92\u6309\u94ae
/\u6253\u5361 \u7761\u89c9             \u5b8c\u6210\u4efb\u52a1\uff0c\u4fdd\u5b58\u65f6\u95f4\u5e76\u5237\u65b0
/\u6253\u5361 \u5de5\u4f5c\u4e00\u5c0f\u65f6 +1   \u8bb0\u5f55\u4e00\u6b21\uff08\u4e0d\u662f\u8ba1\u65f6\u5668\uff09
/\u6253\u5361 7                \u6309\u5e8f\u53f7\u5b8c\u6210\u4e00\u6b21
/\u6253\u5361 \u8bbe\u7f6e             \u67e5\u770b\u76ee\u6807\u3001\u65f6\u95f4\u3001\u5ef6\u8fdf\u3001\u6743\u91cd
/\u6253\u5361 \u65f6\u95f4 \u7761\u89c9 23:30 30  \u6700\u4f73\u65f6\u95f4\u548c\u5ef6\u8fdf\u7a97\u53e3\uff08\u5206\u949f\uff09
/\u6253\u5361 \u6700\u4f73 \u8d77\u5e8a 08:00    \u53ea\u4fee\u6539\u6700\u4f73\u65f6\u95f4
/\u6253\u5361 \u5ef6\u8fdf \u8d77\u5e8a 30       \u53ea\u4fee\u6539\u5ef6\u8fdf\u7a97\u53e3
/\u6253\u5361 \u65f6\u95f4 \u8fd0\u52a8 \u5173\u95ed      \u53d6\u6d88\u9650\u65f6\u8ba1\u5206
/\u6253\u5361 \u6743\u91cd \u8fd0\u52a8 2        \u8bbe\u7f6e\u8be5\u4efb\u52a1\u6574\u5929\u7684\u6743\u91cd
/\u6253\u5361 \u6743\u91cd \u8fd0\u52a8 0        \u6539\u4e3a\u53ef\u9009\uff0c\u4e0d\u5f71\u54cd\u5b8c\u6210\u5ea6
/\u6253\u5361 \u76ee\u6807 \u5de5\u4f5c\u4e00\u5c0f\u65f6 3    \u4fee\u6539\u6b21\u6570\uff1b\u4e0d\u5b58\u5728\u5219\u521b\u5efa
/\u6253\u5361 \u6dfb\u52a0 \u9605\u8bfb 1 0       \u65b0\u5efa\uff1a\u6b21\u6570 1\uff0c\u6743\u91cd 0
/\u6253\u5361 \u5220\u9664 \u9605\u8bfb           \u505c\u7528\uff1b\u5386\u53f2\u4fdd\u7559
/\u6253\u5361 \u6062\u590d \u9605\u8bfb           \u91cd\u65b0\u542f\u7528
/\u6253\u5361 \u64a4\u9500 \u7761\u89c9           \u64a4\u9500\u5f53\u65e5\u8be5\u9879\u5168\u90e8\u6253\u5361
/\u6253\u5361 \u8865\u7b7e \u7761\u89c9 \u6628\u5929 23:50 \u81ea\u62a5\u5b9e\u9645\u5b8c\u6210\u65f6\u95f4
/\u6253\u5361 \u8bb0\u5f55 [\u4efb\u52a1] [\u65e5\u671f] \u67e5\u770b\u5b8c\u6210\u548c\u64cd\u4f5c\u65f6\u95f4
/\u6253\u5361 \u6708\u5386 [\u4efb\u52a1] [YYYY-MM] \u52a0\u6743\u5b8c\u6210\u5ea6\u6708\u5386
/\u6253\u5361 \u6587\u5b57\u6708\u5386           \u4e0d\u53d1\u9001\u56fe\u7247
/\u6253\u5361 \u89c4\u5219               \u8ba1\u5206\u8bf4\u660e
/\u6253\u5361 \u4ea4\u4e92 \u6309\u94ae|\u6587\u5b57\u94fe|\u81ea\u52a8|\u7eaf\u6587\u5b57
/\u6253\u5361 \u8eab\u4efd | /\u6253\u5361 \u8bca\u65ad
\u597d\u53cb\u3001\u6392\u884c\u3001\u9690\u79c1\u3001\u7ed1\u5b9a\u548c\u63d0\u9192\u529f\u80fd\u4fdd\u7559\u3002
04:00 \u6362\u65e5\uff1b\u4fee\u6539\u89c4\u5219\u5f71\u54cd\u5f53\u5929\u53ca\u4ee5\u540e\uff0c\u4e0d\u6539\u5199\u5f80\u65e5\u3002"""

RULE_HELP = """\U0001f4d0 \u8ba1\u5206\u89c4\u5219
\u6bcf\u65e5\u8303\u56f4\uff1a04:00 \u81f3\u6b21\u65e5 03:59:59\uff08\u6309\u914d\u7f6e\u65f6\u533a\uff09\u3002
\u6700\u4f73\u65f6\u95f4\u4e4b\u524d\u548c\u7b2c\u4e00\u4e2a\u5ef6\u8fdf\u7a97\u53e3\u5185\uff1a100%\u3002
\u8d85\u51fa\u540e\u6bcf\u8fdb\u5165\u4e0b\u4e00\u4e2a\u7a97\u53e3\uff1a50%\u300125%\u300112.5%\u2026
\u4f8b\uff1a\u7761\u89c9 23:30 +30\u5206\uff0c\u81f3\u6b21\u65e5 00:00:00 \u4ecd\u6ee1\u5206\uff1b
00:00:01\uff5e00:30:00 \u4e3a50%\uff1b00:30:01\uff5e01:00:00 \u4e3a25%\u3002
\u4efb\u52a1\u5f97\u5206 = \u6743\u91cd \u00d7 \u5404\u6b21\u6253\u5361\u65f6\u95f4\u7cfb\u6570\u4e4b\u548c / \u76ee\u6807\u6b21\u6570\u3002
\u6b21\u6570\u8fbe\u6807\u540e\u4e0d\u518d\u52a0\u5206\uff1b\u591a\u6b21\u4efb\u52a1\u5168\u90e8\u5b8c\u6210\u5408\u8ba1\u53ea\u5360\u4e00\u4efd\u6743\u91cd\u3002
\u65e5\u5b8c\u6210\u5ea6 = \u5f53\u5929\u5f97\u5206\u5408\u8ba1 / \u5f53\u5929\u6709\u6548\u6743\u91cd\u5408\u8ba1\u3002
\u6708\u5b8c\u6210\u5ea6 = \u5df2\u7ecf\u8fc7\u7684\u6253\u5361\u65e5\u5f97\u5206\u5408\u8ba1 / \u5bf9\u5e94\u6709\u6548\u6743\u91cd\u5408\u8ba1\uff08\u542b\u4eca\u5929\uff09\u3002
\u6743\u91cd0\u53ef\u8bb0\u5f55\u4f46\u4e0d\u52a0\u5206\u3001\u4e0d\u62c9\u4f4e\u5b8c\u6210\u5ea6\uff1b\u5168\u90e8\u4e3a0\u663e\u793a--\u3002
\u672a\u542f\u7528\u65e5\u548c\u672a\u6765\u65e5\u4e0d\u8ba1\u5165\u6708\u5ea6\u5206\u6bcd\u3002
\u8bbe\u7f6e\u5f53\u5929\u751f\u6548\uff0c\u5f53\u5929\u5df2\u6709\u8bb0\u5f55\u4f1a\u91cd\u7b97\uff0c\u5f80\u65e5\u7528\u5386\u53f2\u89c4\u5219\u3002
\u5386\u53f2\u65e7\u6570\u636e\u4e0d\u8ffd\u6eaf\u6263\u5206\uff0c\u4e0d\u731c\u6d4b\u4e0d\u5b58\u5728\u7684\u5b8c\u6210\u65f6\u95f4\u3002
\u6253\u5361\u4e0e\u8865\u7b7e\u5747\u4e3a\u81ea\u62a5\uff0c\u4e0d\u9a8c\u8bc1\u662f\u5426\u771f\u7684\u7761\u89c9\u6216\u5de5\u4f5c\u4e00\u5c0f\u65f6\u3002"""


class GroupDaka(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        raw_tz = str(self.config.get("timezone", "Asia/Shanghai"))
        try:
            self.tz = ZoneInfo(raw_tz)
        except ZoneInfoNotFoundError:
            self.tz = ZoneInfo("Asia/Shanghai")
            logger.warning("群习惯打卡：时区无效，已回退到 Asia/Shanghai")
        self.max_habits = max(7, min(64, int(self.config.get("max_habits_per_group", 64))))
        self.max_backfill = max(0, min(365, int(self.config.get("max_backfill_days", 14))))
        self.allow_member_add = bool(self.config.get("allow_member_add", True))
        self.qq_buttons_enabled = bool(self.config.get("qq_buttons_enabled", True))
        self.interaction_mode = str(self.config.get("interaction_mode", "auto"))
        if self.interaction_mode not in ("auto", "keyboard", "links", "text"):
            self.interaction_mode = "auto"
        self._last_delivery = {}
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_daka"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.data_dir / "checkin.sqlite3"
        if db_path.is_file() and db_path.stat().st_size:
            with sqlite3.connect(db_path) as old:
                if old.execute("PRAGMA user_version").fetchone()[0] < 10600:
                    backup_dir = Path(get_astrbot_data_path()) / "backups" / "astrbot_plugin_daka"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup = backup_dir / f"pre-1.6-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3"
                    with sqlite3.connect(backup) as copy:
                        old.backup(copy)
                    logger.info("[daka 1.6] Pre-migration database snapshot: %s", backup)
        self.store = ScoredStore(db_path, self.tz, clock=lambda: self._now())
        with self.store._db() as conn:
            conn.execute("PRAGMA user_version=10600")
            conn.execute("CREATE TABLE IF NOT EXISTS ui_preferences ("
                         "scope TEXT NOT NULL, origin TEXT NOT NULL, mode TEXT NOT NULL, "
                         "PRIMARY KEY(scope, origin))")
        self._reminder_task: asyncio.Task | None = None

    def _today(self) -> date:
        return business_day(self._now())

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _time_label(self, raw: str | None, *, seconds: bool = False) -> str:
        if not raw:
            return ""
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is not None:
                dt = dt.astimezone(self.tz)
            return dt.strftime("%H:%M:%S" if seconds else "%H:%M")
        except (ValueError, TypeError, OverflowError):
            return "时间未知"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        # AstrBot administrators, NOT just QQ group moderators.
        return str(getattr(event, "role", "")).lower() == "admin"

    def _start_scheduler(self):
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(self._reminder_loop())

    @filter.on_astrbot_loaded()
    async def bot_loaded(self):
        """Initialize scheduled reminders after the bot is ready."""
        self._start_scheduler()

    async def _reminder_loop(self):
        try:
            while True:
                await asyncio.sleep(45)
                now = datetime.now(self.tz)
                for scope in self.store.due_reminders(now):
                    try:
                        chain = MessageChain().message(
                            f"🌱 {now:%Y-%m-%d} 习惯打卡提醒\n"
                            "今天的个人任务完成了吗？打卡日在凌晨 04:00 换日。\n"
                            "发送 /打卡 今日 查看进度，/打卡 清单 查看习惯。"
                        )
                        await self.context.send_message(scope, chain)
                        self.store.mark_reminder_sent(scope, now.date())
                    except Exception:
                        # QQ official bots can restrict proactive group messages.
                        logger.exception("群习惯打卡：向会话 %s 发送主动提醒失败", scope)
        except asyncio.CancelledError:
            return

    async def terminate(self):
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _extract_command(raw: str) -> str:
        """Accept either /打卡 ... or 打卡 ... after wake-prefix stripping.

        The core's native command matcher validates that this is actually our
        command, so unusual platform text formatting should not fall through
        to the LLM just because the plain text representation differs.
        """
        raw = str(raw or "").strip()
        found = COMMAND_TOKEN.search(raw)
        if found:
            return raw[found.end():].strip()
        return raw

    @staticmethod
    def _is_group_event(event: AstrMessageEvent) -> bool:
        kind = getattr(event, "get_message_type", None)
        if callable(kind):
            try:
                if "group" in str(kind()).lower():
                    return True
            except Exception:
                pass
        origin = str(getattr(event, "unified_msg_origin", ""))
        return len(origin.split(":")) >= 3 and "group" in origin.split(":", 2)[1].lower()

    def _daily_status(self, scope, sender, nick, today):
        return daily_panel(self.store.daily(scope, sender, today), nick, today, self._now(), self.tz)

    def _canonical_habit(self, scope, name):
        if name in RENAMES:
            try:
                self.store.habit(scope, name)
            except HabitError:
                try:
                    self.store.habit(scope, RENAMES[name])
                    return RENAMES[name]
                except HabitError:
                    pass
        return name

    def _history_day(self, raw: str | None, today: date) -> date:
        if raw in (None, "今天"):
            return today
        if raw == "昨天":
            return today - timedelta(days=1)
        if raw == "前天":
            return today - timedelta(days=2)
        try:
            day = date.fromisoformat(raw)
        except ValueError as exc:
            raise HabitError("记录日期用今天、昨天、前天或 YYYY-MM-DD。") from exc
        if day > today:
            raise HabitError("不能查询未来日期的打卡记录。")
        return day

    def _history_text(self, scope: str, sender: str, args: list[str], today: date) -> str:
        if len(args) > 2:
            raise HabitError("用法：/打卡 记录 [习惯名] [今天/昨天/YYYY-MM-DD]")
        name = None
        date_arg = None
        for arg in args:
            if arg in ("今天", "昨天", "前天") or re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
                if date_arg is not None:
                    raise HabitError("只能指定一个日期。")
                date_arg = arg
            elif name is None:
                name = arg
            else:
                raise HabitError("用法：/打卡 记录 [习惯名] [日期]")
        day = self._history_day(date_arg, today)
        if name is not None:
            name = self._canonical_habit(scope, name)
            if not self.store.has_history(scope, name):
                raise HabitError("Unknown habit")
        entries = self.store.checkin_times(scope, sender, day, name)
        lines = [f"⏱️ {day} 打卡时间" + (f" · {name}" if name else "")]
        if not entries:
            return "\n".join(lines + ["这一天没有相应的打卡记录。"])
        for e in entries[:50]:
            clock = self._time_label(e["checked_at"], seconds=True)
            if e["legacy"]:
                lines.append(f"• {e['name']} 旧数据最后更新时间约 {clock}（无法还原每次打卡时刻）")
            else:
                actual = e.get("occurred_at")
                if e["kind"] == "backfill":
                    detail = self._time_label(actual, seconds=True) if actual else "\u672a\u63d0\u4f9b"
                    lines.append(f"\u2022 {e['name']} +{e['delta']} \u5b8c\u6210 {detail}\uff08\u8865\u7b7e\uff09")
                    lines.append(f"  \u64cd\u4f5c\u65f6\u95f4\uff1a{e['checked_at']}")
                else:
                    stamp = datetime.fromisoformat(actual or e['checked_at'])
                    suffix = "\uff08\u6b21\u65e5\uff09" if stamp.date() > day else ""
                    lines.append(f"\u2022 {e['name']} +{e['delta']}  {clock}{suffix}")
        if len(entries) > 50:
            lines.append(f"……其余 {len(entries) - 50} 条，请指定习惯名称缩小范围。")
        return "\n".join(lines)

    def _status_after(self, message: str, scope: str, sender: str, nick: str, today: date) -> tuple[str, None]:
        return f"{message}\n\n{self._daily_status(scope, sender, nick, today)}", None

    @staticmethod
    def _union_id_from_event(event: AstrMessageEvent) -> str | None:
        """Use the official verified union_openid only when actually supplied."""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return None
        author = raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", None)
        union = (author.get("union_openid") if isinstance(author, dict)
                 else getattr(author, "union_openid", None))
        return str(union).strip() if union else None

    def _resolve_person(self, origin: str, sender: str, is_group: bool,
                        union_openid: str | None = None,
                        adapter_type: str | None = None,
                        app_id: str | None = None) -> str:
        aliases = identity_aliases(origin, sender, is_group, union_openid,
                                   adapter_type=adapter_type, app_id=app_id)
        return self.store.resolve_person(aliases, origin, sender)

    def _binding_command(self, command: str, origin: str, sender: str, is_group: bool,
                         union_openid: str | None = None,
                         nickname: str = "群友",
                         adapter_type: str | None = None,
                         app_id: str | None = None) -> str:
        """One-time proof of DM and group ownership; merge both histories."""
        parts = command.split()
        action, args = parts[0], parts[1:]
        if action == "解绑":
            if is_group:
                raise HabitError("请在私聊中发送 /打卡 解绑。")
            if args:
                raise HabitError("用法：/打卡 解绑")
            changed = self.store.unbind_private_link(origin, sender)
            return ("✅ 已取消私聊的默认群关联。个人习惯和历史打卡仍然统一，不会分裂数据。" if changed
                    else "当前私聊没有选定默认群；个人数据仍会按用户身份保存。")
        if is_group:
            if len(args) != 1:
                raise HabitError("请先私聊机器人发送 /打卡 绑定 获取临时码，再在群里发送 /打卡 绑定 绑定码。")
            # Redeem first to verify that this is an authorized user-controlled
            # link. Keep token spent even if a later step errors out.
            private = self.store.redeem_private_link_token(args[0], origin, sender)
            group_account = self._resolve_person(origin, sender, True, union_openid, adapter_type, app_id)
            private_account = self._resolve_person(
                private["private_origin"], private["private_user_id"], False,
                adapter_type=adapter_type, app_id=app_id
            )
            self.store.merge_persons(group_account, private_account)
            self.store.member_in_group(origin, group_account, nickname, sender)
            return ("✅ 身份绑定成功！群聊与私聊现在共用一份个人习惯和打卡记录。"
                    "\n两边旧数据都已合并；相同日期同项的历史次数取较大值，避免重复计算。"
                    "\n以后在私聊或群里发送 /打卡，都会看到更新后的个人状态。")
        if args:
            raise HabitError("请在私聊直接发送 /打卡 绑定，不需要额外参数。")
        token = self.store.issue_private_link_token(origin, sender)
        return ("🔐 你的 QQ 身份绑定码（10 分钟有效、一次性）：\n"
                f"{token}\n\n请【本人】在目标 QQ 群发送：\n/打卡 绑定 {token}\n"
                "绑定后会合并已有的群和私聊历史，而不是仅切换查看某个群。"
                "\n不要将绑定码私发给其他人。")

    def _ui_mode(self, scope: str, origin: str) -> str:
        with self.store._db() as conn:
            row = conn.execute('SELECT mode FROM ui_preferences WHERE scope=? AND origin=?',
                               (scope, origin)).fetchone()
        mode = row['mode'] if row else self.interaction_mode
        if not self.qq_buttons_enabled and mode in ('auto', 'keyboard'):
            mode = 'links'
        return mode

    def _set_ui_mode(self, scope: str, origin: str, mode: str):
        with self.store._db() as conn:
            conn.execute('INSERT OR REPLACE INTO ui_preferences(scope,origin,mode) VALUES (?,?,?)',
                         (scope, origin, mode))

    async def _send_plain(self, event, reply: str):
        # Explicit send before stop_event. No yield chain that can be closed
        # after the first response by AstrBot's after_message_sent hook.
        chain = MessageChain().message(reply)
        chain.use_markdown_ = False
        await event.send(chain)

    async def _send_calendar(self, event, reply: str, img: str | None):
        if img:
            try:
                if platform_name(event) in OFFICIAL:
                    transport = QQTransport(event)
                    caption = reply.split('\n', 1)[0] + '\n' + '\u52a0\u6743\u5b8c\u6210\u5ea6\uff08\u542b\u8fdf\u5230\u6263\u5206\uff09\uff1b04:00 \u6362\u65e5\u3002'
                    await transport.image(img, caption)
                    self._last_delivery[str(event.unified_msg_origin)] = {'mode': 'image', 'ack': True}
                    logger.info('[daka 1.6] Calendar uploaded and media message accepted')
                else:
                    await event.send(MessageChain().message(reply.split('\n', 1)[0]).file_image(img))
                return
            except Exception as exc:
                reason = safe_error(exc)
                logger.warning('[daka 1.6] Calendar image failed: %s', reason)
                self._last_delivery[str(event.unified_msg_origin)] = {'mode': 'calendar-text', 'errors': [reason]}
                reply = '⚠️ 月历图片发送失败，以下为文字月历。\n' + reply
        await self._send_plain(event, reply)

    @filter.command("打卡", priority=10)
    async def daka_command(self, event: AstrMessageEvent):
        """One coroutine: resolve account, persist action, send replies, then stop."""
        self._start_scheduler()
        origin = str(event.unified_msg_origin)
        raw_sender = str(event.get_sender_id())
        nick = str(event.get_sender_name() or "我")
        is_group = self._is_group_event(event)
        command = self._extract_command(str(event.message_str or ""))
        adapter_type = platform_name(event)
        app_id = app_id_of(event) if adapter_type in OFFICIAL else None
        union_openid = self._union_id_from_event(event)
        sender, scope = self.store.SELF, ''
        reply, img = None, None
        calendar_request = False
        logger.info('[daka 1.6] scene=%s command=%s', 'group' if is_group else 'c2c',
                    command.split(' ', 1)[0] if command else 'today')
        try:
            try:
                account_id = self._resolve_person(origin, raw_sender, is_group, union_openid,
                                                   adapter_type, app_id)
                social_scope = origin if is_group else None
                if is_group:
                    self.store.remember_group_identity(origin, adapter_type, app_id)
                    self.store.member_in_group(origin, account_id, nick, raw_sender)
                else:
                    linked = self.store.get_private_link(origin, raw_sender)
                    if linked is not None:
                        social_scope = linked['group_scope']
                        group_account = self._resolve_person(linked['group_scope'], linked['group_user_id'],
                                                             True, adapter_type=adapter_type, app_id=app_id)
                        account_id = self.store.merge_persons(group_account, account_id)
                scope = self.store.person_scope(account_id)
                parts = command.split()
                action = parts[0] if parts else ''
                code = hashlib.sha256(account_id.encode()).hexdigest()[:10]
                uid_code = hashlib.sha256(raw_sender.encode()).hexdigest()[:10]
                if action in ('绑定', '解绑'):
                    reply = self._binding_command(command, origin, raw_sender, is_group,
                                                  union_openid, nick, adapter_type, app_id)
                elif command in ('身份', '账户', '绑定状态'):
                    reply = (f'👤 个人账户：{code}\n'
                             f'发送者指纹：{uid_code}\n'
                             f'接入：{adapter_type}\n'
                             '同一机器人下相同 OpenID 自动共用账户，群号不再参与个人数据分区。\n'
                             '只有发送者指纹不同时，才需用 /打卡 绑定 确认身份。')
                elif action == '交互':
                    modes = {'自动': 'auto', '按钮': 'keyboard', '文字链': 'links',
                             '纯文字': 'text'}
                    if len(parts) != 2 or parts[1] not in modes:
                        raise HabitError('用法：/打卡 交互 自动|按钮|文字链|纯文字')
                    self._set_ui_mode(scope, origin, modes[parts[1]])
                    reply = f'✅ 本会话交互模式：{parts[1]}\n\n' + self._daily_status(scope, sender, nick, self._today())
                elif command in ('按钮诊断', '按钮状态', '诊断'):
                    report = self._last_delivery.get(origin, {})
                    try:
                        transport = QQTransport(event)
                        api_state = transport.ctx.scene + ' / bot.api available'
                    except Exception as exc:
                        api_state = safe_error(exc)
                    reply = (f'🔍 打卡诊断 v1.6.0\n账户：{code}\n'
                             f'发送者指纹：{uid_code}\n'
                             f'模式：{self._ui_mode(scope, origin)}\n'
                             f'接口：{api_state}\n'
                             f'最近发送：{report.get("mode", "not tried")}\n'
                             f'接口确认：{report.get("ack", False)}\n'
                             f'错误：{"; ".join(report.get("errors", [])) or "none"}\n'
                             '接口接受不代表 QQ 客户端已显示按钮。\n'
                             '群聊指令按钮填入后需手动发送；私聊才使用自动发送。\n'
                             '可切换：/打卡 交互 文字链')
                else:
                    text_calendar = action == '文字月历'
                    if text_calendar:
                        command = '月历' + command[len(action):]
                    calendar_request = command.split(' ', 1)[0] in ('月历', '好友')
                    reply, img = self._dispatch(command, scope, sender, nick,
                                                self._is_admin(event), is_group,
                                                social_scope, account_id)
                    if text_calendar:
                        img = None
            except HabitError as exc:
                reply, img = f'⚠️ {exc}', None
                calendar_request = False
            except Exception:
                logger.exception('[daka 1.6] Command processing failed')
                reply, img = '⚠️ 处理失败，请查看 AstrBot 日志。', None
                calendar_request = False

            if calendar_request:
                await self._send_calendar(event, reply, img)
            elif reply and '今日打卡' in reply and adapter_type in OFFICIAL:
                try:
                    report = await send_status(event, reply,
                                               self.store.daily(scope, sender, self._today()),
                                               self._ui_mode(scope, origin), logger.info)
                    self._last_delivery[origin] = report
                except Exception as exc:
                    reason = safe_error(exc)
                    self._last_delivery[origin] = {'mode': 'astrbot-text-fallback', 'errors': [reason]}
                    logger.warning('[daka 1.6] Interactive send failed: %s', reason)
                    await self._send_plain(event, reply)
            elif reply:
                await self._send_plain(event, reply)
        finally:
            # All explicit sends have already happened at this point.
            event.stop_event()

    def _dispatch(
        self, command: str, scope: str, sender: str, nick: str, is_admin: bool,
        is_group: bool = True, group_scope: str | None = None,
        account_id: str | None = None,
    ) -> tuple[str, str | None]:
        parts = command.split()
        today = self._today()
        if not parts:
            return self._daily_status(scope, sender, nick, today), None
        if parts[0] in ("帮助", "help", "?"):
            return HELP, None
        action, *args = parts

        if action == "\u89c4\u5219":
            return RULE_HELP, None
        if action in ("\u6e05\u5355", "\u4e60\u60ef", "\u8bbe\u7f6e"):
            habits = self.store.list_habits(scope)
            if args:
                if len(args) != 1:
                    raise HabitError("\u7528\u6cd5\uff1a/\u6253\u5361 \u8bbe\u7f6e [\u4efb\u52a1]")
                habits = [self.store.habit(scope, self._canonical_habit(scope, args[0]))]
            lines = ["\u2699\ufe0f \u4e2a\u4eba\u4efb\u52a1\u8bbe\u7f6e \u00b7 04:00 \u6362\u65e5"]
            for index, h in enumerate(habits, 1):
                timed = f"\u6700\u4f73{h['best_time']} \u00b7 \u5ef6\u8fdf{h['delay_minutes']}\u5206" if h['best_time'] else "\u4e0d\u9650\u65f6"
                lines.append(f"{index}. {h['name']} \u00b7 {h['target']}\u6b21/\u65e5 \u00b7 \u6743\u91cd{fmt_number(h['weight'])}\n   {timed}")
            lines.append("\n/\u6253\u5361 \u65f6\u95f4 \u7761\u89c9 23:30 30\n/\u6253\u5361 \u6743\u91cd \u8fd0\u52a8 0\n/\u6253\u5361 \u89c4\u5219")
            lines.append("\u4fee\u6539\u5f71\u54cd\u5f53\u5929\u53ca\u4ee5\u540e\uff0c\u4e0d\u6539\u5199\u5f80\u65e5\u3002")
            return "\n".join(lines), None

        schedule = action in ("\u6700\u4f73", "\u5ef6\u8fdf", "\u6743\u91cd", "\u65f6\u6bb5") or (action == "\u65f6\u95f4" and len(args) >= 2 and
                   (":" in args[1] or args[1] == "\u5173\u95ed"))
        if schedule:
            if len(args) < 2:
                raise HabitError("\u7528\u6cd5\uff1a/\u6253\u5361 \u65f6\u95f4 \u7761\u89c9 23:30 30\uff1b/\u6253\u5361 \u6743\u91cd \u8fd0\u52a8 1")
            name = self._canonical_habit(scope, args[0])
            if action == "\u6743\u91cd" and len(args) == 2:
                self.store.configure(scope, name, weight=validate_weight(args[1]))
            elif action == "\u5ef6\u8fdf" and len(args) == 2:
                self.store.configure(scope, name, delay_minutes=validate_delay(args[1]))
            elif action == "\u6700\u4f73" and len(args) == 2:
                self.store.configure(scope, name, best_time=validate_clock(args[1]))
            elif action in ("\u65f6\u95f4", "\u65f6\u6bb5") and args[1] == "\u5173\u95ed" and len(args) == 2:
                self.store.configure(scope, name, best_time=None)
            elif action in ("\u65f6\u95f4", "\u65f6\u6bb5") and len(args) in (2, 3):
                self.store.configure(scope, name, best_time=validate_clock(args[1]),
                                     delay_minutes=validate_delay(args[2]) if len(args) == 3 else None)
            else:
                raise HabitError("\u53c2\u6570\u6570\u91cf\u4e0d\u6b63\u786e\uff0c\u8bf7\u53d1\u9001 /\u6253\u5361 \u5e2e\u52a9\u3002")
            return self._status_after(f"\u2705 \u5df2\u66f4\u65b0\u300c{name}\u300d\uff0c\u5f53\u5929\u53ca\u4ee5\u540e\u751f\u6548\u3002", scope, sender, nick, today)

        if action == "\u6062\u590d":
            if len(args) != 1:
                raise HabitError("\u7528\u6cd5\uff1a/\u6253\u5361 \u6062\u590d \u4efb\u52a1\u540d")
            self.store.restore_habit(scope, args[0])
            return self._status_after("\u2705 \u5df2\u6062\u590d\u4efb\u52a1\uff0c\u4fdd\u7559\u539f\u6709\u5386\u53f2\u3002", scope, sender, nick, today)

        if action in ("今日", "今天", "进度"):
            return self._daily_status(scope, sender, nick, today), None

        if action in ("记录", "时间", "历史时间"):
            return self._history_text(scope, sender, args, today), None

        if action == "添加":
            if not self.allow_member_add and not is_admin:
                raise HabitError("管理员关闭了普通用户添加习惯的权限。")
            if not 1 <= len(args) <= 3:
                raise HabitError("\u7528\u6cd5\uff1a/\u6253\u5361 \u6dfb\u52a0 \u4efb\u52a1 [\u6b21\u6570] [\u6743\u91cd]")
            target = self._positive_int(args[1]) if len(args) >= 2 else 1
            weight = validate_weight(args[2]) if len(args) == 3 else 1.0
            self.store.add_habit(scope, args[0], target, sender, self.max_habits)
            self.store.configure(scope, args[0], weight=weight)
            return self._status_after(f"✅ 已添加习惯「{args[0]}」，每日目标 {target} 次。", scope, sender, nick, today)

        if action == "删除":
            if len(args) != 1:
                raise HabitError("用法：/打卡 删除 运动")
            self.store.delete_habit(scope, self._canonical_habit(scope, args[0]), sender, is_admin)
            return self._status_after(f"✅ 已停用「{args[0]}」，旧打卡记录仍保留在数据库中。", scope, sender, nick, today)

        if action == "目标":
            if len(args) != 2:
                raise HabitError("用法：/打卡 目标 运动 3")
            target = self._positive_int(args[1])
            name = self._canonical_habit(scope, args[0])
            if self.store.has_history(scope, name):
                self.store.configure(scope, name, target=target, enabled=1)
                msg = f"\u2705 \u300c{name}\u300d\u6bcf\u65e5\u76ee\u6807 {target} \u6b21\u3002"
            else:
                if not self.allow_member_add and not is_admin:
                    raise HabitError("\u7ba1\u7406\u5458\u5173\u95ed\u4e86\u666e\u901a\u7528\u6237\u6dfb\u52a0\u4e60\u60ef\u7684\u6743\u9650\u3002")
                self.store.add_habit(scope, name, target, sender, self.max_habits)
                msg = f"\u2705 \u5df2\u521b\u5efa\u300c{name}\u300d\uff0c\u6bcf\u65e5 {target} \u6b21\uff0c\u6743\u91cd 1\u3002"
            return self._status_after(msg, scope, sender, nick, today)

        if action in ("撤销", "取消"):
            if not 1 <= len(args) <= 2:
                raise HabitError("用法：/打卡 撤销 早起 [昨天/日期]")
            selected = validate_day(args[1] if len(args) == 2 else None, today, self.max_backfill)
            changed = self.store.undo(scope, sender, self._canonical_habit(scope, args[0]), selected)
            message = (f"↩️ 已清除 {selected} 的「{args[0]}」记录。" if changed
                       else "这一天还没有这个习惯的打卡记录。")
            return self._status_after(message, scope, sender, nick, today)

        if action == "\u8865\u7b7e":
            if not 2 <= len(args) <= 4:
                raise HabitError("\u7528\u6cd5\uff1a/\u6253\u5361 \u8865\u7b7e \u7761\u89c9 \u6628\u5929 23:50 [\u6b21\u6570]")
            name = self._canonical_habit(scope, args[0])
            selected = validate_day(args[1], today, self.max_backfill)
            amount, occurred = 1, None
            seen_count = False
            for token in args[2:]:
                if ":" in token and occurred is None:
                    occurred = parse_backfill_time(selected, token, self.tz, self._now())
                elif not seen_count and ":" not in token:
                    amount = self._positive_int(token.removeprefix("+"))
                    seen_count = True
                else:
                    raise HabitError("\u8865\u7b7e\u53c2\u6570\u91cd\u590d\u6216\u4e0d\u6b63\u786e\u3002")
            return self._perform_checkin(scope, sender, name, selected, amount, nick, today,
                                         occurred_at=occurred, explicit_backfill=True)

        if action == "隐私":
            if len(args) != 1 or args[0] not in ("隐藏", "公开"):
                raise HabitError("用法：/打卡 隐私 隐藏 或 /打卡 隐私 公开")
            public = args[0] == "公开"
            if group_scope is None or account_id is None:
                raise HabitError("隐私设置请在群聊或已绑定群的私聊中执行。")
            self.store.set_visible(group_scope, account_id, public)
            return ("🔒 已隐藏：好友查询和月度排行榜不再显示你的记录；"
                    "但你自己发出的打卡群消息仍然可见。" if not public else
                    "🔓 已公开：好友可以查看你的打卡月历，排行榜会重新计入你。"), None

        if action in ("排行", "排行榜"):
            if group_scope is None:
                raise HabitError("排行榜只在群聊或已绑定群的私聊中可查看。")
            if len(args) > 1:
                raise HabitError("用法：/打卡 排行 [YYYY-MM]")
            year, month = validate_month(args[0] if args else None, today)
            self.store.upgrade_group_members(group_scope)
            members = self.store.group_rankings(group_scope, year, month)
            if not members:
                return f"📊 {year}-{month:02d} 还没有公开的已完成打卡记录。", None
            lines = [f"🏆 {year}-{month:02d} 群友打卡排行榜", "\u6309\u6708\u5ea6\u52a0\u6743\u5b8c\u6210\u5ea6\u6392\u5e8f\uff08\u542b\u8fdf\u5230\u6263\u5206\uff09"]
            for i, member in enumerate(members[:20], start=1):
                lines.append(f"{i}. {member['display_name']}  {fmt_percent(member['ratio'])}  ({fmt_number(member['earned'])}/{fmt_number(member['weight'])})")
            return "\n".join(lines), None

        if action == "月历":
            if len(args) > 2:
                raise HabitError("用法：/打卡 月历 [习惯名] [YYYY-MM]")
            year, month, chosen = self._parse_calendar_args(args, today)
            return self._make_calendar(scope, sender, nick, chosen, year, month, today)

        if action == "好友":
            if not 1 <= len(args) <= 3:
                raise HabitError("用法：/打卡 好友 昵称 [习惯名] [YYYY-MM]")
            if group_scope is None:
                raise HabitError("好友记录只在群聊或已绑定群的私聊中可查看。")
            self.store.upgrade_group_members(group_scope)
            friend = self.store.group_friend(group_scope, args[0].removeprefix("@"))
            year, month, chosen = self._parse_calendar_args(args[1:], today)
            return self._make_calendar(
                self.store.person_scope(friend["user_id"]), self.store.SELF,
                friend["display_name"], chosen, year, month, today
            )

        if action == "提醒":
            if not is_group:
                raise HabitError("请在群聊设置提醒。")
            if not is_admin:
                raise HabitError("只有 AstrBot 管理员可以设置群定时提醒。")
            if len(args) != 1:
                raise HabitError("用法：/打卡 提醒 21:30")
            self.store.set_reminder(group_scope, args[0])
            return (f"⏰ 已设置本群每日 {args[0]} 习惯提醒（{self.tz.key}）。\n"
                    "提示：QQ 官方机器人可能限制主动群消息，建议先观察实际推送。"), None

        if action == "关闭提醒":
            if not is_group:
                raise HabitError("请在群聊关闭提醒。")
            if not is_admin:
                raise HabitError("只有 AstrBot 管理员可以关闭群定时提醒。")
            self.store.disable_reminder(group_scope)
            return "⏰ 已关闭本群的习惯提醒。", None

        # Dynamic name OR a numeric quick-action shown by /打卡.
        if action.isascii() and action.isdecimal():
            index = int(action)
            habits = self.store.list_habits(scope)
            if index < 1 or index > len(habits):
                raise HabitError("序号超出范围，请发送 /打卡 查看对应习惯。")
            action = habits[index - 1]["name"]

        # Dynamic named habit e.g. /打卡 早睡 or /打卡 喝水 +2
        if len(args) > 2:
            raise HabitError("用法：/打卡 习惯名 [+次数] [今天/昨天/日期]")
        amount, day_arg = 1, None
        for value in args:
            if re.fullmatch(r"\+\d+", value):
                amount = self._positive_int(value[1:])
            elif day_arg is None:
                day_arg = value
            else:
                raise HabitError("打卡参数格式不正确。")
        selected = validate_day(day_arg, today, self.max_backfill)
        return self._perform_checkin(scope, sender, self._canonical_habit(scope, action), selected, amount, nick, today)

    @staticmethod
    def _positive_int(raw: str) -> int:
        try:
            result = int(raw)
        except ValueError as exc:
            raise HabitError("次数必须是整数。") from exc
        if not 1 <= result <= 100:
            raise HabitError("次数必须在 1～100 之间。")
        return result

    def _parse_calendar_args(self, args: list[str], today: date) -> tuple[int, int, str | None]:
        chosen = None
        month_text = None
        for val in args:
            if re.fullmatch(r"\d{4}-\d{2}", val):
                if month_text is not None:
                    raise HabitError("只能指定一个月份。")
                month_text = val
            elif chosen is None:
                chosen = val if val not in ("全部", "总览") else None
            else:
                raise HabitError("用法：/打卡 月历 [习惯名] [YYYY-MM]")
        year, month = validate_month(month_text, today)
        return year, month, chosen

    def _perform_checkin(self, scope, sender, name, day, amount, nick, today,
                         occurred_at=None, explicit_backfill=False):
        habit = self.store.rule_for_day(scope, name, day)
        if habit["target"] == 1 and amount != 1:
            raise HabitError(f"\u300c{name}\u300d\u662f\u6bcf\u65e5\u4e00\u6b21\u7684\u4efb\u52a1\u3002")
        when = self._now()
        backfill = explicit_backfill or day != today
        result = self.store.checkin(scope, sender, name, day, amount, checked_at=when,
                                    occurred_at=occurred_at,
                                    kind="backfill" if backfill else "normal")
        if result["changed"]:
            reply = f"\u2705 \u300c{name}\u300d{result['new']}/{result['target']} \u00b7 \u64cd\u4f5c {when:%H:%M:%S}"
            if backfill:
                actual = (occurred_at.strftime("%Y-%m-%d %H:%M:%S") if occurred_at else "\u672a\u63d0\u4f9b\uff08\u975e\u9650\u65f6\u4efb\u52a1\uff09")
                reply += f"\n\u8865\u7b7e\u6253\u5361\u65e5 {day}\uff1b\u81ea\u62a5\u5b8c\u6210\u65f6\u95f4 {actual}"
        else:
            reply = f"\u2705 \u300c{name}\u300d\u5df2\u8fbe\u5230 {result['target']} \u6b21\uff0c\u4e0d\u91cd\u590d\u8ba1\u5206\u6216\u6539\u5199\u65f6\u95f4\u3002"
        return self._status_after(reply, scope, sender, nick, today)

    def _make_calendar(self, scope, user_id, name, habit_name, year, month, today):
        if habit_name:
            habit_name = self._canonical_habit(scope, habit_name)
        days, stats = self.store.month_scores(scope, user_id, year, month, today, habit_name)
        text = weighted_text(year, month, days, stats, today, habit_name)
        path = cache_file(self.data_dir / "cache", scope, user_id,
                          f"{year}-{month:02d}", "weighted-" + (habit_name or "all"))
        try:
            image = render_weighted(path, year, month, days, stats, today, name, habit_name)
        except Exception as exc:
            logger.warning('[daka 1.6] Calendar render failed: %s', safe_error(exc))
            image = None
        return text, image
