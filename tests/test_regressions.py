"""Offline regression tests. QQ network/client rendering is NOT mocked as proven."""
from __future__ import annotations
import asyncio
import base64
import importlib
import inspect
import json
import logging
import sqlite3
import sys
import types
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DATA_ROOT = ROOT / 'test_runtime'

# AstrBot contract fixtures: decorators, MessageChain, send-before-stop semantics.
# The transport signatures and raw event field shapes follow upstream source.
class Chain:
    def __init__(self, **kwargs):
        self.parts = []
        self.use_markdown_ = None
    def message(self, text):
        self.parts.append(('text', text)); return self
    def file_image(self, path):
        self.parts.append(('image', path)); return self

class Filters:
    def command(self, *a, **kw):
        return lambda fn: fn
    def on_astrbot_loaded(self, *a, **kw):
        return lambda fn: fn

class Star:
    def __init__(self, context):
        self.context = context

for name in ('astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
             'astrbot.core', 'astrbot.core.utils', 'astrbot.core.utils.astrbot_path',
             'botpy', 'botpy.http'):
    sys.modules[name] = types.ModuleType(name)
sys.modules['astrbot.api'].AstrBotConfig = dict
sys.modules['astrbot.api'].logger = logging.getLogger('daka-test')
sys.modules['astrbot.api.event'].AstrMessageEvent = object
sys.modules['astrbot.api.event'].MessageChain = Chain
sys.modules['astrbot.api.event'].filter = Filters()
sys.modules['astrbot.api.star'].Star = Star
sys.modules['astrbot.api.star'].Context = object
sys.modules['astrbot.core.utils.astrbot_path'].get_astrbot_data_path = lambda: str(DATA_ROOT)
class Route:
    def __init__(self, method, path, **params):
        self.method, self.path, self.params = method, path, params
        self.url = path.format(**params)
sys.modules['botpy.http'].Route = Route

from astrbot_plugin_daka.accounts import AccountCheckinStore, identity_aliases, legacy_identity_aliases
from astrbot_plugin_daka.store import DEFAULT_HABITS, HabitError
from astrbot_plugin_daka.main import GroupDaka
from astrbot_plugin_daka import calendar_image as cal
from astrbot_plugin_daka.qq_transport import (
    QQTransport, context_of, command_links, keyboard, send_status,
    app_id_of, validate_response, QQAPIError, markdown_escape,
)

D = date(2026, 10, 17)
TZ = timezone(timedelta(hours=8))
WATER = '\u559d\u6c34'
DINNER = '\u665a\u9910'
WAKE = '\u65e9\u8d77'
SPORT = '\u8fd0\u52a8'
CMD = '\u6253\u5361'
UID = 'A' * 32
OTHER = 'B' * 32
GID = 'C' * 32
DM = f'IceBot:FriendMessage:{UID}'
GROUP = f'IceBot:GroupMessage:{GID}'

class FakeHTTP:
    def __init__(self, outcomes=None):
        self.calls=[]
        self.outcomes=list(outcomes or [])
    async def request(self, route, json):
        self.calls.append((route.url, json.copy()))
        if self.outcomes:
            result=self.outcomes.pop(0)
            if isinstance(result, Exception): raise result
            return result
        if route.url.endswith('/files'):
            return {'file_info':'upload-proof', 'file_uuid':'f1', 'ttl':3600}
        return {'id':f'reply-{len(self.calls)}', 'timestamp':'2026-10-17T20:00:00+08:00'}

