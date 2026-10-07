"""Historical diagnostic contracts, without remote probes or training."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.now = 10000
        self.spec = {'id': 'demo', 'host': 'local', 'status': {'path': 'status.json'},
                     'controller': {'type': 'research_handoff', 'run_id': 'run-1'}}
        self.cfg = {'tasks': [self.spec]}
        self.task = {'id': 'demo', 'host': 'local', 'state': 'RUNNING', 'alerts': [],
                     'controller': deepcopy(self.spec['controller']), 'streams': [{'id': 'train'}]}
        self.document = {'controller': 'autoresearch-longrun', 'run_id': 'run-1', 'status': 'RUNNING',
                         'budget': {'mode': 'active', 'active_seconds': 500, 'window_seconds': 20000,
                                    'credit_policy': 'reported', 'hard_deadline_at': diagnostics.utc(50000),
                                    'boot_id': 'boot-1', 'active_monotonic': 200, 'active_started_at': diagnostics.utc(6400)},
                         'turn': {'number': 1, 'status': 'RUNNING', 'started_at': diagnostics.utc(6400)}}
        self.raw = {'files': {}, 'processes': []}
        self.host = {'boot_id': 'boot-1', 'tasks': {'demo': self.raw}}

    def sample(self, events=(), previous=None, formal=None, now=None):
        self.raw['files']['status.json'] = {'text': json.dumps(self.document)}
        self.raw['files']['events.jsonl'] = {'text': ''.join(json.dumps(row) + '\n' for row in events)}
        data = {'tasks': [deepcopy(self.task)]}
        return diagnostics.augment(data, self.cfg, previous, self.now if now is None else now,
                                   {'local': self.host}, formal)

    def event(self, name, at=None, **kw):
        return {'event': name, 'at': diagnostics.utc(self.now - 100 if at is None else at), **kw}

    def alert(self, data, rule):
        return next((row for row in data['alerts'] if row['rule'] == rule), None)

    def test_reported_live_pending_credited_are_distinct(self):
        data = self.sample()
        timing = data['tasks'][0]['timing']
        self.assertEqual(timing['live_elapsed_seconds'], 3600)
        self.assertEqual(timing['pending_seconds'], 3600)
        self.assertEqual(timing['credited_effective_seconds'], 500)
        self.assertEqual(timing['effective_remaining_seconds'], 19500)

    def test_waiting_gpu_preserves_live_excludes_pending(self):
        self.document['turn'].update(status='WAITING_GPU', gpu_wait={
            'excluded_seconds': 600, 'started_monotonic': 800})
        data = self.sample()
        timing = data['tasks'][0]['timing']
        self.assertEqual(timing['live_elapsed_seconds'], 3600)
        self.assertEqual(timing['excluded_gpu_wait_seconds'], 3600)
        self.assertEqual(timing['pending_seconds'], 0)
        self.assertEqual(timing['credited_effective_seconds'], 500)
        self.assertEqual(timing['calculation_state'], 'waiting_gpu')

    def test_pending_does_not_grow_during_gpu_wait(self):
        self.document['turn'].update(status='WAITING_GPU', gpu_wait={
            'excluded_seconds': 0, 'started_monotonic': 3200})
        old = self.sample()
        new = self.sample(previous=old, now=10600)
        self.assertEqual(old['tasks'][0]['timing']['pending_seconds'], 3000)
        self.assertEqual(new['tasks'][0]['timing']['pending_seconds'], 3000)

    def test_clock_future_and_boot_mismatch_are_unknown(self):
        self.document['turn']['started_at'] = diagnostics.utc(10001)
        data = self.sample()
        self.assertIsNone(data['tasks'][0]['timing']['live_elapsed_seconds'])
        self.assertIn('turn_clock_invalid', data['tasks'][0]['timing']['data_gap'])
        self.document['turn']['started_at'] = diagnostics.utc(6400)
        self.host['boot_id'] = 'boot-2'
        self.assertIn('host_boot_identity_changed', self.sample()['tasks'][0]['timing']['data_gap'])

    def test_identity_mismatch_never_uses_registered_document_credit(self):
        self.document['run_id'] = 'other-run'
        self.task['effective_seconds'] = 100000
        timing = self.sample()['tasks'][0]['timing']
        self.assertIsNone(timing['credited_effective_seconds'])
        self.assertEqual(timing['calculation_state'], 'unknown')

    def test_reported_pending_grace_and_unreachable_differ(self):
        self.document['budget'].update(window_seconds=2000, hard_deadline_at=diagnostics.utc(11000))
        first = self.sample()
        self.assertIsNone(self.alert(first, 'EFFECTIVE_TARGET_UNREACHABLE'))
        self.assertIsNone(self.alert(first, 'EFFECTIVE_PENDING_SETTLEMENT'))
        second = self.sample(previous=first, now=10301)
        self.assertEqual(self.alert(second, 'EFFECTIVE_PENDING_SETTLEMENT')['severity'], 'warning')
        self.document['budget']['window_seconds'] = 10000
        self.assertEqual(self.alert(self.sample(), 'EFFECTIVE_TARGET_UNREACHABLE')['severity'], 'critical')

    def test_unknown_credit_requires_grace(self):
        self.document['budget']['active_seconds'] = None
        first = self.sample()
        self.assertIsNone(self.alert(first, 'EFFECTIVE_CREDIT_UNKNOWN'))
        second = self.sample(previous=first, now=10301)
        self.assertEqual(self.alert(second, 'EFFECTIVE_CREDIT_UNKNOWN')['severity'], 'warning')

    def test_retries_replay_deduplicate_and_restart_preserves_count(self):
        events = [self.event('turn.retry_scheduled', at=9000 + turn, failed_turn=turn,
                             reason='agent_exit_nonzero') for turn in range(1, 10)]
        first = self.sample(events)
        alert = self.alert(first, 'AGENT_RETRY_STORM')
        self.assertEqual(alert['observed_count'], 9)
        self.assertEqual(alert['severity'], 'critical')
        restarted = json.loads(json.dumps(first))
        second = self.sample(events, restarted)
        self.assertEqual(self.alert(second, 'AGENT_RETRY_STORM')['observed_count'], 9)
        self.assertEqual(self.alert(second, 'AGENT_RETRY_STORM')['observation_count'], 1)

    def test_infrastructure_events_do_not_consume_agent_retries(self):
        events = [self.event('turn.retry_scheduled', at=9000 + i, failed_turn=i, reason=reason)
                  for i, reason in enumerate(('gpu_expired', 'gpu_infeasible', 'gpu_unknown', 'queue_timeout') * 10)]
        first = self.sample(events)
        self.assertEqual(first['diagnostics']['tasks']['demo']['retry_consecutive'], 0)
        self.assertIsNone(self.alert(first, 'AGENT_RETRY_STORM'))
        self.task['active_job'] = {'state': 'INFEASIBLE', 'requested_memory_mib': 32000,
                                   'capacity_memory_mib': 24000}
        self.assertEqual(self.alert(self.sample(events), 'RESOURCE_INFEASIBLE')['severity'], 'critical')

    def test_recovery_reopening_and_endpoint_gap(self):
        events = [self.event('turn.retry_scheduled', at=9000 + i, failed_turn=i,
                             reason='heartbeat_stale') for i in range(3)]
        first = self.sample(events)
        self.host['error'] = 'do not publish secret path'
        self.task['state'] = 'UNREACHABLE'
        gap = self.sample(previous=first)
        alert = self.alert(gap, 'AGENT_RETRY_STORM')
        self.assertEqual(alert['state'], 'open')
        self.assertIn('endpoint_unreachable', alert['data_gap'])
        self.host.pop('error')
        self.task['state'] = 'RUNNING'
        recovery = self.sample(events + [self.event('turn.finished', at=9500, turn=3, returncode=0)], gap)
        self.assertEqual(self.alert(recovery, 'AGENT_RETRY_STORM')['state'], 'resolved')
        new = [self.event('turn.retry_scheduled', at=9700 + i, failed_turn=4 + i,
                          reason='heartbeat_stale') for i in range(3)]
        reopened = self.sample(new, recovery)
        alert = self.alert(reopened, 'AGENT_RETRY_STORM')
        self.assertEqual(alert['state'], 'open')
        self.assertEqual(alert['reopen_count'], 1)
        self.assertEqual(alert['first_seen'], self.alert(first, 'AGENT_RETRY_STORM')['first_seen'])

    def test_invalid_alternation_window_detects_pattern(self):
        events = []
        for i in range(8):
            events.append(self.event('agent.event', at=9000 + i * 2, turn=i, payload={'autoresearch': 'heartbeat'}))
            events.append(self.event('turn.retry_scheduled', at=9001 + i * 2, failed_turn=i, reason='invalid_agent_event'))
        alert = self.alert(self.sample(events), 'INVALID_AGENT_EVENT_PATTERN')
        self.assertEqual(alert['severity'], 'critical')
        self.assertEqual(alert['evidence']['alternations'], 15)

    def test_summary_warning_needs_formal_fact_for_critical(self):
        events = [self.event('context.compacted', at=9000 + i, generation=i,
                             summary_sha256='a' * 64) for i in range(22)]
        data = self.sample(events)
        self.assertEqual(self.alert(data, 'SUMMARY_STAGNANT')['severity'], 'warning')
        data = self.sample(events, data, formal={'demo': True})
        self.assertEqual(self.alert(data, 'SUMMARY_STAGNANT')['severity'], 'critical')
        self.assertEqual(self.alert(data, 'SUMMARY_STAGNANT')['observed_count'], 22)

    def test_summary_generation_and_event_dedup(self):
        events = [self.event('context.compacted', at=9000 + i, generation=1, summary_sha256='a' * 64)
                  for i in range(22)]
        data = self.sample(events)
        self.assertEqual(data['diagnostics']['tasks']['demo']['summary_unchanged_generations'], 1)

    def test_queue_expiry_counts_distinct_attempts(self):
        events = [self.event('turn.gpu_state', at=9000 + i, request_id='req-1', state='EXPIRED') for i in range(10)]
        first = self.sample(events)
        self.assertIsNone(self.alert(first, 'REPEATED_QUEUE_EXPIRY'))
        events.append(self.event('turn.gpu_state', at=9500, request_id='req-2', state='EXPIRED'))
        alert = self.alert(self.sample(events, first), 'REPEATED_QUEUE_EXPIRY')
        self.assertEqual(alert['observed_count'], 2)

    def test_memory_oom_and_limit_without_oom(self):
        self.raw['processes'] = [{'pid': 123, 'cgroup_memory': [{'path': '/private/cgroup',
                                  'current_bytes': 1024, 'max_bytes': 1024, 'events': {'oom_kill': 1}}]}]
        alert = self.alert(self.sample(), 'CONTAINER_MEMORY_LIMIT')
        self.assertEqual(alert['severity'], 'critical')
        self.assertNotIn('/private/cgroup', json.dumps(alert))
        self.raw['processes'][0]['cgroup_memory'][0]['events'] = {'max': 1}
        self.assertEqual(self.alert(self.sample(), 'CONTAINER_MEMORY_LIMIT')['severity'], 'warning')

    def test_external_queue_rules_never_include_identity(self):
        self.task['active_job'] = {'state': 'QUEUED', 'wait_seconds': 1900,
                                   'queue': {'external_blocked': True, 'blocking_reason': 'external_occupancy'}}
        self.task['external_queue_summary'] = {'count': 2, 'memory_mib': 24000, 'cu': 80,
                                               'blocked_by_our_reservation': True,
                                               'estimated_wait_seconds': 3700,
                                               'owner': 'private-owner', 'job_id': 'private-job'}
        data = self.sample()
        self.assertIsNotNone(self.alert(data, 'EXTERNAL_OCCUPANCY_BLOCKING'))
        self.assertIsNotNone(self.alert(data, 'OWN_RESERVATION_BLOCKING_EXTERNAL'))
        serialized = json.dumps(data['alerts'])
        self.assertNotIn('private-owner', serialized)
        self.assertNotIn('private-job', serialized)

    def test_timeline_has_48_rolling_buckets_and_gap_reason(self):
        data = self.sample()
        timeline = data['tasks'][0]['timeline_12h']
        self.assertEqual(len(timeline), 48)
        self.assertEqual(diagnostics.epoch(timeline[-1]['end_at']), self.now)
        self.assertEqual(diagnostics.epoch(timeline[0]['start_at']), self.now - 43200)
        self.assertTrue(all(diagnostics.epoch(row['end_at']) - diagnostics.epoch(row['start_at']) == 900 for row in timeline))
        self.assertEqual(timeline[-1]['category'], 'pending')
        self.assertEqual(timeline[0]['data_gap'], ['no_registered_history'])
        self.assertEqual(data['tasks'][0]['streams'][0]['timeline_12h'], timeline)

    def test_sensitive_event_payloads_are_never_stored(self):
        events = [self.event('agent.event', payload={'autoresearch': 'turn.complete',
                                                   'scientific_score': 987654321, 'B': 123456789,
                                                   'R': 876543219, 'password': 'private-password',
                                                   'evidence': '/private/verifier/reference.py'})]
        data = self.sample(events)
        serialized = json.dumps(data)
        for forbidden in ('987654321', '123456789', '876543219', 'private-password', 'reference.py', 'scientific_score'):
            self.assertNotIn(forbidden, serialized)

    def test_thresholds_are_configurable(self):
        self.cfg['diagnostics'] = {'thresholds': {'retry_warning': 1, 'retry_critical': 2}}
        events = [self.event('turn.retry_scheduled', failed_turn=1, reason='agent_exit_nonzero')]
        self.assertEqual(self.alert(self.sample(events), 'AGENT_RETRY_STORM')['severity'], 'warning')

    def test_real_handoff_rejected_events_and_progress_state(self):
        events = []
        for i in range(5):
            events.append(self.event('agent.event', at=9000 + i * 2, payload={'autoresearch': 'heartbeat'}))
            events.append(self.event('agent.event_rejected', at=9001 + i * 2, error='private invalid event details'))
        self.document['progress'] = {'consecutive_same_context_summary': 22,
                                     'last_context_summary_sha256': 'a' * 64,
                                     'last_context_summary_generation': 22}
        data = self.sample(events)
        self.assertIsNotNone(self.alert(data, 'INVALID_AGENT_EVENT_PATTERN'))
        self.assertEqual(self.alert(data, 'SUMMARY_STAGNANT')['observed_count'], 22)
        self.assertNotIn('private invalid event details', json.dumps(data))

    def test_native_probe_memory_fields_are_deduplicated_by_cgroup(self):
        memory = {'path': '/private/container/cgroup', 'current': 512, 'max': 1024,
                  'events': {'oom': 1, 'oom_kill': 1}}
        self.raw['processes'] = [{'pid': 1, 'memory': memory}, {'pid': 2, 'memory': memory}]
        alert = self.alert(self.sample(), 'CONTAINER_MEMORY_LIMIT')
        self.assertEqual(alert['observed_count'], 2)

    def test_null_active_job_is_supported(self):
        self.task['active_job'] = None
        self.document['budget']['window_seconds'] = 100000
        self.assertIsNotNone(self.alert(self.sample(), 'EFFECTIVE_TARGET_UNREACHABLE'))

    def test_event_intervals_show_waiting_across_multiple_buckets(self):
        events = [self.event('turn.started', at=6400, turn=1),
                  self.event('turn.gpu_state', at=7000, turn=1, waiting=True),
                  self.event('turn.gpu_state', at=9100, turn=1, waiting=False)]
        timeline = self.sample(events)['tasks'][0]['timeline_12h']
        self.assertTrue(timeline[-2]['waiting'])
        self.assertTrue(timeline[-3]['waiting'])
        self.assertEqual(timeline[-1]['category'], 'pending')

    def test_older_event_does_not_reset_current_retry_history(self):
        events = [self.event('turn.retry_scheduled', at=9900 + i, failed_turn=i,
                             reason='heartbeat_stale') for i in range(3)]
        first = self.sample(events)
        second = self.sample([self.event('turn.finished', at=9000, returncode=0)], first)
        self.assertEqual(second['diagnostics']['tasks']['demo']['retry_consecutive'], 3)
        self.assertIn('out_of_order_events_ignored', self.alert(second, 'AGENT_RETRY_STORM')['data_gap'])

    def test_invalid_threshold_is_rejected(self):
        for threshold in ({'retry_warning': -1}, {'invalid_critical_rate': 2},
                          {'summary_warning_generations': 30}, {'private_config': 1}):
            self.cfg['diagnostics'] = {'thresholds': threshold}
            with self.assertRaises(ValueError):
                self.sample()

    def test_scheduler_replayed_attempts_drive_expiry_and_infeasible(self):
        self.raw['scheduler_view'] = {'attempts': [
            {'request_id': 'req-1', 'state': 'EXPIRED', 'finished_at': 9000},
            {'request_id': 'req-2', 'state': 'EXPIRED', 'finished_at': 9200},
            {'request_id': 'req-3', 'state': 'INFEASIBLE', 'last_updated_at': 9400,
             'resources': {'memory_mib': 99999, 'compute_units': 100}}]}
        data = self.sample()
        self.assertEqual(self.alert(data, 'REPEATED_QUEUE_EXPIRY')['observed_count'], 2)
        self.assertEqual(self.alert(data, 'RESOURCE_INFEASIBLE')['evidence']['memory_mib'], 99999)
        replay = self.sample(previous=data)
        self.assertEqual(self.alert(replay, 'REPEATED_QUEUE_EXPIRY')['observed_count'], 2)

    def test_a_single_expired_attempt_gets_infrastructure_alert(self):
        self.raw['scheduler_view'] = {'attempts': [
            {'request_id': 'req-1', 'state': 'EXPIRED', 'finished_at': 9000}]}
        data = self.sample()
        self.assertIsNotNone(self.alert(data, 'GPU_QUEUE_EXPIRED'))
        self.assertIsNone(self.alert(data, 'AGENT_RETRY_STORM'))

    def test_rebinding_registered_run_invalidates_previous_counts(self):
        events = [self.event('turn.retry_scheduled', at=9000 + i, failed_turn=i,
                             reason='agent_exit_nonzero') for i in range(5)]
        old = self.sample(events)
        self.spec['controller']['run_id'] = 'run-2'
        self.task['controller']['run_id'] = 'run-2'
        self.document['run_id'] = 'run-2'
        current = self.sample(previous=old)
        self.assertEqual(current['tasks'][0]['retry_count'], 0)
        self.assertIsNone(self.alert(current, 'AGENT_RETRY_STORM'))

    def test_duplicate_failure_turns_are_counted_once(self):
        events = [self.event('turn.retry_scheduled', at=9000 + i, failed_turn=1,
                             reason='agent_exit_nonzero') for i in range(5)]
        data = self.sample(events)
        self.assertEqual(data['tasks'][0]['retry_count'], 1)

    def test_untrusted_event_types_do_not_crash_history(self):
        events = [self.event('context.compacted', at=9000, generation=1, summary_sha256='a' * 64),
                  self.event('context.compacted', at=9001, generation={'bad': True},
                             summary_sha256='a' * 64, state={'bad': True}, turn=[])]
        self.document['heartbeat'] = None
        data = self.sample(events)
        self.assertEqual(data['tasks'][0]['summary_stagnant_generations'], 1)

    def test_nonobject_diagnostic_config_is_rejected(self):
        for value in (None, [], 'bad'):
            self.cfg['diagnostics'] = value
            with self.assertRaises(ValueError):
                self.sample()


if __name__ == '__main__':
    unittest.main()
