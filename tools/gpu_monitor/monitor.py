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
    from .askpass import (FIFO_IDENTITY_ENV, FIFO_PATH_ENV, MAX_PASSWORD_BYTES,
                          inspect_fifo, open_password_fifo, validate_helper)
except ImportError:  # Preserve direct script execution.
    from probe import field
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


def load_config(path: Path, auth: dict[str, Any] | None = None) -> dict[str, Any]:
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
        if task['host'] not in cfg['hosts'] or not Path(task['root']).is_absolute():
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
    code = (HERE / 'probe.py').read_text() + '\nprint(json.dumps(collect(json.loads(' + repr(json.dumps(request)) + '))))\n'
    if host.get('transport', 'ssh') == 'local':
        command = [sys.executable, '-']
    else:
        command = ssh_command(host, shlex.join([host.get('python', 'python3'), '-']),
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
    monitor_argv = ['python3', 'tools/gpu_monitor/monitor.py', 'watch', '--view', 'agents',
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
    if rc is not None:
        state = 'COMPLETED' if rc == 0 else 'FAILED'
        state_source = 'exit_code'
    elif terminal:
        state = terminal
        state_source = 'declared_state'
    elif alive:
        state = 'PAUSED' if all(p['state'] in ('T', 't') for p in processes) else 'RUNNING'
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
    if official:
        result.update(current_turn=field(status_doc, 'turn.number'),
                      heartbeat_stale=field(status_doc, 'heartbeat.stale'),
                      scientific_score=field(status_doc, 'completion.scientific_score'))
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
    gpu_processes = [dict(p, belongs_to_task=p['pid'] in own_pids or bool(container_ids.intersection(p.get('container_ids', []))))
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
    result['advice'] = [SUGGESTIONS[k] for k in result['alerts']]
    return result


def snapshot(cfg: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    old = {t['id']: t for t in (previous or {}).get('tasks', [])}
    grouped = {name: tasks for name in cfg['hosts']
               if (tasks := [t for t in cfg['tasks'] if t['host'] == name])}
    hosts = {}
    if grouped:
        round_timeout = cfg.get('probe_round_timeout_seconds',
                                cfg['timeout_seconds'] * cfg.get('connection_attempts', 1)
                                + cfg.get('retry_delay_seconds', 0) * (cfg.get('connection_attempts', 1) - 1))
        deadline = time.monotonic() + round_timeout
        pool = ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(grouped)))
        pending = {pool.submit(probe_host, cfg['hosts'][name], tasks, cfg, deadline): name
                   for name, tasks in grouped.items()}
        try:
            for future in as_completed(pending, timeout=max(0, deadline - time.monotonic())):
                try:
                    hosts[pending[future]] = future.result()
                except Exception as exc:
                    hosts[pending[future]] = {'error': f'probe failed: {type(exc).__name__}'}
        except FuturesTimeoutError:
            pass
        finally:
            for future, name in pending.items():
                if name not in hosts:
                    future.cancel()
                    hosts[name] = {'error': 'probe round timeout'}
            # The context manager waits for every worker even after as_completed
            # times out. Subprocesses share the deadline; queued work is cancelled.
            pool.shutdown(wait=False, cancel_futures=True)
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
    agents = aggregate_agents(evaluated)
    return {'schema_version': 1, 'collected_at': utc(), 'read_only': True,
            'hosts': host_views, 'tasks': evaluated, 'agents': agents}


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
                'current_turn', 'heartbeat_stale', 'scientific_score') if key in task},
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
        elif t.get('budget_mode') == 'active':
            print(f"  进程   {pids}    已确认有效 {duration(t.get('effective_seconds'))} / 目标 {duration(t.get('effective_target_seconds'))}")
            print(f"  预算   距有效目标 {duration(t.get('effective_target_remaining_seconds'))}    距墙钟截止 {duration(t.get('budget_remaining_seconds'))}")
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


