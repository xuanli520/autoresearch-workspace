"""Explicit restoration of an expired task-owned Docker/containerd runtime."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from .common import PROCESS_SOURCE, atomic_json
from tools.research_handoff.core.longrun import check_storage
try:
    from tools.process_control import processes
except ImportError:
    from tools.research_handoff.core import processes


def read(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def load_plan(path: str | Path, *, require_future: bool = True) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = read(path)
    expected = {'version', 'root', 'data_mount', 'runtime_contract', 'runtime_launch',
                'docker_config', 'containerd_config', 'deadline_epoch', 'authorization',
                'unit_prefix', 'source_sha256'}
    if set(plan) != expected or type(plan['version']) is not int or plan['version'] != 1:
        raise ValueError('invalid runtime restoration plan')
    if not isinstance(plan['authorization'], str) or not plan['authorization'].strip():
        raise ValueError('runtime restoration requires explicit authorization')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', plan['unit_prefix']):
        raise ValueError('invalid runtime unit prefix')
    for name in ('root', 'data_mount', 'runtime_contract', 'runtime_launch',
                 'docker_config', 'containerd_config'):
        if not isinstance(plan[name], str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', plan[name]):
            raise ValueError('runtime paths must be absolute without systemd expansion')
    if (type(plan['deadline_epoch']) not in (int, float) or not math.isfinite(plan['deadline_epoch'])
            or require_future and not 0 < plan['deadline_epoch'] - time.time() <= 172800):
        raise ValueError('runtime deadline must be in the future, within 48 hours')
    hashes = plan['source_sha256']
    required = {plan[k] for k in ('runtime_contract', 'runtime_launch', 'docker_config', 'containerd_config')}
    required.update(str(p.resolve()) for p in Path(__file__).parent.glob('*.py'))
    required.update(str(p.resolve()) for p in (
        PROCESS_SOURCE, PROCESS_SOURCE.with_name('__init__.py'),
        Path(__file__).parents[1] / 'research_handoff/core/processes.py',
        Path(__file__).parents[1] / 'research_handoff/core/longrun.py'))
    if not isinstance(hashes, dict) or not required.issubset(hashes):
        raise ValueError('runtime plan must pin code and all original runtime inputs')
    for source, expected_hash in hashes.items():
        check_storage(plan['data_mount'], Path(source), Path(path), Path(plan['root']))
        if hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f'immutable runtime input changed: {source}')
    contract = read(plan['runtime_contract'])
    docker = read(plan['docker_config'])
    containerd = tomllib.loads(Path(plan['containerd_config']).read_text())
    state = Path(contract['runtime_state']).resolve()
    store = Path(contract['runtime_root']).resolve() / 'image-store'
    if (docker['data-root'] != str(store / 'docker')
            or containerd['root'] != str(store / 'containerd')
            or docker['hosts'] != [contract['docker_host']]
            or contract['docker_host'] != 'unix://' + str(state / 'docker.sock')
            or containerd['grpc']['address'] != str(state / 'containerd.sock')
            or docker['containerd'] != containerd['grpc']['address']
            or docker['bridge'] == 'docker0'):
        raise ValueError('runtime storage/socket/isolation contract differs')
    paths = [docker[k] for k in ('data-root', 'exec-root', 'pidfile')]
    paths += [containerd[k] for k in ('root', 'state')]
    paths += [state, store, Path(contract['recovery_root'])]
    check_storage(plan['data_mount'], *map(Path, paths))
    if (not Path(docker['exec-root']).resolve().is_relative_to(state)
            or not Path(containerd['state']).resolve().is_relative_to(state)):
        raise ValueError('runtime writable state differs from its dedicated contract')
    return plan, contract, docker, containerd


def ensure_stopped(plan: dict[str, Any], docker: dict[str, Any], containerd: dict[str, Any]) -> None:
    old = read(plan['runtime_launch'])['processes']
    if {item['name'] for item in old} != {'dockerd', 'containerd'}:
        raise ValueError('original runtime launch is incomplete')
    for item in old:
        if processes.pid_matches(item['pid'], item['start_ticks'], item['boot_id']):
            raise ValueError('original runtime wrapper remains alive')
    # A different launcher may have adopted the same store after the old exit.
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            argv = [part.decode() for part in (proc / 'cmdline').read_bytes().split(b'\0') if part]
            if not argv or Path(argv[0]).name not in ('dockerd', 'containerd'):
                continue
            name = Path(argv[0]).name
            flag = '--config-file' if name == 'dockerd' else '--config'
            config = argv[argv.index(flag) + 1] if flag in argv else (
                '/etc/docker/daemon.json' if name == 'dockerd' else '/etc/containerd/config.toml')
            if name == 'dockerd':
                active = read(config) if Path(config).exists() else {}
                root = active.get('data-root', '/var/lib/docker')
                if '--data-root' in argv:
                    root = argv[argv.index('--data-root') + 1]
                target = docker['data-root']
            else:
                active = tomllib.loads(Path(config).read_text()) if Path(config).exists() else {}
                root = active.get('root', '/var/lib/containerd')
                if '--root' in argv:
                    root = argv[argv.index('--root') + 1]
                target = containerd['root']
            if Path(root).resolve() == Path(target).resolve():
                raise ValueError(f'live {name} already uses this runtime store')
        except (FileNotFoundError, ProcessLookupError):
            continue


def unit_command(plan: dict[str, Any], name: str, *, now: float | None = None) -> list[str]:
    remaining = plan['deadline_epoch'] - (time.time() if now is None else now)
    if remaining <= 0:
        raise ValueError('runtime deadline expired before launch')
    root = Path(plan['root'])
    config = plan['containerd_config' if name == 'containerd' else 'docker_config']
    executable = '/usr/bin/' + name
    flag = '--config' if name == 'containerd' else '--config-file'
    properties = ['Type=notify', 'NotifyAccess=all', 'Restart=no', 'Delegate=yes',
                  'TimeoutStartSec=30s', 'TimeoutStopSec=10s', 'KillMode=mixed',
                  f'RuntimeMaxSec={remaining:.6f}s', 'WorkingDirectory=' + str(root),
                  'RequiresMountsFor=' + plan['data_mount'],
                  'StandardOutput=append:' + str(root / (name + '.log')),
                  'StandardError=inherit']
    env = {'TMPDIR': root / 'tmp', 'DOCKER_TMPDIR': root / 'tmp',
           'XDG_CACHE_HOME': root / 'cache', 'DOCKER_CONFIG': root / 'docker-config'}
    properties.extend('Environment=' + key + '=' + str(value) for key, value in env.items())
    return ['systemd-run', '--unit=' + plan['unit_prefix'] + '-' + name,
            *['--property=' + value for value in properties], executable, flag, config]


def prepare_bridge(plan: dict[str, Any], docker: dict[str, Any]) -> dict[str, Any] | None:
    name = docker.get('bridge')
    if name in (None, 'none'):
        return None
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', name) or name == 'docker0':
        raise ValueError('runtime requires an exact dedicated bridge')
    if 'bip' in docker:
        raise ValueError('a named Docker bridge cannot also declare bip')
    network = ipaddress.ip_network(docker['fixed-cidr'], strict=True)
    address = str(network.network_address + 1) + '/' + str(network.prefixlen)
    inspected = subprocess.run(['ip', '-j', '-d', 'link', 'show', name], capture_output=True, text=True, timeout=10)
    if inspected.returncode == 0:
        link = json.loads(inspected.stdout)
        if len(link) != 1 or link[0].get('linkinfo', {}).get('info_kind') != 'bridge':
            raise ValueError('dedicated runtime interface is not a bridge')
        addresses = json.loads(subprocess.run(['ip', '-j', '-4', 'address', 'show', name],
            capture_output=True, text=True, timeout=10, check=True).stdout)
        present = [v['local'] + '/' + str(v['prefixlen']) for row in addresses for v in row.get('addr_info', [])]
        if present != [address]:
            raise ValueError('existing runtime bridge has a different address')
        created = False
    else:
        routes = json.loads(subprocess.run(['ip', '-j', '-4', 'route', 'show'],
            capture_output=True, text=True, timeout=10, check=True).stdout)
        for route in routes:
            if route.get('dst', 'default') != 'default' and network.overlaps(ipaddress.ip_network(route['dst'], strict=False)):
                raise ValueError('new runtime bridge overlaps an existing route')
        subprocess.run(['ip', 'link', 'add', name, 'type', 'bridge'], check=True, timeout=10)
        subprocess.run(['ip', 'address', 'add', address, 'dev', name], check=True, timeout=10)
        created = True
    subprocess.run(['ip', 'link', 'set', name, 'mtu', str(docker.get('mtu', 1500)), 'up'], check=True, timeout=10)
    receipt = {'name': name, 'address': address, 'created': created, 'at_epoch': time.time()}
    atomic_json(Path(plan['root']) / 'bridge.json', receipt)
    return receipt


def _cleanup_unit(unit: str) -> dict[str, Any]:
    receipt: dict[str, Any] = {"unit": unit}
    for operation, key in (("stop", "returncode"), ("reset-failed", "reset_failed_returncode")):
        try:
            result = subprocess.run(["systemctl", operation, unit], capture_output=True, text=True, timeout=20)
            receipt[key] = result.returncode
            if result.returncode:
                receipt[operation + "_error"] = result.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            receipt[key] = None
            receipt[operation + "_error"] = str(exc)
    return receipt


def restart_plan(path: str | Path) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise ValueError('runtime restoration must run as root')
    plan, contract, docker, containerd = load_plan(path)
    ensure_stopped(plan, docker, containerd)
    root = Path(plan['root'])
    if (root / 'planned.json').exists():
        raise ValueError('runtime plan already attempted; inspect status before any further action')
    for name in ('containerd', 'dockerd'):
        unit = plan['unit_prefix'] + '-' + name + '.service'
        found = subprocess.run(['systemctl', 'show', unit, '--property=LoadState', '--value'],
                               capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        if found != 'not-found':
            raise ValueError('runtime unit already exists; inspect its original launch')
    for directory in ('tmp', 'cache', 'docker-config'):
        target = root / directory
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        owner = root.stat()
        os.chown(target, owner.st_uid, owner.st_gid)
    atomic_json(root / 'planned.json', {'plan_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                                      'deadline_epoch': plan['deadline_epoch'], 'at_epoch': time.time()})
    installed = []
    try:
        prepare_bridge(plan, docker)
        for name in ('containerd', 'dockerd'):
            command = unit_command(plan, name)
            subprocess.run(command, capture_output=True, text=True, timeout=40, check=True)
            installed.append(plan['unit_prefix'] + '-' + name + '.service')
        result = subprocess.run(['docker', '--host', contract['docker_host'], 'info',
                                 '--format', '{{.DockerRootDir}}'], capture_output=True, text=True,
                                timeout=10, check=True)
        if result.stdout.strip() != docker['data-root']:
            raise ValueError('restored Docker data-root differs')
        result = {'at_epoch': time.time(), 'units': installed, 'deadline_epoch': plan['deadline_epoch'],
                  'docker_host': contract['docker_host'], 'docker_root': result.stdout.strip(),
                  'restored': True, 'gpu_queue_changed': False}
        atomic_json(root / 'installed.json', result)
        return result
    except Exception as exc:
        cleanup = []
        for name in ('dockerd', 'containerd'):
            unit = plan['unit_prefix'] + '-' + name + '.service'
            cleanup.append(_cleanup_unit(unit))
        atomic_json(root / 'failure.json', {'at_epoch': time.time(), 'installed_units': installed,
                                          'error': str(exc), 'cleanup': cleanup})
        raise


def status_plan(path: str | Path) -> dict[str, Any]:
    plan, _, _, _ = load_plan(path, require_future=False)
    units = [plan['unit_prefix'] + '-' + name + '.service' for name in ('containerd', 'dockerd')]
    result = subprocess.run(['systemctl', 'show', *units, '--no-pager',
                             '--property=Id,ActiveState,SubState,MainPID,RuntimeMaxUSec,Result'],
                            capture_output=True, text=True, timeout=10, check=True)
    root = Path(plan['root'])
    return {'units': result.stdout, 'receipts': {name: read(root / name) for name in
            ('planned.json', 'installed.json', 'failure.json') if (root / name).exists()}}
