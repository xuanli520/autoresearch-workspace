#!/usr/bin/env python3
"""AutoResearch controller. Linux/Python 3.10+, task-owned argv and JSONL events.

Use --remote connection.json to control a cloud-resident supervisor over SSH.
The supervisor, guard, worker and state always live on the execution host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from types import FrameType
from typing import Any

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE / 'core'))
from longrun import (ControllerError, DEFAULTS, TERMINAL_STATES, COMPLETION_TERMINAL_STATES, append_event, apply_context_usage, atomic_json,
    begin_run, budget_view, check_storage, compact_context, epoch, file_lock, finish_active_interval,
    context_guard_deadline, load_config, load_state, new_state, read_json, record_context_usage, reopen_context, safe_summary,
    record_progress_summary, run_config, save_state, start_active_interval, state_path, utc_now, validate_config)
from turn_outcome import compute_turn_outcome
from gpu_wait import (INFRASTRUCTURE_REASONS, close_gpu_wait, gpu_infrastructure_reason, gpu_wait_seconds,
                      observe_gpu_state)
from processes import boot_id, pid_matches, process_start_ticks, scope_members, signal_identity, terminate_scope
from cleanup import cleanup_task
from credit import partial_report, validate_evidence, validate_intervals
from docker_network import bridge_preflight
from completion import (COMPLETION_FAILURES, contract_for_run, diagnostic_receipt, digest,
    evidence_heartbeat, inspect_evaluation, issue_receipt, timestamp, trust_boundary, validate_receipt, within)

STOP_FILE = 'STOP'
DEFAULT_STATE_DIR = '.autoresearch-controller'
MAX_LINE = DEFAULTS.max_agent_line_bytes
EXIT_CODES = {'FAILED': 1, 'EXPIRED': 3, 'FINALIZING': 4,
              **{status: 4 for status in COMPLETION_FAILURES}}
RETRYABLE_TURN_REASONS = frozenset({
    'agent_exit_nonzero', 'completion_missing', 'context_usage_missing', 'invalid_agent_event',
    'agent_reported_failure', 'heartbeat_stale', 'controller_lost', 'worker_error',
    'worker_exit_missing', 'turn_timeout',
})
CONTEXT_BOUNDARY_REASONS = frozenset({
    'turn_completed', 'context_window', 'completion_missing',
    'agent_reported_failure', 'agent_exit_nonzero', 'worker_exit_missing',
    'turn_timeout', 'invalid_agent_event', 'heartbeat_stale', 'worker_error',
})


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task_config(run_dir: Path) -> dict[str, Any]:
    return run_config(run_dir)


def stop_reason(run_dir: Path) -> str | None:
    marker = run_dir / STOP_FILE
    if not marker.exists():
        return None
    record = read_json(marker)
    return record.get('code', 'operator_stop')


def persist_exit(run_dir: Path, state: dict[str, Any]) -> int:
    code = EXIT_CODES.get(state['status'], 0)
    view = budget_view(state)
    if state.get('stop_reason') == 'operator_stop' and state.get('target_reached_at_stop') is None:
        # The final active interval is normally closed before this receipt is
        # written.  Preserve the observed boundary even when an operator stop
        # races a controller exit or recovery path.
        state['target_reached_at_stop'] = bool(view['target_reached'])
    result = {'at': utc_now(), 'attempt': state['attempt'], 'status': state['status'],
              'exit_code': code, 'stop_reason': state['stop_reason'], 'resume_required': state['resume_required'],
              'target_reached_at_stop': state.get('target_reached_at_stop'),
              'target_reached': view['target_reached'], 'budget': view,
              'completion': state.get('completion')}
    atomic_json(run_dir / 'attempts' / f"{state['attempt']:06d}" / 'exit.json', result)
    atomic_json(run_dir / 'exit.json', result)
    return code


def frozen_completion_contract(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    path = within(run_dir, state['completion']['contract'])
    value = read_json(path)
    if digest(value) != state['completion']['contract_hash']:
        raise ControllerError('frozen completion contract changed')
    return value


def completion_step(run_dir: Path, state: dict[str, Any], config: dict[str, Any], *, live: bool = False) -> str:
    """One durable close-out step. This function has no job submission path."""
    if state['status'] in COMPLETION_TERMINAL_STATES:
        return state['status']
    state.update(status='FINALIZING', stop_reason='completion_gate')
    state['completion'].update(phase='FINALIZING', last_checked_at=utc_now())
    state['controller_heartbeat_monotonic'] = time.monotonic()
    save_state(run_dir, state)
    try:
        contract = frozen_completion_contract(run_dir, state)
    except (OSError, ValueError, KeyError, TypeError):
        status = 'PROTOCOL_BINDING_MISMATCH'
        state.update(status=status, stop_reason='completion_contract_changed', resume_required=False)
        state['completion'].update(phase=status, decided_at=utc_now(),
            issues=[{'code': 'CONTRACT_BINDING_MISMATCH', 'message': 'frozen completion contract is missing or changed'}])
        atomic_json(run_dir / 'completion.decision.json', {'version': 1, 'run_id': state['run_id'],
            'status': status, 'at': utc_now(), 'original_deadline': state['budget']['hard_deadline_at'],
            'issues': state['completion']['issues']})
        save_state(run_dir, state)
        return status
    scored = contract['score_expectation'] == 'required'
    path = None
    verdict = None
    try:
        path = (within(contract['completion']['evidence_root'], contract['completion']['receipt']) if scored
                else within(run_dir, 'completion.receipt.json'))
        if path.is_file():
            receipt = read_json(path)
            verdict = validate_receipt(contract, receipt, live=live)
    except (OSError, ValueError, KeyError, TypeError):
        verdict = {'ok': False, 'status': 'COMPLETION_RECEIPT_MISSING', 'jobs': [],
                   'issues': [{'code': 'RECEIPT_INVALID', 'message': 'completion receipt is malformed or unsafe'}]}
    # An on-time, durable receipt survives a crash. An already expired run never gets here.
    if verdict is None or not verdict['ok']:
        if budget_view(state)['hard_reached'] or time.time() >= timestamp(contract['deadline']):
            observation = inspect_evaluation(contract, live=False) if scored else {'jobs': []}
            verdict = {'ok': False, 'status': 'INCOMPLETE_FINAL_SCORE' if scored else 'EXPIRED',
                       'jobs': observation['jobs'], 'issues': [{'code': 'ORIGINAL_DEADLINE_REACHED',
                       'message': 'original hard deadline reached before valid completion'}]}
        elif verdict is None and scored:
            try:
                receipt = issue_receipt(contract, live=live)
                verdict = validate_receipt(contract, receipt, live=live)
            except (OSError, ValueError, KeyError, TypeError):
                verdict = {'ok': False, 'status': 'COMPLETION_RECEIPT_MISSING', 'jobs': [],
                           'issues': [{'code': 'RECEIPT_WRITE_FAILED',
                                       'message': 'trusted completion receipt could not be sealed'}]}
        elif verdict is None:
            receipt = diagnostic_receipt(contract)
            atomic_json(path, receipt)
            verdict = validate_receipt(contract, receipt)
    state['completion'].update(last_checked_at=utc_now(), jobs=verdict['jobs'], issues=verdict['issues'])
    status = verdict['status']
    if status == 'EVALUATION_PENDING':
        state['completion']['evaluation_status'] = status
    else:
        state.update(status=status, stop_reason='completion_gate_passed' if verdict['ok'] else status.lower(),
                     resume_required=False)
        state['completion'].update(phase=status, decided_at=utc_now())
        if verdict['ok']:
            state['completion'].update(receipt=str(path), receipt_sha256=sha256(path))
            if scored:
                state['completion']['scientific_score'] = receipt['scientific_score']
        atomic_json(run_dir / 'completion.decision.json', {
            'version': 1, 'run_id': state['run_id'], 'task_id': state['task_id'], 'status': status,
            'at': utc_now(), 'original_deadline': contract['deadline'], 'contract_hash': digest(contract),
            'jobs': verdict['jobs'], 'issues': verdict['issues'],
            'receipt_sha256': state['completion'].get('receipt_sha256')})
    state['controller_heartbeat_monotonic'] = time.monotonic()
    save_state(run_dir, state)
    return status


def init_run(config_path: Path | None, state_dir: Path, run: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = load_config(config_path) if config is None else validate_config(config)
    if not Path(config['root']).is_absolute():
        raise ControllerError('root must be absolute for remote initialization')
    root = Path(config['root']).resolve()
    cwd = (root / config['workdir']).resolve()
    if not cwd.is_relative_to(root) or not cwd.is_dir():
        raise ControllerError('workdir must exist within root on the execution host')
    run_dir = state_path(state_dir, run)
    contract = contract_for_run(config, run)
    trust_boundary(contract)
    if config['score_expectation'] == 'required' and run_dir.is_relative_to(root):
        raise ControllerError('scored run state and controller audit must be outside the candidate root')
    storage = check_storage(config['storage']['data_mount'], root, run_dir)
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name in ('context', 'turns', 'attempts'):
        (run_dir / name).mkdir()
    atomic_json(run_dir / 'config.json', config)
    state = new_state(config, run, run_dir)
    atomic_json(run_dir / 'completion.contract.json', contract)
    state['completion']['contract_hash'] = digest(contract)
    state.update(config_sha256=sha256(run_dir / 'config.json'), process_boot_id=boot_id(), storage=storage)
    # Pin executable sources used by this run. Running code is never hot-updated.
    from bundle import source_hashes
    state['source_sha256'] = source_hashes()
    save_state(run_dir, state)
    append_event(run_dir, 'run.created', config_sha256=state['config_sha256'], storage=storage)
    return safe_summary(state)


def recover_locked(run_dir: Path, *, reason: str = 'controller_lost') -> dict[str, Any]:
    state = load_state(run_dir)
    if pid_matches(state.get('controller_pid'), state.get('controller_start_ticks'), state.get('process_boot_id')):
        raise ControllerError('controller is still alive')
    token = state['turn'].get('token')
    if token and not terminate_scope(token, 0):
        raise ControllerError('worker cleanup incomplete; recovery is blocked')
    if token and not cleanup_task(Path(state['turn']['dir'])):
        raise ControllerError('task cleanup hook failed; inspect cleanup.log before resuming')
    if state['status'] in COMPLETION_TERMINAL_STATES:
        state.update(controller_pid=None, controller_start_ticks=None)
        if not (run_dir / 'exit.json').exists():
            persist_exit(run_dir, state)
        save_state(run_dir, state)
        return safe_summary(state)
    if state.get('completion', {}).get('phase') == 'FINALIZING':
        completion_step(run_dir, state, task_config(run_dir), live=False)
        state.update(controller_pid=None, controller_start_ticks=None,
                     resume_required=state['status'] == 'FINALIZING')
        persist_exit(run_dir, state)
        save_state(run_dir, state)
        append_event(run_dir, 'run.completion_recovered', status=state['status'])
        return safe_summary(state)
    if not state.get('controller_pid') and not state['budget'].get('active_started_at'):
        return safe_summary(state)
    if state['budget'].get('active_started_at'):
        worker = read_json(Path(state['turn']['dir']) / 'worker-exit.json', {})
        elapsed = finish_active_interval(state, credit=False, duration=worker.get('runtime_seconds', 0))
        state['turn'].update(status='FAILED', ended_at=utc_now(), reason=reason, credited=False,
                             elapsed_seconds=elapsed, runtime_observed='runtime_seconds' in worker)
        atomic_json(Path(state['turn']['dir']) / 'exit.json', state['turn'])
    if state['status'] not in TERMINAL_STATES or state.get('controller_pid'):
        state.update(status='EXPIRED' if budget_view(state)['hard_reached'] else 'PAUSED',
                     stop_reason=reason, resume_required=True)
    state.update(controller_pid=None, controller_start_ticks=None)
    persist_exit(run_dir, state)
    save_state(run_dir, state)
    append_event(run_dir, 'run.recovered', reason=reason, credited_seconds=0)
    return safe_summary(state)


def recover_run(run_dir: Path) -> dict[str, Any]:
    with file_lock(run_dir / '.controller.lock', blocking=False):
        return recover_locked(run_dir)


def rebase_boot_run(run_dir: Path, *, reason: str) -> dict[str, Any]:
    """Reconcile a controller-lost run after a host reboot.

    A reboot invalidates the old monotonic clock, but it does not consume the
    wall-clock deadline. This operation records the old state and exit receipt,
    then establishes a new monotonic baseline without changing the deadline,
    target, credit, or scientific configuration.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ControllerError('boot rebase requires an explicit reason')
    with file_lock(run_dir / '.controller.lock', blocking=False), file_lock(run_dir / '.state.lock'):
        state = load_state(run_dir)
        current_boot = boot_id()
        previous_boot = state.get('budget', {}).get('boot_id')
        if not previous_boot or previous_boot == current_boot:
            raise ControllerError('boot rebase requires a changed host boot identity')
        if state.get('status') not in {'EXPIRED', 'PAUSED', 'STOPPED', 'FAILED'}:
            raise ControllerError('boot rebase requires a stopped or controller-lost run')
        # A host reboot invalidates the saved monotonic clock.  A run may have
        # been marked hard_limit during a failed resume (or authorized budget
        # extension) before that mismatch was reconciled; the wall-clock
        # checks below still decide whether rebasing is safe.
        if state.get('stop_reason') not in {'controller_lost', 'authorized_deadline_extension', 'hard_limit'}:
            raise ControllerError('boot rebase requires a controller-loss or reboot-affected stop reason')
        if state.get('completion', {}).get('phase') not in (None, 'NOT_STARTED'):
            raise ControllerError('boot rebase cannot alter a completion workflow')
        if (pid_matches(state.get('controller_pid'), state.get('controller_start_ticks'), previous_boot)
                or pid_matches(state.get('guard_pid'), state.get('guard_start_ticks'), previous_boot)):
            raise ControllerError('controller or guard is still alive; recover it first')
        if state['budget'].get('active_started_at') is not None:
            raise ControllerError('active interval is still open; recover it first')
        if scope_members(state['turn'].get('token', '')):
            raise ControllerError('owned worker is still alive; cleanup is required before rebasing')
        if state['turn'].get('token') and not cleanup_task(Path(state['turn']['dir'])):
            raise ControllerError('task cleanup hook failed; inspect cleanup.log before rebasing')

        config = task_config(run_dir)
        started = epoch(state['budget'].get('started_at'))
        deadline = epoch(state['budget'].get('hard_deadline_at'))
        declared = epoch(state.get('declared_deadline'))
        configured = timestamp(config['deadline'])
        expected_deadline = None if started is None else min(
            started + float(state['budget']['hard_limit_seconds']), configured)
        if (started is None or deadline is None or declared is None or expected_deadline is None
                or abs(deadline - expected_deadline) > 1e-3):
            raise ControllerError('boot rebase requires an intact original deadline')
        if abs(declared - configured) > 1e-6:
            raise ControllerError('boot rebase requires the declared deadline to remain unchanged')
        now = time.time()
        if now < state['budget'].get('last_observed_epoch', started):
            raise ControllerError('wall clock rolled back; original deadline cannot be reconciled safely')
        wall = max(float(state['budget'].get('wall_age_seconds', 0)), now - started)
        remaining = min(state['budget']['hard_limit_seconds'] - wall, deadline - time.time())
        if remaining <= 0:
            raise ControllerError('original hard deadline has passed; use the expiry extension contract')
        if state['budget']['active_seconds'] >= state['budget']['window_seconds']:
            raise ControllerError('research target is already reached')

        from bundle import source_hashes
        sources = source_hashes()
        recovery_root = run_dir / 'recovery'
        recovery_root.mkdir(exist_ok=True)
        number = 1
        while (recovery_root / f'{number:06d}').exists():
            number += 1
        folder = recovery_root / f'{number:06d}'
        folder.mkdir()
        atomic_json(folder / 'previous-state.json', state)
        atomic_json(folder / 'previous-exit.json', read_json(run_dir / 'exit.json', {}))
        receipt = {
            'version': 1,
            'at': utc_now(),
            'reason': reason,
            'kind': 'boot_rebase',
            'previous_boot_id': previous_boot,
            'current_boot_id': current_boot,
            'previous_status': state['status'],
            'previous_stop_reason': state['stop_reason'],
            'hard_deadline_at': state['budget']['hard_deadline_at'],
            'active_seconds_preserved': state['budget']['active_seconds'],
            'remaining_wall_seconds': remaining,
            'previous_config_sha256': state['config_sha256'],
            'previous_source_sha256': state['source_sha256'],
            'source_sha256': sources,
        }
        atomic_json(folder / 'receipt.json', receipt)
        state['budget'].update(boot_id=current_boot, started_monotonic=time.monotonic(),
                               active_monotonic=None)
        state.update(controller_pid=None, controller_start_ticks=None, guard_pid=None,
                     guard_start_ticks=None, process_boot_id=current_boot, status='PAUSED',
                     stop_reason='boot_rebased', resume_required=True, source_sha256=sources,
                     controller_release=str(HERE))
        state['turn'].update(pid=None, pid_start_ticks=None)
        save_state(run_dir, state)
        append_event(run_dir, 'run.boot_rebased', recovery=str(folder.relative_to(run_dir)),
                     previous_boot_id=previous_boot, current_boot_id=current_boot,
                     hard_deadline_at=state['budget']['hard_deadline_at'],
                     active_seconds_preserved=state['budget']['active_seconds'])
        return {'recovery': receipt, 'status': safe_summary(state)}