class Event:
    def __init__(self, command='', scene='c2c', uid=UID, group=GID, instance='IceBot',
                 app='12345', http=None, adapter='qq_official', raw_dict=False):
        self.message_str=CMD + (' ' + command if command else '')
        self.unified_msg_origin=f'{instance}:{"GroupMessage" if scene=="group" else "FriendMessage"}:{group if scene=="group" else uid}'
        self.uid, self.scene, self.adapter = uid, scene, adapter
        body={'id':'incoming-message', 'author':{'member_openid':uid} if scene=='group' else {'user_openid':uid}}
        if scene=='group': body['group_openid']=group
        raw=body if raw_dict else NS(id=body['id'], author=NS(**body['author']), raw_data=body,
                                     **({'group_openid':group} if scene=='group' else {}))
        self.message_obj=NS(raw_message=raw, message_id='incoming-message', group_id=group if scene=='group' else '')
        self.http=http or FakeHTTP()
        self.bot=NS(api=NS(_http=self.http), platform=NS(appid=app))
        self.role='member'
        self.sent=[]
        self.stopped=False
    def get_sender_id(self): return self.uid
    def get_sender_name(self): return '\u6d4b\u8bd5\u7528\u6237'
    def get_platform_name(self): return self.adapter
    def get_message_type(self): return 'GroupMessage' if self.scene=='group' else 'FriendMessage'
    async def send(self, chain):
        # Critical regression: do not let test collection consume all yields.
        assert not self.stopped, 'outgoing reply attempted after event stop'
        self.sent.append(chain)
    def stop_event(self): self.stopped=True

@pytest.fixture
def store(tmp_path):
    return AccountCheckinStore(tmp_path/'checkin.sqlite3')

@pytest.fixture
def plugin(tmp_path, monkeypatch):
    import astrbot_plugin_daka.main as main
    monkeypatch.setattr(main, 'get_astrbot_data_path', lambda: str(tmp_path))
    bot=GroupDaka(NS(), {})
    monkeypatch.setattr(bot, '_today', lambda:D)
    monkeypatch.setattr(bot, '_now', lambda:datetime(2026,10,17,20,17,4,tzinfo=TZ))
    monkeypatch.setattr(bot, '_start_scheduler', lambda:None)
    return bot

def resolve(store, origin=DM, uid=UID, group=False, app='12345', adapter='qq_official'):
    return store.resolve_person(identity_aliases(origin, uid, group, adapter_type=adapter, app_id=app), origin, uid)

def seed_legacy(store, origin, group):
    return store.resolve_person(legacy_identity_aliases(origin, UID, group, adapter_type='qq_official'), origin, UID)

def check(store, account, habit, amount=1, hour=20, minute=17):
    return store.checkin(store.person_scope(account), 'self', habit, D, amount,
                         checked_at=datetime(2026,10,17,hour,minute,4,tzinfo=TZ))

def amount(store, account, habit):
    return next(x['amount'] for x in store.daily(store.person_scope(account),'self',D) if x['name']==habit)


def test_same_sender_dm_group_same_account(store):
    assert resolve(store)==resolve(store,GROUP,group=True)

def test_different_groups_same_sender(store):
    assert resolve(store,GROUP,group=True)==resolve(store,'IceBot:GroupMessage:ANOTHER',group=True)

def test_appid_namespaces_different_bots(store):
    assert resolve(store)!=resolve(store,DM.replace('IceBot','OtherBot'),app='67890')

def test_one_app_multiple_adapter_instances_share_account(store):
    assert resolve(store)==resolve(store,DM.replace('IceBot','SecondConnection'))

def test_app_changed_same_instance_rejected(store):
    resolve(store)
    with pytest.raises(HabitError): resolve(store,app='67890')

def test_distinct_users_not_merged(store):
    assert resolve(store)!=resolve(store,DM.replace(UID,OTHER),uid=OTHER)

def test_onebot_existing_behavior(store):
    assert resolve(store,adapter='aiocqhttp')==resolve(store,GROUP,group=True,adapter='aiocqhttp')

def test_unknown_adapters_do_not_merge_by_nickname(store):
    assert resolve(store,adapter='unknown')!=resolve(store,GROUP,group=True,adapter='unknown')

def test_legacy_accounts_start_separate_reproduces_failure(store):
    assert seed_legacy(store,DM,False)!=seed_legacy(store,GROUP,True)

def test_existing_v14_records_automerged_on_first_command(store):
    dm=seed_legacy(store,DM,False); group=seed_legacy(store,GROUP,True)
    check(store,dm,WATER,2); check(store,dm,DINNER,1,minute=19)
    check(store,group,WAKE,1,minute=5)
    new=resolve(store)
    assert amount(store,new,WATER)==2
    assert amount(store,new,DINNER)==1
    assert amount(store,new,WAKE)==1
    assert resolve(store,GROUP,group=True)==new

def test_overlapping_counts_use_max_not_sum(store):
    dm=seed_legacy(store,DM,False); group=seed_legacy(store,GROUP,True)
    check(store,dm,WATER,2); check(store,group,WATER,1)
    new=resolve(store)
    assert amount(store,new,WATER)==2

