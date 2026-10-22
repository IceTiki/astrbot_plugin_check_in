"""v1.9.5 scoring migration, history preservation, UI and SVG previews.

Pure unit and local SQLite/Pillow-style checks, not QQ live API tests.
"""
from datetime import date, datetime
from math import log, isfinite
from pathlib import Path
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import pytest

from astrbot_plugin_daka.scoring import CURRENT_MODEL, business_day, late_factor, score_item
from astrbot_plugin_daka.scored_store import ScoredStore
from astrbot_plugin_daka.panel import timing_line
from test_regressions import resolve

Z = ZoneInfo('Asia/Shanghai')
D = date(2026, 10, 17)

def dt(day, h, m=0, s=0):
    return datetime(2026, 10, day, h, m, s, tzinfo=Z)


def factor(x):
    return (1 + x * log(2)) * 2 ** (-x)


@pytest.mark.parametrize('minutes,expected', [
    (-1, 1), (0, 1), (15, factor(.25)), (30, factor(.5)),
    (60, factor(1)), (120, factor(2)), (180, factor(3)),
])
def test_new_smooth_decay_examples(minutes, expected):
    from datetime import timedelta
    timestamp = dt(17, 8) + timedelta(minutes=minutes)
    assert late_factor(D, '08:00', 60, timestamp, Z) == pytest.approx(expected)


def test_new_curve_does_not_exceed_full_credit_and_is_monotone():
    from datetime import timedelta
    checkpoints = [dt(17, 8) + timedelta(seconds=i) for i in range(0, 5 * 3600, 15)]
    values = [late_factor(D, '08:00', 60, stamp, Z) for stamp in checkpoints]
    assert values[0] == 1
    assert all(1 >= a > b >= 0 for a, b in zip(values, values[1:]))
    assert late_factor(D, '08:00', 60, dt(19, 8), Z) >= 0
    assert all(isfinite(x) for x in values)


def test_old_model_is_still_available_for_historical_rule_snapshots():
    assert late_factor(D, '08:00', 60, dt(17, 9), Z, model='half_life') == pytest.approx(.5)
    assert late_factor(D, '08:00', 60, dt(17, 9), Z, model='weighted') == 1
    assert late_factor(D, '08:00', 60, dt(17, 9, 1), Z, model='weighted') == .5
    assert late_factor(D, '08:00', 60, dt(17, 9), Z, model=CURRENT_MODEL) == pytest.approx(factor(1))
    assert business_day(dt(18, 0, 30)) == D


def test_bootstrap_has_eight_gentle_score_tasks(tmp_path):
    db = ScoredStore(tmp_path/'new.sqlite3', Z, clock=lambda: dt(17, 12))
    scope = db.person_scope(resolve(db))
    today = db.daily(scope, 'self', D)
    assert len(today) == 8
    assert all(x['model'] == CURRENT_MODEL for x in today)
    assert next(x for x in today if x['name'] == '运动')['best_time'] is None
    db.checkin(scope, 'self', '起床', D, checked_at=dt(17, 9))
    part = next(x for x in db.daily(scope, 'self', D) if x['name'] == '起床')
    assert part['score'] == pytest.approx(factor(1))
    assert part['earned'] == pytest.approx(factor(1))
    assert part['complete'] and part['penalized']
    assert '缓降 H=60分' in timing_line(part,D,dt(17, 10),Z)
    assert '84.7%' in timing_line(part,D,dt(17, 10),Z)
    days, month = db.month_scores(scope, 'self', 2026, 10, D)
    assert days[17]['ratio'] == pytest.approx(factor(1) / 8)
    assert days[17]['items'][1]['score'] == pytest.approx(factor(1))
    assert month['ratio'] == pytest.approx(factor(1) / 8)


def test_existing_v194_old_day_frozen_and_today_migrated_once(tmp_path):
    path=tmp_path/'upgrade.sqlite3'
    clock=[dt(17, 12)]
    db=ScoredStore(path, Z, clock=lambda: clock[0])
    scope=db.person_scope(resolve(db))
    db.checkin(scope,'self','起床',D,checked_at=dt(17,9))
    clock[0]=dt(18, 12)
    next_day=date(2026,10,18)
    db.checkin(scope,'self','起床',next_day,checked_at=dt(18,9))
    # Simulate two v1.9.4 rule revisions and absence of the new migration marker.
    with db._db() as conn:
        conn.execute("UPDATE habit_rules SET model='half_life' WHERE scope=? AND name='起床'",(scope,))
        conn.execute("DELETE FROM score_engine_migrations WHERE name='v1.9.5-smooth-decay'")
    assert next(x for x in db.daily(scope,'self',D) if x['name']=='起床')['score'] == pytest.approx(.5)
    db2=ScoredStore(path, Z, clock=lambda:clock[0])
    before=next(x for x in db2.daily(scope,'self',D) if x['name']=='起床')
    after=next(x for x in db2.daily(scope,'self',next_day) if x['name']=='起床')
    assert before['model']=='half_life' and before['score']==pytest.approx(.5)
    assert after['model']==CURRENT_MODEL and after['score']==pytest.approx(factor(1))
    with db2._db() as conn:
        amount_before=conn.execute("SELECT COUNT(*) FROM checkin_events WHERE scope=?",(scope,)).fetchone()[0]
        count_before=conn.execute("SELECT COUNT(*) FROM habit_rules WHERE scope=?",(scope,)).fetchone()[0]
        assert amount_before==2
    # The marker prevents rewriting old snapshots on later starts or day changes.
    clock[0]=dt(19, 12)
    db3=ScoredStore(path, Z, clock=lambda:clock[0])
    with db3._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkin_events WHERE scope=?",(scope,)).fetchone()[0]==amount_before
        assert conn.execute("SELECT COUNT(*) FROM habit_rules WHERE scope=?",(scope,)).fetchone()[0]==count_before
        assert conn.execute("SELECT COUNT(*) FROM score_engine_migrations WHERE name='v1.9.5-smooth-decay'").fetchone()[0]==1
    assert next(x for x in db3.daily(scope,'self',D) if x['name']=='起床')['score']==pytest.approx(.5)


def test_readme_preview_assets_are_valid_svg_and_self_contained():
    root=Path(__file__).resolve().parents[1]
    readme=(root/'README.md').read_text(encoding='utf-8')
    assert 'f(t)' in readme and '84.7%' in readme
    for name in ('monthly-preview.svg','daily-preview.svg'):
        file=root/'docs'/'images'/name
        assert f'docs/images/{name}' in readme
        svg=ET.parse(file).getroot()
        assert svg.tag.endswith('svg')
        assert svg.attrib.get('viewBox')
        assert svg.find('{http://www.w3.org/2000/svg}title') is not None
        assert '<script' not in file.read_text(encoding='utf-8').lower()
