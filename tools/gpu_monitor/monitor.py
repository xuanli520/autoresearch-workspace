#!/usr/bin/env python3
"""AutoResearch GPU task monitor: read-only collection, local alerts and handoff."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid

try:
    from .probe import field
except ImportError:  # Preserve direct script execution.
    from probe import field

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / 'tasks.json'

# 唯一认证来源：工作区根目录的 auth.txt 结构化凭据文件。其余认证方式（配置内
# password、内嵌密码表、password_env 环境变量、identity_file 密钥、SSH config 别名）
# 已全部移除。更换 auth.txt 中的 IP/用户名/密码/端口后，下次启动即自动生效。
DEFAULT_AUTH = Path(os.environ.get('AUTORESEARCH_AUTH_FILE') or (HERE.parent.parent / 'auth.txt'))

# 凭据文件用中文标签，归一化（去空白、转小写）后映射到内部字段名。
AUTH_LABELS = {
    'ip': 'host', 'ip地址': 'host', '地址': 'host', '主机': 'host', 'hostname': 'host',
    'user': 'user', 'username': 'user', '用户名': 'user', '用户': 'user',
    'password': 'password', 'passwd': 'password', '密码': 'password',
    'port': 'port', '端口': 'port', '登录端口': 'port',
}
AUTH_REQUIRED = ('host', 'user', 'password')

TERMINAL = {'COMPLETED', 'FAILED', 'STOPPED'}
SUGGESTIONS = {
    'UNREACHABLE': '检查 SSH 网络和交互认证；远端训练是否存活未知。',
    'EXITED_WITHOUT_RESULT': '进程已不见但无可靠终态；检查退出码、OOM、控制器日志。',
    'STATUS_CONFLICT': '终态仍有所属进程存活；核对控制器及子任务，勿直接重启。',
    'STALE_LOG': '日志长时间未更新；核对是否在评估、保存、等待资源或阻塞。',
    'STALLED_PROGRESS': '步数长期无推进；核对训练阶段、数据加载和日志频率。',
    'NONFINITE': '出现 NaN/Inf；核对数值稳定性、输入和损失。',
    'ERROR_LOG': '日志包含错误特征；查看日志上下文与退出码。',
    'DEADLINE_EXCEEDED': '预算已到期且任务未确认结束；核对原任务 watchdog。',
    'EFFECTIVE_TARGET_NOT_REACHED': '已退出但有效研究未达目标；不能以墙钟、等待或容器存活时间补足。',
    'EFFECTIVE_LIMIT_EXCEEDED': '有效研究记录超过硬上限；检查控制器、独立 guard 与逐轮计时证据。',
    'ETA_OVER_BUDGET': '按近期吞吐估计训练无法在预算内完成；另计评估和重载时间。',
    'IDENTITY_MISMATCH': 'PID 对应进程身份不符；核对 launch 文件，勿对该 PID 操作。',
    'GPU_UNAVAILABLE': 'GPU 查询失败；不能由此推断训练退出。',
    'LOW_DISK': '输出盘空间不足；核对 checkpoint 和证据回收计划。',
    'FAILED': '任务有失败终态或非零退出码；保留失败证据后分析。',
    'OBSERVATION_ERROR': '部分文件或进程不可读；核对权限、路径和文件格式。',
}


def utc(value=None):
    return dt.datetime.fromtimestamp(time.time() if value is None else value, dt.timezone.utc).isoformat()


def timestamp(value):
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamps must include timezone')
    return parsed.timestamp()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def dump(value):
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False)


def _split_label(line):
    label, sep, value = line.partition('：')
    if not sep:
        label, sep, value = line.partition(':')
    if not sep:
        return None, value
    return re.sub(r'\s+', '', label).lower(), value


def load_auth(path):
    """Parse the labelled credential file; the only source of SSH auth material.

    Accepts ``标签：值`` on one line or the label alone followed by the value on the
    next line, and tolerates full-width or half-width colons plus spaces inside
    labels (for example ``密 码``).
    """
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as e:
        raise ValueError(f'无法读取凭据文件 {path}: {e}')
    fields = {}
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if pending:
            # A value on its own line is taken verbatim so colons inside a
            # password cannot be mistaken for a label separator.
            fields.setdefault(pending, line)
            pending = None
            continue
        label, value = _split_label(line)
        name = AUTH_LABELS.get(label)
        if name is None:
            continue
        value = value.strip()
        if value:
            fields[name] = value
        else:
            pending = name
    missing = [name for name in AUTH_REQUIRED if not fields.get(name)]
    if missing:
        raise ValueError(f'凭据文件 {path} 缺少字段: ' + '、'.join(missing)
                         + '（需要 IP地址/用户名/密码，可选 登录端口）')
    try:
        port = int(fields.get('port') or 22)
    except ValueError:
        raise ValueError(f'凭据文件 {path} 的登录端口不是数字: {fields.get("port")}')
    if not 1 <= port <= 65535:
        raise ValueError(f'凭据文件 {path} 的登录端口超出范围: {port}')
    host = fields['host']
    if not re.fullmatch(r'[A-Za-z0-9_.:%\[\]-]+', host) or host.startswith('-'):
        raise ValueError(f'凭据文件 {path} 的 IP地址无效: {host}')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', fields['user']):
        raise ValueError(f'凭据文件 {path} 的用户名无效: {fields["user"]}')
    return {'host': host, 'user': fields['user'], 'password': fields['password'], 'port': port}


def apply_auth(host, auth):
    """Overlay the credential file onto an SSH host; auth.txt is authoritative."""
    host = dict(host)
    host.update(hostname=auth['host'], user=auth['user'], port=auth['port'], password=auth['password'])
    for stale in ('target', 'password_env', 'identity_file'):
        host.pop(stale, None)
    return host


def load_config(path, auth=None):
    cfg = json.loads(path.read_text())
    if cfg.get('version') != 1 or not isinstance(cfg.get('tasks'), list) or not isinstance(cfg.get('hosts'), dict):
        raise ValueError('config requires version=1, hosts object and tasks array')
    cfg.setdefault('interval_seconds', 60)
    cfg.setdefault('timeout_seconds', 30)
    cfg.setdefault('connection_attempts', 2)
    cfg.setdefault('retry_delay_seconds', 2)
    cfg.setdefault('max_hours', 24)
    cfg.setdefault('state_dir', '.state')
    for key in ('interval_seconds', 'timeout_seconds', 'max_hours', 'connection_attempts'):
        if not finite(cfg[key]) or cfg[key] <= 0:
            raise ValueError(f'{key} must be positive')
    if int(cfg['connection_attempts']) != cfg['connection_attempts']:
        raise ValueError('connection_attempts must be an integer')
    if not finite(cfg['retry_delay_seconds']) or cfg['retry_delay_seconds'] < 0:
        raise ValueError('retry_delay_seconds must be nonnegative')
    if cfg['interval_seconds'] < 1:
        raise ValueError('interval_seconds must be >= 1')
    for key, default, upper in [('tail_bytes', 65536, 1048576), ('metadata_bytes', 262144, 1048576)]:
        cfg.setdefault(key, default)
        if not isinstance(cfg[key], int) or not 256 <= cfg[key] <= upper:
            raise ValueError(f'{key} must be an integer in [256, {upper}]')
    ids = set()
    for name, host in cfg['hosts'].items():
        transport = host.get('transport', 'ssh')
        if transport not in ('ssh', 'local'):
            raise ValueError(f'{name}: transport must be ssh or local')
        if transport == 'ssh':
            if auth is not None:
                host = cfg['hosts'][name] = apply_auth(host, auth)
            if host.get('connect_timeout_seconds') is not None and (not isinstance(host['connect_timeout_seconds'], int) or host['connect_timeout_seconds'] < 1):
                raise ValueError(f'{name}: connect_timeout_seconds must be a positive integer')
            if not isinstance(host.get('options', []), list):
                raise ValueError('SSH options must be an argv list')
    for task in cfg['tasks']:
        if not re.fullmatch(r'[\w.-]+', task['id']) or task['id'] in ids:
            raise ValueError('task IDs must be unique simple names')
        ids.add(task['id'])
        if task['host'] not in cfg['hosts'] or not Path(task['root']).is_absolute():
            raise ValueError(f"{task['id']}: unknown host or nonabsolute root")
        paths = [task[k]['path'] for k in ('status', 'exit', 'launch', 'deadline_file') if task.get(k)]
        for selector in task.get('processes', []):
            if not selector.get('contains') or not all(isinstance(s, str) and s for s in selector['contains']):
                raise ValueError('process selectors require nonempty contains tokens')
            if selector.get('pid_file'):
                paths.append(selector['pid_file'])
        stop = task.get('stop')
        if stop:
            if stop['mode'] == 'marker':
                paths.append(stop['path'])
            elif stop['mode'] == 'process_groups':
                if not stop.get('targets'):
                    raise ValueError('stop targets cannot be empty')
                for target in stop['targets']:
                    if not target.get('contains') or not target.get('cwd'):
                        raise ValueError('stop targets require contains and cwd')
                    if target.get('pid_file'):
                        paths.append(target['pid_file'])
                    elif not isinstance(target.get('pid'), int) or target['pid'] <= 1:
                        raise ValueError('stop target requires a valid PID or PID file')
            else:
                raise ValueError('stop mode must be marker or process_groups')
        stream_ids = set()
        for stream in task.get('streams', []):
            if stream['id'] in stream_ids:
                raise ValueError('stream IDs must be unique per task')
            stream_ids.add(stream['id'])
            paths.append(stream['path'])
            if stream.get('format', 'jsonl') not in ('jsonl', 'regex', 'text'):
                raise ValueError('stream format must be jsonl, regex or text')
            if stream.get('format') == 'regex':
                re.compile(stream['pattern'])
            if stream.get('complete_pattern'):
                re.compile(stream['complete_pattern'])
        if any(Path(p).is_absolute() or '..' in Path(p).parts for p in paths):
            raise ValueError('file paths must stay relative to task root')
        if task.get('deadline_at'):
            timestamp(task['deadline_at'])
    return cfg


KNOWN_HOSTS = HERE / '.state' / 'known_hosts'
HOST_KEY_ERRORS = ('host key verification failed', 'remote host identification has changed',
                   'no matching host key type found')


def known_hosts_options():
    """Pin verification to a managed known_hosts so host rotation cannot break startup."""
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    return ['-o', 'StrictHostKeyChecking=accept-new',
            '-o', f'UserKnownHostsFile={KNOWN_HOSTS}',
            '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'HashKnownHosts=no']


def purge_host_key(host):
    """Drop any stored key for the target so a reused cloud IP re-verifies cleanly."""
    name = host.get('hostname')
    if not name:
        return
    port = host.get('port')
    spec = f'[{name}]:{port}' if port and port != 22 else name
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_HOSTS.touch(exist_ok=True)
    try:
        subprocess.run(['ssh-keygen', '-R', spec, '-f', str(KNOWN_HOSTS)],
                       capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # ssh-keygen -R backs the previous file up; keep the managed dir tidy.
    KNOWN_HOSTS.with_name(KNOWN_HOSTS.name + '.old').unlink(missing_ok=True)


def prepare_ssh(cfg):
    """Refresh host keys for every SSH host before a run; credentials come from auth.txt."""
    for host in cfg.get('hosts', {}).values():
        if host.get('transport', 'ssh') == 'ssh':
            purge_host_key(host)


def ssh_command(host, remote_command, password_auth=False):
    """Build a non-interactive SSH command without reusing potentially stale mux sockets."""
    target = f"{host['user']}@{host['hostname']}" if host.get('user') else host['hostname']
    command = ['ssh', '-o', f"BatchMode={'no' if password_auth else 'yes'}",
               '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
               '-o', f"ConnectTimeout={host.get('connect_timeout_seconds', 10)}",
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2']
    command += known_hosts_options()
    if password_auth:
        # 密钥认证已移除：只允许密码／键盘交互，不尝试 publickey。
        command.extend(['-o', 'PreferredAuthentications=keyboard-interactive,password',
                        '-o', 'PubkeyAuthentication=no',
                        '-o', 'NumberOfPasswordPrompts=1'])
    if host.get('port') is not None:
        command.extend(['-p', str(host['port'])])
    command.extend(host.get('options', []))
    command.extend([target, remote_command])
    return command


def run_ssh(host, command, code, timeout):
    """Run SSH with the askpass password resolved from auth.txt.

    OpenSSH execs the askpass helper after closefrom(), so an inherited descriptor
    cannot deliver the secret; the helper re-opens a FIFO by the path given in its
    environment. The secret stays out of argv, the inherited environment and any
    regular file.
    """
    password = host.get('password')
    if not password:
        return subprocess.run(command, input=code, text=True, capture_output=True, timeout=timeout)

    env = os.environ.copy()
    secrets_dir = tempfile.mkdtemp(prefix='autoresearch-askpass-')
    fifo_path = os.path.join(secrets_dir, 'password.fifo')
    os.mkfifo(fifo_path, 0o600)
    # Hold the FIFO open read-write: the write never blocks on a reader and the
    # buffered secret survives until the askpass helper consumes it.
    fifo_fd = os.open(fifo_path, os.O_RDWR)
    try:
        os.write(fifo_fd, password.encode() + b'\n')
        env.update({'AUTORESEARCH_SSH_PASSWORD_FIFO': fifo_path,
                    'SSH_ASKPASS': str(HERE / 'askpass.py'),
                    'SSH_ASKPASS_REQUIRE': 'force',
                    'DISPLAY': env.get('DISPLAY') or ':autoresearch-gpu-monitor'})
        return subprocess.run(command, input=code, text=True, capture_output=True, timeout=timeout,
                              env=env)
    finally:
        os.close(fifo_fd)
        shutil.rmtree(secrets_dir, ignore_errors=True)


def ssh_error_hint(stderr, host):
    """Add actionable, secret-free context to common SSH failures."""
    message = stderr.lower()
    if 'ssh_askpass:' in message:
        return ('SSH askpass helper 启动失败；SSH_ASKPASS 必须是可执行文件路径，'
                '不能是“解释器 路径”形式。请检查 tools/gpu_monitor/askpass.py 的路径和执行权限。')
    if 'permission denied' in message:
        return ('SSH 认证被服务端拒绝；确认 auth.txt 中的用户名与密码仍有效，'
                '且远端允许密码/键盘交互登录。不会重试确定性的认证失败。')
    if any(token in message for token in HOST_KEY_ERRORS):
        return ('SSH 主机密钥校验失败；脚本会在每次启动时清理托管 known_hosts，'
                '请确认 auth.txt 指向正确主机。')
    return ''


def probe_host(host, tasks, cfg):
    request = {'tasks': tasks, 'tail_bytes': cfg['tail_bytes'], 'metadata_bytes': cfg['metadata_bytes']}
    # Encode JSON as a Python literal, never interpolate task data into shell commands.
    code = (HERE / 'probe.py').read_text() + '\nprint(json.dumps(collect(json.loads(' + repr(json.dumps(request)) + '))))\n'
    if host.get('transport', 'ssh') == 'local':
        command = [sys.executable, '-']
    else:
        command = ssh_command(host, shlex.join([host.get('python', 'python3'), '-']),
                              password_auth=bool(host.get('password')))
    try:
        attempts = int(cfg.get('connection_attempts', 1)) if host.get('transport', 'ssh') == 'ssh' else 1
        for attempt in range(attempts):
            try:
                result = (subprocess.run(command, input=code, text=True, capture_output=True,
                                         timeout=cfg['timeout_seconds']) if host.get('transport', 'ssh') == 'local'
                          else run_ssh(host, command, code, cfg['timeout_seconds']))
                if result.returncode:
                    error = f'probe exit {result.returncode}: {result.stderr.strip()[-1000:]}'
                    if any(token in result.stderr.lower() for token in HOST_KEY_ERRORS):
                        # A rotated cloud IP can present a new host key; drop the stale
                        # record and retry so credential swaps stay seamless.
                        purge_host_key(host)
                        if attempt + 1 == attempts:
                            return {'error': f'{error} {ssh_error_hint(result.stderr, host)}'.strip()}
                        continue
                    # Authentication/configuration errors are deterministic; don't repeat them.
                    if any(token in result.stderr.lower() for token in (
                            'permission denied', 'bad configuration option', 'ssh_askpass:')):
                        hint = ssh_error_hint(result.stderr, host)
                        return {'error': f'{error} {hint}'.strip()}
                    if attempt + 1 == attempts:
                        return {'error': error}
                else:
                    data = json.loads(result.stdout)
                    if not isinstance(data, dict) or 'tasks' not in data or 'observed_at' not in data:
                        raise ValueError('invalid probe response')
                    return data
            except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                error = f'{type(e).__name__}: {e}'
                if attempt + 1 == attempts:
                    return {'error': error}
            time.sleep(cfg.get('retry_delay_seconds', 0))
        return {'error': error}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {'error': f'{type(e).__name__}: {e}'}


def stop_task(cfg, task, reason, dry_run, state):
    host = cfg['hosts'][task['host']]
    code = (HERE / 'stopper.py').read_text() + '\nprint(json.dumps(request_stop(json.loads(' + repr(json.dumps(task)) + '), ' + repr(reason) + ', ' + repr(dry_run) + ')))\n'
    if host.get('transport', 'ssh') == 'local':
        command = [sys.executable, '-']
    else:
        command = ssh_command(host, shlex.join([host.get('python', 'python3'), '-']),
                              password_auth=bool(host.get('password')))
    state.mkdir(parents=True, exist_ok=True)
    failed = False
    try:
        result = (subprocess.run(command, input=code, text=True, capture_output=True, timeout=cfg['timeout_seconds'])
                  if host.get('transport', 'ssh') == 'local'
                  else run_ssh(host, command, code, cfg['timeout_seconds']))
        if result.returncode:
            failed = True
            receipt = {'task': task['id'], 'time': utc(), 'result': 'STOP_COMMAND_ERROR',
                       'dry_run': dry_run, 'error': result.stderr[-1500:]}
        else:
            receipt = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        failed = True
        receipt = {'task': task['id'], 'time': utc(), 'result': 'STOP_OUTCOME_UNKNOWN',
                   'dry_run': dry_run, 'error': str(e), 'advice': '先查询真实状态，不能假定停止已执行或未执行。'}
    with (state / 'stop-requests.jsonl').open('a') as f:
        f.write(dump(receipt) + '\n')
    print(dump(receipt))
    return 1 if failed else 0


def metadata(raw, spec):
    if not spec:
        return None
    entry = raw['files'].get(spec['path'], {})
    try:
        value = json.loads(entry['text'])
        return field(value, spec['key']) if spec.get('key') else value
    except (KeyError, ValueError):
        return None


def parse_stream(spec, entry, old, now, alive, default_stale):
    text = entry.get('text', '')
    rows = []
    errors = []
    pattern = re.compile(spec['pattern']) if spec.get('format') == 'regex' else None
    fmt = spec.get('format', 'jsonl')
    # Ignore an incomplete final JSONL record, but still scan it for error signals.
    for line in text.splitlines():
        try:
            if fmt == 'jsonl':
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                if any(field(row, k) != v for k, v in spec.get('where', {}).items()):
                    continue
                row = {k: field(row, v) for k, v in spec.get('fields', {'step': 'step', 'elapsed_seconds': 'elapsed_seconds'}).items()}
            elif pattern:
                match = pattern.search(line)
                if not match:
                    continue
                row = {k: float(v) for k, v in match.groupdict().items() if v is not None}
            else:
                continue
            if any(v is not None for v in row.values()):
                rows.append(row)
        except (ValueError, TypeError):
            continue
    latest = rows[-1] if rows else {}
    metric = latest.get('metric')
    values = [r['metric'] for r in rows if finite(r.get('metric'))]
    step = latest.get('step')
    previous_step = old.get('latest', {}).get('step')
    same_file = entry.get('inode') == old.get('inode') and (entry.get('size') or 0) >= (old.get('size') or 0)
    changed = not same_file or step != previous_step
    progress_since = now if changed else old.get('progress_since', now)
    rate = None
    if finite(step):
        for row in reversed(rows[:-1]):
            elapsed, prior = latest.get('elapsed_seconds'), row.get('elapsed_seconds')
            if finite(row.get('step')) and finite(elapsed) and finite(prior) and elapsed > prior and step > row['step']:
                rate = (step - row['step']) / (elapsed - prior)
                break
        if rate is None and same_file and finite(previous_step) and step > previous_step:
            seconds = now - old.get('observed_at', now)
            if seconds > 0:
                rate = (step - previous_step) / seconds
    total = spec.get('total_steps')
    eta = max(0, total - step) / rate if finite(total) and finite(step) and rate else None
    age = max(0, now - entry['mtime']) if entry.get('mtime') else None
    threshold = spec.get('stale_seconds', default_stale)
    finished = finite(total) and finite(step) and step >= total
    phase_complete = bool(spec.get('complete_pattern') and re.search(spec['complete_pattern'], text))
    phase_complete = phase_complete or bool(same_file and old.get('phase_complete'))
    if phase_complete:
        finished = True
        eta = 0 if alive else None
    if alive and not finished and threshold and (latest or fmt == 'text'):
        if age is not None and age > threshold:
            errors.append('STALE_LOG')
        if finite(step) and now - progress_since > threshold:
            errors.append('STALLED_PROGRESS')
    if re.search(r'(?i)(?<![\w])(?:nan|[+-]?inf(?:inity)?)(?![\w])', text):
        errors.append('NONFINITE')
    if re.search(r'Traceback \(most recent call last\)|CUDA out of memory|OutOfMemoryError|NCCL.*(?:Error|error)', text):
        errors.append('ERROR_LOG')
    if entry.get('error'):
        errors.append('OBSERVATION_ERROR')
    best = (min(values) if spec.get('direction') == 'min' else max(values)) if values else None
    return {'id': spec['id'], 'path': spec['path'], 'latest': clean(latest),
            'metric_name': spec.get('metric_name'), 'best_in_tail': best,
            'metric_delta_in_tail': metric - values[0] if finite(metric) and values else None,
            'step_per_second': rate, 'eta_seconds': eta, 'total_steps': total,
            'phase_complete': phase_complete,
            'log_age_seconds': age, 'progress_since': progress_since, 'observed_at': now,
            'inode': entry.get('inode'), 'size': entry.get('size'), 'missing': entry.get('missing', False),
            'tail': text.splitlines()[-spec.get('tail_lines', 3):], 'alerts': errors}


def evaluate(task, host, previous=None):
    previous = previous or {}
    result = {'id': task['id'], 'host': task['host'], 'label': task.get('label', task['id']),
              'protocol': task.get('protocol'), 'alerts': []}
    if host.get('error'):
        result.update(state='UNREACHABLE', observed_at=None, error=host['error'], processes=[], streams=[],
                      last_success_at=previous.get('last_success_at'),
                      last_known_state=previous.get('last_known_state') if previous.get('state') == 'UNREACHABLE' else previous.get('state'))
        result['alerts'] = ['UNREACHABLE']
        result['advice'] = [SUGGESTIONS['UNREACHABLE']]
        return result
    now = host['observed_at']
    raw = host['tasks'][task['id']]
    processes = raw['processes']
    alive = bool(processes)
    declared = metadata(raw, task.get('status'))
    rc = metadata(raw, task.get('exit'))
    if not isinstance(rc, int) or isinstance(rc, bool):
        rc = None
    states = {'COMPLETED': ['COMPLETED', 'COMPLETE', 'DONE', 'OK', 'SUCCESS'],
              'FAILED': ['FAILED', 'ERROR'], 'STOPPED': ['STOPPED', 'CANCELLED', 'CANCELED']}
    states.update(task.get('terminal_states', {}))
    terminal = next((k for k, vals in states.items() if declared in vals), None)
    if terminal is None and rc is not None:
        terminal = 'COMPLETED' if rc == 0 else 'FAILED'
    if terminal:
        state = terminal
        if alive or (terminal == 'COMPLETED' and rc is not None and rc != 0):
            result['alerts'].append('STATUS_CONFLICT')
        if terminal == 'COMPLETED' and rc is not None and rc != 0:
            state = 'FAILED'
    elif alive:
        state = 'PAUSED' if all(p['state'] in ('T', 't') for p in processes) else 'RUNNING'
    elif declared is not None or rc is not None or any(s.get('text') for s in raw['files'].values()):
        state = 'EXITED_WITHOUT_RESULT'
        result['alerts'].append(state)
    else:
        state = 'NOT_STARTED'
    if host.get('proc_permission_errors') or raw.get('process_errors'):
        result['alerts'].append('OBSERVATION_ERROR')
        if state in ('NOT_STARTED', 'EXITED_WITHOUT_RESULT'):
            state = 'UNKNOWN'
            result['alerts'] = [a for a in result['alerts'] if a != 'EXITED_WITHOUT_RESULT']
    if state == 'FAILED':
        result['alerts'].append('FAILED')
    if raw['identity_errors']:
        result['alerts'].append('IDENTITY_MISMATCH')
    file_errors = {p: f['error'] for p, f in raw['files'].items() if f.get('error')}
    for key in ('status', 'exit', 'launch', 'deadline_file'):
        spec = task.get(key)
        if spec and raw['files'].get(spec['path'], {}).get('text') and metadata(raw, spec) is None:
            document = metadata(raw, {'path': spec['path']})
            if key == 'deadline_file' and isinstance(document, dict) and document.get('budget_mode') == 'effective':
                continue  # An effective budget intentionally has no absolute deadline.
            file_errors[spec['path']] = 'invalid JSON or missing configured key'
    if file_errors:
        result['alerts'].append('OBSERVATION_ERROR')
    launch = metadata(raw, task.get('launch')) or {}
    status_spec = task.get('status', {})
    status_doc = metadata(raw, {'path': status_spec['path']}) if status_spec.get('path') else {}
    status_doc = status_doc if isinstance(status_doc, dict) else {}
    group_match = re.fullmatch(r'agents\.(sol|seed)\.status', status_spec.get('key', ''))
    agent = status_doc.get('agents', {}).get(group_match[1], {}) if group_match else {}
    mode = status_doc.get('budget_mode', launch.get('budget_mode', 'wall')) if isinstance(launch, dict) else status_doc.get('budget_mode', 'wall')
    effective = agent.get('effective_seconds')
    target = status_doc.get('effective_target_seconds')
    limit = status_doc.get('effective_limit_seconds')
    if mode == 'effective' and not (finite(effective) and effective >= 0 and finite(target) and finite(limit) and 39600 <= target < limit <= 43200):
        result['alerts'].append('OBSERVATION_ERROR')
    if finite(effective) and finite(target) and effective < target and (state in TERMINAL or state == 'EXITED_WITHOUT_RESULT'):
        result['alerts'].append('EFFECTIVE_TARGET_NOT_REACHED')
    if finite(effective) and finite(limit) and effective > limit:
        result['alerts'].append('EFFECTIVE_LIMIT_EXCEEDED')
    deadline = (task.get('deadline_at') or metadata(raw, task.get('deadline_file'))) if mode != 'effective' else None
    deadline_ts = None
    try:
        if deadline:
            deadline_ts = timestamp(deadline)
        elif isinstance(launch, dict) and mode != 'effective':
            budget = field(launch, task.get('budget_key', 'budget_seconds'))
            started = timestamp(field(launch, task.get('started_key', 'started_at')))
            if finite(budget) and started is not None:
                deadline_ts = started + budget
    except (ValueError, TypeError):
        result['alerts'].append('OBSERVATION_ERROR')
    remaining = deadline_ts - now if deadline_ts else None
    effective_remaining = max(0, limit-effective) if finite(effective) and finite(limit) else None
    if mode == 'effective':
        remaining = effective_remaining
    if mode != 'effective' and remaining is not None and remaining <= 0 and (alive or state not in TERMINAL):
        result['alerts'].append('DEADLINE_EXCEEDED')
    prev_streams = {s['id']: s for s in previous.get('streams', [])} if previous.get('boot_id') == host.get('boot_id') else {}
    streams = []
    for spec in task.get('streams', []):
        stream = parse_stream(spec, raw['files'][spec['path']], prev_streams.get(spec['id'], {}),
                              now, alive, task.get('stale_seconds', 900))
        streams.append(stream)
        result['alerts'].extend(stream['alerts'])
        if mode != 'effective' and alive and remaining is not None and stream['eta_seconds'] is not None and stream['eta_seconds'] > max(0, remaining):
            result['alerts'].append('ETA_OVER_BUDGET')
        if not alive:
            stream['eta_seconds'] = None
    uses_gpu = task.get('uses_gpu', True)
    if uses_gpu and host['gpu'].get('error'):
        result['alerts'].append('GPU_UNAVAILABLE')
    free = raw['disk_free_bytes']
    if free is not None and free < task.get('min_disk_free_gib', 2) * 1024 ** 3:
        result['alerts'].append('LOW_DISK')
    gpu_ids = set(task.get('gpu_uuids', []))
    own_pids = {str(p['pid']) for p in processes}
    container_ids = set(task.get('gpu_container_ids', []))
    gpu_processes = [dict(p, belongs_to_task=p['pid'] in own_pids or bool(container_ids.intersection(p.get('container_ids', []))))
                     for p in host['gpu_processes'].get('rows', []) if uses_gpu and (not gpu_ids or p['gpu_uuid'] in gpu_ids)]
    result.update(state=state, declared_state=declared, exit_code=rc, observed_at=utc(now), last_success_at=utc(now),
                  boot_id=host.get('boot_id'), processes=processes, streams=streams,
                  deadline_at=utc(deadline_ts) if deadline_ts else None, budget_remaining_seconds=remaining,
                  budget_mode=mode, effective_seconds=effective, effective_target_seconds=target,
                  effective_limit_seconds=limit, effective_remaining_seconds=effective_remaining,
                  effective_target_remaining_seconds=max(0,target-effective) if finite(target) and finite(effective) else None,
                  disk_free_gib=free / 1024 ** 3 if free is not None else None,
                  gpu=[g for g in host['gpu'].get('rows', []) if uses_gpu and (not gpu_ids or g['uuid'] in gpu_ids)],
                  gpu_processes=gpu_processes, observation_errors=file_errors,
                  process_errors=raw.get('process_errors', []),
                  identity_errors=raw['identity_errors'])
    result['alerts'] = sorted(set(result['alerts']))
    result['advice'] = [SUGGESTIONS[k] for k in result['alerts']]
    return result


def snapshot(cfg, previous=None):
    old = {t['id']: t for t in (previous or {}).get('tasks', [])}
    grouped = {name: [t for t in cfg['tasks'] if t['host'] == name] for name in cfg['hosts']}
    hosts = {}
    with ThreadPoolExecutor(max_workers=min(8, len(grouped))) as pool:
        pending = {pool.submit(probe_host, cfg['hosts'][name], tasks, cfg): name
                   for name, tasks in grouped.items() if tasks}
        for future in as_completed(pending):
            hosts[pending[future]] = future.result()
    evaluated = [evaluate(t, hosts[t['host']], old.get(t['id'])) for t in cfg['tasks']]
    host_views = {}
    for name, raw in hosts.items():
        host_tasks = [task for task in evaluated if task['host'] == name]
        if raw.get('error'):
            host_views[name] = {'state': 'UNREACHABLE', 'error': raw['error'], 'gpu_count': None, 'gpus': []}
            continue
        task_by_pid = {}
        for task in host_tasks:
            for process in task.get('processes', []):
                task_by_pid[str(process['pid'])] = task
            for app in task.get('gpu_processes', []):
                if app.get('belongs_to_task'):
                    task_by_pid[str(app['pid'])] = task
        gpu_rows = raw.get('gpu', {}).get('rows', [])
        app_rows = raw.get('gpu_processes', {}).get('rows', [])
        gpus = []
        for gpu in gpu_rows:
            uuid_value = gpu.get('uuid')
            apps = [app for app in app_rows if app.get('gpu_uuid') == uuid_value]
            running = []
            unknown = []
            for app in apps:
                task = task_by_pid.get(str(app.get('pid')))
                if task:
                    running.append({'task_id': task['id'], 'label': task.get('label', task['id']),
                                    'state': task['state'], 'pid': str(app.get('pid')),
                                    'memory_used_mib': app.get('memory_used_mib')})
                else:
                    unknown.append(app)
            gpus.append({'index': gpu.get('index'), 'uuid': uuid_value, 'name': gpu.get('name'),
                         'memory_total_mib': gpu.get('memory_total_mib'), 'memory_used_mib': gpu.get('memory_used_mib'),
                         'utilization_pct': gpu.get('utilization_pct'), 'temperature_c': gpu.get('temperature_c'),
                         'running_tasks': running, 'unknown_processes': unknown})
        host_views[name] = {'state': 'OK', 'gpu_count': len(gpus), 'gpus': gpus,
                            'process_query_error': raw.get('gpu_processes', {}).get('error')}
    return {'schema_version': 1, 'collected_at': utc(), 'read_only': True,
            'hosts': host_views, 'tasks': evaluated}


def atomic_json(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(dump(value) + '\n')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load_latest(state):
    try:
        return json.loads((state / 'latest.json').read_text())
    except (FileNotFoundError, ValueError):
        return {}


def persist(state, data, previous):
    state.mkdir(parents=True, exist_ok=True)
    atomic_json(state / 'latest.json', data)
    day = data['collected_at'][:10]
    with (state / f'observations-{day}.jsonl').open('a') as f:
        f.write(dump(data) + '\n')
    old = {t['id']: t for t in previous.get('tasks', [])}
    with (state / f'events-{day}.jsonl').open('a') as f:
        for task in data['tasks']:
            before = old.get(task['id'], {})
            if (task['state'], task['alerts']) != (before.get('state'), before.get('alerts')):
                f.write(dump({'time': data['collected_at'], 'task': task['id'],
                              'previous_state': before.get('state'), 'state': task['state'],
                              'alerts': task['alerts'], 'advice': task['advice']}) + '\n')


def duration(seconds):
    if seconds is None:
        return '?'
    sign = '-' if seconds < 0 else ''
    seconds = abs(int(seconds))
    return f'{sign}{seconds // 3600}h{seconds % 3600 // 60:02d}m'


ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


class Colors:
    def __init__(self, enabled):
        self.enabled = enabled

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.enabled else text

    def __getattr__(self, name):
        codes = {'reset': '0', 'bold': '1', 'dim': '2', 'red': '31', 'green': '32',
                 'yellow': '33', 'blue': '34', 'magenta': '35', 'cyan': '36', 'white': '37'}
        if name not in codes:
            raise AttributeError(name)
        return lambda text: self.paint(text, codes[name])


def _color_enabled(mode):
    if mode == 'always':
        return True
    if mode == 'never' or os.environ.get('NO_COLOR') is not None:
        return False
    return sys.stdout.isatty()


def _wrap_lines(text, width, indent='', colors=None):
    text = str(text)
    if width <= len(indent) + 8:
        width = len(indent) + 8
    return textwrap.wrap(text, width=max(8, width - len(indent)),
                         initial_indent=indent, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False) or [indent.rstrip()]


def _number(value, digits=3):
    if value is None:
        return '?'
    if isinstance(value, float):
        return f'{value:.{digits}f}'.rstrip('0').rstrip('.')
    if isinstance(value, int):
        return f'{value:,}'
    return str(value)


def _state_style(colors, state):
    if state in ('COMPLETED', 'STOPPED'):
        return colors.green(state)
    if state in ('FAILED', 'UNKNOWN', 'EXITED_WITHOUT_RESULT'):
        return colors.red(state)
    if state in ('PAUSED', 'UNREACHABLE'):
        return colors.yellow(state)
    if state == 'RUNNING':
        return colors.cyan(state)
    return colors.dim(state)


def render_tasks(data, color='auto'):
    """Render task/log details for debugging; JSON callers must use ``--json`` instead."""
    colors = Colors(_color_enabled(color))
    width = shutil.get_terminal_size((110, 24)).columns
    rule = colors.dim('─' * min(max(width, 48), 96))
    tasks = data.get('tasks', [])
    print()
    print(colors.bold(colors.blue('AutoResearch GPU 任务详情')))
    print(colors.dim(f"采集时间 {data.get('collected_at', '?')}   只读模式   任务 {len(tasks)} 个"))
    print(rule)
    for index, t in enumerate(tasks):
        state = t.get('state', 'UNKNOWN')
        alerts = t.get('alerts', [])
        title = f"{t['id']}  ·  {t.get('label', t['id'])}"
        print(colors.bold(colors.white(f'[{index + 1}/{len(tasks)}] {title}')))
        print(f"  状态   {_state_style(colors, state)}    阶段 {colors.bold(str(t.get('declared_state') or '?'))}")
        pids = ', '.join(str(p['pid']) for p in t.get('processes', [])) or '-'
        if t.get('budget_mode') == 'effective':
            print(f"  进程   {pids}    已计有效 {colors.bold(duration(t.get('effective_seconds')))} / 目标 {duration(t.get('effective_target_seconds'))} / 硬上限 {duration(t.get('effective_limit_seconds'))}")
            print(f"  预算   距有效目标 {duration(t.get('effective_target_remaining_seconds'))}    有效余额 {duration(t.get('effective_remaining_seconds'))}")
        else:
            print(f"  进程   {pids}    距墙钟截止 {colors.bold(duration(t.get('budget_remaining_seconds')))}")
        if t.get('error'):
            for line in _wrap_lines(t['error'], width, '  查询失败 ', colors):
                print(colors.red(line))
        for stream in t.get('streams', []):
            latest = stream.get('latest') or {}
            print(f"  流程   {colors.magenta(stream['id'])}")
            if latest:
                step = latest.get('step')
                total = stream.get('total_steps')
                progress = f"{_number(step)}/{_number(total)}" if total else _number(step)
                rate = f"{stream['step_per_second']:.3g}/s" if stream.get('step_per_second') is not None else '?'
                delta = _number(stream.get('metric_delta_in_tail'))
                metric = latest.get('metric')
                print(f"    进度   {colors.bold(progress)}   速率 {rate}   ETA {colors.bold(duration(stream.get('eta_seconds')))}")
                print(f"    指标   {stream.get('metric_name') or 'metric'}={colors.bold(_number(metric))}   "
                      f"窗口变化 {delta}   日志年龄 {duration(stream.get('log_age_seconds'))}")
                extras = [(key, value) for key, value in latest.items()
                          if key not in {'step', 'metric', 'elapsed_seconds'} and value is not None]
                if extras:
                    extra_text = '   '.join(f'{key}={_number(value)}' for key, value in extras)
                    for line in _wrap_lines(extra_text, width, '    细节   ', colors):
                        print(colors.dim(line))
            elif stream.get('tail'):
                for line in _wrap_lines(stream['tail'][-1], width, '    日志   ', colors):
                    print(colors.dim(line))
        if alerts:
            print(f"  {colors.red(colors.bold('告警'))}  " + colors.red('  '.join(alerts)))
            for advice in t.get('advice', []):
                for line in _wrap_lines(advice, width, '         ', colors):
                    print(colors.yellow(line))
        if index != len(tasks) - 1:
            print(rule)
    print()


def _percent(used, total):
    try:
        return f'{float(used) / float(total) * 100:.1f}%'
    except (TypeError, ValueError, ZeroDivisionError):
        return '?'


def render_gpu(data, color='auto'):
    """Render the resource view: host -> GPU -> live task ownership."""
    colors = Colors(_color_enabled(color))
    width = shutil.get_terminal_size((110, 24)).columns
    rule = colors.dim('─' * min(max(width, 48), 96))
    hosts = data.get('hosts', {})
    tasks = data.get('tasks', [])
    active_ids = {item['task_id'] for host in hosts.values() for gpu in host.get('gpus', [])
                  for item in gpu.get('running_tasks', [])}
    unreachable_hosts = {name for name, host in hosts.items()
                         if host.get('error') or host.get('state') == 'UNREACHABLE'}
    known_gpu_count = sum((host.get('gpu_count') or 0) for name, host in hosts.items()
                          if name not in unreachable_hosts)
    if unreachable_hosts:
        gpu_summary = (f'{known_gpu_count}+? 张' if known_gpu_count else '? 张') + \
            f'（{len(unreachable_hosts)} 台主机不可达）'
        unknown_task_hosts = {task.get('host') for task in tasks
                              if task.get('host') in unreachable_hosts}
        task_summary = (f'{len(active_ids)}+? 个' if active_ids else '? 个') \
            if unknown_task_hosts else f'{len(active_ids)} 个'
    else:
        gpu_summary = f'{known_gpu_count} 张'
        task_summary = f'{len(active_ids)} 个'
    print()
    print(colors.bold(colors.blue('AutoResearch GPU 资源总览')))
    print(colors.dim(f"采集时间 {data.get('collected_at', '?')}   只读模式   "
                     f"主机 {len(hosts)} 台   GPU {gpu_summary}   "
                     f"运行任务 {task_summary}"))
    print(rule)
    for host_index, (host_name, host) in enumerate(hosts.items()):
        if host.get('error'):
            print(colors.bold(colors.white(f'[{host_name}]')))
            print(colors.red('  查询失败: ' + str(host['error'])))
            if host_index != len(hosts) - 1:
                print(rule)
            continue
        gpu_count = host.get('gpu_count', 0)
        print(colors.bold(colors.white(f'[{host_name}]  ·  {gpu_count} 张 GPU')))
        if not host.get('gpus'):
            print(colors.yellow('  没有读取到 GPU；请检查 nvidia-smi 或主机连接。'))
        for gpu_index, gpu in enumerate(host.get('gpus', [])):
            used = gpu.get('memory_used_mib', '?')
            total = gpu.get('memory_total_mib', '?')
            utilization = str(gpu.get('utilization_pct', '?')) + '%'
            title = f"GPU{gpu.get('index', '?')}  {gpu.get('name') or 'Unknown GPU'}"
            print(f"  {colors.bold(colors.cyan(title))}")
            print(f"    占用   {colors.bold(str(used) + '/' + str(total) + ' MiB')} ({_percent(used, total)})  "
                  f"利用率 {colors.bold(utilization)}  温度 {gpu.get('temperature_c', '?')}°C")
            running = gpu.get('running_tasks', [])
            unknown = gpu.get('unknown_processes', [])
            if running:
                print(f"    任务   {colors.green(str(len(running)) + ' 个已识别任务')}")
                seen = set()
                for item in running:
                    key = (item.get('task_id'), item.get('pid'))
                    if key in seen:
                        continue
                    seen.add(key)
                    state = _state_style(colors, item.get('state', 'UNKNOWN'))
                    memory = item.get('memory_used_mib', '?')
                    print(f"      • {item.get('label', item.get('task_id'))}  {state}  "
                          f"PID {item.get('pid', '?')}  显存 {memory} MiB")
            else:
                print(f"    任务   {colors.dim('无已识别运行任务')}")
            if unknown:
                unknown_text = '、'.join(f"PID {item.get('pid', '?')} ({item.get('memory_used_mib', '?')} MiB)"
                                        for item in unknown)
                for line in _wrap_lines(unknown_text, width, '    其他   ', colors):
                    print(colors.yellow(line))
            elif str(used) not in ('0', '0.0') and not running:
                print(colors.yellow('    其他   显存有占用，但 nvidia-smi 未返回可归属进程。'))
            if host.get('process_query_error'):
                print(colors.yellow('    其他   进程列表查询失败：' + str(host['process_query_error'])))
            if gpu_index != len(host.get('gpus', [])) - 1:
                print()
        if host_index != len(hosts) - 1:
            print(rule)
    print()


def render(data, color='auto', view='gpu'):
    """Render a human view; ``gpu`` is the default and ``tasks`` is diagnostic."""
    if view == 'tasks':
        return render_tasks(data, color)
    return render_gpu(data, color)


def refresh_auth(cfg, auth_path):
    """Re-read auth.txt so a live watch follows credential changes without a restart.

    Falls back to the credentials already loaded when the file is momentarily
    invalid, so a half-written edit cannot take the monitor down.
    """
    try:
        auth = load_auth(auth_path)
    except ValueError:
        return
    for name, host in cfg['hosts'].items():
        if host.get('transport', 'ssh') != 'ssh':
            continue
        if any(host.get(key) != auth[key] for key in ('host', 'user', 'port')):
            purge_host_key(auth)
        cfg['hosts'][name] = apply_auth(host, auth)


def watch(cfg, state, interval, max_hours, max_polls, until_terminal, token=None, auth_path=None):
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'watch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('a monitor already owns this state directory')
        token = token or uuid.uuid4().hex
        info = {'pid': os.getpid(), 'token': token, 'started_at': utc(),
                'deadline_at': utc(time.time() + max_hours * 3600), 'state': 'RUNNING', 'read_only': True}
        atomic_json(state / 'watch.json', info)
        stop_path = state / 'stop-monitor.json'
        end = time.monotonic() + max_hours * 3600
        count = 0
        reason = 'MONITOR_TIME_LIMIT'
        try:
            while time.monotonic() < end:
                if auth_path:
                    refresh_auth(cfg, Path(auth_path))
                previous = load_latest(state)
                data = snapshot(cfg, previous)
                persist(state, data, previous)
                render(data)
                count += 1
                info.update(last_poll_at=data['collected_at'], polls=count)
                atomic_json(state / 'watch.json', info)
                if max_polls and count >= max_polls:
                    reason = 'POLL_LIMIT'
                    break
                if until_terminal and all(t['state'] in TERMINAL and not t['processes'] for t in data['tasks']):
                    reason = 'ALL_TASKS_TERMINAL'
                    break
                wake = min(end, time.monotonic() + interval)
                while time.monotonic() < wake:
                    try:
                        stop = json.loads(stop_path.read_text())
                        if stop.get('token') == token:
                            reason = 'LOCAL_MONITOR_STOP_REQUESTED'
                            return
                    except (FileNotFoundError, ValueError):
                        pass
                    time.sleep(min(0.5, max(0, wake - time.monotonic())))
        except KeyboardInterrupt:
            reason = 'KEYBOARD_INTERRUPT'
        except BaseException:
            reason = 'MONITOR_ERROR'
            raise
        finally:
            info.update(state='STOPPED', ended_at=utc(), reason=reason)
            atomic_json(state / 'watch.json', info)


def monitor_alive(state):
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'watch.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['validate', 'status', 'watch', 'maintain', 'monitor-status', 'stop-monitor', 'stop-task'])
    p.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    p.add_argument('--auth', type=Path, default=DEFAULT_AUTH,
                   help='凭据文件，默认工作区根目录 auth.txt（可用 AUTORESEARCH_AUTH_FILE 覆盖）')
    p.add_argument('--task', action='append', help='task ID; repeat to select several')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--view', choices=['gpu', 'tasks'], default='gpu',
                   help='human view: GPU ownership summary (default) or task details')
    p.add_argument('--color', choices=['auto', 'always', 'never'], default='auto',
                   help='terminal colors for human output; JSON is never colored')
    p.add_argument('--interval', type=float)
    p.add_argument('--max-hours', type=float)
    p.add_argument('--max-polls', type=int)
    p.add_argument('--until-terminal', action='store_true')
    p.add_argument('--check', action='store_true', help='status exit 2 if alerts exist')
    p.add_argument('--dry-run', action='store_true', help='preview stop-task without remote changes')
    p.add_argument('--reason', help='required for an explicit stop-task request')
    p.add_argument('--token', help=argparse.SUPPRESS)
    a = p.parse_args()
    try:
        auth_path = a.auth.resolve()
        needs_remote = a.action in ('status', 'watch', 'maintain', 'stop-task')
        # 凭据文件是唯一的认证来源；只有需要连接远端时才强制要求它存在。
        auth = load_auth(auth_path) if needs_remote or auth_path.exists() else None
        cfg = load_config(a.config.resolve(), auth)
        if needs_remote:
            prepare_ssh(cfg)
        if a.task:
            unknown = set(a.task) - {t['id'] for t in cfg['tasks']}
            if unknown:
                raise ValueError(f'unknown task IDs: {sorted(unknown)}')
            cfg['tasks'] = [t for t in cfg['tasks'] if t['id'] in a.task]
        state = (a.config.resolve().parent / cfg['state_dir']).resolve()
        # A selection gets its own watch lock/history; a full watch cannot lose tasks.
        if a.task:
            key = hashlib.sha256('\n'.join(sorted(set(a.task))).encode()).hexdigest()[:12]
            state = state / ('selection-' + key)
        interval = a.interval if a.interval is not None else cfg['interval_seconds']
        hours = a.max_hours if a.max_hours is not None else cfg['max_hours']
        if not finite(interval) or interval < 1 or not finite(hours) or hours <= 0 or (a.max_polls is not None and a.max_polls < 1):
            raise ValueError('interval >= 1, max-hours > 0 and max-polls >= 1 required')
        if a.action == 'validate':
            print(dump({'valid': True, 'tasks': [t['id'] for t in cfg['tasks']], 'state_dir': str(state)}))
        elif a.action == 'stop-task':
            if not a.task or len(a.task) != 1 or not a.reason:
                raise ValueError('stop-task requires exactly one --task and a --reason')
            return stop_task(cfg, cfg['tasks'][0], a.reason, a.dry_run, state)
        elif a.action == 'status':
            data = snapshot(cfg, load_latest(state))
            print(dump(data)) if a.json else render(data, a.color, a.view)
            if a.check and any(t['alerts'] for t in data['tasks']):
                return 2
        elif a.action == 'watch':
            if not cfg['tasks']:
                print('没有活动任务，无需启动监控器。')
                return 0
            watch(cfg, state, interval, hours, a.max_polls, a.until_terminal, a.token, auth_path)
        elif a.action == 'monitor-status':
            info = json.loads((state / 'watch.json').read_text()) if (state / 'watch.json').exists() else {}
            print(dump({'collector_alive': monitor_alive(state), 'watch': info, 'state_dir': str(state)}))
        elif a.action == 'stop-monitor':
            if not monitor_alive(state):
                print('监控器未运行；训练任务未改动。')
            else:
                info = json.loads((state / 'watch.json').read_text())
                atomic_json(state / 'stop-monitor.json', {'token': info['token'], 'time': utc()})
                print('已请求停止本地监控器；在当前查询超时以内退出。训练任务未改动。')
        else:
            if not cfg['tasks']:
                print('没有活动任务，无需启动监控器。')
                return 0
            if monitor_alive(state):
                print('只读监控器已运行: ' + str(state / 'watch.json'))
                return 0
            state.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            command = [sys.executable, '-u', str(Path(__file__).resolve()), 'watch', '--config', str(a.config.resolve()),
                       '--auth', str(auth_path),
                       '--interval', str(interval), '--max-hours', str(hours), '--token', token]
            for task_id in a.task or []:
                command += ['--task', task_id]
            if a.until_terminal:
                command += ['--until-terminal']
            if a.max_polls:
                command += ['--max-polls', str(a.max_polls)]
            with (state / 'collector.log').open('a') as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            # Wait only for lock/metadata, not a cloud poll.
            for _ in range(30):
                if child.poll() is not None:
                    if monitor_alive(state):
                        break
                    raise ValueError('collector failed to start; see collector.log')
                if monitor_alive(state) and (state / 'watch.json').exists():
                    break
                time.sleep(0.1)
            else:
                raise ValueError('collector startup not confirmed; see collector.log')
            print(dump({'collector_pid': child.pid, 'state_dir': str(state), 'read_only': True}))
    except (ValueError, KeyError, OSError, TypeError) as e:
        print(f'gpu-monitor: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
