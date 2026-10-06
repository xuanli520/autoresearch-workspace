"""Idempotent, bounded task-owned cancellation for resources outside /proc."""
from __future__ import annotations
import os
import hashlib
import subprocess
import time
from pathlib import Path

from longrun import atomic_json, file_lock, read_json, utc_now, load_config
from processes import terminate_scope


def amended_cleanup(turn_dir: Path, launch: dict) -> tuple[dict, dict, str | None]:
    """Use a verified official amendment to recover an obsolete failed hook."""
    run_dir = turn_dir.parent.parent
    state = read_json(run_dir / 'state.json', {})
    relative = state.get('paths', {}).get('config', '')
    if not relative.startswith('amendments/'):
        return launch.get('cleanup', {}), {}, None
    config_path = (run_dir / relative).resolve(strict=True)
    if not config_path.is_relative_to((run_dir / 'amendments').resolve()):
        raise ValueError('amended cleanup config is outside official amendments')
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    receipt = read_json(config_path.parent / 'receipt.json')
    if digest != state.get('config_sha256') or digest != receipt.get('config_sha256'):
        raise ValueError('amended cleanup configuration hash mismatch')
    config = load_config(config_path)
    if config['cleanup'] == launch.get('cleanup', {}):
        return launch.get('cleanup', {}), {}, None
    return config['cleanup'], config['env'], str(config_path)


def cleanup_task(turn_dir: Path, *, retry_pending: bool = False) -> bool:
    if not (turn_dir / 'launch.json').is_file():
        # No command can pass the launch gate without this durable record.
        return not (turn_dir / 'GO').exists()
    launch = read_json(turn_dir/'launch.json')
    cfg, amended_env, amendment = amended_cleanup(turn_dir, launch)
    if not cfg.get('command'):
        return True
    receipt_name = 'cleanup-retry-exit.json' if retry_pending else 'cleanup-exit.json'
    attempt_prefix = 'cleanup-retry-attempt-' if retry_pending else 'cleanup-attempt-'
    timeout = cfg['timeout_seconds']
    deadline = time.monotonic() + timeout + 3
    while True:
        try:
            with file_lock(turn_dir/'.cleanup.lock', blocking=False):
                receipt = read_json(turn_dir/receipt_name, {})
                if receipt.get('ok'):
                    return True
                # Hook must cancel only resources identified by this turn.
                env = os.environ.copy()
                env.update(launch['env'])
                env.update(amended_env)
                env['AUTORESEARCH_RETRY_PENDING'] = '1' if retry_pending else '0'
                with (turn_dir/'cleanup.log').open('ab') as log:
                    proc = None
                    try:
                        proc = subprocess.Popen(cfg['command'], cwd=launch['cwd'], env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                        code = proc.wait(timeout=timeout)
                        reason = 'process_exit'
                    except subprocess.TimeoutExpired:
                        terminate_scope(launch['token'], 0)
                        proc.wait(timeout=2)
                        code, reason = None, 'timeout'
                    except OSError as exc:
                        log.write((f'cleanup spawn error: {type(exc).__name__}: {exc}\n').encode())
                        code, reason = None, 'spawn_error'
                attempt = receipt.get('attempt', 0) + 1
                while (turn_dir / f'{attempt_prefix}{attempt:04d}.json').exists():
                    attempt += 1
                receipt = {'at': utc_now(), 'ok': code == 0, 'attempt': attempt,
                           'returncode': code, 'reason': reason, 'retry_pending': retry_pending}
                if amendment:
                    receipt['cleanup_config'] = amendment
                atomic_json(turn_dir / f'{attempt_prefix}{attempt:04d}.json', receipt)
                atomic_json(turn_dir/receipt_name, receipt)
                return code == 0
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(.05)
