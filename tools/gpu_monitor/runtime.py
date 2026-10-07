"""Read-only endpoint, scheduler and container ownership projections.

This standard-library module is also streamed to the registered SSH host.  It
only inspects registered scheduler scopes and never opens scoring assets.
"""
from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

try:
    from tools.gpu_scheduler import container_ownership as _official_ownership
except ImportError:
    # A streamed SSH probe may have no installed workspace package.  The
    # fallback below preserves the official read-only predicates.
    _official_ownership = None

_READ_CACHE = OrderedDict()
_JSON_CACHE = OrderedDict()
_ROW_CACHE = OrderedDict()
_LIVE = {'QUEUED', 'STARTING', 'RUNNING', 'CANCELLING', 'UNKNOWN'}
_TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'EXPIRED', 'INFEASIBLE'}
_SIMPLE_ID = re.compile(r'^[A-Za-z0-9_.-]+$')
_HEX_ID = re.compile(r'^[a-f0-9]{12,64}$')
_RESOURCE_KEYS = ('memory_mib', 'compute_units', 'cpu_cores', 'ram_mib')


def _valid_name(value):
    return isinstance(value, str) and value not in ('.', '..') and bool(_SIMPLE_ID.fullmatch(value))


def endpoint_key(host):
    """Connection identity contains no password and no configuration alias."""
    transport = host.get('transport', 'ssh')
    identity = [transport, host.get('hostname', host.get('host', '')),
                host.get('port', 22) if transport == 'ssh' else None,
                host.get('user', '') if transport == 'ssh' else '', host.get('auth_profile', 'global')]
    return 'endpoint-' + hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()[:24]


def group_endpoints(hosts):
    result = {}
    for alias, host in hosts.items():
        key = endpoint_key(host)
        result.setdefault(key, {'endpoint_id': key, 'aliases': [], 'host': host})['aliases'].append(alias)
    for item in result.values():
        item['aliases'].sort()
    return result


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _epoch(value):
    if _finite(value):
        return float(value)
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _strings(value):
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def _object(value):
    return value if isinstance(value, dict) else {}


def _request_patterns(requests):
    patterns = []
    for request in requests:
        parts = request.split('/')
        # Attempt numbers may precede a fixed suffix such as /research.  Only
        # that numeric segment is variable; preserve the group and suffix.
        indexes = [index for index in (len(parts) - 1, len(parts) - 2)
                   if index >= 2 and parts[index].isdigit()]
        if indexes:
            index = indexes[0]
            pattern = '/'.join(r'[0-9]+' if pos == index else re.escape(part)
                               for pos, part in enumerate(parts))
            patterns.append(re.compile('^' + pattern + '$'))
    return patterns


def _registered_match(row, job_ids, requests, patterns):
    request = row.get('request_id', _object(row.get('spec')).get('request_id'))
    return (row.get('id', row.get('job_id')) in job_ids or request in requests or
            isinstance(request, str) and any(pattern.fullmatch(request) for pattern in patterns))


def validate_registration(task):
    """Validate scheduler discovery bounds without reading any registered file."""
    scheduler = task.get('scheduler') or {}
    for key in ('root',):
        value = scheduler.get(key)
        if value is not None and (not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts):
            raise ValueError('scheduler root must be an absolute bounded path')
    root = _registered_root(task)
    for key in ('status_paths', 'job_status_paths', 'job_dirs', 'job_directories'):
        value = scheduler.get(key)
        if value is None:
            continue
        values = _strings(value)
        if not values or isinstance(value, list) and len(values) != len(value):
            raise ValueError('scheduler paths must be nonempty strings')
        for item in values:
            path = Path(item)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('scheduler paths must be absolute and scoped')
            if key in ('status_paths', 'job_status_paths') and path.name != 'status.json':
                raise ValueError('scheduler status paths must name status.json')
            if root is not None and not path.is_relative_to(root / 'sessions'):
                raise ValueError('scheduler paths must stay in registered scheduler sessions')
    for key in ('session_id', 'session_ids', 'job_id', 'job_ids'):
        if any(not _SIMPLE_ID.fullmatch(value) or value in ('.', '..') for value in _strings(scheduler.get(key))):
            raise ValueError('scheduler identities must be simple path-safe names')


