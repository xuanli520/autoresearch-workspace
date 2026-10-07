"""Turn protocol and durable deadline regressions; no research/GPU jobs."""

from __future__ import annotations

import copy
import datetime
import unittest
from unittest.mock import patch

from tools.research_handoff.core import longrun
from tools.research_handoff.core.turn_outcome import compute_turn_outcome


def turn_outcome(**changes):
    facts = dict(
        reason=None, worker={"reason": "process_exit", "returncode": 0}, returncode=0,
        context_required=True, context_reported=True, summary_present=False,
        result={"credit": True}, credit_policy="successful_turn",
        retryable_reasons=frozenset({"agent_exit_nonzero", "context_usage_missing", "turn_timeout"}),
        stop_requested=False, hard_reached=False, target_reached=False, context_state="OPEN",
    )
    facts.update(changes)
    return compute_turn_outcome(**facts)


def budget_state():
    return {"budget": {
        "mode": "active", "credit_policy": "running", "window_seconds": 600,
        "hard_limit_seconds": 1200, "started_at": "1970-01-01T00:16:40+00:00",
        "hard_deadline_at": "1970-01-01T00:36:40+00:00", "started_monotonic": 50.0,
        "active_started_at": "1970-01-01T00:16:50+00:00", "active_monotonic": 60.0,
        "active_seconds": 7.0, "runtime_seconds": 7.0, "boot_id": "boot-one",
    }}


class TurnOutcomeTests(unittest.TestCase):
    def test_context_completion_race_accepts_final_signal_stop(self):
        outcome = turn_outcome(reason="context_window", worker={"reason": "signal_stop"}, summary_present=True)
        self.assertEqual(outcome.reason, "turn_completed")
        self.assertTrue(outcome.completed and outcome.credit and outcome.context_completion_recovered)

    def test_context_completion_race_requires_all_success_evidence(self):
        for change in ({"summary_present": False}, {"context_reported": False},
                       {"result": {"credit": False}}, {"returncode": 1}):
            with self.subTest(change=change):
                outcome = turn_outcome(**{**dict(reason="context_window", summary_present=True), **change})
                self.assertEqual(outcome.reason, "context_window")
                self.assertFalse(outcome.completed or outcome.credit)

    def test_timeout_uses_original_turn_deadline_reason(self):
        outcome = turn_outcome(worker={"reason": "turn_timeout"}, deadline_reason="target_reached")
        self.assertEqual(outcome.reason, "target_reached")
        self.assertFalse(outcome.retry_pending)

    def test_retry_is_suppressed_by_stops_deadlines_and_context(self):
        base = {"worker": {"reason": "process_exit"}, "returncode": 1}
        self.assertTrue(turn_outcome(**base).retry_pending)
        for change in ({"stop_requested": True}, {"hard_reached": True}, {"target_reached": True},
                       {"context_state": "COMPACTION_REQUIRED"}, {"cleanup_ok": False}):
            with self.subTest(change=change):
                self.assertFalse(turn_outcome(**base, **change).retry_pending)

    def test_cleanup_failure_vetoes_success_and_credit(self):
        outcome = turn_outcome(cleanup_ok=False, credit_policy="running")
        self.assertEqual(outcome.reason, "cleanup_incomplete")
        self.assertFalse(outcome.completed or outcome.credit)

    def test_partial_credit_keeps_failure_and_checks_runtime_bound(self):
        args = dict(reason="turn_timeout", credit_policy="reported", allow_partial_credit=True,
                    partial_report={"credited_seconds": 5.0}, observed_seconds=6.0)
        outcome = turn_outcome(**args)
        self.assertEqual(outcome.reason, "turn_timeout")
        self.assertFalse(outcome.completed)
        self.assertTrue(outcome.partial_credit and outcome.credit)
        self.assertEqual(outcome.reported_seconds, 5.0)
        args["observed_seconds"] = 4.0
        rejected = turn_outcome(**args)
        self.assertEqual(rejected.reason, "invalid_agent_event")
        self.assertTrue(rejected.credit_rejected)
        self.assertFalse(rejected.credit or rejected.retry_pending)

    def test_computation_does_not_mutate_input(self):
        worker, result = {"reason": "process_exit"}, {"credit": True}
        before = copy.deepcopy((worker, result))
        turn_outcome(worker=worker, result=result)
        self.assertEqual((worker, result), before)

    def test_completion_contract_failure_retains_only_audited_partial_credit(self):
        args = dict(reason='completion_contract_error', returncode=70,
                    credit_policy='reported', result={'credit': True, 'credited_seconds': 6.0},
                    allow_partial_credit=True, observed_seconds=6.0,
                    retryable_reasons=frozenset({'completion_contract_error'}))
        without_evidence = turn_outcome(**args)
        self.assertFalse(without_evidence.credit or without_evidence.retry_pending)
        audited = turn_outcome(**args, partial_report={'credited_seconds': 5.0})
        self.assertEqual(audited.reason, 'completion_contract_error')
        self.assertTrue(audited.partial_credit and audited.credit)
        self.assertFalse(audited.completed or audited.retry_pending)
        invalid = turn_outcome(**{**args, 'reason': 'deterministic_evidence_failure'},
                               partial_report={'credited_seconds': 5.0})
        self.assertFalse(invalid.credit or invalid.retry_pending)


