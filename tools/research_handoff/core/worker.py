"""A gated task worker with an independent deadline and parent-loss check."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from longrun import atomic_json, read_json, utc_now
from processes import pid_matches, terminate_scope


def gpu_wait_duration(turn_dir: Path, token: str, now: float) -> float:
    record = read_json(turn_dir / 'gpu-wait.json', {})
    if not record:
        return 0.0
    seconds = record.get('excluded_seconds')
    started = record.get('started_monotonic')
    if (record.get('version') != 1 or record.get('token') != token
            or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
            or (started is not None and (type(started) not in (int, float)
                                        or not math.isfinite(started) or started > now))):
        raise ValueError('invalid controller GPU wait record')
    return seconds + (max(0, now - started) if started is not None else 0)


def main() -> int:
    turn_dir = Path(sys.argv[1])
    launch = read_json(turn_dir / 'launch.json')
    token = launch['token']
    requested = None

    def stopped(signum, _frame):
        nonlocal requested
        requested = 'signal_stop'

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stopped)
    child = None
    started = None
    code = 1
    reason = 'launch_cancelled'
    cleanup_ok = True
    deadline = None
    # A controller must persist this worker's identity before opening its gate.
    gate_deadline = time.monotonic() + 10
    try:
        while not (turn_dir / 'GO').exists():
            if requested or time.monotonic() >= gate_deadline or not pid_matches(
                    launch['controller_pid'], launch['controller_start_ticks'], launch['boot_id']):
                return code
            time.sleep(.02)
        if requested or not pid_matches(launch['controller_pid'], launch['controller_start_ticks'], launch['boot_id']):
            return code
        remaining = min(launch['seconds'], launch['hard_deadline_epoch'] - time.time())
        if remaining <= 0:
            reason = 'hard_limit'
            return code
        started = time.monotonic()
        atomic_json(turn_dir / 'worker-start.json', {'at': utc_now(), 'monotonic': started})
        env = os.environ.copy()
        env.update(launch['env'])
        child = subprocess.Popen(launch['command'], cwd=launch['cwd'], env=env,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = started + remaining
        while child.poll() is None:
            if requested:
                reason = requested
                break
            if not pid_matches(launch['controller_pid'], launch['controller_start_ticks'], launch['boot_id']):
                reason = 'controller_lost'
                break
            # Both clocks: a backward wall adjustment cannot extend the turn.
            now = time.monotonic()
            deadline = started + remaining + gpu_wait_duration(turn_dir, token, now)
            if now >= deadline or time.time() >= launch['hard_deadline_epoch']:
                reason = 'hard_limit' if time.time() >= launch['hard_deadline_epoch'] else 'turn_timeout'
                break
            time.sleep(.03)
        else:
            reason = 'process_exit'
        if child.poll() is not None:
            code = child.returncode
    except BaseException as exc:
        reason = 'worker_error'
        print(f'worker error: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
    finally:
        ended = time.monotonic()
        # Shutdown grace never extends either the turn or the wall deadline.
        grace = min(launch.get('grace_seconds', 1), max(0, launch['hard_deadline_epoch']-time.time()))
        if deadline is not None:
            grace = min(grace, max(0, deadline-time.monotonic()))
        cleanup_ok = terminate_scope(token, grace)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cleanup_ok = False
        atomic_json(turn_dir / 'worker-exit.json', {
            'at': utc_now(), 'returncode': code, 'reason': reason, 'cleanup_ok': cleanup_ok,
            'cleanup_scope_only': True,
            'runtime_seconds': max(0, ended - started) if started is not None else 0,
        })
    return 0 if code == 0 and cleanup_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
