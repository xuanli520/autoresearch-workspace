import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.gpu_monitor import runtime
from tools.gpu_monitor import probe


class RuntimeTests(unittest.TestCase):
    def test_endpoint_aliases_are_deduplicated_without_password(self):
        hosts = {
            'a': {'transport': 'ssh', 'hostname': 'gpu.example', 'port': 22,
                  'user': 'runner', 'password': 'secret-a'},
            'b': {'transport': 'ssh', 'hostname': 'gpu.example', 'port': 22,
                  'user': 'runner', 'password': 'secret-b'},
            'other': {'transport': 'ssh', 'hostname': 'gpu.example', 'port': 2222,
                      'user': 'runner'},
        }
        grouped = runtime.group_endpoints(hosts)
        self.assertEqual(len(grouped), 2)
        merged = next(item for item in grouped.values() if set(item['aliases']) == {'a', 'b'})
        self.assertNotIn('password', merged['endpoint_id'])

    def test_scheduler_projection_uses_explicit_status_and_marks_terminal_historical(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'job'
            directory.mkdir()
            (directory / 'status.json').write_text(json.dumps({
                'id': 'job-current', 'request_id': 'req/1', 'state': 'SUCCEEDED',
                'revision': 4, 'submitted_at': 10, 'finished_at': 20,
            }))
            task = {'id': 'task', 'root': str(directory), 'scheduler': {
                'type': 'gpu_scheduler', 'job_ids': ['job-old'],
                'status_paths': [str(directory / 'status.json')],
            }}
            value = runtime.scheduler_attempts(task)
            self.assertEqual(value[0]['state'], 'SUCCEEDED')
            self.assertTrue(value[0]['stale'])
            self.assertIsNone(runtime.active_scheduler_job(task))

    def test_ambiguous_container_owner_is_unknown(self):
        app = {'pid': '33', 'container_ids': ['a' * 64]}
        tasks = [{'id': 'one', 'gpu_container_ids': ['a' * 64]},
                 {'id': 'two', 'gpu_container_ids': ['a' * 64]}]
        result = runtime.resolve_gpu_owner(app, tasks)
        self.assertIsNone(result['owner_task_id'])
        self.assertEqual(result['confidence'], 'unknown')
        self.assertIn('conflicting', result['reason'])

    def test_pid_identity_is_a_compatibility_fallback(self):
        app = {'pid': '33', 'container_ids': []}
        tasks = [{'id': 'one', 'processes': [{'pid': '33'}]}]
        result = runtime.resolve_gpu_owner(app, tasks)
        self.assertEqual(result['owner_task_id'], 'one')
        self.assertEqual(result['source'], 'registered_pid_identity')

    def test_scheduler_events_find_current_attempt_and_restore_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'sessions' / 'session-new'
            directory.mkdir(parents=True)
            (root / 'service.json').write_text(json.dumps({'session_id': 'session-new'}))
            rows = [
                {'id': 'job-old', 'request_id': 'task/group/0001', 'state': 'EXPIRED',
                 'revision': 2, 'submitted_at': 10, 'at': 20},
                {'id': 'job-current', 'request_id': 'task/group/0002', 'state': 'RUNNING',
                 'revision': 2, 'submitted_at': 30, 'started_at': 40, 'at': 40,
                 'session_id': 'session-old', 'origin_session_id': 'session-old'},
                {'id': 'job-current', 'request_id': 'task/group/0002', 'state': 'UNKNOWN',
                 'revision': 3, 'submitted_at': 30, 'started_at': 40, 'at': 50,
                 'reconciling': True, 'session_id': 'session-new', 'origin_session_id': 'session-old'},
                {'id': 'private-job', 'request_id': 'other/group/0002', 'owner': 'secret-owner',
                 'state': 'QUEUED', 'submitted_at': 20, 'reason': 'GPU-a:insufficient_memory_reservation',
                 'resources': {'memory_mib': 4000}},
            ]
            (directory / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
            task = {'id': 'task', 'root': str(root), 'scheduler': {
                'root': str(root), 'job_ids': ['job-old'], 'request_ids': ['task/group/0001']}}
            view = runtime.collect_scheduler(task, now=60)
            active = view['active_job']
            self.assertEqual(active['job_id'], 'job-current')
            self.assertEqual(active['state'], 'UNKNOWN')
            self.assertEqual(active['origin_session_id'], 'session-old')
            self.assertEqual(view['configured_job_state'], 'historical')
            external = view['external_queue_summary']
            self.assertEqual(external['count'], 1)
            self.assertEqual(external['blocking_reasons'], ['insufficient_memory_reservation'])
            self.assertNotIn('secret-owner', json.dumps(view))
            self.assertNotIn('private-job', json.dumps(view))

    def test_infeasible_is_history_and_duplicate_request_spec_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'sessions' / 'session'
            directory.mkdir(parents=True)
            rows = [
                {'id': 'job', 'request_id': 'req', 'state': 'QUEUED', 'revision': 1,
                 'spec': {'request_id': 'req', 'memory_mib': 1000}},
                {'id': 'job', 'request_id': 'req', 'state': 'RUNNING', 'revision': 2,
                 'spec': {'request_id': 'req', 'memory_mib': 2000}},
            ]
            (directory / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
            task = {'id': 'task', 'root': tmp, 'scheduler': {'root': tmp, 'session_id': 'session',
                                                          'job_ids': ['job'], 'request_ids': ['req']}}
            view = runtime.collect_scheduler(task)
            self.assertEqual(view['active_job']['state'], 'UNKNOWN')
            self.assertTrue(view['active_job']['identity_conflict'])
            (directory / 'events.jsonl').write_text(json.dumps({
                'id': 'job', 'request_id': 'req', 'state': 'INFEASIBLE', 'revision': 3,
                'resources': {'memory_mib': 2000}}) + '\n')
            self.assertIsNone(runtime.collect_scheduler(task)['active_job'])

    def test_trusted_receipt_has_priority_and_bad_boot_is_unknown(self):
        cid = 'a' * 64
        cgroup = '/system.slice/docker-' + cid + '.scope'
        app = {'pid': '77', 'cgroup_paths': [{'controller': '', 'path': cgroup + '/child'}],
               'container_ids': [cid], 'pid_identity': {'start_ticks': 99}}
        receipt = {'trusted': True, 'boot_id': 'boot', 'job_id': 'job',
                   'cgroup_paths': [{'controller': '', 'path': cgroup}]}
        task = {'id': 'task', 'ownership_receipts': [receipt]}
        owner = runtime.resolve_gpu_owner(app, [task], boot_id='boot')
        self.assertEqual(owner['source'], 'scheduler_receipt_cgroup')
        self.assertEqual(owner['owner_job_id'], 'job')
        self.assertIsNone(runtime.resolve_gpu_owner(app, [task], boot_id='another')['owner_task_id'])
        task['processes'] = [{'pid': '77', 'start_ticks': 98}]
        self.assertIsNone(runtime.resolve_gpu_owner(app, [task], boot_id='another')['owner_task_id'])

    def test_short_container_ids_are_accepted_only_when_unique(self):
        cid = 'b' * 64
        app = {'pid': '77', 'container_ids': [cid]}
        task = {'id': 'one', 'gpu_container_ids': [cid[:12]]}
        self.assertEqual(runtime.resolve_gpu_owner(app, [task])['owner_task_id'], 'one')
        self.assertIsNone(runtime.resolve_gpu_owner(app, [task, {'id': 'two',
                         'gpu_container_ids': [cid[:12]]}])['owner_task_id'])

    def test_receipt_collection_checks_boot_pid_and_token_without_exporting_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / 'sessions' / 'session' / 'jobs' / 'job'
            receipts = job / 'containers'
            receipts.mkdir(parents=True, mode=0o700)
            cid = 'a' * 64
            (job / 'launch.json').write_text(json.dumps({'id': 'job', 'token': 'private-token'}))
            row = {'version': 1, 'job_id': 'job', 'token': 'private-token', 'boot_id': 'boot',
                   'container_id': cid, 'init_pid': 77, 'init_start_ticks': 99}
            (receipts / (cid + '.json')).write_text(json.dumps(row))
            task = {'id': 'task', 'root': tmp, 'scheduler': {'root': tmp, 'session_id': 'session'}}
            view = {'attempts': [{'job_id': 'job', 'state': 'RUNNING', 'session_id': 'session'}]}
            with patch.object(runtime, '_pid_matches', return_value=True), patch.object(runtime, '_pid_cgroups',
                    return_value=[{'controller': '', 'path': '/system.slice/docker-' + cid + '.scope'}]):
                value = runtime.collect_receipts(task, view, boot_id='boot')
                self.assertEqual(len(value), 1)
                self.assertNotIn('private-token', json.dumps(value))
                self.assertEqual(runtime.collect_receipts(task, view, boot_id='wrong-boot'), [])
            with patch.object(runtime, '_pid_matches', return_value=False):
                self.assertEqual(runtime.collect_receipts(task, view, boot_id='boot'), [])

    def test_enrich_missing_remote_scheduler_state_never_reads_paths_locally(self):
        raw = {'observed_at': 10, 'boot_id': 'boot', 'tasks': {'task': {'files': {}, 'processes': []}},
               'gpu_processes': {'rows': []}}
        task = {'id': 'task', 'root': '/remote/task', 'scheduler': {'root': '/remote/scheduler', 'job_ids': ['historical']}}
        with patch.object(runtime, 'collect_scheduler', side_effect=AssertionError('local remote-path read')):
            value = runtime.enrich_host(raw, [task], 'endpoint')
        self.assertTrue(value['tasks']['task']['scheduler_view']['data_gap'])
        self.assertIsNone(value['tasks']['task']['scheduler_view']['active_job'])

    def test_endpoint_external_summary_excludes_all_registered_tasks(self):
        outside = {'state': 'QUEUED', 'resources': {'memory_mib': 1200}, 'waiting_seconds': 30,
                   'reason': 'insufficient_memory_reservation'}
        def identity(request):
            return runtime.hashlib.sha256(request.encode()).hexdigest()
        one = {'job_id': 'one-job', 'request_id': 'one/1', 'state': 'QUEUED', 'waiting_seconds': 20}
        two = {'job_id': 'two-job', 'request_id': 'two/1', 'state': 'QUEUED', 'waiting_seconds': 30}
        raw = {'observed_at': 100, 'tasks': {
            'one': {'processes': [], 'scheduler_view': {'attempts': [one],
                    '_external_jobs': [{**outside, '_identity': identity('two/1')},
                                       {**outside, '_identity': identity('private/1')}] }},
            'two': {'processes': [], 'scheduler_view': {'attempts': [two],
                    '_external_jobs': [{**outside, '_identity': identity('one/1')},
                                       {**outside, '_identity': identity('private/1')}] }}},
               'gpu_processes': {'rows': []}}
        value = runtime.enrich_host(raw, [{'id': 'one'}, {'id': 'two'}], 'endpoint')
        self.assertEqual(value['external_queue_summary']['count'], 1)
        self.assertEqual(value['external_queue_summary']['resources']['memory_mib'], 1200)
        self.assertEqual(value['scheduler_summary']['queued'], 2)

    def test_handoff_events_are_collected_from_registered_status_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'controller' / 'runs' / 'run'
            run.mkdir(parents=True)
            (run / 'state.json').write_text(json.dumps({'run_id': 'run'}))
            (run / 'events.jsonl').write_text('unused-private-prefix' * 100 + '\n' +
                json.dumps({'event': 'turn.retry_scheduled', 'at': 1, 'reason': 'agent_exit_nonzero'}) + '\n')
            task = {'id': 'task', 'root': tmp, 'controller': {'type': 'research_handoff', 'run_id': 'run'},
                    'status': {'path': 'controller/runs/run/state.json'}, 'processes': [], 'streams': []}
            with patch.object(probe, 'process_table', return_value=([], 0)), patch.object(probe, 'gpu_query', return_value={'rows': []}):
                value = probe.collect({'tasks': [task], 'tail_bytes': 256})
            entry = value['tasks']['task']['files']['controller/runs/run/events.jsonl']
            self.assertTrue(entry['truncated'])
            self.assertIn('turn.retry_scheduled', entry['text'])
            self.assertNotIn('unused-private-prefix', entry['text'])

    def test_durable_ledger_replays_latest_session_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records, previous = [], '0' * 64
            spec = {'request_id': 'task/group/0001', 'memory_mib': 1000}
            for number, state in enumerate(('RUNNING', 'UNKNOWN'), 1):
                row = {'version': 1, 'sequence': number, 'previous': previous,
                       'at_epoch': number * 10, 'session_id': 'session-new' if number == 2 else 'session-old',
                       'job': {'id': 'job', 'state': state, 'revision': number, 'started_at': 10,
                               'submitted_at': 1, 'origin_session_id': 'session-old', 'spec': spec,
                               'reconciling': number == 2}}
                previous = runtime.hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=True, allow_nan=False).encode()).hexdigest()
                records.append({**row, 'sha256': previous})
            path = root / 'requests.jsonl'
            path.write_text(''.join(json.dumps(row) + '\n' for row in records))
            original = path.read_bytes()
            task = {'id': 'task', 'root': tmp, 'scheduler': {'root': tmp, 'job_ids': ['job'],
                    'request_ids': ['task/group/0001']}}
            view = runtime.collect_scheduler(task, now=30)
            self.assertEqual(view['active_job']['state'], 'UNKNOWN')
            self.assertEqual(view['active_job']['session_id'], 'session-new')
            self.assertEqual(view['active_job']['origin_session_id'], 'session-old')
            self.assertEqual(view['active_job']['source'], 'scheduler_ledger')
            self.assertEqual(path.read_bytes(), original)

    def test_request_attempt_template_keeps_group_and_research_suffix(self):
        requests = {'auto1066/run/gpt/000027/research'}
        patterns = runtime._request_patterns(requests)
        good = {'id': 'new', 'request_id': 'auto1066/run/gpt/000028/research'}
        self.assertTrue(runtime._registered_match(good, set(), requests, patterns))
        for bad in ('auto1066/run/gpt/000027/private', 'auto1066/run/seed/000028/research',
                    'auto1066/run/gpt/000028/research/extra'):
            self.assertFalse(runtime._registered_match({'id': 'bad', 'request_id': bad}, set(), requests, patterns))
        self.assertEqual(runtime._request_patterns({'task/run/diagnostic'}), [])

    def test_invalid_scheduler_path_and_conflicting_same_job_receipts_are_unknown(self):
        task = {'id': 'task', 'root': '/remote/task', 'scheduler': {'root': '/remote/scheduler',
                    'job_ids': ['job'], 'status_paths': ['/private/evidence/status.json']}}
        with patch.object(runtime, '_json', side_effect=AssertionError('invalid path read')):
            self.assertTrue(runtime.collect_scheduler(task)['data_gap'])
        app = {'pid': '77', 'pid_identity': {'start_ticks': 99},
               'cgroup_paths': [{'controller': '', 'path': '/docker/container/child'}]}
        scopes = [{'trusted': True, 'boot_id': 'boot', 'job_id': jid,
                   'cgroup_paths': [{'controller': '', 'path': '/docker/container'}]} for jid in ('job-one', 'job-two')]
        self.assertIsNone(runtime.resolve_gpu_owner(app, [{'id': 'task', 'ownership_receipts': scopes}], boot_id='boot')['owner_task_id'])

    def test_status_event_disagreement_at_same_revision_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / 'sessions' / 'session'
            job = session / 'jobs' / 'job'
            job.mkdir(parents=True)
            (job / 'status.json').write_text(json.dumps({'id': 'job', 'request_id': 'req',
                       'state': 'RUNNING', 'revision': 2, 'submitted_at': 1}))
            (session / 'events.jsonl').write_text(json.dumps({'id': 'job', 'request_id': 'req',
                       'state': 'QUEUED', 'revision': 2, 'submitted_at': 1, 'at': 2}) + '\n')
            task = {'id': 'task', 'root': tmp, 'scheduler': {'root': tmp, 'session_id': 'session', 'job_ids': ['job']}}
            view = runtime.collect_scheduler(task)
            self.assertEqual(view['active_job']['state'], 'UNKNOWN')
            self.assertIn('SCHEDULER_IDENTITY_CONFLICT', view['alerts'])

    def test_corrupt_ledger_does_not_make_static_running_status_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'sessions' / 'session' / 'jobs' / 'job'
            directory.mkdir(parents=True)
            (directory / 'status.json').write_text(json.dumps({'id': 'job', 'request_id': 'req',
                                                               'state': 'RUNNING', 'revision': 2}))
            ledger = root / 'requests.jsonl'
            ledger.write_text(json.dumps({'sha256': 'invalid', 'job': {'id': 'job'}}) + '\n')
            original = ledger.read_bytes()
            task = {'id': 'task', 'root': tmp, 'scheduler': {'root': tmp, 'session_id': 'session', 'job_ids': ['job']}}
            value = runtime.collect_scheduler(task)
            self.assertEqual(value['active_job']['state'], 'UNKNOWN')
            self.assertIn('SCHEDULER_LEDGER_INVALID', value['alerts'])
            self.assertEqual(ledger.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