def amend_run(run_dir: Path, config: dict[str, Any], *, expected_sha256: str, expected_turn: int,
              reason: str, credit: dict[str, Any] | None = None, extend_expired_budget: bool = False,
              storage_migration_source: Path | None = None) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise ControllerError('amend requires an explicit authorization reason')
    with file_lock(run_dir / '.controller.lock', blocking=False), file_lock(run_dir / '.state.lock'):
        state = load_state(run_dir)
        old = task_config(run_dir)
        config = validate_config(config)
        if state['config_sha256'] != expected_sha256 or state['turn']['number'] != expected_turn:
            raise ControllerError('stale amendment; reload state before applying')
        if (state.get('controller_pid') or state['budget'].get('active_started_at')
                or pid_matches(state.get('guard_pid'), state.get('guard_start_ticks'), state.get('process_boot_id'))
                or scope_members(state['turn'].get('token', ''))):
            raise ControllerError('stop and confirm controller/guard/worker cleanup before amending')
        view = budget_view(state)
        expired = state['status'] == 'EXPIRED' or view['hard_reached']
        if state['status'] in COMPLETION_TERMINAL_STATES - {'EXPIRED'} or view['target_reached']:
            raise ControllerError('cannot amend a completed budget')
        if expired and not extend_expired_budget:
            raise ControllerError('expired budget requires explicit --extend-expired-budget authorization')
        deadline_changed = config['deadline'] != old['deadline']
        if deadline_changed and not extend_expired_budget:
            raise ControllerError('deadline extension requires explicit authorization')
        for key in ('task_id', 'root', 'workdir', 'context'):
            if config[key] != old[key]:
                raise ControllerError('amend cannot change task identity, storage or context contract')
        if config['storage'] != old['storage'] and storage_migration_source is None:
            raise ControllerError('storage change requires explicit --storage-migration-source')
        if storage_migration_source is not None and (deadline_changed or extend_expired_budget
                                                    or credit and credit.get('turns')):
            raise ControllerError('storage migration cannot change deadline or add historical credit')
        for key in ('stage', 'score_expectation', 'metric', 'direction', 'required_seeds',
                    'candidate_manifest', 'protocol_hash', 'completion'):
            if config[key] != old[key]:
                raise ControllerError('amend preserves the frozen scientific completion contract')
        for key in ('mode', 'window_seconds', 'credit_policy'):
            if config['budget'][key] != old['budget'][key]:
                raise ControllerError('amend preserves budget mode, target and credit policy')
        started = epoch(state['budget']['started_at'])
        if started is None:
            raise ControllerError('amend requires an already started run')
        deadline = min(started + config['budget']['hard_limit_seconds'], timestamp(config['deadline']))
        if deadline <= time.time():
            raise ControllerError('amended deadline must be in the future')
        if extend_expired_budget:
            if (not config['budget']['allow_extended_hard_limit']
                    or deadline <= epoch(state['budget']['hard_deadline_at'])):
                raise ControllerError('explicit expiry extension requires opt-in and a later deadline')
            if credit and credit.get('turns'):
                raise ControllerError('expiry extension cannot add historical credit')
        storage = check_storage(config['storage']['data_mount'], run_dir, Path(config['root']))
        migration = None
        if storage_migration_source is not None:
            from storage_migration import verify_migration
            migration = verify_migration(run_dir, Path(config['root']), state, storage,
                                         Path(storage_migration_source))
        elif storage != state['storage']:
            raise ControllerError('data device changed')
        credit = credit or {'run_id': state['run_id'], 'turns': [], 'evidence': []}
        if credit.get('run_id') != state['run_id'] or not isinstance(credit.get('turns'), list):
            raise ControllerError('credit audit belongs to another run or has invalid turns')
        previous = {n for adjustment in state.get('credit_adjustments', []) for n in adjustment['turns']}
        intervals, numbers = [], []
        for item in credit['turns']:
            number = item.get('turn')
            if type(number) is not int or not 1 <= number <= expected_turn or number in previous or number in numbers:
                raise ControllerError('duplicate, already audited or invalid credit turn')
            exited = read_json(run_dir / 'turns' / f'{number:06d}' / 'exit.json')
            if exited.get('credited_seconds', 0) != 0 or exited.get('credited'):
                raise ControllerError('credit adjustment cannot recount a credited turn')
            lower, upper = epoch(exited['started_at']), epoch(exited['ended_at'])
            pairs = item.get('intervals')
            if not isinstance(pairs, list):
                raise ControllerError('credit intervals must be an array')
            subtotal = 0
            for pair in pairs:
                if (not isinstance(pair, list) or len(pair) != 2
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in pair)
                        or not lower <= pair[0] < pair[1] <= upper):
                    raise ControllerError('credit interval falls outside its recorded turn')
                intervals.append(pair)
                subtotal += pair[1] - pair[0]
            if subtotal > exited.get('elapsed_seconds', 0) + .001:
                raise ControllerError('credit exceeds recorded worker runtime')
            numbers.append(number)
        ordered = sorted(intervals)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise ControllerError('credit intervals overlap')
        evidence = credit.get('evidence')
        if not isinstance(evidence, list) or (intervals and not evidence):
            raise ControllerError('credit requires hashed evidence')
        try:
            validate_evidence(evidence, config['storage']['data_mount'] or config['root'], required=bool(intervals))
        except (OSError, ValueError, KeyError) as exc:
            raise ControllerError(str(exc)) from exc
        delta = sum(b - a for a, b in ordered)
        from bundle import source_hashes
        sources = source_hashes()
        number = 1
        while (run_dir / 'amendments' / f'{number:06d}').exists():
            number += 1
        folder = run_dir / 'amendments' / f'{number:06d}'
        folder.mkdir(parents=True)
        previous_contract = frozen_completion_contract(run_dir, state)
        atomic_json(folder / 'previous-state.json', state)
        atomic_json(folder / 'previous-config.json', old)
        atomic_json(folder / 'previous-completion-contract.json', previous_contract)
        atomic_json(folder / 'credit-audit.json', credit)
        if migration is not None:
            atomic_json(folder / 'storage-migration.json', migration)
        atomic_json(folder / 'config.json', config)
        relative = str((folder / 'config.json').relative_to(run_dir))
        import datetime
        new_deadline = datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc).isoformat()
        contract = contract_for_run(config, state['run_id'], new_deadline)
        receipt = {'at': utc_now(), 'reason': reason, 'previous_deadline_at': state['budget']['hard_deadline_at'],
                   'hard_deadline_at': new_deadline, 'credited_seconds_added': delta, 'turns': numbers,
                   'expired_budget_extended': bool(expired and extend_expired_budget),
                   'previous_config_sha256': state['config_sha256'], 'config_sha256': sha256(folder / 'config.json'),
                   'previous_contract_hash': digest(previous_contract), 'contract_hash': digest(contract),
                   'controller_release': str(HERE), 'source_sha256': sources}
        if migration is not None:
            receipt['storage_migration'] = {'path': str(folder / 'storage-migration.json'),
                                            'sha256': sha256(folder / 'storage-migration.json')}
        atomic_json(folder / 'receipt.json', receipt)
        state['budget'].update(hard_limit_seconds=config['budget']['hard_limit_seconds'], hard_deadline_at=new_deadline,
                               active_seconds=state['budget']['active_seconds'] + delta)
        state['paths']['config'] = relative
        atomic_json(run_dir / 'completion.contract.json', contract)
        state['completion']['contract_hash'] = digest(contract)
        state['declared_deadline'] = config['deadline']
        state.update(config_sha256=receipt['config_sha256'], source_sha256=sources,
                     controller_release=str(HERE), resume_required=True, storage=storage)
        if state['status'] == 'EXPIRED':
            state.update(status='PAUSED', stop_reason='authorized_deadline_extension')
        state.setdefault('credit_adjustments', []).append({'path': str(folder / 'credit-audit.json'),
            'sha256': sha256(folder / 'credit-audit.json'), 'turns': numbers, 'credited_seconds': delta})
        save_state(run_dir, state)
        append_event(run_dir, 'run.amended', **receipt)
        return {'amendment': receipt, 'status': safe_summary(state)}


