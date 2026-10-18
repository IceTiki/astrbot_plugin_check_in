"""QQ v2 transport: Markdown+keyboard, native command links, real media uploads.

Uses AstrBot's authenticated botpy client. No token, extra account or public image
host is required. Native command buttons (type=2) use normal message events;
this module deliberately does NOT invent or monkey-patch INTERACTION_CREATE.
API acceptance is logged as acceptance, not proof of client-side rendering.
"""
from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

OFFICIAL = {'qq_official', 'qq_official_webhook', 'qqofficial'}


def get(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def platform_name(event) -> str:
    fn = get(event, 'get_platform_name')
    return str(fn() if callable(fn) else '').lower()


def app_id_of(event) -> str | None:
    """AppID is a namespace, not a secret. Prefer the authenticated adapter config."""
    bot = get(event, 'bot')
    platform = get(bot, 'platform')
    value = get(platform, 'appid') or get(get(platform, 'config', {}), 'appid')
    if not value:
        value = get(bot, 'appid')
    return str(value) if value else None


@dataclass(frozen=True)
class QQContext:
    scene: str
    destination: str
    sender: str
    msg_id: str
    api: Any


def context_of(event) -> QQContext:
    if platform_name(event) not in OFFICIAL:
        raise RuntimeError('not a QQ official adapter')
    obj = get(event, 'message_obj')
    raw = get(obj, 'raw_message')
    data = get(raw, 'raw_data', raw if isinstance(raw, dict) else {}) or {}
    data = get(data, 'd', data)
    author = get(raw, 'author') or get(data, 'author', {})
    group = get(raw, 'group_openid') or get(data, 'group_openid')
    user = (get(raw, 'user_openid') or get(author, 'user_openid') or
            get(data, 'user_openid'))
    # Never mistake a guild/channel for a QQ group or C2C conversation.
    if get(raw, 'guild_id') or get(data, 'guild_id') or get(raw, 'channel_id'):
        raise RuntimeError('QQ guilds are not supported by this v2 transport')
    uid = str(event.get_sender_id() or '')
    if not uid or uid == 'None':
        raise RuntimeError('missing sender identity')
    if group:
        scene, target = 'group', str(group)
    elif user:
        scene, target = 'c2c', str(user)
    else:
        origin = str(event.unified_msg_origin).split(':', 2)
        if len(origin) == 3 and origin[1] == 'FriendMessage':
            scene, target = 'c2c', uid
        elif len(origin) == 3 and origin[1] == 'GroupMessage' and get(obj, 'group_id'):
            scene, target = 'group', str(obj.group_id)
        else:
            raise RuntimeError('missing QQ group_openid / C2C user_openid')
    msg_id = str(get(obj, 'message_id') or get(raw, 'id') or get(data, 'id') or '')
    if not msg_id:
        raise RuntimeError('missing inbound msg_id; refusing an unsolicited push')
    api = get(get(event, 'bot'), 'api')
    if api is None:
        raise RuntimeError('QQ bot.api is unavailable')
    return QQContext(scene, target, uid, msg_id, api)


def safe_error(exc: Exception) -> str:
    """Preserve error type/codes, not tokens, message bodies, or full OpenIDs."""
    text = str(exc)
    code = get(exc, 'code') or get(exc, 'err_code')
    numbers = re.findall(r'(?<!\d)\d{4,10}(?!\d)', text)
    summary = type(exc).__name__
    if code:
        summary += ' code=' + str(code)
    elif numbers:
        summary += ' code=' + ','.join(dict.fromkeys(numbers[:3]))
    if isinstance(exc, (TypeError, AttributeError, ImportError)):
        summary += ': ' + re.sub(r'[A-Fa-f0-9]{24,}', '[id]', text)[:160]
    elif isinstance(exc, QQAPIError):
        summary += ': ' + text[:160]
    return summary


class QQAPIError(RuntimeError):
    pass


def validate_response(result: Any, required: str = 'id') -> Any:
    if result is None:
        raise QQAPIError('empty API response (delivery unknown)')
    for field in ('err_code', 'code'):
        code = get(result, field)
        if code not in (None, 0, '0'):
            trace = str(get(result, 'trace_id', '') or '')
            raise QQAPIError(f'QQ {field}={code}' + (f' trace_id={trace}' if trace else ''))
    if not get(result, required):
        raise QQAPIError(f'API response has no {required}; not confirmed')
    return result


class QQTransport:
    def __init__(self, event):
        self.event = event
        self.ctx = context_of(event)
        # This plugin issues at most three message attempts. Every attempt for
        # this inbound message gets a distinct msg_seq (no random duplicates).
        self.seq = 0

    async def _request(self, suffix: str, payload: dict) -> Any:
        ctx = self.ctx
        family = 'groups' if ctx.scene == 'group' else 'users'
        http = get(ctx.api, '_http')
        request = get(http, 'request')
        if callable(request):
            from botpy.http import Route
            route = Route('POST', f'/v2/{family}/{{target}}/{suffix}', target=ctx.destination)
            return await asyncio.wait_for(request(route, json=payload), timeout=25)
        if suffix != 'messages':
            raise RuntimeError('QQ SDK has no authenticated media upload transport')
        sender = (get(ctx.api, 'post_group_message') if ctx.scene == 'group' else
                  get(self.event, 'post_c2c_message') or get(ctx.api, 'post_c2c_message'))
        if not callable(sender):
            raise RuntimeError('QQ message send method unavailable')
        target = {'group_openid': ctx.destination} if ctx.scene == 'group' else {'openid': ctx.destination}
        return await asyncio.wait_for(sender(**target, **payload), timeout=25)

    async def message(self, payload: dict) -> Any:
        self.seq += 1
        payload = dict(payload, msg_id=self.ctx.msg_id, msg_seq=self.seq)
        if 'markdown' in payload:
            payload.pop('content', None)
            payload['msg_type'] = 2
        if 'event_id' in payload:
            raise ValueError('normal messages cannot use an interaction event_id')
        result = validate_response(await self._request('messages', payload))
        self.event._has_send_oper = True
        return result

    async def image(self, path: str, caption: str) -> Any:
        content = await asyncio.to_thread(Path(path).read_bytes)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError('calendar exceeds the plugin 8 MiB image limit')
        upload = validate_response(await self._request('files', {
            'file_type': 1,
            'file_data': base64.b64encode(content).decode('ascii'),
            'srv_send_msg': False,
        }), required='file_info')
        return await self.message({'msg_type': 7, 'media': {'file_info': get(upload, 'file_info')},
                                   'content': caption[:600]})


def command_for(item: dict, root: str = '/\u6253\u5361') -> str:
    return f"{root} {item['name']}" + (' +1' if int(item['target']) > 1 else '')


def keyboard(items: list[dict], ctx: QQContext, root: str = '/\u6253\u5361') -> dict:
    actions = []
    for item in items[:8]:
        target, amount = int(item['target']), int(item['amount'])
        label = str(item['name'])
        if target > 1:
            label += f" {amount}/{target}"
        elif amount >= target:
            label = "\u2713 " + label
        actions.append((label, command_for(item, root), amount < target))
    actions += [('\u5237\u65b0\u4eca\u65e5', root, False),
                ('\u672c\u6708\u6708\u5386', root + ' \u6708\u5386', False)]
    buttons = []
    for index, (label, command, incomplete) in enumerate(actions):
        buttons.append({
            'id': f'daka_{index}',
            'render_data': {'label': label[:10], 'visited_label': label[:10],
                            'style': 1 if incomplete else 0},
            'action': {
                'type': 2,
                'permission': {'type': 0, 'specify_user_ids': [ctx.sender]},
                'data': command,
                # Official API: enter applies to C2C only. In groups users
                # press Send after the client inserts @bot + command.
                'enter': ctx.scene == 'c2c',
                'reply': False,
                'unsupport_tips': '\u8bf7\u76f4\u63a5\u53d1\u9001\u5bf9\u5e94\u6253\u5361\u6307\u4ee4',
            },
        })
    return {'content': {'rows': [{'buttons': buttons[i:i+2]} for i in range(0, len(buttons), 2)]}}


def markdown_escape(text: str) -> str:
    text = escape(text, quote=False)
    return re.sub(r'([\\`*_\[\]])', r'\\\1', text)


def command_links(items: list[dict], scene: str, root: str = '/\u6253\u5361') -> str:
    """Native QQ XML tags are generated AFTER escaping user-provided text."""
    commands = [command_for(item, root) for item in items[:8]] + [root, root + ' \u6708\u5386']
    tags = []
    for cmd in commands:
        encoded = quote(cmd, safe='')
        if scene == 'c2c':
            tags.append(f'<qqbot-cmd-enter text="{encoded}" />')
        else:
            show = quote(cmd.removeprefix(root).strip() or '\u5237\u65b0\u4eca\u65e5', safe='')
            tags.append(f'<qqbot-cmd-input text="{encoded}" show="{show}" reference="false" />')
    return '\n'.join('  '.join(tags[i:i+2]) for i in range(0, len(tags), 2))


async def send_status(event, text: str, items: list[dict], mode: str, log,
                      root: str = '/\u6253\u5361', transport: QQTransport | None = None) -> dict:
    """auto: Markdown+keyboard -> Markdown+links -> actual plain text.

    Unlike v1.4 this does not return False silently and does not disable every
    user's buttons after one failure. No assumption about visual rendering.
    """
    transport = transport or QQTransport(event)
    ctx = transport.ctx
    hint_keyboard = ('\u70b9\u4e0b\u65b9\u6309\u94ae\u6253\u5361\uff0c\u968f\u540e\u5237\u65b0\u4eca\u65e5\u72b6\u6001\u3002' if ctx.scene == 'c2c' else
                     '\u70b9\u6309\u94ae\u586b\u5165\u6307\u4ee4\u540e\uff0c\u8bf7\u518d\u6309\u53d1\u9001\u3002')
    hint_links = ('\u70b9\u84dd\u5b57\u53d1\u9001\u6253\u5361\u6307\u4ee4\u3002' if ctx.scene == 'c2c' else
                  '\u70b9\u84dd\u5b57\u586b\u5165\u540e\u53d1\u9001\uff0c\u8bf7\u786e\u8ba4\u5df2@\u673a\u5668\u4eba\u3002')
    stages = ['keyboard', 'links', 'text'] if mode in ('auto', 'keyboard') else (['links', 'text'] if mode == 'links' else ['text'])
    failures = []
    for stage in stages:
        # Never duplicate the entire action list as BOTH blue links and buttons.
        md = markdown_escape(text)
        if stage == 'keyboard':
            md += '\n\n' + hint_keyboard
        elif stage == 'links':
            md += '\n\n' + hint_links + '\n' + command_links(items, ctx.scene, root)
        payload = {'msg_type': 0, 'content': text} if stage == 'text' else {
            'msg_type': 2, 'markdown': {'content': md}}
        if stage == 'keyboard':
            payload['keyboard'] = keyboard(items, ctx, root)
        try:
            result = await transport.message(payload)
        except Exception as exc:
            error = safe_error(exc)
            failures.append(stage + ': ' + error)
            log(f'[daka 1.6] scene={ctx.scene} mode={stage} failed: {error}')
            continue
        report = {'mode': stage, 'scene': ctx.scene, 'errors': failures,
                  'ack': True, 'client_rendering': 'unverified'}
        log(f'[daka 1.6] scene={ctx.scene} mode={stage} API accepted; client rendering unverified')
        return report
    raise QQAPIError('; '.join(failures))