def _bounded_read(path, *, tail=False, limit=1024 * 1024):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not tail and info.st_size > limit:
                return None
            key = (str(path), tail, limit)
            fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            cached = _READ_CACHE.get(key)
            if cached and cached[0] == fingerprint:
                _READ_CACHE.move_to_end(key)
                return cached[1]
            offset = max(0, info.st_size - limit) if tail else 0
            stream.seek(offset)
            payload = stream.read(limit)
            if offset:
                payload = payload.split(b'\n', 1)[1] if b'\n' in payload else b''
            _READ_CACHE[key] = (fingerprint, payload)
            while len(_READ_CACHE) > 128:
                _READ_CACHE.popitem(last=False)
            return payload
    except OSError:
        return None


def _json(path):
    payload = _bounded_read(path)
    cached = _JSON_CACHE.get(str(path))
    if cached and cached[0] is payload:
        return cached[1]
    try:
        value = json.loads(payload)
        value = value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        value = None
    _JSON_CACHE[str(path)] = (payload, value)
    while len(_JSON_CACHE) > 128:
        _JSON_CACHE.popitem(last=False)
    return value


def _rows(path):
    payload = _bounded_read(path, tail=True)
    cached = _ROW_CACHE.get(str(path))
    if cached and cached[0] is payload:
        return cached[1]
    result = []
    for line in (payload or b'').splitlines(keepends=True):
        if not line.endswith(b'\n'):
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                result.append(row)
        except (ValueError, UnicodeError):
            continue
    _ROW_CACHE[str(path)] = (payload, result)
    while len(_ROW_CACHE) > 128:
        _ROW_CACHE.popitem(last=False)
    return result


def _registered_root(task):
    root = (task.get('scheduler') or {}).get('root')
    if isinstance(root, str) and Path(root).is_absolute():
        return Path(root)
    path = Path(task.get('root', ''))
    if path.is_absolute() and path.parent.name == 'jobs' and path.parent.parent.parent.name == 'sessions':
        return path.parent.parent.parent.parent
    return None


def _controller_gpu(task, raw_task):
    entry = (raw_task or {}).get('files', {}).get((task.get('status') or {}).get('path'), {})
    try:
        status = json.loads(entry.get('text', '{}'))
    except (ValueError, TypeError):
        return []
    if not isinstance(status, dict):
        return []
    expected = (task.get('controller') or {}).get('run_id')
    if expected and status.get('run_id') != expected:
        return []
    gpu = status.get('gpu') or status.get('gpu_state') or {}
    requests = _object(_object(_object(status.get('turn')).get('gpu_wait')).get('requests'))
    snapshots = list(requests.values())
    if isinstance(gpu, dict) and gpu:
        snapshots.append(gpu)
    return [value for value in snapshots if isinstance(value, dict)]


def _project(row, source, now):
    spec = _object(row.get('spec'))
    resources = _object(row.get('resources')) or spec
    result = {key: row.get(key) for key in ('state', 'gpu_uuid', 'submitted_at', 'started_at',
              'finished_at', 'bypasses', 'revision', 'session_id', 'origin_session_id', 'reconciling')}
    result.update(job_id=row.get('id', row.get('job_id')), request_id=row.get('request_id', spec.get('request_id')),
                  source=source, reason=str(row.get('reason') or 'unspecified')[:160],
                  resources={key: resources[key] for key in _RESOURCE_KEYS if _finite(resources.get(key))},
                  deadline_epoch=_epoch(row.get('deadline_epoch', spec.get('deadline_epoch'))))
    updated = _epoch(row.get('at_epoch', row.get('at', row.get('updated_at'))))
    result['last_updated_at'] = updated
    result['staleness_seconds'] = max(0, now - updated) if updated is not None else None
    result['waiting_seconds'] = (max(0, now - row['submitted_at'])
                                 if _finite(row.get('submitted_at')) and row.get('state') == 'QUEUED' else 0)
    queue = _object(row.get('queue'))
    result['queue'] = {key: queue[key] for key in ('wait_seconds', 'protected', 'latest_start_epoch',
                       'projected_start_epoch', 'deadline_risk') if key in queue}
    return result


