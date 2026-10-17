"""Identity-aware storage for per-person QQ habits.

QQ official OpenIDs are not QQ UINs. Exact matching OpenIDs in the same bot
namespace share one account; differing values need a verified binding. Legacy (scope, user_id) rows are copied, never
removed, into a person's record namespace on first encounter.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import datetime

from .store import CheckinStore, DEFAULT_HABITS, HabitError


def legacy_identity_aliases(origin: str, sender: str, is_group: bool,
                     union_openid: str | None = None,
                     adapter_type: str | None = None) -> list[str]:
    """Return trusted aliases, plus the exact legacy v1.3 alias.

    AstrBot's unified_msg_origin starts with the *instance id*, which may be
    `ice_public_astrbot` rather than `qq_official`. The underlying adapter
    must come from event.get_platform_name(), not the origin prefix.

    QQ official group_member_openid and C2C user_openid must not be assumed
    identical. Bind once with the challenge flow if no verified union id exists.
    """
    instance = origin.split(":", 1)[0].lower()
    platform = str(adapter_type or instance).strip().lower()
    if not sender or sender == "None":
        raise HabitError("未取得当前用户的平台标识，无法保存打卡。")

    # Earlier 1.3.0 releases used the UMO prefix as adapter type. Retain their
    # group-/private-specific aliases to merge existing person accounts.
    old_alias = f"{instance}:{'group' if is_group else 'private'}:{origin}:{sender}"

    aliases = []
    if platform in ("aiocqhttp", "onebot", "onebot_v11"):
        aliases.append(f"{instance}:qq-uin:{sender}")
        if instance == "aiocqhttp":
            aliases.append(f"aiocqhttp:uin:{sender}")
    elif platform in ("qq_official", "qq_official_webhook", "qqofficial"):
        if union_openid:
            aliases.append(f"{instance}:union:{str(union_openid).strip()}")
            if instance == platform:
                aliases.append(f"{platform}:union:{str(union_openid).strip()}")
        if is_group:
            aliases.append(f"{instance}:official-group:{origin}:{sender}")
        else:
            aliases.append(f"{instance}:official-c2c:{sender}")
        # The old official-specific key must also resolve when the instance
        # used the default adapter name instead of a custom ID.
        if instance in ("qq_official", "qq_official_webhook"):
            aliases.append(f"{instance}:{'group' if is_group else 'private'}:{origin if is_group else sender}{':' + sender if is_group else ''}")
    else:
        aliases.append(f"{instance}:unknown:{'group' if is_group else 'private'}:{origin}:{sender}")
    aliases.append(old_alias)
    return list(dict.fromkeys(alias for alias in aliases if alias))


def identity_aliases(origin: str, sender: str, is_group: bool,
                     union_openid: str | None = None,
                     adapter_type: str | None = None,
                     app_id: str | None = None) -> list[str]:
    """Same exact sender OpenID in the same bot namespace is one person.

    A group ID/session is a delivery destination, not part of a user's identity.
    Different OpenIDs are NEVER linked by name or by a user-supplied QQ number.
    Keep legacy aliases only as migration inputs.
    """
    instance = origin.split(":", 1)[0].lower()
    platform = str(adapter_type or instance).lower()
    legacy = legacy_identity_aliases(origin, sender, is_group, union_openid,
                                      adapter_type=adapter_type)
    if platform in ("qq_official", "qq_official_webhook", "qqofficial"):
        namespace = "app:" + str(app_id) if app_id else "instance:" + instance
        canonical = f"qqv2:{namespace}:openid:{sender}"
        shared = [canonical]
        if app_id:
            shared.append(f"qqv2:instance:{instance}:openid:{sender}")
        return list(dict.fromkeys(shared + legacy))
    return legacy


def _same_legacy_sender(alias: str, instance: str, sender: str) -> bool:
    """Match only known old key formats, not arbitrary substring matches."""
    if alias in (f"{instance}:official-c2c:{sender}",
                 f"{instance}:private:{sender}"):
        return True
    for prefix in (f"{instance}:official-group:", f"{instance}:group:",
                   f"{instance}:private:", f"{instance}:unknown:group:",
                   f"{instance}:unknown:private:"):
        if not alias.startswith(prefix):
            continue
        tail = alias[len(prefix):]
        if ":" not in tail:
            continue
        origin, uid = tail.rsplit(":", 1)
        fields = origin.split(":", 2)
        if (uid == sender and len(fields) == 3 and
                fields[0].lower() == instance and
                fields[1] in ("GroupMessage", "FriendMessage")):
            return True
    return False


class AccountCheckinStore(CheckinStore):
    """Reuses v1 check-in tables under a global, anonymous per-person scope.

    V1 tables remain intact for rollback. Each legacy identity's records are
    imported at most once. On account linking, equal name+day amounts use the
    maximum, never a sum, to avoid double-crediting historical check-ins.
    """

    SELF = 'self'

    def __init__(self, db_path):
        super().__init__(db_path)
        with self._db() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS account_redirects (
                    source TEXT PRIMARY KEY, target TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS identity_namespaces (
                    instance TEXT PRIMARY KEY, namespace TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_identity_types (
                    scope TEXT PRIMARY KEY, adapter TEXT NOT NULL, app_id TEXT
                );
                CREATE TABLE IF NOT EXISTS person_accounts (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS person_aliases (
                    alias TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    linked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_person_alias_account
                ON person_aliases(account_id);
                CREATE TABLE IF NOT EXISTS imported_legacy (
                    old_scope TEXT NOT NULL, old_user_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, imported_at TEXT NOT NULL,
                    PRIMARY KEY(old_scope,old_user_id)
                );
            ''')

    @staticmethod
    def person_scope(account_id: str) -> str:
        return 'person:' + account_id

    def _ensure_person_locked(self, conn, account_id: str) -> None:
        scope = self.person_scope(account_id)
        now = self._now()
        result = conn.execute('INSERT OR IGNORE INTO groups(scope,created_at) VALUES (?,?)', (scope, now))
        if result.rowcount:
            conn.executemany(
                'INSERT INTO habits(scope,name,target,created_by,created_at) VALUES (?,?,?, ?,?)',
                [(scope, name, target, '__system__', now) for name, target in DEFAULT_HABITS]
            )

    @staticmethod
    def _combine_record_locked(conn, scope: str, user: str, name: str,
                               day: str, amount: int, updated_at: str) -> None:
        conn.execute('''
            INSERT INTO records(scope,user_id,name,day,amount,updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(scope,user_id,name,day) DO UPDATE SET
                updated_at=CASE WHEN excluded.amount > records.amount
                    THEN excluded.updated_at ELSE records.updated_at END,
                amount=MAX(records.amount,excluded.amount)
        ''', (scope, user, name, day, amount, updated_at))

    def _import_legacy_locked(self, conn, account_id: str, old_scope: str, old_user: str):
        if old_scope.startswith('person:'):
            return account_id
        imported = conn.execute(
            'SELECT account_id FROM imported_legacy WHERE old_scope=? AND old_user_id=?',
            (old_scope, old_user),
        ).fetchone()
        if imported:
            if imported['account_id'] != account_id:
                self._merge_persons_locked(conn, account_id, imported['account_id'])
            return account_id

        destination = self.person_scope(account_id)
        now = self._now()
        default_targets = dict(DEFAULT_HABITS)
        is_first_import = not conn.execute(
            'SELECT 1 FROM imported_legacy WHERE account_id=? LIMIT 1', (account_id,)
        ).fetchone()
        for habit in conn.execute(
            'SELECT name,target,created_by,enabled,created_at FROM habits WHERE scope=? ORDER BY created_at,rowid',
            (old_scope,),
        ).fetchall():
            if not habit['enabled']:
                continue
            creator = '__system__' if habit['created_by'] == '__system__' else self.SELF
            conn.execute(
                'INSERT OR IGNORE INTO habits(scope,name,target,created_by,enabled,created_at) '
                'VALUES (?,?,?,?,1,?)',
                (destination, habit['name'], habit['target'], creator, habit['created_at'] or now),
            )
            if is_first_import and habit['name'] in default_targets:
                # Newly seeded defaults (e.g. water 8) must not mask a
                # customized legacy group target (e.g. water 10).
                conn.execute(
                    'UPDATE habits SET target=? WHERE scope=? AND name=? '
                    'AND target=? AND created_by=?',
                    (habit['target'], destination, habit['name'],
                     default_targets[habit['name']], '__system__'),
                )
        for record in conn.execute(
            'SELECT name,day,amount,updated_at FROM records WHERE scope=? AND user_id=?',
            (old_scope, old_user),
        ).fetchall():
            self._combine_record_locked(conn, destination, self.SELF,
                                        record['name'], record['day'], record['amount'], record['updated_at'])
        # Time records are copied with their recorded actual timestamps. Import
        # marker prevents repeated insertion on later commands.
        for event in conn.execute(
            'SELECT name,day,delta,checked_at,kind FROM checkin_events WHERE scope=? AND user_id=? ORDER BY id',
            (old_scope, old_user),
        ).fetchall():
            conn.execute(
                'INSERT INTO checkin_events(scope,user_id,name,day,delta,checked_at,kind) '
                'VALUES (?,?,?,?,?,?,?)',
                (destination, self.SELF, event['name'], event['day'], event['delta'], event['checked_at'], event['kind']),
            )
        conn.execute(
            'INSERT INTO imported_legacy(old_scope,old_user_id,account_id,imported_at) VALUES (?,?,?,?)',
            (old_scope, old_user, account_id, now),
        )
        return account_id

    @staticmethod
    def _root_locked(conn, account_id: str) -> str:
        visited = set()
        while account_id not in visited:
            visited.add(account_id)
            row = conn.execute('SELECT target FROM account_redirects WHERE source=?',
                               (account_id,)).fetchone()
            if not row:
                return account_id
            account_id = row['target']
        raise HabitError('Account redirect cycle: restore a database backup.')

    def _merge_persons_locked(self, conn, target: str, source: str) -> str:
        target = self._root_locked(conn, target)
        source = self._root_locked(conn, source)
        if target == source:
            return target
        self._ensure_person_locked(conn, target)
        dst, src = self.person_scope(target), self.person_scope(source)
        for h in conn.execute(
            'SELECT name,target,created_by,enabled,created_at FROM habits WHERE scope=?', (src,)
        ).fetchall():
            conn.execute(
                'INSERT OR IGNORE INTO habits(scope,name,target,created_by,enabled,created_at) '
                'VALUES (?,?,?,?,?,?)',
                (dst, h['name'], h['target'], h['created_by'], h['enabled'], h['created_at']),
            )
            default = dict(DEFAULT_HABITS).get(h['name'])
            if default is not None and h['target'] != default:
                conn.execute('UPDATE habits SET target=? WHERE scope=? AND name=? '
                             'AND target=? AND created_by=?',
                             (h['target'], dst, h['name'], default, '__system__'))
        for r in conn.execute(
            'SELECT name,day,amount,updated_at FROM records WHERE scope=? AND user_id=?', (src, self.SELF)
        ).fetchall():
            self._combine_record_locked(conn, dst, self.SELF, r['name'], r['day'], r['amount'], r['updated_at'])
        for e in conn.execute(
            'SELECT name,day,delta,checked_at,kind FROM checkin_events WHERE scope=? AND user_id=? ORDER BY id',
            (src, self.SELF),
        ).fetchall():
            key = (dst, self.SELF, e['name'], e['day'], e['delta'], e['checked_at'], e['kind'])
            if not conn.execute('SELECT 1 FROM checkin_events WHERE scope=? AND user_id=? '
                                'AND name=? AND day=? AND delta=? AND checked_at=? AND kind=?',
                                key).fetchone():
                conn.execute('INSERT INTO checkin_events(scope,user_id,name,day,delta,checked_at,kind) '
                             'VALUES (?,?,?,?,?,?,?)', key)
        # Preserve group membership and most-private choice when duplicates exist.
        for member in conn.execute(
            'SELECT scope,display_name,visible,joined_at FROM members WHERE user_id=?', (source,)
        ).fetchall():
            conn.execute('''
                INSERT INTO members(scope,user_id,display_name,visible,joined_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(scope,user_id) DO UPDATE SET
                    visible=MIN(members.visible,excluded.visible)
            ''', (member['scope'], target, member['display_name'], member['visible'], member['joined_at']))
        conn.execute('DELETE FROM members WHERE user_id=?', (source,))
        conn.execute('UPDATE person_aliases SET account_id=? WHERE account_id=?', (target, source))
        conn.execute('UPDATE imported_legacy SET account_id=? WHERE account_id=?', (target, source))
        conn.execute('INSERT OR REPLACE INTO account_redirects(source,target) VALUES (?,?)',
                     (source, target))
        # Keep the former account data for rollback, but never import it twice.
        return target

    def resolve_person(self, aliases: list[str], old_scope: str, old_user: str) -> str:
        if not aliases or not all(aliases):
            raise HabitError("Missing reliable platform identity.")
        instance = old_scope.split(":", 1)[0].lower()
        is_qq = aliases[0].startswith("qqv2:")
        with self._db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if is_qq:
                namespace = aliases[0].rsplit(":openid:", 1)[0]
                claim = conn.execute('SELECT namespace FROM identity_namespaces WHERE instance=?',
                                     (instance,)).fetchone()
                # Do not reuse one instance's historical account keys for a different AppID.
                if claim and claim['namespace'] != namespace:
                    fallback = 'qqv2:instance:' + instance
                    if claim['namespace'] != fallback and namespace != fallback:
                        raise HabitError("QQ AppID changed for this instance. Use a new AstrBot platform instance ID to avoid merging different applications.")
                    if namespace == fallback and claim['namespace'] != fallback:
                        aliases[0] = claim['namespace'] + ':openid:' + old_user
                        namespace = claim['namespace']
                conn.execute('INSERT OR REPLACE INTO identity_namespaces(instance,namespace) VALUES (?,?)',
                             (instance, namespace))
                # Collect every v1.3/v1.4 split account for THIS exact sender in THIS instance.
                for row in conn.execute('SELECT alias FROM person_aliases').fetchall():
                    if _same_legacy_sender(row['alias'], instance, old_user):
                        aliases.append(row['alias'])
            aliases = list(dict.fromkeys(aliases))
            matches = []
            for alias in aliases:
                row = conn.execute('SELECT account_id FROM person_aliases WHERE alias=?', (alias,)).fetchone()
                if row:
                    root = self._root_locked(conn, row['account_id'])
                    if root not in matches:
                        matches.append(root)
            if matches:
                selected = matches[0]
            else:
                selected = uuid.uuid4().hex
                conn.execute('INSERT INTO person_accounts(id,created_at) VALUES (?,?)',
                             (selected, self._now()))
            self._ensure_person_locked(conn, selected)
            for other in matches[1:]:
                selected = self._merge_persons_locked(conn, selected, other)
            for alias in aliases:
                conn.execute('INSERT INTO person_aliases(alias,account_id,linked_at) VALUES (?,?,?) '
                             'ON CONFLICT(alias) DO UPDATE SET account_id=excluded.account_id',
                             (alias, selected, self._now()))
            self._import_legacy_locked(conn, selected, old_scope, old_user)
            if is_qq:
                # Also import older per-conversation rows for this same sender, not just
                # the window that happened to issue the first v1.5 command.
                rows = conn.execute('SELECT DISTINCT scope FROM records WHERE user_id=? '
                                    'UNION SELECT DISTINCT scope FROM checkin_events WHERE user_id=?',
                                    (old_user, old_user)).fetchall()
                for row in rows:
                    parts = row['scope'].split(':', 2)
                    if (len(parts) == 3 and parts[0].lower() == instance and
                            parts[1] in ('GroupMessage', 'FriendMessage')):
                        self._import_legacy_locked(conn, selected, row['scope'], old_user)
            selected = self._root_locked(conn, selected)
        return selected

    def remember_group_identity(self, scope: str, adapter: str, app_id: str | None):
        with self._db() as conn:
            conn.execute('INSERT OR REPLACE INTO group_identity_types(scope,adapter,app_id) VALUES (?,?,?)',
                         (scope, adapter, app_id))

    def merge_persons(self, preferred: str, other: str) -> str:
        with self._db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return self._merge_persons_locked(conn, preferred, other)

    def member_in_group(self, group_scope: str, account_id: str, nickname: str,
                        original_user_id: str | None = None) -> None:
        nickname = (nickname or '群友').replace('\n', ' ').replace('\r', ' ').strip()[:32]
        with self._db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            privacy = None
            if original_user_id:
                old = conn.execute(
                    'SELECT visible FROM members WHERE scope=? AND user_id=?',
                    (group_scope, original_user_id),
                ).fetchone()
                if old:
                    privacy = old['visible']
            present = conn.execute(
                'SELECT visible FROM members WHERE scope=? AND user_id=?',
                (group_scope, account_id),
            ).fetchone()
            visible = int(present['visible']) if present else (int(privacy) if privacy is not None else 1)
            conn.execute('''
                INSERT INTO members(scope,user_id,display_name,visible,joined_at)
                VALUES (?,?,?,?,?) ON CONFLICT(scope,user_id)
                DO UPDATE SET display_name=excluded.display_name
            ''', (group_scope, account_id, nickname, visible, self._now()))
            if original_user_id and original_user_id != account_id:
                # Legacy source rows in records/checkin_events are retained.
                conn.execute(
                    'DELETE FROM members WHERE scope=? AND user_id=?',
                    (group_scope, original_user_id),
                )

    def upgrade_group_members(self, group_scope: str) -> None:
        """Lazy-migrate old group participants for friend queries/rankings.

        Members from v1.2 need not send a new command after plugin upgrade.
        No nickname-based account merge is performed.
        """
        with self._db() as conn:
            old_members = conn.execute('''
                SELECT m.user_id,m.display_name FROM members m
                LEFT JOIN person_accounts p ON p.id=m.user_id
                WHERE m.scope=? AND p.id IS NULL
            ''', (group_scope,)).fetchall()
            identity = conn.execute('SELECT adapter,app_id FROM group_identity_types WHERE scope=?',
                                    (group_scope,)).fetchone()
        for member in old_members:
            aliases = identity_aliases(group_scope, member['user_id'], True,
                                      adapter_type=identity['adapter'] if identity else None,
                                      app_id=identity['app_id'] if identity else None)
            account_id = self.resolve_person(aliases, group_scope, member['user_id'])
            self.member_in_group(group_scope, account_id, member['display_name'], member['user_id'])

    def group_friend(self, group_scope: str, search: str) -> dict:
        with self._db() as conn:
            rows = conn.execute('''
                SELECT m.user_id,m.display_name,m.visible FROM members m
                JOIN person_accounts a ON a.id=m.user_id
                WHERE m.scope=? AND (m.user_id=? OR m.display_name=?)
            ''', (group_scope, search, search)).fetchall()
        if not rows:
            raise HabitError('未找到这位群友。对方需要先在本群使用过打卡插件。')
        if len(rows) > 1:
            raise HabitError('有重名群友，请使用对方的打卡用户 ID，或让对方修改群昵称。')
        member = dict(rows[0])
        if not member['visible']:
            raise HabitError('这位群友关闭了打卡记录共享。')
        return member

    def group_rankings(self, group_scope: str, year: int, month: int) -> list[dict]:
        start = f'{year:04d}-{month:02d}-01'
        end = f'{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}'
        with self._db() as conn:
            rows = conn.execute('''
                SELECT m.user_id,m.display_name,
                    COUNT(CASE WHEN h.name IS NOT NULL AND r.amount >= h.target THEN 1 END) AS completed
                FROM members m JOIN person_accounts a ON a.id=m.user_id
                LEFT JOIN records r ON r.scope='person:' || m.user_id
                    AND r.user_id='self' AND r.day BETWEEN ? AND ?
                LEFT JOIN habits h ON h.scope=r.scope AND h.name=r.name AND h.enabled=1
                WHERE m.scope=? AND m.visible=1
                GROUP BY m.user_id,m.display_name
                HAVING completed>0
                ORDER BY completed DESC,m.joined_at ASC LIMIT 30
            ''', (start, end, group_scope)).fetchall()
        return [dict(row) for row in rows]
