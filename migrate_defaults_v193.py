#!/usr/bin/env python3
"""One-time migration of existing AstrBot daka users to the v1.9.3 defaults.

Safe by default: report-only unless --apply is supplied. Makes a SQLite online
backup before changes. It intentionally deletes all old "工作一小时" task data
for every scope, then adds/updates the eight default tasks and appends today's
60-minute rules without rewriting earlier scoring history.
"""
from __future__ import annotations
import argparse
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULTS = (
    ("睡觉", 1, "23:30"),
    ("起床", 1, "08:00"),
    ("早餐", 1, "09:00"),
    ("午餐", 1, "12:00"),
    ("晚餐", 1, "18:00"),
    ("运动", 1, None),
    ("规划待办", 1, "10:00"),
    ("完成待办", 1, "22:00"),
)
REMOVE = "工作一小时"

def business_day(tz_name: str) -> str:
    now = datetime.now(ZoneInfo(tz_name))
    return (now - timedelta(hours=4)).date().isoformat()

def discover_db(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"数据库不存在：{path}")
        return path
    candidates = [
        Path("/opt/services/astrbot/data/plugin_data/astrbot_plugin_daka/checkin.sqlite3"),
        Path("/opt/services/astrbot/data/plugin_data/astrbot_plugin_check_in/checkin.sqlite3"),
    ]
    found = [p for p in candidates if p.is_file()]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise SystemExit("发现多个数据库，请使用 --db 明确指定正确文件。")
    raise SystemExit("未找到默认数据库。请使用 --db /path/to/checkin.sqlite3 指定。")

