"""Read-only Linux collector, executed locally or streamed to python3 over SSH.

Only the standard library is required. Never imports training code or writes remotely.
"""
import base64
import csv
import io
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import time
from typing import Any

try:
    from .runtime import collect_scheduler, collect_receipts
except (ImportError, ValueError):
    try:
        from runtime import collect_scheduler, collect_receipts
    except ImportError:
        # SSH execution streams runtime.py into the same globals first.
        pass

DEFAULT_TAIL_BYTES = 64 * 1024
MAX_METADATA_BYTES = 256 * 1024
GPU_QUERY_TIMEOUT_SECONDS = 8


def field(value: Any, key: str, default: Any = None) -> Any:
    for part in key.split('.'):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def read_file(root: Path, name: str, limit: int, tail: bool = False) -> dict:
    p = (root / name).resolve()
    if not p.is_relative_to(root):
        return {'error': 'path escapes task root'}
    try:
        # Nonblocking open also prevents a misconfigured FIFO from blocking the probe.
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return {'error': 'not a regular file'}
            if not tail and st.st_size > limit:
                return {'error': 'metadata exceeds byte limit', 'size': st.st_size}
            offset = max(0, st.st_size - limit) if tail else 0
            at_boundary = True
            if offset:
                f.seek(offset - 1)
                at_boundary = f.read(1) == b'\n'
            f.seek(offset)
            data = f.read(limit)
            read_end = offset + len(data)
        if offset and not at_boundary and b'\n' in data:
            # The first record may begin before the bounded tail.  Discard it
            # and report the exact byte offset of the first complete record so
            # callers can safely resume incremental parsing.
            skipped, data = data.split(b'\n', 1)
            offset += len(skipped) + 1
        elif offset and not at_boundary:
            data = b''
            offset = read_end
        entry = {'text': data.decode('utf-8', errors='replace'), 'size': st.st_size,
                 'mtime': st.st_mtime, 'mtime_ns': st.st_mtime_ns,
                 'inode': st.st_ino, 'device': st.st_dev, 'offset': offset,
                 'read_end': read_end, 'truncated': bool(offset)}
        if tail:
            # Preserve exact bytes when an append splits a UTF-8 code point.
            entry['raw_b64'] = base64.b64encode(data).decode('ascii')
        return entry
    except FileNotFoundError:
        return {'missing': True}
    except OSError as e:
        return {'error': f'{type(e).__name__}: {e}'}