def collect_scheduler(task, raw_task=None, *, now=None):
    """Replay registered job/session state and bounded native scheduler events."""
    now = time.time() if now is None else now
    scheduler = task.get('scheduler') or {}
    configured = _strings(scheduler.get('job_ids', scheduler.get('job_id')))
    try:
        validate_registration(task)
    except ValueError:
        return {'configured_job_ids': configured, 'configured_job_state': 'historical',
                'active_job': None, 'attempts': [], 'history': [], 'alerts': ['SCHEDULER_REGISTRATION_INVALID'],
                'data_gap': True, 'gap_reason': 'invalid bounded scheduler registration'}
    requests = set(_strings(scheduler.get('request_ids', scheduler.get('request_id'))))
    controller = _controller_gpu(task, raw_task)
    job_ids = set(configured)
    for gpu in controller:
        job_ids.update(_strings(gpu.get('job_id', gpu.get('id'))))
        requests.update(_strings(gpu.get('request_id')))
    patterns = _request_patterns(requests)
    sessions = set(_strings(scheduler.get('session_ids', scheduler.get('session_id'))))
    paths = [Path(v) for v in _strings(scheduler.get('job_dirs', scheduler.get('job_directories'))) if Path(v).is_absolute()]
    for value in _strings(scheduler.get('status_paths', scheduler.get('job_status_paths'))):
        if Path(value).is_absolute() and Path(value).name == 'status.json':
            paths.append(Path(value).parent)
    if Path(task.get('root', '')).name in configured:
        paths.append(Path(task['root']))
    root = _registered_root(task)
    candidates = []
    external = []
    ledger_invalid = False
    if root:
        service = _json(root / 'service.json') or {}
        sid = service.get('session_id')
        if _valid_name(sid):
            sessions.add(sid)
        for sid in sorted(sessions):
            if not _valid_name(sid):
                continue
            session = root / 'sessions' / sid
            if not session.resolve().is_relative_to(root.resolve() / 'sessions'):
                continue
            paths.extend(session / 'jobs' / jid for jid in job_ids if _valid_name(jid))
            for row in _rows(session / 'events.jsonl'):
                request = row.get('request_id', _object(row.get('spec')).get('request_id'))
                if _registered_match(row, job_ids, requests, patterns):
                    candidates.append((row, 'scheduler_events'))
                    directory = row.get('directory')
                    if isinstance(directory, str):
                        path = Path(directory)
                        if (path.is_absolute() and path.is_relative_to(root / 'sessions')
                                and path.parent.name == 'jobs' and _valid_name(path.name)):
                            paths.append(path)
                else:
                    external.append(row)
        # The durable ledger is the current intent authority after a service
        # restart.  Verify individual record checksums without instantiating
        # Journal, whose torn-tail recovery intentionally mutates files.
        ledger_rows = _rows(root / 'requests.jsonl')
        previous_record = None
        for record in ledger_rows:
            checksum = record.get('sha256')
            if not isinstance(checksum, str):
                ledger_invalid = True
                continue
            payload = {key: value for key, value in record.items() if key != 'sha256'}
            try:
                encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()
            except (TypeError, ValueError):
                ledger_invalid = True
                continue
            if hashlib.sha256(encoded).hexdigest() != checksum:
                ledger_invalid = True
                continue
            if previous_record and (record.get('sequence') != previous_record.get('sequence', 0) + 1
                                    or record.get('previous') != previous_record.get('sha256')):
                ledger_invalid = True
                continue
            previous_record = record
            row = record.get('job')
            if not isinstance(row, dict):
                continue
            request = row.get('request_id', _object(row.get('spec')).get('request_id'))
            row = dict(row, session_id=record.get('session_id'), at_epoch=record.get('at_epoch'))
            if _registered_match(row, job_ids, requests, patterns):
                candidates.append((row, 'scheduler_ledger'))
            else:
                external.append(row)
        # A scope-limited receipt from the official handoff state can supply a
        # job identity even after scheduler event rotation.  Its sequence is
        # the official scheduler revision and is compared with native records.
        for row in controller:
            if row.get('job_id'):
                candidates.append((dict(row, id=row['job_id'], revision=row.get('sequence', 0)), 'handoff_gpu_state'))
    for path in dict.fromkeys(paths):
        # Every path is exact, but symlinks must not expand the scope.
        if root and not path.resolve().is_relative_to(root.resolve() / 'sessions'):
            continue
        row = _json(path / 'status.json')
        if row:
            spec = _json(path / 'spec.json') or {}
            row = dict(row, spec=spec)
            if path.parent.name == 'jobs' and row.get('id') != path.name:
                row.update(id=path.name, _registration_conflict=True)
            if spec.get('request_id') and row.get('request_id') and spec['request_id'] != row['request_id']:
                row['_registration_conflict'] = True
            try:
                row.setdefault('at_epoch', (path / 'status.json').stat().st_mtime)
            except OSError:
                pass
            candidates.append((row, 'scheduler_status'))
    latest, fingerprints, conflicts, request_jobs = {}, {}, set(), {}
    for row, source in candidates:
        jid = row.get('id', row.get('job_id'))
        if not isinstance(jid, str) or row.get('state') not in _LIVE | _TERMINAL:
            continue
        request = row.get('request_id', _object(row.get('spec')).get('request_id'))
        key = request or jid
        if row.get('_registration_conflict'):
            conflicts.add(key)
        if request_jobs.setdefault(key, jid) != jid:
            conflicts.add(key)
        spec = _object(row.get('spec'))
        if spec:
            digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if fingerprints.setdefault(key, digest) != digest:
                conflicts.add(key)
        rank = (row.get('revision') if _finite(row.get('revision')) else 0,
                _epoch(row.get('at_epoch', row.get('at'))) or 0)
        old = latest.get(key)
        if old and rank[0] == old[0][0] and row['state'] != old[1]['state']:
            conflicts.add(key)
        if old is None or rank >= old[0]:
            latest[key] = (rank, row, source)
    attempts = []
    for key, (_, row, source) in latest.items():
        value = _project(row, source, now)
        if key in conflicts:
            value.update(state='UNKNOWN', reason='request identity or state conflict', identity_conflict=True)
        elif ledger_invalid and value.get('state') in _LIVE:
            value.update(state='UNKNOWN', reason='durable ledger invalid; current execution unverified', data_gap=True)
        attempts.append(value)
    attempts.sort(key=lambda row: (_epoch(row.get('submitted_at')) or 0, row.get('revision') or 0))
    live = [row for row in attempts if row['state'] in _LIVE]
    outside = {}
    for row in external:
        key = row.get('request_id') or _object(row.get('spec')).get('request_id') or row.get('id')
        previous = outside.get(key)
        rank = (row.get('revision') if _finite(row.get('revision')) else 0,
                _epoch(row.get('at_epoch', row.get('at'))) or 0)
        old_rank = ((previous.get('revision') if _finite(previous.get('revision')) else 0,
                     _epoch(previous.get('at_epoch', previous.get('at'))) or 0) if previous else (0, 0))
        if key is not None and (previous is None or rank >= old_rank):
            outside[key] = row
    external_rows = []
    for key, row in outside.items():
        projected = _project(row, 'scheduler_aggregate', now)
        external_rows.append({**{name: projected.get(name) for name in
                                ('state', 'gpu_uuid', 'resources', 'waiting_seconds')},
                              '_identity': hashlib.sha256(str(key).encode()).hexdigest(),
                              'reason': _blocking_category(projected.get('reason'))})
    external_summary = _external_summary(external_rows)
    alerts = []
    if conflicts:
        alerts.append('SCHEDULER_IDENTITY_CONFLICT')
    if ledger_invalid:
        alerts.append('SCHEDULER_LEDGER_INVALID')
    if any(row.get('state') == 'INFEASIBLE' for row in attempts[-1:]):
        alerts.append('RESOURCE_INFEASIBLE')
    return {'configured_job_ids': configured, 'configured_job_state': 'historical',
            'active_job': live[-1] if live else None, 'attempts': attempts,
            'history': [row for row in attempts if row['state'] not in _LIVE],
            'alerts': alerts, 'data_gap': ledger_invalid or not bool(attempts), 'external_queue_summary': external_summary,
            '_external_jobs': external_rows}