def test_migration_idempotent_and_times_survive(store):
    dm=seed_legacy(store,DM,False); group=seed_legacy(store,GROUP,True)
    check(store,dm,WATER,2); check(store,group,DINNER,1)
    new=resolve(store)
    scope=store.person_scope(new)
    first=store.checkin_times(scope,'self',D,None)
    for _ in range(4):
        assert resolve(store,GROUP,group=True)==new
        assert resolve(store)==new
        store.merge_persons(new,dm); store.merge_persons(new,group)
    assert store.checkin_times(scope,'self',D,None)==first
    assert len(first)==2
    assert any('20:17:04+08:00'==x['checked_at'][11:] for x in first)

def test_custom_habits_and_targets_merge(store):
    dm=seed_legacy(store,DM,False); group=seed_legacy(store,GROUP,True)
    store.add_habit(store.person_scope(dm),SPORT,3,'self')
    store.change_target(store.person_scope(group),WATER,10,'self',False)
    new=resolve(store)
    assert store.habit(store.person_scope(new),SPORT)['target']==3
    assert store.habit(store.person_scope(new),WATER)['target']==10

def test_group_privacy_preserved_on_merge(store):
    dm=seed_legacy(store,DM,False); group=seed_legacy(store,GROUP,True)
    store.member_in_group(GROUP,dm,'a'); store.member_in_group(GROUP,group,'a')
    store.set_visible(GROUP,group,False)
    new=resolve(store)
    with store._db() as con:
        assert con.execute('SELECT visible FROM members WHERE scope=? AND user_id=?',(GROUP,new)).fetchone()[0]==0

def test_no_llm_or_generator_path(plugin):
    assert inspect.iscoroutinefunction(plugin.daka_command)
    assert not inspect.isasyncgenfunction(plugin.daka_command)

def test_reported_private_then_group_status_shared(plugin):
    asyncio.run(plugin.daka_command(Event('\u5de5\u4f5c\u4e00\u5c0f\u65f6 +2')))
    event=Event(scene='group')
    asyncio.run(plugin.daka_command(event))
    body=event.http.calls[-1][1]['markdown']['content']
    assert '\u5de5\u4f5c\u4e00\u5c0f\u65f6 2/3' in body
    assert event.stopped

def test_target_nonexistent_creates_personal_habit(plugin):
    asyncio.run(plugin.daka_command(Event('\u76ee\u6807 '+SPORT+' 3')))
    event=Event(scene='group')
    asyncio.run(plugin.daka_command(event))
    assert SPORT+' 0/3' in event.http.calls[-1][1]['markdown']['content']

def test_checkin_updates_reply_and_records_time(plugin):
    event=Event(WAKE)
    asyncio.run(plugin.daka_command(event))
    body=event.http.calls[-1][1]['markdown']['content']
    assert '20:17:04' in body and '\u5df2\u5b8c\u6210 1/7' in body

def test_daily_once_duplicate_not_counted(plugin):
    asyncio.run(plugin.daka_command(Event(WAKE)))
    asyncio.run(plugin.daka_command(Event(WAKE)))
    a=resolve(plugin.store)
    assert amount(plugin.store,a,'\u8d77\u5e8a')==1
    assert len(plugin.store.checkin_times(plugin.store.person_scope(a),'self',D,'\u8d77\u5e8a'))==1

@pytest.mark.parametrize('scene',['c2c','group'])
@pytest.mark.parametrize('raw_dict',[False,True])
def test_raw_sdk_object_and_webhook_dict_context(scene,raw_dict):
    ctx=context_of(Event(scene=scene,raw_dict=raw_dict))
    assert ctx.scene==scene and ctx.sender==UID
    assert ctx.destination==(GID if scene=='group' else UID)

def test_appid_from_authenticated_adapter():
    assert app_id_of(Event())=='12345'

def test_private_links_are_encoded_enter_commands():
    text=command_links([{'name':WATER,'target':8}],'c2c')
    assert '<qqbot-cmd-enter ' in text and '%2F' in text and '%20%2B1' in text
    assert 'qqbot-cmd-input' not in text

