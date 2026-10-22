"""Versioned, per-person task settings on top of the proven v1.5 identity DB.

No destructive schema replacement. Historical check-in times and each day's
weight/target/time settings remain intact. v1.9.6 scores *all* days with the
same smooth curve. Display order is a presentation preference shared by all
dates; sorting rewrites only sort_order of that person's rule revisions.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .accounts import AccountCheckinStore
from .store import HabitError, validate_name
from .scoring import (DEFAULT_RULES, RENAMES, business_day, score_item, summarize,
                      month_summary, validate_clock, validate_delay, validate_weight, CURRENT_MODEL)

UNSET = object()


class ScoredStore(AccountCheckinStore):
    def __init__(self, db_path, tz: ZoneInfo | None = None, clock=None):
        self.tz = tz or ZoneInfo("Asia/Shanghai")
        self.clock = clock or (lambda: datetime.now(self.tz))
        super().__init__(db_path)
        with self._db() as conn:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(checkin_events)")}
            if "occurred_at" not in columns:
                conn.execute("ALTER TABLE checkin_events ADD COLUMN occurred_at TEXT")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS score_profiles (
                    scope TEXT PRIMARY KEY, preset_at TEXT NOT NULL, since_day TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS habit_rules (
                    scope TEXT NOT NULL, name TEXT NOT NULL, since_day TEXT NOT NULL,
                    target INTEGER NOT NULL CHECK(target BETWEEN 1 AND 100),
                    weight REAL NOT NULL CHECK(weight >= 0 AND weight <= 1000),
                    best_time TEXT, delay_minutes INTEGER NOT NULL DEFAULT 30,
                    enabled INTEGER NOT NULL, sort_order INTEGER NOT NULL,
                    model TEXT NOT NULL DEFAULT 'smooth_decay',
                    PRIMARY KEY(scope,name,since_day)
                );
                CREATE INDEX IF NOT EXISTS idx_habit_rules_date
                ON habit_rules(scope,since_day,name);
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS score_engine_migrations ("
                         "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            scopes = [r[0] for r in conn.execute("SELECT scope FROM groups WHERE scope LIKE 'person:%'")]
        # Version-aware backup is performed by main.py before this constructor.
        for scope in scopes:
            self.ensure_rules(scope)
        self._migrate_unified_decay_once()

    def _migrate_unified_decay_once(self):
        """Set every historical rule revision to the single current model.

        Does NOT touch records, checkin_events, timestamps, targets, weights,
        best_time or any user/identity table. A transaction and one-time marker
        keep restarts idempotent. Older migration markers remain as audit data.
        """
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marker = "v1.9.6-unified-smooth-decay"
            if conn.execute("SELECT 1 FROM score_engine_migrations WHERE name=?",
                            (marker,)).fetchone():
                return
            conn.execute("UPDATE habit_rules SET model=? WHERE model<>?",
                         (CURRENT_MODEL, CURRENT_MODEL))
            conn.execute("INSERT INTO score_engine_migrations VALUES (?,?)",
                         (marker, self.clock().isoformat(timespec="seconds")))

    def today(self):
        return business_day(self.clock().astimezone(self.tz))

    def resolve_person(self, aliases, old_scope, old_user):
        account = super().resolve_person(aliases, old_scope, old_user)
        self.ensure_rules(self.person_scope(account))
        return account

    def _merge_persons_locked(self, conn, target, source):
        """Preserve extra timestamps and rule revisions when identities link.

        Destination rules win for same-name conflicts (the explicitly preferred
        account). Unknown source-only tasks keep their own weights and history.
        """
        target, source = self._root_locked(conn, target), self._root_locked(conn, source)
        if target == source:
            return target
        dst, src = self.person_scope(target), self.person_scope(source)
        result = super()._merge_persons_locked(conn, target, source)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "habit_rules" not in tables:
            return result
        for r in conn.execute("SELECT * FROM habit_rules WHERE scope=?", (src,)).fetchall():
            conn.execute("INSERT OR IGNORE INTO habit_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (dst, r["name"], r["since_day"], r["target"], r["weight"],
                          r["best_time"], r["delay_minutes"], r["enabled"], r["sort_order"], CURRENT_MODEL))
        # AccountCheckinStore copies core event columns; carry the new column too.
        for e in conn.execute("SELECT * FROM checkin_events WHERE scope=? AND occurred_at IS NOT NULL",
                              (src,)).fetchall():
            conn.execute("UPDATE checkin_events SET occurred_at=? WHERE scope=? AND user_id='self' "
                         "AND name=? AND day=? AND delta=? AND checked_at=? AND kind=? "
                         "AND occurred_at IS NULL", (e["occurred_at"], dst, e["name"], e["day"],
                          e["delta"], e["checked_at"], e["kind"]))
        p = conn.execute("SELECT * FROM score_profiles WHERE scope=?", (src,)).fetchone()
        if p:
            conn.execute("INSERT OR IGNORE INTO score_profiles VALUES (?,?,?)",
                         (dst, p["preset_at"], p["since_day"]))
            conn.execute("UPDATE score_profiles SET since_day=MIN(since_day,?) WHERE scope=?",
                         (p["since_day"], dst))
        return result

    def _start_day(self, raw: str, fallback: date) -> date:
        try:
            stamp = datetime.fromisoformat(raw)
            stamp = stamp.replace(tzinfo=self.tz) if stamp.tzinfo is None else stamp.astimezone(self.tz)
            return min(fallback, business_day(stamp))
        except (TypeError, ValueError):
            return fallback

    def ensure_rules(self, scope: str):
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM score_profiles WHERE scope=?", (scope,)).fetchone():
                return
            habits = conn.execute("SELECT *,rowid AS old_order FROM habits WHERE scope=? "
                                  "ORDER BY created_at,rowid", (scope,)).fetchall()
            if not habits:
                return
            cutover = self.today()
            starts = [cutover]
            for index, h in enumerate(habits):
                first_record = conn.execute("SELECT MIN(day) FROM records WHERE scope=? AND name=?",
                                            (scope, h["name"])).fetchone()[0]
                start = self._start_day(h["created_at"], cutover)
                if first_record:
                    start = min(start, date.fromisoformat(first_record))
                starts.append(start)
                # Keep the old account's known historical tasks/settings.
                # When timestamps are absent, no lateness is presumed.
                conn.execute("INSERT OR IGNORE INTO habit_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (scope, h["name"], start.isoformat(), h["target"], 1.0, None,
                              30, h["enabled"], index, CURRENT_MODEL))
            # Rename without changing assigned dates or destroying event times.
            for old, new in RENAMES.items():
                oldrow = conn.execute("SELECT 1 FROM habits WHERE scope=? AND name=?", (scope, old)).fetchone()
                newrow = conn.execute("SELECT 1 FROM habits WHERE scope=? AND name=?", (scope, new)).fetchone()
                if oldrow and not newrow:
                    for table in ("habits", "records", "checkin_events", "habit_rules"):
                        conn.execute(f"UPDATE {table} SET name=? WHERE scope=? AND name=?", (new, scope, old))
                elif oldrow and newrow:
                    # Keep ambiguous old/new histories separate instead of adding them.
                    rule = self._rule_locked(conn, scope, old, cutover, allow_disabled=True)
                    self._write_locked(conn, scope, dict(rule, enabled=0, model=CURRENT_MODEL), cutover)
            # Existing custom tasks inherit current default scoring for all dates.
            for r in self._rules_locked(conn, scope, cutover, include_disabled=True):
                self._write_locked(conn, scope, dict(r, model=CURRENT_MODEL), cutover)
            for order, (name, target, best, delay) in enumerate(DEFAULT_RULES):
                conn.execute("INSERT OR IGNORE INTO habits(scope,name,target,created_by,created_at) "
                             "VALUES (?,?,?,'__system__',?)",
                             (scope, name, target, self.clock().isoformat(timespec="seconds")))
                self._write_locked(conn, scope, {"name": name, "target": target, "weight": 1.0,
                                   "best_time": best, "delay_minutes": delay, "enabled": 1,
                                   "sort_order": order, "model": CURRENT_MODEL}, cutover)
            water = conn.execute("SELECT created_by FROM habits WHERE scope=? AND name=?",
                                 (scope, "\u559d\u6c34")).fetchone()
            if water and water["created_by"] == "__system__":
                r = self._rule_locked(conn, scope, "\u559d\u6c34", cutover, allow_disabled=True)
                self._write_locked(conn, scope, dict(r, enabled=0), cutover)
            # Custom tasks follow all default tasks, without resetting their targets.
            defaults = {x[0] for x in DEFAULT_RULES}
            order = len(DEFAULT_RULES)
            for r in self._rules_locked(conn, scope, cutover, include_disabled=True):
                if r["name"] not in defaults:
                    self._write_locked(conn, scope, dict(r, sort_order=order), cutover)
                    order += 1
            conn.execute("INSERT INTO score_profiles VALUES (?,?,?)",
                         (scope, self.clock().isoformat(timespec="seconds"), min(starts).isoformat()))

    @staticmethod
    def _rules_locked(conn, scope: str, day: date, include_disabled=False):
        rows = conn.execute("""
            SELECT r.* FROM habit_rules r WHERE r.scope=? AND r.since_day=(
                SELECT MAX(x.since_day) FROM habit_rules x
                WHERE x.scope=r.scope AND x.name=r.name AND x.since_day<=?)
            ORDER BY r.sort_order,r.name
        """, (scope, day.isoformat())).fetchall()
        return [dict(r) for r in rows if include_disabled or r["enabled"]]

    def _rule_locked(self, conn, scope, name, day, allow_disabled=False):
        for r in self._rules_locked(conn, scope, day, include_disabled=True):
            if r["name"] == name and (r["enabled"] or allow_disabled):
                return r
        raise HabitError(f"\u6ca1\u6709\u627e\u5230\u8be5\u65e5\u542f\u7528\u7684\u4e60\u60ef\u300c{name}\u300d\uff0c\u53ef\u53d1\u9001 /\u6253\u5361 \u6e05\u5355\u3002")

    @staticmethod
    def _write_locked(conn, scope, r, day):
        conn.execute("INSERT OR REPLACE INTO habit_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (scope, r["name"], day.isoformat(), r["target"], r["weight"], r["best_time"],
                      r["delay_minutes"], r["enabled"], r["sort_order"], r.get("model", CURRENT_MODEL)))
        conn.execute("UPDATE habits SET target=?,enabled=? WHERE scope=? AND name=?",
                     (r["target"], r["enabled"], scope, r["name"]))

    def list_habits(self, scope):
        self.ensure_rules(scope)
        with self._db() as conn:
            return self._rules_locked(conn, scope, self.today())

    def habit(self, scope, name):
        self.ensure_rules(scope)
        with self._db() as conn:
            return self._rule_locked(conn, scope, name, self.today())

    def rule_for_day(self, scope, name, day, allow_disabled=False):
        self.ensure_rules(scope)
        with self._db() as conn:
            return self._rule_locked(conn, scope, name, day, allow_disabled)

    def has_history(self, scope, name):
        with self._db() as conn:
            return bool(conn.execute("SELECT 1 FROM habits WHERE scope=? AND name=?", (scope, name)).fetchone())

    def configure(self, scope, name, *, target=None, weight=None, best_time=UNSET,
                  delay_minutes=None, enabled=None):
        self.ensure_rules(scope)
        if target is not None and (not isinstance(target, int) or not 1 <= target <= 100):
            raise HabitError("\u76ee\u6807\u6b21\u6570\u9700\u8981\u5728 1\uff5e100 \u4e4b\u95f4\u3002")
        if weight is not None:
            weight = validate_weight(weight)
        if best_time is not UNSET and best_time is not None:
            best_time = validate_clock(best_time)
        if delay_minutes is not None:
            delay_minutes = validate_delay(delay_minutes)
        day = self.today()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            r = self._rule_locked(conn, scope, name, day, allow_disabled=enabled is not None)
            for k, v in (("target", target), ("weight", weight), ("delay_minutes", delay_minutes), ("enabled", enabled)):
                if v is not None:
                    r[k] = v
            if best_time is not UNSET:
                r["best_time"] = best_time
            r["model"] = CURRENT_MODEL
            self._write_locked(conn, scope, r, day)
        return r

    def task_order(self, scope: str) -> list[str]:
        """Current presentation order, including disabled tasks for history."""
        self.ensure_rules(scope)
        with self._db() as conn:
            return [r["name"] for r in self._rules_locked(
                conn, scope, self.today(), include_disabled=True)]

    def reorder_tasks(self, scope: str, names: list[str], *, position: int | None = None) -> list[str]:
        """Reorder only this person's tasks for ALL dates, atomically.

        ``position`` is a 1-based target slot for exactly one task. Without a
        position, provided names become the prefix; unmentioned tasks retain
        their relative order. Disabled tasks remain configurable and are never
        deleted. Targets, scoring settings, events, and record counts are intact.
        """
        self.ensure_rules(scope)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ordered = [r["name"] for r in self._rules_locked(
                conn, scope, self.today(), include_disabled=True)]
            if not ordered:
                raise HabitError("当前没有可排序的任务。")
            if not names or len(set(names)) != len(names):
                raise HabitError("请提供不重复的任务名称。")
            unknown = [name for name in names if name not in ordered]
            if unknown:
                raise HabitError(f"找不到任务「{unknown[0]}」，请先发送 /打卡 任务 查看任务列表。")
            if position is not None:
                if len(names) != 1 or not 1 <= position <= len(ordered):
                    raise HabitError(f"排序位置必须在 1～{len(ordered)} 之间。")
                ordered.remove(names[0])
                ordered.insert(position - 1, names[0])
            else:
                ordered = names + [name for name in ordered if name not in names]
            for i, name in enumerate(ordered):
                # Display sorting applies to historical date snapshots too.
                conn.execute("UPDATE habit_rules SET sort_order=? WHERE scope=? AND name=?",
                             (i, scope, name))
            return ordered

    def add_habit(self, scope, name, target, user_id, cap=64):
        self.ensure_rules(scope)
        name = validate_name(name)
        super().add_habit(scope, name, target, user_id, cap)
        with self._db() as conn:
            order = conn.execute(
                "SELECT COALESCE(MAX(sort_order),?)+1 FROM habit_rules WHERE scope=?",
                (len(DEFAULT_RULES)-1, scope),
            ).fetchone()[0]
            self._write_locked(conn, scope, {"name": name, "target": target, "weight": 1.0,
                               "best_time": None, "delay_minutes": 60, "enabled": 1,
                               "sort_order": order, "model": CURRENT_MODEL}, self.today())

    def delete_habit(self, scope, name, user_id, is_admin):
        # Each person is the owner of their own task list, regardless of scene.
        self.configure(scope, name, enabled=0)

    def change_target(self, scope, name, target, user_id, is_admin):
        self.configure(scope, name, target=target)

    def restore_habit(self, scope, name):
        return self.configure(scope, name, enabled=1)

    def daily(self, scope, user_id, day):
        self.ensure_rules(scope)
        with self._db() as conn:
            rules = self._rules_locked(conn, scope, day)
            records = {r["name"]: dict(r) for r in conn.execute(
                "SELECT * FROM records WHERE scope=? AND user_id=? AND day=?",
                (scope, user_id, day.isoformat()))}
            by_name = {}
            for e in conn.execute("SELECT * FROM checkin_events WHERE scope=? AND user_id=? AND day=? ORDER BY id",
                                  (scope, user_id, day.isoformat())):
                by_name.setdefault(e["name"], []).append(dict(e))
        return [score_item(r, records.get(r["name"], {}).get("amount", 0),
                           by_name.get(r["name"], []), day, self.tz,
                           records.get(r["name"], {}).get("updated_at")) for r in rules]

    def checkin(self, scope, user_id, name, day, amount=1, *, checked_at=None,
                kind="normal", occurred_at=None):
        self.ensure_rules(scope)
        actual = checked_at or self.clock()
        if actual.tzinfo is None or actual.utcoffset() is None:
            raise HabitError("\u6253\u5361\u65f6\u95f4\u5fc5\u987b\u5305\u542b\u65f6\u533a\u3002")
        actual = actual.astimezone(self.tz)
        if kind not in ("normal", "backfill"):
            raise HabitError("\u672a\u77e5\u7684\u6253\u5361\u7c7b\u578b\u3002")
        if kind == "normal":
            if day != business_day(actual):
                raise HabitError("\u666e\u901a\u6253\u5361\u5fc5\u987b\u5c5e\u4e8e\u5f53\u524d 04:00 \u6362\u65e5\u7684\u6253\u5361\u65e5\u3002")
            occurred_at = actual
        if not 1 <= amount <= 100:
            raise HabitError("\u6b21\u6570\u9700\u8981\u5728 1\uff5e100 \u4e4b\u95f4\u3002")
        if day > business_day(actual):
            raise HabitError("\u4e0d\u80fd\u63d0\u524d\u4e3a\u672a\u6765\u65e5\u671f\u6253\u5361\u3002")
        if occurred_at is not None:
            if occurred_at.tzinfo is None or occurred_at > actual or business_day(occurred_at.astimezone(self.tz)) != day:
                raise HabitError("\u5b9e\u9645\u5b8c\u6210\u65f6\u95f4\u5fc5\u987b\u5c5e\u4e8e\u6240\u9009\u6253\u5361\u65e5\uff0c\u4e14\u4e0d\u80fd\u5728\u672a\u6765\u3002")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = self._rule_locked(conn, scope, name, day)
            if kind == "backfill" and item["best_time"] and occurred_at is None:
                raise HabitError("\u9650\u65f6\u4efb\u52a1\u8865\u7b7e\u9700\u63d0\u4f9b\u771f\u5b9e\u5b8c\u6210\u65f6\u95f4\uff1a/\u6253\u5361 \u8865\u7b7e " + name + " \u6628\u5929 23:45")
            row = conn.execute("SELECT amount FROM records WHERE scope=? AND user_id=? AND name=? AND day=?",
                               (scope, user_id, name, day.isoformat())).fetchone()
            old = row[0] if row else 0
            if old >= item["target"]:
                return dict(old=old, new=old, target=item["target"], changed=False, checked_at=None)
            new = min(item["target"], old + amount)
            timestamp = actual.isoformat(timespec="seconds")
            conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?) ON CONFLICT(scope,user_id,name,day) "
                         "DO UPDATE SET amount=excluded.amount,updated_at=excluded.updated_at",
                         (scope, user_id, name, day.isoformat(), new, timestamp))
            conn.execute("INSERT INTO checkin_events(scope,user_id,name,day,delta,checked_at,kind,occurred_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (scope, user_id, name, day.isoformat(), new-old, timestamp,
                          kind, occurred_at.isoformat(timespec="seconds") if occurred_at else None))
        return dict(old=old, new=new, target=item["target"], changed=True, checked_at=timestamp)

    def undo(self, scope, user_id, name, day):
        if not self.has_history(scope, name):
            raise HabitError("\u6ca1\u6709\u627e\u5230\u8fd9\u4e2a\u4e60\u60ef\u3002")
        with self._db() as conn:
            conn.execute("DELETE FROM checkin_events WHERE scope=? AND user_id=? AND name=? AND day=?",
                         (scope, user_id, name, day.isoformat()))
            cur = conn.execute("DELETE FROM records WHERE scope=? AND user_id=? AND name=? AND day=?",
                               (scope, user_id, name, day.isoformat()))
            return bool(cur.rowcount)

    def checkin_times(self, scope, user_id, day, name=None):
        entries = super().checkin_times(scope, user_id, day, name)
        with self._db() as conn:
            actuals = {tuple(r[k] for k in ("name", "checked_at", "delta", "kind")): r["occurred_at"]
                       for r in conn.execute("SELECT * FROM checkin_events WHERE scope=? AND user_id=? AND day=?",
                                             (scope, user_id, day.isoformat()))}
        for e in entries:
            e["occurred_at"] = actuals.get(tuple(e[k] for k in ("name", "checked_at", "delta", "kind")))
        return entries

    def month_scores(self, scope, user_id, year, month, today=None, chosen=None):
        self.ensure_rules(scope)
        today = today or self.today()
        if chosen is not None and not self.has_history(scope, chosen):
            raise HabitError(f"\u6ca1\u6709\u627e\u5230\u4e60\u60ef\u300c{chosen}\u300d\u3002")
        days = {}
        for n in range(1, calendar.monthrange(year, month)[1] + 1):
            day = date(year, month, n)
            if day > today:
                continue
            items = self.daily(scope, user_id, day)
            if chosen is not None:
                items = [i for i in items if i["name"] == chosen]
            metric = summarize(items)
            if items:
                if chosen is not None:
                    # Single optional-task calendars still show its own progress;
                    # it never changes daily/monthly aggregate denominators.
                    metric["display_ratio"] = items[0]["score"]
                else:
                    metric["display_ratio"] = metric["ratio"]
                days[n] = metric
        return days, month_summary(days)

    def group_rankings(self, group_scope, year, month):
        with self._db() as conn:
            members = [dict(r) for r in conn.execute("SELECT m.user_id,m.display_name FROM members m "
                       "JOIN person_accounts p ON p.id=m.user_id WHERE m.scope=? AND m.visible=1 "
                       "ORDER BY m.joined_at", (group_scope,))]
        ranked = []
        for m in members:
            _, stats = self.month_scores(self.person_scope(m["user_id"]), self.SELF, year, month)
            if stats["ratio"] is not None:
                ranked.append(dict(m, **stats))
        return sorted(ranked, key=lambda x: (-x["ratio"], -x["earned"]))[:30]
