"""v1.7 historical half-life decay, settings and migration integration tests.

The AstrBot transport is mocked: these checks do NOT prove QQ button delivery.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo
import asyncio
import math

import pytest

from test_regressions import resolve, Event, plugin  # Install offline stubs.

from astrbot_plugin_daka.scoring import late_factor, score_item, business_day
from astrbot_plugin_daka.scored_store import ScoredStore
from astrbot_plugin_daka.panel import timing_line, daily_panel
from test_v16_scoring import D, dt, Z, WAKE, SLEEP, WORK, SPORT, item, record


@pytest.mark.parametrize("when", [
    dt(17,23,0), dt(17,23,30), dt(17,23,30,1), dt(17,23,35),
    dt(17,23,40), dt(17,23,45), dt(18,0,0), dt(18,0,15),
    dt(18,0,30), dt(18,1,0),
])
def test_old_half_life_flag_uses_current_smooth_curve(when):
    minutes=max(0,(when-dt(17,23,30)).total_seconds()/60)
    x=minutes/30
    factor=(1+math.log(2)*x)*2**(-x)
    assert late_factor(D,'23:30',30,when,Z,model='half_life')==pytest.approx(factor)


def test_continuity_and_monotonicity_over_the_second():
    best = dt(17, 8)
    values = [late_factor(D, '08:00', 30, datetime.fromtimestamp(best.timestamp() + i, Z), Z)
              for i in range(0, 31 * 60, 15)]
    assert values[0] == 1
    assert all(a > b > 0 for a, b in zip(values, values[1:]))
    assert values[1] > .99
    assert values[-1] > .49


def test_midnight_target_anchored_to_next_calendar_day():
    assert late_factor(D, '00:00', 30, dt(18,0,0), Z, model='half_life') == 1
    assert late_factor(D, '00:00', 30, dt(18,0,30), Z, model='half_life') == pytest.approx((1+math.log(2))/2)
    assert business_day(dt(18,0,30)) == D


def test_no_time_rule_always_full():
    assert late_factor(D, None, 30, dt(18,0), Z) == 1
    assert late_factor(D, '23:30', 30, None, Z) == 1


def test_daily_score_and_panel_use_smooth_factor(tmp_path):
    db = ScoredStore(tmp_path/'new.sqlite3', Z, clock=lambda:dt(17,21))
    person = resolve(db); scope = db.person_scope(person)
    record(db,scope,SLEEP,dt(17,23,45))
    result=item(db,scope,SLEEP)
    assert result['model']=='smooth_decay'
    assert result['score']==pytest.approx((1+math.log(2)/4)*2**(-.25))
    assert result['penalized']
    label=timing_line(result,D,dt(17,23,45),Z)
    assert '缓降 H=60分' in label and '次日00:30约84.7%' in label
    panel=daily_panel(db.daily(scope,'self',D),'user',D,dt(17,23,45),Z)
    assert '0.99/1' in panel
    assert '缓降 H=60分' in panel


def test_three_repeated_timed_events_weight_one(tmp_path):
    db=ScoredStore(tmp_path/'work.sqlite3',Z,clock=lambda:dt(17,21))
    person=resolve(db);scope=db.person_scope(person)
    db.configure(scope, SPORT, target=3, best_time='18:00', delay_minutes=30)
    for when in [dt(17,18),dt(17,18,15),dt(17,18,30)]:
        record(db,scope,SPORT,when)
    i=item(db,scope,SPORT)
    f=lambda x:(1+math.log(2)*x)*2**(-x)
    assert i['earned']==pytest.approx((1+f(.5)+f(1))/3)
    assert i['weight']==1 and i['complete']


def test_old_database_flags_migrate_to_unified_model(tmp_path):
    path=tmp_path/'upgrade.sqlite3'
    clock=[dt(17,21)]
    db=ScoredStore(path,Z,clock=lambda:clock[0]); account=resolve(db);scope=db.person_scope(account)
    record(db,scope,WAKE,dt(17,8,45))
    clock[0]=dt(18,12)
    record(db,scope,WAKE,dt(18,8,45))
    db.configure(scope, SPORT, weight=0)
    with db._db() as conn:
        conn.execute("UPDATE habit_rules SET model='weighted' WHERE scope=?",(scope,))
        conn.execute("DELETE FROM score_engine_migrations WHERE name='v1.9.6-unified-smooth-decay'")
    upgraded=ScoredStore(path,Z,clock=lambda:clock[0])
    for day in (D,date(2026,10,18)):
        assert upgraded.rule_for_day(scope,WAKE,day)['model']=='smooth_decay'
        f=(1+math.log(2)*.75)*2**(-.75)  # 45 minutes past the former H=60 best time
        assert item(upgraded,scope,WAKE,day)['earned']==pytest.approx(f)
    assert upgraded.habit(scope,SPORT)['weight']==0
    with upgraded._db() as conn:
        n=conn.execute('SELECT COUNT(*) FROM habit_rules WHERE scope=?',(scope,)).fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM score_engine_migrations WHERE name='v1.9.6-unified-smooth-decay'").fetchone()[0]==1
    clock[0]=dt(19,12)
    again=ScoredStore(path,Z,clock=lambda:clock[0])
    with again._db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM habit_rules WHERE scope=?',(scope,)).fetchone()[0]==n

def test_new_time_command_and_legacy_aliases_are_retired(plugin):
    scope=plugin.store.person_scope(resolve(plugin.store))
    # Current command sets H. Old top-level commands are intentionally retired.
    message,_=plugin._dispatch('任务 时间 睡觉 23:30 60',scope,'self','Test',False)
    assert '已更新' in message
    assert plugin.store.habit(scope,SLEEP)['model']=='smooth_decay'
    assert plugin.store.habit(scope,SLEEP)['delay_minutes']==60
    from astrbot_plugin_daka.store import HabitError
    for cmd in ('半衰期 睡觉 60','延迟 起床 45'):
        with pytest.raises(HabitError):
            plugin._dispatch(cmd,scope,'self','Test',False)


def test_obsolete_model_arguments_do_not_reenable_legacy_step():
    early=dt(18,0,0)
    later=dt(18,0,0,1)
    actual=lambda when: late_factor(D,'23:30',30,when,Z)
    assert late_factor(D,'23:30',30,early,Z,model='weighted')==pytest.approx(actual(early))
    assert late_factor(D,'23:30',30,later,Z,model='weighted')==pytest.approx(actual(later))
    assert late_factor(D,'23:30',30,later,Z,model='half_life')==pytest.approx(actual(later))