def _external_summary(rows):
    queued = [row for row in rows if row.get('state') == 'QUEUED']
    return {'count': len(queued),
            'resources': {key: sum((row.get('resources') or {}).get(key, 0)
                                   for row in queued if _finite((row.get('resources') or {}).get(key)))
                          for key in _RESOURCE_KEYS},
            'oldest_wait_seconds': max((row.get('waiting_seconds', 0) for row in queued), default=None),
            'blocking_reasons': sorted(set(_blocking_category(row.get('reason')) for row in queued))}


def _blocking_category(reason):
    reason = str(reason or 'unknown')
    categories = ('insufficient_memory_reservation', 'insufficient_compute_units', 'insufficient_cpu_cores',
                  'insufficient_ram_mib', 'external_or_unidentified_process', 'global_concurrency_limit',
                  'waiting_for_protected_earlier_job', 'telemetry_unavailable_or_stale', 'quarantined',
                  'missing_or_capacity_mismatch', 'no_matching_gpu')
    return next((value for value in categories if value in reason), 'unknown')


def scheduler_attempts(task):
    rows = collect_scheduler(task)['attempts']
    for row in rows:
        row['stale'] = row.get('state') not in _LIVE
    return rows


def active_scheduler_job(task):
    return collect_scheduler(task)['active_job']