def test_group_links_are_input_not_enter():
    text=command_links([{'name':WATER,'target':8}],'group')
    assert '<qqbot-cmd-input ' in text and 'qqbot-cmd-enter' not in text
    assert 'reference="false"' in text

def test_keyboard_only_owner_and_scene_correct_enter():
    items=[{'name':WATER,'target':8,'amount':0}]
    for scene in ('group','c2c'):
        k=keyboard(items,context_of(Event(scene=scene)))
        b=k['content']['rows'][0]['buttons'][0]
        assert b['action']['type']==2
        assert b['action']['enter']==(scene=='c2c')
        assert b['action']['permission']=={'type':0,'specify_user_ids':[UID]}

def test_native_button_limits():
    items=[{'name':'a'*20+str(i),'target':3,'amount':0} for i in range(64)]
    k=keyboard(items,context_of(Event()))
    assert len(k['content']['rows'])<=5
    assert all(len(b['render_data']['label'])<=10 for row in k['content']['rows'] for b in row['buttons'])

def test_user_text_cannot_inject_qq_markup():
    assert '<qqbot-cmd-enter' not in markdown_escape('<qqbot-cmd-enter text="evil"/>')

def test_keyboard_sent_in_markdown_not_plain_content(plugin):
    event=Event()
    asyncio.run(plugin.daka_command(event))
    payload=event.http.calls[-1][1]
    assert payload['msg_type']==2 and 'keyboard' in payload and 'markdown' in payload
    assert 'content' not in payload
    assert 'event_id' not in payload
    assert payload['msg_id']=='incoming-message'

def test_keyboard_rejected_falls_back_to_clickable_links(plugin):
    http=FakeHTTP([{'err_code':11253},{'id':'ok'}])
    event=Event(http=http)
    asyncio.run(plugin.daka_command(event))
    assert len(http.calls)==2
    payload=http.calls[1][1]
    assert 'keyboard' not in payload and '<qqbot-cmd-enter' in payload['markdown']['content']
    assert [p['msg_seq'] for _,p in http.calls]==[1,2]

def test_markdown_rejected_falls_back_to_genuine_plain_text(plugin):
    http=FakeHTTP([{'err_code':11253},{'err_code':11253},{'id':'ok'}])
    event=Event(http=http)
    asyncio.run(plugin.daka_command(event))
    payload=http.calls[-1][1]
    assert payload['msg_type']==0 and 'markdown' not in payload
    assert 'qqbot-cmd-' not in payload['content']

def test_failures_do_not_disable_other_scenes(plugin):
    bad=Event(http=FakeHTTP([{'err_code':11253},{'id':'ok'}]))
    asyncio.run(plugin.daka_command(bad))
    good=Event(scene='group')
    asyncio.run(plugin.daka_command(good))
    assert 'keyboard' in good.http.calls[0][1]

def test_missing_api_falls_back_and_does_not_call_llm(plugin):
    event=Event(); event.bot.api=None
    asyncio.run(plugin.daka_command(event))
    assert event.sent and event.stopped

def test_native_links_mode_can_be_selected_and_persists(plugin):
    event=Event('\u4ea4\u4e92 \u6587\u5b57\u94fe')
    asyncio.run(plugin.daka_command(event))
    assert 'keyboard' not in event.http.calls[0][1]
    again=Event()
    asyncio.run(plugin.daka_command(again))
    assert 'keyboard' not in again.http.calls[0][1]
    assert '<qqbot-cmd-enter' in again.http.calls[0][1]['markdown']['content']

def test_plain_mode_not_markup(plugin):
    event=Event('\u4ea4\u4e92 \u7eaf\u6587\u5b57')
    asyncio.run(plugin.daka_command(event))
    assert event.http.calls[0][1]['msg_type']==0

def test_calendar_upload_before_reply_termination(plugin):
    event=Event('\u6708\u5386')
    asyncio.run(plugin.daka_command(event))
    assert len(event.http.calls)==2
    url,upload=event.http.calls[0]
    assert url.endswith('/files') and upload['srv_send_msg'] is False
    assert base64.b64decode(upload['file_data']).startswith(b'\x89PNG')
    url,payload=event.http.calls[1]
    assert payload['msg_type']==7 and payload['media']['file_info']=='upload-proof'
    assert 'markdown' not in payload
    assert event.stopped and not event.sent

