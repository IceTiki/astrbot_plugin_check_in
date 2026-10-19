"""AstrBot check-in plugin: group chats, private chats, and daily status.

All commands are registered natively and do not require an LLM.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import hmac
import secrets
import time
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
from .help_content import HELP_TEXT
from .help_image import render_help_image
from .daily_image import render_daily_panel
from .store import HabitError, validate_day, validate_month
from .accounts import identity_aliases
from .scored_store import ScoredStore
from .task_maintenance import (inspect_task, rename_task, purge_task,
                               backup_before_edit)
from .scoring import (business_day, RENAMES, at_clock, validate_clock, validate_delay,
                      validate_weight, parse_backfill_time, fmt_number, fmt_percent, summarize)
from .panel import daily_panel
from .weighted_calendar import render_weighted, weighted_text
from .qq_transport import (QQTransport, app_id_of, platform_name, send_status, safe_error, OFFICIAL)


COMMAND_TOKEN = re.compile(r"(?:^|\s)[/／]?打卡(?=\s|$)")
PERSONAL_PREF_ORIGIN = "__person__"
from .command_names import RESERVED_COMMANDS, RETIRED_COMMANDS, validate_task_name


RULE_HELP = """📐 连续半衰期计分（v1.7）
打卡日：04:00 至次日 03:59:59（配置时区）。
提前或准时完成得100%；超过最佳时间后立即连续下降。
每经过一个半衰期H，剩余得分降到一半；晚2H得25%，晚3H得12.5%。
时间系数 = 2 ** (-迟到分钟 / H)，迟到分钟取 max(0, 实际时间-最佳时间)。
例：睡觉最佳23:30、H=60分钟：23:45 得84.1%，次日00:30得50%，次日01:30得25%。
任务得分 = 权重 × 目标次数内各次打卡时间系数之和 / 目标次数。
每日完成度=当天累计得分/当天有效权重，月完成度按已计入日期累计。
多次任务各次独立计分；权重为0可记录但不影响总分。
任务达标后额外打卡不增加次数、不覆盖完成时间。
修改设置从当前打卡日起生效，以前完成的日期继续沿用旧规则：
v1.6 已结束日期使用原阶梯式扣分；历史旧数据不猜测不存在的时间。
命令：/打卡 任务 时间 睡觉 23:30 30；/打卡 任务 权重 阅读 0。
此功能只记录自报完成时间，不验证真实睡眠或工作。"""


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
        self.daily_display_mode = str(self.config.get("daily_display_mode", "image")).lower()
        if self.daily_display_mode not in ("image", "text"):
            self.daily_display_mode = "image"
        self.month_display_mode = str(self.config.get("month_display_mode", "image")).lower()
        if self.month_display_mode not in ("image", "text"):
            self.month_display_mode = "image"
        self.help_display_mode = str(self.config.get("help_display_mode", "image")).lower()
        if self.help_display_mode not in ("image", "text"):
            self.help_display_mode = "image"
        self._last_delivery = {}
        self._debug_pending = {}
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_daka"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.data_dir / "checkin.sqlite3"
        if db_path.is_file() and db_path.stat().st_size:
            with sqlite3.connect(db_path) as old:
                if old.execute("PRAGMA user_version").fetchone()[0] < 10901:
                    backup_dir = Path(get_astrbot_data_path()) / "backups" / "astrbot_plugin_daka"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup = backup_dir / f"pre-1.9.1-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3"
                    with sqlite3.connect(backup) as copy:
                        old.backup(copy)
                    logger.info("[daka 1.9.1] Pre-upgrade database snapshot: %s", backup)
        self.store = ScoredStore(db_path, self.tz, clock=lambda: self._now())
        with self.store._db() as conn:
            conn.execute("PRAGMA user_version=10901")
            conn.execute("CREATE TABLE IF NOT EXISTS ui_preferences ("
                         "scope TEXT NOT NULL, origin TEXT NOT NULL, mode TEXT NOT NULL, "
                         "PRIMARY KEY(scope, origin))")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ui_preferences)")}
            if "daily_view_mode" not in columns:
                conn.execute("ALTER TABLE ui_preferences ADD COLUMN daily_view_mode TEXT")
            if "month_view_mode" not in columns:
                conn.execute("ALTER TABLE ui_preferences ADD COLUMN month_view_mode TEXT")
            if "help_view_mode" not in columns:
                conn.execute("ALTER TABLE ui_preferences ADD COLUMN help_view_mode TEXT")
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
                            "发送 /打卡 查看进度，/打卡 任务 查看习惯。"
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

    def _ensure_person_preferences(self, scope: str, origin: str):
        """Move old origin-scoped values to one per-account row, on first access.

        Old rows remain untouched for rollback. If group/DM preferences differ,
        the conversation that first opens v1.9 wins, deterministically.
        """
        with self.store._db() as conn:
            if conn.execute(
                "SELECT 1 FROM ui_preferences WHERE scope=? AND origin=?",
                (scope, PERSONAL_PREF_ORIGIN),
            ).fetchone():
                return
            old = conn.execute(
                "SELECT mode,daily_view_mode,month_view_mode,help_view_mode "
                "FROM ui_preferences WHERE scope=? AND origin=?",
                (scope, origin),
            ).fetchone()
            if old is None:
                old = conn.execute(
                    "SELECT mode,daily_view_mode,month_view_mode,help_view_mode "
                    "FROM ui_preferences WHERE scope=? AND origin<>? "
                    "ORDER BY rowid LIMIT 1",
                    (scope, PERSONAL_PREF_ORIGIN),
                ).fetchone()
            values = tuple(old) if old else (self.interaction_mode, None, None, None)
            conn.execute(
                "INSERT OR IGNORE INTO ui_preferences("
                "scope,origin,mode,daily_view_mode,month_view_mode,help_view_mode) "
                "VALUES (?,?,?,?,?,?)",
                (scope, PERSONAL_PREF_ORIGIN, *values),
            )

    def _preferences(self, scope: str, origin: str):
        self._ensure_person_preferences(scope, origin)
        with self.store._db() as conn:
            return conn.execute(
                "SELECT * FROM ui_preferences WHERE scope=? AND origin=?",
                (scope, PERSONAL_PREF_ORIGIN),
            ).fetchone()

    def _display_mode(self, scope: str, origin: str, kind: str) -> str:
        columns = {"daily": "daily_view_mode", "month": "month_view_mode",
                   "help": "help_view_mode"}
        defaults = {"daily": self.daily_display_mode, "month": self.month_display_mode,
                    "help": self.help_display_mode}
        if kind not in columns:
            raise ValueError("unknown display mode")
        pref = self._preferences(scope, origin)
        value = pref[columns[kind]]
        return value if value in ("image", "text") else defaults[kind]

    def _set_display_mode(self, scope: str, origin: str, kind: str, mode: str):
        columns = {"daily": "daily_view_mode", "month": "month_view_mode",
                   "help": "help_view_mode"}
        if kind not in columns or mode not in ("image", "text"):
            raise ValueError("invalid display preference")
        self._ensure_person_preferences(scope, origin)
        with self.store._db() as conn:
            conn.execute(
                f"UPDATE ui_preferences SET {columns[kind]}=? "
                "WHERE scope=? AND origin=?",
                (mode, scope, PERSONAL_PREF_ORIGIN),
            )

    def _reset_preferences(self, scope: str, origin: str):
        self._ensure_person_preferences(scope, origin)
        with self.store._db() as conn:
            conn.execute(
                "UPDATE ui_preferences SET mode=?,daily_view_mode=NULL,"
                "month_view_mode=NULL,help_view_mode=NULL "
                "WHERE scope=? AND origin=?",
                (self.interaction_mode, scope, PERSONAL_PREF_ORIGIN),
            )

    def _make_daily_image(self, scope, sender, nick, today):
        items = self.store.daily(scope, sender, today)
        path = cache_file(self.data_dir / "cache", scope, sender, today.isoformat(), "daily-v180")
        try:
            return render_daily_panel(path, items, nick, today, self._now(), self.tz)
        except Exception as exc:
            logger.warning("[daka 1.8] Daily image render failed: %s", safe_error(exc))
            return None

    def _daily_caption(self, reply, items, today):
        stats = summarize(items)
        prefix = str(reply).split("\n\n", 1)[0].splitlines()[0][:100]
        summary = (f"{fmt_percent(stats['ratio'])} · {fmt_number(stats['earned'])}/"
                   f"{fmt_number(stats['weight'])} · {stats['required_finished']}/"
                   f"{stats['required_total']} 项")
        return (prefix + "\n" + summary)[:580]

    async def _send_daily_image(self, event, reply, img, scope, sender, nick, today):
        items = self.store.daily(scope, sender, today)
        stats = summarize(items)
        caption = self._daily_caption(reply, items, today)
        origin = str(event.unified_msg_origin)
        adapter_type = platform_name(event)
        if adapter_type in OFFICIAL:
            transport = QQTransport(event)
            try:
                await transport.image(img, caption)
            except Exception as exc:
                reason = safe_error(exc)
                logger.warning("[daka 1.8] Daily image upload failed: %s", reason)
                self._last_delivery[origin] = {"mode": "text-fallback", "errors": [reason]}
                try:
                    report = await send_status(event, reply, items, self._ui_mode(scope, origin),
                                               logger.info, transport=transport)
                    self._last_delivery[origin] = report
                except Exception as send_exc:
                    logger.warning("[daka 1.8] Daily text fallback failed: %s", safe_error(send_exc))
                    await self._send_plain(event, reply)
                return

            short = (f"{fmt_percent(stats['ratio'])} · {fmt_number(stats['earned'])}/"
                     f"{fmt_number(stats['weight'])} 分 · {stats['required_finished']}/"
                     f"{stats['required_total']} 项\n"
                     "点下方按钮打卡；文字版 /打卡 视图 今日 文字")
            try:
                report = await send_status(event, short, items, self._ui_mode(scope, origin),
                                           logger.info, transport=transport)
                report["image"] = "accepted"
                self._last_delivery[origin] = report
            except Exception as exc:
                reason = safe_error(exc)
                logger.warning("[daka 1.8] Daily button message failed: %s", reason)
                self._last_delivery[origin] = {"mode": "image+text", "errors": [reason]}
                await self._send_plain(event, short)
            return

        try:
            await event.send(MessageChain().message(caption).file_image(img))
            self._last_delivery[origin] = {"mode": "image", "ack": True}
        except Exception as exc:
            reason = safe_error(exc)
            logger.warning("[daka 1.8] Daily image send failed: %s", reason)
            self._last_delivery[origin] = {"mode": "text-fallback", "errors": [reason]}
            await self._send_plain(event, reply)

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
                raise HabitError("请在私聊中发送 /打卡 账户 解绑。")
            if args:
                raise HabitError("用法：/打卡 账户 解绑")
            changed = self.store.unbind_private_link(origin, sender)
            return ("✅ 已取消私聊的默认群关联。个人习惯和历史打卡仍然统一，不会分裂数据。" if changed
                    else "当前私聊没有选定默认群；个人数据仍会按用户身份保存。")
        if is_group:
            if len(args) != 1:
                raise HabitError("请先私聊发送 /打卡 账户 绑定，再在群里发送 /打卡 账户 绑定 绑定码。")
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
            raise HabitError("请在私聊直接发送 /打卡 账户 绑定，不需要额外参数。")
        token = self.store.issue_private_link_token(origin, sender)
        return ("🔐 你的 QQ 身份绑定码（10 分钟有效、一次性）：\n"
                f"{token}\n\n请【本人】在目标 QQ 群发送：\n/打卡 账户 绑定 {token}\n"
                "绑定后会合并已有的群和私聊历史，而不是仅切换查看某个群。"
                "\n不要将绑定码私发给其他人。")

    def _ui_mode(self, scope: str, origin: str) -> str:
        pref = self._preferences(scope, origin)
        mode = pref['mode']
        if mode not in ('auto', 'keyboard', 'links', 'text'):
            mode = self.interaction_mode
        if not self.qq_buttons_enabled and mode in ('auto', 'keyboard'):
            mode = 'links'
        return mode

    def _set_ui_mode(self, scope: str, origin: str, mode: str):
        if mode not in ('auto', 'keyboard', 'links', 'text'):
            raise ValueError('invalid interaction mode')
        self._ensure_person_preferences(scope, origin)
        with self.store._db() as conn:
            # UPDATE instead of REPLACE: do not erase the other three preferences.
            conn.execute('UPDATE ui_preferences SET mode=? WHERE scope=? AND origin=?',
                         (mode, scope, PERSONAL_PREF_ORIGIN))

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
                    logger.info('[daka 1.8] Calendar uploaded and media message accepted')
                else:
                    await event.send(MessageChain().message(reply.split('\n', 1)[0]).file_image(img))
                return
            except Exception as exc:
                reason = safe_error(exc)
                logger.warning('[daka 1.8] Calendar image failed: %s', reason)
                self._last_delivery[str(event.unified_msg_origin)] = {'mode': 'calendar-text', 'errors': [reason]}
                reply = '⚠️ 月历图片发送失败，以下为文字月历。\n' + reply
        await self._send_plain(event, reply)

    async def _send_text_chunks(self, event, text: str, limit: int = 950):
        """QQ-safe full help fallback; never silently truncate commands."""
        buffer = ""
        for line in text.splitlines(keepends=True):
            if buffer and len(buffer) + len(line) > limit:
                await self._send_plain(event, buffer.rstrip())
                buffer = ""
            if len(line) > limit:
                if buffer:
                    await self._send_plain(event, buffer.rstrip())
                    buffer = ""
                for offset in range(0, len(line), limit):
                    await self._send_plain(event, line[offset:offset+limit])
            else:
                buffer += line
        if buffer.strip():
            await self._send_plain(event, buffer.rstrip())

    async def _send_help(self, event, scope: str, origin: str):
        if self._display_mode(scope, origin, "help") == "image":
            try:
                image_path = cache_file(self.data_dir / "cache", "help", "all",
                                        "v1.9.1", "help-metro-v191")
                image = render_help_image(image_path)
                if image:
                    if platform_name(event) in OFFICIAL:
                        await QQTransport(event).image(image, "🌱 打卡命令大全 v1.9.1 · 发送 /打卡 帮助 随时查看")
                    else:
                        await event.send(MessageChain().message(
                            "🌱 打卡命令大全 v1.9.1").file_image(image))
                    self._last_delivery[origin] = {"mode": "help-image", "ack": True}
                    return
                logger.warning("[daka 1.9] Help image unavailable: Pillow or CJK font missing")
            except Exception as exc:
                reason = safe_error(exc)
                logger.warning("[daka 1.9] Help image send failed: %s", reason)
                self._last_delivery[origin] = {"mode": "help-text-fallback", "errors": [reason]}
        await self._send_text_chunks(event, HELP_TEXT)

    def _view_command(self, event, args: list[str], scope: str, origin: str,
                      account_code: str, user_code: str) -> str:
        if not args:
            modes = {"image": "图片", "text": "文字"}
            ops = {"auto": "自动", "keyboard": "按钮", "links": "文字链接", "text": "关闭"}
            pref = self._preferences(scope, origin)
            mode = pref['mode'] if pref['mode'] in ops else self.interaction_mode
            return ("🖼 我的默认视图（群聊／私聊共用）\n"
                    f"今日：{modes[self._display_mode(scope, origin, 'daily')]}\n"
                    f"月历：{modes[self._display_mode(scope, origin, 'month')]}\n"
                    f"帮助：{modes[self._display_mode(scope, origin, 'help')]}\n"
                    f"快捷操作：{ops.get(mode, '自动')}\n"
                    "设置：/打卡 视图 今日 图片\n"
                    "或：/打卡 视图 帮助 文字\n"
                    "其他选项请见 /打卡 帮助。")
        if args == ["重置"]:
            self._reset_preferences(scope, origin)
            return "✅ 个人每日、月历、帮助与快捷操作视图均已恢复插件默认值。"
        if args == ["诊断"]:
            report = self._last_delivery.get(origin, {})
            try:
                transport = QQTransport(event)
                api_state = transport.ctx.scene + ' / bot.api available'
            except Exception as exc:
                api_state = safe_error(exc)
            return (f"🔍 打卡诊断 v1.9\n账户：{account_code}\n发送者指纹：{user_code}\n"
                    f"当前操作模式：{self._ui_mode(scope, origin)}\n接口：{api_state}\n"
                    f"最近发送：{report.get('mode', 'not tried')}\n"
                    f"接口确认：{report.get('ack', False)}\n"
                    f"错误：{'; '.join(report.get('errors', [])) or 'none'}\n"
                    "接口接受不代表 QQ 客户端显示了按钮。\n"
                    "群内指令按钮插入后可能需要手动发送。\n"
                    "可切换：/打卡 视图 操作 文字链接")
        if len(args) != 2:
            raise HabitError("用法：/打卡 视图 今日|月历|帮助 图片|文字；或 /打卡 视图 操作 自动|按钮|文字链接|关闭")
        category, setting = args
        modes = {"图片": "image", "文字": "text"}
        kinds = {"今日": "daily", "月历": "month", "帮助": "help"}
        if category in kinds and setting in modes:
            self._set_display_mode(scope, origin, kinds[category], modes[setting])
            return f"✅ 个人默认{category}视图已设为「{setting}」，群聊和私聊同步生效。"
        operations = {"自动": "auto", "按钮": "keyboard",
                      "文字链接": "links", "关闭": "text"}
        if category == "操作" and setting in operations:
            self._set_ui_mode(scope, origin, operations[setting])
            return f"✅ 个人快捷操作方式已设为「{setting}」，不会覆盖其他视图设置。"
        raise HabitError("用法：/打卡 视图 今日|月历|帮助 图片|文字；或 /打卡 视图 操作 自动|按钮|文字链接|关闭")

    def _account_command(self, args: list[str], origin: str, raw_sender: str,
                         is_group: bool, union_openid: str | None, nick: str,
                         adapter_type: str, app_id: str | None,
                         account_code: str, user_code: str) -> str:
        if not args:
            return (f"👤 个人账户：{account_code}\n"
                    f"发送者指纹：{user_code}\n接入：{adapter_type}\n"
                    "同一机器人已确认的相同 OpenID 共用个人数据。\n"
                    "身份不同请使用 /打卡 账户 绑定 合并群聊和私聊记录。")
        if args[0] not in ("绑定", "解绑"):
            raise HabitError("用法：/打卡 账户 [绑定 [绑定码]|解绑]")
        return self._binding_command(" ".join(args), origin, raw_sender, is_group,
                                     union_openid, nick, adapter_type, app_id)

    @filter.command("打卡", priority=10)
    async def daka_command(self, event: AstrMessageEvent):
        """Resolve a person, handle v1.9 commands, then emit the chosen view."""
        self._start_scheduler()
        origin = str(event.unified_msg_origin)
        raw_sender = str(event.get_sender_id())
        nick = str(event.get_sender_name() or "我")
        is_group = self._is_group_event(event)
        command = self._extract_command(str(event.message_str or ""))
        adapter_type = platform_name(event)
        app_id = app_id_of(event) if adapter_type in OFFICIAL else None
        union_openid = self._union_id_from_event(event)
        sender, scope = self.store.SELF, ""
        reply, img = None, None
        calendar_request = help_request = False
        logger.info('[daka 1.9] scene=%s command=%s',
                    'group' if is_group else 'c2c',
                    command.split(' ', 1)[0] if command else 'today')
        try:
            try:
                account_id = self._resolve_person(origin, raw_sender, is_group,
                                                  union_openid, adapter_type, app_id)
                social_scope = origin if is_group else None
                if is_group:
                    self.store.remember_group_identity(origin, adapter_type, app_id)
                    self.store.member_in_group(origin, account_id, nick, raw_sender)
                else:
                    linked = self.store.get_private_link(origin, raw_sender)
                    if linked is not None:
                        social_scope = linked['group_scope']
                        group_account = self._resolve_person(linked['group_scope'],
                                                             linked['group_user_id'], True,
                                                             adapter_type=adapter_type, app_id=app_id)
                        account_id = self.store.merge_persons(group_account, account_id)
                scope = self.store.person_scope(account_id)
                parts = command.split()
                action = parts[0] if parts else ''
                code = hashlib.sha256(account_id.encode()).hexdigest()[:10]
                uid_code = hashlib.sha256(raw_sender.encode()).hexdigest()[:10]
                if action == "帮助":
                    if len(parts) != 1:
                        raise HabitError("用法：/打卡 帮助（自动使用你设置的默认帮助视图）")
                    help_request, reply = True, HELP_TEXT
                elif action == "视图":
                    reply = self._view_command(event, parts[1:], scope, origin, code, uid_code)
                elif action == "账户":
                    reply = self._account_command(parts[1:], origin, raw_sender, is_group,
                                                  union_openid, nick, adapter_type, app_id,
                                                  code, uid_code)
                else:
                    calendar_request = action == "月历" or (action == "好友" and
                                       len(parts) > 1 and parts[1] == "月历")
                    reply, img = self._dispatch(command, scope, sender, nick,
                                                self._is_admin(event), is_group,
                                                social_scope, account_id, operator_scope=self.store.person_scope(account_id))
                    if calendar_request and self._display_mode(scope, origin, 'month') == 'text':
                        img = None
            except HabitError as exc:
                reply, img = f"⚠️ {exc}", None
                calendar_request = help_request = False
            except Exception:
                logger.exception("[daka 1.9] Command processing failed")
                reply, img = "⚠️ 处理失败，请查看 AstrBot 日志。", None
                calendar_request = help_request = False

            if help_request:
                await self._send_help(event, scope, origin)
            elif calendar_request:
                await self._send_calendar(event, reply, img)
            elif reply and "今日打卡" in reply and self._display_mode(scope, origin, "daily") == "image":
                daily_img = self._make_daily_image(scope, sender, nick, self._today())
                if daily_img:
                    await self._send_daily_image(event, reply, daily_img, scope, sender, nick, self._today())
                elif adapter_type in OFFICIAL:
                    try:
                        report = await send_status(event, reply,
                                                   self.store.daily(scope, sender, self._today()),
                                                   self._ui_mode(scope, origin), logger.info)
                        self._last_delivery[origin] = report
                    except Exception as exc:
                        reason = safe_error(exc)
                        self._last_delivery[origin] = {"mode": "astrbot-text-fallback", "errors": [reason]}
                        logger.warning("[daka 1.9] Interactive send failed: %s", reason)
                        await self._send_plain(event, reply)
                else:
                    await self._send_plain(event, reply)
            elif reply and "今日打卡" in reply and adapter_type in OFFICIAL:
                try:
                    report = await send_status(event, reply,
                                               self.store.daily(scope, sender, self._today()),
                                               self._ui_mode(scope, origin), logger.info)
                    self._last_delivery[origin] = report
                except Exception as exc:
                    reason = safe_error(exc)
                    self._last_delivery[origin] = {"mode": "astrbot-text-fallback", "errors": [reason]}
                    logger.warning("[daka 1.9] Interactive send failed: %s", reason)
                    await self._send_plain(event, reply)
            elif reply:
                await self._send_plain(event, reply)
        finally:
            event.stop_event()

    @staticmethod
    def _debug_table_exists(conn, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _debug_account_snapshot(self, conn, account_id: str) -> dict:
        """Fingerprint a user, including merged account lineage and imported legacy rows."""
        if not re.fullmatch(r"[a-f0-9]{32}", account_id):
            raise HabitError("账户 ID 格式不正确；请从用户列表复制完整账户 ID。")
        family = {account_id}
        if self._debug_table_exists(conn, "account_redirects"):
            changed = True
            while changed:
                changed = False
                for row in conn.execute("SELECT source,target FROM account_redirects").fetchall():
                    if row["target"] in family and row["source"] not in family:
                        family.add(row["source"])
                        changed = True
        ids = sorted(family)
        scopes = sorted("person:" + item for item in ids)
        placeholders = ",".join("?" for _ in ids)
        scope_ph = ",".join("?" for _ in scopes)

        aliases = []
        if self._debug_table_exists(conn, "person_aliases"):
            aliases = [r["alias"] for r in conn.execute(
                f"SELECT alias FROM person_aliases WHERE account_id IN ({placeholders}) ORDER BY alias",
                ids).fetchall()]
        legacy_pairs = set()
        if self._debug_table_exists(conn, "imported_legacy"):
            legacy_pairs.update((r["old_scope"], r["old_user_id"]) for r in conn.execute(
                f"SELECT old_scope,old_user_id FROM imported_legacy "
                f"WHERE account_id IN ({placeholders})", ids).fetchall())

        # Recover exact old per-conversation keys that were never imported yet.
        platform_pairs = set()
        for alias in aliases:
            if alias.startswith("qqv2:"):
                continue
            instance = alias.split(":", 1)[0].lower()
            if any(marker in alias for marker in (
                ":qq-uin:", ":uin:", ":official-c2c:", ":official-group:",
                ":official-group:", ":unknown:", ":union:",
            )):
                platform_pairs.add((instance, alias.rsplit(":", 1)[-1]))
            for marker in (":group:", ":private:", ":official-group:",
                           ":unknown:group:", ":unknown:private:"):
                prefix = instance + marker
                if alias.startswith(prefix):
                    tail = alias[len(prefix):]
                    if ":" in tail:
                        old_scope, old_user = tail.rsplit(":", 1)
                        if old_scope and old_user:
                            legacy_pairs.add((old_scope, old_user))
                    break
        legacy_pairs = sorted(legacy_pairs)
        platform_pairs = sorted(platform_pairs)

        digest = hashlib.sha256()
        counts = {"accounts": len(ids), "habits": 0, "rules": 0,
                  "records": 0, "events": 0, "legacy_scopes": len(legacy_pairs)}

        def include(label: str, rows):
            digest.update((label + "\n").encode("utf-8"))
            for row in rows:
                digest.update(json.dumps(tuple(row), ensure_ascii=False,
                                         separators=(",", ":"), default=str).encode("utf-8"))
                digest.update(b"\n")

        def rows_for(table: str, where: str, params: tuple):
            if not self._debug_table_exists(conn, table):
                return []
            return conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid",
                                params).fetchall()

        # Include all per-person tables for every merged account ID.
        scope_tables = ("groups", "habits", "records", "checkin_events",
                        "habit_rules", "score_profiles", "ui_preferences", "reminders")
        for table in scope_tables:
            rows = rows_for(table, f"scope IN ({scope_ph})", tuple(scopes))
            include(table + ":person", rows)
            if table == "habits":
                counts["habits"] += len(rows)
            elif table == "habit_rules":
                counts["rules"] += len(rows)
            elif table == "records":
                counts["records"] += len(rows)
            elif table == "checkin_events":
                counts["events"] += len(rows)

        for table, where, params in (
            ("person_accounts", f"id IN ({placeholders})", tuple(ids)),
            ("person_aliases", f"account_id IN ({placeholders})", tuple(ids)),
            ("account_redirects", f"source IN ({placeholders}) OR target IN ({placeholders})",
             tuple(ids) + tuple(ids)),
            ("imported_legacy", f"account_id IN ({placeholders})", tuple(ids)),
            ("members", f"user_id IN ({placeholders})", tuple(ids)),
        ):
            rows = rows_for(table, where, params)
            include(table + ":account", rows)

        for old_scope, old_user in legacy_pairs:
            for table in ("records", "checkin_events", "members"):
                rows = rows_for(table, "scope=? AND user_id=?", (old_scope, old_user))
                include(f"{table}:legacy:{old_scope}:{old_user}", rows)
                if table == "records":
                    counts["records"] += len(rows)
                elif table == "checkin_events":
                    counts["events"] += len(rows)

        # Binding links have no account_id column; resolve them using exact
        # instance + sender identifiers extracted from the account's aliases.
        matching_links = []
        matching_tokens = []
        if self._debug_table_exists(conn, "dm_links"):
            for row in conn.execute("SELECT * FROM dm_links ORDER BY rowid").fetchall():
                private_instance = str(row["private_origin"]).split(":", 1)[0].lower()
                group_instance = str(row["group_scope"]).split(":", 1)[0].lower()
                if ((private_instance, str(row["private_user_id"])) in platform_pairs or
                    (group_instance, str(row["group_user_id"])) in platform_pairs):
                    matching_links.append(tuple(row))
            include("dm_links:account", matching_links)
        if self._debug_table_exists(conn, "dm_link_tokens"):
            for row in conn.execute("SELECT * FROM dm_link_tokens ORDER BY rowid").fetchall():
                instance = str(row["private_origin"]).split(":", 1)[0].lower()
                if (instance, str(row["private_user_id"])) in platform_pairs:
                    matching_tokens.append(tuple(row))
            include("dm_link_tokens:account", matching_tokens)

        # The display fields are part of the fingerprint, so nickname/alias
        # changes after preview also invalidate a whole-user deletion token.
        include("aliases:normalized", [(a,) for a in aliases])
        include("legacy_pairs:normalized", legacy_pairs)
        include("platform_pairs:normalized", platform_pairs)
        return {
            "family": ids, "scopes": scopes, "aliases": aliases,
            "legacy_pairs": legacy_pairs, "platform_pairs": platform_pairs,
            "matching_links": matching_links, "matching_tokens": matching_tokens,
            "counts": counts, "fingerprint": digest.hexdigest(),
        }

    @staticmethod
    def _debug_alias_labels(aliases: list[str]) -> list[str]:
        values = []
        for alias in aliases:
            label = None
            if ":openid:" in alias:
                label = "OpenID"
            elif ":union:" in alias:
                label = "UnionID"
            elif ":qq-uin:" in alias or ":uin:" in alias:
                label = "QQ/UIN"
            elif ":official-c2c:" in alias:
                label = "私聊SID"
            elif ":official-group:" in alias:
                label = "群SID"
            elif ":unknown:" in alias:
                label = "平台ID"
            elif ":group:" in alias or ":private:" in alias:
                label = "旧版ID"
            if label:
                value = alias.rsplit(":", 1)[-1]
                rendered = label + "=" + value[:72]
                if value and rendered not in values:
                    values.append(rendered)
        return values[:4]

    def _debug_user_list(self, args: list[str], is_admin: bool) -> tuple[str, None]:
        if not is_admin:
            raise HabitError("用户列表仅限 AstrBot 管理员。")
        if len(args) > 1 or (args and not args[0].isdecimal()):
            raise HabitError("用法：/打卡 调试 用户列表 [页码]")
        # Bound conversion to avoid Python's integer-string digit limit on
        # intentionally oversized admin input; such a page clamps to the last.
        requested_page = (10**9 if args and len(args[0]) > 9
                          else max(1, int(args[0])) if args else 1)
        page_size = 5
        with self.store._db() as conn:
            total = conn.execute("""
                SELECT COUNT(*) FROM person_accounts a
                WHERE NOT EXISTS (SELECT 1 FROM account_redirects r WHERE r.source=a.id)
            """).fetchone()[0]
            max_page = max(1, (int(total) + page_size - 1) // page_size)
            page = min(requested_page, max_page)
            rows = conn.execute("""
                SELECT a.id,a.created_at
                FROM person_accounts a
                WHERE NOT EXISTS (
                    SELECT 1 FROM account_redirects r WHERE r.source=a.id
                )
                ORDER BY a.created_at DESC,a.id LIMIT ? OFFSET ?
            """, (page_size, (page-1)*page_size)).fetchall()
            if not rows:
                return (f"第 {page} 页没有用户。共 {total} 个账户；"
                        "请使用 /打卡 调试 用户列表 查看第一页。"), None
            lines = [f"👥 用户列表 · 第 {page} 页 / 共 {total} 个账户"]
            for row in rows:
                account_id = row["id"]
                # Show nicknames and identifiers for the canonical merged account.
                nicknames = [r["display_name"] for r in conn.execute(
                    "SELECT DISTINCT display_name FROM members WHERE user_id=? "
                    "ORDER BY display_name LIMIT 4", (account_id,)).fetchall()]
                aliases = [r["alias"] for r in conn.execute(
                    "SELECT alias FROM person_aliases WHERE account_id=? ORDER BY alias",
                    (account_id,)).fetchall()]
                labels = self._debug_alias_labels(aliases)
                lines.append(
                    f"昵称：{'、'.join(nicknames) if nicknames else '暂无群昵称'}\n"
                    f"账户ID：{account_id}\n"
                    f"SID/平台标识：{'；'.join(labels) if labels else '暂无可识别标识'}\n"
                    f"创建：{row['created_at']}"
                )
            if (page * page_size) < total:
                lines.append(f"下一页：/打卡 调试 用户列表 {page+1}")
            lines.append("跨用户操作请使用列表中的完整账户ID。")
            return "\n\n".join(lines), None

    def _debug_delete_user(self, args: list[str], operator_scope: str,
                           is_admin: bool) -> tuple[str, None]:
        if not is_admin:
            raise HabitError("彻底删除用户仅限 AstrBot 管理员。")
        if len(args) not in (1, 3) or (len(args) == 3 and args[1] != "确认"):
            raise HabitError("用法：/打卡 调试 删除用户 账户ID；预览后发送 /打卡 调试 删除用户 账户ID 确认 确认码")
        target_id = args[0].lower()
        if not re.fullmatch(r"[a-f0-9]{32}", target_id):
            raise HabitError("账户 ID 格式不正确，请从 /打卡 调试 用户列表 复制完整账户ID。")
        actor_id = operator_scope.removeprefix("person:")
        key = "delete-user:" + operator_scope + ":" + target_id
        if len(args) == 1:
            with self.store._db() as conn:
                target_id = self.store._root_locked(conn, target_id)
                if target_id == actor_id:
                    raise HabitError("不能删除当前操作者自己的账户。")
                if not conn.execute("SELECT 1 FROM person_accounts WHERE id=?", (target_id,)).fetchone():
                    raise HabitError("未找到该账户 ID。请先使用用户列表查询。")
                snap = self._debug_account_snapshot(conn, target_id)
                nicknames = [r["display_name"] for r in conn.execute(
                    "SELECT DISTINCT display_name FROM members WHERE user_id IN (" +
                    ",".join("?" for _ in snap["family"]) + ") ORDER BY display_name LIMIT 6",
                    tuple(snap["family"])).fetchall()]
            code = secrets.token_hex(6).upper()
            self._debug_pending[key] = {
                "code": code, "expires": time.monotonic()+300,
                "fingerprint": snap["fingerprint"], "target": target_id,
                "operator": operator_scope,
            }
            c = snap["counts"]
            return (f"⚠️ 永久删除用户预览\n昵称：{'、'.join(nicknames) if nicknames else '暂无群昵称'}\n"
                    f"账户ID：{target_id}\n合并账户历史：{len(snap['family'])} 个账户\n"
                    f"任务 {c['habits']} 条、规则 {c['rules']} 条、日记录 {c['records']} 条、"
                    f"打卡事件 {c['events']} 条、旧会话 {c['legacy_scopes']} 个。\n"
                    "将删除账户身份、个人任务与记录，并清理可识别的旧会话记录及绑定关系。\n"
                    "执行前会生成 SQLite 备份；备份文件不会自动删除。\n"
                    f"五分钟内确认：/打卡 调试 删除用户 {target_id} 确认 {code}"), None

        code = args[2].upper()
        pending = self._debug_pending.get(key)
        if (not pending or pending["expires"] < time.monotonic() or
                pending["target"] != target_id or pending["operator"] != operator_scope or
                not hmac.compare_digest(pending["code"], code)):
            raise HabitError("确认码无效、已过期或操作者不匹配，请重新预览。")
        # First compare, then make a consistent backup, then re-check inside
        # BEGIN IMMEDIATE to close the race between the preview and deletion.
        with self.store._db() as conn:
            target_id = self.store._root_locked(conn, target_id)
            snap = self._debug_account_snapshot(conn, target_id)
        if snap["fingerprint"] != pending["fingerprint"]:
            self._debug_pending.pop(key, None)
            raise HabitError("预览后用户数据发生变化，已中止，请重新预览。")
        backup = backup_before_edit(
            self.store.db_path,
            Path(get_astrbot_data_path()) / "backups" / "astrbot_plugin_daka" / "maintenance",
            "purge",
        )
        with self.store._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            target_id = self.store._root_locked(conn, target_id)
            fresh = self._debug_account_snapshot(conn, target_id)
            if fresh["fingerprint"] != pending["fingerprint"]:
                self._debug_pending.pop(key, None)
                raise HabitError("备份后用户数据仍发生变化，已中止，请重新预览。")
            family = fresh["family"]
            scopes = fresh["scopes"]
            legacy_pairs = fresh["legacy_pairs"]
            platform_pairs = set(fresh["platform_pairs"])
            for table in ("checkin_events", "records", "habit_rules", "habits",
                          "score_profiles", "groups", "ui_preferences", "reminders"):
                if self._debug_table_exists(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE scope IN (" +
                                 ",".join("?" for _ in scopes) + ")", tuple(scopes))
            if self._debug_table_exists(conn, "members"):
                conn.execute("DELETE FROM members WHERE user_id IN (" +
                             ",".join("?" for _ in family) + ")", tuple(family))
            for old_scope, old_user in legacy_pairs:
                for table in ("records", "checkin_events", "members"):
                    if self._debug_table_exists(conn, table):
                        conn.execute(f"DELETE FROM {table} WHERE scope=? AND user_id=?",
                                     (old_scope, old_user))
            if self._debug_table_exists(conn, "dm_links"):
                for row in conn.execute("SELECT * FROM dm_links").fetchall():
                    pi = str(row["private_origin"]).split(":",1)[0].lower()
                    gi = str(row["group_scope"]).split(":",1)[0].lower()
                    if ((pi, str(row["private_user_id"])) in platform_pairs or
                            (gi, str(row["group_user_id"])) in platform_pairs):
                        conn.execute("DELETE FROM dm_links WHERE private_origin=? AND private_user_id=?",
                                     (row["private_origin"], row["private_user_id"]))
            if self._debug_table_exists(conn, "dm_link_tokens"):
                for row in conn.execute("SELECT * FROM dm_link_tokens").fetchall():
                    pi = str(row["private_origin"]).split(":",1)[0].lower()
                    if (pi, str(row["private_user_id"])) in platform_pairs:
                        conn.execute("DELETE FROM dm_link_tokens WHERE token_hash=?",
                                     (row["token_hash"],))
            if self._debug_table_exists(conn, "person_aliases"):
                conn.execute("DELETE FROM person_aliases WHERE account_id IN (" +
                             ",".join("?" for _ in family) + ")", tuple(family))
            if self._debug_table_exists(conn, "imported_legacy"):
                conn.execute("DELETE FROM imported_legacy WHERE account_id IN (" +
                             ",".join("?" for _ in family) + ")", tuple(family))
            if self._debug_table_exists(conn, "account_redirects"):
                conn.execute("DELETE FROM account_redirects WHERE source IN (" +
                             ",".join("?" for _ in family) + ") OR target IN (" +
                             ",".join("?" for _ in family) + ")", tuple(family)+tuple(family))
            conn.execute("DELETE FROM person_accounts WHERE id IN (" +
                         ",".join("?" for _ in family) + ")", tuple(family))
        self._debug_pending.pop(key, None)
        return f"✅ 已彻底删除用户 {target_id} 及其可关联的历史数据。操作前备份：{backup}", None

    def _debug_command(self, args: list[str], scope: str, is_admin: bool,
                       operator_scope: str | None = None,
                       target_account_id: str | None = None) -> tuple[str, None]:
        """Allow self-service maintenance; restrict cross-account operations to AstrBot admins."""
        operator_scope = operator_scope or scope
        usage = ("🔧 调试命令（默认只操作自己的个人任务）\n"
                 "/打卡 调试 更名 旧名称 新名称\n"
                 "/打卡 调试 彻底删除 任务名称\n"
                 "更名遇到同名任务会预览合并；彻底删除会删除该任务的在线记录。\n"
                 "所有任务变更先预览，确认码五分钟有效，执行前自动备份。")
        if not args:
            lines = [usage]
            if is_admin:
                lines.extend(("/打卡 调试 用户列表 [页码]",
                              "/打卡 调试 用户 账户ID 更名 旧名称 新名称",
                              "/打卡 调试 用户 账户ID 彻底删除 任务名称",
                              "/打卡 调试 删除用户 账户ID"))
            return "\n".join(lines), None

        if args[0] == "用户列表":
            return self._debug_user_list(args[1:], is_admin)
        if args[0] == "删除用户":
            return self._debug_delete_user(args[1:], operator_scope, is_admin)
        if args[0] == "用户":
            if not is_admin:
                raise HabitError("管理员指定其他用户需要 AstrBot 管理员权限；自己的任务可以直接调试。")
            if len(args) < 4:
                raise HabitError("用法：/打卡 调试 用户 账户ID 更名 旧名称 新名称；或 用户 账户ID 彻底删除 任务名称")
            target_id = args[1].lower()
            if not re.fullmatch(r"[a-f0-9]{32}", target_id):
                raise HabitError("请使用 /打卡 调试 用户列表 中的完整账户 ID。")
            with self.store._db() as conn:
                target_id = self.store._root_locked(conn, target_id)
                if not conn.execute("SELECT 1 FROM person_accounts WHERE id=?", (target_id,)).fetchone():
                    raise HabitError("未找到该账户 ID。请先用 /打卡 调试 用户列表 查询。")
            target_scope = self.store.person_scope(target_id)
            return self._debug_command(args[2:], target_scope, True,
                                       operator_scope=operator_scope,
                                       target_account_id=target_id)

        operation = args[0]
        if operation == "更名":
            if len(args) not in (3, 5):
                raise HabitError("用法：/打卡 调试 更名 旧名称 新名称 [确认 确认码]")
            old, new = args[1:3]
            new = validate_task_name(new)
            confirmed = len(args) == 5 and args[3] == "确认"
            if len(args) == 5 and not confirmed:
                raise HabitError("用法：/打卡 调试 更名 旧名称 新名称 确认 确认码")
            token = args[4] if confirmed else None
            target = new
        elif operation == "彻底删除":
            if len(args) not in (2, 4):
                raise HabitError("用法：/打卡 调试 彻底删除 任务名称 [确认 确认码]")
            old = args[1]
            confirmed = len(args) == 4 and args[2] == "确认"
            if len(args) == 4 and not confirmed:
                raise HabitError("用法：/打卡 调试 彻底删除 任务名称 确认 确认码")
            token = args[3] if confirmed else None
            target = None
        else:
            raise HabitError(usage)

        cross_user = target_account_id is not None
        pending_key = f"task:{scope}:{operator_scope}"
        if not confirmed:
            preview = inspect_task(self.store, scope, old, target)
            confirm_code = secrets.token_hex(6).upper()
            self._debug_pending[pending_key] = {
                "operation": operation, "old": preview["source"],
                "new": target, "fingerprint": preview["fingerprint"],
                "code": confirm_code, "expires": time.monotonic() + 300,
                "target_account_id": target_account_id,
            }
            c = preview["counts"]
            prefix = (f"/打卡 调试 用户 {target_account_id} " if cross_user else "/打卡 调试 ")
            intro = (f"⚠️ 调试预览：{old} → {target}\n"
                     + ("目标名称已存在，将合并两项。" if preview["merge"]
                        else "目标名称不存在，将直接更名。")) if target else (
                        f"⚠️ 永久删除预览：{old}\n将删除该任务及其全部在线历史记录。")
            lines = [intro,
                     f"操作对象：{'用户 '+target_account_id if cross_user else '你自己'}",
                     f"规则 {c['habit_rules']} 条、日记录 {c['records']} 条、事件 {c['checkin_events']} 条。"]
            if preview["merge"]:
                lines.append(f"两项同日重合：{preview['overlapping_days']} 天。")
                lines.append("合并后以目标任务设置为准，同日次数相加但不低于目标已有次数、最多按当日目标计分；所有原始事件时间保留。")
                if not preview["destination_enabled"]:
                    lines.append("注意：目标任务处于停用状态，合并不会自动启用它。")
            lines.append("执行前会自动创建 SQLite 备份；备份文件不会随删除自动清理。")
            cmd = (f"{prefix}更名 {old} {target} 确认 {confirm_code}" if target is not None
                   else f"{prefix}彻底删除 {old} 确认 {confirm_code}")
            lines.append("请在 5 分钟内发送完整确认指令：\n" + cmd)
            return "\n".join(lines), None

        pending = self._debug_pending.get(pending_key)
        if (not pending or pending["expires"] < time.monotonic() or
            pending["operation"] != operation or pending["old"] != old or
            pending["new"] != target or
            pending["target_account_id"] != target_account_id or
            not hmac.compare_digest(pending["code"], token.upper())):
            raise HabitError("确认码无效、已过期、操作者或目标不匹配。请重新预览。")
        current = inspect_task(self.store, scope, old, target)
        if current["fingerprint"] != pending["fingerprint"]:
            self._debug_pending.pop(pending_key, None)
            raise HabitError("预览后任务或记录发生变化，已中止，请重新预览。")
        backup = backup_before_edit(
            self.store.db_path,
            Path(get_astrbot_data_path()) / "backups" / "astrbot_plugin_daka" / "maintenance",
            "rename" if target is not None else "purge",
        )
        # rename_task/purge_task recheck the fingerprint under BEGIN IMMEDIATE.
        if target is not None:
            result = rename_task(self.store, scope, old, target, pending["fingerprint"])
            self._debug_pending.pop(pending_key, None)
            detail = "两项已合并" if result["merge"] else "已完成更名"
            return (f"✅ {detail}：「{old}」→「{target}」。\n"
                    f"保留原始打卡事件 {result['counts']['checkin_events']} 条。\n"
                    f"操作前备份：{backup}"), None
        result = purge_task(self.store, scope, old, pending["fingerprint"])
        self._debug_pending.pop(pending_key, None)
        deleted = result["deleted"]
        return (f"✅ 已彻底删除{'用户 '+target_account_id+' 的' if cross_user else '你的'}「{old}」。\n"
                f"删除规则 {deleted['habit_rules']} 条、日记录 {deleted['records']} 条、"
                f"打卡事件 {deleted['checkin_events']} 条。\n操作前备份：{backup}"), None

    def _task_command(self, args, scope, sender, nick, is_admin, today):
        if not args or (len(args) == 1 and args[0] not in ("目标", "删除", "时间", "权重", "规则")):
            name = self._canonical_habit(scope, args[0]) if args else None
            self.store.ensure_rules(scope)
            with self.store._db() as conn:
                habits = self.store._rules_locked(conn, scope, today, include_disabled=True)
            if name:
                habits = [h for h in habits if h["name"] == name]
                if not habits:
                    raise HabitError(f"找不到任务「{name}」，发送 /打卡 任务 查看全部任务。")
            lines = ["⚙️ 我的任务 · 04:00 换日"]
            for h in habits:
                status = "启用" if h["enabled"] else "已停用"
                timed = (f"最佳 {h['best_time']} · 半衰期 {h['delay_minutes']} 分"
                         if h['best_time'] else "不限时")
                lines.append(f"{'✅' if h['enabled'] else '⏸️'} {h['name']} · {h['target']} 次/天 · "
                             f"权重 {fmt_number(h['weight'])} · {status}\n   {timed}")
            lines.append("\n管理：/打卡 任务 目标 阅读 2\n"
                         "停用：/打卡 任务 删除 阅读\n"
                         "规则：/打卡 任务 规则")
            return "\n".join(lines), None

        kind, *values = args
        if kind == "规则":
            if values:
                raise HabitError("用法：/打卡 任务 规则")
            return RULE_HELP, None
        if kind == "目标":
            if len(values) != 2:
                raise HabitError("用法：/打卡 任务 目标 阅读 2")
            name, target_raw = values
            name = validate_task_name(name)
            name = self._canonical_habit(scope, name)
            target = self._positive_int(target_raw)
            if self.store.has_history(scope, name):
                self.store.configure(scope, name, target=target, enabled=1)
                msg = f"✅ 已更新／恢复「{name}」：每天 {target} 次，历史记录保留。"
            else:
                if not self.allow_member_add and not is_admin:
                    raise HabitError("管理员关闭了普通用户新建任务的权限。")
                self.store.add_habit(scope, name, target, sender, self.max_habits)
                msg = f"✅ 已创建「{name}」：每天 {target} 次，权重 1。"
            return msg, None
        if kind == "删除":
            if len(values) != 1:
                raise HabitError("用法：/打卡 任务 删除 阅读")
            name = self._canonical_habit(scope, values[0])
            self.store.delete_habit(scope, name, sender, is_admin)
            return f"✅ 已停用「{name}」，历史记录保持不变。", None
        if kind == "时间":
            if len(values) not in (2, 3):
                raise HabitError("用法：/打卡 任务 时间 睡觉 23:30 30；或 /打卡 任务 时间 运动 关闭")
            name, raw = self._canonical_habit(scope, values[0]), values[1]
            if raw == "关闭" and len(values) == 2:
                self.store.configure(scope, name, best_time=None)
            elif raw != "关闭":
                self.store.configure(scope, name, best_time=validate_clock(raw),
                                     delay_minutes=validate_delay(values[2]) if len(values) == 3 else None)
            else:
                raise HabitError("关闭时间规则时不需要半衰期参数。")
            return f"✅ 「{name}」的时间规则已更新，从今天起生效。", None
        if kind == "权重":
            if len(values) != 2:
                raise HabitError("用法：/打卡 任务 权重 阅读 0")
            name = self._canonical_habit(scope, values[0])
            self.store.configure(scope, name, weight=validate_weight(values[1]))
            return f"✅ 「{name}」权重已更新，从今天起生效。", None
        raise HabitError("任务命令不正确。发送 /打卡 帮助 查看全部正式命令。")

    def _record_command(self, args, scope, sender, nick, today):
        if args and args[0] == "补签":
            values = args[1:]
            if len(values) not in (2, 3):
                raise HabitError("用法：/打卡 记录 补签 睡觉 昨天 23:50（每次只能补签 1 次）")
            name = self._canonical_habit(scope, values[0])
            day = validate_day(values[1], today, self.max_backfill)
            completed = parse_backfill_time(day, values[2], self.tz, self._now()) if len(values) == 3 else None
            return self._perform_checkin(scope, sender, name, day, 1, nick, today,
                                         occurred_at=completed, explicit_backfill=True)
        if args and args[0] == "清除":
            values = args[1:]
            if len(values) not in (1, 2):
                raise HabitError("用法：/打卡 记录 清除 睡觉 [昨天/日期]")
            name = self._canonical_habit(scope, values[0])
            day = validate_day(values[1] if len(values) == 2 else None, today, self.max_backfill)
            changed = self.store.undo(scope, sender, name, day)
            msg = (f"↩️ 已清除 {day} 的「{name}」全部打卡记录。" if changed
                   else f"{day} 的「{name}」暂无打卡记录。")
            return self._status_after(msg, scope, sender, nick, today)
        return self._history_text(scope, sender, args, today), None

    def _friend_command(self, args, group_scope, scope, sender, nick, today):
        if group_scope is None:
            raise HabitError("好友功能仅支持群聊或绑定过群聊的私聊。")
        if not args:
            raise HabitError("用法：/打卡 好友 月历 昵称；/打卡 好友 排行；/打卡 好友 隐私 隐藏|公开")
        action, *values = args
        if action == "隐私":
            if len(values) != 1 or values[0] not in ("隐藏", "公开"):
                raise HabitError("用法：/打卡 好友 隐私 隐藏|公开")
            public = values[0] == "公开"
            # The current user ID is recovered from this per-person scope.
            account_id = scope.removeprefix("person:")
            self.store.set_visible(group_scope, account_id, public)
            return ("🔒 已隐藏：好友查询和排行榜不再展示你的记录。"
                    if not public else "🔓 已公开：你的月历与排行重新可见。"), None
        if action == "排行":
            if len(values) > 1:
                raise HabitError("用法：/打卡 好友 排行 [YYYY-MM]")
            year, month = validate_month(values[0] if values else None, today)
            self.store.upgrade_group_members(group_scope)
            members = self.store.group_rankings(group_scope, year, month)
            if not members:
                return f"📊 {year}-{month:02d} 暂无公开的打卡记录。", None
            lines = [f"🏆 {year}-{month:02d} 群友打卡排行榜",
                     "按月度加权完成度排序（含迟到扣分）"]
            for i, member in enumerate(members[:20], start=1):
                lines.append(f"{i}. {member['display_name']}  {fmt_percent(member['ratio'])}  "
                             f"({fmt_number(member['earned'])}/{fmt_number(member['weight'])})")
            return "\n".join(lines), None
        if action == "月历":
            if not 1 <= len(values) <= 3:
                raise HabitError("用法：/打卡 好友 月历 昵称 [习惯] [YYYY-MM]")
            self.store.upgrade_group_members(group_scope)
            friend = self.store.group_friend(group_scope, values[0].removeprefix("@"))
            year, month, chosen = self._parse_calendar_args(values[1:], today)
            return self._make_calendar(
                self.store.person_scope(friend["user_id"]), self.store.SELF,
                friend["display_name"], chosen, year, month, today
            )
        raise HabitError("用法：/打卡 好友 月历 昵称；/打卡 好友 排行；/打卡 好友 隐私 隐藏|公开")

    def _group_command(self, args, group_scope, is_group, is_admin):
        if not is_group:
            raise HabitError("请在群聊设置提醒。")
        if not is_admin:
            raise HabitError("只有 AstrBot 管理员可以设置群提醒。")
        if len(args) != 2 or args[0] != "提醒":
            raise HabitError("用法：/打卡 群设置 提醒 21:30；或 /打卡 群设置 提醒 关闭")
        if args[1] == "关闭":
            self.store.disable_reminder(group_scope)
            return "⏰ 已关闭本群每日打卡提醒。", None
        clock = validate_clock(args[1])
        self.store.set_reminder(group_scope, clock)
        return (f"⏰ 本群每日 {clock} 提醒已设置（{self.tz.key}）。\n"
                "QQ 官方机器人可能限制主动群消息，请观察实际推送。"), None

    def _dispatch(self, command: str, scope: str, sender: str, nick: str,
                  is_admin: bool, is_group: bool = True,
                  group_scope: str | None = None,
                  account_id: str | None = None,
                  operator_scope: str | None = None) -> tuple[str, str | None]:
        parts = command.split()
        today = self._today()
        if not parts:
            return self._daily_status(scope, sender, nick, today), None
        action, *args = parts
        if action == "调试":
            return self._debug_command(args, scope, is_admin, operator_scope=operator_scope or scope)
        if action == "任务":
            return self._task_command(args, scope, sender, nick, is_admin, today)
        if action == "记录":
            return self._record_command(args, scope, sender, nick, today)
        if action == "月历":
            if len(args) > 2:
                raise HabitError("用法：/打卡 月历 [习惯] [YYYY-MM]")
            year, month, chosen = self._parse_calendar_args(args, today)
            return self._make_calendar(scope, sender, nick, chosen, year, month, today)
        if action == "好友":
            return self._friend_command(args, group_scope, scope, sender, nick, today)
        if action == "群设置":
            return self._group_command(args, group_scope, is_group, is_admin)
        if action.isdecimal() or re.fullmatch(r"\+\d+", action):
            raise HabitError("新版不支持编号打卡；请直接发送 /打卡 睡觉 等习惯名称。")
        if action in RETIRED_COMMANDS:
            raise HabitError("该旧命令已合并。请发送 /打卡 帮助 查看新命令体系。")
        if args:
            raise HabitError("单次打卡不能附带次数或日期。请发送 /打卡 习惯名称；历史补签请用 /打卡 记录 补签。")
        return self._perform_checkin(scope, sender, self._canonical_habit(scope, action),
                                     today, 1, nick, today)

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
        if amount != 1:
            raise HabitError("每次只能打卡 1 次。")
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
            logger.warning('[daka 1.7] Calendar render failed: %s', safe_error(exc))
            image = None
        return text, image