class LongRunController:
    def __init__(self, run_dir: Path, *, resume: bool = False, with_guard: bool = True,
                 request_id: str | None = None) -> None:
        self.run_dir = run_dir.resolve()
        self.config = task_config(run_dir)
        self.state = load_state(run_dir)
        self.resume, self.with_guard = resume, with_guard
        self.request_id = request_id or uuid.uuid4().hex
        self.process: subprocess.Popen[bytes] | None = None
        self.guard: subprocess.Popen[bytes] | None = None
        self.signalled = False
        self.pending_reason: str | None = None
        self.summary: str | None = None
        self.result: dict[str, Any] = {}
        self.partial_credit: dict[str, Any] = {}
        self.context_reported = False
        self.offset = 0
        self.fragment = b''
        self.dropping_line = False
        self.last_disk_check = 0
        self.retry_pending = False
        self.context_guard_triggered = False

    def save(self) -> None:
        self.state['controller_heartbeat_monotonic'] = time.monotonic()
        if self.state['budget'].get('started_at'):
            self.state['budget'].update(
                wall_age_seconds=budget_view(self.state)['wall_age_seconds'],
                last_observed_epoch=max(self.state['budget'].get('last_observed_epoch', 0), time.time()),
            )
        save_state(self.run_dir, self.state)

    def event(self, event: str, **fields: Any) -> None:
        append_event(self.run_dir, event, **fields)

    def record_method_summary(self, value: dict[str, Any]) -> None:
        """Persist a bounded duplicate-summary signal without changing credit.

        A repeated handoff can be a legitimate unchanged method, so this is a
        diagnostic only.  The state and event log make the signal available to
        monitors and later QA without inventing a turn failure.
        """
        record_progress_summary(self.run_dir, self.state, value.get('method_summary'),
                                source='method_summary')

    def check_startable(self) -> None:
        state = self.state
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise ControllerError('requires Linux pidfd support (kernel 5.3+, Python 3.10+)')
        if state.get('controller_pid'):
            if not self.resume:
                raise ControllerError('controller ownership exists; inspect status, then recover/--resume')
            recover_locked(self.run_dir)
            self.state = state = load_state(self.run_dir)
        if state['status'] in COMPLETION_TERMINAL_STATES:
            raise ControllerError(f"run is {state['status']}; its budget cannot be reset")
        if state['context']['state'] == 'COMPACTION_REQUIRED':
            raise ControllerError('context compact is required before starting')
        if (state['status'] in ('PAUSED', 'STOPPED', 'FAILED') or state['resume_required'] or
                (self.run_dir / STOP_FILE).exists()) and not self.resume:
            raise ControllerError('explicit --resume is required after stop/failure')
        if scope_members(state['turn'].get('token', '')):
            raise ControllerError('owned worker is still alive; recover before starting')
        if state['turn'].get('token') and not cleanup_task(Path(state['turn']['dir'])):
            raise ControllerError('previous task cleanup is incomplete; inspect cleanup.log')
        from bundle import source_hashes
        current_sources = source_hashes()
        for name, expected in state['source_sha256'].items():
            if current_sources.get(name) != expected:
                raise ControllerError(f'run source differs: {name}; use the original release')
        storage = check_storage(self.config['storage']['data_mount'], self.run_dir, Path(self.config['root']))
        if storage != state['storage']:
            raise ControllerError('data device changed since init; inspect the mount before resuming')
        if self.resume:
            (self.run_dir / STOP_FILE).unlink(missing_ok=True)
        state['resume_required'] = False

    def start_guard(self) -> None:
        if not self.with_guard:
            return
        with (self.run_dir / 'guard.log').open('a') as log:
            self.guard = subprocess.Popen([sys.executable, '-B', str(HERE / 'controller.py'), '--state-dir',
                str(self.run_dir.parents[1]), 'guard', '--run-id', self.state['run_id'], '--attempt',
                str(self.state['attempt'])], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True)
        self.state.update(guard_pid=self.guard.pid, guard_start_ticks=process_start_ticks(self.guard.pid))
        self.save()

    def prepare_context(self, turn_dir: Path) -> Path:
        ctx = self.state['context']
        snapshot = None
        if ctx.get('last_snapshot'):
            path = (self.run_dir / ctx['last_snapshot']).resolve()
            if not path.is_relative_to(self.run_dir / 'context'):
                raise ControllerError('snapshot path escaped context directory')
            snapshot = read_json(path)
            if snapshot['generation'] != ctx['generation'] or hashlib.sha256(snapshot['summary'].encode()).hexdigest() != snapshot['summary_sha256']:
                raise ControllerError('context snapshot failed generation/hash verification')
        payload = {**ctx, 'protocol': 'autoresearch-context-v1', 'run_id': self.state['run_id'],
                   'turn': self.state['turn']['number'], 'handoff': snapshot,
                   'budget': budget_view(self.state), 'turn_dir': str(turn_dir),
                   'compaction_request_file': str(turn_dir / 'context-request.json')}
        atomic_json(turn_dir / 'context.json', payload)
        atomic_json(self.run_dir / 'context/current.json', payload)
        return turn_dir / 'context.json'

    def start_turn(self) -> None:
        self.process = None
        self.partial_credit = {}
        if self.config['docker_network']['enabled']:
            receipt = bridge_preflight(self.config['docker_network'],
                                       repair_forwarding=self.config['docker_network']['repair_forwarding'])
            self.event('docker.network_preflight', **receipt)
        self.state['context'].update(
            guard_boot_id=None,
            summary_deadline_monotonic=None,
            summary_deadline_at=None,
            completion_grace_deadline_monotonic=None,
            completion_grace_deadline_at=None,
        )
        self.context_guard_triggered = False
        number = self.state['turn']['number'] + 1
        while (self.run_dir / 'turns' / f'{number:06d}').exists():
            number += 1
        turn_dir = self.run_dir / 'turns' / f'{number:06d}'
        turn_dir.mkdir()
        retry_state = self.state.setdefault('retry', {'anchor_turn': None, 'used': 0})
        retry_index = retry_state.get('used', 0)
        self.state['turn'] = {'number': number, 'status': 'STARTING', 'dir': str(turn_dir),
            'generation': self.state['context']['generation'], 'pid': None, 'pid_start_ticks': None,
            'token': uuid.uuid4().hex, 'started_at': utc_now(), 'returncode': None,
            'retry_of': retry_state.get('anchor_turn') if retry_index else None,
            'retry_index': retry_index}
        self.save()
        context_file = self.prepare_context(turn_dir)
        view = budget_view(self.state)
        seconds = min(self.config['turn']['seconds'], view['remaining_seconds'])
        if self.config['budget']['mode'] == 'wall' or self.config['budget']['credit_policy'] == 'running':
            seconds = min(seconds, view['target_remaining_seconds'])
        deadline_reason = 'target_reached' if seconds == view['target_remaining_seconds'] else (
            'hard_limit' if seconds == view['remaining_seconds'] else 'turn_timeout')
        self.state['turn'].update(deadline_monotonic=time.monotonic()+seconds, deadline_reason=deadline_reason,
                                  execution_seconds=seconds, runtime_synced=False)
        dirs = {name: turn_dir / name for name in ('tmp', 'cache')}
        for path in dirs.values():
            path.mkdir()
        cache_dir = dirs['cache']
        env = {'TMPDIR': str(dirs['tmp']), 'XDG_CACHE_HOME': str(cache_dir),
               'PIP_CACHE_DIR': str(cache_dir / 'pip'), 'HF_HOME': str(cache_dir / 'huggingface'),
               'TORCH_HOME': str(cache_dir / 'torch'), 'npm_config_cache': str(cache_dir / 'npm'), **self.config['env'],
               'AUTORESEARCH_RUN_ID': self.state['run_id'], 'AUTORESEARCH_TASK_ID': self.state['task_id'],
               'AUTORESEARCH_TURN': str(number), 'AUTORESEARCH_TURN_DIR': str(turn_dir),
               'AUTORESEARCH_CONTEXT_FILE': str(context_file),
               'AUTORESEARCH_COMPLETION_CONTRACT': str(self.run_dir / 'completion.contract.json'),
               'AUTORESEARCH_CONTROLLER_RELEASE': str(HERE),
               'AUTORESEARCH_CONTEXT_GENERATION': str(self.state['context']['generation']),
               'AUTORESEARCH_CONTEXT_MAX_TOKENS': str(self.state['context']['max_tokens']),
               'AUTORESEARCH_CONTEXT_COMPACT_AT_TOKENS': str(self.state['context']['compact_at_tokens']),
               'AUTORESEARCH_CONTEXT_RESERVE_TOKENS': str(self.state['context']['reserve_tokens']),
               'AUTORESEARCH_REMAINING_SECONDS': str(seconds),
               'AUTORESEARCH_PROCESS_TOKEN': self.state['turn']['token']}
        check_storage(self.config['storage']['data_mount'], *(Path(env[name]) for name in (
            'TMPDIR', 'XDG_CACHE_HOME', 'PIP_CACHE_DIR', 'HF_HOME', 'TORCH_HOME', 'npm_config_cache')))
        launch = {'command': self.config['command'], 'env': env,
                  'cwd': str(Path(self.config['root']) / self.config['workdir']),
                  'controller_pid': os.getpid(), 'controller_start_ticks': process_start_ticks(os.getpid()),
                  'boot_id': boot_id(), 'token': self.state['turn']['token'], 'seconds': seconds,
                  'grace_seconds': self.config['turn']['grace_seconds'],
                  'cleanup': self.config['cleanup'],
                  'docker_network': self.config['docker_network'],
                  'hard_deadline_epoch': epoch(self.state['budget']['hard_deadline_at'])}
        atomic_json(turn_dir / 'launch.json', launch)
        with (turn_dir / 'stdout.log').open('xb') as out, (turn_dir / 'stderr.log').open('xb') as err:
            self.process = subprocess.Popen([sys.executable, '-B', str(HERE / 'core/worker.py'), str(turn_dir)],
                env={**os.environ, **env}, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        self.state['turn'].update(pid=self.process.pid, pid_start_ticks=process_start_ticks(self.process.pid), status='RUNNING')
        self.state['heartbeat'] = {'last_event_at': None, 'last_monotonic': time.monotonic(), 'stale': False}
        start_active_interval(self.state)
        self.save()
        # This gate is opened only after worker ownership has been persisted.
        (turn_dir / 'GO').touch()
        self.event('turn.started', turn=number, generation=self.state['context']['generation'], seconds=seconds)

    def ingest_line(self, line: bytes) -> None:
        try:
            value = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        except (ValueError, UnicodeError, RecursionError):
            return
        if not isinstance(value, dict) or not isinstance(value.get('autoresearch'), str):
            return
        kind = value['autoresearch']
        self.event('agent.event', turn=self.state['turn']['number'], payload=value)
        try:
            if type(value.get('generation')) is not int or value['generation'] != self.state['context']['generation']:
                raise ControllerError('event belongs to a stale or missing context generation')
            if kind == 'heartbeat':
                self.state['heartbeat'].update(last_event_at=utc_now(), last_monotonic=time.monotonic())
            if kind == 'gpu.state':
                turn = self.state['turn']
                if observe_gpu_state(turn, value, time.monotonic()):
                    waiting = turn['status'] == 'WAITING_GPU'
                    self.state['status'] = 'WAITING_GPU' if waiting else 'RUNNING'
                    self.state['heartbeat'].update(last_event_at=utc_now(), last_monotonic=time.monotonic())
                    atomic_json(Path(turn['dir']) / 'gpu-wait.json', {
                        'version': 1, 'run_id': self.state['run_id'], 'turn': turn['number'],
                        'token': turn['token'], 'waiting': waiting,
                        'started_monotonic': turn['gpu_wait']['started_monotonic'],
                        'excluded_seconds': turn['gpu_wait']['excluded_seconds']})
                    self.event('turn.gpu_state', turn=turn['number'], request=value,
                               waiting=waiting, excluded_seconds=gpu_wait_seconds(turn, time.monotonic()))
                    if value['state'] == 'UNKNOWN' and value.get('reconciling') is not True:
                        self.pending_reason = 'gpu_unknown'
            if kind == 'context.usage' or kind == 'heartbeat' and 'used_tokens' in value:
                apply_context_usage(self.state, value.get('used_tokens'), generation=value.get('generation'),
                    conversation_id=value.get('conversation_id'), input_tokens=value.get('input_tokens'),
                    output_tokens=value.get('output_tokens'))
                self.context_reported = True
            if kind == 'context.compact':
                if type(value.get('generation')) is not int or value['generation'] != self.state['context']['generation'] or not isinstance(value.get('summary'), str) or not value['summary'].strip():
                    raise ControllerError('compaction requires current generation and non-empty summary')
                if len(value['summary'].encode()) > self.config['context']['max_summary_bytes']:
                    raise ControllerError('summary exceeds max_summary_bytes')
                self.summary = value['summary']
                self.state['context']['state'] = 'COMPACTION_REQUIRED'
                atomic_json(self.run_dir / 'context/pending-summary.json', value)
            if kind == 'turn.completed':
                if type(value.get('credit')) is not bool:
                    raise ControllerError('turn.completed requires boolean credit')
                if self.config['budget']['credit_policy'] == 'reported':
                    seconds = value.get('credited_seconds')
                    if (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
                            or not isinstance(value.get('credit_evidence'), str) or not value['credit_evidence'].strip()):
                        raise ControllerError('reported credit requires finite nonnegative seconds and credit_evidence')
                self.result = value
                self.record_method_summary(value)
            if kind == 'turn.credit':
                seconds = value.get('credited_seconds')
                if (not self.config['budget']['allow_partial_credit']
                        or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
                        or not isinstance(value.get('credit_evidence'), str) or not value['credit_evidence'].strip()):
                    raise ControllerError('partial credit requires explicit policy, finite seconds and evidence')
                self.partial_credit = value
            if kind == 'turn.failed':
                retryable = value.get('retryable', True)
                if type(retryable) is not bool:
                    raise ControllerError('turn.failed retryable must be boolean')
                infrastructure = value.get('reason') in INFRASTRUCTURE_REASONS
                if infrastructure:
                    if value.get('failure_class') != 'infrastructure':
                        raise ControllerError('GPU failure requires infrastructure failure_class')
                    self.result = value
                    self.state['failure'] = value
                    self.pending_reason = value['reason']
                elif not retryable:
                    if value.get('reason') != 'deterministic_evidence_failure':
                        raise ControllerError('nonretryable failure requires an explicit evidence failure reason')
                    path = Path(value.get('failure_evidence', '')).resolve(strict=True)
                    if (not path.is_relative_to(Path(self.state['turn']['dir']).resolve())
                            or sha256(path) != value.get('failure_evidence_sha256')):
                        raise ControllerError('failure evidence must match the current turn artifact')
                    self.result = value
                    self.state['failure'] = value
                    self.pending_reason = 'deterministic_evidence_failure'
                else:
                    self.pending_reason = 'agent_reported_failure'
        except (ControllerError, TypeError, OSError, ValueError) as exc:
            self.pending_reason = 'invalid_agent_event'
            self.event('agent.event_rejected', error=str(exc))

    def ingest_output(self) -> None:
        path = Path(self.state['turn']['dir']) / 'stdout.log'
        with path.open('rb') as stream:
            stream.seek(self.offset)
            data = stream.read(256*1024)
            self.offset = stream.tell()
        for part in data.splitlines(keepends=True):
            if not self.dropping_line:
                self.fragment += part
                if len(self.fragment) > MAX_LINE:
                    self.fragment = b''
                    self.dropping_line = True
            if part.endswith(b'\n'):
                if not self.dropping_line:
                    self.ingest_line(self.fragment)
                self.fragment, self.dropping_line = b'', False

    def fallback_compaction_summary(self, reason: str) -> str:
        """Build a bounded, explicitly incomplete handoff when the agent did not."""
        context = self.state['context']
        max_summary_bytes = self.config['context']['max_summary_bytes']
        previous_summary = None
        relative = context.get('last_snapshot')
        if relative:
            try:
                snapshot_path = (self.run_dir / relative).resolve()
                if snapshot_path.is_relative_to(self.run_dir / 'context'):
                    previous_summary = read_json(snapshot_path).get('summary')
            except (OSError, ControllerError, TypeError, ValueError):
                previous_summary = None
        method_summary = self.result.get('method_summary')
        if not isinstance(method_summary, str) or not method_summary.strip():
            method_summary = None

        def clip(value: str | None, byte_limit: int = 12000) -> str | None:
            if value is None:
                return None
            encoded = value.encode('utf-8')
            if len(encoded) <= byte_limit:
                return value
            marker = '\n[truncated]'.encode('utf-8')
            return (encoded[:max(0, byte_limit - len(marker))].decode('utf-8', errors='ignore')
                    + marker.decode('utf-8'))

        def render(value: dict[str, Any]) -> str:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)

        payload = {
            'controller_fallback': 'context_guard',
            'complete': False,
            'reason': reason,
            'generation': context['generation'],
            'turn': self.state['turn']['number'],
            'used_tokens': context['used_tokens'],
            'max_tokens': context['max_tokens'],
            'evidence_dir': self.state['turn']['dir'],
            'method_summary': clip(method_summary),
            'previous_handoff': clip(previous_summary if isinstance(previous_summary, str) else None),
            'next_steps': 'Start a fresh generation, inspect the retained turn evidence, and do not assume this turn succeeded.',
        }

        summary = render(payload)
        if len(summary.encode('utf-8')) > max_summary_bytes:
            payload['method_summary'] = None
            payload['previous_handoff'] = None
            summary = render(payload)
        if len(summary.encode('utf-8')) > max_summary_bytes:
            payload = {'controller_fallback': 'context_guard', 'complete': False,
                       'generation': context['generation'], 'turn': self.state['turn']['number'],
                       'reason': reason}
            summary = render(payload)
        if len(summary.encode('utf-8')) > max_summary_bytes:
            payload = {'controller_fallback': 'context_guard', 'complete': False}
            summary = render(payload)
        if len(summary.encode('utf-8')) > max_summary_bytes:
            payload = {'complete': False}
            summary = render(payload)
        if len(summary.encode('utf-8')) > max_summary_bytes:
            raise ControllerError('context.max_summary_bytes is too small for controller fallback')
        atomic_json(Path(self.state['turn']['dir']) / 'context-fallback.json', payload)
        self.event('context.fallback_summary', turn=self.state['turn']['number'],
                   generation=context['generation'], reason=reason,
                   summary_sha256=hashlib.sha256(summary.encode()).hexdigest())
        return summary

    def monitor(self) -> str:
        cfg = self.config
        turn = self.state['turn']
        while True:
            started_path = Path(turn['dir']) / 'worker-start.json'
            if not turn['runtime_synced'] and started_path.exists():
                started = read_json(started_path)
                self.state['budget'].update(active_started_at=started['at'], active_monotonic=started['monotonic'])
                turn.update(runtime_synced=True, deadline_monotonic=started['monotonic']+turn['execution_seconds'])
            self.ingest_output()
            now = time.monotonic()
            reason = stop_reason(self.run_dir) or ('operator_stop' if self.signalled else None)
            view = budget_view(self.state)
            if reason is None:
                if view['hard_reached']:
                    reason = 'hard_limit'
                else:
                    reason = self.pending_reason

            # Drain a worker that has already closed its stdout before applying
            # the context guard. This preserves a completed turn whose final
            # usage report arrived just before the process exited.
            if reason is None and self.process.poll() is not None:
                while self.offset < (Path(turn['dir']) / 'stdout.log').stat().st_size:
                    self.ingest_output()
                return self.finish_turn(None)

            ctx = self.state['context']
            if ctx['state'] == 'COMPACTION_REQUIRED':
                request_needed = not (Path(turn['dir']) / 'context-request.json').exists()
                context_reason = context_guard_deadline(ctx,
                    summary_seconds=cfg['context']['summary_seconds'],
                    grace_seconds=cfg['turn']['grace_seconds'],
                    summary_present=self.summary is not None, now_monotonic=now)
                # Commit deadlines before asking the worker to exit; a crash
                # between the two writes cannot grant another grace period.
                self.save()
                if request_needed:
                    atomic_json(Path(turn['dir']) / 'context-request.json', {'generation': ctx['generation'],
                        'action': 'summarize_and_exit', 'remaining_tokens': max(0, ctx['max_tokens']-ctx['used_tokens']),
                        'summary_deadline_at': ctx['summary_deadline_at']})
                if reason is None:
                    reason = context_reason
            if reason == 'context_window' and not self.context_guard_triggered:
                self.context_guard_triggered = True
                ctx['guard_triggered_at'] = utc_now()
                self.event('context.guard_triggered', turn=turn['number'], generation=ctx['generation'],
                           used_tokens=ctx['used_tokens'], max_tokens=ctx['max_tokens'],
                           summary_present=self.summary is not None,
                           completion_present=self.result.get('credit') is True)
            waiting_gpu = turn.get('status') == 'WAITING_GPU'
            turn['deadline_monotonic'] = (self.state['budget']['active_monotonic']
                                          + turn['execution_seconds'] + gpu_wait_seconds(turn, now))
            if reason is None and not waiting_gpu and now >= turn['deadline_monotonic']:
                reason = turn['deadline_reason']
            if reason is None and not waiting_gpu and cfg['heartbeat']['required'] and now-self.state['heartbeat']['last_monotonic'] >= cfg['heartbeat']['stale_after_seconds']:
                reason = 'heartbeat_stale'
            if reason is None and self.with_guard and self.guard.poll() is not None:
                reason = 'guard_lost'
            if reason is None and now-self.last_disk_check >= 1:
                self.last_disk_check = now
                if cfg['storage']['data_mount'] is not None:
                    storage = check_storage(cfg['storage']['data_mount'], self.run_dir, Path(cfg['root']))
                    if storage != self.state['storage']:
                        reason = 'storage_changed'
                size = sum(p.stat().st_size for p in self.run_dir.rglob('*') if p.is_file() and not p.is_symlink())
                if size > cfg['output']['max_run_bytes'] or shutil.disk_usage(self.run_dir).free < cfg['output']['min_free_bytes']:
                    reason = 'storage_limit'
            self.save()
            if reason:
                # Worker handles TERM and writes its own precise runtime result.
                signal_identity(self.process.pid, turn['pid_start_ticks'], signal.SIGTERM)
                try:
                    self.process.wait(timeout=cfg['turn']['grace_seconds']+cfg['cleanup']['timeout_seconds']+2.5)
                except subprocess.TimeoutExpired:
                    terminate_scope(turn['token'], 0)
                    self.process.wait(timeout=2)
                self.ingest_output()
                return self.finish_turn(reason)
            time.sleep(min(cfg['heartbeat']['interval_seconds'], max(.01, turn['deadline_monotonic']-now)))

    def finish_turn(self, reason: str | None) -> str:
        turn = self.state['turn']
        worker = read_json(Path(turn['dir']) / 'worker-exit.json', {})
        code = worker.get('returncode', self.process.poll())
        retry_state = self.state.setdefault('retry', {'anchor_turn': None, 'used': 0})
        view = budget_view(self.state)
        target_reached = view['target_reached']
        if self.config['budget']['mode'] == 'active' and self.config['budget']['credit_policy'] in ('successful_turn', 'reported'):
            target_reached = self.state['budget']['active_seconds'] >= self.state['budget']['window_seconds']
        facts = dict(
            context_required=self.config['context']['required'], context_reported=self.context_reported,
            summary_present=bool(self.summary), result=self.result,
            credit_policy=self.config['budget']['credit_policy'], retryable_reasons=RETRYABLE_TURN_REASONS,
            stop_requested=stop_reason(self.run_dir) is not None or self.signalled,
            hard_reached=view['hard_reached'], target_reached=target_reached,
            context_state=self.state['context']['state'], deadline_reason=turn['deadline_reason'],
        )
        observed_reason = reason or self.pending_reason
        if reason == 'context_window' and self.pending_reason is not None:
            observed_reason = self.pending_reason
        if observed_reason is None and code != 0:
            observed_reason = gpu_infrastructure_reason(turn)
        protocol = compute_turn_outcome(observed_reason, worker, code, **facts)
        if protocol.context_completion_recovered:
            self.event('context.completion_recovered', turn=turn['number'], generation=turn['generation'],
                       used_tokens=self.state['context']['used_tokens'])
        process_cleanup_ok = terminate_scope(turn['token'], 0)
        # Automatic compaction needs the same task resources in its next generation.
        retain_for_compaction = (
            self.config['context']['auto_compact']
            and self.state['context']['state'] == 'COMPACTION_REQUIRED'
            and protocol.reason in CONTEXT_BOUNDARY_REASONS
            and not budget_view(self.state)['hard_reached']
            and stop_reason(self.run_dir) is None
            and not self.signalled
        )
        task_cleanup_ok = cleanup_task(Path(turn['dir']), retry_pending=protocol.retry_pending or retain_for_compaction)
        cleanup_ok = process_cleanup_ok and task_cleanup_ok
        partial_error = None
        if not protocol.completed and self.config['budget']['allow_partial_credit'] and cleanup_ok:
            report_path = self.partial_credit.get('credit_evidence') or str(Path(turn['dir']) / 'partial-credit.json')
            if self.partial_credit or Path(report_path).is_file():
                try:
                    self.partial_credit = partial_report(report_path, run_id=self.state['run_id'], turn=turn['number'],
                        lower=epoch(turn['started_at']), upper=epoch(worker.get('at')) or time.time(),
                        maximum=worker.get('runtime_seconds', 0),
                        allowed=self.config['storage']['data_mount'] or self.config['root'],
                        expected_sha256=self.partial_credit.get('credit_evidence_sha256'),
                        expected_seconds=self.partial_credit.get('credited_seconds'))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    partial_error = str(exc)
                    self.partial_credit = {}
                    self.event('agent.partial_credit_rejected', error=partial_error)
        duration = worker.get('runtime_seconds', 0)
        observed_end = min(time.monotonic(), self.state['budget']['active_monotonic'] + duration)
        close_gpu_wait(turn, observed_end)
        observed = max(0, observed_end - self.state['budget']['active_monotonic']
                       - gpu_wait_seconds(turn, observed_end))
        final = compute_turn_outcome(observed_reason, worker, code, **facts,
            cleanup_ok=cleanup_ok, allow_partial_credit=self.config['budget']['allow_partial_credit'],
            partial_report=self.partial_credit, observed_seconds=observed)
        if final.credit_rejected:
            report = self.partial_credit if self.partial_credit else self.result
            self.event('agent.credit_rejected', reported_seconds=report.get('credited_seconds'), observed_seconds=observed)
        elapsed = finish_active_interval(self.state, credit=final.credit, duration=duration,
                                         credited_duration=final.reported_seconds)
        # Recompute after closing the interval.  The pre-close view can still
        # be one tick short while the final worker interval supplies the
        # remaining confirmed credit.
        post_close_view = budget_view(self.state)
        if observed_reason == 'operator_stop':
            self.state['target_reached_at_stop'] = bool(post_close_view['target_reached'])
            self.event('target.observed_at_stop', turn=turn['number'],
                       active_seconds=post_close_view['active_seconds'],
                       window_seconds=post_close_view['window_seconds'],
                       target_reached=post_close_view['target_reached'],
                       hard_reached=post_close_view['hard_reached'])
        reason = final.reason
        turn.update(status='COMPLETED' if final.completed else (
            'FAILED' if reason == 'deterministic_evidence_failure' else 'STOPPED'), ended_at=utc_now(), returncode=code,
                    reason=reason, elapsed_seconds=elapsed, credited=final.credit,
                    credited_seconds=(final.reported_seconds if final.reported_seconds is not None else elapsed) if final.credit else 0,
                    target_reached=post_close_view['target_reached'], result=self.result)
        if reason in INFRASTRUCTURE_REASONS:
            turn['failure_class'] = 'infrastructure'
        if final.partial_credit:
            turn['partial_credit'] = self.partial_credit
        if partial_error:
            turn['partial_credit_error'] = partial_error
        atomic_json(Path(turn['dir']) / 'exit.json', turn)
        self.save()
        self.event('turn.finished', **turn)
        self.process.wait(timeout=3)
        self.retry_pending = final.retry_pending
        if final.completed:
            self.state['retry'] = {'anchor_turn': None, 'used': 0}
        elif final.retry_pending:
            anchor = retry_state.get('anchor_turn') or turn['number']
            used = retry_state.get('used', 0) + 1
            self.state['retry'] = {'anchor_turn': anchor, 'used': used}
            self.event('turn.retry_scheduled', failed_turn=turn['number'], retry_of=anchor,
                       retry_index=used, reason=reason, next_turn=turn['number'] + 1)
        else:
            self.state['retry'] = {'anchor_turn': None, 'used': 0}
        self.save()
        return reason

    def wait_retry_backoff(self) -> bool:
        deadline = time.monotonic() + self.config['policy']['retry_backoff_seconds']
        while time.monotonic() < deadline:
            reason = stop_reason(self.run_dir) or ('operator_stop' if self.signalled else None)
            if reason == 'hard_limit' or budget_view(self.state)['hard_reached']:
                self.state.update(status='EXPIRED', stop_reason='hard_limit')
                return False
            if reason:
                self.state.update(status='STOPPED', stop_reason=reason, resume_required=True)
                return False
            # Backoff is controller activity, but grants no worker heartbeat or credit.
            self.save()
            time.sleep(min(.2, self.config['heartbeat']['interval_seconds'],
                           max(.01, deadline - time.monotonic())))
        return True

    def loop(self) -> None:
        while True:
            view = budget_view(self.state)
            reason = stop_reason(self.run_dir) or ('operator_stop' if self.signalled else None)
            if reason:
                if (reason == 'operator_stop' and view['target_reached']
                        and not view['hard_reached']):
                    # The stop raced with the final credited interval.  Keep
                    # the stop request in the event log, but let the verified
                    # target proceed through the normal completion gate.
                    self.finalize()
                    return
                self.state.update(status='STOPPED', stop_reason=reason, resume_required=True)
                return
            if self.state.get('completion', {}).get('phase') == 'FINALIZING':
                self.finalize()
                return
            if view['hard_reached']:
                self.state.update(status='EXPIRED', stop_reason='hard_limit')
                return
            if view['target_reached']:
                self.finalize()
                return
            self.pending_reason, self.summary, self.result, self.context_reported = None, None, {}, False
            self.retry_pending = False
            self.offset, self.fragment, self.dropping_line = 0, b'', False
            self.start_turn()
            reason = self.monitor()
            if reason == 'hard_limit' or budget_view(self.state)['hard_reached']:
                self.state.update(status='EXPIRED', stop_reason='hard_limit')
                return
            if reason == 'operator_stop':
                view = budget_view(self.state)
                if view['target_reached'] and not view['hard_reached']:
                    self.finalize()
                    return
                self.state.update(status='STOPPED', stop_reason=reason, resume_required=True)
                return
            if reason == 'target_reached':
                if budget_view(self.state)['target_reached']:
                    self.finalize()
                    return
                continue
            if (reason in CONTEXT_BOUNDARY_REASONS
                    and self.state['context']['state'] == 'COMPACTION_REQUIRED'):
                self.state.update(status='WAITING_COMPACTION', stop_reason='context_window')
                self.save()
                if self.config['context']['auto_compact']:
                    summary = self.summary or self.fallback_compaction_summary(reason)
                    self.summary = summary
                    compact_context(self.run_dir, summary, expected_generation=self.state['context']['generation'],
                                    controller_owned=True)
                    self.state = load_state(self.run_dir)
                    self.state['status'] = 'RUNNING'
                    self.save()
                    continue
                return
            if reason != 'turn_completed':
                if self.retry_pending:
                    self.state.update(status='RUNNING', stop_reason=None, resume_required=False)
                    self.save()
                    if self.wait_retry_backoff():
                        continue
                    return
                self.state.update(status='PAUSED' if reason in INFRASTRUCTURE_REASONS else 'FAILED',
                                  stop_reason=reason, resume_required=True)
                return

    def finalize(self) -> None:
        self.event('run.finalizing', original_deadline=self.state['budget']['hard_deadline_at'])
        previous = None
        last_pulse = time.monotonic()
        def pulse() -> None:
            nonlocal last_pulse
            now = time.monotonic()
            if now - last_pulse >= min(1, self.config['heartbeat']['interval_seconds']):
                self.save()
                last_pulse = now
        while True:
            reason = stop_reason(self.run_dir) or ('operator_stop' if self.signalled else None)
            view = budget_view(self.state)
            if reason and reason != 'hard_limit' and not view['hard_reached'] and not view['target_reached']:
                status = 'EVALUATION_PENDING' if self.config['score_expectation'] == 'required' else 'STOPPED'
                self.state.update(status=status, stop_reason=reason)
                self.state['completion'].update(phase=status, decided_at=utc_now())
                atomic_json(self.run_dir / 'completion.decision.json', {
                    'status': status, 'run_id': self.state['run_id'], 'at': utc_now(),
                    'original_deadline': self.state['budget']['hard_deadline_at'], 'reason': reason})
                self.save()
                return
            with evidence_heartbeat(pulse):
                status = completion_step(self.run_dir, self.state, self.config, live=True)
            current = (status, digest(self.state['completion'].get('jobs', [])))
            if current != previous:
                self.event('run.completion_progress', status=status, jobs=self.state['completion'].get('jobs'),
                           issues=self.state['completion'].get('issues'))
                previous = current
            if self.state['status'] != 'FINALIZING':
                return
            self.save()
            time.sleep(min(self.config['heartbeat']['interval_seconds'],
                           max(.01, budget_view(self.state)['remaining_seconds'])))

    def run(self) -> int:
        with file_lock(self.run_dir / '.controller.lock', blocking=False):
            self.state = load_state(self.run_dir)
            self.check_startable()
            old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
            def stopped(_sig: int, _frame: FrameType | None) -> None:
                self.signalled = True
            for sig in old_handlers:
                signal.signal(sig, stopped)
            first_start = self.state['budget']['started_at'] is None
            begin_run(self.state)
            if first_start:
                contract = contract_for_run(self.config, self.state['run_id'], self.state['budget']['hard_deadline_at'])
                atomic_json(self.run_dir / 'completion.contract.json', contract)
                self.state['completion']['contract_hash'] = digest(contract)
            self.state.update(controller_pid=os.getpid(), controller_start_ticks=process_start_ticks(os.getpid()),
                              process_boot_id=boot_id())
            self.save()
            (self.run_dir / 'exit.json').unlink(missing_ok=True)
            launch = {'request_id': self.request_id, 'attempt': self.state['attempt'], 'pid': os.getpid(),
                      'pid_start_ticks': self.state['controller_start_ticks'], 'at': utc_now(), 'status': 'ACCEPTED'}
            atomic_json(self.run_dir / 'attempts' / f"{self.state['attempt']:06d}" / 'launch.json', launch)
            atomic_json(self.run_dir / 'launch.json', launch)
            try:
                self.start_guard()
                self.event('run.started', **launch)
                self.loop()
            except BaseException as exc:
                self.state.update(status='FAILED', stop_reason='controller_error', error=str(exc), resume_required=True)
                (self.run_dir / 'attempts' / f"{self.state['attempt']:06d}" / 'exception.log').write_text(traceback.format_exc())
            finally:
                try:
                    if self.process is not None:
                        if not terminate_scope(self.state['turn']['token'], self.config['turn']['grace_seconds']):
                            self.state.update(status='FAILED', stop_reason='cleanup_incomplete')
                        self.process.wait(timeout=3)
                        if not cleanup_task(Path(self.state['turn']['dir'])):
                            self.state.update(status='FAILED', stop_reason='cleanup_incomplete')
                    if self.state['budget'].get('active_started_at'):
                        finish_active_interval(self.state, credit=False)
                        self.state['turn'].update(status='FAILED', reason='controller_error', ended_at=utc_now(), credited=False)
                        atomic_json(Path(self.state['turn']['dir']) / 'exit.json', self.state['turn'])
                    if self.guard is not None:
                        self.guard.terminate()
                        self.guard.wait(timeout=3)
                    self.state.update(controller_pid=None, controller_start_ticks=None,
                                      guard_pid=None, guard_start_ticks=None)
                    code = persist_exit(self.run_dir, self.state)
                    self.save()
                    self.event('run.exited', status=self.state['status'], exit_code=code)
                finally:
                    for sig, handler in old_handlers.items():
                        signal.signal(sig, handler)
            return code


