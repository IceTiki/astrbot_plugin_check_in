"""SQLite storage and business rules for the AstrBot daka plugin.

All day boundaries are supplied by the caller in its configured timezone.
No network calls or AstrBot imports appear in this module.
"""

from __future__ import annotations

import calendar
import re
import secrets
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path


DEFAULT_HABITS = (
    ("早睡", 1),
    ("早起", 1),
    ("早餐", 1),
    ("午餐", 1),
    ("晚餐", 1),
    ("喝水", 8),
)

NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff-]{1,16}$", re.UNICODE)


class HabitError(ValueError):
    """Expected error suitable for reporting to a user."""


def validate_name(name: str) -> str:
    name = name.strip()
    if not NAME_PATTERN.fullmatch(name):
        raise HabitError("习惯名称需要 1～16 个字，只能含中文、字母、数字、下划线或横线。")
    return name


def validate_day(raw: str | None, today: date, max_backfill: int = 14) -> date:
    if not raw:
        return today
    try:
        if raw == "昨天":
            selected = today - timedelta(days=1)
        elif raw == "今天":
            selected = today
        else:
            selected = date.fromisoformat(raw)
    except ValueError as exc:
        raise HabitError("日期格式应为 YYYY-MM-DD，例如 2026-10-16。") from exc
    if selected > today:
        raise HabitError("不能提前为未来日期打卡。")
    if (today - selected).days > max_backfill:
        raise HabitError(f"最多只能补签过去 {max_backfill} 天内的记录。")
    return selected


def validate_month(raw: str | None, today: date) -> tuple[int, int]:
    if not raw:
        return today.year, today.month
    if not re.fullmatch(r"\d{4}-\d{2}", raw):
        raise HabitError("月份应为 YYYY-MM，例如 2026-10。")
    year, month = map(int, raw.split("-"))
    if not 2000 <= year <= 2099 or not 1 <= month <= 12:
        raise HabitError("月份超出支持范围（2000-01 至 2099-12）。")
    if (year, month) > (today.year, today.month):
        raise HabitError("暂不显示未来月份的记录。")
    return year, month


