"""Verify a stopped run against its preserved copy on the original device."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from longrun import ControllerError, check_storage, read_json


def tree_manifest(root: Path) -> dict:
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            item = {'mode': info.st_mode, 'uid': info.st_uid, 'gid': info.st_gid}
            if stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                item.update(bytes=info.st_size, sha256=digest.hexdigest())
            elif stat.S_ISLNK(info.st_mode):
                item['target'] = os.readlink(path)
            elif not stat.S_ISDIR(info.st_mode):
                raise ControllerError('storage migration contains a special file')
            result[str(path.relative_to(root))] = item
    return result


def verify_migration(run_dir: Path, root: Path, state: dict, storage: dict, source: Path) -> dict:
    old = state['storage']
    if not old.get('verified') or not storage.get('verified') or old['device'] == storage['device']:
        raise ControllerError('storage migration requires two distinct verified data devices')
    source = source.resolve(strict=True)
    mount = Path(storage['mount'])
    if source.is_relative_to(mount) or mount.is_relative_to(source):
        raise ControllerError('storage migration source overlaps the destination')
    if check_storage(old['mount'], source) != old:
        raise ControllerError('storage migration source is not on the original device')
    if state['status'] not in ('FAILED', 'STOPPED', 'PAUSED'):
        raise ControllerError('storage migration requires a stopped run')
    turn = state['turn']
    if turn.get('dir'):
        launch = read_json(Path(turn['dir']) / 'launch.json', {})
        if launch.get('cleanup', {}).get('command'):
            if not read_json(Path(turn['dir']) / 'cleanup-exit.json', {}).get('ok'):
                raise ControllerError('storage migration requires successful previous cleanup')
    manifests = []
    for destination in (run_dir, root):
        relative = destination.resolve().relative_to(mount)
        original = source / relative
        if not original.is_dir() or original.is_symlink():
            raise ControllerError('storage migration original directory is missing')
        if original.stat().st_dev != old['device']:
            raise ControllerError('storage migration original directory changed device')
        before, after = tree_manifest(original), tree_manifest(destination)
        if before != after:
            raise ControllerError('storage migration copy differs from the preserved original')
        import json
        manifests.append({'path': str(relative), 'entries': len(after),
                          'sha256': hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()})
    return {'source': str(source), 'previous_storage': old, 'storage': storage, 'trees': manifests}
