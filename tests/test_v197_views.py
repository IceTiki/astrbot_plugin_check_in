"""Verify the actual plugin entrypoints enable the released brush view."""
from pathlib import Path

import pytest
from PIL import Image

from test_regressions import plugin, resolve, D


def test_daily_plugin_entrypoint_renders_brush(plugin, monkeypatch):
    import astrbot_plugin_daka.main as main
    calls = []
    original = main.render_daily_panel
    def render(*args, **kwargs):
        calls.append(kwargs.get('grade_style'))
        return original(*args, **kwargs)
    monkeypatch.setattr(main, 'render_daily_panel', render)
    scope = plugin.store.person_scope(resolve(plugin.store))
    output = plugin._make_daily_image(scope, 'self', '测试用户', D)
    assert calls == ['brush']
    with Image.open(output) as image:
        assert image.width == 1040


@pytest.mark.parametrize('chosen', [None, '起床'])
def test_month_plugin_entrypoint_renders_brush(plugin, monkeypatch, chosen):
    import astrbot_plugin_daka.main as main
    calls = []
    original = main.render_weighted
    def render(*args, **kwargs):
        calls.append(kwargs.get('grade_style'))
        return original(*args, **kwargs)
    monkeypatch.setattr(main, 'render_weighted', render)
    scope = plugin.store.person_scope(resolve(plugin.store))
    text, output = plugin._make_calendar(scope, 'self', '测试用户', chosen, 2026, 10, D)
    assert calls == ['brush'] and text
    with Image.open(output) as image:
        assert image.width == 1080


def test_release_metadata_help_and_changelog_agree():
    from astrbot_plugin_daka.help_content import help_text
    root = Path(__file__).resolve().parents[1]
    assert 'version: 1.9.8' in (root / 'metadata.yaml').read_text(encoding='utf-8')
    assert 'v1.9.8' in help_text()
    readme = (root / 'README.md').read_text(encoding='utf-8')
    assert '<code>v1.9.8</code>' in readme
    assert '正式业务调用默认仍关闭评级' not in readme
    changes = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert '1.9.7' in changes and 'OCD' in changes