class DurableDeadlineTests(unittest.TestCase):
    def test_summary_and_completion_grace_survive_serialization(self):
        context = {"state": "COMPACTION_REQUIRED", "used_tokens": 1200, "max_tokens": 1500}
        with patch.object(longrun, "boot_id", return_value="boot-one"):
            def check(state, now, summary=False):
                return longrun.context_guard_deadline(state, summary_seconds=60, grace_seconds=1,
                    summary_present=summary, now_monotonic=now, now_epoch=1000 + now)
            self.assertIsNone(check(context, 10))
            self.assertEqual(context["summary_deadline_monotonic"], 70)
            restored = copy.deepcopy(context)
            self.assertIsNone(check(restored, 20, True))
            self.assertEqual(restored["summary_deadline_monotonic"], 70)
            self.assertEqual(restored["completion_grace_deadline_monotonic"], 21)
            restored = copy.deepcopy(restored)
            self.assertEqual(check(restored, 22, True), "context_window")
            self.assertEqual(restored["completion_grace_deadline_monotonic"], 21)

    def test_boot_change_never_rearms_deadline(self):
        context = {"state": "COMPACTION_REQUIRED", "used_tokens": 1200, "max_tokens": 1500,
                   "guard_boot_id": "boot-one", "summary_deadline_monotonic": 70}
        with patch.object(longrun, "boot_id", return_value="boot-two"):
            self.assertEqual(longrun.context_guard_deadline(context, summary_seconds=60, grace_seconds=1,
                summary_present=False, now_monotonic=10, now_epoch=1000), "context_window")
        self.assertEqual(context["summary_deadline_monotonic"], 70)

    def test_absolute_context_deadline_also_bounds_wait(self):
        context = {"state": "COMPACTION_REQUIRED", "used_tokens": 1200, "max_tokens": 1500}
        with patch.object(longrun, "boot_id", return_value="boot-one"):
            longrun.context_guard_deadline(context, summary_seconds=60, grace_seconds=1,
                summary_present=False, now_monotonic=10, now_epoch=1000)
            self.assertEqual(longrun.context_guard_deadline(context, summary_seconds=60, grace_seconds=1,
                summary_present=False, now_monotonic=20, now_epoch=1061), "context_window")

    def test_boot_mismatch_expires_without_cross_boot_credit(self):
        state = budget_state()
        with patch.object(longrun, "boot_id", return_value="boot-two"):
            view = longrun.budget_view(state, now_epoch=1100)
            self.assertTrue(view["hard_reached"])
            self.assertEqual(view["clock_issue"], "host_rebooted")
            self.assertEqual(longrun.active_elapsed(state, 1100), 7.0)
            self.assertEqual(longrun.finish_active_interval(state, duration=90), 0.0)
        self.assertEqual(state["budget"]["active_seconds"], 7.0)

    def test_wall_clock_rollback_keeps_conservative_age_and_expires(self):
        state = budget_state()
        state["budget"].update(last_observed_epoch=1110, wall_age_seconds=110)
        with patch.object(longrun, "boot_id", return_value="boot-one"), \
             patch.object(longrun.time, "time", return_value=1050), \
             patch.object(longrun.time, "monotonic", return_value=100):
            view = longrun.budget_view(state)
        self.assertTrue(view["hard_reached"])
        self.assertEqual(view["wall_age_seconds"], 110)
        self.assertEqual(view["clock_issue"], "wall_clock_rollback")

    def test_timezone_offsets_are_preserved_and_naive_time_rejected(self):
        value = "2026-10-07T00:00:00+08:00"
        expected = datetime.datetime(2026, 10, 6, 16, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(longrun.epoch(value), expected)
        with self.assertRaises(longrun.ControllerError):
            longrun.epoch("2026-10-07T00:00:00")


if __name__ == "__main__":
    unittest.main()
