#!/usr/bin/env python3
"""Read-only scientific completion audit. Never repairs, submits or rewrites evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
try:
    from .core.completion import (COMPLETION_FAILURES, CONTRACT_FIELDS, contract_for_run, digest,
        inspect_evaluation, report, validate_contract, validate_receipt, within)
    from .core.longrun import read_json
except ImportError:
    from core.completion import (COMPLETION_FAILURES, CONTRACT_FIELDS, contract_for_run, digest,
        inspect_evaluation, report, validate_contract, validate_receipt, within)
    from core.longrun import read_json


def issue(code, message):
    return {'code': code, 'message': message}


def null_score_locations(state, summary):
    locations = []
    for label, value in (('state', state), ('batch_summary', summary)):
        if value.get('scientific_score', 'absent') is None:
            locations.append(label)
        completion = value.get('completion', {})
        if isinstance(completion, dict) and completion.get('scientific_score', 'absent') is None:
            locations.append(label + '.completion')
    return locations


def audit_run(run_dir):
    run_dir = Path(run_dir).resolve()
    issues, jobs, state, config = [], [], {}, {}
    try:
        state = read_json(run_dir / 'state.json', {})
        if not state:
            state = read_json(run_dir / 'batch_status.json', {}) or read_json(run_dir / 'run_summary.json', {})
        summary = read_json(run_dir / 'batch_summary.json', {})
        if not isinstance(state, dict) or not isinstance(summary, dict):
            raise ValueError('run state and summary must be objects')
        config_file = run_dir / state.get('paths', {}).get('config', 'config.json')
        if not config_file.resolve().is_relative_to(run_dir):
            raise ValueError('config path escaped run directory')
        config = read_json(config_file, {}) or read_json(run_dir / 'batch_config.json', {})
        if not isinstance(config, dict):
            raise ValueError('run configuration must be an object')
        null_scores = null_score_locations(state, summary)
        contract_file = run_dir / 'completion.contract.json'
        if contract_file.is_file():
            contract = read_json(contract_file)
            validate_contract(contract)
            expected = state.get('completion', {}).get('contract_hash')
            if expected is not None and expected != digest(contract):
                issues.append(issue('CONTRACT_HASH_MISMATCH', 'frozen completion contract changed'))
            if config.get('root') and state.get('run_id'):
                rebuilt = contract_for_run(config, state['run_id'], state.get('budget', {}).get('hard_deadline_at'))
                if rebuilt != contract:
                    issues.append(issue('CONTRACT_CONFIG_MISMATCH', 'completion contract differs from the frozen run configuration'))
            if state.get('config_sha256'):
                from hashlib import sha256
                if sha256(config_file.read_bytes()).hexdigest() != state['config_sha256']:
                    issues.append(issue('CONFIG_HASH_MISMATCH', 'frozen configuration changed'))
        else:
            try:
                validate_contract(config)
                contract = contract_for_run(config, state.get('run_id', run_dir.name),
                                            state.get('budget', {}).get('hard_deadline_at'))
            except (ValueError, KeyError, TypeError):
                contract = None
                issues.append(issue('COMPLETION_CONTRACT_MISSING', 'historical run lacks an explicit completion contract; stage/score semantics cannot be inferred'))
        status = state.get('status', 'UNKNOWN')
        if contract is None:
            if status in ('COMPLETED', 'complete', 'completed'):
                issues.append(issue('COMPLETED_WITHOUT_RECEIPT', 'completion claim has no verifiable scientific completion receipt'))
            if null_scores:
                issues.append(issue('SCIENTIFIC_SCORE_NULL', 'scientific_score is null without an explicit not_expected contract'))
            if config.get('formal') or summary.get('formal'):
                issues.append(issue('FORMAL_FINAL_SCORE_UNVERIFIED', 'formal batch summary does not certify a complete final evaluation'))
            return {'run_dir': str(run_dir), 'run_id': state.get('run_id'), 'status': status,
                    'ok': False, 'issues': issues, 'jobs': []}
        scored = contract['score_expectation'] == 'required'
        if scored and null_scores:
            issues.append(issue('SCIENTIFIC_SCORE_NULL', 'stage requires a finite scientific score'))
        receipt_file = (within(contract['completion']['evidence_root'], contract['completion']['receipt']) if scored
                        else within(run_dir, 'completion.receipt.json'))
        if receipt_file.is_file():
            verdict = validate_receipt(contract, read_json(receipt_file))
            issues.extend(verdict['issues'])
            jobs = verdict['jobs']
            pinned = state.get('completion', {}).get('receipt_sha256')
            if pinned:
                from hashlib import sha256
                if sha256(receipt_file.read_bytes()).hexdigest() != pinned:
                    issues.append(issue('RECEIPT_HASH_MISMATCH', 'accepted receipt changed after completion'))
            if status in ('EXPIRED', *COMPLETION_FAILURES) and verdict['ok']:
                issues.append(issue('LATE_EVIDENCE_ONLY', 'terminal run remains unsuccessful even when a later receipt is present'))
        elif scored or status == 'COMPLETED':
            issues.append(issue('COMPLETION_RECEIPT_MISSING', 'required final completion receipt is missing'))
            if scored:
                observation = inspect_evaluation(contract)
                issues.extend(observation['issues'])
                jobs = observation['jobs']
        if status == 'COMPLETED' and issues:
            issues.append(issue('COMPLETED_WITHOUT_VALID_RECEIPT', 'COMPLETED has not passed the scientific completion gate'))
        if scored and status in ('EXPIRED', *COMPLETION_FAILURES):
            issues.append(issue('RUN_INCOMPLETE', 'formal/final scientific completion was not certified before the original deadline'))
        return {'run_dir': str(run_dir), 'run_id': contract['run_id'], 'stage': contract['stage'],
                'score_expectation': contract['score_expectation'], 'status': status,
                'ok': not issues, 'issues': issues, 'jobs': jobs}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {'run_dir': str(run_dir), 'run_id': state.get('run_id') if isinstance(state, dict) else None,
                'ok': False, 'issues': issues + [issue('AUDIT_INPUT_INVALID', 'run evidence or trust inputs are missing or malformed')], 'jobs': jobs}


def run_directories(root, recursive):
    root = Path(root).resolve(strict=True)
    if not recursive:
        return [root]
    found = set()
    # No symlink traversal, archive extraction or arbitrary code imports.
    import os
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in ('.git', 'node_modules', '__pycache__', 'venv', '.venv', 'site-packages')
                                and not (Path(folder) / d).is_symlink())
        if set(files) & {'state.json', 'completion.contract.json', 'batch_status.json', 'batch_summary.json', 'run_summary.json'}:
            found.add(Path(folder))
    return sorted(found)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    audit = sub.add_parser('audit', help='read-only audit of a run or an extracted delivery package')
    audit.add_argument('--run-dir', type=Path, required=True)
    audit.add_argument('--recursive', action='store_true', help='scan descendant run/batch directories')
    validate = sub.add_parser('validate', help='validate signed receipt against the original trusted contract')
    validate.add_argument('--contract', type=Path, required=True)
    validate.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            result = validate_receipt(read_json(args.contract), read_json(args.receipt))
        else:
            paths = run_directories(args.run_dir, args.recursive)
            runs = [audit_run(path) for path in paths]
            result = {'ok': bool(runs) and all(v['ok'] for v in runs), 'read_only': True,
                      'runs_checked': len(runs), 'runs': runs}
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0 if result['ok'] else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        print(json.dumps({'ok': False, 'read_only': True, 'issues': [issue('AUDIT_INPUT_INVALID', 'audit input is missing or malformed')]}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
