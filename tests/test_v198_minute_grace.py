"""The configured HH:MM includes the entire minute, without rounding events."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math

import pytest
from test_regressions import resolve
from astrbot_plugin_daka.scoring import at_clock, late_factor
from astrbot_plugin_daka.scored_store import ScoredStore
from astrbot_plugin_daka.grade_badge import grade_for

TZ = ZoneInfo('Asia/Shanghai')
DAY = date(2026, 10, 29)


@pytest.mark.parametrize('clock', ['12:00', '23:59', '00:00', '03:59'])
@pytest.mark.parametrize('seconds', [-1, 0, 1, 59, 59.999999, 60, 60.001, 61, 3600])
def test_entire_target_minute_and_first_penalty(clock, seconds):
    when = at_clock(DAY, clock, TZ) + timedelta(seconds=seconds)
    actual = late_factor(DAY, clock, 60, when, TZ)
    if seconds < 60:
        assert actual == 1.
    else:
        x = seconds / 3600
        assert actual == pytest.approx((1 + x * math.log(2)) * 2 ** -x)
        assert actual < 1.
        assert grade_for(actual) != 'OCD'
    assert late_factor(DAY, clock, 60, when.astimezone(timezone.utc), TZ) == actual


@pytest.mark.parametrize('kind', ['normal', 'backfill'])
def test_historical_normal_and_backfill_queries_preserve_timestamps(tmp_path, kind):
    now = datetime(2026, 10, 29, 14, tzinfo=TZ)
    db = ScoredStore(tmp_path / 'checkin.sqlite3', TZ, clock=lambda: now)
    scope = db.person_scope(resolve(db))
    stamp = datetime(2026, 10, 29, 12, 0, 59, 999999, tzinfo=TZ)
    db.checkin(scope, 'self', '午餐', DAY, checked_at=stamp if kind == 'normal' else now,
               kind=kind, occurred_at=stamp)
    with db._db() as conn:
        original = [tuple(row) for row in conn.execute('SELECT * FROM checkin_events')]
    for _ in range(2):
        item = next(i for i in db.daily(scope, 'self', DAY) if i['name'] == '午餐')
        assert item['score'] == 1. and not item['penalized']
        days, _ = db.month_scores(scope, 'self', 2026, 10, DAY, '午餐')
        assert days[29]['ratio'] == 1.
    with db._db() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM checkin_events')] == original
