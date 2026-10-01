#!/usr/bin/env python3
"""Archive obsolete monitor entries locally; never stop or contact remote tasks."""
import argparse
import copy
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

DEFAULT_CONFIG = Path(__file__).resolve().with_name('tasks.json')
TERMINAL = {'COMPLETED', 'FAILED', 'STOPPED'}
UNKNOWN = {'UNREACHABLE', 'UNKNOWN'}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def eligible(snapshot, now, max_age=300):
    """Only fresh observations without live processes or observation errors qualify."""
    selected = {}
    for task in snapshot.get('tasks', []):
        observed = timestamp(task.get('observed_at'))
        if observed is None or not 0 <= now-observed <= max_age:
            continue
        if task.get('state') in UNKNOWN or task.get('processes'):
            continue
        if set(task.get('alerts', [])) & {'OBSERVATION_ERROR', 'IDENTITY_MISMATCH', 'STATUS_CONFLICT'}:
            continue
        deadline = timestamp(task.get('deadline_at'))
        if task.get('state') in TERMINAL:
            selected[task['id']] = 'observed terminal state '+task['state']
        elif deadline is not None and deadline <= now:
            selected[task['id']] = 'observed expired deadline and no live task process'
    return selected


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def archive(config_path, reasons, apply=False, expected_digest=None):
    config_path = Path(config_path).resolve()
    # Load the raw configuration, never monitor.load_config() (which resolves credentials).
    with config_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = config_path.read_bytes()
        if expected_digest and hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ValueError('registry changed after preview; inspect the new configuration')
        config = json.loads(raw)
        tasks = {task['id']: task for task in config['tasks']}
        missing = set(reasons)-set(tasks)
        if missing:
            raise ValueError('unknown task IDs: '+', '.join(sorted(missing)))
        selected = [task for task in config['tasks'] if task['id'] in reasons]
        result = {'applied': False, 'selected': [task['id'] for task in selected],
                  'remaining': len(config['tasks'])-len(selected),
                  'remote_tasks_affected': False, 'config_sha256': hashlib.sha256(raw).hexdigest()}
        if not apply or not selected:
            return result
        backup = copy.deepcopy(config)
        backup['tasks'] = selected
        hosts = {t['host'] for t in selected}
        backup['hosts'] = {k:v for k,v in config['hosts'].items() if k in hosts}
        backup['state_dir'] = str((config_path.parent/config.get('state_dir', '.state')).resolve())
        now = datetime.datetime.now(datetime.timezone.utc)
        backup['archive_metadata'] = {'archived_at': now.isoformat(), 'source': str(config_path),
                                      'reasons': reasons, 'source_sha256': result['config_sha256']}
        folder = config_path.parent/'archive'
        folder.mkdir(exist_ok=True)
        destination = folder/('tasks-'+now.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]+'.json')
        atomic_json(destination, backup)
        config['tasks'] = [t for t in config['tasks'] if t['id'] not in reasons]
        needed = {t['host'] for t in config['tasks']}
        config['hosts'] = {k:v for k,v in config['hosts'].items() if k in needed}
        # Detect an editor that did not take the advisory lock before replacing the registry.
        if config_path.read_bytes() != raw:
            raise ValueError('registry changed during archival; backup retained, active registry untouched')
        atomic_json(config_path, config)
        result.update(applied=True, archive=str(destination))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest='action', required=True)
    explicit = commands.add_parser('archive', help='archive explicitly selected obsolete entries')
    explicit.add_argument('--task', action='append', required=True)
    explicit.add_argument('--reason', required=True)
    prune = commands.add_parser('prune', help='select finished/expired entries from a recent status snapshot')
    prune.add_argument('--snapshot', type=Path, required=True)
    prune.add_argument('--max-age-seconds', type=float, default=300)
    for command in (explicit, prune):
        command.add_argument('--apply', action='store_true', help='write archive and remove selected registry entries')
        command.add_argument('--expected-sha256', help='optional digest from an earlier preview')
    args = parser.parse_args()
    try:
        if args.action == 'archive':
            if not args.reason.strip():
                raise ValueError('a nonempty archival reason is required')
            reasons = {task:args.reason for task in args.task}
        else:
            if not 0 < args.max_age_seconds <= 3600:
                raise ValueError('snapshot age must be in (0, 3600] seconds')
            snapshot = json.loads(args.snapshot.read_text())
            reasons = eligible(snapshot, datetime.datetime.now(datetime.timezone.utc).timestamp(), args.max_age_seconds)
            active = {t['id'] for t in json.loads(args.config.read_text())['tasks']}
            reasons = {k:v for k,v in reasons.items() if k in active}
        print(json.dumps(archive(args.config, reasons, args.apply, args.expected_sha256), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