def start_background(run_dir: Path, resume: bool, with_guard: bool) -> dict[str, Any]:
    request = uuid.uuid4().hex
    with (run_dir / 'controller.log').open('a') as log:
        args = [sys.executable, '-B', str(HERE / 'controller.py'), '--state-dir', str(run_dir.parents[1]),
                'run', '--run-id', run_dir.name, '--request-id', request]
        if resume:
            args.append('--resume')
        if not with_guard:
            args.append('--no-guard')
        # A detached child has no Popen object to be destructed while still
        # running; the accepted launch contract is the readiness handshake.
        child_pid = os.fork()
        if child_pid == 0:
            try:
                os.setsid()
                with open(os.devnull, 'rb') as inp:
                    os.dup2(inp.fileno(), 0)
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
                os.execv(sys.executable, args)
            except BaseException:
                os._exit(127)
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        launch = read_json(run_dir / 'launch.json', {})
        if launch.get('request_id') == request:
            return {**launch, 'run_id': run_dir.name, 'log': str(run_dir / 'controller.log')}
        exited, status = os.waitpid(child_pid, os.WNOHANG)
        if exited:
            raise ControllerError(f'controller did not accept launch (exit {os.waitstatus_to_exitcode(status)}); inspect controller.log')
        time.sleep(.03)
    raise ControllerError('launch acknowledgement timed out; status is UNKNOWN, inspect before retrying')