def process_table() -> tuple[list[dict[str, Any]], int]:
    rows = []
    errors = 0
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != os.geteuid():
                continue
            args = (p / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
            if not args:
                continue
            fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
            try:
                cwd = os.readlink(p / 'cwd')
            except PermissionError:
                cwd = None
            rows.append({'pid': int(p.name), 'ppid': int(fields[1]), 'pgid': int(fields[2]),
                         'state': fields[0], 'start_ticks': int(fields[19]),
                         'cwd': cwd, '_command': args})
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            errors += 1
        except OSError:
            errors += 1
    return rows, errors


def _process_identity(pid: str | int) -> dict[str, Any]:
    """Read stable identity and cgroup metadata for one GPU process.

    The monitor never treats a PID as an owner by itself: ``boot_id`` and
    ``start_ticks`` let consumers reject PID reuse.  Cgroup paths are retained
    as evidence so containerd-shim descendants can be matched without walking
    private task directories.
    """
    value = str(pid)
    result: dict[str, Any] = {'pid': value, 'cgroup_paths': [], 'container_ids': [],
                              'runtime': None, 'memory': {}}
    proc = Path('/proc') / value
    try:
        result['command_summary'] = (proc / 'comm').read_text().strip()[:80]
    except OSError:
        result['command_summary'] = None
    try:
        stat_fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        result['start_ticks'] = int(stat_fields[19])
    except (OSError, ValueError, IndexError):
        result['start_ticks'] = None
    try:
        result['cgroup_text'] = (proc / 'cgroup').read_text()
        paths = []
        for line in result['cgroup_text'].splitlines():
            fields = line.split(':', 2)
            if len(fields) == 3:
                controller, path = fields[1], fields[2]
                paths.append({'controller': controller, 'path': path})
        result['cgroup_paths'] = paths
        result['cgroup_ancestors'] = sorted({str(ancestor) for item in paths
                                           for ancestor in Path(item['path']).parents})
        # Keep both full and short IDs.  A short ID is evidence only when it is
        # unambiguous in the caller's registered receipts.
        text = result['cgroup_text']
        result['container_ids'] = sorted(set(re.findall(
            r'(?<![a-f0-9])([a-f0-9]{12,64})(?![a-f0-9])', text)))
        lowered = text.lower()
        if 'containerd' in lowered:
            result['runtime'] = 'containerd'
        elif 'docker' in lowered:
            result['runtime'] = 'docker'
        elif 'kubepods' in lowered:
            result['runtime'] = 'kubernetes'
    except OSError as exc:
        result['cgroup_error'] = f'{type(exc).__name__}: {exc}'
    # cgroup v2 exposes memory files below /sys/fs/cgroup.  Do not search the
    # filesystem; only inspect exact paths reported by /proc.
    memory_candidates = []
    candidate_paths = []
    for item in result.get('cgroup_paths', []):
        group_path = Path(item['path'])
        candidate_paths.extend([str(group_path), *[str(parent) for parent in group_path.parents if str(parent) != '/']])
    for path in dict.fromkeys(candidate_paths):
        rel = path.lstrip('/')
        root = Path('/sys/fs/cgroup') / rel
        if not root.resolve().is_relative_to(Path('/sys/fs/cgroup').resolve()):
            continue
        try:
            max_value = (root / 'memory.max').read_text().strip()
            current = (root / 'memory.current').read_text().strip()
            events_path = root / 'memory.events'
            events = {}
            if events_path.exists():
                for line in events_path.read_text().splitlines():
                    key, _, value = line.partition(' ')
                    if key and value.isdigit():
                        events[key] = int(value)
            memory_candidates.append({'max': None if max_value == 'max' else int(max_value),
                                      'current': int(current), 'events': events, 'path': str(root)})
        except (OSError, ValueError):
            continue
    if memory_candidates:
        limited = [item for item in memory_candidates if item['max'] is not None]
        result['memory'] = min(limited, key=lambda item: item['max']) if limited else memory_candidates[0]
        result['memory_ancestors'] = memory_candidates
    try:
        current_ticks = int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19])
        result['identity_changed'] = current_ticks != result.get('start_ticks')
    except (OSError, ValueError, IndexError):
        result['identity_changed'] = True
    return result


def _host_resources() -> dict[str, Any]:
    values = {}
    try:
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, text = line.partition(':')
            if key in ('MemTotal', 'MemAvailable'):
                memory[key] = int(text.split()[0]) * 1024
        values.update(ram_total_bytes=memory.get('MemTotal'), ram_available_bytes=memory.get('MemAvailable'))
    except (OSError, ValueError, IndexError):
        values.update(ram_total_bytes=None, ram_available_bytes=None)
    values['cpu_count'] = os.cpu_count()
    try:
        values['load_average'] = list(os.getloadavg())
    except OSError:
        values['load_average'] = None
    return values


def gpu_query(query: str, names: list[str]) -> dict[str, Any]:
    try:
        p = subprocess.run(['nvidia-smi', query, '--format=csv,noheader,nounits'],
                           text=True, capture_output=True, timeout=GPU_QUERY_TIMEOUT_SECONDS)
        if p.returncode:
            return {'error': p.stderr.strip()[:500] or p.stdout.strip()[:500]}
        return {'rows': [dict(zip(names, [v.strip() for v in row]))
                         for row in csv.reader(io.StringIO(p.stdout)) if row]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'error': type(e).__name__ + ': ' + str(e)}


