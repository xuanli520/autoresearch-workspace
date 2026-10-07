#!/usr/bin/env python3
"""AutoResearch GPU task monitor: read-only collection, local alerts and handoff."""
from __future__ import annotations
import argparse
import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
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
from typing import Any

try:
    from .probe import field
    from .privacy import public_record
    from .ui import render_unified
    from .askpass import (FIFO_IDENTITY_ENV, FIFO_PATH_ENV, MAX_PASSWORD_BYTES,
                          inspect_fifo, open_password_fifo, validate_helper)
except ImportError:  # Preserve direct script execution.
    from probe import field
    from privacy import public_record
    from ui import render_unified
    from askpass import (FIFO_IDENTITY_ENV, FIFO_PATH_ENV, MAX_PASSWORD_BYTES,
                         inspect_fifo, open_password_fifo, validate_helper)

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / 'tasks.json'
DEFAULT_STALE_THRESHOLD_SECONDS = 15 * 60
DEFAULT_MIN_DISK_FREE_GIB = 2
DEFAULT_TAIL_BYTES = 64 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_READ_BYTES = 1024 * 1024
MAX_PROBE_WORKERS = 8

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
IDENTITY_ALERTS = {'CONTROLLER_TYPE_MISMATCH', 'CONTROLLER_IDENTITY_MISMATCH'}
UNCERTAIN_LIFECYCLE_ALERTS = IDENTITY_ALERTS | {'OBSERVATION_ERROR', 'IDENTITY_MISMATCH', 'STATUS_CONFLICT'}
DEFAULT_ERROR_PATTERNS = (
    {'pattern': r'Traceback \(most recent call last\)', 'severity': 'error'},
    {'pattern': r'CUDA out of memory', 'severity': 'critical'},
    {'pattern': r'OutOfMemoryError', 'severity': 'critical'},
    {'pattern': r'NCCL.*(?:Error|error)', 'severity': 'error'},
)
DEFAULT_ERROR_WINDOW_LINES = 100
MAX_STREAM_CACHE_ENTRIES = 256
_STREAM_CACHES: OrderedDict[str, dict] = OrderedDict()
_OPERATOR_OVERLAY = {}
SUGGESTIONS = {
    'UNREACHABLE': '检查 SSH 网络和交互认证；远端训练是否存活未知。',
    'EXITED_WITHOUT_RESULT': '进程已不见但无可靠终态；检查退出码、OOM、控制器日志。',
    'STATUS_CONFLICT': '退出码、声明状态或存活进程相互冲突；核对控制器及子任务，勿直接重启。',
    'CONTROLLER_TYPE_MISMATCH': '状态文件的控制器类型与登记不符；修正登记或状态路径，计时保持未知。',
    'CONTROLLER_IDENTITY_MISMATCH': '状态文件的 run_id 与登记不符；核对运行身份，不能混用另一运行的计时。',
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
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError('timestamps must be finite')
        return float(value)
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


def password_bytes(password: str) -> bytes:
    if not isinstance(password, str) or not password:
        raise ValueError('SSH password is required from auth.txt')
    secret = password.encode('utf-8')
    if len(secret) > MAX_PASSWORD_BYTES or any(c in secret for c in (b'\n', b'\r', b'\x00')):
        raise ValueError('SSH password is too long or contains unsupported control characters')
    return secret


def load_auth(path, raw_bytes=None):
    """Parse the labelled credential file; the only source of SSH auth material.

    Accepts ``标签：值`` on one line or the label alone followed by the value on the
    next line, and tolerates full-width or half-width colons plus spaces inside
    labels (for example ``密 码``).
    """
    path = Path(path)
    try:
        text = raw_bytes.decode('utf-8') if raw_bytes is not None else path.read_text(encoding='utf-8')
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
        raise ValueError(f'凭据文件 {path} 的登录端口不是数字')
    if not 1 <= port <= 65535:
        raise ValueError(f'凭据文件 {path} 的登录端口超出范围')
    host = fields['host']
    if not re.fullmatch(r'[A-Za-z0-9_.:%\[\]-]+', host) or host.startswith('-'):
        raise ValueError(f'凭据文件 {path} 的 IP地址无效')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', fields['user']):
        raise ValueError(f'凭据文件 {path} 的用户名无效')
    password_bytes(fields['password'])
    return {'host': host, 'user': fields['user'], 'password': fields['password'], 'port': port}


def validate_ssh_host(host: dict, *, allow_credentials: bool = False) -> None:
    legacy = {'target', 'password_env', 'identity_file'} & host.keys()
    if legacy:
        raise ValueError('legacy SSH authentication fields are unsupported: ' + ', '.join(sorted(legacy)))
    if not allow_credentials:
        configured = {'hostname', 'host', 'user', 'port', 'password'} & host.keys()
        if configured:
            raise ValueError('SSH credentials must come from auth.txt: ' + ', '.join(sorted(configured)))
    options = host.get('options', [])
    if not isinstance(options, list) or not all(isinstance(value, str) for value in options):
        raise ValueError('SSH options must be an argv list of strings')
    allowed = {'addressfamily', 'compression', 'connectionattempts', 'connecttimeout',
               'ipqos', 'loglevel', 'serveraliveinterval', 'serveralivecountmax', 'tcpkeepalive'}
    index = 0
    while index < len(options):
        option = options[index]
        index += 1
        if option in ('-4', '-6'):
            continue
        if option == '-o' and index < len(options):
            setting = options[index]
            index += 1
        elif option.startswith('-o') and len(option) > 2:
            setting = option[2:]
        else:
            raise ValueError('SSH options may only set approved connection and diagnostic options')
        key, separator, value = setting.partition('=')
        if not separator or key.lower() not in allowed or not value or any(c in setting for c in '\x00\r\n'):
            raise ValueError('SSH option is not approved: ' + key)


def apply_auth(host: dict, auth: dict) -> dict:
    """Overlay the credential file onto an SSH host; auth.txt is authoritative."""
    validate_ssh_host(host, allow_credentials=True)
    password_bytes(auth['password'])
    host = dict(host)
    host.update(hostname=auth['host'], user=auth['user'], port=auth['port'], password=auth['password'])
    return host


def validate_pattern(pattern: str) -> None:
    try:
        re.compile(pattern)
    except (re.error, TypeError) as exc:
        raise ValueError(f'invalid stream regular expression: {exc}') from exc


def load_config(path: Path, auth: dict[str, Any] | None = None, raw_bytes=None) -> dict[str, Any]:
    source_bytes = raw_bytes if raw_bytes is not None else path.read_bytes()
    cfg = json.loads(source_bytes)
    if not isinstance(cfg, dict):
        raise ValueError('config must be an object')
    if cfg.get('version') != 1 or not isinstance(cfg.get('tasks'), list) or not isinstance(cfg.get('hosts'), dict):
        raise ValueError('config requires version=1, hosts object and tasks array')
    cfg.setdefault('interval_seconds', 60)
    cfg.setdefault('timeout_seconds', 30)
    cfg.setdefault('connection_attempts', 2)
    cfg.setdefault('retry_delay_seconds', 2)
    cfg.setdefault('max_hours', 12)
    cfg.setdefault('state_dir', '.state')
    for key in ('interval_seconds', 'timeout_seconds', 'max_hours', 'connection_attempts'):
        if not finite(cfg[key]) or cfg[key] <= 0:
            raise ValueError(f'{key} must be positive')
    if int(cfg['connection_attempts']) != cfg['connection_attempts']:
        raise ValueError('connection_attempts must be an integer')
    if not finite(cfg['retry_delay_seconds']) or cfg['retry_delay_seconds'] < 0:
        raise ValueError('retry_delay_seconds must be nonnegative')
    cfg.setdefault('probe_round_timeout_seconds',
                   cfg['timeout_seconds'] * cfg['connection_attempts']
                   + cfg['retry_delay_seconds'] * (cfg['connection_attempts'] - 1))
    if not finite(cfg['probe_round_timeout_seconds']) or cfg['probe_round_timeout_seconds'] <= 0:
        raise ValueError('probe_round_timeout_seconds must be positive')
    if cfg['interval_seconds'] < 1:
        raise ValueError('interval_seconds must be >= 1')
    for key, default, upper in [('tail_bytes', DEFAULT_TAIL_BYTES, MAX_READ_BYTES),
                               ('metadata_bytes', MAX_METADATA_BYTES, MAX_READ_BYTES)]:
        cfg.setdefault(key, default)
        if not isinstance(cfg[key], int) or not 256 <= cfg[key] <= upper:
            raise ValueError(f'{key} must be an integer in [256, {upper}]')
    ids = set()
    for name, host in cfg['hosts'].items():
        if not isinstance(host, dict):
            raise ValueError('host must be an object')
        transport = host.get('transport', 'ssh')
        if transport not in ('ssh', 'local'):
            raise ValueError(f'{name}: transport must be ssh or local')
        if transport == 'ssh':
            validate_ssh_host(host)
            if auth is not None:
                host = cfg['hosts'][name] = apply_auth(host, auth)
            if host.get('connect_timeout_seconds') is not None and (not isinstance(host['connect_timeout_seconds'], int) or host['connect_timeout_seconds'] < 1):
                raise ValueError(f'{name}: connect_timeout_seconds must be a positive integer')
            if not isinstance(host.get('options', []), list):
                raise ValueError('SSH options must be an argv list')
    for task in cfg['tasks']:
        if not isinstance(task, dict):
            raise ValueError('task must be an object')
        if not re.fullmatch(r'[\w.-]+', task['id']) or task['id'] in ids:
            raise ValueError('task IDs must be unique simple names')
        ids.add(task['id'])
        # Every registered long-running agent has one official lifecycle
        # identity.  Compatibility stop markers are intentionally rejected;
        # they made the same task represent two different control contracts.
        controller = task.get('controller')
        scheduler = task.get('scheduler')
        if controller is not None:
            if not isinstance(controller, dict) or controller.get('type') != 'research_handoff' or not controller.get('run_id'):
                raise ValueError(f"{task['id']}: controller must be research_handoff with run_id")
        if scheduler is not None:
            if not isinstance(scheduler, dict) or scheduler.get('type') != 'gpu_scheduler':
                raise ValueError(f"{task['id']}: scheduler must be gpu_scheduler")
            job_ids = scheduler.get('job_ids', scheduler.get('job_id', scheduler.get('request_ids', scheduler.get('request_id'))))
            if isinstance(job_ids, str):
                job_ids = [job_ids]
            if not isinstance(job_ids, list) or not job_ids or not all(isinstance(j, str) and j for j in job_ids):
                raise ValueError(f"{task['id']}: scheduler requires job_id or nonempty job_ids")
            scheduler['job_ids'] = job_ids
        if (task['host'] not in cfg['hosts'] or not Path(task['root']).is_absolute()
                or Path(task['root']) == Path('/') or '..' in Path(task['root']).parts):
            raise ValueError(f"{task['id']}: unknown host or nonabsolute root")
        paths = [task[k]['path'] for k in ('status', 'exit', 'launch', 'deadline_file') if task.get(k)]
        for selector in task.get('processes', []):
            if not selector.get('contains') or not all(isinstance(s, str) and s for s in selector['contains']):
                raise ValueError('process selectors require nonempty contains tokens')
            if selector.get('pid_file'):
                paths.append(selector['pid_file'])
        if any(key in task for key in ('stop', 'marker', 'process_groups')):
            raise ValueError(f"{task['id']}: custom stop contracts are unsupported; use the official controller")
        stream_ids = set()
        for stream in task.get('streams', []):
            if stream['id'] in stream_ids:
                raise ValueError('stream IDs must be unique per task')
            stream_ids.add(stream['id'])
            paths.append(stream['path'])
            if stream.get('format', 'jsonl') not in ('jsonl', 'regex', 'text'):
                raise ValueError('stream format must be jsonl, regex or text')
            if stream.get('format') == 'regex':
                validate_pattern(stream['pattern'])
            if stream.get('complete_pattern'):
                validate_pattern(stream['complete_pattern'])
            if 'error_patterns' in stream:
                patterns = stream['error_patterns']
                if not isinstance(patterns, list):
                    raise ValueError('error_patterns must be an array')
                for rule in patterns:
                    if (not isinstance(rule, dict) or not isinstance(rule.get('pattern'), str)
                            or not rule['pattern'] or rule.get('severity', 'error') not in
                            ('warning', 'error', 'critical')):
                        raise ValueError('error_patterns require a pattern and valid severity')
                    validate_pattern(rule['pattern'])
            window = stream.get('error_window_lines', DEFAULT_ERROR_WINDOW_LINES)
            if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                raise ValueError('error_window_lines must be a positive integer')
        if any(Path(p).is_absolute() or '..' in Path(p).parts for p in paths):
            raise ValueError('file paths must stay relative to task root')
        if task.get('deadline_at'):
            timestamp(task['deadline_at'])
        overlay = task.get('operator_overlay', {})
        if not isinstance(overlay, dict):
            raise ValueError('operator_overlay must be an object')
        for record in overlay.get('records', []):
            name = record.get('contract_path') if isinstance(record, dict) else None
            if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
                raise ValueError('operator contract paths must stay relative to registered root')
        name = overlay.get('metadata_path')
        if name is not None and (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts):
            raise ValueError('operator metadata path must stay relative to registered root')
    try:
        from .diagnostics import validate_thresholds
        from .runtime import validate_registration
    except ImportError:
        from diagnostics import validate_thresholds
        from runtime import validate_registration
    validate_thresholds(cfg)
    for task in cfg['tasks']:
        validate_registration(task)
    cfg['_config_metadata'] = {'path': str(path.resolve()), 'sha256': hashlib.sha256(source_bytes).hexdigest(),
                               'revision': 1, 'loaded_at': utc(), 'reload_state': 'ACTIVE',
                               'last_reload_at': None, 'last_reload_error': None}
    return cfg


KNOWN_HOSTS = HERE / '.state' / 'known_hosts'
HOST_KEY_ERRORS = ('host key verification failed', 'remote host identification has changed',
                   'no matching host key type found')


def known_hosts_options() -> list[str]:
    """Pin verification to a managed known_hosts so host rotation cannot break startup."""
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    return ['-o', 'StrictHostKeyChecking=accept-new',
            '-o', f'UserKnownHostsFile={KNOWN_HOSTS}',
            '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'HashKnownHosts=no']


def purge_host_key(host, timeout: float = 10):
    """Drop any stored key for the target so a reused cloud IP re-verifies cleanly."""
    name = host.get('hostname')
    if not name:
        return
    port = host.get('port')
    spec = f'[{name}]:{port}' if port and port != 22 else name
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_HOSTS.touch(exist_ok=True)
    try:
        # macOS ssh-keygen can emit locale-specific bytes that are not UTF-8;
        # host-key cleanup is diagnostic and must not block the SSH attempt.
        subprocess.run(['ssh-keygen', '-R', spec, '-f', str(KNOWN_HOSTS)],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # ssh-keygen -R backs the previous file up; keep the managed dir tidy.
    KNOWN_HOSTS.with_name(KNOWN_HOSTS.name + '.old').unlink(missing_ok=True)


def prepare_ssh(cfg):
    """Refresh host keys for every SSH host before a run; credentials come from auth.txt."""
    for host in cfg.get('hosts', {}).values():
        if host.get('transport', 'ssh') == 'ssh':
            purge_host_key(host)


def ssh_command(host: dict, remote_command: str, password_auth: bool = False) -> list[str]:
    """Build a non-interactive SSH command without reusing potentially stale mux sockets."""
    validate_ssh_host(host, allow_credentials=True)
    target = f"{host['user']}@{host['hostname']}" if host.get('user') else host['hostname']
    command = ['ssh', '-F', '/dev/null', '-T', '-o', f"BatchMode={'no' if password_auth else 'yes'}",
               '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
               '-o', f"ConnectTimeout={host.get('connect_timeout_seconds', 10)}",
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
               '-o', 'PubkeyAuthentication=no', '-o', 'HostbasedAuthentication=no',
               '-o', 'GSSAPIAuthentication=no']
    command += known_hosts_options()
    if password_auth:
        # 密钥认证已移除：只允许密码／键盘交互，不尝试 publickey。
        command.extend(['-o', 'PreferredAuthentications=keyboard-interactive,password',
                        '-o', 'NumberOfPasswordPrompts=1'])
    if host.get('port') is not None:
        command.extend(['-p', str(host['port'])])
    command.extend(host.get('options', []))
    command.extend([target, remote_command])
    return command


def run_ssh(host: dict, command: list[str], code: str, timeout: float) -> subprocess.CompletedProcess:
    """Run SSH with the askpass password resolved from auth.txt.

    OpenSSH execs the askpass helper after closefrom(), so an inherited descriptor
    cannot deliver the secret; the helper re-opens a FIFO by the path given in its
    environment. The secret stays out of argv, the inherited environment and any
    regular file.
    """
    secret = password_bytes(host.get('password'))
    helper = HERE / 'askpass.py'
    validate_helper(helper)

    env = os.environ.copy()
    secrets_dir = tempfile.mkdtemp(prefix='autoresearch-askpass-')
    fifo_path = os.path.join(secrets_dir, 'password.fifo')
    fifo_fd = None
    try:
        os.mkfifo(fifo_path, 0o600)
        identity = inspect_fifo(fifo_path)
        # An atomic nonblocking write keeps the secret in the verified FIFO
        # until askpass consumes it, with no inherited descriptor required.
        fifo_fd = open_password_fifo(fifo_path, identity, os.O_RDWR)
        payload = secret + b'\n'
        if len(payload) > os.fpathconf(fifo_fd, 'PC_PIPE_BUF') or os.write(fifo_fd, payload) != len(payload):
            raise ValueError('SSH password could not be written atomically')
        inspect_fifo(fifo_path, identity)
        env.update({FIFO_PATH_ENV: fifo_path, FIFO_IDENTITY_ENV: identity,
                    'SSH_ASKPASS': str(helper),
                    'SSH_ASKPASS_REQUIRE': 'force',
                    'DISPLAY': env.get('DISPLAY') or ':autoresearch-gpu-monitor'})
        return subprocess.run(command, input=code, text=True, capture_output=True, timeout=timeout,
                              env=env)
    finally:
        if fifo_fd is not None:
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


def probe_host(host: dict[str, Any], tasks: list[dict[str, Any]], cfg: dict[str, Any],
               deadline: float | None = None) -> dict[str, Any]:
    request = {'tasks': tasks, 'tail_bytes': cfg['tail_bytes'], 'metadata_bytes': cfg['metadata_bytes']}
    # Encode JSON as a Python literal, never interpolate task data into shell commands.
    prefix = (HERE / 'runtime.py').read_text() if (HERE / 'runtime.py').exists() else ''
    code = prefix + '\n' + (HERE / 'probe.py').read_text()
    if cfg.get('_operator_tty'):
        try:
            from .operator_overlay import completion_bootstrap
        except ImportError:
            from operator_overlay import completion_bootstrap
        code += completion_bootstrap(HERE.parent.parent)
        code += '\n' + (HERE / 'operator_overlay.py').read_text()
    code += '\n_monitor_tasks = json.loads(' + repr(json.dumps(request)) + ')\n_monitor_raw = collect(_monitor_tasks)\n'
    if cfg.get('_operator_tty'):
        code += "_monitor_raw['_operator_overlay'] = collect_operator_overlay(_monitor_tasks['tasks'])\n"
    code += 'print(json.dumps(_monitor_raw))\n'
    if host.get('transport', 'ssh') == 'local':
        command = [sys.executable, '-B', '-']
    else:
        command = ssh_command(host, shlex.join([host.get('python', 'python3'), '-B', '-']),
                              password_auth=bool(host.get('password')))
    try:
        attempts = int(cfg.get('connection_attempts', 1)) if host.get('transport', 'ssh') == 'ssh' else 1
        for attempt in range(attempts):
            remaining = deadline - time.monotonic() if deadline is not None else cfg['timeout_seconds']
            if remaining <= 0:
                return {'error': 'probe round timeout'}
            timeout = min(cfg['timeout_seconds'], remaining)
            try:
                result = (subprocess.run(command, input=code, text=True, capture_output=True,
                                         timeout=timeout) if host.get('transport', 'ssh') == 'local'
                          else run_ssh(host, command, code, timeout))
                if result.returncode:
                    error = f'probe exit {result.returncode}: {result.stderr.strip()[-1000:]}'
                    if any(token in result.stderr.lower() for token in HOST_KEY_ERRORS):
                        # A rotated cloud IP can present a new host key; drop the stale
                        # record and retry so credential swaps stay seamless.
                        remaining = deadline - time.monotonic() if deadline is not None else 10
                        if remaining <= 0:
                            return {'error': 'probe round timeout'}
                        purge_host_key(host, timeout=min(10, remaining))
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
            delay = cfg.get('retry_delay_seconds', 0)
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            time.sleep(delay)
        return {'error': error}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {'error': f'{type(e).__name__}: {e}'}


def stop_task(cfg: dict[str, Any], task: dict[str, Any], reason: str, dry_run: bool,
              state: Path, config_path: Path | None = None, auth_path: Path | None = None) -> int:
    if not reason.strip():
        raise ValueError('a nonempty stop reason is required')
    if any(key in task for key in ('stop', 'marker', 'process_groups')):
        raise ValueError('custom stop contracts are unsupported; use the official controller')
    monitor_argv = ['python3', 'tools/gpu_monitor/monitor.py', 'watch',
                    '--interval', '60', '--max-hours', '12']
    if config_path is not None:
        monitor_argv.extend(['--config', str(config_path)])
    if auth_path is not None:
        monitor_argv.extend(['--auth', str(auth_path)])
    monitor_cmd = shlex.join(monitor_argv)
    verification_cmd = shlex.join(monitor_argv + ['--task', task['id']])
    if not task.get('controller') and not task.get('scheduler'):
        raise ValueError(f"{task['id']}: stopping requires an official controller registration")
    state.mkdir(parents=True, exist_ok=True)
    receipt = {'task': task['id'], 'time': utc(), 'result': 'OFFICIAL_STOP_REQUIRED',
               'reason': reason, 'dry_run': dry_run, 'training_stopped_confirmed': False,
               'controller': task.get('controller'), 'scheduler': task.get('scheduler'),
               'advice': '请调用 research_handoff stop 或 gpu_scheduler cancel；监控器保持只读。',
               'monitor_command': monitor_cmd, 'verification_command': verification_cmd}
    with (state / 'stop-requests.jsonl').open('a') as f:
        f.write(dump(receipt) + '\n')
    print(dump(receipt))
    print('停止后轮询监控命令: ' + monitor_cmd)
    print('单任务复核命令: ' + verification_cmd)
    return 2


def metadata(raw: dict[str, Any], spec: dict[str, Any] | None) -> Any:
    if not spec:
        return None
    entry = raw['files'].get(spec['path'], {})
    try:
        value = json.loads(entry['text'])
        return field(value, spec['key']) if spec.get('key') else value
    except (KeyError, ValueError):
        return None


def read_budget(task: dict[str, Any], raw: dict[str, Any], launch: Any,
                status_doc: dict[str, Any]) -> dict[str, Any]:
    """Select one budget authority; a mismatched official run never uses legacy credit."""
    controller = task.get('controller')
    registered = isinstance(controller, dict) and bool(controller)
    document_official = status_doc.get('controller') == 'autoresearch-longrun'
    official = False
    alerts = []
    mode, source = 'unknown', 'unknown'
    effective = target = limit = wall_limit = deadline = None
    launch = launch if isinstance(launch, dict) else {}
    if registered:
        if controller.get('type') != 'research_handoff' or not document_official:
            if status_doc or controller.get('type') != 'research_handoff':
                alerts.append('CONTROLLER_TYPE_MISMATCH')
        elif status_doc.get('run_id') != controller.get('run_id'):
            alerts.append('CONTROLLER_IDENTITY_MISMATCH')
        else:
            official = True
            source = 'research_handoff'
            budget = status_doc.get('budget')
            budget = budget if isinstance(budget, dict) else {}
            mode = budget.get('mode', 'unknown')
            wall_limit = budget.get('hard_limit_seconds')
            window = budget.get('window_seconds')
            if (mode not in ('active', 'wall') or not finite(window) or window <= 0
                    or not finite(wall_limit) or wall_limit < window):
                alerts.append('OBSERVATION_ERROR')
            if mode == 'active':
                effective, target = budget.get('active_seconds'), window
                if not finite(effective) or effective < 0:
                    alerts.append('OBSERVATION_ERROR')
            deadline = budget.get('hard_deadline_at')
    elif document_official:
        alerts.append('CONTROLLER_TYPE_MISMATCH')
    else:
        source = 'legacy'
        mode = status_doc.get('budget_mode', launch.get('budget_mode', 'wall'))
        if mode == 'effective':
            status_key = task.get('status', {}).get('key', '')
            match = re.fullmatch(r'agents\.(sol|seed)\.status', status_key)
            agent = field(status_doc, f'agents.{match[1]}', {}) if match else {}
            effective = agent.get('effective_seconds') if isinstance(agent, dict) else None
            target = status_doc.get('effective_target_seconds')
            limit = status_doc.get('effective_limit_seconds')
            if not (finite(effective) and effective >= 0 and finite(target)
                    and finite(limit) and 0 < target <= limit):
                alerts.append('OBSERVATION_ERROR')
        else:
            deadline = task.get('deadline_at') or metadata(raw, task.get('deadline_file'))
    deadline_ts = None
    try:
        if deadline is not None:
            deadline_ts = timestamp(deadline)
        if official and finite(wall_limit) and wall_limit > 0:
            started = timestamp(field(status_doc, 'budget.started_at'))
            if started is not None:
                hard_deadline = started + wall_limit
                deadline_ts = min(deadline_ts, hard_deadline) if deadline_ts is not None else hard_deadline
        elif not registered and not document_official and mode != 'effective':
            budget_seconds = field(launch, task.get('budget_key', 'budget_seconds'))
            started = timestamp(field(launch, task.get('started_key', 'started_at')))
            if deadline_ts is None and finite(budget_seconds) and started is not None:
                deadline_ts = started + budget_seconds
    except (ValueError, TypeError, OverflowError):
        alerts.append('OBSERVATION_ERROR')
    return {'mode': mode, 'source': source, 'official': official, 'alerts': alerts,
            'effective': effective if finite(effective) and effective >= 0 else None,
            'target': target if finite(target) and target > 0 else None,
            'limit': limit if finite(limit) and limit > 0 else None,
            'wall_limit': wall_limit if finite(wall_limit) and wall_limit > 0 else None,
            'deadline_ts': deadline_ts}


def _parse_stream_line(spec: dict, line: str, pattern: re.Pattern | None) -> dict | None:
    try:
        if spec.get('format', 'jsonl') == 'jsonl':
            row = json.loads(line)
            if not isinstance(row, dict) or any(field(row, k) != v for k, v in spec.get('where', {}).items()):
                return None
            row = {k: field(row, v) for k, v in spec.get('fields', {'step': 'step', 'elapsed_seconds': 'elapsed_seconds'}).items()}
        elif pattern:
            match = pattern.search(line)
            if not match:
                return None
            row = {k: float(v) for k, v in match.groupdict().items() if v is not None}
        else:
            return None
        return row if any(v is not None for v in row.values()) else None
    except (ValueError, TypeError):
        return None


def _stream_records(spec: dict, entry: dict, old: dict) -> tuple[list[dict], bytes, bool, str]:
    """Reuse complete records only while their exact bytes remain in the tail.

    Cache data stays in process memory, so daily observation files do not grow
    by an extra copy of every parsed log window.  A restarted watch reparses its
    first window.  Each cache is bounded by that window and the LRU entry limit.
    """
    data = (base64.b64decode(entry['raw_b64'], validate=True) if 'raw_b64' in entry
            else entry.get('text', '').encode('utf-8'))
    start = entry.get('offset', max(0, (entry.get('size') or len(data)) - len(data)))
    end = entry.get('read_end', start + len(data))
    signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    cache_key = old.get('_cache_key')
    cached = _STREAM_CACHES.get(cache_key, {})
    same_file = (entry.get('inode') is not None and entry.get('inode') == old.get('inode')
                 and (entry.get('device') is None or old.get('device') is None
                      or entry.get('device') == old.get('device'))
                 and (entry.get('size') or 0) >= (old.get('size') or 0)
                 and not entry.get('missing') and not entry.get('error'))
    if cache_key and cache_key.partition(':')[0] != signature:
        same_file = False
    if cached and cached.get('signature') != signature:
        same_file = False
    compatible = (same_file and cached.get('signature') == signature
                  and cached.get('start', start) <= start <= cached.get('end', -1) <= end)
    if compatible:
        overlap_end = cached['end'] - start
        old_overlap = cached['data'][start - cached['start']:]
        compatible = data[:overlap_end] == old_overlap
        if not compatible:
            same_file = False
    if compatible:
        records = [record for record in cached['records'] if record['start'] >= start]
        partial = cached['partial'] if cached['partial_start'] >= start else b''
        partial_start = cached['partial_start'] if partial else cached['end']
        new_data = partial + data[cached['end'] - start:]
        parse_start = partial_start
    else:
        records, new_data, parse_start = [], data, start
        _STREAM_CACHES.pop(cache_key, None)
        cache_key = signature + ':' + uuid.uuid4().hex
    pattern = re.compile(spec['pattern']) if spec.get('format') == 'regex' else None
    for line in new_data.split(b'\n')[:-1]:
        raw_line = line + b'\n'
        records.append({'start': parse_start, 'end': parse_start + len(raw_line),
                        'row': _parse_stream_line(spec, raw_line.decode('utf-8', errors='replace'), pattern)})
        parse_start += len(raw_line)
    partial = new_data[parse_start - (partial_start if compatible else start):]
    _STREAM_CACHES[cache_key] = {'signature': signature, 'start': start, 'end': end, 'data': data,
                                 'records': records, 'partial_start': parse_start, 'partial': partial}
    _STREAM_CACHES.move_to_end(cache_key)
    while len(_STREAM_CACHES) > MAX_STREAM_CACHE_ENTRIES:
        _STREAM_CACHES.popitem(last=False)
    return records, data, same_file, cache_key


def parse_stream(spec: dict, entry: dict, old: dict, now: float,
                 alive: bool, default_stale: float) -> dict:
    records, data, same_file, cache_key = _stream_records(spec, entry, old)
    text = data.decode('utf-8', errors='replace')
    rows = [record['row'] for record in records if record['row'] is not None]
    errors = []
    fmt = spec.get('format', 'jsonl')
    latest = rows[-1] if rows else {}
    metric = latest.get('metric')
    values = [r['metric'] for r in rows if finite(r.get('metric'))]
    step = latest.get('step')
    previous_step = old.get('latest', {}).get('step')
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
    error_text = '\n'.join(text.splitlines()[-spec.get('error_window_lines', DEFAULT_ERROR_WINDOW_LINES):])
    error_matches = [{'pattern': item['pattern'], 'severity': item.get('severity', 'error')}
                     for item in spec.get('error_patterns', DEFAULT_ERROR_PATTERNS)
                     if re.search(item['pattern'], error_text)]
    if error_matches:
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
            'inode': entry.get('inode'), 'device': entry.get('device'), 'size': entry.get('size'),
            'missing': entry.get('missing', False), '_cache_key': cache_key,
            'tail': text.splitlines()[-spec.get('tail_lines', 3):],
            'error_matches': error_matches, 'alerts': errors}


def evaluate(task: dict[str, Any], host: dict[str, Any],
             previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    result = {'id': task['id'], 'host': task['host'], 'label': task.get('label', task['id']),
              'protocol': task.get('protocol'), 'alerts': []}
    if task.get('controller') is not None:
        result['controller'] = {'type': 'research_handoff', 'run_id': task['controller'].get('run_id')}
    if task.get('scheduler') is not None:
        ids = task['scheduler'].get('job_ids', task['scheduler'].get('job_id', []))
        result['scheduler'] = {'type': 'gpu_scheduler', 'job_ids': [ids] if isinstance(ids, str) else list(ids or [])}
    if host.get('error'):
        result.update(state='UNREACHABLE', observed_at=None, error=host['error'], processes=[], streams=[],
                      last_success_at=previous.get('last_success_at'),
                      last_known_state=previous.get('last_known_state') if previous.get('state') == 'UNREACHABLE' else previous.get('state'))
        result['alerts'] = ['UNREACHABLE']
        result['advice'] = [SUGGESTIONS['UNREACHABLE']]
        return result
    now = host['observed_at']
    raw = host['tasks'][task['id']]
    scheduler_view = raw.get('scheduler_view', {})
    result['active_job'] = scheduler_view.get('active_job')
    result['configured_job_ids'] = scheduler_view.get('configured_job_ids', result.get('scheduler', {}).get('job_ids', []))
    result['scheduler_history'] = scheduler_view.get('attempts', scheduler_view.get('history', []))
    processes = raw['processes']
    alive = bool(processes)
    declared = metadata(raw, task.get('status'))
    launch = metadata(raw, task.get('launch')) or {}
    status_spec = task.get('status', {})
    status_doc = metadata(raw, {'path': status_spec['path']}) if status_spec.get('path') else {}
    status_doc = status_doc if isinstance(status_doc, dict) else {}
    budget_view = read_budget(task, raw, launch, status_doc)
    identity_conflict = bool(IDENTITY_ALERTS.intersection(budget_view['alerts']))
    rc = metadata(raw, task.get('exit'))
    if not isinstance(rc, int) or isinstance(rc, bool):
        rc = None
    states = {'COMPLETED': ['COMPLETED', 'COMPLETE', 'DONE', 'OK', 'SUCCESS'],
              'FAILED': ['FAILED', 'ERROR'], 'STOPPED': ['STOPPED', 'CANCELLED', 'CANCELED']}
    states.update(task.get('terminal_states', {}))
    terminal = next((k for k, vals in states.items() if declared in vals), None) if not identity_conflict else None
    state_source = 'processes'
    if terminal == 'STOPPED' and rc in (None, 0):
        state = terminal
        state_source = 'declared_state'
    elif rc is not None:
        state = 'COMPLETED' if rc == 0 else 'FAILED'
        state_source = 'exit_code'
    elif terminal:
        state = terminal
        state_source = 'declared_state'
    elif alive:
        state = 'PAUSED' if all(p['state'] in ('T', 't') for p in processes) else 'RUNNING'
    elif not identity_conflict and result.get('active_job') and result['active_job'].get('state') in ('QUEUED', 'STARTING', 'RUNNING', 'UNKNOWN'):
        state = result['active_job']['state']
        state_source = 'gpu_scheduler'
    elif not identity_conflict and task.get('scheduler') is not None and declared in ('QUEUED', 'STARTING'):
        state = declared
    elif identity_conflict:
        state = 'UNKNOWN'
        state_source = 'identity_conflict'
    elif declared is not None or rc is not None or any(s.get('text') for s in raw['files'].values()):
        state = 'EXITED_WITHOUT_RESULT'
        result['alerts'].append(state)
    else:
        state = 'NOT_STARTED'
    if (state in TERMINAL and alive) or (rc is not None and terminal is not None and terminal != state):
        result['alerts'].append('STATUS_CONFLICT')
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
    mode = budget_view['mode']
    effective, target, limit = (budget_view[key] for key in ('effective', 'target', 'limit'))
    official = budget_view['official']
    result['alerts'].extend(budget_view['alerts'])
    result['alerts'].extend(scheduler_view.get('alerts', []))
    if official:
        result.update(current_turn=field(status_doc, 'turn.number'),
                      heartbeat_stale=field(status_doc, 'heartbeat.stale'))
    if finite(effective) and finite(target) and effective < target and (state in TERMINAL or state == 'EXITED_WITHOUT_RESULT'):
        result['alerts'].append('EFFECTIVE_TARGET_NOT_REACHED')
    if finite(effective) and finite(limit) and effective > limit:
        result['alerts'].append('EFFECTIVE_LIMIT_EXCEEDED')
    deadline_ts = budget_view['deadline_ts']
    remaining = deadline_ts - now if deadline_ts is not None else None
    effective_remaining = max(0, limit-effective) if finite(effective) and finite(limit) else None
    if mode == 'effective':
        remaining = effective_remaining
    if mode != 'effective' and remaining is not None and remaining <= 0 and (alive or state not in TERMINAL):
        result['alerts'].append('DEADLINE_EXCEEDED')
    prev_streams = {s['id']: s for s in previous.get('streams', [])} if previous.get('boot_id') == host.get('boot_id') else {}
    streams = []
    for spec in task.get('streams', []):
        stream = parse_stream(spec, raw['files'][spec['path']], prev_streams.get(spec['id'], {}),
                              now, alive, task.get('stale_seconds', DEFAULT_STALE_THRESHOLD_SECONDS))
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
    if free is not None and free < task.get('min_disk_free_gib', DEFAULT_MIN_DISK_FREE_GIB) * 1024 ** 3:
        result['alerts'].append('LOW_DISK')
    gpu_ids = set(task.get('gpu_uuids', []))
    own_pids = {str(p['pid']) for p in processes}
    container_ids = set(task.get('gpu_container_ids', []))
    gpu_processes = [dict(p, belongs_to_task=p.get('owner_task_id') == task['id'] if 'owner_task_id' in p else
                         str(p['pid']) in own_pids or bool(container_ids.intersection(p.get('container_ids', []))))
                     for p in host['gpu_processes'].get('rows', []) if uses_gpu and (not gpu_ids or p['gpu_uuid'] in gpu_ids)]
    result.update(state=state, state_source=state_source, declared_state=declared, exit_code=rc, observed_at=utc(now), last_success_at=utc(now),
                  boot_id=host.get('boot_id'), processes=processes, streams=streams,
                  deadline_at=utc(deadline_ts) if deadline_ts is not None else None, budget_remaining_seconds=remaining,
                  budget_mode=mode, budget_source=budget_view['source'], wall_limit_seconds=budget_view['wall_limit'],
                  effective_seconds=effective, effective_target_seconds=target,
                  effective_limit_seconds=limit, effective_remaining_seconds=effective_remaining,
                  effective_target_remaining_seconds=max(0,target-effective) if finite(target) and finite(effective) else None,
                  disk_free_gib=free / 1024 ** 3 if free is not None else None,
                  gpu=[g for g in host['gpu'].get('rows', []) if uses_gpu and (not gpu_ids or g['uuid'] in gpu_ids)],
                  gpu_processes=gpu_processes, observation_errors=file_errors,
                  process_errors=raw.get('process_errors', []),
                  identity_errors=raw['identity_errors'])
    result['alerts'] = sorted(set(result['alerts']))
    result['advice'] = [SUGGESTIONS.get(k, k) for k in result['alerts']]
    return result


def snapshot(cfg: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        from .runtime import endpoint_key, enrich_host
        from .diagnostics import augment
    except ImportError:
        from runtime import endpoint_key, enrich_host
        from diagnostics import augment
    started = time.monotonic()
    old = {t['id']: t for t in (previous or {}).get('tasks', [])}
    grouped = {}
    for task in cfg['tasks']:
        host = cfg['hosts'][task['host']]
        key = endpoint_key(host) if host.get('transport') == 'local' or host.get('hostname') else 'unconfigured:' + task['host']
        group = grouped.setdefault(key, {'host': host, 'aliases': [], 'tasks': []})
        if task['host'] not in group['aliases']:
            group['aliases'].append(task['host'])
        group['tasks'].append(task)
    hosts = {}
    if grouped:
        round_timeout = cfg.get('probe_round_timeout_seconds',
                                cfg['timeout_seconds'] * cfg.get('connection_attempts', 1)
                                + cfg.get('retry_delay_seconds', 0) * (cfg.get('connection_attempts', 1) - 1))
        deadline = time.monotonic() + round_timeout
        pool = ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(grouped)))
        pending = {pool.submit(probe_host, group['host'], group['tasks'], cfg, deadline): key
                   for key, group in grouped.items()}
        try:
            for future in as_completed(pending, timeout=max(0, deadline - time.monotonic())):
                try:
                    hosts[pending[future]] = future.result()
                except Exception as exc:
                    hosts[pending[future]] = {'error': f'probe failed: {type(exc).__name__}'}
        except FuturesTimeoutError:
            pass
        finally:
            for future, key in pending.items():
                if key not in hosts:
                    future.cancel()
                    hosts[key] = {'error': 'probe round timeout'}
            pool.shutdown(wait=False, cancel_futures=True)
    _OPERATOR_OVERLAY.clear()
    by_alias = {}
    for key, raw in hosts.items():
        if cfg.get('_operator_tty'):
            _OPERATOR_OVERLAY.update(raw.pop('_operator_overlay', {}))
        else:
            raw.pop('_operator_overlay', None)
        if not raw.get('error'):
            enrich_host(raw, grouped[key]['tasks'], key, raw.get('observed_at'))
        for alias in grouped[key]['aliases']:
            by_alias[alias] = raw
    evaluated = [evaluate(t, by_alias[t['host']], old.get(t['id'])) for t in cfg['tasks']]
    host_views = {}
    for key, raw in hosts.items():
        group = grouped[key]
        aliases = group['aliases']
        host_tasks = [t for t in evaluated if t['host'] in aliases]
        view = {'endpoint_id': key, 'aliases': aliases, 'probe_count': 1}
        if raw.get('error'):
            view.update(state='UNREACHABLE', error=raw['error'], gpu_count=None, gpus=[])
            host_views[aliases[0]] = view
            continue
        for task in host_tasks:
            source = raw.get('tasks', {}).get(task['id'], {})
            scheduler_view = source.get('scheduler_view', {})
            task['configured_job_ids'] = scheduler_view.get('configured_job_ids', task.get('scheduler', {}).get('job_ids', []))
            task['active_job'] = scheduler_view.get('active_job', source.get('active_job'))
            task['scheduler_history'] = scheduler_view.get('attempts', scheduler_view.get('history', source.get('scheduler_history', [])))
            task['alerts'] = sorted(set(task['alerts'] + scheduler_view.get('alerts', source.get('scheduler_alerts', []))))
            task['gpu_processes'] = [dict(app, belongs_to_task=app.get('owner_task_id') == task['id'])
                                     for app in raw.get('gpu_processes', {}).get('rows', [])
                                     if task.get('uses_gpu', True) and (not next(t for t in group['tasks'] if t['id'] == task['id']).get('gpu_uuids')
                                         or app.get('gpu_uuid') in next(t for t in group['tasks'] if t['id'] == task['id']).get('gpu_uuids', []))]
        task_map = {task['id']: task for task in host_tasks}
        gpus = []
        seen_gpu = set()
        for gpu in raw.get('gpu', {}).get('rows', []):
            gpu_uuid = gpu.get('uuid')
            if gpu_uuid in seen_gpu:
                continue
            seen_gpu.add(gpu_uuid)
            apps = [app for app in raw.get('gpu_processes', {}).get('rows', []) if app.get('gpu_uuid') == gpu_uuid]
            owners, unknown = {}, []
            for app in apps:
                task = task_map.get(app.get('owner_task_id'))
                if task:
                    item = owners.setdefault(task['id'], {'task_id': task['id'], 'label': task.get('label', task['id']),
                        'state': task['state'], 'pids': [], 'memory_used_mib': 0,
                        'source': app.get('source'), 'confidence': app.get('confidence')})
                    item['pids'].append(str(app.get('pid')))
                    try:
                        item['memory_used_mib'] += float(app.get('memory_used_mib') or 0)
                    except (ValueError, TypeError):
                        pass
                else:
                    unknown.append(app)
            def memory_sum(rows):
                total = 0
                for row in rows:
                    try:
                        total += float(row.get('memory_used_mib') or 0)
                    except (ValueError, TypeError):
                        pass
                return total
            measured = {name: gpu.get(name) for name in ('memory_total_mib', 'memory_used_mib', 'utilization_pct', 'temperature_c')}
            reserved = gpu.get('reserved', raw.get('reserved', {}).get(gpu_uuid, {}))
            if not reserved:
                jobs = [source.get('scheduler_view', {}).get('active_job') for source in raw.get('tasks', {}).values()]
                jobs = {j.get('job_id', j.get('id')): j for j in jobs if j and j.get('state') in ('STARTING', 'RUNNING', 'UNKNOWN')
                        and j.get('gpu_uuid') in (None, gpu_uuid)}
                reserved = {name: sum(float(j.get(name) or j.get('resources', {}).get(name) or 0) for j in jobs.values())
                            for name in ('memory_mib', 'compute_units', 'ram_mib', 'cpu_cores')}
            waiting = [t['active_job'] for t in host_tasks if t.get('active_job') and t['active_job'].get('state') == 'QUEUED']
            gpus.append({**gpu, 'endpoint_id': key, 'measured': measured, 'reserved': reserved,
                         'host_resources': raw.get('host_resources', {}),
                         'running_tasks': list(owners.values()), 'unknown_processes': unknown,
                         'owned_memory_mib': sum(item['memory_used_mib'] for item in owners.values()),
                         'unknown_memory_mib': memory_sum(unknown),
                         'external_queue_summary': raw.get('external_queue_summary', {}),
                         'queued': len(waiting),
                         'blocking_reason': ', '.join(sorted({j.get('reason') or 'unknown' for j in waiting}))})
        view.update(state='OK', gpu_count=len(gpus), gpus=gpus, host_resources=raw.get('host_resources', {}),
                    scheduler_summary=raw.get('scheduler_summary', {}),
                    external_queue_summary=raw.get('external_queue_summary', {}),
                    process_query_error=raw.get('gpu_processes', {}).get('error'))
        host_views[aliases[0]] = view
    data = {'schema_version': 2, 'collected_at': utc(), 'read_only': True,
            'interval_seconds': cfg.get('_interval_override', cfg.get('interval_seconds', 60)),
            'hosts': host_views, 'tasks': evaluated}
    if cfg.get('_config_metadata'):
        data['config'] = cfg['_config_metadata']
    augment(data, cfg, previous or {}, time.time(), by_alias,
            formal_unchanged={key: value.get('unchanged', False) for key, value in _OPERATOR_OVERLAY.items()})
    for task in data['tasks']:
        records = {alert['id']: alert for alert in task.get('alert_history', [])}
        prior = {alert['id']: alert for alert in old.get(task['id'], {}).get('alert_history', [])}
        for code in task.get('alerts', []):
            alert_id = task['id'] + ':' + code
            before = prior.get(alert_id, {})
            records[alert_id] = {'id': alert_id, 'task_id': task['id'], 'severity': 'critical'
                if code in ('FAILED', 'NONFINITE', 'STATUS_CONFLICT', 'CONTROLLER_IDENTITY_MISMATCH',
                            'SCHEDULER_IDENTITY_CONFLICT', 'SCHEDULER_LEDGER_INVALID', 'DEADLINE_EXCEEDED') else 'warning',
                'state': 'open', 'first_seen': before.get('first_seen', data['collected_at']),
                'last_seen': data['collected_at'], 'observed_count': before.get('observed_count', 0) + 1,
                'threshold': 1, 'source': 'task_probe', 'evidence': {'rule': code},
                'data_gap': task['state'] == 'UNREACHABLE'}
        for alert_id, before in prior.items():
            if before.get('source') == 'task_probe' and alert_id not in records:
                records[alert_id] = dict(before, state=before.get('state') if task['state'] == 'UNREACHABLE' else 'resolved',
                                         data_gap=task['state'] == 'UNREACHABLE')
        task['alert_history'] = list(records.values())
    data['alerts'] = [alert for task in data['tasks'] for alert in task.get('alert_history', [])]
    data['agents'] = aggregate_agents(data['tasks'])
    for task in data['tasks']:
        for stream in task.get('streams', []):
            stream['latest'] = {key: value for key, value in stream.get('latest', {}).items()
                                if key in ('step', 'elapsed_seconds', 'event', 'state', 'turn', 'generation')}
    gpus = [gpu for host in host_views.values() for gpu in host.get('gpus', [])]
    active_jobs = {task['active_job'].get('request_id') or task['active_job'].get('job_id'): task['active_job']
                   for task in data['tasks'] if task.get('active_job')}
    queue = [job for job in active_jobs.values() if job.get('state') == 'QUEUED']
    waits = [job.get('waiting_seconds', job.get('wait_seconds')) for job in queue if finite(job.get('waiting_seconds', job.get('wait_seconds')))]
    open_alerts = [a for a in data.get('alerts', []) if a.get('state') == 'open']
    data['summary'] = {'critical_alerts': sum(a.get('severity') == 'critical' for a in open_alerts),
        'warning_alerts': sum(a.get('severity') in ('warning', 'error') for a in open_alerts),
        'running_streams': sum(len(task.get('streams') or [None]) for task in data['tasks'] if task['state'] == 'RUNNING'),
        'gpu_occupied': sum(bool(g.get('running_tasks') or g.get('unknown_processes')) for g in gpus),
        'gpu_total': len(gpus) if not any(h.get('error') for h in host_views.values()) else None,
        'scheduler': {'running': sum(j.get('state') in ('STARTING', 'RUNNING') for j in active_jobs.values()),
                      'queued': len(queue), 'oldest_wait_seconds': max(waits) if waits else None},
        'collection_seconds': round(time.monotonic() - started, 3)}
    return public_record(data, [host.get('password') for host in cfg['hosts'].values()])


def aggregate_agents(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the official-controller view from one probe snapshot.

    This is deliberately a pure projection: lifecycle state still comes from
    the registered task probe, while controller/scheduler identities are
    retained as provenance for operators and machine consumers.
    """
    agents = []
    for task in tasks:
        controller = task.get('controller')
        scheduler = task.get('scheduler')
        if controller is None and scheduler is None:
            continue
        identity = {}
        if controller is not None:
            identity['controller'] = {'type': 'research_handoff', 'run_id': controller.get('run_id')}
        if scheduler is not None:
            ids = scheduler.get('job_ids', scheduler.get('job_id', []))
            identity['scheduler'] = {'type': 'gpu_scheduler', 'job_ids': [ids] if isinstance(ids, str) else list(ids or [])}
        agents.append({
            'id': task['id'], 'label': task.get('label', task['id']), 'host': task.get('host'),
            'state': task.get('state', 'UNKNOWN'), 'observed_at': task.get('observed_at'),
            'alerts': list(task.get('alerts', [])), 'processes': task.get('processes', []),
            **{key: task[key] for key in ('budget_mode', 'effective_seconds', 'effective_target_seconds',
                'effective_target_remaining_seconds', 'deadline_at', 'budget_remaining_seconds',
                'current_turn', 'heartbeat_stale', 'timing', 'active_job', 'configured_job_ids', 'timeline_12h') if key in task},
            **identity,
        })
    return agents


def atomic_json(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(dump(value) + '\n')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load_latest(state: Path) -> dict[str, Any]:
    try:
        return json.loads((state / 'latest.json').read_text())
    except (FileNotFoundError, ValueError):
        return {}


def persist(state: Path, data: dict[str, Any], previous: dict[str, Any]) -> None:
    data = public_record(data)
    state.mkdir(parents=True, exist_ok=True)
    atomic_json(state / 'latest.json', data)
    day = data['collected_at'][:10]
    with (state / f'observations-{day}.jsonl').open('a') as f:
        f.write(dump(data) + '\n')
    old = {t['id']: t for t in previous.get('tasks', [])}
    with (state / f'events-{day}.jsonl').open('a') as f:
        for event in data.get('config_events', []):
            f.write(dump(event) + '\n')
        for task in data['tasks']:
            before = old.get(task['id'], {})
            if (task['state'], task['alerts']) != (before.get('state'), before.get('alerts')):
                f.write(dump({'time': data['collected_at'], 'task': task['id'],
                              'previous_state': before.get('state'), 'state': task['state'],
                              'alerts': task['alerts'], 'advice': task['advice']}) + '\n')
        prior = {a['id']: a for a in previous.get('alerts', [])}
        for alert in data.get('alerts', []):
            before = prior.get(alert['id'], {})
            if (alert.get('state'), alert.get('severity'), alert.get('observed_count')) != (before.get('state'), before.get('severity'), before.get('observed_count')):
                f.write(dump({'event': 'ALERT_UPDATED', 'time': data['collected_at'], 'alert': alert}) + '\n')


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


def render(data, color='auto'):
    return render_unified(data, color)


def refresh_auth(cfg: dict[str, Any], auth_path: Path) -> None:
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
        updated = apply_auth(host, auth)
        if any(host.get(key) != updated[key] for key in ('hostname', 'user', 'port')):
            purge_host_key(updated)
        cfg['hosts'][name] = updated


def all_tasks_terminal(tasks: list[dict[str, Any]]) -> bool:
    return all(task['state'] in TERMINAL and not task['processes']
               and not UNCERTAIN_LIFECYCLE_ALERTS.intersection(task.get('alerts', [])) for task in tasks)


def watch(cfg, state, interval, max_hours, max_polls, until_terminal, token=None,
          auth_path=None, config_path=None, task_ids=(), config_check_interval=60,
          auth_check_interval=60, interval_override=None, color='auto', json_output=False):
    try:
        from .reload import ConfigReloader, AuthReloader
    except ImportError:
        from reload import ConfigReloader, AuthReloader
    initial_auth = next(({'host': h['hostname'], 'user': h['user'], 'password': h['password'], 'port': h.get('port', 22)}
                         for h in cfg['hosts'].values() if h.get('password')), None)
    auth_reloader = AuthReloader(auth_path, initial_auth, load_auth, apply_auth, purge_host_key) if auth_path else None
    reloader = ConfigReloader(config_path, cfg, load_config, task_ids,
                              auth_getter=lambda: auth_reloader.current if auth_reloader else None) if config_path else None
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'watch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('a monitor already owns this state directory')
        token = token or uuid.uuid4().hex
        info = {'pid': os.getpid(), 'token': token, 'started_at': utc(),
                'deadline_at': utc(time.time() + max_hours * 3600), 'state': 'RUNNING', 'read_only': True}
        if reloader:
            info['config'] = reloader.metadata()
        atomic_json(state / 'watch.json', info)
        stop_path = state / 'stop-monitor.json'
        end = time.monotonic() + max_hours * 3600
        next_probe_at = time.monotonic()
        next_config_check_at = next_probe_at + config_check_interval
        next_auth_check_at = next_probe_at + auth_check_interval
        count, reason, events = 0, 'MONITOR_TIME_LIMIT', []
        try:
            while time.monotonic() < end:
                try:
                    stop = json.loads(stop_path.read_text())
                    if stop.get('token') == token:
                        reason = 'LOCAL_MONITOR_STOP_REQUESTED'
                        break
                except (FileNotFoundError, ValueError):
                    pass
                now = time.monotonic()
                if reloader and now >= next_config_check_at:
                    events.extend(reloader.check(count + 1))
                    cfg = reloader.current
                    next_config_check_at = time.monotonic() + config_check_interval
                if auth_reloader and now >= next_auth_check_at:
                    events.extend(auth_reloader.check(cfg))
                    next_auth_check_at = time.monotonic() + auth_check_interval
                if now >= next_probe_at:
                    probe_started = time.monotonic()
                    current = dict(reloader.selected_config() if reloader else cfg)
                    current['_operator_tty'] = sys.stdout.isatty() and not json_output
                    current['_interval_override'] = interval_override if interval_override is not None else current.get('interval_seconds', interval)
                    current['probe_round_timeout_seconds'] = min(current.get('probe_round_timeout_seconds', current.get('timeout_seconds', 30)),
                                                                 max(0.001, end - probe_started))
                    previous = load_latest(state)
                    data = snapshot(current, previous)
                    if reloader:
                        data['config'] = reloader.metadata()
                    if events:
                        data['config_events'] = public_record(events)
                        events = []
                    for task_id in data.get('config', {}).get('missing_task_ids', []):
                        data['alerts'].append({'id': 'CONFIG_TASK_MISSING:' + task_id, 'task_id': task_id,
                            'severity': 'critical', 'state': 'open', 'first_seen': data['collected_at'],
                            'last_seen': data['collected_at'], 'observed_count': 1, 'threshold': 1,
                            'source': 'config', 'evidence': {'task_id': task_id}, 'data_gap': True})
                    if data.get('config', {}).get('reload_state') in ('FAILED', 'RESTART_REQUIRED'):
                        prior = next((a for a in previous.get('alerts', []) if a.get('id') == 'CONFIG_RELOAD_FAILED'), {})
                        data['alerts'].append({'id': 'CONFIG_RELOAD_FAILED', 'severity': 'warning', 'state': 'open',
                            'first_seen': prior.get('first_seen', data['collected_at']), 'last_seen': data['collected_at'],
                            'observed_count': prior.get('observed_count', 0) + 1, 'threshold': 1, 'source': 'config',
                            'evidence': {'error': data['config']['last_reload_error']}, 'data_gap': True})
                    summary = data.setdefault('summary', {})
                    summary['critical_alerts'] = sum(a.get('severity') == 'critical' and a.get('state') == 'open' for a in data['alerts'])
                    summary['warning_alerts'] = sum(a.get('severity') in ('warning', 'error') and a.get('state') == 'open' for a in data['alerts'])
                    persist(state, data, previous)
                    if current['_operator_tty']:
                        print('\033[2J\033[H', end='')
                        render_unified(data, color, _OPERATOR_OVERLAY)
                    else:
                        print(dump(public_record(data)), flush=True)
                    _OPERATOR_OVERLAY.clear()
                    count += 1
                    info.update(last_poll_at=data['collected_at'], polls=count, config=data.get('config'),
                                auth=auth_reloader.metadata() if auth_reloader else None)
                    atomic_json(state / 'watch.json', info)
                    if max_polls and count >= max_polls:
                        reason = 'POLL_LIMIT'
                        break
                    if until_terminal and data['tasks'] and all_tasks_terminal(data['tasks']):
                        reason = 'ALL_TASKS_TERMINAL'
                        break
                    next_probe_at = probe_started + current['_interval_override']
                wake = min(end, next_probe_at,
                           next_config_check_at if reloader else end,
                           next_auth_check_at if auth_reloader else end)
                time.sleep(min(0.5, max(0, wake - time.monotonic())))
        except KeyboardInterrupt:
            reason = 'KEYBOARD_INTERRUPT'
        except BaseException:
            reason = 'MONITOR_ERROR'
            raise
        finally:
            _OPERATOR_OVERLAY.clear()
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
    p.add_argument('--view', help=argparse.SUPPRESS)
    p.add_argument('--color', choices=['auto', 'always', 'never'], default='auto',
                   help='terminal colors for human output; JSON is never colored')
    p.add_argument('--interval', type=float)
    p.add_argument('--config-check-interval', type=float, default=60)
    p.add_argument('--auth-check-interval', type=float, default=60)
    p.add_argument('--max-hours', type=float)
    p.add_argument('--max-polls', type=int)
    p.add_argument('--until-terminal', action='store_true')
    p.add_argument('--check', action='store_true', help='status exit 2 if alerts exist')
    p.add_argument('--dry-run', action='store_true', help='preview stop-task without remote changes')
    p.add_argument('--reason', help='required for an explicit stop-task request')
    p.add_argument('--token', help=argparse.SUPPRESS)
    a = p.parse_args()
    try:
        if a.view is not None or a.action == 'status' and not a.json:
            raise ValueError('旧分屏/人类 status 已取消；请使用 python3 tools/gpu_monitor/monitor.py watch（无 --view）；机器读取使用 status --json')
        auth_path = a.auth.resolve()
        needs_remote = a.action in ('status', 'watch', 'maintain')
        # 凭据文件是唯一的认证来源；只有需要连接远端时才强制要求它存在。
        cfg = load_config(a.config.resolve())
        full_cfg = cfg
        if a.task:
            unknown = set(a.task) - {t['id'] for t in cfg['tasks']}
            if unknown:
                raise ValueError(f'unknown task IDs: {sorted(unknown)}')
            cfg = dict(cfg, tasks=[t for t in cfg['tasks'] if t['id'] in a.task])
        active_hosts = {task['host'] for task in cfg['tasks']}
        if needs_remote and any(host.get('transport', 'ssh') == 'ssh'
                                for name, host in cfg['hosts'].items() if name in active_hosts):
            auth = load_auth(auth_path)
            for name in active_hosts:
                if cfg['hosts'][name].get('transport', 'ssh') == 'ssh':
                    cfg['hosts'][name] = apply_auth(cfg['hosts'][name], auth)
            prepare_ssh({'hosts': {name: cfg['hosts'][name] for name in active_hosts}})
        state = (a.config.resolve().parent / cfg['state_dir']).resolve()
        # A configuration owns one collector and one history, including fixed task selections.
        interval = a.interval if a.interval is not None else cfg['interval_seconds']
        hours = a.max_hours if a.max_hours is not None else cfg['max_hours']
        if (not finite(interval) or interval < 1 or not finite(hours) or not 0 < hours <= 12
                or not finite(a.config_check_interval) or a.config_check_interval <= 0
                or not finite(a.auth_check_interval) or a.auth_check_interval <= 0
                or (a.max_polls is not None and a.max_polls < 1)):
            raise ValueError('interval >= 1, 0 < max-hours <= 12, positive check intervals and max-polls >= 1 required')
        if a.action == 'validate':
            print(dump({'valid': True, 'tasks': [t['id'] for t in cfg['tasks']], 'state_dir': str(state)}))
        elif a.action == 'stop-task':
            if not a.task or len(a.task) != 1 or not a.reason or not a.reason.strip():
                raise ValueError('stop-task requires exactly one --task and a --reason')
            return stop_task(cfg, cfg['tasks'][0], a.reason, a.dry_run, state, a.config.resolve(), auth_path)
        elif a.action == 'status':
            data = snapshot(cfg, load_latest(state))
            print(dump(public_record(data)))
            if a.check and (any(t['alerts'] for t in data['tasks']) or
                            any(alert.get('state') == 'open' for alert in data.get('alerts', []))):
                return 2
        elif a.action == 'watch':
            if not cfg['tasks']:
                print('没有活动任务，无需启动监控器。')
                return 0
            watch(full_cfg, state, interval, hours, a.max_polls, a.until_terminal, a.token,
                  auth_path, a.config.resolve(), a.task or (), a.config_check_interval,
                  a.auth_check_interval, a.interval, a.color, a.json)
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
                       '--max-hours', str(hours), '--token', token,
                       '--config-check-interval', str(a.config_check_interval),
                       '--auth-check-interval', str(a.auth_check_interval)]
            if a.interval is not None:
                command += ['--interval', str(a.interval)]
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
        print(f'gpu-monitor: {public_record(str(e))}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