class CheckinStore:
    """Small synchronous DB: each write is committed atomically."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    scope TEXT PRIMARY KEY, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS habits (
                    scope TEXT NOT NULL,
                    name TEXT NOT NULL,
                    target INTEGER NOT NULL CHECK(target BETWEEN 1 AND 100),
                    created_by TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scope, name)
                );
                CREATE TABLE IF NOT EXISTS members (
                    scope TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    visible INTEGER NOT NULL DEFAULT 1,
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY(scope, user_id)
                );
                CREATE TABLE IF NOT EXISTS records (
                    scope TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    day TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount BETWEEN 1 AND 999),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope, user_id, name, day)
                );
                -- Append-only records of successful clicks/check-ins.
                -- A historic backfill is recorded with its actual operation time
                -- and a separate target day; it is never backdated silently.
                CREATE TABLE IF NOT EXISTS checkin_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    day TEXT NOT NULL,
                    delta INTEGER NOT NULL CHECK(delta BETWEEN 1 AND 100),
                    checked_at TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'normal'
                );
                CREATE INDEX IF NOT EXISTS idx_checkin_events_lookup
                ON checkin_events(scope,user_id,day,name,checked_at);
                CREATE TABLE IF NOT EXISTS reminders (
                    scope TEXT PRIMARY KEY,
                    time_of_day TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_sent_day TEXT
                );
                CREATE TABLE IF NOT EXISTS dm_links (
                    private_origin TEXT NOT NULL,
                    private_user_id TEXT NOT NULL,
                    group_scope TEXT NOT NULL,
                    group_user_id TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    PRIMARY KEY(private_origin, private_user_id)
                );
                CREATE TABLE IF NOT EXISTS dm_link_tokens (
                    token_hash TEXT PRIMARY KEY,
                    private_origin TEXT NOT NULL,
                    private_user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_month
                ON records(scope, day, user_id);
                """
            )

    @contextmanager
    def _db(self):
        """Commit on success, rollback on error, always close the connection."""
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 15000")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def ensure_group(self, scope: str) -> None:
        if not scope:
            raise HabitError("无法识别当前群会话。")
        with self._db() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO groups(scope, created_at) VALUES (?, ?)",
                (scope, self._now()),
            )
            if cursor.rowcount:
                conn.executemany(
                    "INSERT INTO habits(scope, name, target, created_by, created_at) "
                    "VALUES (?, ?, ?, '__system__', ?)",
                    [(scope, name, target, self._now()) for name, target in DEFAULT_HABITS],
                )

    def touch_member(self, scope: str, user_id: str, display_name: str) -> None:
        display_name = (display_name or "群友").replace("\n", " ").replace("\r", " ").strip()[:32]
        with self._db() as conn:
            conn.execute(
                "INSERT INTO members(scope, user_id, display_name, joined_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope,user_id) DO UPDATE SET display_name=excluded.display_name",
                (scope, user_id, display_name, self._now()),
            )

    def list_habits(self, scope: str) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT name,target,created_by FROM habits WHERE scope=? AND enabled=1 "
                "ORDER BY created_at, rowid",
                (scope,),
            ).fetchall()
            return [dict(row) for row in rows]

    def habit(self, scope: str, name: str) -> dict:
        with self._db() as conn:
            row = conn.execute(
                "SELECT name,target,created_by FROM habits "
                "WHERE scope=? AND name=? AND enabled=1",
                (scope, name),
            ).fetchone()
        if row is None:
            raise HabitError(f"没有找到习惯「{name}」，请发送 /打卡 任务 查看。")
        return dict(row)

    def add_habit(self, scope: str, name: str, target: int, user_id: str, cap: int = 64) -> None:
        name = validate_name(name)
        if not 1 <= target <= 100:
            raise HabitError("目标次数需要在 1～100 之间。")
        with self._db() as conn:
            current = conn.execute(
                "SELECT COUNT(*) FROM habits WHERE scope=? AND enabled=1", (scope,)
            ).fetchone()[0]
            if current >= cap:
                raise HabitError(f"群内习惯已达到设置的 {cap} 项上限。")
            existing = conn.execute(
                "SELECT enabled FROM habits WHERE scope=? AND name=?", (scope, name)
            ).fetchone()
            if existing is not None:
                raise HabitError("该习惯已经存在或曾被删除，请换个名称。")
            conn.execute(
                "INSERT INTO habits(scope,name,target,created_by,created_at) VALUES (?,?,?,?,?)",
                (scope, name, target, user_id, self._now()),
            )

    def delete_habit(self, scope: str, name: str, user_id: str, is_admin: bool) -> None:
        item = self.habit(scope, name)
        if not (scope.startswith("person:") and user_id == "self") and not is_admin and item["created_by"] != user_id:
            raise HabitError("只能删除自己添加的习惯；默认习惯需要 AstrBot 管理员操作。")
        with self._db() as conn:
            conn.execute(
                "UPDATE habits SET enabled=0 WHERE scope=? AND name=?", (scope, name)
            )

    def change_target(self, scope: str, name: str, target: int, user_id: str, is_admin: bool) -> None:
        if not 1 <= target <= 100:
            raise HabitError("目标次数需要在 1～100 之间。")
        item = self.habit(scope, name)
        if not (scope.startswith("person:") and user_id == "self") and not is_admin and item["created_by"] != user_id:
            raise HabitError("只能调整自己添加的习惯；默认习惯需要 AstrBot 管理员操作。")
        with self._db() as conn:
            conn.execute(
                "UPDATE habits SET target=? WHERE scope=? AND name=?", (target, scope, name)
            )

    def checkin(
        self,
        scope: str,
        user_id: str,
        name: str,
        day: date,
        amount: int = 1,
        *,
        checked_at: datetime | None = None,
        kind: str = "normal",
    ) -> dict:
        item = self.habit(scope, name)
        if not 1 <= amount <= 100:
            raise HabitError("每次打卡的次数必须在 1～100 之间。")
        if kind not in ("normal", "backfill"):
            raise HabitError("未知的打卡记录类型。")
        # Caller supplies a timezone-aware timestamp, so timestamps do not
        # depend on host/container local timezone or database clock.
        actual = checked_at or datetime.now().astimezone()
        if actual.tzinfo is None or actual.utcoffset() is None:
            raise HabitError("打卡时间必须包含时区信息。")
        timestamp = actual.isoformat(timespec="seconds")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT amount FROM records WHERE scope=? AND user_id=? AND name=? AND day=?",
                (scope, user_id, name, day.isoformat()),
            ).fetchone()
            old = row[0] if row else 0
            if item["target"] == 1 and old >= 1:
                return {"old": old, "new": old, "target": 1, "changed": False, "checked_at": None}
            new = min(999, old + amount)
            credited_delta = new - old
            if credited_delta == 0:
                return {"old": old, "new": new, "target": item["target"], "changed": False, "checked_at": None}
            conn.execute(
                "INSERT INTO records(scope,user_id,name,day,amount,updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(scope,user_id,name,day) "
                "DO UPDATE SET amount=excluded.amount,updated_at=excluded.updated_at",
                (scope, user_id, name, day.isoformat(), new, timestamp),
            )
            conn.execute(
                "INSERT INTO checkin_events(scope,user_id,name,day,delta,checked_at,kind) "
                "VALUES (?,?,?,?,?,?,?)",
                (scope, user_id, name, day.isoformat(), credited_delta, timestamp, kind),
            )
            return {"old": old, "new": new, "target": item["target"], "changed": True, "checked_at": timestamp}

    def undo(self, scope: str, user_id: str, name: str, day: date) -> bool:
        self.habit(scope, name)
        with self._db() as conn:
            # Undo removes the day record and its detailed event timestamps.
            conn.execute(
                "DELETE FROM checkin_events WHERE scope=? AND user_id=? AND name=? AND day=?",
                (scope, user_id, name, day.isoformat()),
            )
            result = conn.execute(
                "DELETE FROM records WHERE scope=? AND user_id=? AND name=? AND day=?",
                (scope, user_id, name, day.isoformat()),
            )
            return bool(result.rowcount)

    def daily(self, scope: str, user_id: str, day: date) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT h.name,h.target,COALESCE(r.amount,0) AS amount, "
                "log.first_at,log.last_at, "
                "CASE WHEN r.amount IS NOT NULL AND log.first_at IS NULL "
                "THEN r.updated_at ELSE NULL END AS legacy_at "
                "FROM habits h LEFT JOIN records r ON h.scope=r.scope AND h.name=r.name "
                "AND r.user_id=? AND r.day=? "
                "LEFT JOIN (SELECT scope,user_id,name,day, "
                "MIN(checked_at) AS first_at,MAX(checked_at) AS last_at "
                "FROM checkin_events WHERE user_id=? AND day=? "
                "GROUP BY scope,user_id,name,day) log ON log.scope=h.scope "
                "AND log.user_id=? AND log.day=? AND log.name=h.name "
                "WHERE h.scope=? AND h.enabled=1 ORDER BY h.created_at,h.rowid",
                (user_id, day.isoformat(), user_id, day.isoformat(), user_id, day.isoformat(), scope),
            ).fetchall()
        return [dict(row) for row in rows]

    def checkin_times(self, scope: str, user_id: str, day: date, name: str | None = None) -> list[dict]:
        """Get each actual check-in time; include a labeled legacy fallback.

        The old records table only stores updated_at, not all clicks or the
        original first check-in time. Never pretend those older timestamps are
        precise individual check-in events.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT name,day,delta,checked_at,kind FROM checkin_events "
                "WHERE scope=? AND user_id=? AND day=? AND (? IS NULL OR name=?) "
                "ORDER BY checked_at,id",
                (scope, user_id, day.isoformat(), name, name),
            ).fetchall()
            missing = conn.execute(
                "SELECT r.name,r.day,r.amount,r.updated_at FROM records r "
                "WHERE r.scope=? AND r.user_id=? AND r.day=? "
                "AND (? IS NULL OR r.name=?) "
                "AND NOT EXISTS (SELECT 1 FROM checkin_events e "
                "WHERE e.scope=r.scope AND e.user_id=r.user_id "
                "AND e.name=r.name AND e.day=r.day)",
                (scope, user_id, day.isoformat(), name, name),
            ).fetchall()
        return [dict(row) | {"legacy": False} for row in rows] + [
            {
                "name": row["name"], "day": row["day"], "delta": row["amount"],
                "checked_at": row["updated_at"], "kind": "legacy", "legacy": True,
            } for row in missing
        ]

    def monthly(self, scope: str, user_id: str, year: int, month: int) -> dict[str, dict[int, int]]:
        beginning = f"{year:04d}-{month:02d}-01"
        ending = f"{year:04d}-{month:02d}-{calendar.monthrange(year,month)[1]:02d}"
        with self._db() as conn:
            rows = conn.execute(
                "SELECT name,day,amount FROM records "
                "WHERE scope=? AND user_id=? AND day BETWEEN ? AND ?",
                (scope, user_id, beginning, ending),
            ).fetchall()
        result: dict[str, dict[int, int]] = {}
        for row in rows:
            result.setdefault(row["name"], {})[int(row["day"][-2:])] = row["amount"]
        return result

    def streak(self, scope: str, user_id: str, name: str, today: date) -> int:
        target = self.habit(scope, name)["target"]
        start = (today - timedelta(days=450)).isoformat()
        with self._db() as conn:
            rows = conn.execute(
                "SELECT day,amount FROM records WHERE scope=? AND user_id=? AND name=? AND day>=?",
                (scope, user_id, name, start),
            ).fetchall()
        completed = {row["day"] for row in rows if row["amount"] >= target}
        cursor = today
        if cursor.isoformat() not in completed:
            cursor -= timedelta(days=1)
        count = 0
        while cursor.isoformat() in completed:
            count += 1
            cursor -= timedelta(days=1)
        return count

    def set_visible(self, scope: str, user_id: str, visible: bool) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE members SET visible=? WHERE scope=? AND user_id=?",
                (1 if visible else 0, scope, user_id),
            )

    def find_member(self, scope: str, search: str) -> dict:
        """Require an exact unique display name or exact id; no fuzzy matching."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT user_id,display_name,visible FROM members WHERE scope=? "
                "AND (user_id=? OR display_name=?)",
                (scope, search, search),
            ).fetchall()
        if not rows:
            raise HabitError("未找到这位群友。对方需要先在本群发送一次打卡指令。")
        if len(rows) > 1:
            raise HabitError("有重名群友，请让对方修改群昵称，或使用其平台用户 ID。")
        result = dict(rows[0])
        if not result["visible"]:
            raise HabitError("这位群友关闭了打卡记录共享。")
        return result

    def rankings(self, scope: str, year: int, month: int) -> list[dict]:
        start = f"{year:04d}-{month:02d}-01"
        end = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
        with self._db() as conn:
            rows = conn.execute(
                "SELECT m.user_id,m.display_name," 
                "COUNT(CASE WHEN r.amount>=h.target THEN 1 END) AS completed "
                "FROM members m LEFT JOIN records r "
                "ON r.scope=m.scope AND r.user_id=m.user_id AND r.day BETWEEN ? AND ? "
                "LEFT JOIN habits h ON h.scope=r.scope AND h.name=r.name AND h.enabled=1 "
                "WHERE m.scope=? AND m.visible=1 "
                "GROUP BY m.user_id,m.display_name "
                "HAVING completed>0 ORDER BY completed DESC,m.joined_at ASC LIMIT 30",
                (start, end, scope),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_reminder(self, scope: str, hhmm: str) -> None:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", hhmm):
            raise HabitError("提醒时间应为 HH:MM，例如 21:30。")
        with self._db() as conn:
            conn.execute(
                "INSERT INTO reminders(scope,time_of_day,enabled,last_sent_day) VALUES (?,?,1,NULL) "
                "ON CONFLICT(scope) DO UPDATE SET time_of_day=excluded.time_of_day,enabled=1,last_sent_day=NULL",
                (scope, hhmm),
            )

    def disable_reminder(self, scope: str) -> None:
        with self._db() as conn:
            conn.execute("UPDATE reminders SET enabled=0 WHERE scope=?", (scope,))

    def due_reminders(self, now: datetime) -> list[str]:
        """Return groups scheduled up to 4 minutes ago (suppress old missed reminders)."""
        today = now.date().isoformat()
        current_minutes = now.hour * 60 + now.minute
        with self._db() as conn:
            rows = conn.execute(
                "SELECT scope,time_of_day FROM reminders WHERE enabled=1 "
                "AND (last_sent_day IS NULL OR last_sent_day!=?)",
                (today,),
            ).fetchall()
        result = []
        for row in rows:
            hh, mm = map(int, row["time_of_day"].split(":"))
            delta = current_minutes - (hh * 60 + mm)
            if 0 <= delta <= 4:
                result.append(row["scope"])
        return result

    def mark_reminder_sent(self, scope: str, day: date) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE reminders SET last_sent_day=? WHERE scope=?",
                (day.isoformat(), scope),
            )

    def get_private_link(self, private_origin: str, private_user_id: str) -> dict | None:
        """Return the selected group identity for private-chat check-ins."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT group_scope,group_user_id FROM dm_links "
                "WHERE private_origin=? AND private_user_id=?",
                (private_origin, private_user_id),
            ).fetchone()
        return dict(row) if row else None

    def issue_private_link_token(self, private_origin: str, private_user_id: str) -> str:
        """Issue a single-use secret in DM; only the group message can redeem it.

        A secret is never persisted in plaintext. Its group disclosure occurs at
        redemption, after which it is deleted atomically, preventing replays.
        """
        from datetime import timezone

        secret = secrets.token_hex(8).upper()
        hashed = hashlib.sha256(secret.encode("ascii")).hexdigest()
        now = datetime.now(timezone.utc)
        until = (now + timedelta(minutes=10)).isoformat()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM dm_link_tokens WHERE expires_at < ?", (now.isoformat(),))
            conn.execute(
                "DELETE FROM dm_link_tokens WHERE private_origin=? AND private_user_id=?",
                (private_origin, private_user_id),
            )
            conn.execute(
                "INSERT INTO dm_link_tokens(token_hash,private_origin,private_user_id,expires_at) "
                "VALUES (?,?,?,?)",
                (hashed, private_origin, private_user_id, until),
            )
        return secret

    def redeem_private_link_token(self, secret: str, group_scope: str, group_user_id: str) -> None:
        """Bind a DM to a specific group identity without assuming QQ ids match."""
        from datetime import timezone

        if not re.fullmatch(r"[0-9A-Fa-f]{16}", secret):
            raise HabitError("绑定码格式不正确。请先私聊机器人发送 /打卡 账户 绑定。")
        hashed = hashlib.sha256(secret.upper().encode("ascii")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT private_origin,private_user_id,expires_at FROM dm_link_tokens "
                "WHERE token_hash=?", (hashed,),
            ).fetchone()
            if row is None or row["expires_at"] < now:
                raise HabitError("绑定码已过期或使用过。请私聊机器人重新获取绑定码。")
            # Prevent linking unrelated platform adapters accidentally.
            if row["private_origin"].split(":", 1)[0] != group_scope.split(":", 1)[0]:
                raise HabitError("私聊和群聊必须使用同一个 QQ 机器人平台接入方式。")
            conn.execute(
                "INSERT INTO dm_links(private_origin,private_user_id,group_scope,group_user_id,bound_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(private_origin,private_user_id) "
                "DO UPDATE SET group_scope=excluded.group_scope," 
                "group_user_id=excluded.group_user_id,bound_at=excluded.bound_at",
                (row["private_origin"], row["private_user_id"], group_scope, group_user_id, now),
            )
            conn.execute("DELETE FROM dm_link_tokens WHERE token_hash=?", (hashed,))
        return {"private_origin": row["private_origin"], "private_user_id": row["private_user_id"]}

    def unbind_private_link(self, private_origin: str, private_user_id: str) -> bool:
        with self._db() as conn:
            cursor = conn.execute(
                "DELETE FROM dm_links WHERE private_origin=? AND private_user_id=?",
                (private_origin, private_user_id),
            )
            return cursor.rowcount > 0