def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def main():
    parser = argparse.ArgumentParser(description="迁移旧用户到打卡插件 v1.9.3 默认任务")
    parser.add_argument("--db", help="checkin.sqlite3 的实际路径")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="业务时区，默认 Asia/Shanghai")
    parser.add_argument("--apply", action="store_true", help="实际执行；默认只预览")
    args = parser.parse_args()
    db = discover_db(args.db)
    day = business_day(args.timezone)
    backup_dir = db.parent / "backups" / "astrbot_plugin_daka" / "migration"
    print(f"数据库：{db}")
    print(f"生效业务日期：{day}（04:00 换日）")
    print(f"模式：{'实际执行' if args.apply else '只预览，不修改'}")
    print("将对所有个人账户和旧版 scope 执行以下迁移：")
    print(f"  删除「{REMOVE}」的任务定义、规则、每日记录、打卡事件")
    for name, target, best in DEFAULTS:
        print(f"  确保 {name}: {target}/天，权重 1，最佳时间 {best or '不限时'}，缓降尺度 H=60 分钟")
    with sqlite3.connect(str(db), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        required = {"groups", "habits", "records", "checkin_events", "habit_rules"}
        missing = [t for t in required if not table_exists(conn, t)]
        if missing:
            raise SystemExit(f"数据库缺少必要表 {missing}，可能指定了错误文件或版本太旧。")
        scopes = [r[0] for r in conn.execute("SELECT DISTINCT scope FROM habits")]
        print(f"检测到任务 scope 数量：{len(scopes)}")
        for scope in scopes[:12]:
            names = [r[0] for r in conn.execute("SELECT name FROM habits WHERE scope=? ORDER BY name", (scope,))]
            print(f"  {scope}: {', '.join(names[:20]) or '(无任务)'}")
        if len(scopes) > 12:
            print(f"  ... 另有 {len(scopes)-12} 个 scope")
        old_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE name=?", (REMOVE,)).fetchone()[0]
                      for t in ("habits", "habit_rules", "records", "checkin_events")}
        print(f"预计删除「{REMOVE}」：{old_counts}")
        if not args.apply:
            print("预览结束。确认数据库路径和删除范围正确后，添加 --apply 执行。")
            return 0
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(backup_dir, 0o700)
        except OSError:
            pass
        backup = backup_dir / f"before-defaults-v193-{datetime.now():%Y%m%d-%H%M%S-%f}-{secrets.token_hex(3)}.sqlite3"
        with sqlite3.connect(str(backup), timeout=30) as dest:
            conn.backup(dest)
        os.chmod(backup, 0o600)
        print(f"备份：{backup}")
        now = datetime.now(ZoneInfo(args.timezone)).isoformat(timespec="seconds")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("checkin_events", "records", "habit_rules", "habits"):
                conn.execute(f"DELETE FROM {table} WHERE name=?", (REMOVE,))
            scopes = sorted(set(r[0] for r in conn.execute("SELECT DISTINCT scope FROM groups")) |
                            set(r[0] for r in conn.execute("SELECT DISTINCT scope FROM habits")))
            # Convert legacy default names without silently overwriting data.
            for scope in scopes:
                for old_name, new_name in (("早睡", "睡觉"), ("早起", "起床")):
                    old_exists = conn.execute(
                        "SELECT 1 FROM habits WHERE scope=? AND name=?", (scope, old_name)
                    ).fetchone()
                    if not old_exists:
                        continue
                    new_exists = conn.execute(
                        "SELECT 1 FROM habits WHERE scope=? AND name=?", (scope, new_name)
                    ).fetchone()
                    target_data = any(conn.execute(
                        f"SELECT 1 FROM {table} WHERE scope=? AND name=? LIMIT 1",
                        (scope, new_name),
                    ).fetchone() for table in ("records", "checkin_events", "habit_rules"))
                    if not new_exists and not target_data:
                        for table in ("habits", "records", "checkin_events", "habit_rules"):
                            conn.execute(f"UPDATE {table} SET name=? WHERE scope=? AND name=?",
                                         (new_name, scope, old_name))
                    else:
                        # If both names have history, retain the old history but
                        # disable the old task rather than risking a unique-key collision.
                        current = conn.execute(
                            "SELECT target,weight,best_time,delay_minutes,sort_order,model "
                            "FROM habit_rules WHERE scope=? AND name=? AND since_day<=? "
                            "ORDER BY since_day DESC LIMIT 1", (scope, old_name, day)
                        ).fetchone()
                        conn.execute("UPDATE habits SET enabled=0 WHERE scope=? AND name=?",
                                     (scope, old_name))
                        if current:
                            conn.execute(
                                "INSERT INTO habit_rules(scope,name,since_day,target,weight,best_time,delay_minutes,enabled,sort_order,model) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,name,since_day) DO UPDATE SET enabled=0",
                                (scope, old_name, day, current["target"], current["weight"],
                                 current["best_time"], current["delay_minutes"], 0,
                                 current["sort_order"], current["model"]),
                            )
                # "喝水" was a legacy system default, not part of the new template.
                # Disable only the system-created task; preserve custom user tasks.
                water = conn.execute(
                    "SELECT created_by FROM habits WHERE scope=? AND name='喝水'", (scope,)
                ).fetchone()
                if water and water["created_by"] == "__system__":
                    current = conn.execute(
                        "SELECT target,weight,best_time,delay_minutes,sort_order,model "
                        "FROM habit_rules WHERE scope=? AND name='喝水' AND since_day<=? "
                        "ORDER BY since_day DESC LIMIT 1", (scope, day)
                    ).fetchone()
                    conn.execute("UPDATE habits SET enabled=0 WHERE scope=? AND name='喝水'", (scope,))
                    if current:
                        conn.execute(
                            "INSERT INTO habit_rules(scope,name,since_day,target,weight,best_time,delay_minutes,enabled,sort_order,model) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,name,since_day) DO UPDATE SET enabled=0",
                            (scope, "喝水", day, current["target"], current["weight"],
                             current["best_time"], current["delay_minutes"], 0,
                             current["sort_order"], current["model"]),
                        )
            for scope in scopes:
                # Defaults occupy fixed positions 0..7; custom tasks are
                # moved after them with a new rule effective from this day.
                for offset, (name, target, best) in enumerate(DEFAULTS):
                    existing = conn.execute("SELECT 1 FROM habits WHERE scope=? AND name=?", (scope, name)).fetchone()
                    if existing:
                        conn.execute("UPDATE habits SET target=?,enabled=1 WHERE scope=? AND name=?",
                                     (target, scope, name))
                    else:
                        conn.execute("INSERT INTO habits(scope,name,target,created_by,enabled,created_at) "
                                     "VALUES (?,?,?,'__system__',1,?)", (scope, name, target, now))
                    conn.execute(
                        "INSERT INTO habit_rules(scope,name,since_day,target,weight,best_time,delay_minutes,enabled,sort_order,model) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,name,since_day) DO UPDATE SET "
                        "target=excluded.target,weight=excluded.weight,best_time=excluded.best_time,"
                        "delay_minutes=excluded.delay_minutes,enabled=1,sort_order=excluded.sort_order,model='smooth_decay'",
                        (scope, name, day, target, 1.0, best, 60, 1,
                         offset, "smooth_decay"))
                default_names = {item[0] for item in DEFAULTS}
                custom_names = [r[0] for r in conn.execute(
                    "SELECT name FROM habits WHERE scope=? ORDER BY created_at,rowid", (scope,)
                ).fetchall() if r[0] not in default_names]
                for custom_index, custom_name in enumerate(custom_names, start=len(DEFAULTS)):
                    current = conn.execute(
                        "SELECT target,weight,best_time,delay_minutes,enabled,model FROM habit_rules "
                        "WHERE scope=? AND name=? AND since_day<=? ORDER BY since_day DESC LIMIT 1",
                        (scope, custom_name, day),
                    ).fetchone()
                    if current:
                        conn.execute(
                            "INSERT INTO habit_rules(scope,name,since_day,target,weight,best_time,delay_minutes,enabled,sort_order,model) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,name,since_day) DO UPDATE SET "
                            "sort_order=excluded.sort_order",
                            (scope, custom_name, day, current["target"], current["weight"],
                             current["best_time"], current["delay_minutes"], current["enabled"],
                             custom_index, current["model"]),
                        )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    print("迁移完成。既有其他习惯与非「工作一小时」历史记录未删除。")
    print("注意：这是全用户数据迁移；请重启 AstrBot 后检查 /打卡 任务。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