def test_calendar_group_upload_route(plugin):
    event=Event('\u6708\u5386',scene='group')
    asyncio.run(plugin.daka_command(event))
    assert event.http.calls[0][0]==f'/v2/groups/{GID}/files'
    assert event.http.calls[1][0]==f'/v2/groups/{GID}/messages'

def test_calendar_upload_error_has_full_text_not_only_stats(plugin):
    event=Event('\u6708\u5386',http=FakeHTTP([{'err_code':40001}]))
    asyncio.run(plugin.daka_command(event))
    text=event.sent[0].parts[0][1]
    assert '01' in text and '17' in text and '31' in text
    assert '\u56fe\u7247\u53d1\u9001\u5931\u8d25' in text

def test_single_calendar_uses_actual_media_upload(plugin):
    event=Event('\u6708\u5386 '+WAKE)
    asyncio.run(plugin.daka_command(event))
    assert event.http.calls[-1][1]['msg_type']==7

def test_text_calendar_explicit_no_network_image_upload(plugin):
    event=Event('\u6587\u5b57\u6708\u5386')
    asyncio.run(plugin.daka_command(event))
    assert not event.http.calls and event.sent
    assert '31' in event.sent[0].parts[0][1]

def test_identity_reply_send_before_stop(plugin):
    event=Event('\u8eab\u4efd')
    asyncio.run(plugin.daka_command(event))
    assert event.sent and event.stopped

def test_diagnostic_does_not_claim_client_success(plugin):
    asyncio.run(plugin.daka_command(Event()))
    event=Event('\u8bca\u65ad')
    asyncio.run(plugin.daka_command(event))
    assert '\u4e0d\u4ee3\u8868 QQ' in event.sent[0].parts[0][1]

def test_response_error_checks():
    for result in (None,{}, {'code':123}, {'err_code':123}):
        with pytest.raises(QQAPIError): validate_response(result)
    assert validate_response({'id':'yes'})['id']=='yes'

def test_overview_completion_counts_whole_habits_only():
    habits=[{'name':WAKE,'target':1},{'name':WATER,'target':8}]
    records={WAKE:{17:1},WATER:{17:3}}
    assert cal.overview_day_stats(habits,records,17)==(1,2)

def test_month_calendar_week_alignment_and_render(tmp_path):
    habits=[{'name':WAKE,'target':1}]
    path=cal.render_overview(tmp_path/'calendar.png',2026,10,habits,{WAKE:{17:1}},D,'test')
    im=Image.open(path)
    assert im.size[0]>=900 and im.size[1]>=700
    assert cal.calendar.monthcalendar(2026,10)[0]==[0,0,0,1,2,3,4]

def test_six_week_month_single_render(tmp_path):
    path=cal.render_single(tmp_path/'single.png',2026,8,1,{1:1},date(2026,8,30),WAKE,'test')
    assert Image.open(path).size==(900,740)

def test_sqlite_backup_created_outside_uninstall_directory(tmp_path,monkeypatch):
    import astrbot_plugin_daka.main as main
    monkeypatch.setattr(main,'get_astrbot_data_path',lambda:str(tmp_path))
    db=tmp_path/'plugin_data'/'astrbot_plugin_daka'/'checkin.sqlite3'
    old=AccountCheckinStore(db)
    a=seed_legacy(old,DM,False); check(old,a,WATER,2)
    plugin=GroupDaka(NS(),{})
    backups=list((tmp_path/'backups'/'astrbot_plugin_daka').glob('*.sqlite3'))
    assert len(backups)==1 and 'plugin_data' not in backups[0].parts
    with sqlite3.connect(backups[0]) as con:
        assert con.execute('SELECT SUM(amount) FROM records').fetchone()[0]==2
    GroupDaka(NS(),{})
    assert len(list((tmp_path/'backups'/'astrbot_plugin_daka').glob('*.sqlite3')))==1

# Safe update regression tests: subprocess/Docker is a fixture, not a real host.
import zipfile
from astrbot_plugin_daka import update_source as updater

