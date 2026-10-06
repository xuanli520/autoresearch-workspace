"""Scheduler waiting stays within one round and never earns research credit."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from tools.research_handoff.core import longrun
from tools.research_handoff.core.gpu_wait import (close_gpu_wait, gpu_infrastructure_reason, gpu_wait_seconds,
                                                observe_gpu_state)
from tools.research_handoff.core.turn_outcome import compute_turn_outcome


class GpuWaitTests(unittest.TestCase):
    def test_wait_restores_same_request_and_preserves_concurrent_wait(self):
        turn = {'number': 2, 'status': 'RUNNING'}
        observe_gpu_state(turn, {'request_id': 'original', 'state': 'QUEUED', 'sequence': 1}, 10)
        turn = copy.deepcopy(turn)
        observe_gpu_state(turn, {'request_id': 'other', 'state': 'QUEUED'}, 12)
        observe_gpu_state(turn, {'request_id': 'original', 'state': 'RUNNING', 'sequence': 2}, 15)
        self.assertEqual(turn['status'], 'WAITING_GPU')
        self.assertEqual(gpu_wait_seconds(turn, 18), 8)
        observe_gpu_state(turn, {'request_id': 'other', 'state': 'INFEASIBLE'}, 20)
        self.assertEqual(turn['status'], 'RUNNING')
        self.assertEqual(turn['number'], 2)
        self.assertEqual(turn['gpu_wait']['excluded_seconds'], 10)
        self.assertFalse(observe_gpu_state(turn,
            {'request_id': 'original', 'state': 'QUEUED', 'sequence': 1}, 21))
        self.assertEqual(turn['status'], 'RUNNING')

    def test_unknown_only_pauses_while_reconciliation_is_active(self):
        turn = {'status': 'RUNNING'}
        observe_gpu_state(turn, {'request_id': 'r', 'state': 'UNKNOWN', 'reconciling': True}, 10)
        self.assertEqual(turn['status'], 'WAITING_GPU')
        observe_gpu_state(turn, {'request_id': 'r', 'state': 'UNKNOWN', 'reconciling': False}, 20)
        self.assertEqual(turn['status'], 'RUNNING')
        self.assertEqual(gpu_infrastructure_reason(turn), 'gpu_unknown')

    def test_wait_is_excluded_from_credit_but_preserved_in_worker_runtime(self):
        state = {'turn': {}, 'budget': {
            'mode': 'active', 'credit_policy': 'running', 'window_seconds': 60,
            'hard_limit_seconds': 120, 'started_at': '1970-01-01T00:16:40+00:00',
            'hard_deadline_at': '1970-01-01T00:18:40+00:00', 'started_monotonic': 0,
            'active_started_at': '1970-01-01T00:16:40+00:00', 'active_monotonic': 0,
            'active_seconds': 0, 'runtime_seconds': 0, 'boot_id': 'boot-one',
        }}
        observe_gpu_state(state['turn'], {'request_id': 'r', 'state': 'QUEUED'}, 5)
        with patch.object(longrun, 'boot_id', return_value='boot-one'), \
             patch.object(longrun.time, 'time', return_value=1020), \
             patch.object(longrun.time, 'monotonic', return_value=20):
            self.assertEqual(longrun.budget_view(state)['active_seconds'], 5)
            self.assertEqual(longrun.finish_active_interval(state, duration=20), 5)
        self.assertEqual(state['budget']['active_seconds'], 5)
        self.assertEqual(state['budget']['runtime_seconds'], 20)

    def test_gpu_infrastructure_exit_never_uses_agent_retry_policy(self):
        for reason in ('gpu_infeasible', 'gpu_expired', 'gpu_unknown', 'gpu_session_changed'):
            with self.subTest(reason=reason):
                result = compute_turn_outcome(reason, {'reason': 'process_exit'}, 1,
                    context_required=True, context_reported=True, summary_present=False,
                    result={}, credit_policy='successful_turn', retryable_reasons=frozenset({reason}),
                    stop_requested=False, hard_reached=False, target_reached=False, context_state='OPEN')
                self.assertFalse(result.retry_pending or result.completed or result.credit)

    def test_shutdown_wait_does_not_erase_work_done_before_queue(self):
        state = {'turn': {}, 'budget': {'active_started_at': '1970-01-01T00:00:00+00:00',
            'active_monotonic': 0, 'active_seconds': 0, 'runtime_seconds': 0,
            'boot_id': 'boot-one'}}
        observe_gpu_state(state['turn'], {'request_id': 'r', 'state': 'QUEUED'}, 5)
        with patch.object(longrun, 'boot_id', return_value='boot-one'), \
             patch.object(longrun.time, 'monotonic', return_value=25):
            self.assertEqual(longrun.finish_active_interval(state, duration=20), 5)
        self.assertEqual(state['budget']['runtime_seconds'], 20)

    def test_terminal_wait_interval_is_closed_at_worker_exit(self):
        turn = {}
        observe_gpu_state(turn, {'request_id': 'r', 'state': 'QUEUED'}, 5)
        close_gpu_wait(turn, 20)
        self.assertEqual(turn['gpu_wait']['intervals'], [[5, 20]])
        self.assertIsNone(turn['gpu_wait']['started_monotonic'])
        self.assertEqual(gpu_wait_seconds(turn, 30), 15)


if __name__ == '__main__':
    unittest.main()