def operator_stop(run_dir: Path, reason: str, *, signal_controller: bool = True) -> dict[str, Any]:
    state = load_state(run_dir)
    if not reason.strip():
        raise ControllerError('stop reason must be non-empty')
    atomic_json(run_dir / STOP_FILE, {'at': utc_now(), 'reason': reason, 'code': 'operator_stop'})
    append_event(run_dir, 'stop.requested', reason=reason)
    if signal_controller:
        signal_identity(state.get('controller_pid'), state.get('controller_start_ticks'), signal.SIGTERM,
                        state.get('process_boot_id'))
    if not pid_matches(state.get('controller_pid'), state.get('controller_start_ticks'), state.get('process_boot_id')):
        with file_lock(run_dir / '.controller.lock', blocking=False):
            if state.get('controller_pid'):
                recover_locked(run_dir)
            state = load_state(run_dir)
            if state['status'] not in COMPLETION_TERMINAL_STATES:
                state.update(status='STOPPED', stop_reason='operator_stop', resume_required=True)
                state['target_reached_at_stop'] = bool(budget_view(state)['target_reached'])
                save_state(run_dir, state)
                persist_exit(run_dir, state)
    return safe_summary(load_state(run_dir))


def guard_loop(run_dir: Path, attempt: int) -> int:
    config = task_config(run_dir)
    poll = min(.5, config['heartbeat']['interval_seconds'])
    while True:
        state = load_state(run_dir)
        if state['attempt'] != attempt or not state.get('controller_pid'):
            return 0
        pid, ticks, boot = state['controller_pid'], state['controller_start_ticks'], state['process_boot_id']
        now = time.monotonic()
        reason = stop_reason(run_dir)
        if not pid_matches(pid, ticks, boot):
            reason = 'controller_lost'
        elif now-state['controller_heartbeat_monotonic'] >= config['heartbeat']['controller_stale_seconds']:
            reason = 'controller_stale'
        elif budget_view(state)['hard_reached']:
            reason = 'hard_limit'
        elif state['turn'].get('status') in ('RUNNING', 'WAITING_GPU'):
            waiting_gpu = state['turn']['status'] == 'WAITING_GPU'
            if not waiting_gpu and now >= state['turn']['deadline_monotonic'] + poll*2:
                reason = state['turn']['deadline_reason']
            elif not waiting_gpu and config['heartbeat']['required'] and now-state['heartbeat']['last_monotonic'] >= config['heartbeat']['stale_after_seconds']+poll*2:
                reason = 'heartbeat_stale'
        if reason:
            atomic_json(run_dir / STOP_FILE, {'at': utc_now(), 'reason': reason, 'code': reason, 'source': 'guard'})
            append_event(run_dir, 'guard.stop', reason=reason, attempt=attempt)
            signal_identity(pid, ticks, signal.SIGTERM, boot)
            deadline = time.monotonic()+config['turn']['grace_seconds']+1
            while pid_matches(pid, ticks, boot) and time.monotonic() < deadline:
                time.sleep(.05)
            signal_identity(pid, ticks, signal.SIGKILL, boot)
            # Re-read only this attempt; a later controller cannot be stopped by an old guard.
            latest = load_state(run_dir)
            if latest['attempt'] != attempt:
                return 0
            token = latest['turn'].get('token')
            if token:
                terminate_scope(token, 0)
            try:
                with file_lock(run_dir / '.controller.lock', blocking=False):
                    if load_state(run_dir)['attempt'] == attempt:
                        recover_locked(run_dir, reason=reason)
            except BlockingIOError:
                pass
            return 0
        time.sleep(poll)


