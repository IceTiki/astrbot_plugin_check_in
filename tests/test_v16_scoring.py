"""v1.6 business-day, score, migration and QQ payload regression coverage.

QQ is a contract stub. These tests never claim a real QQ delivery has happened.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import math
import sqlite3
import zipfile

import pytest
from PIL import Image

from test_regressions import (plugin, Event, FakeHTTP, AccountCheckinStore,
                              resolve, DM, GROUP, UID, D, TZ)
from astrbot_plugin_daka.scoring import (DEFAULT_RULES, business_day, at_clock, late_factor,
    score_item, summarize, validate_weight, validate_delay, validate_clock,
    fmt_percent, parse_backfill_time)
from astrbot_plugin_daka.scored_store import ScoredStore
from astrbot_plugin_daka.store import HabitError
from astrbot_plugin_daka.weighted_calendar import render_weighted, weighted_text
from astrbot_plugin_daka.panel import daily_panel

Z = ZoneInfo('Asia/Shanghai')
SLEEP = '\u7761\u89c9'
WAKE = '\u8d77\u5e8a'
SPORT = '\u8fd0\u52a8'
WORK = '\u5de5\u4f5c\u4e00\u5c0f\u65f6'
WATER = '\u559d\u6c34'
OLD_SLEEP = '\u65e9\u7761'
OLD_WAKE = '\u65e9\u8d77'
READ = '\u9605\u8bfb'


def dt(day=17, hour=21, minute=0, second=0):
    return datetime(2026,10,day,hour,minute,second,tzinfo=Z)


@pytest.fixture
def system(tmp_path):
    clock = [dt()]
    db = ScoredStore(tmp_path/'db.sqlite3', Z, clock=lambda:clock[0])
    person = resolve(db)
    scope = db.person_scope(person)
    return db, scope, clock


def item(db, scope, name, day=D):
    return next(i for i in db.daily(scope, 'self', day) if i['name']==name)


def record(db, scope, name, when, amount=1):
    return db.checkin(scope,'self',name,business_day(when),amount,checked_at=when)


@pytest.mark.parametrize('stamp,expected', [
    ('2026-10-18T00:00:00+08:00','2026-10-17'),
    ('2026-10-18T03:59:59+08:00','2026-10-17'),
    ('2026-10-18T04:00:00+08:00','2026-10-18'),
    ('2026-11-01T00:10:00+08:00','2026-10-31'),
    ('2027-01-01T03:00:00+08:00','2026-12-31'),
    ('2028-03-01T03:59:59+08:00','2028-02-29'),
])
def test_four_am_boundary(stamp,expected):
    assert business_day(datetime.fromisoformat(stamp))==date.fromisoformat(expected)


def test_naive_business_day_rejected():
    with pytest.raises(HabitError): business_day(datetime(2026,10,17))


@pytest.mark.parametrize('when,factor', [
    (dt(17,21),1), (dt(17,23,30),1), (dt(17,23,59,59),1),
    (dt(18,0,0,0),1), (dt(18,0,0,1),.5), (dt(18,0,15),.5),
    (dt(18,0,30,0),.5), (dt(18,0,30,1),.25), (dt(18,1,0),.25),
    (dt(18,1,0,1),.125),
])
def test_v16_old_step_delay_inclusive_boundaries(when,factor):
    assert late_factor(D,'23:30',30,when,Z,model='weighted')==factor


@pytest.mark.parametrize('clock,when,factor', [
    ('08:00',dt(17,7),1),('08:00',dt(17,8,30),1),
    ('08:00',dt(17,8,30,1),.5),('08:00',dt(17,9,0,1),.25),
    ('00:00',dt(18,0,30),1),('00:00',dt(18,0,30,1),.5),
])
def test_v16_old_step_other_targets_and_midnight_anchor(clock,when,factor):
    assert late_factor(D,clock,30,when,Z,model='weighted')==factor


def test_before_four_clock_is_next_day():
    assert at_clock(D,'03:00',Z).date()==D+timedelta(days=1)
    assert at_clock(D,'04:00',Z).date()==D


def test_exact_new_defaults(system):
    db,scope,_=system
    rules=db.list_habits(scope)
    assert [(r['name'],r['target'],r['best_time'],r['delay_minutes']) for r in rules]==list(DEFAULT_RULES)
    assert all(r['weight']==1 for r in rules)


def test_three_repetitions_total_one_weight(system):
    db,scope,clock=system
    for n in range(1,4):
        record(db,scope,WORK,dt(17,17+n))
        i=item(db,scope,WORK)
        assert i['amount']==n and i['earned']==pytest.approx(n/3)
        assert summarize(db.daily(scope,'self',D))['ratio']==pytest.approx(n/21)
    result=record(db,scope,WORK,dt(17,21))
    assert not result['changed']
    assert len(db.checkin_times(scope,'self',D,WORK))==3


def test_multicount_clamped_to_remaining(system):
    db,scope,_=system
    record(db,scope,WORK,dt(),2)
    result=record(db,scope,WORK,dt(17,22),100)
    assert result['new']==3
    assert [e['delta'] for e in db.checkin_times(scope,'self',D,WORK)]==[2,1]


def test_late_task_penalty_does_not_half_every_other_task(system):
    db,scope,_=system
    record(db,scope,SLEEP,dt(18,0,15))
    for name,_,best,_ in DEFAULT_RULES:
        if name==SLEEP: continue
        when=at_clock(D,best,Z) if best else dt(17,20)
        record(db,scope,name,when,3 if name==WORK else 1)
    stats=summarize(db.daily(scope,'self',D))
    assert stats['finished']==7
    assert stats['earned']==pytest.approx(6+2**(-1.5)) and stats['weight']==7
    assert stats['ratio']==pytest.approx((6+2**(-1.5))/7)
    assert not stats['full_score']


def test_zero_weight_ignored_but_still_recorded(system):
    db,scope,_=system
    db.configure(scope,SPORT,weight=0)
    before=summarize(db.daily(scope,'self',D))
    record(db,scope,SPORT,dt())
    after=summarize(db.daily(scope,'self',D))
    assert before['ratio']==after['ratio']==0
    assert after['weight']==6 and item(db,scope,SPORT)['complete']


def test_all_optional_undefined_ratio_not_100_percent(system):
    db,scope,_=system
    for r in db.list_habits(scope): db.configure(scope,r['name'],weight=0)
    assert summarize(db.daily(scope,'self',D))['ratio'] is None
    days,stats=db.month_scores(scope,'self',2026,10,D)
    assert stats['ratio'] is None and stats['days']==0
    # Still useful in a single-task calendar.
    record(db,scope,SPORT,dt())
    single,_=db.month_scores(scope,'self',2026,10,D,SPORT)
    assert single[17]['display_ratio']==1


def test_custom_weight_changes_denominator(system):
    db,scope,_=system
    db.configure(scope,SPORT,weight=2)
    record(db,scope,SPORT,dt())
    stats=summarize(db.daily(scope,'self',D))
    assert stats['earned']==2 and stats['weight']==8 and stats['ratio']==.25


def test_optional_history_and_archive_still_queryable(system):
    db,scope,clock=system
    record(db,scope,SPORT,dt())
    clock[0]=dt(18,12)
    db.delete_habit(scope,SPORT,'self',False)
    assert item(db,scope,SPORT,D)['earned']==1
    assert all(i['name']!=SPORT for i in db.daily(scope,'self',date(2026,10,18)))
    days,stats=db.month_scores(scope,'self',2026,10,date(2026,10,18),SPORT)
    assert 17 in days and 18 not in days
    db.restore_habit(scope,SPORT)
    assert db.habit(scope,SPORT)['enabled']==1


def test_yesterday_rules_frozen_today_change_immediate(system):
    db,scope,clock=system
    record(db,scope,WAKE,dt(17,8,45))
    clock[0]=dt(18,9)
    db.configure(scope,WAKE,best_time='09:00',weight=2)
    record(db,scope,WAKE,clock[0])
    assert item(db,scope,WAKE,D)['earned']==pytest.approx(2**(-1.5))
    assert item(db,scope,WAKE,date(2026,10,18))['earned']==2
    # Same-day edit re-scores current day's earlier event.
    db.configure(scope,WAKE,best_time='08:00')
    assert item(db,scope,WAKE,date(2026,10,18))['earned']==pytest.approx(2*2**(-2))
    assert item(db,scope,WAKE,D)['earned']==pytest.approx(2**(-1.5))


def test_month_aggregate_is_points_over_weights_not_average_of_ratios(system):
    db,scope,clock=system
    record(db,scope,SPORT,dt())  # 1 / 7
    clock[0]=dt(18,12)
    db.configure(scope,SPORT,weight=2)
    record(db,scope,SPORT,clock[0])  # 2 / 8
    days,stats=db.month_scores(scope,'self',2026,10,date(2026,10,18))
    assert list(days)==[17,18]
    assert stats['ratio']==pytest.approx(3/15)
    assert stats['days']==2


def test_missed_days_count_but_before_start_and_future_do_not(system):
    db,scope,clock=system
    record(db,scope,SPORT,dt())
    clock[0]=dt(19,10)
    days,stats=db.month_scores(scope,'self',2026,10,date(2026,10,19))
    assert list(days)==[17,18,19]
    assert stats['ratio']==pytest.approx(1/21)


def test_late_repeat_weight_average_not_whole_task_last_timestamp(system):
    db,scope,_=system
    db.configure(scope,WORK,best_time='18:00',delay_minutes=30)
    record(db,scope,WORK,dt(17,18))
    record(db,scope,WORK,dt(17,18,45))
    record(db,scope,WORK,dt(17,19,15))
    i=item(db,scope,WORK)
    assert i['earned']==pytest.approx((1+2**(-1.5)+2**(-2.5))/3)
    assert i['complete']


def test_backfill_timed_task_requires_declared_time(system):
    db,scope,clock=system
    clock[0]=dt(18,12)
    with pytest.raises(HabitError):
        db.checkin(scope,'self',SLEEP,D,checked_at=clock[0],kind='backfill')
    db.checkin(scope,'self',SLEEP,D,checked_at=clock[0],kind='backfill',occurred_at=dt(18,0,15))
    assert item(db,scope,SLEEP)['earned']==pytest.approx(2**(-1.5))
    e=db.checkin_times(scope,'self',D,SLEEP)[0]
    assert e['checked_at']!=e['occurred_at']
    assert e['kind']=='backfill'


def test_backfill_untimed_without_clock_allowed(system):
    db,scope,clock=system
    clock[0]=dt(18,12)
    db.checkin(scope,'self',SPORT,D,checked_at=clock[0],kind='backfill')
    assert item(db,scope,SPORT)['earned']==1


def test_backfill_future_time_and_wrong_day_rejected(system):
    db,scope,clock=system
    for when in (dt(18,0,15),dt(16,21)):
        with pytest.raises(HabitError):
            db.checkin(scope,'self',SLEEP,D,checked_at=clock[0],kind='backfill',occurred_at=when)
    assert parse_backfill_time(D,'00:15',Z,dt(18,10))==dt(18,0,15)
    with pytest.raises(HabitError): parse_backfill_time(D,'25:00',Z,dt(18,10))


def test_normal_time_cannot_backdate_to_civil_yesterday(system):
    db,scope,_=system
    with pytest.raises(HabitError): record(db,scope,SLEEP,datetime(2026,10,17))
    with pytest.raises(HabitError):
        db.checkin(scope,'self',SLEEP,D,checked_at=dt(18,4))


@pytest.mark.parametrize('invalid',['-1','nan','inf','1001','abc'])
def test_invalid_weights_rejected(invalid):
    with pytest.raises(HabitError): validate_weight(invalid)


@pytest.mark.parametrize('invalid',['0','-10','1.5','1441','abc'])
def test_invalid_delays_rejected(invalid):
    with pytest.raises(HabitError): validate_delay(invalid)


@pytest.mark.parametrize('invalid',['24:00','12:60','8:3','abc','-1:00'])
def test_invalid_clock_rejected(invalid):
    with pytest.raises(HabitError): validate_clock(invalid)


def test_formats_do_not_report_small_penalty_as_100_percent():
    assert fmt_percent(.99999)=='99.9%'
    assert fmt_percent(None)=='--'


def test_migration_preserves_legacy_dates_counts_and_renames(tmp_path):
    path=tmp_path/'old.sqlite3'
    old=AccountCheckinStore(path)
    account=resolve(old); scope=old.person_scope(account)
    old.checkin(scope,'self',OLD_SLEEP,date(2026,10,16),checked_at=dt(16,23,55))
    old.checkin(scope,'self',WATER,date(2026,10,16),2,checked_at=dt(16,20))
    old.add_habit(scope,READ,2,'self')
    new=ScoredStore(path,Z,clock=lambda:dt(17,21))
    assert item(new,scope,SLEEP,date(2026,10,16))['amount']==1
    assert item(new,scope,SLEEP,date(2026,10,16))['score']==1
    assert item(new,scope,WATER,date(2026,10,16))['score']==0  # legacy binary completion
    assert new.has_history(scope,WATER)
    assert WATER not in [r['name'] for r in new.list_habits(scope)]
    assert new.habit(scope,READ)['target']==2
    times=new.checkin_times(scope,'self',date(2026,10,16),SLEEP)
    assert times[0]['checked_at']==dt(16,23,55).isoformat()


def test_restart_does_not_reset_user_settings_or_resurrect_deleted_tasks(system):
    db,scope,clock=system
    db.configure(scope,SLEEP,weight=.7,best_time='00:00',delay_minutes=45)
    db.delete_habit(scope,SPORT,'self',False)
    again=ScoredStore(db.db_path,Z,clock=lambda:clock[0])
    assert again.habit(scope,SLEEP)['weight']==.7
    assert again.habit(scope,SLEEP)['best_time']=='00:00'
    with pytest.raises(HabitError): again.habit(scope,SPORT)


def test_current_day_legacy_known_time_can_use_new_rule(tmp_path):
    path=tmp_path/'old.sqlite3'; old=AccountCheckinStore(path)
    account=resolve(old); scope=old.person_scope(account)
    old.checkin(scope,'self',OLD_WAKE,D,checked_at=dt(17,8,45))
    new=ScoredStore(path,Z,clock=lambda:dt(17,21))
    assert item(new,scope,WAKE)['earned']==pytest.approx(2**(-1.5))


def test_unknown_legacy_time_is_not_fabricated(system):
    db,scope,_=system
    with db._db() as conn:
        conn.execute('INSERT INTO records VALUES (?,?,?,?,?,?)',(scope,'self',SLEEP,D.isoformat(),1,dt().isoformat()))
    i=item(db,scope,SLEEP)
    assert i['time_unknown'] and i['first_at'] is None and i['earned']==1


def test_merge_copies_new_rules_and_actual_backfill_times(tmp_path):
    clock=[dt()]
    db=ScoredStore(tmp_path/'merge.sqlite3',Z,clock=lambda:clock[0])
    a=resolve(db); sa=db.person_scope(a)
    b=resolve(db, DM.replace(UID,'B'*32),uid='B'*32); sb=db.person_scope(b)
    db.add_habit(sb,READ,2,'self'); db.configure(sb,READ,weight=.25,best_time='18:00')
    clock[0]=dt(18,12)
    db.checkin(sb,'self',READ,D,1,checked_at=clock[0],kind='backfill',occurred_at=dt(17,18,40))
    db.merge_persons(a,b)
    i=item(db,sa,READ)
    assert i['weight']==.25 and i['earned']==pytest.approx(.25*2**(-40/30)/2)
    assert db.checkin_times(sa,'self',D,READ)[0]['occurred_at']==dt(17,18,40).isoformat()


def test_commands_update_time_weight_target_and_optional_add(plugin):
    async def run():
        for cmd in ('\u65f6\u95f4 '+SLEEP+' 00:00 45', '\u6743\u91cd '+SPORT+' 0',
                    '\u76ee\u6807 '+WORK+' 4', '\u6dfb\u52a0 '+READ+' 1 0'):
            e=Event(cmd); await plugin.daka_command(e)
            assert not e.sent, 'settings should send interactive updated status'
            assert e.http.calls[-1][1]['msg_type']==2
    asyncio.run(run())
    a=resolve(plugin.store); scope=plugin.store.person_scope(a)
    assert plugin.store.habit(scope,SLEEP)['best_time']=='00:00'
    assert plugin.store.habit(scope,SLEEP)['delay_minutes']==45
    assert plugin.store.habit(scope,SPORT)['weight']==0
    assert plugin.store.habit(scope,READ)['weight']==0
    assert plugin.store.habit(scope,WORK)['target']==4


def test_disabled_timing_command_and_repeat_default_half_life(plugin):
    asyncio.run(plugin.daka_command(Event('\u65f6\u95f4 '+SLEEP+' \u5173\u95ed')))
    scope=plugin.store.person_scope(resolve(plugin.store))
    assert plugin.store.habit(scope,SLEEP)['best_time'] is None
    asyncio.run(plugin.daka_command(Event('\u65f6\u95f4 '+SLEEP+' 23:00')))
    assert plugin.store.habit(scope,SLEEP)['best_time']=='23:00'


def test_invalid_add_weight_does_not_create_task(plugin):
    e=Event('\u6dfb\u52a0 '+READ+' 1 NaN')
    asyncio.run(plugin.daka_command(e))
    scope=plugin.store.person_scope(resolve(plugin.store))
    assert not plugin.store.has_history(scope,READ)


def test_native_panel_no_duplicate_blue_links_when_keyboard_succeeds(plugin):
    event=Event(); asyncio.run(plugin.daka_command(event))
    payload=event.http.calls[-1][1]
    assert 'keyboard' in payload
    assert '<qqbot-cmd-' not in payload['markdown']['content']
    assert '23:30' in payload['markdown']['content'] and '04:00' in payload['markdown']['content']
    buttons=[b for row in payload['keyboard']['content']['rows'] for b in row['buttons']]
    assert len(buttons)==9
    assert len(payload['keyboard']['content']['rows'])<=5
    assert any(WORK in b['action']['data'] for b in buttons)


def test_keyboard_failure_retains_clickable_link_fallback(plugin):
    http=FakeHTTP([{'code':304003},{'id':'links'}])
    e=Event(http=http);asyncio.run(plugin.daka_command(e))
    assert '<qqbot-cmd-enter' in http.calls[-1][1]['markdown']['content']
    assert 'keyboard' not in http.calls[-1][1]


def test_private_and_group_share_time_and_weight_settings(plugin):
    asyncio.run(plugin.daka_command(Event('\u6743\u91cd '+SPORT+' 2')))
    asyncio.run(plugin.daka_command(Event(WORK+' +1')))
    e=Event(scene='group');asyncio.run(plugin.daka_command(e))
    body=e.http.calls[-1][1]['markdown']['content']
    assert WORK+' 1/3' in body and '0.33/8' in body
    a=resolve(plugin.store);b=resolve(plugin.store,GROUP,group=True)
    assert a==b


def test_real_handler_four_am_rollover_and_month_choice(plugin,monkeypatch):
    scope=plugin.store.person_scope(resolve(plugin.store))
    monkeypatch.setattr(plugin,'_now',lambda:dt(18,0,15))
    monkeypatch.setattr(plugin,'_today',lambda:business_day(plugin._now()))
    e=Event(SLEEP);asyncio.run(plugin.daka_command(e))
    assert item(plugin.store,scope,SLEEP,D)['earned']==pytest.approx(2**(-1.5))
    assert item(plugin.store,scope,SLEEP,D)['last_at']==dt(18,0,15).isoformat()
    monkeypatch.setattr(plugin,'_now',lambda:dt(18,4))
    e=Event();asyncio.run(plugin.daka_command(e))
    assert '10-18' in e.http.calls[-1][1]['markdown']['content']
    assert item(plugin.store,scope,SLEEP,date(2026,10,18))['amount']==0


def test_backfill_command_records_actual_and_submission(plugin,monkeypatch):
    scope=plugin.store.person_scope(resolve(plugin.store))
    monkeypatch.setattr(plugin,'_now',lambda:dt(18,10))
    monkeypatch.setattr(plugin,'_today',lambda:date(2026,10,18))
    e=Event('\u8865\u7b7e '+SLEEP+' \u6628\u5929 00:10')
    asyncio.run(plugin.daka_command(e))
    assert item(plugin.store,scope,SLEEP,D)['earned']==pytest.approx(2**(-40/30))
    e=Event('\u8bb0\u5f55 '+SLEEP+' \u6628\u5929');asyncio.run(plugin.daka_command(e))
    text=e.sent[0].parts[0][1]
    assert '00:10:00' in text and '10:00:00' in text


def test_images_contain_weighted_metrics_and_handle_six_rows(system,tmp_path):
    db,scope,_=system
    record(db,scope,WORK,dt(),1)
    days,stats=db.month_scores(scope,'self',2026,10,D)
    path=render_weighted(tmp_path/'calendar.png',2026,10,days,stats,D,'test')
    im=Image.open(path); assert im.width==1080 and im.height>=900
    text=weighted_text(2026,10,days,stats,D)
    assert '4.8%' in text and '0.33/7' in text
    p=render_weighted(tmp_path/'six.png',2026,8,{},dict(weight=0,earned=0,ratio=None,days=0,full_days=0),date(2026,8,31),'test')
    assert Image.open(p).height==1132


def test_complete_but_penalized_panel_does_not_claim_perfect(system):
    db,scope,_=system
    for name,target,best,_ in DEFAULT_RULES:
        when=dt(18,0,15) if name==SLEEP else at_clock(D,best,Z) if best else dt()
        record(db,scope,name,when,target)
    panel=daily_panel(db.daily(scope,'self',D),'test',D,dt(18,0,15),Z)
    assert '90.8%' in panel and '6.35/7' in panel
    assert '\u8fdf\u5230' in panel and '04:00' in panel


def test_weighted_rankings_use_ratio_not_raw_task_count(system):
    db,scope,_=system
    a=resolve(db);db.member_in_group(GROUP,a,'A',UID)
    record(db,scope,WORK,dt())
    ranked=db.group_rankings(GROUP,2026,10)
    assert ranked[0]['ratio']==pytest.approx(1/21)


def test_concurrent_count_cannot_exceed_target(system):
    from concurrent.futures import ThreadPoolExecutor
    db,scope,_=system
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _: record(db,scope,WORK,dt()),range(16)))
    assert sum(r['changed'] for r in results)==3
    assert item(db,scope,WORK)['amount']==3
    assert len(db.checkin_times(scope,'self',D,WORK))==3


def test_randomized_score_bounds_and_early_full_credit():
    import random
    rng=random.Random(16)
    for _ in range(300):
        target=rng.randint(1,100)
        amount=rng.randint(0,150)
        weight=rng.choice([0,.3,1,2,1000])
        rule=dict(target=target,weight=weight,best_time='08:00',delay_minutes=rng.randint(1,180),model='weighted')
        events=[dict(delta=amount or 1, checked_at=(dt(17,5)+timedelta(minutes=rng.randint(0,1000))).isoformat(),kind='normal')]
        i=score_item(rule,amount,events,D,Z)
        assert 0<=i['earned']<=weight+1e-10
        assert 0<=i['score']<=i['progress']+1e-10
        early=score_item(rule,target,[dict(delta=target,checked_at=dt(17,7).isoformat(),kind='normal')],D,Z)
        assert early['earned']==weight
