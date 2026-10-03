"""Validate trusted host evidence before accepting interrupted-turn credit."""
import hashlib
import json
import math
import os
from pathlib import Path


def checked_file(path, allowed, expected=None):
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


def validate_intervals(pairs, lower, upper, maximum):
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


def validate_evidence(evidence, allowed, *, required=True):
    if not isinstance(evidence, list) or required and not evidence:
        raise ValueError('credit requires hashed original evidence')
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('invalid credit evidence entry')
        checked_file(item['path'], allowed, item['sha256'])


def partial_report(path, *, run_id, turn, lower, upper, maximum, allowed,
                   expected_sha256=None, expected_seconds=None):
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