def update_fixture(tmp_path, monkeypatch):
    root=tmp_path/'server'; root.mkdir()
    target=root/'data'/'plugins'/'astrbot_plugin_daka'; target.mkdir(parents=True)
    (target/'main.py').write_text('OLD SOURCE')
    pdata=root/'data'/'plugin_data'/'astrbot_plugin_daka'; pdata.mkdir(parents=True)
    (pdata/'checkin.sqlite3').write_bytes(b'EXISTING DATA DO NOT DELETE')
    config=root/'data'/'config'; config.mkdir()
    (config/'astrbot_plugin_daka_config.json').write_text('{"timezone":"Asia/Shanghai"}')
    archive=tmp_path/'update.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for name in ('main.py','accounts.py','store.py','qq_transport.py','scoring.py','scored_store.py','panel.py','weighted_calendar.py'):
            z.writestr('astrbot_plugin_daka/'+name,'NEW SOURCE')
        z.writestr('astrbot_plugin_daka/metadata.yaml','name: astrbot_plugin_daka\nversion: 1.6.0\n')
    calls=[]
    def fake_docker(*args):
        calls.append(args)
        if args[0]=='inspect':
            return json.dumps([{'State':{'Running':True},'Mounts':[{
                'Type':'bind','Destination':'/AstrBot/data','Source':str(root/'data')}]}])
        return 'astrbot\n'
    monkeypatch.setattr(updater,'docker',fake_docker)
    return root,target,pdata,archive,calls

def test_updater_keeps_database_and_backs_up_source(tmp_path,monkeypatch):
    root,target,pdata,archive,calls=update_fixture(tmp_path,monkeypatch)
    backup=updater.update(archive,root)
    assert (target/'main.py').read_text()=='NEW SOURCE'
    assert (pdata/'checkin.sqlite3').read_bytes()==b'EXISTING DATA DO NOT DELETE'
    assert (backup/'plugin_data'/'checkin.sqlite3').read_bytes()==b'EXISTING DATA DO NOT DELETE'
    assert (backup/'old-plugin'/'main.py').read_text()=='OLD SOURCE'
    assert (backup/'config'/'astrbot_plugin_daka_config.json').is_file()
    assert [call[0] for call in calls]==['inspect','stop','start']

def test_updater_dry_check_does_not_touch_or_stop(tmp_path,monkeypatch):
    root,target,pdata,archive,calls=update_fixture(tmp_path,monkeypatch)
    assert updater.update(archive,root,check_only=True) is None
    assert (target/'main.py').read_text()=='OLD SOURCE'
    assert [call[0] for call in calls]==['inspect']
    assert not (root/'backups').exists()

def test_updater_rejects_mismatched_bind_before_stopping(tmp_path,monkeypatch):
    root,target,pdata,archive,calls=update_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(updater,'docker',lambda *a:json.dumps([{
        'State':{'Running':True},'Mounts':[{'Type':'bind','Destination':'/AstrBot/data','Source':'/somewhere/else'}]}]))
    with pytest.raises(RuntimeError, match='bind mount'):
        updater.update(archive,root)
    assert (target/'main.py').read_text()=='OLD SOURCE'

def test_updater_rejects_path_traversal(tmp_path):
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('astrbot_plugin_daka/../../victim','bad')
    with pytest.raises(ValueError,match='Unsafe'):
        updater.validate_archive(archive)

def test_updater_restart_failure_restores_source(tmp_path,monkeypatch):
    root,target,pdata,archive,calls=update_fixture(tmp_path,monkeypatch)
    original=updater.docker
    failed=False
    def docker_once_fail(*args):
        nonlocal failed
        if args[0]=='start' and not failed:
            failed=True
            raise RuntimeError('synthetic start failure')
        return original(*args)
    monkeypatch.setattr(updater,'docker',docker_once_fail)
    with pytest.raises(RuntimeError,match='synthetic'):
        updater.update(archive,root)
    assert (target/'main.py').read_text()=='OLD SOURCE'
    assert (pdata/'checkin.sqlite3').read_bytes()==b'EXISTING DATA DO NOT DELETE'
    assert calls[-1][0]=='start'

def test_text_calendar_uses_seven_explicit_cells_in_first_week():
    text=cal.calendar_overview_text(2026,10,[{'name':WAKE,'target':1}],{},D)
    cells=text.splitlines()[1].split('\uff5c')
    assert len(cells)==7
    assert cells[:3]==['--\u00b7']*3
    assert cells[3].startswith('01')
