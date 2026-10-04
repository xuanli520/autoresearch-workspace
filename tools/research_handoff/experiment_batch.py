"""Shared trusted batch close-out; batch_summary.json is descriptive evidence only.

Task adapters supply all seed files and separate-process reload evidence. This
module never runs a candidate, launches an Agent, or submits a GPU job.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

try:
    from .core.completion import (issue_receipt, validate_contract, write_score_result)
    from .core.longrun import atomic_json, read_json
except ImportError:
    from core.completion import issue_receipt, validate_contract, write_score_result
    from core.longrun import atomic_json, read_json


def complete_batch(contract, seed_results, *, harbor=None):
    validate_contract(contract)
    if contract['score_expectation'] != 'required':
        raise ValueError('unscored diagnostics close through the controller, not the scoring adapter')
    if not seed_results:
        raise ValueError('a scored batch cannot complete without seed results')
    scientific_score = math.fsum(row['score'] for row in seed_results) / len(seed_results)
    write_score_result(contract, scientific_score, seed_results, harbor=harbor)
    # When called inside the scoring job this is normally EVALUATION_PENDING.
    # The controller later queries that same job and issues the terminal receipt.
    return issue_receipt(contract, live=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--seed-results', type=Path, required=True)
    args = parser.parse_args(argv)
    contract = read_json(args.contract)
    rows = read_json(args.seed_results)
    receipt = complete_batch(contract, rows)
    print(receipt['status'])
    # Pending is a successful scoring child; only the controller can finish the run.
    return 0 if receipt['status'] in ('COMPLETED', 'EVALUATION_PENDING') else 4


if __name__ == '__main__':
    raise SystemExit(main())
