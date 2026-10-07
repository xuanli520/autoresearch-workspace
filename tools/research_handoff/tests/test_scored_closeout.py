"""Scored adapters keep audited time independently of scientific close-out."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.research_handoff.core.completion import CompletionContractError
from tools.research_handoff.core.credit import partial_report, persist_report
from tools.research_handoff.tests.test_scored_failure_reporting import load_adapters


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class ScoredCloseoutTests(unittest.TestCase):
    def fixture(self, root, task, *, active=35990, terminal=True):
        modules = load_adapters(task)
        adapter = modules['scored_research']
        run_id = 'gpt' if task == 'auto0804' else 'run-gpt'
        config = {'stage_root': str(root), 'gpu_root': str(root / 'scheduler'), 'gpu_session': 'old-session',
                  'formal_seeds': [1, 2, 3], 'partial_credit_enabled': True, 'evaluation_slice_seconds': 0,
                  'deadline_epoch': 9999, 'agents': {'gpt': {'run_id': run_id}}}
        turn = root / 'controller/runs' / run_id / 'turns/000342'
        write(turn / 'worker-start.json', {'at': '1970-01-01T00:01:40Z', 'monotonic': time.monotonic() - 20})
        write(turn.parent.parent / 'state.json', {'budget': {'window_seconds': 36000, 'active_seconds': active}})
        write(turn.parent.parent / 'completion.contract.json', {})
        agent, _ = adapter.roots(config, 'gpt')
        folder = agent / 'evaluations/000342'
        write(folder / 'evaluation-contract.json', {})
        write(folder / 'request.json', {'request_id': 'original-request'})
        session = root / 'session.jsonl'
        session.write_text('{"original": "native session"}\n')
        record = {'group': 'gpt', 'turn': 342, 'research_status': 'SUCCEEDED', 'candidate': str(root / 'method.py'),
                  'candidate_sha256': 'a' * 64, 'method_summary': 'Completed real research feedback.',
                  'intervals': [[100, 110]], 'sessions': [{'path': str(session),
                  'sha256': hashlib.sha256(session.read_bytes()).hexdigest(), 'conversation_id': 'conversation',
                  'last_token_usage': {'input_tokens': 1, 'output_tokens': 1}}]}
        job = {'id': 'original-job', 'request_id': 'original-request', 'origin_session_id': 'origin-session',
               'state': 'SUCCEEDED' if terminal else 'RUNNING',
               'exit': {'returncode': 0, 'cleanup_ok': True} if terminal else None}
        pending = {'folder': str(folder), 'record': record, 'request_id': job['request_id'], 'job_id': job['id']}
        write(agent / 'pending.json', pending)
        write(folder / 'evaluation-result.json', {'status': 'OK', 'score': .5})
        activity = folder / 'jobs/formal/trial/verifier/activity.jsonl'
        activity.parent.mkdir(parents=True)
        activity.write_text(json.dumps({'event': 'stage_started', 'pid': 1, 'log': 'train',
                                        'started_at_epoch': 100, 'at_epoch': 100}) + '\n' +
                            json.dumps({'event': 'stage_progress', 'pid': 1, 'log': 'train',
                                        'started_at_epoch': 100, 'at_epoch': 110}) + '\n')
        client = Mock(session_id='origin-session')
        client.ensure.return_value = client.wait.return_value = client.get.return_value = job
        return modules, adapter, config, turn, folder, pending, client

    @contextlib.contextmanager
    def mocked_run(self, fixture, *, selection_error=None, seal_error=None):
        modules, adapter, config, turn, folder, pending, client = fixture
        calls = []
        def clock():
            calls.append(None)
            return 100 if len(calls) == 1 else 110
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'AUTORESEARCH_TURN_DIR': str(turn), 'AUTORESEARCH_TURN': '342'}))
            stack.enter_context(patch.dict(__import__('sys').modules, modules))
            stack.enter_context(patch('time.time', side_effect=clock))
            stack.enter_context(patch.object(adapter, 'Client', return_value=client))
            for name in ('register_jobs', 'write_summary', 'cleanup_round', 'context_usage', 'compact', 'turn_credit', 'gpu_state'):
                stack.enter_context(patch.object(adapter, name))
            stack.enter_context(patch.object(modules['research_turn'], 'infrastructure_result', create=True))
            design = stack.enter_context(patch.object(modules['research_turn'], 'run', side_effect=AssertionError('new model call')))
            seal = stack.enter_context(patch.object(adapter, 'seal_experiment', side_effect=seal_error, return_value=(.5, None)))
            adopt = stack.enter_context(patch.object(adapter, 'adopt_evaluation', side_effect=selection_error,
                                                    return_value={'status': 'COMPLETED'}))
            complete = stack.enter_context(patch.object(adapter, 'turn_complete'))
            yield design, seal, adopt, complete

    def validate_proof(self, fixture):
        _, adapter, config, turn, _, _, _ = fixture
        report = json.loads((turn / 'partial-credit.json').read_text())
        checked = partial_report(turn / 'partial-credit.json', run_id=report['run_id'], turn=342,
                                 lower=100, upper=110, maximum=10, allowed=config['stage_root'])
        self.assertEqual(checked['credited_seconds'], report['credited_seconds'])
        for source in report['evidence']:
            self.assertTrue(Path(source['path']).is_relative_to(turn / 'research-evidence'))
        return report

    def test_adoption_failure_keeps_full_credit_and_real_successful_score(self):
        for task in ('auto0802', 'auto0804'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), task)
                _, adapter, config, turn, _, _, client = fixture
                error = CompletionContractError('SCIENTIFIC_CONTRACT_MISMATCH', 'incompatible contract')
                with self.mocked_run(fixture, selection_error=error) as (design, seal, adopt, complete):
                    with self.assertRaises(CompletionContractError):
                        adapter.run(config, 'gpt')
                    design.assert_not_called()
                    client.submit.assert_not_called()
                    client.submit_async.assert_not_called()
                    complete.assert_not_called()
                    self.assertEqual(self.validate_proof(fixture)['credited_seconds'], 10)
                    failure = json.loads((turn / 'selection-failure.json').read_text())
                    self.assertEqual(failure['score'], .5)
                    self.assertFalse(failure['retryable'])
                    agent, _ = adapter.roots(config, 'gpt')
                    trajectory = json.loads((agent / 'trajectory.json').read_text())
                    self.assertEqual(trajectory['rounds'][0]['status'], 'SUCCEEDED')
                    self.assertEqual(trajectory['rounds'][0]['score'], .5)

    def test_last_second_waits_original_job_and_adopts_without_new_work(self):
        for task in ('auto0802', 'auto0804'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), task, active=35999, terminal=False)
                _, adapter, config, turn, _, _, client = fixture
                running = client.wait.return_value
                succeeded = {**running, 'state': 'SUCCEEDED', 'exit': {'returncode': 0, 'cleanup_ok': True}}
                client.wait.side_effect = [running, succeeded] if task == 'auto0804' else [succeeded]
                with self.mocked_run(fixture) as (design, seal, adopt, complete):
                    self.assertEqual(adapter.run(config, 'gpt'), 0)
                    self.assertEqual(complete.call_args.kwargs['credited_seconds'], 1)
                    self.assertEqual(self.validate_proof(fixture)['credited_seconds'], 1)
                    agent, _ = adapter.roots(config, 'gpt')
                    self.assertFalse((agent / 'pending.json').exists())
                    design.assert_not_called()
                    adopt.assert_called_once()
                    seal.assert_called_once()
                    self.assertTrue(all(call.args == ('original-job',) for call in client.wait.call_args_list))
                    client.submit.assert_not_called()
                    client.submit_async.assert_not_called()

    def test_pending_below_target_survives_cleanup_with_its_original_proof(self):
        for task in ('auto0802', 'auto0804'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), task, active=35900, terminal=False)
                _, adapter, config, turn, _, pending, _ = fixture
                with self.mocked_run(fixture) as (design, seal, adopt, complete):
                    self.assertEqual(adapter.run(config, 'gpt'), 0)
                    self.assertEqual(complete.call_args.kwargs['credited_seconds'], 10)
                    before = (turn / 'partial-credit.json').read_bytes()
                    adapter.cleanup(config, 'gpt')
                    self.assertEqual((turn / 'partial-credit.json').read_bytes(), before)
                    agent, _ = adapter.roots(config, 'gpt')
                    self.assertEqual(json.loads((agent / 'pending.json').read_text()), pending)
                    design.assert_not_called()
                    adopt.assert_not_called()
                    seal.assert_not_called()

    def test_pending_target_interruption_retains_credit_without_completing_turn(self):
        for task in ('auto0802', 'auto0804'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), task, active=35999, terminal=False)
                _, adapter, config, turn, _, pending, _ = fixture
                with self.mocked_run(fixture) as (_, seal, adopt, complete):
                    with self.assertRaises(RuntimeError):
                        adapter.run(config, 'gpt')
                    self.assertEqual(self.validate_proof(fixture)['credited_seconds'], 1)
                    agent, _ = adapter.roots(config, 'gpt')
                    self.assertEqual(json.loads((agent / 'pending.json').read_text()), pending)
                    complete.assert_not_called()
                    seal.assert_not_called()
                    adopt.assert_not_called()

    def test_sealing_contract_error_preserves_original_pending_and_score(self):
        for task in ('auto0802', 'auto0804'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), task)
                _, adapter, config, turn, folder, pending, _ = fixture
                error = CompletionContractError('IMMUTABLE_EVIDENCE_CONFLICT', 'already sealed differently')
                with self.mocked_run(fixture, seal_error=error):
                    with self.assertRaises(CompletionContractError):
                        adapter.run(config, 'gpt')
                    self.assertEqual(self.validate_proof(fixture)['credited_seconds'], 10)
                    agent, _ = adapter.roots(config, 'gpt')
                    self.assertEqual(json.loads((agent / 'pending.json').read_text()), pending)
                    self.assertEqual(json.loads((agent / 'latest.json').read_text())['score'], .5)
                    before = (agent / 'latest.json').read_bytes()
                    with patch.object(adapter, 'preserve_pending', return_value=False):
                        adapter.cleanup(config, 'gpt')
                    self.assertEqual((agent / 'latest.json').read_bytes(), before)
                    self.assertEqual(json.loads((agent / 'pending.json').read_text()), pending)
                with patch.object(adapter, '_seal_experiment', side_effect=error):
                    with self.assertRaises(CompletionContractError):
                        adapter.seal_experiment(config, pending, {'id': 'original-job'})
                failure = json.loads((folder / 'seal-failure.json').read_text())
                self.assertEqual(failure['failure_category'], 'completion_contract_error')
                self.assertEqual(failure['score'], .5)

    def test_missing_selected_contract_is_nonretryable_contract_failure(self):
        for task in ('auto0802', 'auto0804'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), task)
                _, adapter, config, turn, _, _, _ = fixture
                agent, _ = adapter.roots(config, 'gpt')
                write(agent / 'best.json', {'score': -.5, 'evaluation_contract': str(agent / 'missing.json')})
                with self.assertRaises(CompletionContractError) as failure:
                    adapter.select_evaluation(config, 'gpt', turn)
                self.assertEqual(failure.exception.code, 'SELECTED_EVALUATION_INPUT_INVALID')
                receipt = json.loads((turn / 'selection-failure.json').read_text())
                self.assertFalse(receipt['retryable'])
                self.assertEqual(receipt['score'], -.5)

    def test_official_report_clips_runtime_deduplicates_and_caps_balance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root / 'worker-start.json', {'at': '1970-01-01T00:01:40Z', 'monotonic': 1})
            write(root / 'worker-exit.json', {'at': '1970-01-01T00:01:50Z', 'runtime_seconds': 10})
            original = root / 'original.jsonl'
            original.write_text('{"event": "training"}\n')
            report = persist_report(root, run_id='original-run', turn=1, intervals=[[90, 105], [103, 120]],
                                    sources=[{'path': str(original)}], prior=[[100, 102]], remaining=3)
            self.assertEqual(report['intervals'], [[102, 105]])
            self.assertEqual(report['credited_seconds'], 3)
            self.assertEqual(report['queue_credit'], 0)
            original.write_text('changed after snapshot')
            checked = partial_report(root / 'partial-credit.json', run_id='original-run', turn=1,
                                     lower=100, upper=110, maximum=10, allowed=root)
            self.assertEqual(checked['credited_seconds'], 3)

    def test_official_report_excludes_queue_intervals_from_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root / 'worker-start.json', {'at': '1970-01-01T00:01:40Z', 'monotonic': 1})
            write(root / 'worker-exit.json', {'at': '1970-01-01T00:01:50Z', 'runtime_seconds': 10})
            write(root / 'gpu-wait.json', {'waiting': False, 'excluded_seconds': 4, 'intervals': [[1, 5]]})
            original = root / 'original.jsonl'
            original.write_text('{"event": "training"}\n')
            report = persist_report(root, run_id='original-run', turn=1, intervals=[[100, 110]],
                                    sources=[{'path': str(original)}], prior=[], remaining=36000)
            self.assertEqual(report['intervals'], [[104, 110]])
            self.assertEqual(report['credited_seconds'], 6)
            self.assertEqual(report['queue_excluded_intervals'], [[100, 104]])

    def test_fractional_last_balance_does_not_disappear_at_real_epoch(self):
        from datetime import datetime, timezone
        start = 1800000000.0
        for active in (35998.765433, 35999.99999992696):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                iso = lambda epoch: datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                write(root / 'worker-start.json', {'at': iso(start), 'monotonic': 1})
                write(root / 'worker-exit.json', {'at': iso(start + 10), 'runtime_seconds': 10})
                original = root / 'original.jsonl'
                original.write_text('{"event": "training"}\n')
                remaining = 36000 - active
                report = persist_report(root, run_id='original-run', turn=1,
                                        intervals=[[start, start + 10]], sources=[{'path': str(original)}],
                                        prior=[], remaining=remaining)
                self.assertGreaterEqual(active + report['credited_seconds'], 36000)
                self.assertLessEqual(report['credited_seconds'], remaining + __import__('math').ulp(start))
                checked = partial_report(root / 'partial-credit.json', run_id='original-run', turn=1,
                                         lower=start, upper=start + 10, maximum=10, allowed=root)
                self.assertEqual(checked['credited_seconds'], report['credited_seconds'])
                self.assertLessEqual(report['intervals'][0][1], start + 10)

    def test_cleanup_recovers_formal_activity_without_design_sessions(self):
        for task in ('auto0802', 'auto0804'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                fixture = self.fixture(Path(temporary), task)
                _, adapter, config, turn, _, pending, _ = fixture
                write(turn / 'worker-exit.json', {'at': '1970-01-01T00:01:50Z', 'runtime_seconds': 10})
                agent, _ = adapter.roots(config, 'gpt')
                with patch.dict(os.environ, {'AUTORESEARCH_TURN': '342'}):
                    adapter.write_partial_credit(config, 'gpt', turn, agent / 'rounds/000342', pending=pending)
                self.assertEqual(self.validate_proof(fixture)['credited_seconds'], 10)

    def test_durable_adapter_binds_original_session_and_never_resubmits_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(Path(temporary), 'auto0802')
            _, adapter, config, _, folder, pending, client = fixture
            write(folder / 'submit-intent.json', {'request_id': pending['request_id']})
            pending['job_id'] = None
            with patch.object(adapter, 'Client', return_value=client), patch.object(adapter, 'register_jobs') as register:
                _, job = adapter.accept_evaluation_job(config, pending)
                client.get.assert_called_once_with(request_id='original-request')
                client.ensure.assert_not_called()
                client.submit_async.assert_not_called()
                self.assertEqual(register.call_args.args[1][0]['session_id'], 'origin-session')
                self.assertEqual(job['id'], 'original-job')


if __name__ == '__main__':
    unittest.main()