def doctor_run(run_dir: Path) -> dict[str, Any]:
    state = load_state(run_dir)
    errors, warnings = [], []
    try:
        config = task_config(run_dir)
        storage = check_storage(config['storage']['data_mount'], run_dir, Path(config['root']))
        if storage != state['storage']:
            errors.append('data device changed since init')
        from bundle import source_hashes
        current_sources = source_hashes()
        for name, expected in state['source_sha256'].items():
            if current_sources.get(name) != expected:
                errors.append(f'source changed: {name}')
        if config['docker_network']['enabled']:
            bridge_preflight(config['docker_network'])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        errors.append(str(exc))
    if state.get('controller_pid') and not pid_matches(state['controller_pid'], state['controller_start_ticks'], state['process_boot_id']):
        errors.append('controller lost; recover before resuming')
    if state['context']['state'] == 'COMPACTION_REQUIRED':
        warnings.append('context compact requires summary + current generation')
    if (run_dir / STOP_FILE).exists():
        warnings.append('STOP marker exists; explicit --resume required')
    if state['context'].get('last_snapshot'):
        try:
            snapshot_path = (run_dir / state['context']['last_snapshot']).resolve()
            if not snapshot_path.is_relative_to(run_dir / 'context'):
                raise ControllerError('snapshot path escaped context directory')
            snapshot = read_json(snapshot_path)
            if snapshot['generation'] != state['context']['generation'] or hashlib.sha256(snapshot['summary'].encode()).hexdigest() != snapshot['summary_sha256']:
                errors.append('context snapshot failed generation/hash verification')
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f'context snapshot unreadable: {exc}')
    with (run_dir / 'events.jsonl').open('rb') as stream:
        for number, line in enumerate(stream, 1):
            try:
                json.loads(line)
            except ValueError:
                errors.append(f'events.jsonl malformed line {number}')
    turn_dir = Path(state['turn']['dir']) if state['turn'].get('dir') else None
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'status': safe_summary(state),
            'stderr_tail': tail(turn_dir / 'stderr.log', 8192) if turn_dir else '',
            'next_action': 'context compact' if state['context']['state'] == 'COMPACTION_REQUIRED' else 'inspect exit.json and turn logs'}


