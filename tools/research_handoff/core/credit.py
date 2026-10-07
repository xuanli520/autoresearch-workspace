"""Validate trusted host evidence before accepting interrupted-turn credit."""
from __future__ import annotations
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

try:
    from .longrun import atomic_json, atomic_write
    from .research_time import merge, subtract, timestamp
except ImportError:
    from longrun import atomic_json, atomic_write
    from research_time import merge, subtract, timestamp


def checked_file(path: str | Path, allowed: str | Path, expected: str | None = None) -> tuple[Path, str]:
    path = Path(path)
    if path.is_symlink():
        raise ValueError('credit evidence must not be symlinked')
    path = path.resolve(strict=True)
    if not path.is_relative_to(Path(allowed).resolve()) or not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError('credit evidence is outside trusted host storage')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError('credit evidence hash mismatch')
    return path, digest


def validate_intervals(pairs: list[list[float]], lower: float, upper: float, maximum: float) -> float:
    if not isinstance(pairs, list):
        raise ValueError('credit intervals must be an array')
    ordered = []
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in pair)
                or not lower <= pair[0] < pair[1] <= upper):
            raise ValueError('credit interval falls outside recorded worker runtime')
        ordered.append(pair)
    ordered.sort()
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
        raise ValueError('credit intervals overlap')
    seconds = sum(b-a for a,b in ordered)
    if seconds > maximum + .001:
        raise ValueError('credit exceeds recorded worker runtime')
    return seconds


def validate_evidence(evidence: list[dict[str, Any]], allowed: str | Path, *, required: bool = True) -> None:
    if not isinstance(evidence, list) or required and not evidence:
        raise ValueError('credit requires hashed original evidence')
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('invalid credit evidence entry')
        checked_file(item['path'], allowed, item['sha256'])


def partial_report(path: str | Path, *, run_id: str, turn: int, lower: float, upper: float,
                   maximum: float, allowed: str | Path, expected_sha256: str | None = None,
                   expected_seconds: float | None = None) -> dict[str, Any]:
    path, digest = checked_file(path, allowed, expected_sha256)
    if path.stat().st_size > 4*1024*1024:
        raise ValueError('partial credit report exceeds 4 MiB')
    report = json.loads(path.read_text())
    if report.get('version') != 1 or report.get('run_id') != run_id or report.get('turn') != turn:
        raise ValueError('partial credit report identity mismatch')
    seconds = validate_intervals(report.get('intervals'), lower, upper, maximum)
    declared = report.get('credited_seconds')
    if type(declared) not in (int, float) or not math.isfinite(declared) or abs(declared-seconds) > .001:
        raise ValueError('partial credit seconds differ from audited intervals')
    if expected_seconds is not None and abs(expected_seconds-seconds) > .001:
        raise ValueError('partial credit event differs from trusted report')
    validate_evidence(report.get('evidence'), allowed, required=seconds > 0)
    return {'credited_seconds': seconds, 'credit_evidence': str(path),
            'credit_evidence_sha256': digest, 'intervals': report['intervals']}


def persist_report(turn_dir: str | Path, *, run_id: str, turn: int,
                   intervals: list[list[float]], sources: list[dict[str, Any]],
                   prior: list[list[float]], remaining: float,
                   metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist host snapshots before scientific close-out can fail.

    Only audited activity is passed here. Worker bounds, confirmed prior credit
    and the original target balance are applied independently of score selection.
    """
    turn_dir = Path(turn_dir)
    started = json.loads((turn_dir / 'worker-start.json').read_text())
    exited_path = turn_dir / 'worker-exit.json'
    exited = json.loads(exited_path.read_text()) if exited_path.exists() else None
    lower = timestamp(started['at'])
    upper = timestamp(exited['at']) if exited else time.time()
    runtime = exited['runtime_seconds'] if exited else max(0, time.monotonic() - started['monotonic'])
    wait_path = turn_dir / 'gpu-wait.json'
    queue_intervals = []
    if wait_path.exists():
        wait = json.loads(wait_path.read_text())
        waiting = max(0, time.monotonic() - wait['started_monotonic']) if wait.get('waiting') else 0
        runtime = max(0, runtime - wait.get('excluded_seconds', 0) - waiting)
        wait_intervals = list(wait.get('intervals', []))
        state_path = turn_dir.parent.parent / 'state.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state.get('turn', {}).get('number') == turn:
                wait_intervals += state['turn'].get('gpu_wait', {}).get('intervals', [])
        offset = lower - started['monotonic']
        queue_intervals = [[a + offset, b + offset] for a, b in wait_intervals]
        if wait.get('waiting'):
            queue_intervals.append([wait['started_monotonic'] + offset, upper])
    maximum = min(max(0, remaining), runtime)
    clipped = merge([[max(a, lower), min(b, upper)] for a, b in intervals if b > lower and a < upper])
    available = merge([piece for pair in clipped for piece in subtract(pair, prior + queue_intervals)])
    selected, left = [], maximum
    for a, b in available:
        width = min(b - a, left)
        if width > 0:
            end = min(b, a + width)
            # Epoch arithmetic can round a fractional budget down to zero.
            # Use the next real timestamp, still inside the audited interval.
            if end - a < width:
                end = min(b, math.nextafter(end, math.inf))
            selected.append([a, end])
            left -= end - a
    evidence, originals = [], []
    retained = turn_dir / 'research-evidence'
    retained.mkdir(mode=0o700, exist_ok=True)
    for item in sources:
        path = Path(item['path'])
        if path.is_symlink() or not path.is_file():
            raise ValueError('original credit evidence must be a regular file')
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if item.get('sha256') is not None and digest != item['sha256']:
            raise ValueError('original evidence changed before credit was persisted')
        snapshot = retained / (digest + path.suffix)
        if not snapshot.exists():
            atomic_write(snapshot, raw)
            snapshot.chmod(0o444)
        checked_file(snapshot, retained, digest)
        evidence.append({'path': str(snapshot), 'sha256': digest})
        originals.append({'path': str(path), 'sha256': digest, 'snapshot': str(snapshot)})
    seconds = validate_intervals(selected, lower, upper, maximum)
    if seconds > 0 and not evidence:
        raise ValueError('positive research credit requires original evidence')
    report = {**(metadata or {}), 'version': 1, 'run_id': run_id, 'turn': turn,
              'intervals': selected, 'credited_seconds': seconds, 'evidence': evidence,
              'original_sources': originals, 'queue_credit': 0,
              'queue_excluded_intervals': merge(queue_intervals), 'finished_at_epoch': upper}
    atomic_json(turn_dir / 'effective-time.json', report)
    atomic_json(turn_dir / 'partial-credit.json', report)
    return report
