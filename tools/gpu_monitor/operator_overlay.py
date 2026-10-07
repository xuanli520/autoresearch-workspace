"""Transient operator-only reading of official completion receipts.

This file is also streamed into the existing probe process. It never writes
evidence, logs a score, or runs a completion producer.
"""
import datetime
import importlib
import json
import math
from pathlib import Path
import sys


def completion_bootstrap(workspace):
    """Load the canonical validator in memory, including on an uninstalled SSH host."""
    workspace = Path(workspace)
    sources = [('processes', workspace / 'tools/process_control/processes.py'),
               ('gpu_wait', workspace / 'tools/research_handoff/core/gpu_wait.py'),
               ('longrun', workspace / 'tools/research_handoff/core/longrun.py'),
               ('completion', workspace / 'tools/research_handoff/core/completion.py')]
    payload = [(name, path.read_text()) for name, path in sources]
    return ("\nimport types, sys\n"
            "_monitor_package = types.ModuleType('_monitor_completion')\n"
            "_monitor_package.__path__ = []\n"
            "sys.modules['_monitor_completion'] = _monitor_package\n"
            "for _monitor_name, _monitor_source in " + repr(payload) + ":\n"
            "    _monitor_module = types.ModuleType('_monitor_completion.' + _monitor_name)\n"
            "    _monitor_module.__package__ = '_monitor_completion'\n"
            "    sys.modules[_monitor_module.__name__] = _monitor_module\n"
            "    exec(compile(_monitor_source, '<trusted-monitor-validator>', 'exec'), _monitor_module.__dict__)\n")


def collect_operator_overlay(tasks):
    overlay = {}
    for task in tasks:
        settings = task.get('operator_overlay', {})
        records = settings.get('records', [])
        if not records:
            overlay[task['id']] = {'state': 'UNAVAILABLE'}
            continue
        values = []
        comparability = set()
        try:
            completion = sys.modules.get('_monitor_completion.completion') or importlib.import_module('tools.research_handoff.core.completion')
            for record in records:
                root = Path(task['root']).resolve(strict=True)
                contract_path = root / record['contract_path']
                if (not contract_path.resolve().is_relative_to(root) or
                        contract_path.is_symlink()):
                    continue
                contract = json.loads(contract_path.read_bytes())
                controller = task.get('controller', {})
                if (contract.get('run_id') != controller.get('run_id') or
                        contract.get('task_id') != settings.get('task_id') or
                        contract.get('stage') not in ('formal', 'final') or
                        contract.get('score_expectation') != 'required'):
                    continue
                receipt_path = completion.within(contract['completion']['evidence_root'],
                                                 contract['completion']['receipt'])
                receipt = json.loads(receipt_path.read_bytes())
                verdict = completion.validate_receipt(contract, receipt, live=False)
                if verdict['ok']:
                    value = receipt.get('scientific_score')
                    if type(value) in (int, float) and math.isfinite(value):
                        when = datetime.datetime.fromisoformat(receipt['generated_at'].replace('Z', '+00:00'))
                        values.append((when.timestamp(), value, contract['direction']))
                        comparability.add((contract['metric'], contract['protocol_hash'],
                                           contract['completion']['data_hash'], contract['completion']['evaluator_hash']))
            if not values or len({item[2] for item in values}) != 1 or len(comparability) != 1:
                overlay[task['id']] = {'state': 'UNAVAILABLE'}
                continue
            values.sort()
            direction = values[-1][2]
            numbers = [item[1] for item in values]
            latest = numbers[-1]
            change = latest - numbers[-2] if len(numbers) > 1 else 0
            improving = change < 0 if direction == 'min' else change > 0
            view = {'state': 'VERIFIED', 'best': min(numbers) if direction == 'min' else max(numbers),
                    'latest': latest, 'direction': direction,
                    'trend': '=' if change == 0 else '+' if improving else '-',
                    'unchanged': len(numbers) > 1 and change == 0}
            metadata_path = root / settings['metadata_path'] if settings.get('metadata_path') else None
            if metadata_path and metadata_path.resolve().is_relative_to(root) and not metadata_path.is_symlink():
                metadata_bytes = metadata_path.read_bytes()
                import hashlib
                if hashlib.sha256(metadata_bytes).hexdigest() == settings.get('metadata_sha256'):
                    anchors = json.loads(metadata_bytes).get('frozen_anchors', {})
                    if all(type(anchors.get(k)) in (int, float) and math.isfinite(anchors[k]) for k in ('B', 'R')):
                        view['B'], view['R'] = anchors['B'], anchors['R']
            overlay[task['id']] = view
        except Exception:
            # No exception text crosses this boundary: it may contain private paths.
            overlay[task['id']] = {'state': 'UNAVAILABLE'}
    return overlay