def render_agents(data, color='auto'):
    """Render the official research_handoff/gpu_scheduler task registry."""
    colors = Colors(_color_enabled(color))
    agents = data.get('agents') or aggregate_agents(data.get('tasks', []))
    print()
    print(colors.bold(colors.blue('AutoResearch 官方长时间 Agent 总览')))
    print(colors.dim(f"采集时间 {data.get('collected_at', '?')}   只读模式   Agent {len(agents)} 个"))
    for agent in agents:
        state = _state_style(colors, agent.get('state', 'UNKNOWN'))
        refs = []
        if agent.get('controller'):
            refs.append(f"research_handoff:{agent['controller'].get('run_id')}")
        if agent.get('scheduler'):
            refs.append('gpu_scheduler:' + ','.join(agent['scheduler'].get('job_ids', [])))
        print(f"  {colors.bold(agent.get('id', '?'))}  {state}  主机 {agent.get('host', '?')}")
        print(f"    官方身份  {'; '.join(refs) or '?'}   进程 {len(agent.get('processes', []))} 个")
        if agent.get('budget_mode') == 'active':
            print(f"    有效时间  {duration(agent.get('effective_seconds'))}/{duration(agent.get('effective_target_seconds'))}"
                  f"   墙钟余额 {duration(agent.get('budget_remaining_seconds'))}   当前轮 {agent.get('current_turn', '?')}")
        if agent.get('alerts'):
            print(colors.yellow('    告警  ' + ' '.join(agent['alerts'])))
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
    if view == 'agents':
        return render_agents(data, color)
    return render_gpu(data, color)


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


def watch(cfg, state, interval, max_hours, max_polls, until_terminal, token=None, auth_path=None, view='gpu'):
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
                render(data, view=view)
                count += 1
                info.update(last_poll_at=data['collected_at'], polls=count)
                atomic_json(state / 'watch.json', info)
                if max_polls and count >= max_polls:
                    reason = 'POLL_LIMIT'
                    break
                if until_terminal and all_tasks_terminal(data['tasks']):
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
    p.add_argument('--view', choices=['gpu', 'tasks', 'agents'], default='gpu',
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
        needs_remote = a.action in ('status', 'watch', 'maintain')
        # 凭据文件是唯一的认证来源；只有需要连接远端时才强制要求它存在。
        cfg = load_config(a.config.resolve())
        if a.task:
            unknown = set(a.task) - {t['id'] for t in cfg['tasks']}
            if unknown:
                raise ValueError(f'unknown task IDs: {sorted(unknown)}')
            cfg['tasks'] = [t for t in cfg['tasks'] if t['id'] in a.task]
        active_hosts = {task['host'] for task in cfg['tasks']}
        if needs_remote and any(host.get('transport', 'ssh') == 'ssh'
                                for name, host in cfg['hosts'].items() if name in active_hosts):
            auth = load_auth(auth_path)
            for name in active_hosts:
                if cfg['hosts'][name].get('transport', 'ssh') == 'ssh':
                    cfg['hosts'][name] = apply_auth(cfg['hosts'][name], auth)
            prepare_ssh({'hosts': {name: cfg['hosts'][name] for name in active_hosts}})
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
            if not a.task or len(a.task) != 1 or not a.reason or not a.reason.strip():
                raise ValueError('stop-task requires exactly one --task and a --reason')
            return stop_task(cfg, cfg['tasks'][0], a.reason, a.dry_run, state, a.config.resolve(), auth_path)
        elif a.action == 'status':
            data = snapshot(cfg, load_latest(state))
            print(dump(data)) if a.json else render(data, a.color, a.view)
            if a.check and any(t['alerts'] for t in data['tasks']):
                return 2
        elif a.action == 'watch':
            if not cfg['tasks']:
                print('没有活动任务，无需启动监控器。')
                return 0
            watch(cfg, state, interval, hours, a.max_polls, a.until_terminal, a.token, auth_path, a.view)
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
                       '--interval', str(interval), '--max-hours', str(hours), '--token', token, '--view', a.view]
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