def tail(path: Path, size: int) -> str:
    if not path.is_file():
        return ''
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size-size))
        return stream.read(size).decode('utf-8', errors='replace')


def watch_run(run_dir: Path, interval: float, maximum: float | None, as_json: bool) -> int:
    started = time.monotonic()
    while True:
        value = safe_summary(load_state(run_dir))
        print(_json_dump(value) if as_json else format_status(value), flush=True)
        if not value['controller_alive'] or maximum is not None and time.monotonic()-started >= maximum:
            return 0
        time.sleep(interval)


def format_status(value: dict[str, Any]) -> str:
    b, c = value['budget']['view'], value['context']['view']
    return (f"{value['run_id']} {value['observed_status']} turn={value['turn']['number']} "
        f"active={b['active_seconds']:.1f}s remaining={b['remaining_seconds']:.1f}s "
        f"context={c['used_tokens']}/{c['max_tokens']} generation={c['generation']} reason={value['stop_reason']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir')
    parser.add_argument('--remote', type=Path, help='SSH connection config; supervisor remains on the execution host')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('deploy', help='install a new immutable controller release using --remote')
    network = sub.add_parser('docker-network', help='inspect bridge and model HTTPS forwarding; optionally repair scoped missing rules')
    network.add_argument('--config', type=Path, required=True)
    network.add_argument('--repair', action='store_true')
    network.add_argument('--restore-default-forwarding', action='store_true',
                         help='explicitly restore docker0 egress for default-bridge builds; Agent namespace isolation still applies')
    init = sub.add_parser('init'); init.add_argument('--config', type=Path, required=True); init.add_argument('--run-id', required=True)
    for name in ('start', 'run'):
        cmd = sub.add_parser(name); cmd.add_argument('--run-id', required=True)
        cmd.add_argument('--resume', action='store_true'); cmd.add_argument('--no-guard', action='store_true')
        if name == 'start':
            cmd.add_argument('--background', action='store_true')
        else:
            cmd.add_argument('--request-id', help=argparse.SUPPRESS)
    for name in ('status', 'doctor', 'recover'):
        sub.add_parser(name).add_argument('--run-id', required=True)
    cmd = sub.add_parser('rebase'); cmd.add_argument('--run-id', required=True); cmd.add_argument('--reason', required=True)
    cmd = sub.add_parser('watch'); cmd.add_argument('--run-id', required=True)
    cmd.add_argument('--interval', type=float, default=5); cmd.add_argument('--seconds', type=float); cmd.add_argument('--json', action='store_true')
    cmd = sub.add_parser('stop'); cmd.add_argument('--run-id', required=True); cmd.add_argument('--reason', required=True)
    cmd.add_argument('--no-signal', action='store_true')
    cmd = sub.add_parser('amend'); cmd.add_argument('--run-id', required=True)
    cmd.add_argument('--config', type=Path, required=True); cmd.add_argument('--credit-file', type=Path)
    cmd.add_argument('--expected-config-sha256', required=True); cmd.add_argument('--expected-turn', type=int, required=True)
    cmd.add_argument('--reason', required=True)
    cmd.add_argument('--extend-expired-budget', action='store_true',
                     help='explicitly authorize a later deadline for a fully stopped run (before or after expiry); preserves target and credit')
    cmd.add_argument('--storage-migration-source', type=Path,
                     help='explicitly migrate a fully stopped run, verifying its preserved source on the old data device')
    cmd = sub.add_parser('logs'); cmd.add_argument('--run-id', required=True)
    cmd.add_argument('--stream', choices=('stdout', 'stderr', 'controller', 'guard', 'events'), default='stderr')
    cmd.add_argument('--bytes', type=int, default=16384)
    cmd = sub.add_parser('guard'); cmd.add_argument('--run-id', required=True); cmd.add_argument('--attempt', type=int, required=True)
    ctx = sub.add_parser('context').add_subparsers(dest='context_action', required=True)
    for name in ('report', 'compact', 'reopen'):
        cmd = ctx.add_parser(name); cmd.add_argument('--run-id', required=True)
        cmd.add_argument('--generation', type=int, required=True); cmd.add_argument('--conversation-id')
        if name == 'report':
            cmd.add_argument('--used-tokens', type=int, required=True)
            cmd.add_argument('--input-tokens', type=int); cmd.add_argument('--output-tokens', type=int)
        else:
            cmd.add_argument('--summary-file', type=Path, required=True)
            if name == 'compact': cmd.add_argument('--force', action='store_true')
            else: cmd.add_argument('--reason', default='operator_reopen')
    return parser


def dispatch(args: argparse.Namespace, *, remote_payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, int]:
    if args.action == 'deploy':
        raise ControllerError('deploy requires --remote')
    if args.action == 'docker-network':
        config = remote_payload['network_config'] if remote_payload else read_json(args.config)
        return bridge_preflight(config, repair=args.repair,
                                restore_default_forwarding=args.restore_default_forwarding), 0
    state_dir = Path(args.state_dir or DEFAULT_STATE_DIR).expanduser().resolve()
    run_dir = state_path(state_dir, args.run_id)
    if args.action == 'init':
        return init_run(args.config, state_dir, args.run_id, config=remote_payload.get('config') if remote_payload else None), 0
    if args.action in ('start', 'run'):
        if args.action == 'start' and args.background:
            return start_background(run_dir, args.resume, not args.no_guard), 0
        return None, LongRunController(run_dir, resume=args.resume, with_guard=not args.no_guard,
                                      request_id=getattr(args, 'request_id', None)).run()
    if args.action == 'status': return safe_summary(load_state(run_dir)), 0
    if args.action == 'doctor':
        result = doctor_run(run_dir); return result, 0 if result['ok'] else 1
    if args.action == 'stop': return operator_stop(run_dir, args.reason, signal_controller=not args.no_signal), 0
    if args.action == 'recover': return recover_run(run_dir), 0
    if args.action == 'rebase': return rebase_boot_run(run_dir, reason=args.reason), 0
    if args.action == 'amend':
        config = remote_payload['config'] if remote_payload else load_config(args.config)
        credit = remote_payload.get('credit') if remote_payload else read_json(args.credit_file) if args.credit_file else None
        return amend_run(run_dir, config, expected_sha256=args.expected_config_sha256,
                         expected_turn=args.expected_turn, reason=args.reason, credit=credit,
                         extend_expired_budget=args.extend_expired_budget,
                         storage_migration_source=getattr(args, 'storage_migration_source', None)), 0
    if args.action == 'guard': return None, guard_loop(run_dir, args.attempt)
    if args.action == 'watch':
        if not .05 <= args.interval <= 3600 or args.seconds is not None and not 0 < args.seconds <= 43200:
            raise ControllerError('invalid watch interval/seconds')
        return None, watch_run(run_dir, args.interval, args.seconds, args.json)
    if args.action == 'logs':
        if not 1 <= args.bytes <= 1048576: raise ControllerError('logs --bytes must be 1..1048576')
        state = load_state(run_dir)
        if args.stream in ('stdout', 'stderr'):
            if not state['turn'].get('dir'): return {'text': ''}, 0
            path = Path(state['turn']['dir']) / (args.stream+'.log')
        else:
            path = run_dir / ('events.jsonl' if args.stream == 'events' else args.stream+'.log')
        return {'path': str(path), 'text': tail(path, args.bytes)}, 0
    if args.action == 'context':
        if args.context_action == 'report':
            return record_context_usage(run_dir, args.used_tokens, generation=args.generation,
                input_tokens=args.input_tokens, output_tokens=args.output_tokens, conversation_id=args.conversation_id), 0
        if remote_payload is None and args.summary_file.stat().st_size > task_config(run_dir)['context']['max_summary_bytes']:
            raise ControllerError('summary file exceeds max_summary_bytes')
        summary = remote_payload['summary'] if remote_payload is not None else args.summary_file.read_text(encoding='utf-8')
        if args.context_action == 'compact':
            return compact_context(run_dir, summary, expected_generation=args.generation,
                                    conversation_id=args.conversation_id, force=args.force), 0
        return reopen_context(run_dir, summary=summary, expected_generation=args.generation,
                              conversation_id=args.conversation_id, reason=args.reason), 0
    raise ControllerError('unknown command')


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.remote:
            from remote import remote_dispatch
            return remote_dispatch(args)
        result, code = dispatch(args)
        if result is not None:
            print(_json_dump(result))
        return code
    except (ControllerError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(_json_dump({'ok': False, 'error': str(exc), 'action': args.action}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
