"""Pure turn-close decision rules used by the research handoff controller.

Keeping the protocol decision separate from process cleanup makes the ordering
and the context-window race independently testable.  This module deliberately
has no filesystem, clock, or process dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping

try:
    from .gpu_wait import INFRASTRUCTURE_REASONS
except ImportError:
    from gpu_wait import INFRASTRUCTURE_REASONS


@dataclass(frozen=True)
class TurnOutcome:
    """Protocol outcome before cleanup and optional partial-credit validation."""

    reason: str
    completed: bool
    retry_pending: bool
    credit: bool
    context_completion_recovered: bool = False
    partial_credit: bool = False
    reported_seconds: float | None = None
    credit_rejected: bool = False


def compute_turn_outcome(
    reason: str | None,
    worker: Mapping[str, Any],
    returncode: int | None,
    *,
    context_required: bool,
    context_reported: bool,
    summary_present: bool,
    result: Mapping[str, Any],
    credit_policy: str,
    retryable_reasons: FrozenSet[str],
    stop_requested: bool,
    hard_reached: bool,
    target_reached: bool,
    context_state: str,
    deadline_reason: str = "turn_timeout",
    cleanup_ok: bool = True,
    allow_partial_credit: bool = False,
    partial_report: Mapping[str, Any] | None = None,
    observed_seconds: float | None = None,
) -> TurnOutcome:
    """Compute a deterministic turn result from observed protocol facts.

    ``finish_turn`` still performs cleanup and validates partial-credit evidence;
    this function only decides the protocol result that those operations consume.
    """
    recovered = (
        reason == "context_window"
        and worker.get("reason") in ("process_exit", "signal_stop")
        and returncode == 0
        and summary_present
        and context_reported
        and result.get("credit") is True
    )
    final_reason = "turn_completed" if recovered else reason
    if final_reason is None:
        if not worker:
            final_reason = "worker_exit_missing"
        elif worker.get("reason") in ("turn_timeout", "hard_limit"):
            final_reason = deadline_reason if worker.get("reason") == "turn_timeout" else "hard_limit"
        elif worker.get("reason") != "process_exit" or returncode != 0:
            final_reason = "agent_exit_nonzero"
        elif context_required and not context_reported:
            final_reason = "context_usage_missing"
        elif credit_policy in ("successful_turn", "reported") and result.get("credit") is not True:
            final_reason = "completion_missing"
        else:
            final_reason = "turn_completed"

    if not cleanup_ok:
        final_reason = "cleanup_incomplete"
    completed = final_reason == "turn_completed"
    retry_pending = (
        final_reason in retryable_reasons
        and final_reason not in INFRASTRUCTURE_REASONS
        and not stop_requested
        and not hard_reached
        and not target_reached
        and context_state != "COMPACTION_REQUIRED"
    )
    partial = (
        not completed and bool(partial_report) and allow_partial_credit and cleanup_ok
        and final_reason in retryable_reasons | {"operator_stop", "hard_limit", "context_window"}
    )
    credit = credit_policy == "running" or (completed and result.get("credit") is True) or partial
    if final_reason in {
        "controller_lost",
        "cleanup_incomplete",
        "invalid_agent_event",
        "context_usage_missing",
        "deterministic_evidence_failure",
    }:
        credit = False
    reported = None
    rejected = False
    if credit_policy == "reported":
        report = partial_report if partial else result
        reported = float(report.get("credited_seconds", 0)) if credit else 0.0
        if observed_seconds is not None and reported > observed_seconds:
            final_reason, credit, completed, retry_pending = "invalid_agent_event", False, False, False
            reported, partial, rejected = 0.0, False, True
    return TurnOutcome(final_reason, completed, retry_pending, credit, recovered, partial, reported, rejected)