def _trusted(path, *, directory=False):
    if _official_ownership is not None:
        try:
            return _official_ownership._trusted(Path(path), directory=directory)
        except OSError:
            return False
    try:
        info = Path(path).lstat()
        return (info.st_uid == os.getuid() and not info.st_mode & 0o022
                and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))
    except OSError:
        return False


def _pid_ticks(pid):
    try:
        return int((Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _pid_cgroups(pid):
    try:
        lines = (Path('/proc') / str(pid) / 'cgroup').read_text().splitlines()
        return [{'controller': parts[1], 'path': parts[2]} for line in lines if len(parts := line.split(':', 2)) == 3]
    except OSError:
        return []


def _pid_matches(pid, ticks, boot):
    if _official_ownership is not None:
        try:
            return _official_ownership.processes.pid_matches(pid, ticks, boot)
        except OSError:
            return False
    if type(pid) is not int or pid <= 1 or type(ticks) is not int:
        return False
    try:
        current_boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return current_boot == boot and fields[0] not in ('Z', 'X') and int(fields[19]) == ticks
    except (OSError, ValueError, IndexError):
        return False


def collect_receipts(task, scheduler_view, *, boot_id):
    """The official container_ownership attestation contract, without writes.

    Tokens are checked in memory and omitted from returned facts.  The collector
    cannot import installed scheduler modules when streamed over SSH, so it uses
    the same receipt permissions, boot/PID and exact container-scope predicates.
    """
    root = _registered_root(task)
    if root is None or not boot_id:
        return []
    result = []
    for job in scheduler_view.get('attempts', []):
        if job.get('state') not in _LIVE:
            continue
        sessions = set(_strings((task.get('scheduler') or {}).get('session_id')) +
                       _strings(job.get('origin_session_id')) + _strings(job.get('session_id')))
        for sid in sessions:
            jid = job.get('job_id')
            if not _valid_name(sid) or not _valid_name(jid):
                continue
            directory = root / 'sessions' / sid / 'jobs' / jid
            receipt_dir = directory / 'containers'
            if not (_trusted(directory, directory=True) and _trusted(receipt_dir, directory=True)
                    and _trusted(directory / 'launch.json')):
                continue
            launch = _json(directory / 'launch.json') or {}
            if launch.get('id') != jid or not launch.get('token'):
                continue
            for path in sorted(receipt_dir.glob('*.json'))[:256]:
                if not _trusted(path):
                    continue
                row = _json(path) or {}
                try:
                    if path.stat().st_size > 16384:
                        continue
                except OSError:
                    continue
                cid = row.get('container_id')
                if (row.get('version') != 1 or row.get('job_id') != jid or row.get('token') != launch['token']
                        or row.get('boot_id') != boot_id or not isinstance(cid, str) or len(cid) != 64
                        or not _HEX_ID.fullmatch(cid) or path.name != cid + '.json'
                        or row.get('init_start_ticks') is None
                        or not _pid_matches(row.get('init_pid'), row['init_start_ticks'], boot_id)):
                    continue
                groups = [group for group in _pid_cgroups(row['init_pid'])
                          if {cid, 'docker-' + cid + '.scope'}.intersection(Path(group['path']).parts)]
                if groups and _pid_matches(row['init_pid'], row['init_start_ticks'], boot_id):
                    result.append({'container_id': cid, 'job_id': jid, 'boot_id': boot_id,
                                   'init_pid': row['init_pid'], 'init_start_ticks': row['init_start_ticks'],
                                   'cgroup_paths': groups, 'trusted': True})
    return result


def resolve_gpu_owner(app, tasks, *, boot_id=None):
    owners = {}
    receipt_jobs = {}
    pid = str(app.get('pid'))
    identity = app.get('pid_identity') or {}
    if (identity.get('boot_id') is not None and identity['boot_id'] != boot_id or identity.get('identity_changed')
            or 'pid_identity' in app and identity.get('start_ticks') is None):
        return {'owner_task_id': None, 'owner_job_id': None, 'source': 'unknown', 'confidence': 'unknown',
                'reason': 'GPU PID boot/start identity changed during collection'}
    groups = app.get('cgroup_paths') or identity.get('cgroup_paths') or []
    ids = app.get('container_ids') or identity.get('container_ids') or []
    for task in tasks:
        if not task.get('uses_gpu', True):
            continue
        for receipt in task.get('ownership_receipts', []):
            if (not receipt.get('trusted') or receipt.get('boot_id') != boot_id
                    or identity.get('start_ticks') is None):
                continue
            matched = any(group.get('controller') == scope.get('controller') and
                          (group.get('path') == scope.get('path') or
                           str(group.get('path', '')).startswith(str(scope.get('path', '')) + '/'))
                          for group in groups for scope in receipt.get('cgroup_paths', [])
                          if scope.get('path') not in ('', '/'))
            if matched:
                receipt_jobs.setdefault(task['id'], set()).add(receipt.get('job_id'))
                owners[task['id']] = {'owner_task_id': task['id'], 'owner_job_id': receipt.get('job_id'),
                                      'source': 'scheduler_receipt_cgroup', 'confidence': 'high',
                                      'reason': 'trusted receipt, boot and current init identity matched'}
        for registered in task.get('gpu_container_ids', []):
            if isinstance(registered, str) and _HEX_ID.fullmatch(registered) and any(
                    isinstance(cid, str) and (cid == registered or len(cid) == 64 and cid.startswith(registered)) for cid in ids):
                owners.setdefault(task['id'], {'owner_task_id': task['id'], 'owner_job_id': None,
                                              'source': 'registered_container_id', 'confidence': 'medium',
                                              'reason': 'unique registered container ID matches cgroup'})
        for process in task.get('processes', []):
            if not isinstance(process, dict) or str(process.get('pid')) != pid:
                continue
            if identity.get('start_ticks') is not None and process.get('start_ticks') != identity['start_ticks']:
                continue
            owners.setdefault(task['id'], {'owner_task_id': task['id'], 'owner_job_id': None,
                                          'source': 'registered_pid_identity', 'confidence': 'medium',
                                          'reason': 'same-probe PID and start identity matched'})
    if len(owners) == 1 and all(len(values) == 1 for values in receipt_jobs.values()):
        return next(iter(owners.values()))
    return {'owner_task_id': None, 'owner_job_id': None, 'source': 'unknown', 'confidence': 'unknown',
            'reason': 'conflicting ownership evidence' if owners else 'no verified receipt, container ID or PID identity'}


def enrich_host(raw, tasks, endpoint_id, now=None):
    # The monitor invokes this once on its endpoint response.  Mutating this
    # in-memory response keeps all alias task evaluations on the same facts.
    value = raw
    value['endpoint_id'] = endpoint_id
    if raw.get('error'):
        return value
    resolved = []
    raw_tasks = dict(raw.get('tasks') or {})
    for task in tasks:
        task_raw = dict(raw_tasks.get(task['id']) or {})
        if 'scheduler_view' not in task_raw:
            task_raw['scheduler_view'] = {'configured_job_ids': _strings((task.get('scheduler') or {}).get('job_ids')),
                                          'configured_job_state': 'historical', 'active_job': None,
                                          'attempts': [], 'history': [], 'alerts': [], 'data_gap': True,
                                          'gap_reason': 'remote scheduler observation unavailable'}
        resolved.append(dict(task, processes=task_raw.get('processes', []),
                             ownership_receipts=task_raw.get('ownership_receipts', [])))
        raw_tasks[task['id']] = task_raw
    rows, seen = [], set()
    for app in raw.get('gpu_processes', {}).get('rows', []):
        key = (app.get('gpu_uuid'), str(app.get('pid')), raw.get('boot_id'), (app.get('pid_identity') or {}).get('start_ticks'))
        if key in seen:
            continue
        seen.add(key)
        owner = resolve_gpu_owner(app, resolved, boot_id=raw.get('boot_id'))
        rows.append(dict(app, **owner, ownership=owner))
    value['tasks'] = raw_tasks
    value['gpu_processes'] = dict(raw.get('gpu_processes') or {}, rows=rows)
    jobs = {}
    external = {}
    for source in raw_tasks.values():
        view = source.get('scheduler_view') or {}
        for job in view.get('attempts', []):
            jobs[job.get('request_id') or job.get('job_id')] = job
        for job in view.get('_external_jobs', []):
            external[job.get('_identity')] = job
    value['scheduler_summary'] = {'running': sum(j.get('state') in {'STARTING', 'RUNNING', 'CANCELLING', 'UNKNOWN'} for j in jobs.values()),
                                  'queued': sum(j.get('state') == 'QUEUED' for j in jobs.values()),
                                  'oldest_wait_seconds': max((j.get('waiting_seconds', 0) for j in jobs.values() if j.get('state') == 'QUEUED'), default=None)}
    own_identity = {hashlib.sha256(str(key).encode()).hexdigest() for key in jobs}
    outside = [job for key, job in external.items() if key not in own_identity]
    value['external_queue_summary'] = _external_summary(outside)
    value['reserved'] = {}
    for job in list(jobs.values()) + outside:
        gpu = job.get('gpu_uuid')
        if gpu and job.get('state') in {'STARTING', 'RUNNING', 'CANCELLING', 'UNKNOWN'}:
            resources = value['reserved'].setdefault(gpu, {key: 0 for key in _RESOURCE_KEYS})
            for key in _RESOURCE_KEYS:
                if _finite((job.get('resources') or {}).get(key)):
                    resources[key] += job['resources'][key]
    return value
