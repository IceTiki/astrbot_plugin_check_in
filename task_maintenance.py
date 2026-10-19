"""Administrator-only per-person task maintenance.

Every change uses one SQLite transaction and checks a preview fingerprint.
No other accounts, group data, or historical identity imports are modified.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3

from .store import HabitError, validate_name

TABLES = ("habits", "habit_rules", "records", "checkin_events")


def _check_scope(scope: str):
    if not scope.startswith("person:") or len(scope) <= len("person:"):
        raise HabitError("调试操作只能处理当前个人账户的任务。")


def _task_row(conn, scope, name):
    return conn.execute("SELECT * FROM habits WHERE scope=? AND name=?",
                        (scope, name)).fetchone()


def _inspect_locked(conn, scope, old: str, new: str | None):
    source = _task_row(conn, scope, old)
    if source is None:
        raise HabitError(f"没有找到名为「{old}」的任务（已停用的任务也会检查）。")
    target = _task_row(conn, scope, new) if new else None

    count = {}
    for table in TABLES:
        count[table] = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE scope=? AND name=?",
            (scope, old)).fetchone()[0])
    overlap = 0
    if target is not None:
        overlap = int(conn.execute(
            "SELECT COUNT(*) FROM records a JOIN records b "
            "ON a.scope=b.scope AND a.user_id=b.user_id AND a.day=b.day "
            "WHERE a.scope=? AND a.name=? AND b.name=?",
            (scope, old, new)).fetchone()[0])

    # The fingerprint includes both names across all four tables, so checking
    # it at confirmation detects changed records, event timestamps and rules.
    digest = hashlib.sha256()
    for table in TABLES:
        for name in ([old, new] if new is not None else [old]):
            digest.update(f"{table}:{name}\n".encode("utf-8"))
            if name is None:
                continue
            rows = conn.execute(f"SELECT * FROM {table} WHERE scope=? AND name=? ORDER BY rowid",
                                (scope, name)).fetchall()
            for row in rows:
                digest.update(json.dumps(tuple(row), ensure_ascii=False,
                                         separators=(",", ":"), default=str).encode("utf-8"))
                digest.update(b"\n")
    return {
        "source": old,
        "destination": new,
        "merge": target is not None,
        "source_enabled": bool(source["enabled"]),
        "destination_enabled": bool(target["enabled"]) if target else None,
        "counts": count,
        "overlapping_days": overlap,
        "fingerprint": digest.hexdigest(),
    }


def inspect_task(store, scope: str, old: str, new: str | None = None) -> dict:
    """Read-only preview (except normal lazy rules initialization)."""
    _check_scope(scope)
    old = validate_name(old)
    if new is not None:
        new = validate_name(new)
        if old == new:
            raise HabitError("新旧任务名称不能相同。")
    store.ensure_rules(scope)
    with store._db() as conn:
        return _inspect_locked(conn, scope, old, new)


def backup_before_edit(db_path: Path, backup_directory: Path, label: str) -> Path:
    """Consistent SQLite snapshot including WAL; created before any mutation."""
    backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(backup_directory, 0o700)
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_label = "rename" if label == "rename" else "purge"
    output = backup_directory / f"before-{safe_label}-{stamp}-{secrets.token_hex(3)}.sqlite3"
    with sqlite3.connect(str(db_path), timeout=15) as source:
        with sqlite3.connect(str(output), timeout=15) as destination:
            source.backup(destination)
    os.chmod(output, 0o600)
    return output


def rename_task(store, scope: str, old: str, new: str, expected: str) -> dict:
    """Rename or merge; all task-related edits commit or roll back together.

    Merge: destination settings win where their history exists. Earlier source
    rules migrate to keep earlier-only check-in dates visible. Per-day counts
    sum but are capped at the destination target for that date. All source
    event timestamps survive unmodified.
    """
    _check_scope(scope)
    old, new = validate_name(old), validate_name(new)
    if old == new:
        raise HabitError("新旧任务名称不能相同。")
    with store._db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        info = _inspect_locked(conn, scope, old, new)
        if info["fingerprint"] != expected:
            raise HabitError("预览后任务或记录发生变化，请重新发送更名命令生成新确认码。")
        if not info["merge"]:
            # Extra protection against orphaned records in malformed imports.
            for table in TABLES:
                if conn.execute(f"SELECT 1 FROM {table} WHERE scope=? AND name=? LIMIT 1",
                                (scope, new)).fetchone():
                    raise HabitError("目标名称在历史表中已存在残留数据，已中止以避免覆盖。")
            for table in TABLES:
                conn.execute(f"UPDATE {table} SET name=? WHERE scope=? AND name=?",
                             (new, scope, old))
            return dict(info, result="renamed", merged_records=0)

        # First migrate only the portion of source rule history which predates
        # the destination's initial rule. Do not override existing rules.
        first_dest = conn.execute(
            "SELECT MIN(since_day) FROM habit_rules WHERE scope=? AND name=?",
            (scope, new)).fetchone()[0]
        early = conn.execute("SELECT * FROM habit_rules WHERE scope=? AND name=? "
                             "ORDER BY since_day", (scope, old)).fetchall()
        for r in early:
            if first_dest is None or r["since_day"] < first_dest:
                conn.execute(
                    "INSERT OR IGNORE INTO habit_rules (scope,name,since_day,target,weight,"
                    "best_time,delay_minutes,enabled,sort_order,model) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (scope, new, r["since_day"], r["target"], r["weight"],
                     r["best_time"], r["delay_minutes"], r["enabled"], r["sort_order"],
                     r["model"]),
                )

        new_habit = _task_row(conn, scope, new)
        rows = conn.execute(
            "SELECT * FROM records WHERE scope=? AND name=? ORDER BY day,user_id",
            (scope, old)).fetchall()
        for r in rows:
            dest = conn.execute(
                "SELECT amount,updated_at FROM records "
                "WHERE scope=? AND user_id=? AND name=? AND day=?",
                (scope, r["user_id"], new, r["day"])).fetchone()
            dest_amount = int(dest["amount"]) if dest else 0
            historical_rule = conn.execute(
                "SELECT target FROM habit_rules WHERE scope=? AND name=? AND since_day<=? "
                "ORDER BY since_day DESC LIMIT 1",
                (scope, new, r["day"])).fetchone()
            limit = int(historical_rule["target"]) if historical_rule else int(new_habit["target"])
            # A later target decrease may leave an existing count above limit.
            # Preserve the destination count in that case; never reduce it.
            total = max(dest_amount, min(limit, dest_amount + int(r["amount"])))
            updated = max(str(r["updated_at"]), str(dest["updated_at"])) if dest else r["updated_at"]
            conn.execute(
                "INSERT INTO records(scope,user_id,name,day,amount,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(scope,user_id,name,day) DO UPDATE SET "
                "amount=excluded.amount,updated_at=excluded.updated_at",
                (scope, r["user_id"], new, r["day"], total, updated),
            )
        conn.execute("DELETE FROM records WHERE scope=? AND name=?", (scope, old))
        # Events have no unique (name,day) constraint, so all original times
        # are kept even when two names were completed on the same day.
        conn.execute("UPDATE checkin_events SET name=? WHERE scope=? AND name=?",
                     (new, scope, old))
        conn.execute("DELETE FROM habit_rules WHERE scope=? AND name=?", (scope, old))
        conn.execute("DELETE FROM habits WHERE scope=? AND name=?", (scope, old))
        return dict(info, result="merged", merged_records=len(rows))


def purge_task(store, scope: str, name: str, expected: str) -> dict:
    """Permanently remove a named task from all active per-person task tables."""
    _check_scope(scope)
    name = validate_name(name)
    with store._db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        info = _inspect_locked(conn, scope, name, None)
        if info["fingerprint"] != expected:
            raise HabitError("预览后任务或记录发生变化，请重新发送彻底删除命令生成新确认码。")
        actual = {}
        # Delete children before definition; no FK cascades across persons.
        for table in ("checkin_events", "records", "habit_rules", "habits"):
            actual[table] = conn.execute(
                f"DELETE FROM {table} WHERE scope=? AND name=?",
                (scope, name)).rowcount
        return dict(info, deleted=actual)
