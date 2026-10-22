"""v1.7 historical half-life decay, settings and migration integration tests.

The AstrBot transport is mocked: these checks do NOT prove QQ button delivery.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo
import asyncio
import math

import pytest

from astrbot_plugin_daka.scoring import late_factor, score_item, business_day
from astrbot_plugin_daka.scored_store import ScoredStore
from astrbot_plugin_daka.panel import timing_line, daily_panel
from test_regressions import resolve, Event, plugin
from test_v16_scoring import D, dt, Z, WAKE, SLEEP, WORK, SPORT, item, record


@pytest.mark.parametrize("when,factor", [
    (dt(17,23,0), 1),
    (dt(17,23,30), 1),
    (dt(17,23,30,1), 2**(-1/1800)),
    (dt(17,23,35), 2**(-5/30)),
    (dt(17,23,40), 2**(-10/30)),
    (dt(17,23,45), math.sqrt(.5)),
    (dt(18,0,0), .5),
    (dt(18,0,15), 2**(-1.5)),
    (dt(18,0,30), .25),
    (dt(18,1,0), .125),
])
def test_continuous_sleep_half_life_from_best_time(when, factor):
    assert late_factor(D, '23:30', 30, when, Z, model='half_life') == pytest.approx(factor)


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
    assert late_factor(D, '00:00', 30, dt(18,0,30), Z, model='half_life') == .5
    assert business_day(dt(18,0,30)) == D


def test_no_time_rule_always_full():
    assert late_factor(D, None, 30, dt(18,0), Z) == 1
    assert late_factor(D, '23:30', 30, None, Z) == 1


def test_daily_score_and_panel_use_smooth_factor(tmp_path):
    db = ScoredStore(tmp_path/'new.sqlite3', Z, clock=lambda:dt(17,21))
    person = resolve(db); scope = db.person_scope(person)
    record(db,scope,SLEEP,dt(17,23,45))
    result=item(db,scope,SLEEP)
    assert result['model']=='half_life'
    assert result['score']==pytest.approx(2**(-.5))
    assert result['penalized']
    label=timing_line(result,D,dt(17,23,45),Z)
    assert '半衰期 30分' in label and '次日00:00降至50%' in label
    panel=daily_panel(db.daily(scope,'self',D),'user',D,dt(17,23,45),Z)
    assert '0.71/1' in panel
    assert '半衰期' in panel


def test_three_repeated_timed_events_weight_one(tmp_path):
    db=ScoredStore(tmp_path/'work.sqlite3',Z,clock=lambda:dt(17,21))
    person=resolve(db);scope=db.person_scope(person)
    db.configure(scope, WORK, best_time='18:00', delay_minutes=30)
    for when in [dt(17,18),dt(17,18,15),dt(17,18,30)]:
        record(db,scope,WORK,when)
    i=item(db,scope,WORK)
    assert i['earned']==pytest.approx((1+math.sqrt(.5)+.5)/3)
    assert i['weight']==1 and i['complete']


def test_v16_database_cutover_preserves_completed_days(tmp_path):
    path=tmp_path/'upgrade.sqlite3'
    clock=[dt(17,21)]
    db=ScoredStore(path,Z,clock=lambda:clock[0]); account=resolve(db);scope=db.person_scope(account)
    record(db,scope,WAKE,dt(17,8,45))
    clock[0]=dt(18,12)
    record(db,scope,WAKE,dt(18,8,45))
    db.configure(scope, SPORT, weight=0)
    db.delete_habit(scope,WORK,'self',False)
    # Emulate existing v1.6 rules database (old model, no v1.7 marker).
    with db._db() as conn:
        conn.execute("UPDATE habit_rules SET model='weighted' WHERE scope=?",(scope,))
        conn.execute("DELETE FROM score_engine_migrations WHERE name='v1.7-half-life'")
    upgraded=ScoredStore(path,Z,clock=lambda:clock[0])
    assert upgraded.rule_for_day(scope,WAKE,D)['model']=='weighted'
    assert upgraded.rule_for_day(scope,WAKE,date(2026,10,18))['model']=='half_life'
    assert item(upgraded,scope,WAKE,D)['earned']==pytest.approx(.5)
    assert item(upgraded,scope,WAKE,date(2026,10,18))['earned']==pytest.approx(2**(-1.5))
    assert upgraded.habit(scope,SPORT)['weight']==0
    assert WORK not in [r['name'] for r in upgraded.list_habits(scope)]
    days, total=upgraded.month_scores(scope,'self',2026,10,date(2026,10,18))
    assert days[17]['items'][1]['model']=='weighted'  # wake is second default
    assert total['earned']==pytest.approx(.5+2**(-1.5))
    with upgraded._db() as conn:
        n=conn.execute('SELECT COUNT(*) FROM habit_rules WHERE scope=?',(scope,)).fetchone()[0]
        assert conn.execute('SELECT COUNT(*) FROM score_engine_migrations').fetchone()[0]==1
    clock[0]=dt(19,12)
    again=ScoredStore(path,Z,clock=lambda:clock[0])
    with again._db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM habit_rules WHERE scope=?',(scope,)).fetchone()[0]==n
    assert item(again,scope,WAKE,D)['earned']==.5
    assert item(again,scope,WAKE,date(2026,10,18))['earned']==pytest.approx(2**(-1.5))


def test_command_half_life_and_legacy_delay_alias(plugin):
    async def run():
        e=Event('半衰期 睡觉 60')
        await plugin.daka_command(e)
        assert not e.sent and e.http.calls[-1][1]['msg_type']==2
        e=Event('延迟 起床 45')
        await plugin.daka_command(e)
        assert not e.sent and e.http.calls[-1][1]['msg_type']==2
        e=Event('规则')
        await plugin.daka_command(e)
        assert e.sent and '23:45' in e.sent[0].parts[0][1]
    asyncio.run(run())
    scope=plugin.store.person_scope(resolve(plugin.store))
    assert plugin.store.habit(scope,SLEEP)['delay_minutes']==60
    assert plugin.store.habit(scope,WAKE)['delay_minutes']==45
    assert plugin.store.habit(scope,SLEEP)['model']=='half_life'


def test_scoring_legacy_old_step_still_available():
    early=dt(18,0,0)
    later=dt(18,0,0,1)
    assert late_factor(D,'23:30',30,early,Z,model='weighted')==1
    assert late_factor(D,'23:30',30,later,Z,model='weighted')==.5
    assert late_factor(D,'23:30',30,later,Z,model='half_life')>.49
