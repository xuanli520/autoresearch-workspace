"""Pure, read-only timing and historical diagnostics for the unified monitor.

Only bounded probe documents are consumed. State contains operational facts and
fingerprints, never event payloads, summaries, formal scores or evidence paths.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any


DEFAULT_THRESHOLDS = {
    'retry_warning': 3, 'retry_critical': 5, 'retry_window_seconds': 3600,
    'retry_window_warning': 9, 'retry_window_critical': 25,
    'invalid_window_size': 40, 'invalid_min_events': 6,
    'invalid_warning_rate': .25, 'invalid_critical_rate': .5,
    'invalid_consecutive_warning': 3, 'invalid_consecutive_critical': 5,
    'summary_warning_generations': 10, 'summary_critical_generations': 20,
    'queue_timeout_warning': 2, 'queue_timeout_window_seconds': 43200,
    'credit_unknown_grace_seconds': 300, 'pending_grace_seconds': 300,
    'external_block_seconds': 1800, 'own_block_seconds': 3600,
    'reconcile_timeout_seconds': 1800,
}
AGENT_FAILURES = frozenset({
    'agent_exit_nonzero', 'heartbeat_stale', 'invalid_agent_event',
    'agent_reported_failure', 'completion_missing', 'context_usage_missing',
    'turn_timeout', 'worker_error', 'worker_exit_missing',
})
WAITING_STATES = frozenset({'WAITING_GPU', 'QUEUED', 'STARTING', 'UNKNOWN',
                          'RECONCILING', 'SESSION_CHANGED'})
TERMINAL_STATES = frozenset({'COMPLETED', 'SUCCEEDED', 'STOPPED', 'CANCELLED',
                           'FAILED', 'EXPIRED', 'TIMED_OUT'})
KNOWN_REASONS = AGENT_FAILURES | frozenset({
    'queue_timeout', 'gpu_expired', 'gpu_infeasible', 'gpu_unknown',
    'gpu_reconciling', 'gpu_session_changed', 'operator_stop', 'hard_limit',
    'context_window', 'target_reached', 'external_occupancy', 'external_reserved',
    'external_reservation', 'external_jobs', 'insufficient_memory',
    'insufficient_compute', 'memory_limit', 'oom', 'oom_kill',
})
BUCKET_SECONDS = 900
BUCKET_COUNT = 48


def validate_thresholds(cfg: dict) -> dict:
    diagnostics = cfg.get('diagnostics', {})
    aliases = cfg.get('alert_thresholds', {})
    if not isinstance(diagnostics, dict) or not isinstance(aliases, dict):
        raise ValueError('diagnostic configuration must be an object')
    supplied = diagnostics.get('thresholds', {})
    if not isinstance(supplied, dict):
        raise ValueError('diagnostic thresholds must be an object')
    supplied = {**supplied, **aliases}
    unknown = set(supplied).difference(DEFAULT_THRESHOLDS)
    if unknown:
        raise ValueError('unknown diagnostic threshold')
    for key, value in supplied.items():
        if not finite(value) or value <= 0:
            raise ValueError(f'diagnostic threshold {key} must be positive and finite')
        if key.endswith('_rate'):
            if value > 1:
                raise ValueError(f'diagnostic threshold {key} must be at most 1')
        elif not key.endswith('_seconds') and value != int(value):
            raise ValueError(f'diagnostic threshold {key} must be an integer')
    result = {**DEFAULT_THRESHOLDS, **supplied}
    for lower, upper in (('retry_warning', 'retry_critical'),
                         ('retry_window_warning', 'retry_window_critical'),
                         ('invalid_warning_rate', 'invalid_critical_rate'),
                         ('invalid_consecutive_warning', 'invalid_consecutive_critical'),
                         ('summary_warning_generations', 'summary_critical_generations')):
        if result[lower] > result[upper]:
            raise ValueError('diagnostic warning threshold exceeds critical threshold')
    return result


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def epoch(value: Any) -> float | None:
    if finite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                      default=str).encode()).hexdigest()


def _doc(raw: dict, path: str | None) -> dict:
    if not path:
        return {}
    entry = raw.get('files', {}).get(path, {})
    try:
        value = json.loads(entry.get('text', ''))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _reason(value: Any) -> str | None:
    return value if isinstance(value, str) and value in KNOWN_REASONS else None


def _event(row: dict, now: float) -> dict | None:
    name = row.get('event', row.get('type', row.get('autoresearch')))
    at = epoch(row.get('at', row.get('time', row.get('timestamp'))))
    if not isinstance(name, str) or at is None or at > now + 1:
        return None
    if len(name) > 64 or not re.fullmatch(r'[a-z][a-z0-9_.]*', name):
        return None
    payload = row.get('payload', row.get('request', {}))
    payload = payload if isinstance(payload, dict) else {}
    turn = row.get('turn', row.get('failed_turn', row.get('number')))
    generation = row.get('generation', payload.get('generation'))
    state = row.get('state', row.get('status', payload.get('state')))
    state = state if isinstance(state, str) and state in WAITING_STATES | TERMINAL_STATES | {'RUNNING', 'INFEASIBLE'} else None
    request = row.get('request_id', payload.get('request_id'))
    sequence = row.get('sequence', payload.get('sequence'))
    returncode = row.get('returncode')
    excluded = row.get('excluded_seconds')
    # Payloads may contain private completion evidence. Keep only this whitelist.
    result = {'event': name, 'at': at,
              'turn': turn if type(turn) is int and turn >= 0 else None,
              'generation': generation if type(generation) is int and generation >= 0 else None,
              'reason': _reason(row.get('reason', payload.get('reason'))),
              'state': state,
              'waiting': row.get('waiting') if type(row.get('waiting')) is bool else None,
              'request_id': request if isinstance(request, str) else None,
              'sequence': sequence if type(sequence) is int and sequence >= 0 else None,
              'returncode': returncode if type(returncode) is int else None,
              'excluded_seconds': excluded if finite(excluded) and excluded >= 0 else None}
    if name == 'agent.event_rejected' and isinstance(row.get('error'), str):
        result['error_fingerprint'] = digest(row['error'])
    summary = row.get('summary_sha256')
    if isinstance(summary, str) and re.fullmatch(r'[a-fA-F0-9]{64}', summary):
        result['summary_hash'] = summary
    elif name in ('context.compacted', 'context.reopened', 'context.summary', 'method.summary'):
        text = row.get('summary', payload.get('summary'))
        if isinstance(text, str) and text.strip():
            result['summary_hash'] = digest(text.strip())
    if name.startswith('context.') and result['generation'] is None:
        result.pop('summary_hash', None)
    result['fingerprint'] = digest(result)
    return result


def _events(raw: dict, now: float) -> tuple[list[dict], list[str]]:
    values, gaps = [], []
    for entry in raw.get('files', {}).values():
        if entry.get('error'):
            gaps.append('registered_file_unavailable')
        text = entry.get('text')
        if not isinstance(text, str):
            continue
        lines = text.splitlines()
        malformed = False
        has_events = False
        for index, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                if index < len(lines) - 1 and line.strip():
                    malformed = True
                continue
            if isinstance(row, dict):
                event = _event(row, now)
                if event:
                    values.append(event)
                    has_events = True
                elif any(k in row for k in ('event', 'autoresearch')):
                    gaps.append('event_timestamp_unknown_or_future')
        if has_events and malformed:
            gaps.append('partial_or_invalid_event')
    scheduler = raw.get('scheduler_view') or {}
    for attempt in scheduler.get('attempts', scheduler.get('history', [])):
        state = attempt.get('state')
        if state not in ('EXPIRED', 'INFEASIBLE', 'UNKNOWN', 'RECONCILING'):
            continue
        at = (epoch(attempt.get('finished_at')) or epoch(attempt.get('last_updated_at'))
              or epoch(attempt.get('submitted_at')))
        if at is None:
            gaps.append('scheduler_attempt_timestamp_unknown')
            continue
        row = _event({'event': 'scheduler.state', 'at': at, 'state': state,
                      'request_id': attempt.get('request_id'), 'reason': attempt.get('reason')}, now)
        if row:
            values.append(row)
    unique = {row['fingerprint']: row for row in values}
    return sorted(unique.values(), key=lambda row: (row['at'], row['fingerprint'])), sorted(set(gaps))


def timing(task: dict, spec: dict, document: dict, host: dict, now: float,
           events: list[dict] | None = None) -> dict:
    """Expose live and pending estimates without increasing official credit."""
    budget = document.get('budget', {})
    budget = budget if isinstance(budget, dict) else {}
    turn = document.get('turn', {})
    turn = turn if isinstance(turn, dict) else {}
    controller = spec.get('controller', {})
    registered = bool(controller)
    official = (registered and controller.get('type') == 'research_handoff'
                and document.get('controller') == 'autoresearch-longrun'
                and document.get('run_id') == controller.get('run_id'))
    gaps = []
    if registered and not official:
        gaps.append('controller_identity_unverified')
    source = 'research_handoff' if official else task.get('budget_source', 'legacy')
    credited = (budget.get('active_seconds') if official else
                None if registered else task.get('effective_seconds'))
    credited = credited if finite(credited) and credited >= 0 else None
    target = (budget.get('window_seconds') if official and budget.get('mode') == 'active' else
              None if registered and not official else task.get('effective_target_seconds'))
    target = target if finite(target) and target > 0 else None
    deadline = epoch(task.get('deadline_at'))
    if official:
        official_deadline = epoch(budget.get('hard_deadline_at'))
        deadline = min(deadline, official_deadline) if deadline is not None and official_deadline is not None else (official_deadline or deadline)
    policy = budget.get('credit_policy') if official else task.get('credit_policy')
    started = epoch(turn.get('started_at')) if official else None
    ended = epoch(turn.get('ended_at'))
    state = turn.get('status', document.get('status', task.get('state')))
    live = None
    if started is not None:
        if started > now or ended is not None and ended < started:
            gaps.append('turn_clock_invalid')
        else:
            live = max(0, min(ended, now) - started) if ended is not None else max(0, now - started)
    if official and budget.get('boot_id') and host.get('boot_id') and budget['boot_id'] != host['boot_id']:
        gaps.append('host_boot_identity_changed')
        live = None
    if budget.get('view', {}).get('clock_issue'):
        gaps.append('controller_clock_issue')
        live = None
    excluded = None
    if live is not None:
        wait = turn.get('gpu_wait', {})
        wait = wait if isinstance(wait, dict) else {}
        excluded = wait.get('excluded_seconds', 0)
        excluded = max(0, excluded) if finite(excluded) else 0
        wait_start = wait.get('started_monotonic')
        active_start = budget.get('active_monotonic')
        active_epoch = epoch(budget.get('active_started_at'))
        if finite(wait_start) and finite(active_start) and active_epoch is not None:
            wait_epoch = active_epoch + wait_start - active_start
            excluded += max(0, min(ended or now, now) - max(started, wait_epoch))
        elif state == 'WAITING_GPU':
            matches = [e for e in (events or []) if e['event'] == 'turn.gpu_state'
                       and e.get('turn') == turn.get('number')]
            if matches and matches[-1].get('waiting') is True:
                event = matches[-1]
                previous_excluded = event.get('excluded_seconds', 0)
                excluded = (previous_excluded if finite(previous_excluded) else 0) + max(0, now - event['at'])
            elif not wait:
                gaps.append('gpu_wait_interval_unknown')
                excluded = live
            elif finite(wait_start):
                gaps.append('gpu_wait_clock_unknown')
                excluded = live
        excluded = min(live, excluded)
    pending = None
    if live is not None:
        pending = max(0, live - (excluded or 0)) if state not in TERMINAL_STATES and policy in ('reported', 'successful_turn') else 0
    calculation_state = ('unknown' if gaps else 'waiting_gpu' if state == 'WAITING_GPU'
                         else 'pending_settlement' if pending else 'credited')
    return {'live_elapsed_seconds': live, 'pending_seconds': pending,
            'credited_effective_seconds': credited,
            'effective_remaining_seconds': max(0, target - credited) if target is not None and credited is not None else None,
            'effective_target_seconds': target,
            'remaining_wall_seconds': deadline - now if deadline is not None else None,
            'deadline_at': utc(deadline) if deadline is not None else None,
            'credit_policy': policy, 'source': source,
            'last_credit_at': budget.get('last_credit_at', budget.get('active_ended_at')),
            'calculation_state': calculation_state, 'waiting_gpu': state == 'WAITING_GPU',
            'excluded_gpu_wait_seconds': excluded,
            'gpu_running_seconds': max(0, live - (excluded or 0)) if live is not None else None,
            'data_gap': gaps}


def _alert(task: dict, rule: str, severity: str, threshold: Any, count: int,
           evidence: dict, source: str = 'research_handoff') -> dict:
    return {'id': digest([task['id'], rule])[:24], 'rule': rule,
            'task_id': task['id'], 'agent_id': (task.get('controller') or {}).get('run_id'),
            'job_id': (task.get('active_job') or {}).get('job_id'),
            'endpoint_id': task.get('endpoint_id', task.get('host')),
            'severity': severity, 'threshold': threshold,
            'observed_count': count, 'source': source, 'evidence': evidence}


def _severity(count: int | float, warning: int | float, critical: int | float) -> str | None:
    return 'critical' if count >= critical else 'warning' if count >= warning else None


def _history(old: dict, incoming: list[dict], now: float, thresholds: dict) -> dict:
    history = deepcopy(old)
    seen = set(history.get('seen', []))
    fresh = [row for row in incoming if row['fingerprint'] not in seen]
    seen_order = history.get('seen', []) + [row['fingerprint'] for row in fresh]
    history['seen'] = list(dict.fromkeys(seen_order))[-8192:]
    history.setdefault('retry_consecutive', 0)
    history.setdefault('retry_turns', [])
    history.setdefault('retry_window', [])
    history.setdefault('invalid_window', [])
    history.setdefault('invalid_consecutive', 0)
    history.setdefault('queue_timeouts', [])
    history.setdefault('summary_unchanged_generations', 0)
    history.setdefault('summary_generation', None)
    history.setdefault('summary_hash', None)
    for row in fresh:
        name, reason, at = row['event'], row.get('reason'), row['at']
        if at < (epoch(history.get('latest_event', {}).get('at')) or 0):
            history['out_of_order_events'] = history.get('out_of_order_events', 0) + 1
            continue
        if name in ('turn.retry_scheduled', 'retry') and reason in AGENT_FAILURES:
            retry_key = digest([row.get('turn'), reason]) if row.get('turn') is not None else row['fingerprint']
            if retry_key not in history['retry_turns']:
                history['retry_consecutive'] += 1
                history['retry_window'].append(at)
                history['retry_turns'].append(retry_key)
        elif name == 'turn.finished' and (row.get('state') == 'COMPLETED' or row.get('returncode') == 0 and reason is None):
            history['retry_consecutive'] = 0
        invalid = reason == 'invalid_agent_event' or name in ('agent.event_rejected', 'agent.invalid_event', 'invalid_agent_event')
        if invalid or name == 'agent.event':
            history['invalid_window'].append({'at': at, 'invalid': invalid})
            history['invalid_consecutive'] = history['invalid_consecutive'] + 1 if invalid else 0
        if reason in ('queue_timeout', 'gpu_expired') or name in ('queue_timeout', 'request.expired', 'job.expired') or row.get('state') == 'EXPIRED':
            attempt = row.get('request_id') or row.get('turn') or row['fingerprint']
            key = digest([attempt, 'queue_timeout'])
            if key not in {r['key'] for r in history['queue_timeouts']}:
                history['queue_timeouts'].append({'key': key, 'at': at})
        summary = row.get('summary_hash')
        generation = row.get('generation')
        if summary and (generation is None or history['summary_generation'] is None or generation > history['summary_generation']):
            history['summary_unchanged_generations'] = (history['summary_unchanged_generations'] + 1
                                                       if summary == history['summary_hash'] else 1)
            history['summary_hash'] = summary
            history['summary_generation'] = generation
        history['latest_event'] = {'event': name, 'at': utc(at), 'fingerprint': row['fingerprint']}
    history['retry_window'] = [at for at in history['retry_window'] if now - at <= thresholds['retry_window_seconds']][-4096:]
    history['retry_turns'] = history['retry_turns'][-8192:]
    history['invalid_window'] = history['invalid_window'][-int(thresholds['invalid_window_size']):]
    history['queue_timeouts'] = [row for row in history['queue_timeouts']
                                 if now - row['at'] <= thresholds['queue_timeout_window_seconds']][-4096:]
    return history


def _memory(raw: dict, task: dict, host: dict) -> list[dict]:
    values = list(raw.get('processes', []))
    values.extend(task.get('gpu_processes', []))
    values.extend(p for p in host.get('gpu_processes', {}).get('rows', [])
                  if p.get('owner_task_id') == task['id'])
    found = {}
    for process in values:
        candidates = process.get('cgroup_memory', process.get('memory', {}))
        if isinstance(candidates, dict):
            candidates = [candidates]
        for item in candidates if isinstance(candidates, list) else []:
            if isinstance(item, dict):
                events = item.get('events', item.get('memory_events', {}))
                if isinstance(events, dict):
                    found[digest([item.get('path') or process.get('pid'), events])] = {
                        'oom': events.get('oom', 0), 'oom_kill': events.get('oom_kill', 0),
                        'max_events': events.get('max', 0),
                        'current_bytes': item.get('current_bytes', item.get('memory_current', item.get('current'))),
                        'max_bytes': item.get('max_bytes', item.get('memory_max', item.get('max')))}
    return list(found.values())


def _rules(task: dict, raw: dict, host: dict, state: dict, now: float,
           thresholds: dict, gaps: list[str], formal_unchanged: bool) -> list[dict]:
    active = []
    time = task['timing']
    needed, wall = time['effective_remaining_seconds'], time['remaining_wall_seconds']
    if needed is None and wall is not None and task.get('state') not in TERMINAL_STATES:
        state.setdefault('credit_unknown_since', now)
        unknown_age = max(0, now - state['credit_unknown_since'])
        if unknown_age >= thresholds['credit_unknown_grace_seconds']:
            active.append(_alert(task, 'EFFECTIVE_CREDIT_UNKNOWN', 'warning', thresholds['credit_unknown_grace_seconds'],
                                 1, {'unknown_seconds': unknown_age}, time['source']))
    elif needed is not None:
        state.pop('credit_unknown_since', None)
    if needed is not None and wall is not None and needed > max(0, wall):
        pending = time.get('pending_seconds') or 0
        if needed - pending <= max(0, wall) and time['calculation_state'] == 'pending_settlement':
            state.setdefault('pending_risk_since', now)
            if now - state['pending_risk_since'] >= thresholds['pending_grace_seconds']:
                active.append(_alert(task, 'EFFECTIVE_PENDING_SETTLEMENT', 'warning', thresholds['pending_grace_seconds'], 1,
                                     {'needed_seconds': needed, 'pending_seconds': pending,
                                      'remaining_wall_seconds': wall}, time['source']))
        else:
            active.append(_alert(task, 'EFFECTIVE_TARGET_UNREACHABLE', 'critical', 'needed > remaining_wall', 1,
                                 {'needed_seconds': needed, 'remaining_wall_seconds': wall,
                                  'credited_effective_seconds': time['credited_effective_seconds']}, time['source']))
    else:
        state.pop('pending_risk_since', None)
    count = state['retry_consecutive']
    window_count = len(state['retry_window'])
    severity = _severity(count, thresholds['retry_warning'], thresholds['retry_critical'])
    window_severity = _severity(window_count, thresholds['retry_window_warning'], thresholds['retry_window_critical'])
    if severity or window_severity:
        severity = 'critical' if 'critical' in (severity, window_severity) else 'warning'
        active.append(_alert(task, 'AGENT_RETRY_STORM', severity,
                             {'consecutive_warning': thresholds['retry_warning'], 'consecutive_critical': thresholds['retry_critical'],
                              'window_seconds': thresholds['retry_window_seconds']}, count,
                             {'consecutive_retries': count, 'window_retries': window_count}))
    window = state['invalid_window']
    invalid_count = sum(row['invalid'] for row in window)
    rate = invalid_count / len(window) if window else 0
    alternations = sum(a['invalid'] != b['invalid'] for a, b in zip(window, window[1:]))
    severity = _severity(state['invalid_consecutive'], thresholds['invalid_consecutive_warning'], thresholds['invalid_consecutive_critical'])
    if len(window) >= thresholds['invalid_min_events']:
        rate_severity = _severity(rate, thresholds['invalid_warning_rate'], thresholds['invalid_critical_rate'])
        severity = 'critical' if 'critical' in (severity, rate_severity) else severity or rate_severity
    if severity:
        active.append(_alert(task, 'INVALID_AGENT_EVENT_PATTERN', severity,
                             {'warning_rate': thresholds['invalid_warning_rate'], 'critical_rate': thresholds['invalid_critical_rate'],
                              'window_size': thresholds['invalid_window_size']}, invalid_count,
                             {'window_events': len(window), 'invalid_events': invalid_count,
                              'invalid_rate': rate, 'alternations': alternations,
                              'consecutive_invalid': state['invalid_consecutive']}))
    count = state['summary_unchanged_generations']
    if count >= thresholds['summary_warning_generations']:
        severity = ('critical' if formal_unchanged and count >= thresholds['summary_critical_generations'] else 'warning')
        active.append(_alert(task, 'SUMMARY_STAGNANT', severity,
                             {'warning_generations': thresholds['summary_warning_generations'],
                              'critical_generations': thresholds['summary_critical_generations']}, count,
                             {'unchanged_generations': count, 'summary_fingerprint': state['summary_hash'],
                              'formal_progress_unchanged': bool(formal_unchanged)}))
    count = len(state['queue_timeouts'])
    if count >= thresholds['queue_timeout_warning']:
        active.append(_alert(task, 'REPEATED_QUEUE_EXPIRY', 'warning', thresholds['queue_timeout_warning'], count,
                             {'attempts': count, 'window_seconds': thresholds['queue_timeout_window_seconds']}, 'gpu_scheduler'))
    attempts = (raw.get('scheduler_view') or {}).get('attempts', [])
    job = task.get('active_job') or task.get('latest_job') or (attempts[-1] if attempts else {})
    job_state = job.get('state', job.get('status'))
    if job_state == 'INFEASIBLE':
        resources = {key: job.get(key) for key in ('requested_memory_mib', 'requested_cu', 'capacity_memory_mib', 'capacity_cu') if finite(job.get(key))}
        resources.update({key: value for key, value in job.get('resources', {}).items()
                          if key in ('memory_mib', 'compute_units', 'ram_mib', 'cpu_cores') and finite(value)})
        active.append(_alert(task, 'RESOURCE_INFEASIBLE', 'critical', 'scheduler INFEASIBLE', 1, resources, 'gpu_scheduler'))
    if job_state == 'EXPIRED':
        active.append(_alert(task, 'GPU_QUEUE_EXPIRED', 'warning', 'scheduler EXPIRED', 1,
                             {'state': 'EXPIRED'}, 'gpu_scheduler'))
    if job_state in ('UNKNOWN', 'RECONCILING') and job.get('reconciling') is True:
        age = now - (epoch(job.get('updated_at', job.get('last_updated_at', job.get('submitted_at')))) or now)
        if age >= thresholds['reconcile_timeout_seconds']:
            active.append(_alert(task, 'GPU_RECONCILIATION_TIMEOUT', 'warning', thresholds['reconcile_timeout_seconds'], 1,
                                 {'reconciling_seconds': age}, 'gpu_scheduler'))
    memory = _memory(raw, task, host)
    totals = {key: sum(row[key] for row in memory if finite(row.get(key))) for key in ('oom', 'oom_kill', 'max_events')}
    at_max = any(finite(row.get('max_bytes')) and row['max_bytes'] > 0 and finite(row.get('current_bytes'))
                 and row['current_bytes'] >= row['max_bytes'] for row in memory)
    if totals['oom'] or totals['oom_kill'] or at_max or task.get('exit_code') in (137, -9) and totals['max_events']:
        active.append(_alert(task, 'CONTAINER_MEMORY_LIMIT', 'critical' if totals['oom'] or totals['oom_kill'] else 'warning',
                             'cgroup memory.events or memory.max', int(max(1, totals['oom'] + totals['oom_kill'])),
                             {**totals, 'at_memory_max': at_max}, 'cgroup'))
    queue = task.get('queue', job.get('queue', {}))
    queue = queue if isinstance(queue, dict) else {}
    reason = queue.get('blocking_reason', job.get('blocking_reason', job.get('reason')))
    wait = queue.get('wait_seconds', job.get('wait_seconds', job.get('waiting_seconds')))
    if not finite(wait):
        submitted = epoch(job.get('submitted_at'))
        wait = max(0, now - submitted) if submitted is not None and job_state in WAITING_STATES else 0
    external = queue.get('external_blocked', queue.get('blocked_by_external', False)) or reason in ('external_occupancy', 'external_reserved', 'external_reservation', 'external_jobs')
    if external and wait >= thresholds['external_block_seconds']:
        aggregate = queue.get('external_queue_summary', task.get('external_queue_summary', host.get('external_queue_summary', {})))
        evidence = {key: aggregate[key] for key in ('count', 'memory_mib', 'cu', 'wait_seconds') if isinstance(aggregate, dict) and finite(aggregate.get(key))}
        if isinstance(aggregate, dict):
            evidence['resources'] = {key: value for key, value in aggregate.get('resources', {}).items()
                                     if key in ('memory_mib', 'compute_units', 'ram_mib', 'cpu_cores') and finite(value)}
        evidence.update(wait_seconds=wait, blocking_reason=_reason(reason) or 'external_occupancy')
        active.append(_alert(task, 'EXTERNAL_OCCUPANCY_BLOCKING', 'warning', thresholds['external_block_seconds'], 1, evidence, 'gpu_scheduler'))
    external = task.get('external_queue_summary', queue.get('external_queue_summary', host.get('external_queue_summary', {})))
    if isinstance(external, dict):
        own_wait = external.get('estimated_wait_seconds', external.get('oldest_wait_seconds'))
        if external.get('blocked_by_our_reservation') is True and finite(own_wait) and own_wait >= thresholds['own_block_seconds']:
            active.append(_alert(task, 'OWN_RESERVATION_BLOCKING_EXTERNAL', 'warning', thresholds['own_block_seconds'], 1,
                                 {'estimated_wait_seconds': own_wait, 'suggestion': 'review_memory_and_compute_reservations'}, 'gpu_scheduler'))
    return active


def _transition(old: list[dict], active: list[dict], now: float, gaps: list[str]) -> list[dict]:
    by_id = {row['id']: deepcopy(row) for row in old}
    active_ids = set()
    for alert in active:
        identifier = alert['id']
        active_ids.add(identifier)
        prior = by_id.get(identifier)
        fingerprint = digest([alert['severity'], alert['observed_count'], alert['evidence']])
        if prior:
            alert.update(first_seen=prior['first_seen'], last_seen=utc(now),
                         observation_count=prior.get('observation_count', 1) + (fingerprint != prior.get('fingerprint')),
                         reopen_count=prior.get('reopen_count', 0) + (prior.get('state') == 'resolved'))
        else:
            alert.update(first_seen=utc(now), last_seen=utc(now), observation_count=1, reopen_count=0)
        alert.update(state='open', data_gap=gaps, fingerprint=fingerprint)
        by_id[identifier] = alert
    for identifier, alert in by_id.items():
        if identifier not in active_ids and alert.get('state') == 'open':
            if gaps:
                alert['data_gap'] = gaps
            else:
                alert.update(state='resolved', resolved_at=utc(now), data_gap=[])
    return sorted(by_id.values(), key=lambda alert: (alert['state'] != 'open', alert['severity'] != 'critical', alert['id']))


def _category(state: str, policy: str | None) -> str:
    if state in WAITING_STATES:
        return 'waiting' if state != 'UNKNOWN' else 'unknown'
    if state in ('FAILED', 'EXPIRED', 'TIMED_OUT', 'RETRYING'):
        return 'failed_retry'
    if state in ('STOPPED', 'CANCELLED'):
        return 'manual_stop'
    if state == 'RUNNING':
        return 'pending' if policy in ('reported', 'successful_turn') else 'credited'
    if state in ('COMPLETED', 'SUCCEEDED'):
        return 'completed'
    return 'not_started' if state == 'NOT_STARTED' else 'unknown'


def timeline(task: dict, old: list[dict], events: list[dict], document: dict,
             now: float, gaps: list[str]) -> list[dict]:
    """48 rolling intervals ending at now, based only on events and observations."""
    first = now - BUCKET_SECONDS * BUCKET_COUNT
    samples = []
    for bucket in old:
        begin, end = epoch(bucket.get('start_at')), epoch(bucket.get('end_at'))
        if begin is not None and end is not None and end > first:
            samples.append((begin, end, bucket.get('category', 'unknown'), bucket.get('data_gap', []), bucket.get('event_categories', [])))
    turn = document.get('turn', {})
    started = epoch(turn.get('started_at')) if isinstance(turn, dict) else None
    state = task.get('state', 'UNKNOWN')
    policy = task.get('timing', {}).get('credit_policy')
    category = _category('WAITING_GPU' if task.get('timing', {}).get('waiting_gpu') else state, policy)
    if gaps:
        category = 'unknown'
    previous_time = epoch(task.get('_diagnostic_previous_at'))
    observed_start = previous_time if previous_time is not None else now - 1
    if started is not None and state == 'RUNNING' and not gaps and previous_time is None:
        observed_start = started
    samples.append((max(first, observed_start), now, category, gaps, []))
    transitions = []
    for event in events:
        name = event['event']
        mark = ('waiting' if name == 'turn.gpu_state' and event.get('waiting') else
                'pending' if name == 'turn.gpu_state' and event.get('waiting') is False and policy in ('reported', 'successful_turn') else
                'credited' if name == 'turn.gpu_state' and event.get('waiting') is False else
                'failed_retry' if name == 'turn.retry_scheduled' or event.get('reason') in AGENT_FAILURES else
                'manual_stop' if event.get('reason') == 'operator_stop' else
                'pending' if name == 'turn.started' and policy in ('reported', 'successful_turn') else
                'credited' if name == 'turn.started' else
                'completed' if name == 'turn.finished' and event.get('returncode') == 0 else 'event')
        if mark != 'event':
            transitions.append((event['at'], mark, name))
        if event['at'] >= first:
            samples.append((event['at'], event['at'] + .001, mark, [], [name]))
    for index, (at, mark, name) in enumerate(transitions):
        end = transitions[index + 1][0] if index + 1 < len(transitions) else now
        # Fresh endpoint gaps supersede the last known event, while older valid
        # intervals remain inspectable in the rolling history.
        if gaps:
            end = min(end, observed_start)
        if end > first and end > at:
            samples.append((max(first, at), end, mark, [], [name]))
    result = []
    for index in range(BUCKET_COUNT):
        begin, end = first + index * BUCKET_SECONDS, first + (index + 1) * BUCKET_SECONDS
        overlaps = [sample for sample in samples if sample[0] < end and sample[1] >= begin]
        if overlaps:
            dominant = max(overlaps, key=lambda sample: (sample[1], sample[0]))
            current = dominant[2]
            holes = sorted(set(reason for sample in overlaps for reason in sample[3]))
            categories = sorted(set(kind for sample in overlaps for kind in sample[4]))
        else:
            current, holes, categories = 'unknown', ['no_registered_history'], []
        result.append({'start_at': utc(begin), 'end_at': utc(end), 'state': current,
                       'category': current, 'event_categories': categories,
                       'waiting': any(s[2] == 'waiting' for s in overlaps),
                       'running': any(s[2] in ('credited', 'pending') for s in overlaps),
                       'failed_retry': any(s[2] == 'failed_retry' for s in overlaps),
                       'manual_stop': any(s[2] == 'manual_stop' for s in overlaps),
                       'data_gap': holes})
    return result


def augment(data: dict, cfg: dict, previous: dict | None, now: float,
            raw_hosts: dict, formal_unchanged: dict[str, bool] | None = None) -> dict:
    """Add persisted diagnostics to a snapshot, mutating and returning ``data``.

    ``raw_hosts`` is the same per-alias/endpoint probe map consumed by evaluate.
    ``formal_unchanged`` is an optional verified operator fact, containing only
    task IDs and booleans. A summary alone never escalates to research idling.
    """
    previous = previous or {}
    thresholds = validate_thresholds(cfg)
    old_tasks = {task['id']: task for task in previous.get('tasks', [])}
    old_states = previous.get('diagnostics', {}).get('tasks', {})
    states, alerts = {}, []
    specs = {task['id']: task for task in cfg.get('tasks', [])}
    for task in data.get('tasks', []):
        spec = specs.get(task['id'], {})
        old = old_tasks.get(task['id'], {})
        host = raw_hosts.get(spec.get('host', task.get('host')), raw_hosts.get(task.get('endpoint_id'), {}))
        task['endpoint_id'] = host.get('endpoint_id', task.get('endpoint_id', task.get('host')))
        raw = host.get('tasks', {}).get(task['id'], {})
        document = _doc(raw, spec.get('status', {}).get('path'))
        controller = spec.get('controller') or {}
        if controller and not (controller.get('type') == 'research_handoff'
                               and document.get('controller') == 'autoresearch-longrun'
                               and document.get('run_id') == controller.get('run_id')):
            document = {}
        events, gaps = _events(raw, now)
        if host.get('error') or task.get('state') == 'UNREACHABLE':
            gaps.append('endpoint_unreachable')
        if task.get('controller') and not document:
            gaps.append('controller_status_unavailable')
        prior_state = old_states.get(task['id'], {})
        scope = digest([task['endpoint_id'], spec.get('root'), spec.get('status'),
                        (spec.get('controller') or {}).get('run_id'), spec.get('scheduler')])
        if prior_state.get('scope_fingerprint', scope) != scope:
            prior_state, old = {}, {}
        state = _history(prior_state, events, now, thresholds)
        state['scope_fingerprint'] = scope
        if state.get('out_of_order_events', 0) > prior_state.get('out_of_order_events', 0):
            gaps.append('out_of_order_events_ignored')
        progress = document.get('progress', {})
        if isinstance(progress, dict):
            count = progress.get('consecutive_same_context_summary')
            summary = progress.get('last_context_summary_sha256')
            generation = progress.get('last_context_summary_generation')
            if (finite(count) and count >= 0 and isinstance(summary, str) and re.fullmatch(r'[a-fA-F0-9]{64}', summary)
                    and (state.get('summary_generation') is None or finite(generation) and generation >= state['summary_generation'])):
                state.update(summary_unchanged_generations=int(count), summary_hash=summary,
                             summary_generation=generation)
        task['timing'] = timing(task, spec, document, host, now, events)
        gaps = sorted(set(gaps + task['timing']['data_gap']))
        active = _rules(task, raw, host, state, now, thresholds, gaps,
                        (formal_unchanged or {}).get(task['id']) is True)
        task['alert_history'] = _transition(old.get('alert_history', []), active, now, gaps)
        task['diagnostic_summary'] = {key: state[key] for key in ('retry_consecutive', 'summary_unchanged_generations', 'latest_event') if key in state}
        task['retry_count'] = state['retry_consecutive']
        task['summary_stagnant_generations'] = state['summary_unchanged_generations']
        if state.get('latest_event'):
            task['latest_event'] = state['latest_event']['event']
        heartbeat_doc = document.get('heartbeat') or {}
        heartbeat = epoch(heartbeat_doc.get('last_event_at')) if isinstance(heartbeat_doc, dict) else None
        task['heartbeat_age_seconds'] = max(0, now - heartbeat) if heartbeat is not None and heartbeat <= now else None
        task['_diagnostic_previous_at'] = state.get('last_observed_at')
        task['timeline_12h'] = timeline(task, old.get('timeline_12h', []), events, document, now, gaps)
        task.pop('_diagnostic_previous_at', None)
        for stream in task.get('streams', []):
            stream['timeline_12h'] = deepcopy(task['timeline_12h'])
        state['last_observed_at'] = utc(now)
        states[task['id']] = state
        alerts.extend(task['alert_history'])
    data['diagnostics'] = {'version': 1, 'tasks': states}
    data['alerts'] = alerts
    data['display'] = {'operator_entrypoint': 'watch', 'timeline_hours': 12,
                       'timeline_bucket_seconds': BUCKET_SECONDS, 'layout': 'unified'}
    return data
