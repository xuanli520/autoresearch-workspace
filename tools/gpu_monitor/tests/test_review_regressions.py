"""Regression checks for lifecycle authority, bounded probes and read-only stop guidance."""
import copy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.task = {'id': 'task', 'host': 'local', 'root': '/example', 'uses_gpu': False,
                     'status': {'path': 'status.json', 'key': 'state'},
                     'exit': {'path': 'exit.json', 'key': 'exit_code'}}
        self.raw = {'files': {}, 'processes': [], 'identity_errors': [], 'disk_free_bytes': None}
        self.host = {'observed_at': 1000, 'boot_id': 'boot', 'tasks': {'task': self.raw},
                     'gpu': {'rows': []}, 'gpu_processes': {'rows': []}}

    def document(self, value):
        self.raw['files']['status.json'] = {'text': json.dumps(value)}

    def test_state_precedence_and_conflicts_for_all_terminal_states(self):
        for declared in ('COMPLETED', 'FAILED', 'STOPPED', 'RUNNING'):
            for rc in (None, 0, 1):
                for alive in (False, True):
                    with self.subTest(declared=declared, rc=rc, alive=alive):
                        self.document({'state': declared})
                        self.raw['files']['exit.json'] = {'text': json.dumps({'exit_code': rc})}
                        self.raw['processes'] = [{'pid': 9, 'state': 'S'}] if alive else []
                        result = monitor.evaluate(self.task, self.host)
                        expected = 'STOPPED' if declared == 'STOPPED' and rc in (None, 0) else (
                            ('COMPLETED' if rc == 0 else 'FAILED') if rc is not None else (
                            declared if declared in monitor.TERMINAL else
                            'RUNNING' if alive else 'EXITED_WITHOUT_RESULT'))
                        conflict = (alive and expected in monitor.TERMINAL) or (
                            rc is not None and declared in monitor.TERMINAL and declared != expected)
                        self.assertEqual(result['state'], expected)
                        self.assertEqual('STATUS_CONFLICT' in result['alerts'], conflict)
                        self.assertEqual(result['declared_state'], declared)
                        self.assertEqual(result['exit_code'], rc)

    def test_cancel_aliases_and_custom_terminal_mapping_keep_successful_stop(self):
        for declared in ('STOPPED', 'CANCELLED', 'CANCELED', 'OPERATOR_STOP'):
            with self.subTest(declared=declared):
                task = copy.deepcopy(self.task)
                if declared == 'OPERATOR_STOP':
                    task['terminal_states'] = {'STOPPED': ['OPERATOR_STOP']}
                self.document({'state': declared})
                self.raw['files']['exit.json'] = {'text': '{"exit_code": 0}'}
                result = monitor.evaluate(task, self.host)
                self.assertEqual(result['state'], 'STOPPED')
                self.assertNotIn('STATUS_CONFLICT', result['alerts'])
                self.assertTrue(monitor.all_tasks_terminal([result]))

    def test_paused_and_scheduler_waiting_states(self):
        self.document({'state': 'RUNNING'})
        self.raw['processes'] = [{'pid': 9, 'state': 'T'}, {'pid': 10, 'state': 't'}]
        self.assertEqual(monitor.evaluate(self.task, self.host)['state'], 'PAUSED')
        self.raw['processes'] = []
        self.task['scheduler'] = {'type': 'gpu_scheduler', 'job_ids': ['job']}
        for state in ('QUEUED', 'STARTING'):
            self.document({'state': state})
            self.assertEqual(monitor.evaluate(self.task, self.host)['state'], state)

    def official_document(self):
        self.task['controller'] = {'type': 'research_handoff', 'run_id': 'run-1'}
        return {'state': 'RUNNING', 'controller': 'autoresearch-longrun', 'run_id': 'run-1',
                'budget': {'mode': 'active', 'active_seconds': 100, 'window_seconds': 1000,
                           'hard_limit_seconds': 2000, 'started_at': monitor.utc(500),
                           'hard_deadline_at': monitor.utc(5000)},
                'budget_mode': 'effective', 'effective_target_seconds': 39600,
                'effective_limit_seconds': 43200,
                'agents': {'sol': {'status': 'RUNNING', 'effective_seconds': 30000}}}

    def test_official_source_does_not_mix_legacy_credit_and_applies_wall_limit(self):
        doc = self.official_document()
        self.task['status']['key'] = 'agents.sol.status'
        self.document(doc)
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual(result['effective_seconds'], 100)
        self.assertEqual(result['effective_target_seconds'], 1000)
        self.assertEqual(result['wall_limit_seconds'], 2000)
        self.assertIsNone(result['effective_limit_seconds'])
        self.assertEqual(result['deadline_at'], monitor.utc(2500))
        self.assertEqual(result['budget_source'], 'research_handoff')

    def test_controller_mismatch_keeps_budget_unknown(self):
        for key, value, alert in (
                ('controller', 'another-controller', 'CONTROLLER_TYPE_MISMATCH'),
                ('run_id', 'another-run', 'CONTROLLER_IDENTITY_MISMATCH')):
            with self.subTest(key=key):
                doc = self.official_document()
                doc[key] = value
                self.document(doc)
                result = monitor.evaluate(self.task, self.host)
                self.assertIn(alert, result['alerts'])
                for field in ('effective_seconds', 'effective_target_seconds',
                              'budget_remaining_seconds', 'deadline_at'):
                    self.assertIsNone(result[field])
                self.assertEqual(result['budget_source'], 'unknown')

    def test_wrong_run_terminal_is_not_accepted_or_used_to_finish_watch(self):
        doc = self.official_document()
        doc.update(run_id='another-run', state='COMPLETED')
        self.document(doc)
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertEqual(result['declared_state'], 'COMPLETED')
        self.assertFalse(monitor.all_tasks_terminal([result]))
        self.raw['processes'] = [{'pid': 9, 'state': 'S'}]
        result = monitor.evaluate(self.task, self.host)
        self.assertEqual(result['state'], 'RUNNING')

    def test_until_terminal_refuses_identity_and_observation_conflicts(self):
        task = {'state': 'COMPLETED', 'processes': [], 'alerts': []}
        self.assertTrue(monitor.all_tasks_terminal([task]))
        for alert in monitor.UNCERTAIN_LIFECYCLE_ALERTS:
            with self.subTest(alert=alert):
                self.assertFalse(monitor.all_tasks_terminal([{**task, 'alerts': [alert]}]))

    def test_unregistered_official_status_never_supplies_credit(self):
        doc = self.official_document()
        del self.task['controller']
        self.document(doc)
        result = monitor.evaluate(self.task, self.host)
        self.assertIn('CONTROLLER_TYPE_MISMATCH', result['alerts'])
        self.assertIsNone(result['effective_seconds'])

    def test_invalid_official_budget_stays_observation_error(self):
        for budget in (None, [], {'mode': 'active'}, {'mode': 'invalid'},
                       {'mode': 'active', 'active_seconds': -1, 'window_seconds': 1000,
                        'hard_limit_seconds': 500}):
            with self.subTest(budget=budget):
                doc = self.official_document()
                doc['budget'] = budget
                self.document(doc)
                result = monitor.evaluate(self.task, self.host)
                self.assertIn('OBSERVATION_ERROR', result['alerts'])
                self.assertIsNone(result['effective_seconds'])

    def test_round_timeout_is_shared_and_preserves_successful_host(self):
        release = threading.Event()
        finished = threading.Event()
        slow_task = {**self.task, 'id': 'slow', 'host': 'slow'}
        cfg = {'tasks': [self.task, slow_task], 'hosts': {'local': {}, 'slow': {}},
               'timeout_seconds': 1, 'probe_round_timeout_seconds': .05}
        deadlines = []

        def fake_probe(host, tasks, config, deadline):
            deadlines.append(deadline)
            if tasks[0]['id'] == 'slow':
                try:
                    release.wait(2)
                    return {'error': 'late'}
                finally:
                    finished.set()
            return self.host

        try:
            with patch('monitor.probe_host', side_effect=fake_probe):
                started = time.monotonic()
                result = monitor.snapshot(cfg)
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, .5)
            self.assertEqual(deadlines[0], deadlines[1])
            self.assertEqual(result['tasks'][0]['state'], 'NOT_STARTED')
            self.assertEqual(result['tasks'][1]['state'], 'UNREACHABLE')
            self.assertIn('timeout', result['hosts']['slow']['error'])
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_probe_exception_is_isolated_to_host_and_empty_snapshot_is_valid(self):
        cfg = {'tasks': [self.task], 'hosts': {'local': {}}, 'timeout_seconds': 1}
        with patch('monitor.probe_host', side_effect=RuntimeError('internal failure')):
            result = monitor.snapshot(cfg)
        self.assertEqual(result['tasks'][0]['state'], 'UNREACHABLE')
        cfg.update(tasks=[], hosts={})
        self.assertEqual(monitor.snapshot(cfg)['tasks'], [])

    def test_expired_probe_deadline_launches_nothing(self):
        cfg = {'tail_bytes': 1024, 'metadata_bytes': 1024, 'timeout_seconds': 10}
        with patch('monitor.subprocess.run') as run:
            result = monitor.probe_host({'transport': 'local'}, [], cfg, time.monotonic() - 1)
        run.assert_not_called()
        self.assertIn('timeout', result['error'])

    def test_probe_retry_and_host_key_cleanup_use_remaining_round_budget(self):
        cfg = {'tail_bytes': 1024, 'metadata_bytes': 1024, 'timeout_seconds': 10,
               'connection_attempts': 2, 'retry_delay_seconds': 20}
        host = {'transport': 'ssh', 'hostname': 'example', 'user': 'test', 'password': 'test-secret'}
        failure = subprocess.CompletedProcess('ssh', 255, '', 'Host key verification failed')
        with patch('monitor.time.monotonic', side_effect=[90, 98, 101]), \
                patch('monitor.run_ssh', return_value=failure) as run, \
                patch('monitor.purge_host_key') as purge:
            result = monitor.probe_host(host, [], cfg, deadline=100)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[-1], 10)
        self.assertEqual(purge.call_args.kwargs['timeout'], 2)
        self.assertEqual(result['error'], 'probe round timeout')

    def test_stop_guidance_is_local_and_records_official_identity(self):
        self.task['controller'] = {'type': 'research_handoff', 'run_id': 'run-1'}
        with tempfile.TemporaryDirectory() as tmp, patch('monitor.subprocess.run') as run:
            state = Path(tmp)
            with redirect_stdout(StringIO()):
                code = monitor.stop_task({}, self.task, 'review', False, state)
            receipt = json.loads((state / 'stop-requests.jsonl').read_text())
        run.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(receipt['result'], 'OFFICIAL_STOP_REQUIRED')
        self.assertEqual(receipt['controller']['run_id'], 'run-1')
        self.assertFalse(receipt['training_stopped_confirmed'])

    def test_stop_followup_preserves_custom_configuration_auth_and_task_scope(self):
        self.task['controller'] = {'type': 'research_handoff', 'run_id': 'run-1'}
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            state = Path(tmp)
            config, auth = state / 'custom tasks.json', state / 'custom auth.txt'
            monitor.stop_task({}, self.task, 'review', False, state, config, auth)
            receipt = json.loads((state / 'stop-requests.jsonl').read_text())
        import shlex
        full = shlex.split(receipt['monitor_command'])
        scoped = shlex.split(receipt['verification_command'])
        self.assertEqual(full[full.index('--config') + 1], str(config))
        self.assertEqual(full[full.index('--auth') + 1], str(auth))
        self.assertNotIn('--task', full)
        self.assertEqual(scoped[scoped.index('--task') + 1], self.task['id'])

    def test_cli_stop_requires_exactly_one_task_and_nonempty_reason_without_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'tasks.json'
            task = {**self.task, 'controller': {'type': 'research_handoff', 'run_id': 'run-1'}}
            config.write_text(json.dumps({'version': 1, 'hosts': {'local': {'transport': 'local'}},
                                          'tasks': [task, {**task, 'id': 'other'}]}))
            base = [sys.executable, monitor.__file__, 'stop-task', '--config', str(config),
                    '--auth', str(root / 'absent-auth')]
            for arguments, expected in ((['--task', 'task', '--reason', 'review'], 2),
                                        (['--task', 'task', '--task', 'other', '--reason', 'review'], 1),
                                        (['--task', 'task', '--reason', '  '], 1)):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(base + arguments, text=True, capture_output=True, timeout=3)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_config_rejects_legacy_stop_fields_even_when_empty_and_invalid_error_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'tasks.json'
            base = {'version': 1, 'hosts': {'local': {'transport': 'local'}}, 'tasks': [self.task]}
            for key in ('stop', 'marker', 'process_groups'):
                config = copy.deepcopy(base)
                config['tasks'][0][key] = None
                path.write_text(json.dumps(config))
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'custom stop'):
                    monitor.load_config(path)
            invalid = [{'error_patterns': {}}, {'error_patterns': [{'pattern': ''}]},
                       {'error_patterns': [{'pattern': '['}]},
                       {'error_patterns': [{'pattern': 'x', 'severity': 'fatal'}]},
                       {'error_window_lines': 0}, {'error_window_lines': True}]
            for fields in invalid:
                config = copy.deepcopy(base)
                config['tasks'][0]['streams'] = [{'id': 's', 'path': 'log', **fields}]
                path.write_text(json.dumps(config))
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    monitor.load_config(path)


if __name__ == '__main__':
    unittest.main()