def collect(config: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        boot_id = None
    processes, proc_errors = process_table()
    gpu = gpu_query('--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu',
                    ['index', 'uuid', 'name', 'memory_total_mib', 'memory_used_mib', 'utilization_pct', 'temperature_c'])
    apps = gpu_query('--query-compute-apps=gpu_uuid,pid,used_memory', ['gpu_uuid', 'pid', 'memory_used_mib'])
    # Container training runs under candidate UIDs and is not a descendant of
    # the host docker exec client. Read only its cgroup, never its arguments.
    for app in apps.get('rows', []):
        identity = _process_identity(app.get('pid'))
        identity['boot_id'] = boot_id
        app.update({'pid_identity': identity, 'cgroup_paths': identity.get('cgroup_paths', []),
                    'runtime': identity.get('runtime'), 'memory': identity.get('memory', {})})
        try:
            app['container_ids'] = identity.get('container_ids', [])
        except (KeyError, TypeError):
            app['container_ids'] = []
    tasks = {}
    read_cache = {}
    def cached_task_read(root, name, limit, tail=False):
        key = (str(root), name, limit, tail)
        if key not in read_cache:
            read_cache[key] = read_file(root, name, limit, tail=tail)
        return read_cache[key]
    for task in config['tasks']:
        root = Path(task['root']).resolve()
        names = set()
        for key in ('status', 'exit', 'launch', 'deadline_file'):
            if task.get(key):
                names.add(task[key]['path'])
        for selector in task.get('processes', []):
            if selector.get('pid_file'):
                names.add(selector['pid_file'])
        files = {name: cached_task_read(root, name, config.get('metadata_bytes', MAX_METADATA_BYTES)) for name in names}
        if task.get('controller') and (task.get('status') or {}).get('path'):
            event_name = str(Path(task['status']['path']).with_name('events.jsonl'))
            files[event_name] = cached_task_read(root, event_name, config.get('tail_bytes', DEFAULT_TAIL_BYTES), tail=True)
        for stream in task.get('streams', []):
            files[stream['path']] = cached_task_read(root, stream['path'], config.get('tail_bytes', DEFAULT_TAIL_BYTES), tail=True)
        matched = {}
        identities = []
        process_errors = []
        for selector in task.get('processes', []):
            expected = selector.get('pid')
            if selector.get('pid_file'):
                try:
                    raw = files[selector['pid_file']]['text']
                    expected = int(field(json.loads(raw), selector['pid_key'])) if selector.get('pid_key') else int(raw.strip())
                except (KeyError, ValueError, TypeError):
                    expected = None
                if expected is None:
                    continue
            candidates = [p for p in processes if expected is None or p['pid'] == expected]
            for p in candidates:
                valid = all(token in p['_command'] for token in selector['contains'])
                if valid and selector.get('cwd') and p['cwd'] is None:
                    process_errors.append({'pid': p['pid'], 'error': 'cannot read matching process cwd'})
                    continue
                valid = valid and (not selector.get('cwd') or p['cwd'] == str((root / selector['cwd']).resolve()))
                if selector.get('start_ticks') is not None:
                    valid = valid and p['start_ticks'] == selector['start_ticks']
                if valid and p['state'] != 'Z':
                    matched[p['pid']] = p
                elif expected == p['pid']:
                    identities.append({'pid': p['pid'], 'error': 'process identity mismatch'})
        # Descendants are included so an exited controller does not hide live training,
        # provided a training selector is also registered for already orphaned children.
        while True:
            children = {p['pid']: p for p in processes if p['ppid'] in matched and p['state'] != 'Z'}
            if children.keys() <= matched.keys():
                break
            matched.update(children)
        try:
            fs = os.statvfs(root)
            disk = fs.f_bavail * fs.f_frsize
        except OSError:
            disk = None
        tasks[task['id']] = {'files': files, 'processes': [{k: v for k, v in p.items() if not k.startswith('_')}
                                                         for p in matched.values()],
                             'identity_errors': identities, 'disk_free_bytes': disk}
        tasks[task['id']]['process_errors'] = process_errors
        if 'collect_scheduler' in globals():
            view = collect_scheduler(task, tasks[task['id']], now=now)
            tasks[task['id']]['scheduler_view'] = view
            tasks[task['id']]['ownership_receipts'] = collect_receipts(task, view, boot_id=boot_id)
    return {'observed_at': now, 'collected_at': time.time(), 'boot_id': boot_id, 'proc_permission_errors': proc_errors,
            'gpu': gpu, 'gpu_processes': apps, 'tasks': tasks, 'host_resources': _host_resources()}
