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
    processes, proc_errors = process_table()
    gpu = gpu_query('--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu',
                    ['index', 'uuid', 'name', 'memory_total_mib', 'memory_used_mib', 'utilization_pct', 'temperature_c'])
    apps = gpu_query('--query-compute-apps=gpu_uuid,pid,used_memory', ['gpu_uuid', 'pid', 'memory_used_mib'])
    # Container training runs under candidate UIDs and is not a descendant of
    # the host docker exec client. Read only its cgroup, never its arguments.
    for app in apps.get('rows', []):
        try:
            text = (Path('/proc') / app['pid'] / 'cgroup').read_text()
            app['container_ids'] = re.findall(r'(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])', text)
        except OSError:
            app['container_ids'] = []
    tasks = {}
    for task in config['tasks']:
        root = Path(task['root']).resolve()
        names = set()
        for key in ('status', 'exit', 'launch', 'deadline_file'):
            if task.get(key):
                names.add(task[key]['path'])
        for selector in task.get('processes', []):
            if selector.get('pid_file'):
                names.add(selector['pid_file'])
        files = {name: read_file(root, name, config.get('metadata_bytes', MAX_METADATA_BYTES)) for name in names}
        for stream in task.get('streams', []):
            files[stream['path']] = read_file(root, stream['path'], config.get('tail_bytes', DEFAULT_TAIL_BYTES), tail=True)
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
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        boot_id = None
    return {'observed_at': now, 'boot_id': boot_id, 'proc_permission_errors': proc_errors,
            'gpu': gpu, 'gpu_processes': apps, 'tasks': tasks}
