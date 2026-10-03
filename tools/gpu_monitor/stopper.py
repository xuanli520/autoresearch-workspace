"""Explicit, scoped stop request. This module is never called by polling."""
import datetime
import json
import os
from pathlib import Path
import signal
import time


def identity(pid):
    proc = Path('/proc') / str(pid)
    try:
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'pgid': int(fields[2]), 'start_ticks': int(fields[19]),
                'cwd': os.readlink(proc / 'cwd'),
                'command': (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')}
    except (FileNotFoundError, ProcessLookupError):
        return None


def request_stop(task, reason, dry_run):
    spec = task.get('stop')
    if not spec:
        raise ValueError('task has no custom stop contract; use the official controller')
    raise ValueError('custom stop contracts are unsupported; use the official controller')

    root = Path(task['root']).resolve()
    receipt = {'task': task['id'], 'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'reason': reason, 'dry_run': dry_run, 'mode': spec['mode'], 'training_stopped_confirmed': False}
    checked = []
    for target in spec['targets']:
        if target.get('pid_file'):
            path = (root / target['pid_file']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('pid file escapes root')
            if not path.exists():
                continue
            value = json.loads(path.read_text())
            for key in target.get('pid_key', 'pid').split('.'):
                value = value[key]
            pid = int(value)
        else:
            pid = int(target['pid'])
        if pid <= 1:
            raise ValueError('invalid target PID')
        p = identity(pid)
        if p is None:
            continue
        if (p['pgid'] != pid or p['cwd'] != str((root / target['cwd']).resolve())
                or not all(token in p['command'] for token in target['contains'])
                or (target.get('start_ticks') is not None and p['start_ticks'] != target['start_ticks'])):
            raise ValueError(f'PID {pid}: identity/cwd/process-group mismatch; no stop sent')
        # Reject mixed ownership before sending any signals to a group.
        for child in Path('/proc').iterdir():
            if not child.name.isdigit():
                continue
            try:
                fields = (child / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == pid and child.stat().st_uid != os.geteuid():
                    raise ValueError('process group contains a different owner')
            except (FileNotFoundError, ProcessLookupError):
                continue
        checked.append(p)
    receipt['groups'] = [{k: v for k, v in p.items() if k != 'command'} for p in checked]
    if not dry_run:
        for p in checked:
            current = identity(p['pid'])
            if current is None:
                continue
            if current != p:
                raise ValueError('process identity changed immediately before signal; stop aborted')
            # Continue a paused group so it can process TERM. No unbounded pkill or KILL.
            os.killpg(p['pgid'], signal.SIGCONT)
            os.killpg(p['pgid'], signal.SIGTERM)
    receipt['result'] = ('WOULD_SEND_TERM' if dry_run else 'TERM_SENT') if checked else 'NO_MATCHING_LIVE_GROUP'
    return receipt
