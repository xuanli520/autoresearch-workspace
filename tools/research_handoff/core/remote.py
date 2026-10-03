"""Short SSH control requests to an independently running cloud controller."""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from longrun import ControllerError, positive_number, read_json, validate_config


def _monitor_module():
    try:
        from tools.gpu_monitor import monitor
    except ModuleNotFoundError:
        workspace = Path(__file__).resolve().parents[3]
        if str(workspace) not in sys.path:
            sys.path.insert(0, str(workspace))
        from tools.gpu_monitor import monitor
    return monitor


def connection_config(path: Path) -> dict:
    value = read_json(path)
    defaults = {'port': 22, 'python': 'python3', 'connect_timeout_seconds': 10,
                'command_timeout_seconds': 30, 'identity_file': None, 'known_hosts_file': None,
                'auth_file': None}
    allowed = set(defaults) | {'host', 'user', 'controller_dir', 'state_dir', 'data_mount'}
    if not isinstance(value, dict) or set(value)-allowed:
        raise ControllerError('invalid SSH connection config fields')
    cfg = {**defaults, **value}
    import re
    for name in ('host', 'user'):
        pattern = r'[A-Za-z0-9_.:%\[\]-]+' if name == 'host' else r'[A-Za-z0-9_.-]+'
        if not isinstance(cfg.get(name), str) or not re.fullmatch(pattern, cfg[name]) or cfg[name].startswith('-'):
            raise ControllerError(f'invalid SSH {name}')
    for name in ('controller_dir', 'state_dir', 'data_mount'):
        if not isinstance(cfg.get(name), str) or not Path(cfg[name]).is_absolute() or '..' in Path(cfg[name]).parts:
            raise ControllerError(f'SSH {name} requires an absolute path')
    if type(cfg['port']) is not int or not 1 <= cfg['port'] <= 65535:
        raise ControllerError('invalid SSH port')
    if not isinstance(cfg['python'], str) or not cfg['python'] or cfg['python'].startswith('-') or '\0' in cfg['python']:
        raise ControllerError('invalid remote python executable')
    for name in ('connect_timeout_seconds', 'command_timeout_seconds'):
        cfg[name] = positive_number(cfg[name], name)
        if cfg[name] > 60:
            raise ControllerError(f'{name} must be <= 60')
    for name in ('identity_file', 'known_hosts_file', 'auth_file'):
        if cfg[name] is not None and not isinstance(cfg[name], str):
            raise ControllerError(f'{name} must be a path')
    if cfg['auth_file'] is not None:
        cfg['auth_file'] = str((path.parent / cfg['auth_file']).resolve())
        if cfg['identity_file'] is not None:
            raise ControllerError('choose auth_file or identity_file, not both')
    return cfg


def _password_host(cfg):
    monitor = _monitor_module()
    auth = monitor.load_auth(Path(cfg['auth_file']))
    return monitor.apply_auth({'transport': 'ssh', 'hostname': cfg['host'],
                               'user': cfg['user'], 'port': cfg['port'],
                               'connect_timeout_seconds': int(cfg['connect_timeout_seconds'])}, auth)


def ssh_command(cfg, argv):
    if cfg.get('auth_file'):
        monitor = _monitor_module()
        return monitor.ssh_command(_password_host(cfg), shlex.join(argv), password_auth=True)
    args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', f"ConnectTimeout={max(1, int(cfg['connect_timeout_seconds']))}",
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2', '-p', str(cfg['port'])]
    if cfg['identity_file']:
        args += ['-i', cfg['identity_file']]
    if cfg['known_hosts_file']:
        args += ['-o', 'UserKnownHostsFile='+cfg['known_hosts_file']]
    return [*args, cfg['user']+'@'+cfg['host'], shlex.join(argv)]


def call(cfg, argv, payload):
    try:
        data = json.dumps(payload)
        if cfg.get('auth_file'):
            monitor = _monitor_module()
            result = monitor.run_ssh(_password_host(cfg), ssh_command(cfg, argv), data,
                                     timeout=cfg['command_timeout_seconds'])
        else:
            result = subprocess.run(ssh_command(cfg, argv), input=data.encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=cfg['command_timeout_seconds'])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError('SSH result UNKNOWN; inspect status before repeating a mutation: '+type(exc).__name__) from exc
    stderr = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else str(result.stderr or '')
    stdout = result.stdout.decode(errors='replace') if isinstance(result.stdout, bytes) else str(result.stdout or '')
    if result.returncode == 255:
        raise ControllerError('SSH result UNKNOWN; inspect status before retrying. '+stderr[-2048:])
    try:
        reply = json.loads(stdout)
    except ValueError as exc:
        raise ControllerError('invalid remote response; inspect remote state before retrying') from exc
    if not isinstance(reply, dict):
        raise ControllerError('remote response is not an object; inspect state before retrying')
    return reply, result.returncode


