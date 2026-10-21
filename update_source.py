#!/usr/bin/env python3
"""Offline source-only updater. Never invoke AstrBot's plugin uninstall API.

Usage: python3 update_source.py ./astrbot_plugin_daka_v1.9.4.zip
Only Python's standard library and the already-installed Docker CLI are used.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

NAME = 'astrbot_plugin_daka'
VERSION = '1.9.4'


def docker(*args: str) -> str:
    result = subprocess.run(['docker', *args], text=True, capture_output=True,
                            check=True, timeout=90)
    return result.stdout


def validate_archive(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 150 or sum(e.file_size for e in entries) > 16 * 1024 * 1024:
            raise ValueError('Archive exceeds safety limits.')
        seen = set()
        forbidden_suffixes = ('.sqlite', '.sqlite3', '.db', '.wal', '.shm', '.pyc', '.pyo')
        for entry in entries:
            raw = entry.filename
            p = PurePosixPath(raw)
            is_dir = entry.is_dir()
            normalized = p.as_posix()
            canonical = normalized + ('/' if is_dir else '')
            if (p.is_absolute() or '..' in p.parts or '.' in p.parts or
                    '\\' in raw or not p.parts or p.parts[0] != NAME or
                    raw != canonical or normalized in seen or
                    any(part.startswith('.') for part in p.parts) or
                    Path(p.name).suffix.lower() in forbidden_suffixes):
                raise ValueError('Unsafe/non-canonical/duplicate archive path: ' + raw)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if stat.S_ISLNK(entry.external_attr >> 16) or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('Symlinks and special files are not permitted in this update.')
            seen.add(normalized)
        required = {f'{NAME}/{name}' for name in
                    ('main.py','accounts.py','store.py','qq_transport.py','metadata.yaml',
                     'scoring.py','scored_store.py','panel.py','weighted_calendar.py','daily_image.py','task_colors.py','help_content.py','help_image.py','task_maintenance.py','command_names.py','migrate_defaults_v193.py')}
        if not required <= seen:
            raise ValueError('Required plugin files are missing.')
        metadata = archive.read(f'{NAME}/metadata.yaml').decode('utf-8')
        if not re.search(r'^version:\s*1\.9\.4\s*$', metadata, re.M):
            raise ValueError('This updater only installs version 1.9.4.')
        if not re.search(r'^name:\s*astrbot_plugin_daka\s*$', metadata, re.M):
            raise ValueError('Plugin name mismatch.')
        broken = archive.testzip()
        if broken:
            raise ValueError('ZIP checksum failed: ' + broken)
        return entries


def update(zip_path: Path, root: Path, container: str = 'astrbot', check_only=False) -> Path | None:
    zip_path = zip_path.resolve(strict=True)
    root = root.resolve(strict=True)
    validate_archive(zip_path)
    data = root / 'data'
    if data.is_symlink() or not data.is_dir():
        raise RuntimeError('Expected a real --root/data directory. Aborting.')
    info = json.loads(docker('inspect', container))[0]
    mounts = [m for m in info.get('Mounts', []) if m.get('Destination') == '/AstrBot/data']
    if (len(mounts) != 1 or mounts[0].get('Type') != 'bind' or
            Path(mounts[0]['Source']).resolve() != data.resolve()):
        raise RuntimeError('Container /AstrBot/data bind mount does not match --root/data. Aborting.')
    plugins = data / 'plugins'
    target = plugins / NAME
    if target.is_symlink() or plugins.is_symlink():
        raise RuntimeError('Unexpected plugin directory symlink. Manual review required.')
    if target.exists() and not target.is_dir():
        raise RuntimeError('Plugin path is not a directory.')
    plugin_data_root = data / 'plugin_data'
    plugin_data = plugin_data_root / NAME
    if plugin_data_root.is_symlink() or plugin_data.is_symlink():
        raise RuntimeError('Unexpected plugin_data symlink. Manual review required.')
    if not info.get('State', {}).get('Running'):
        raise RuntimeError('AstrBot is not currently running. Start/check it first; no files changed.')
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print('Archive verified. SHA256:', digest)
    print('Source directory:', target)
    print('Database directory (will NOT be deleted):', data / 'plugin_data' / NAME)
    if check_only:
        print('CHECK ONLY: no changes, no restart.')
        return None
    backups = root / 'backups'
    backups.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = backups / ('daka-' + stamp)
    backup.mkdir(mode=0o700)
    # Extract before stopping into a directory outside AstrBot's watched plugins.
    staging = Path(tempfile.mkdtemp(prefix='.daka-update-', dir=root))
    stopped = False
    old_moved = False
    new_installed = False
    complete = False
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(staging)
        print('Stopping only container:', container)
        docker('stop', '-t', '30', container)
        stopped = True
        plugin_data = data / 'plugin_data' / NAME
        if plugin_data.exists():
            shutil.copytree(plugin_data, backup / 'plugin_data', symlinks=True)
        else:
            (backup / 'NO_PREVIOUS_DATA.txt').write_text(
                'No plugin_data directory existed at backup time. Deleted history is not recoverable from this update.\n')
        config = data / 'config'
        if config.is_dir():
            config_backup = backup / 'config'
            config_backup.mkdir(mode=0o700)
            for item in config.iterdir():
                if NAME in item.name and item.is_file():
                    shutil.copy2(item, config_backup / item.name)
        plugins.mkdir(parents=True, exist_ok=True)
        if target.exists():
            os.replace(target, backup / 'old-plugin')
            old_moved = True
        os.replace(staging / NAME, target)
        new_installed = True
        (backup / 'UPDATE.json').write_text(json.dumps({
            'version': VERSION, 'archive_sha256': digest,
            'container': container, 'plugin_path': str(target),
            'data_path': str(plugin_data),
        }, indent=2) + '\n')
        docker('start', container)
        stopped = False
        complete = True
        print('Source replacement completed. AstrBot container started.')
        print('Safety backup:', backup)
        print('IMPORTANT: container start is not proof of plugin/API success.')
        print('Check: docker logs --since 3m --tail=150 ' + container)
        print('No uninstall, no database deletion, no Compose/network changes were performed.')
        return backup
    finally:
        # If interruption occurs before a successful start, restore the original
        # source folder and restart the previously-running container.
        if not complete and stopped:
            try:
                if new_installed and target.exists():
                    os.replace(target, backup / 'failed-new-plugin')
                if old_moved and (backup / 'old-plugin').exists():
                    os.replace(backup / 'old-plugin', target)
                docker('start', container)
                print('Update aborted; original plugin source restored and container restarted.', file=sys.stderr)
            except Exception as exc:
                print('Automatic restart/rollback failed:', type(exc).__name__, file=sys.stderr)
                print('Backup retained:', backup, file=sys.stderr)
        shutil.rmtree(staging, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip_file', type=Path)
    parser.add_argument('--root', type=Path, default=Path('/opt/services/astrbot'))
    parser.add_argument('--container', default='astrbot')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    try:
        update(args.zip_file, args.root, args.container, args.check_only)
    except (Exception, KeyboardInterrupt) as exc:
        print('UPDATE FAILED:', type(exc).__name__, str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
