"""v1.9.6 unified historical scoring and personal presentation order.

SQLite, command and Pillow unit/integration coverage only; no live QQ calls.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

import pytest

from test_regressions import resolve, plugin  # Install AstrBot offline stubs first.

from astrbot_plugin_daka.scoring import CURRENT_MODEL, late_factor
from astrbot_plugin_daka.scored_store import ScoredStore
from astrbot_plugin_daka.store import HabitError
from astrbot_plugin_daka.weighted_calendar import _task_names, render_weighted
from astrbot_plugin_daka.scoring import summarize
from test_v195_smooth_decay import factor

TZ = ZoneInfo('Asia/Shanghai')
D = date(2026, 10, 17)
D2 = date(2026, 10, 18)


def dt(d: int, h: int, m: int = 0):
    return datetime(2026, 10, d, h, m, tzinfo=TZ)


def seed_store(tmp_path):
    now = [dt(17, 12)]
    db = ScoredStore(tmp_path / 'checkin.sqlite3', TZ, clock=lambda: now[0])
    scope = db.person_scope(resolve(db))
    now[0] = dt(18, 12)
    return db, scope, now


@pytest.mark.parametrize('model', ['smooth_decay', 'half_life', 'weighted', 'legacy', 'nonsense'])
def test_curve_has_no_old_model_switch_anymore(model):
    got = late_factor(D, '08:00', 60, dt(17, 9), TZ, model=model)
    assert got == pytest.approx(factor(1))
    assert got != pytest.approx(0.5)


def test_unified_engine_migrates_all_historic_models_without_touching_events(tmp_path):
    db, scope, now = seed_store(tmp_path)
    db.checkin(scope, 'self', '起床', D, checked_at=dt(17, 9))
    db.checkin(scope, 'self', '起床', D2, checked_at=dt(18, 9))
    # Date-specific weight/target settings are retained, not moved to today.
    with db._db() as conn:
        conn.execute("UPDATE habit_rules SET model='half_life' WHERE scope=? AND name='起床' AND since_day=?",
                     (scope, D.isoformat()))
        conn.execute("UPDATE habit_rules SET model='weighted' WHERE scope=? AND name='起床' AND since_day=?",
                     (scope, D2.isoformat()))
        conn.execute("UPDATE habit_rules SET model='legacy' WHERE scope=? AND name='运动'", (scope,))
        conn.execute("DELETE FROM score_engine_migrations WHERE name='v1.9.6-unified-smooth-decay'")
        original_events = [tuple(x) for x in conn.execute(
            'SELECT * FROM checkin_events WHERE scope=? ORDER BY id', (scope,))]
        original_records = [tuple(x) for x in conn.execute(
            'SELECT * FROM records WHERE scope=? ORDER BY day,name', (scope,))]
        original_rules = [tuple(x) for x in conn.execute(
            'SELECT scope,name,since_day,target,weight,best_time,delay_minutes,enabled,sort_order '
            'FROM habit_rules WHERE scope=? ORDER BY name,since_day', (scope,))]
    upgraded = ScoredStore(tmp_path / 'checkin.sqlite3', TZ, clock=lambda: now[0])
    for day in (D, D2):
        item = next(i for i in upgraded.daily(scope, 'self', day) if i['name'] == '起床')
        assert item['model'] == CURRENT_MODEL
        assert item['score'] == pytest.approx(factor(1))
        assert item['earned'] == pytest.approx(factor(1))
    month, stats = upgraded.month_scores(scope, 'self', 2026, 10, D2)
    assert month[17]['items'][1]['score'] == pytest.approx(factor(1))
    assert month[18]['items'][1]['score'] == pytest.approx(factor(1))
    assert stats['ratio'] == pytest.approx(factor(1) / 8)
    with upgraded._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM habit_rules WHERE model<>?", (CURRENT_MODEL,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM score_engine_migrations "
                            "WHERE name='v1.9.6-unified-smooth-decay'").fetchone()[0] == 1
        assert original_events == [tuple(x) for x in conn.execute(
            'SELECT * FROM checkin_events WHERE scope=? ORDER BY id', (scope,))]
        assert original_records == [tuple(x) for x in conn.execute(
            'SELECT * FROM records WHERE scope=? ORDER BY day,name', (scope,))]
        assert original_rules == [tuple(x) for x in conn.execute(
            'SELECT scope,name,since_day,target,weight,best_time,delay_minutes,enabled,sort_order '
            'FROM habit_rules WHERE scope=? ORDER BY name,since_day', (scope,))]
    again = ScoredStore(tmp_path / 'checkin.sqlite3', TZ, clock=lambda: now[0])
    assert next(i for i in again.daily(scope, 'self', D) if i['name'] == '起床')['score'] == pytest.approx(factor(1))


def test_every_date_recomputes_even_if_stale_model_flag_is_reinserted(tmp_path):
    db, scope, now = seed_store(tmp_path)
    db.checkin(scope, 'self', '起床', D, checked_at=dt(17, 9))
    with db._db() as conn:
        conn.execute("UPDATE habit_rules SET model='half_life' WHERE scope=? AND name='起床'", (scope,))
    assert next(i for i in db.daily(scope, 'self', D) if i['name'] == '起床')['score'] == pytest.approx(factor(1))
    assert 'half_life' not in [i['model'] for i in db.daily(scope, 'self', D)]


def test_historical_rules_preserve_best_times_and_weights_after_unification(tmp_path):
    db, scope, now = seed_store(tmp_path)
    # Manually restore prior-day special settings as if recorded by old version.
    with db._db() as conn:
        conn.execute("UPDATE habit_rules SET best_time='08:00',delay_minutes=60,weight=2.0,model='half_life' "
                     "WHERE scope=? AND name='起床' AND since_day=?", (scope, D.isoformat()))
    db.checkin(scope, 'self', '起床', D, checked_at=dt(17, 9))
    db.configure(scope, '起床', best_time='07:00', weight=3.0, delay_minutes=120)
    past = next(i for i in db.daily(scope, 'self', D) if i['name'] == '起床')
    present = next(i for i in db.daily(scope, 'self', D2) if i['name'] == '起床')
    assert (past['best_time'],past['delay_minutes'],past['weight']) == ('08:00',60,2)
    assert past['earned'] == pytest.approx(2*factor(1))
    assert (present['best_time'],present['delay_minutes'],present['weight']) == ('07:00',120,3)


def test_move_reorders_all_date_snapshots_without_mutating_earned_scores(tmp_path):
    db, scope, now = seed_store(tmp_path)
    db.checkin(scope, 'self', '起床', D, checked_at=dt(17, 9))
    daily_before = db.daily(scope, 'self', D)
    scores_before = {r['name']: r['earned'] for r in daily_before}
    stats_before = summarize(daily_before)
    with db._db() as conn:
        events_before = [tuple(e) for e in conn.execute('SELECT * FROM checkin_events ORDER BY id')]
    names = db.reorder_tasks(scope, ['运动'], position=2)
    assert names[:4] == ['睡觉','运动','起床','早餐']
    assert db.task_order(scope) == names
    for day in (D, D2):
        assert [r['name'] for r in db.daily(scope, 'self', day)][:4] == names[:4]
        with db._db() as conn:
            assert [r['name'] for r in db._rules_locked(conn, scope, day, True)][:4] == names[:4]
    assert {r['name']:r['earned'] for r in db.daily(scope, 'self', D)} == scores_before
    assert summarize(db.daily(scope, 'self', D))['ratio'] == stats_before['ratio']
    with db._db() as conn:
        assert events_before == [tuple(e) for e in conn.execute('SELECT * FROM checkin_events ORDER BY id')]


def test_batch_sort_append_unlisted_and_keep_other_account_intact(tmp_path):
    db, scope, now = seed_store(tmp_path)
    from test_regressions import DM, OTHER, identity_aliases
    account2 = db.resolve_person(identity_aliases(DM.replace('A'*32,OTHER), OTHER, False,
                    adapter_type='qq_official', app_id='12345'), DM.replace('A'*32,OTHER), OTHER)
    other = db.person_scope(account2)
    other_before = db.task_order(other)
    db.delete_habit(scope,'晚餐','self',False)
    baseline = db.task_order(scope)
    result = db.reorder_tasks(scope,['运动','睡觉','早餐'])
    assert result == ['运动','睡觉','早餐'] + [n for n in baseline if n not in ('运动','睡觉','早餐')]
    assert '晚餐' in result
    assert db.task_order(other) == other_before
    with pytest.raises(HabitError): db.reorder_tasks(scope,['运动','运动'])
    with pytest.raises(HabitError): db.reorder_tasks(scope,['不存在的任务'],position=1)
    with pytest.raises(HabitError): db.reorder_tasks(scope,['运动'],position=0)
    with pytest.raises(HabitError): db.reorder_tasks(scope,['运动'],position=100)
    assert db.task_order(scope) == result


def test_calendar_palette_columns_follow_requested_current_order():
    days={1:{'items':[{'name':'睡觉'},{'name':'起床'}]},
          2:{'items':[{'name':'睡觉'},{'name':'起床'},{'name':'运动'}]}}
    assert _task_names(days, ['运动','起床','睡觉']) == ['运动','起床','睡觉']
    assert _task_names(days, ['运动','起床']) == ['运动','起床','睡觉']


def test_plugin_sort_commands_and_errors(plugin):
    scope = plugin.store.person_scope(resolve(plugin.store))
    reply,_=plugin._dispatch('任务 排序',scope,'self','小明',False)
    assert '运动' in reply and '1. 睡觉' in reply
    reply,_=plugin._dispatch('任务 排序 运动 2',scope,'self','小明',False)
    assert '运动' in reply and plugin.store.task_order(scope)[1]=='运动'
    reply,_=plugin._dispatch('任务 排序 早餐 运动 睡觉',scope,'self','小明',False)
    assert '已更新' in reply
    assert plugin.store.task_order(scope)[:3] == ['早餐','运动','睡觉']
    with pytest.raises(HabitError): plugin._dispatch('任务 排序 运动 0',scope,'self','小明',False)
    with pytest.raises(HabitError): plugin._dispatch('任务 排序 运动 500',scope,'self','小明',False)
    with pytest.raises(HabitError): plugin._dispatch('任务 排序 运动 运动',scope,'self','小明',False)


def test_month_and_daily_color_views_use_same_order(tmp_path, monkeypatch, plugin):
    scope = plugin.store.person_scope(resolve(plugin.store))
    plugin.store.reorder_tasks(scope,['运动'],position=1)
    days,stats=plugin.store.month_scores(scope,'self',2026,10,D)
    assert [i['name'] for i in plugin.store.daily(scope,'self',D)][0]=='运动'
    seen=[]
    import astrbot_plugin_daka.main as main
    monkeypatch.setattr(main, 'render_weighted', lambda *a, **kw: seen.append(kw['task_order']) or None)
    plugin._make_calendar(scope,'self','小明',None,2026,10,D)
    assert seen and seen[0][0]=='运动'
    # Exercise path through the actual image renderer too (Pillow installed).
    out=tmp_path/'calendar.png'
    assert render_weighted(out,2026,10,days,stats,D,'小明',task_order=seen[0])
    assert out.is_file() and out.stat().st_size > 10_000


def test_readme_and_version_explain_unified_history_and_sort():
    root=Path(__file__).resolve().parents[1]
    doc=(root/'README.md').read_text(encoding='utf-8')
    assert 'v1.9.6' in doc and '历史统一计分' in doc
    assert '/打卡 任务 排序 运动 2' in doc
    assert '原始打卡记录不变' in doc
    assert '按打卡日安全迁移' not in doc
    version_line = next(line for line in (root/'metadata.yaml').read_text(encoding='utf-8').splitlines()
                        if line.startswith('version:'))
    assert tuple(map(int, version_line.split(':', 1)[1].strip().split('.'))) >= (1, 9, 6)


def test_v196_bootstrap_backup_captures_old_sqlite_before_history_rewrite(tmp_path, monkeypatch):
    import astrbot_plugin_daka.main as main
    from types import SimpleNamespace

    path = tmp_path/'plugin_data'/'astrbot_plugin_daka'/'checkin.sqlite3'
    path.parent.mkdir(parents=True)
    old = ScoredStore(path, TZ, clock=lambda:dt(17, 11))
    scope = old.person_scope(resolve(old))
    old.checkin(scope,'self','起床',D,checked_at=dt(17,9))
    with old._db() as conn:
        conn.execute("UPDATE habit_rules SET model='half_life' WHERE scope=?", (scope,))
        conn.execute("DELETE FROM score_engine_migrations WHERE name='v1.9.6-unified-smooth-decay'")
        conn.execute("PRAGMA user_version=10905")
        original_event = tuple(conn.execute('SELECT * FROM checkin_events WHERE scope=?', (scope,)).fetchone())
    monkeypatch.setattr(main,'get_astrbot_data_path',lambda:str(tmp_path))
    instance = main.GroupDaka(SimpleNamespace(), {})
    backup_dir = tmp_path/'backups'/'astrbot_plugin_daka'
    copies = sorted(backup_dir.glob('pre-1.9.6-*.sqlite3'))
    assert len(copies)==1
    with sqlite3.connect(copies[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM habit_rules WHERE model='half_life'").fetchone()[0] > 0
        assert tuple(conn.execute('SELECT * FROM checkin_events WHERE scope=?', (scope,)).fetchone()) == original_event
    with instance.store._db() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==10906
        assert conn.execute("SELECT COUNT(*) FROM habit_rules WHERE model<>'smooth_decay'").fetchone()[0]==0
        assert tuple(conn.execute('SELECT * FROM checkin_events WHERE scope=?', (scope,)).fetchone()) == original_event
    # Database version marker prevents duplicate snapshots on restart.
    another = main.GroupDaka(SimpleNamespace(), {})
    assert len(list(backup_dir.glob('pre-1.9.6-*.sqlite3')))==1
    assert next(i for i in another.store.daily(scope,'self',D) if i['name']=='起床')['score']==pytest.approx(factor(1))


def test_missing_legacy_completion_timestamp_is_not_fabricated(tmp_path):
    db,scope,now=seed_store(tmp_path)
    db.checkin(scope,'self','起床',D,checked_at=dt(17,9))
    # Old backfilled records can have a known count but unknown completion time.
    with db._db() as conn:
        conn.execute("UPDATE checkin_events SET kind='backfill', occurred_at=NULL WHERE scope=?", (scope,))
    item=next(i for i in db.daily(scope,'self',D) if i['name']=='起床')
    assert item['amount']==1 and item['time_unknown']
    assert item['score']==1 and item['penalized'] is False


def test_historic_sort_only_changes_sort_order_columns(tmp_path):
    db,scope,now=seed_store(tmp_path)
    db.configure(scope,'早餐',weight=2)
    db.checkin(scope,'self','早餐',D,checked_at=dt(17,9))
    columns='scope,name,since_day,target,weight,best_time,delay_minutes,enabled,model'
    with db._db() as conn:
        other_rules_before=[tuple(r) for r in conn.execute(
            f'SELECT {columns} FROM habit_rules WHERE scope=? ORDER BY name,since_day',(scope,))]
    db.reorder_tasks(scope,['运动','早餐'])
    with db._db() as conn:
        assert other_rules_before==[tuple(r) for r in conn.execute(
            f'SELECT {columns} FROM habit_rules WHERE scope=? ORDER BY name,since_day',(scope,))]


def test_long_numeric_position_is_rejected_cleanly(plugin):
    scope=plugin.store.person_scope(resolve(plugin.store))
    with pytest.raises(HabitError):
        plugin._dispatch('任务 排序 运动 '+('9'*5000),scope,'self','User',False)