# Deployment receives only the explicit bundle whitelist. No user text is interpolated into shell code.
INSTALL = r'''
import base64, hashlib, json, os, pathlib, sys, tempfile
p = json.load(sys.stdin)
root, mount = pathlib.Path(p['root']).resolve(), pathlib.Path(p['mount']).resolve(strict=True)
if not mount.is_mount() or mount.stat().st_dev == pathlib.Path('/').stat().st_dev:
    raise ValueError('data_mount must be a separate mounted data device')
if not root.is_relative_to(mount) or root.exists():
    raise ValueError('release must be a new directory inside data_mount')
ancestor = root.parent
while not ancestor.exists(): ancestor = ancestor.parent
if ancestor.stat().st_dev != mount.stat().st_dev: raise ValueError('release device mismatch')
root.mkdir(parents=True, mode=0o700)
for name, encoded in p['files'].items():
    relative = pathlib.PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts: raise ValueError('invalid bundle path')
    data = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(data).hexdigest() != p['manifest']['files'][name]: raise ValueError('hash mismatch')
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as stream: stream.write(data)
    target.chmod(0o755 if name in p['manifest']['executables'] else 0o644)
(root/'CONTROLLER_MANIFEST.json').write_text(json.dumps(p['manifest'], indent=2)+'\n')
print(json.dumps({'installed': str(root), 'files': len(p['files']), 'launch_performed': False}))
'''


def remote_dispatch(args):
    cfg = connection_config(args.remote)
    if args.state_dir:
        if not Path(args.state_dir).is_absolute():
            raise ControllerError('remote --state-dir must be absolute')
        cfg['state_dir'] = args.state_dir
    if args.action == 'deploy':
        from bundle import render, make_manifest
        files = render()
        reply, code = call(cfg, [cfg['python'], '-B', '-c', INSTALL], {
            'root': cfg['controller_dir'], 'mount': cfg['data_mount'], 'manifest': make_manifest(files),
            'files': {name: base64.b64encode(data).decode() for name, data in files.items()}})
        print(json.dumps(reply, ensure_ascii=False, indent=2))
        return code
    if args.action in ('guard', 'run') or getattr(args, 'no_guard', False):
        raise ControllerError('remote control requires start --background with the cloud guard')
    values = {name: str(value) if isinstance(value, Path) else value for name, value in vars(args).items()}
    values.update(remote=None, state_dir=cfg['state_dir'])
    payload = {'args': values, 'data_mount': cfg['data_mount']}
    if args.action == 'docker-network':
        payload['network_config'] = read_json(args.config)
    if args.action == 'start':
        values['background'] = True
    if args.action in ('init', 'amend'):
        config = validate_config(read_json(args.config))
        if not Path(config['root']).is_absolute():
            raise ControllerError('remote task root must be absolute')
        if config['storage']['data_mount'] not in (None, cfg['data_mount']):
            raise ControllerError('task and connection data_mount differ')
        config['storage']['data_mount'] = cfg['data_mount']
        payload['config'] = config
        if args.action == 'amend' and args.credit_file:
            if args.credit_file.stat().st_size > 1048576:
                raise ControllerError('credit file exceeds 1 MiB')
            payload['credit'] = read_json(args.credit_file)
    if args.action == 'context' and args.context_action in ('compact', 'reopen'):
        if args.summary_file.stat().st_size > 131072:
            raise ControllerError('summary file too large')
        payload['summary'] = args.summary_file.read_text(encoding='utf-8')
    command = [cfg['python'], '-B', str(Path(cfg['controller_dir'])/'core/rpc.py')]
    if args.action == 'watch':
        if not .05 <= args.interval <= 3600 or args.seconds is not None and not 0 < args.seconds <= 43200:
            raise ControllerError('invalid watch interval/seconds')
        values['action'] = 'status'
        started = time.monotonic()
        while True:
            try:
                reply, code = call(cfg, command, payload)
                from controller import format_status
                print(json.dumps(reply, ensure_ascii=False) if args.json or code else format_status(reply), flush=True)
                if code or not reply['controller_alive']:
                    return code
            except ControllerError as exc:
                print(json.dumps({'observed_status': 'UNKNOWN', 'error': str(exc)}), flush=True)
            if args.seconds is not None and time.monotonic()-started >= args.seconds:
                return 0
            time.sleep(args.interval)
    reply, code = call(cfg, command, payload)
    print(json.dumps(reply, ensure_ascii=False, indent=2))
    return code
