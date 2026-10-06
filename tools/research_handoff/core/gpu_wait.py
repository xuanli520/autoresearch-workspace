"""Round-local GPU waiting intervals forwarded from the official scheduler SDK."""
from __future__ import annotations

import math
from typing import Any


WAITING_STATES = frozenset({'QUEUED', 'STARTING', 'UNKNOWN', 'RECONCILING', 'SESSION_CHANGED'})
INFRASTRUCTURE_STATES = frozenset({'INFEASIBLE', 'EXPIRED', 'UNKNOWN', 'RECONCILING', 'SESSION_CHANGED'})
GPU_STATES = WAITING_STATES | frozenset({'RUNNING', 'CANCELLING', 'COMPLETED', 'SUCCEEDED', 'FAILED',
                                     'CANCELLED', 'TIMED_OUT', 'INFEASIBLE', 'EXPIRED', 'REJECTED'})
INFRASTRUCTURE_REASONS = frozenset({'gpu_infeasible', 'gpu_expired', 'gpu_unknown',
                                    'gpu_reconciling', 'gpu_session_changed'})


def gpu_wait_seconds(turn: dict[str, Any], now: float) -> float:
    wait = turn.get('gpu_wait', {})
    seconds = float(wait.get('excluded_seconds', 0))
    started = wait.get('started_monotonic')
    return seconds + (max(0, now - started) if started is not None else 0)


def close_gpu_wait(turn: dict[str, Any], now: float) -> None:
    wait = turn.get('gpu_wait', {})
    started = wait.get('started_monotonic')
    if started is not None:
        ended = max(started, now)
        wait['excluded_seconds'] += ended - started
        wait['intervals'].append([started, ended])
        wait['started_monotonic'] = None


def gpu_infrastructure_reason(turn: dict[str, Any]) -> str | None:
    requests = turn.get('gpu_wait', {}).get('requests', {})
    for snapshot in reversed(list(requests.values())):
        if snapshot.get('state') in INFRASTRUCTURE_STATES:
            return 'gpu_' + snapshot['state'].lower()
    return None


def observe_gpu_state(turn: dict[str, Any], event: dict[str, Any], now: float) -> bool:
    """Update one request without discarding another concurrent request's wait."""
    request_id = event.get('request_id')
    state = event.get('state')
    if not isinstance(request_id, str) or not request_id.strip() or state not in GPU_STATES:
        raise ValueError('gpu.state requires nonempty request_id and scheduler state')
    if not math.isfinite(now):
        raise ValueError('GPU observation time must be finite')
    wait = turn.setdefault('gpu_wait', {'requests': {}, 'excluded_seconds': 0.0,
                                        'started_monotonic': None, 'intervals': []})
    existing = wait['requests'].get(request_id)
    sequence = event.get('sequence')
    if sequence is not None and (type(sequence) is not int or sequence < 0):
        raise ValueError('GPU event sequence must be a nonnegative integer')
    if existing and existing.get('sequence') is not None and sequence is not None:
        if sequence < existing['sequence']:
            return False
    snapshot = {key: event[key] for key in ('request_id', 'job_id', 'state', 'session_id', 'sequence',
                                           'reason', 'projected_start', 'latest_start', 'scheduler_root',
                                           'queue', 'reconciling') if key in event}
    wait['requests'][request_id] = snapshot
    waiting = any(item['state'] in WAITING_STATES and
                  (item['state'] != 'UNKNOWN' or item.get('reconciling') is True)
                  for item in wait['requests'].values())
    if waiting and wait['started_monotonic'] is None:
        wait['started_monotonic'] = now
    elif not waiting and wait['started_monotonic'] is not None:
        close_gpu_wait(turn, now)
    turn['status'] = 'WAITING_GPU' if waiting else 'RUNNING'
    return True
