import copy
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'core')]
import completion as c
import controller
import experiment_batch
import longrun
import research_completion as audit

SCRATCH = ROOT / 'incidents/completion-contracts/scratch'
SCRATCH.mkdir(parents=True, exist_ok=True)


def iso(seconds=60):
    return datetime.datetime.fromtimestamp(time.time() + seconds, datetime.timezone.utc).isoformat()


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=self._testMethodName + '-', dir=SCRATCH)
        self.base = Path(self.tmp.name).resolve()
        self.public = self.base / 'candidate'
        self.evidence = self.base / 'private'
        self.keys = self.base / 'keys'
        self.queue = self.base / 'queue'
        for path in (self.public, self.evidence, self.keys, self.queue):
            path.mkdir(mode=0o700)
        (self.public / 'method.py').write_text('def predict(x): return x\n')
        self.key = self.keys / 'completion.key'
        self.key.write_bytes(os.urandom(32))
        self.key.chmod(0o600)
        for path, value in (('protocol.txt', 'protocol-v1'), ('data.bin', 'data-v1'), ('evaluator.py', 'evaluator-v1')):
            (self.evidence / path).write_text(value)
        protocol = {'version': 1, 'protocol': [c.artifact(self.evidence, 'protocol.txt')],
                    'data': [c.artifact(self.evidence, 'data.bin')],
                    'evaluator': [c.artifact(self.evidence, 'evaluator.py')]}
        longrun.atomic_json(self.evidence / 'protocol.manifest.json', protocol)
        self.cfg = {
            'version': 1, 'task_id': 'completion-test', 'root': str(self.public), 'command': ['true'],
            'stage': 'formal', 'score_expectation': 'required', 'metric': 'accuracy', 'direction': 'max',
            'required_seeds': ['0', '1', '2'], 'deadline': iso(), 'candidate_manifest': 'candidate.manifest.json',
            'protocol_hash': c.file_hash(self.evidence / 'protocol.manifest.json'),
            'budget': {'window_seconds': .01, 'hard_limit_seconds': 30, 'credit_policy': 'successful_turn'},
            'turn': {'seconds': 1}, 'context': {'required': False},
            'heartbeat': {'required': False, 'interval_seconds': .03, 'controller_stale_seconds': .3},
            'output': {'min_free_bytes': 1},
            'completion': {'evidence_root': str(self.evidence), 'signing_key': str(self.key),
                           'protocol_manifest': 'protocol.manifest.json', 'data_hash': c.digest(protocol['data']),
                           'evaluator_hash': c.digest(protocol['evaluator']), 'training': True,
                           'jobs_manifest': 'evaluation.jobs.json', 'result': 'final.score.json',
                           'receipt': 'completion.receipt.json', 'isolation': 'isolation.attestation.json',
                           'private_roots': []},
        }
        self.contract = c.contract_for_run(self.cfg, 'test')
        self.make_candidate()
        self.jobs = [{'scheduler_root': str(self.queue), 'session_id': 'session',
                      'request_id': 'test/eval/original', 'job_id': 'job',
                      'seeds': ['0', '1', '2'], 'roles': ['score', 'reload']}]
        self.job_dir = self.queue / 'sessions/session/jobs/job'
        self.job_dir.mkdir(parents=True)
        self.job_state('SUCCEEDED')
        c.register_jobs(self.contract, self.jobs)
        self.attest()
        self.rows = self.make_rows()

    def tearDown(self):
        self.tmp.cleanup()

    def make_candidate(self):
        manifest = {'version': 1, 'source': [c.artifact(self.public, 'method.py')], 'models': [], 'checkpoints': []}
        for seed in self.cfg['required_seeds']:
            (self.public / f'model-{seed}.pt').write_bytes(f'model-{seed}'.encode())
            (self.public / f'checkpoint-{seed}.pt').write_bytes(f'checkpoint-{seed}'.encode())
            manifest['models'].append({'seed': seed, 'file': c.artifact(self.public, f'model-{seed}.pt')})
            manifest['checkpoints'].append({'seed': seed, 'file': c.artifact(self.public, f'checkpoint-{seed}.pt')})
        longrun.atomic_json(self.evidence / self.cfg['candidate_manifest'], manifest)
        self.manifest = manifest

    def attest(self, inspections=None):
        image = 'sha256:' + 'b' * 64
        if inspections is None:
            inspections = [{'Id': 'a' * 64, 'Image': image, 'HostConfig': {}, 'Config': {'Env': []}, 'Mounts': []}]
        with mock.patch.object(c.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(inspections), '')):
            return c.attest_docker_isolation(self.contract, ['a' * 64], docker_host='unix:///private/docker.sock', public_image_digest=image)

    def job_state(self, state):
        ended = time.time() - .01
        exited = {'at': ended, 'returncode': 0, 'reason': 'process_exit', 'cleanup_ok': True}
        snapshot = {'id': 'job', 'request_id': 'test/eval/original', 'session_id': 'session',
                    'state': state, 'finished_at': ended if state in c.JOB_TERMINAL else None,
                    'reason': 'process_exit' if state == 'SUCCEEDED' else 'awaiting_dispatch', 'exit': exited}
        longrun.atomic_json(self.job_dir / 'status.json', snapshot)
        longrun.atomic_json(self.job_dir / 'exit.json', exited)
        return snapshot

    def make_rows(self, value=0.5):
        rows = []
        for index, seed in enumerate(self.cfg['required_seeds']):
            model = self.manifest['models'][index]['file']['sha256']
            checkpoint = self.manifest['checkpoints'][index]['file']['sha256']
            evaluation_pid, reload_pid = 100 + index, 200 + index
            longrun.atomic_json(self.evidence / f'result-{seed}.json', {'accuracy': value,
                'model_sha256': model, 'checkpoint_sha256': checkpoint, 'evaluation_pid': evaluation_pid})
            longrun.atomic_json(self.evidence / f'reload-{seed}.json', {'accuracy': value,
                'model_sha256': model, 'checkpoint_sha256': checkpoint, 'reload_pid': reload_pid})
            rows.append({'seed': seed, 'score': value, 'result_file': c.artifact(self.evidence, f'result-{seed}.json'),
                         'reload': {'score': value, 'model_hash': model, 'checkpoint_hash': checkpoint,
                                    'evaluation_pid': evaluation_pid, 'reload_pid': reload_pid,
                                    'file': c.artifact(self.evidence, f'reload-{seed}.json')}})
        return rows

    def seal(self, value=.5):
        c.write_score_result(self.contract, value, self.rows)
        return c.issue_receipt(self.contract)

    def controller_state(self):
        root = self.base / 'controller-state'
        controller.init_run(None, root, 'test', config=self.cfg)
        self.run = root / 'runs/test'
        state = longrun.load_state(self.run)
        longrun.begin_run(state)
        state['budget']['active_seconds'] = state['budget']['window_seconds']
        # Test the same effective contract which a real first start freezes.
        self.contract = c.contract_for_run(self.cfg, 'test', state['budget']['hard_deadline_at'])
        longrun.atomic_json(self.run / 'completion.contract.json', self.contract)
        state['completion']['contract_hash'] = c.digest(self.contract)
        longrun.save_state(self.run, state)
        (self.evidence / 'evaluation.jobs.json').unlink()
        c.register_jobs(self.contract, self.jobs)
        self.attest()
        return state

    def test_all_config_contract_fields_are_required(self):
        for field in c.CONTRACT_FIELDS:
            with self.subTest(field=field):
                value = copy.deepcopy(self.cfg)
                del value[field]
                with self.assertRaises(longrun.ControllerError):
                    longrun.validate_config(value)

    def test_formal_and_final_cannot_disable_scores(self):
        for stage in ('screen', 'formal', 'final'):
            value = copy.deepcopy(self.cfg)
            value.update(stage=stage, score_expectation='not_expected', completion={})
            with self.assertRaises(longrun.ControllerError):
                longrun.validate_config(value)

    def test_zero_and_negative_scores_are_valid(self):
        for value in (0, -0.75):
            with self.subTest(score=value):
                (self.evidence / 'final.score.json').unlink(missing_ok=True)
                (self.evidence / 'completion.receipt.json').unlink(missing_ok=True)
                self.rows = self.make_rows(value)
                receipt = self.seal(value)
                self.assertEqual(receipt['scientific_score'], value)
                self.assertTrue(c.validate_receipt(self.contract, receipt)['ok'])

    def test_certified_experiment_can_be_selected_without_retraining(self):
        self.seal()
        parent = copy.deepcopy(self.contract)
        parent['run_id'] = 'parent'
        parent['candidate_manifest'] = 'selected.candidate.json'
        parent['completion'].update(jobs_manifest='selected.jobs.json', result='selected.score.json',
                                    receipt='selected.receipt.json', isolation='selected.isolation.json')
        with mock.patch.object(c, 'get_job', return_value=json.loads((self.job_dir / 'status.json').read_text())):
            receipt = c.adopt_evaluation(parent, self.contract)
            self.assertEqual(receipt['status'], 'COMPLETED')
            self.assertEqual(receipt['scientific_score'], .5)
            self.assertEqual(c.adopt_evaluation(parent, self.contract), receipt)
        ledger = json.loads((self.evidence / 'selected.jobs.json').read_text())
        self.assertEqual(ledger['jobs'], self.jobs)

    def test_restored_job_binds_original_session_and_rejects_changed_origin(self):
        snapshot = self.job_state('SUCCEEDED')
        snapshot.update(origin_session_id='session', session_id='new-session')
        longrun.atomic_json(self.job_dir / 'status.json', snapshot)
        with mock.patch.object(c, 'get_job', return_value=snapshot):
            observations = c.job_observations(self.contract, live=True)
        self.assertEqual(observations[0]['state'], 'SUCCEEDED')
        self.assertEqual(observations[0]['session_id'], 'session')
        with mock.patch.object(c, 'get_job', return_value={**snapshot, 'origin_session_id': 'different'}):
            self.assertEqual(c.job_observations(self.contract, live=True)[0]['state'], 'UNKNOWN')

    def test_rejected_selection_writes_nothing_and_can_be_retried(self):
        self.seal()
        parent = copy.deepcopy(self.contract)
        parent['run_id'] = 'parent'
        parent['candidate_manifest'] = 'selected.candidate.json'
        parent['completion'].update(jobs_manifest='selected.jobs.json', result='selected.score.json',
                                    receipt='selected.receipt.json', isolation='selected.isolation.json')
        conflicting = self.evidence / 'selected.isolation.json'
        longrun.atomic_json(conflicting, c.signed(parent, {'version': 1, 'conflict': True}))
        with mock.patch.object(c, 'get_job', return_value=json.loads((self.job_dir / 'status.json').read_text())):
            with self.assertRaisesRegex(longrun.ControllerError, 'isolation evidence is immutable'):
                c.adopt_evaluation(parent, self.contract)
            self.assertFalse((self.evidence / 'selected.jobs.json').exists())
            self.assertFalse((self.evidence / 'selected.candidate.json').exists())
            conflicting.unlink()
            self.assertEqual(c.adopt_evaluation(parent, self.contract)['status'], 'COMPLETED')

    def test_selection_rejects_other_protocol_and_unfinished_experiment(self):
        parent = copy.deepcopy(self.contract)
        parent['run_id'] = 'parent'
        parent['protocol_hash'] = 'f' * 64
        with self.assertRaises(longrun.ControllerError):
            c.adopt_evaluation(parent, self.contract)
        parent['protocol_hash'] = self.contract['protocol_hash']
        self.job_state('RUNNING')
        with mock.patch.object(c, 'get_job', return_value=json.loads((self.job_dir / 'status.json').read_text())):
            with self.assertRaises(longrun.ControllerError):
                c.adopt_evaluation(parent, self.contract)

    def test_nonfinite_and_boolean_scores_are_rejected(self):
        for value in (None, True, float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(c.EvidenceError):
                c.write_score_result(self.contract, value, self.rows)

    def test_succeeded_job_without_result_is_invalid(self):
        verdict = c.inspect_evaluation(self.contract)
        self.assertEqual(verdict['status'], 'FINAL_SCORE_INVALID')
        self.assertEqual(verdict['issues'][0]['code'], 'RESULT_MISSING')
        self.assertFalse((self.evidence / 'completion.receipt.json').exists())

    def test_succeeded_job_without_executor_exit_is_unknown(self):
        (self.job_dir / 'exit.json').unlink()
        self.assertEqual(c.inspect_evaluation(self.contract)['status'], 'EVALUATION_UNKNOWN')

    def test_queue_uses_original_get_only(self):
        snapshot = self.job_state('QUEUED')
        with mock.patch.object(c, 'get_job', return_value=snapshot) as get:
            receipt = c.issue_receipt(self.contract, live=True)
        self.assertEqual(receipt['status'], 'EVALUATION_PENDING')
        self.assertEqual(get.call_args.args[0]['request_id'], 'test/eval/original')
        self.assertEqual(get.call_args.args[0]['job_id'], 'job')
        self.assertFalse((self.evidence / 'completion.receipt.json').exists())

    def test_disconnected_pending_job_is_unknown(self):
        self.job_state('QUEUED')
        with mock.patch.object(c, 'get_job', side_effect=OSError('disconnected')):
            self.assertEqual(c.inspect_evaluation(self.contract, live=True)['status'], 'EVALUATION_UNKNOWN')

    def test_live_query_without_snapshot_still_queries_original_job(self):
        snapshot = self.job_state('RUNNING')
        (self.job_dir / 'status.json').unlink()
        with mock.patch.object(c, 'get_job', return_value=snapshot) as get:
            verdict = c.inspect_evaluation(self.contract, live=True)
        self.assertEqual(verdict['status'], 'EVALUATION_PENDING')
        self.assertEqual(get.call_count, 1)

    def test_failed_job_does_not_become_completed(self):
        self.job_state('FAILED')
        self.assertEqual(c.inspect_evaluation(self.contract)['status'], 'EVALUATION_FAILED')

    def test_candidate_changed_after_scoring_is_detected(self):
        receipt = self.seal()
        (self.public / 'method.py').write_text('changed source\n')
        self.assertEqual(c.validate_receipt(self.contract, receipt)['status'], 'CANDIDATE_BINDING_MISMATCH')

    def test_checkpoint_changed_after_scoring_is_detected(self):
        receipt = self.seal()
        (self.public / 'checkpoint-1.pt').write_bytes(b'changed checkpoint')
        self.assertEqual(c.validate_receipt(self.contract, receipt)['status'], 'CANDIDATE_BINDING_MISMATCH')

    def test_protocol_or_hidden_data_changed_is_detected(self):
        receipt = self.seal()
        (self.evidence / 'data.bin').write_bytes(b'changed hidden data')
        self.assertEqual(c.validate_receipt(self.contract, receipt)['status'], 'PROTOCOL_BINDING_MISMATCH')

    def test_one_formal_seed_missing_cannot_seal(self):
        with self.assertRaises(c.EvidenceError) as error:
            c.write_score_result(self.contract, .5, self.rows[:-1])
        self.assertEqual(error.exception.status, 'INCOMPLETE_FINAL_SCORE')

    def test_missing_seed_in_original_jobs_is_rejected(self):
        jobs = copy.deepcopy(self.jobs)
        jobs[0]['seeds'] = ['0', '1']
        with self.assertRaises(c.EvidenceError):
            c.register_jobs(self.contract, jobs)

    def test_reload_must_be_real_nonempty_and_in_a_separate_process(self):
        for failure in ('missing', 'empty', 'same_process', 'wrong_hash'):
            with self.subTest(failure=failure):
                rows = copy.deepcopy(self.rows)
                if failure == 'missing':
                    rows[0].pop('reload')
                elif failure == 'empty':
                    (self.evidence / 'reload-0.json').write_text('')
                elif failure == 'same_process':
                    rows[0]['reload']['reload_pid'] = rows[0]['reload']['evaluation_pid']
                else:
                    rows[0]['reload']['checkpoint_hash'] = '0' * 64
                with self.assertRaises(c.EvidenceError):
                    c.write_score_result(self.contract, .5, rows)
                self.rows = self.make_rows()

    def test_signed_receipt_and_signed_result_cannot_be_forged(self):
        receipt = self.seal()
        for value in (dict(receipt, scientific_score=99), {k: v for k, v in receipt.items() if k != 'signature'}):
            self.assertFalse(c.validate_receipt(self.contract, value)['ok'])
        path = self.evidence / 'final.score.json'
        result = longrun.read_json(path)
        result['scientific_score'] = 99
        longrun.atomic_json(path, result)
        self.assertFalse(c.validate_receipt(self.contract, receipt)['ok'])

    def test_sealing_and_job_registration_are_idempotent_and_immutable(self):
        receipt = self.seal()
        before = (self.evidence / 'completion.receipt.json').read_bytes()
        self.assertEqual(c.issue_receipt(self.contract), receipt)
        c.register_jobs(self.contract, self.jobs)
        self.assertEqual((self.evidence / 'completion.receipt.json').read_bytes(), before)
        changed = copy.deepcopy(self.jobs)
        changed[0]['job_id'] = 'replacement'
        with self.assertRaises(longrun.ControllerError):
            c.register_jobs(self.contract, changed)

    def test_raw_seed_score_must_equal_bound_result_file(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['score'] = .9
        with self.assertRaises(c.EvidenceError):
            c.write_score_result(self.contract, sum(v['score'] for v in rows) / 3, rows)

    def test_receipt_records_original_deadline_and_rejects_late_results(self):
        c.write_score_result(self.contract, .5, self.rows)
        receipt = c.issue_receipt(self.contract, now=iso(120))
        self.assertEqual(receipt['status'], 'INCOMPLETE_FINAL_SCORE')
        self.assertEqual(receipt['original_deadline'], self.contract['deadline'])
        self.assertFalse((self.evidence / 'completion.receipt.json').exists())

    def test_late_observations_are_archived_without_replacing_the_original(self):
        self.job_state('QUEUED')
        first = c.issue_receipt(self.contract, now=iso(120))
        archive = list((self.evidence / 'completion.late').glob('*.json'))
        self.assertEqual(len(archive), 1)
        original = archive[0].read_bytes()
        self.assertEqual(first['observed_evaluation']['status'], 'EVALUATION_PENDING')
        self.job_state('SUCCEEDED')
        c.write_score_result(self.contract, .5, self.rows)
        second = c.issue_receipt(self.contract, now=iso(121))
        self.assertEqual(second['status'], 'INCOMPLETE_FINAL_SCORE')
        self.assertEqual(second['observed_evaluation']['status'], 'COMPLETED')
        self.assertEqual(archive[0].read_bytes(), original)
        self.assertEqual(len(list((self.evidence / 'completion.late').glob('*.json'))), 2)
        self.assertFalse((self.evidence / 'completion.receipt.json').exists())

    def test_late_scoring_job_is_not_a_final_score(self):
        c.write_score_result(self.contract, .5, self.rows)
        snapshot = self.job_state('SUCCEEDED')
        snapshot['finished_at'] = c.timestamp(self.contract['deadline']) + 1
        longrun.atomic_json(self.job_dir / 'status.json', snapshot)
        self.assertEqual(c.inspect_evaluation(self.contract)['status'], 'INCOMPLETE_FINAL_SCORE')

    def test_atomic_json_fsyncs_and_preserves_original_on_rename_failure(self):
        path = self.evidence / 'atomic.json'
        longrun.atomic_json(path, {'previous': True})
        with mock.patch.object(longrun.os, 'replace', side_effect=OSError('crash before rename')):
            with self.assertRaises(OSError):
                longrun.atomic_json(path, {'partial': True})
        self.assertEqual(longrun.read_json(path), {'previous': True})
        self.assertEqual(list(self.evidence.glob('.atomic.json.*')), [])
        with mock.patch.object(longrun.os, 'fsync', wraps=os.fsync) as synced:
            longrun.atomic_json(path, {'complete': True})
        self.assertGreaterEqual(synced.call_count, 2)

    def test_directory_fsync_failure_is_not_reported_as_durable_success(self):
        path = self.evidence / 'fsync-failure.json'
        real_fsync = os.fsync
        calls = []
        def fail_directory(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('directory sync failed')
            real_fsync(fd)
        with mock.patch.object(longrun.os, 'fsync', side_effect=fail_directory):
            with self.assertRaisesRegex(OSError, 'directory sync failed'):
                longrun.atomic_json(path, {'complete': True})
        self.assertEqual(longrun.read_json(path), {'complete': True})
        self.assertEqual(list(self.evidence.glob('.fsync-failure.json.*')), [])

    def test_private_key_and_evidence_cannot_be_in_candidate_root(self):
        for field in ('evidence_root', 'signing_key'):
            value = copy.deepcopy(self.contract)
            value['completion'][field] = str(self.public / 'secret')
            with self.assertRaises((OSError, longrun.ControllerError)):
                c.trust_boundary(value)
        self.key.chmod(0o644)
        with self.assertRaises(longrun.ControllerError):
            c.trust_boundary(self.contract)

    def test_symlinked_scientific_evidence_is_rejected(self):
        (self.evidence / 'result-0.json').unlink()
        (self.evidence / 'result-0.json').symlink_to(self.public / 'method.py')
        with self.assertRaises(c.EvidenceError):
            c.write_score_result(self.contract, .5, self.rows)

    def test_isolation_rejects_hidden_mounts_privileges_and_verifier_log_mounts(self):
        for modification in (
            {'Mounts': [{'Source': str(self.evidence), 'Destination': '/hidden', 'RW': False}]},
            {'Mounts': [{'Source': str(self.base / 'logs'), 'Destination': '/logs/verifier', 'RW': False}]},
            {'Mounts': [{'Source': str(self.base / 'reward'), 'Destination': '/logs/verifier/reward.txt', 'RW': True}]},
            {'HostConfig': {'Privileged': True}},
            {'HostConfig': {'CapAdd': ['SYS_ADMIN']}},
            {'Image': 'sha256:' + 'c' * 64},
            {'Config': {'Env': ['PRIVATE_KEY=' + self.key.read_bytes().hex()]}},
        ):
            item = {'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64, 'HostConfig': {}, 'Config': {'Env': []}, 'Mounts': []}
            item.update(modification)
            with self.subTest(modification=list(modification)), self.assertRaises(longrun.ControllerError):
                self.attest([item])

    def test_receipt_does_not_copy_private_paths_keys_or_raw_verifier_logs(self):
        receipt = self.seal()
        text = json.dumps(receipt)
        for secret in (str(self.key), str(self.evidence), self.key.read_bytes().hex(), 'evaluator-v1', 'data-v1'):
            self.assertNotIn(secret, text)

    def test_controller_worker_success_without_scientific_result_fails(self):
        state = self.controller_state()
        code = controller.LongRunController(self.run, with_guard=False).run()
        self.assertEqual(code, 4)
        self.assertEqual(longrun.load_state(self.run)['status'], 'FINAL_SCORE_INVALID')
        self.assertEqual(longrun.load_state(self.run)['turn']['number'], 0)

    def test_controller_receipt_write_crash_is_recoverable_even_after_deadline(self):
        state = self.controller_state()
        state['completion']['phase'] = 'FINALIZING'
        state['status'] = 'FINALIZING'
        longrun.save_state(self.run, state)
        self.seal()
        future = c.timestamp(self.contract['deadline']) + 10
        with mock.patch.object(time, 'time', return_value=future):
            recovered = controller.recover_run(self.run)
        self.assertEqual(recovered['status'], 'COMPLETED')
        self.assertEqual(longrun.read_json(self.run / 'exit.json')['exit_code'], 0)
        self.assertTrue(audit.audit_run(self.run)['ok'])

    def test_repeated_controller_finalization_does_not_query_or_submit_again(self):
        state = self.controller_state()
        self.seal()
        self.assertEqual(controller.completion_step(self.run, state, self.cfg), 'COMPLETED')
        before = (self.run / 'completion.decision.json').read_bytes()
        with mock.patch.object(c, 'get_job', side_effect=AssertionError('no repeated scheduling query')):
            self.assertEqual(controller.completion_step(self.run, state, self.cfg, live=True), 'COMPLETED')
        self.assertEqual((self.run / 'completion.decision.json').read_bytes(), before)

    def test_original_deadline_queue_expires_and_late_receipt_does_not_promote(self):
        state = self.controller_state()
        self.job_state('QUEUED')
        future = c.timestamp(self.contract['deadline']) + 1
        with mock.patch.object(time, 'time', return_value=future):
            self.assertEqual(controller.completion_step(self.run, state, self.cfg), 'INCOMPLETE_FINAL_SCORE')
        before = (self.run / 'completion.decision.json').read_bytes()
        self.job_state('SUCCEEDED')
        # A later file, even validly signed, is only extra evidence for a terminal run.
        self.seal()
        self.assertEqual(controller.completion_step(self.run, state, self.cfg), 'INCOMPLETE_FINAL_SCORE')
        self.assertEqual((self.run / 'completion.decision.json').read_bytes(), before)
        self.assertFalse(audit.audit_run(self.run)['ok'])

    def test_guarded_controller_waiting_for_score_obeys_the_original_deadline(self):
        self.cfg['budget']['hard_limit_seconds'] = .7
        self.cfg['turn']['seconds'] = .5
        state = self.controller_state()
        deadline = state['budget']['hard_deadline_at']
        snapshot = self.job_state('QUEUED')
        with mock.patch.object(c, 'get_job', return_value=snapshot):
            code = controller.LongRunController(self.run, with_guard=True).run()
        final = longrun.load_state(self.run)
        self.assertEqual(code, 4)
        self.assertEqual(final['status'], 'INCOMPLETE_FINAL_SCORE')
        self.assertEqual(final['budget']['hard_deadline_at'], deadline)
        self.assertEqual(final['turn']['number'], 0)
        self.assertIsNone(final['guard_pid'])

    def test_valid_receipt_remains_idempotent_after_deadline(self):
        receipt = self.seal()
        future = c.timestamp(self.contract['deadline']) + 10
        with mock.patch.object(time, 'time', return_value=future):
            self.assertEqual(c.issue_receipt(self.contract), receipt)

    def test_controller_waits_for_original_queue_with_heartbeat_and_no_extra_turns(self):
        state = self.controller_state()
        c.write_score_result(self.contract, .5, self.rows)
        self.job_state('QUEUED')
        calls = []
        def advance(job, **kwargs):
            calls.append((job['job_id'], job['request_id']))
            if len(calls) == 2:
                self.job_state('SUCCEEDED')
            return longrun.read_json(self.job_dir / 'status.json')
        with mock.patch.object(c, 'get_job', side_effect=advance):
            result = controller.LongRunController(self.run, with_guard=False).run()
        self.assertEqual(result, 0)
        final = longrun.load_state(self.run)
        self.assertEqual(final['status'], 'COMPLETED')
        self.assertEqual(final['turn']['number'], 0)
        self.assertIsNotNone(final.get('controller_heartbeat_monotonic'))
        self.assertEqual(set(calls), {('job', 'test/eval/original')})
        events = (self.run / 'events.jsonl').read_text()
        self.assertIn('run.finalizing', events)
        self.assertIn('EVALUATION_PENDING', events)

    def test_batch_inside_running_parent_only_issues_pending_receipt(self):
        snapshot = self.job_state('RUNNING')
        with mock.patch.object(c, 'get_job', return_value=snapshot):
            receipt = experiment_batch.complete_batch(self.contract, self.rows)
        self.assertEqual(receipt['status'], 'EVALUATION_PENDING')
        self.assertTrue((self.evidence / 'completion.pending.json').is_file())
        self.assertFalse((self.evidence / 'completion.receipt.json').exists())

    def test_get_queries_the_original_session_over_a_real_unix_socket(self):
        directory_fd = os.open(self.queue, os.O_RDONLY | os.O_DIRECTORY)
        address = f'/proc/self/fd/{directory_fd}/scheduler.sock'
        observed = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(address)
            server.listen(1)
            server.settimeout(3)
            def respond():
                with server.accept()[0] as connection:
                    request = json.loads(connection.makefile('rb').readline())
                    observed.append(request)
                    connection.sendall(c.canonical({'ok': True, 'result': {'state': 'QUEUED'}}) + b'\n')
            thread = threading.Thread(target=respond)
            thread.start()
            try:
                job = dict(self.jobs[0], scheduler_root=f'/proc/self/fd/{directory_fd}')
                self.assertEqual(c.get_job(job)['state'], 'QUEUED')
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(observed, [{'op': 'get', 'session_id': 'session', 'id': 'job',
                                             'request_id': 'test/eval/original'}])
            finally:
                os.close(directory_fd)

    def test_scheduler_symlinks_and_changed_terminal_identity_are_rejected(self):
        original = self.job_dir / 'status.json'
        backup = self.evidence / 'terminal-copy.json'
        backup.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(backup)
        self.assertEqual(c.inspect_evaluation(self.contract)['status'], 'EVALUATION_UNKNOWN')
        original.unlink()
        snapshot = self.job_state('SUCCEEDED')
        snapshot['request_id'] = 'other/request'
        longrun.atomic_json(original, snapshot)
        self.assertEqual(c.inspect_evaluation(self.contract)['status'], 'EVALUATION_UNKNOWN')

    def test_scheduler_and_key_directories_cannot_be_mounted(self):
        for path in (self.queue, self.keys):
            item = {'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64, 'HostConfig': {},
                    'Config': {'Env': []}, 'Mounts': [{'Source': str(path), 'Destination': '/evidence', 'RW': False}]}
            with self.assertRaises(longrun.ControllerError):
                self.attest([item])

    def test_controller_signing_failure_is_a_completion_failure(self):
        state = self.controller_state()
        self.key.chmod(0o644)
        self.assertEqual(controller.completion_step(self.run, state, self.cfg), 'COMPLETION_RECEIPT_MISSING')
        self.assertTrue((self.run / 'completion.decision.json').is_file())

    def test_controller_rejects_a_symlink_receipt_with_an_explicit_failure(self):
        state = self.controller_state()
        external = self.base / 'untrusted.json'
        external.write_text('{}\n')
        (self.evidence / 'completion.receipt.json').symlink_to(external)
        self.assertEqual(controller.completion_step(self.run, state, self.cfg), 'COMPLETION_RECEIPT_MISSING')
        self.assertEqual(state['completion']['issues'][0]['code'], 'RECEIPT_INVALID')

    def test_frozen_contract_change_has_an_explicit_terminal_failure(self):
        state = self.controller_state()
        changed = dict(self.contract, metric='different')
        longrun.atomic_json(self.run / 'completion.contract.json', changed)
        self.assertEqual(controller.completion_step(self.run, state, self.cfg), 'PROTOCOL_BINDING_MISMATCH')

    def test_malformed_contract_and_seed_types_fail_closed(self):
        for field in ('stage', 'required_seeds', 'direction', 'completion'):
            with self.subTest(field=field):
                malformed = copy.deepcopy(self.contract)
                malformed[field] = [{}]
                self.assertFalse(c.validate_receipt(malformed, {})['ok'])
        for malformed in ({}, [], None, True):
            rows = copy.deepcopy(self.rows)
            rows[0]['seed'] = malformed
            with self.assertRaises(c.EvidenceError):
                c.write_score_result(self.contract, .5, rows)

    def harbor_evidence(self, trial='trial-one', reward=.5):
        folder = self.evidence / trial
        (folder / 'verifier').mkdir(parents=True)
        longrun.atomic_json(folder / 'config.json', {'trial_id': trial, 'task': {'name': 'completion-test'}})
        longrun.atomic_json(folder / 'result.json', {'trial_id': trial, 'exception_info': None,
            'verifier_result': {'rewards': {'accuracy': reward}}})
        longrun.atomic_json(folder / 'verifier/reward.json', {'accuracy': reward})
        (folder / 'trial.log').write_text('trusted verifier executed for this Trial\n')
        return {'trial_id': trial, **{name: c.artifact(self.evidence, f'{trial}/{file}') for name, file in
            (('config', 'config.json'), ('result', 'result.json'), ('reward', 'verifier/reward.json'), ('log', 'trial.log'))}}

    def expanded_harbor_evidence(self, trial='task__compact', reward=.5):
        """A real Harbor-shaped compact config plus default-expanded result config."""
        trial_id = '35766f6c-fe94-42b2-b4c6-62d740c198d1'
        folder = self.evidence / trial
        (folder / 'verifier').mkdir(parents=True)
        config = {
            'task': {'path': '/remote/task'}, 'trial_name': trial,
            'trials_dir': '/remote/evaluations/formal-000001',
            'agent': {'import_path': 'scored_environment:ScoringNop'},
            'environment': {'import_path': 'scored_environment:ScoredDockerEnvironment'},
            'job_id': 'ec86fd3b-9bcf-4e10-b564-efba828d01a5',
        }
        expanded = {
            'task': {'path': '/remote/task', 'git_url': None, 'git_commit_id': None,
                     'name': None, 'ref': None, 'overwrite': False, 'download_dir': None, 'source': None},
            'trial_name': trial, 'trials_dir': config['trials_dir'], 'install_only': False,
            'agent': {'name': None, 'import_path': config['agent']['import_path'], 'skills': [], 'kwargs': {}},
            'environment': {'import_path': config['environment']['import_path'], 'force_build': False,
                            'kwargs': {}}, 'job_id': config['job_id'],
        }
        longrun.atomic_json(folder / 'config.json', config)
        longrun.atomic_json(folder / 'result.json', {
            'id': trial_id, 'trial_name': trial,
            'trial_uri': f'file:///remote/evaluations/formal-000001/{trial}',
            'config': expanded, 'exception_info': None,
            'verifier_result': {'rewards': {'accuracy': reward}},
        })
        longrun.atomic_json(folder / 'verifier/reward.json', {'accuracy': reward})
        (folder / 'trial.log').write_text('trusted verifier executed for this Trial\n')
        return {'trial_id': trial_id, **{name: c.artifact(self.evidence, f'{trial}/{file}') for name, file in
            (('config', 'config.json'), ('result', 'result.json'), ('reward', 'verifier/reward.json'), ('log', 'trial.log'))}}

    def bind_seed_trial(self, trial):
        for row in self.rows:
            path = c.within(self.evidence, row['result_file']['path'])
            raw = longrun.read_json(path)
            raw['trial_id'] = trial
            longrun.atomic_json(path, raw)
            row['result_file'] = c.artifact(self.evidence, row['result_file']['path'])

    def test_harbor_same_trial_zero_and_negative_rewards_are_valid(self):
        for value in (0, -.5):
            trial = 'trial-zero' if value == 0 else 'trial-negative'
            self.rows = self.make_rows(value)
            self.bind_seed_trial(trial)
            harbor = self.harbor_evidence(trial, value)
            (self.evidence / 'final.score.json').unlink(missing_ok=True)
            (self.evidence / 'completion.receipt.json').unlink(missing_ok=True)
            c.write_score_result(self.contract, value, self.rows, harbor=harbor)
            self.assertTrue(c.validate_receipt(self.contract, c.issue_receipt(self.contract))['ok'])

    def test_harbor_accepts_compact_config_and_expanded_result_config(self):
        trial = 'task__compact'
        self.rows = self.make_rows(.5)
        self.bind_seed_trial('35766f6c-fe94-42b2-b4c6-62d740c198d1')
        harbor = self.expanded_harbor_evidence(trial, .5)
        c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        self.assertTrue(c.validate_receipt(self.contract, c.issue_receipt(self.contract))['ok'])

    def test_harbor_accepts_renamed_archive_of_the_same_trial(self):
        harbor = self.expanded_harbor_evidence()
        (self.evidence / 'experiments/000001').mkdir(parents=True)
        shutil.copytree(self.evidence / 'task__compact', self.evidence / 'experiments/000001/harbor-trial')
        for name in ('config', 'result', 'reward', 'log'):
            relative = Path(harbor[name]['path']).relative_to('task__compact')
            harbor[name] = c.artifact(self.evidence, str(Path('experiments/000001/harbor-trial') / relative))
        self.bind_seed_trial(harbor['trial_id'])
        c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        self.assertTrue(c.validate_receipt(self.contract, c.issue_receipt(self.contract))['ok'])

    def test_harbor_rejects_missing_or_changed_expanded_settings(self):
        harbor = self.expanded_harbor_evidence()
        path = self.evidence / 'task__compact/result.json'
        original = longrun.read_json(path)
        mutations = [
            ('config', None), ('config', []), ('id', None), ('id', 1),
            ('trial_name', 'task__other'), ('trial_uri', None),
            ('trial_uri', 'https://remote/formal-000001/task__compact'),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                result = copy.deepcopy(original)
                result[field] = value
                longrun.atomic_json(path, result)
                harbor['result'] = c.artifact(self.evidence, 'task__compact/result.json')
                with self.assertRaises(c.EvidenceError) as error:
                    c.validate_harbor(self.contract, harbor, .5)
                self.assertEqual(error.exception.code, 'HARBOR_TRIAL_MISMATCH')
        for field in ('task', 'agent', 'environment', 'job_id', 'trial_name', 'trials_dir'):
            for value in (None, [], 'different'):
                with self.subTest(config_field=field, value=value):
                    result = copy.deepcopy(original)
                    result['config'][field] = value
                    longrun.atomic_json(path, result)
                    harbor['result'] = c.artifact(self.evidence, 'task__compact/result.json')
                    with self.assertRaises(c.EvidenceError) as error:
                        c.validate_harbor(self.contract, harbor, .5)
                    self.assertEqual(error.exception.code, 'HARBOR_TRIAL_MISMATCH')
            result = copy.deepcopy(original)
            del result['config'][field]
            longrun.atomic_json(path, result)
            harbor['result'] = c.artifact(self.evidence, 'task__compact/result.json')
            with self.assertRaises(c.EvidenceError):
                c.validate_harbor(self.contract, harbor, .5)

    def test_harbor_rejects_changed_candidate_in_expanded_kwargs(self):
        harbor = self.expanded_harbor_evidence()
        config_path = self.evidence / 'task__compact/config.json'
        result_path = self.evidence / 'task__compact/result.json'
        config = longrun.read_json(config_path)
        config['agent']['kwargs'] = {'candidate_source': '/remote/candidate/method.py'}
        longrun.atomic_json(config_path, config)
        result = longrun.read_json(result_path)
        result['config']['agent']['kwargs'] = {'candidate_source': '/remote/other/method.py'}
        longrun.atomic_json(result_path, result)
        harbor['config'] = c.artifact(self.evidence, 'task__compact/config.json')
        harbor['result'] = c.artifact(self.evidence, 'task__compact/result.json')
        with self.assertRaises(c.EvidenceError) as error:
            c.validate_harbor(self.contract, harbor, .5)
        self.assertEqual(error.exception.code, 'HARBOR_TRIAL_MISMATCH')

    def test_harbor_rejects_wrong_structured_trial_identity_or_exit(self):
        self.rows = self.make_rows(.5)
        trial_id = '35766f6c-fe94-42b2-b4c6-62d740c198d1'
        self.bind_seed_trial(trial_id)
        harbor = self.expanded_harbor_evidence(reward=.5)
        result_path = self.evidence / 'task__compact/result.json'
        result = longrun.read_json(result_path)
        result['trial_uri'] = 'file:///remote/evaluations/formal-000001/task__other'
        longrun.atomic_json(result_path, result)
        harbor['result'] = c.artifact(self.evidence, 'task__compact/result.json')
        with self.assertRaises(c.EvidenceError) as error:
            c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        self.assertEqual(error.exception.code, 'HARBOR_TRIAL_MISMATCH')

        result['trial_uri'] = 'file:///remote/evaluations/formal-000001/task__compact'
        result['exception_info'] = {'type': 'RuntimeError'}
        longrun.atomic_json(result_path, result)
        harbor['result'] = c.artifact(self.evidence, 'task__compact/result.json')
        with self.assertRaises(c.EvidenceError) as error:
            c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        self.assertEqual(error.exception.code, 'HARBOR_TRIAL_MISMATCH')

    def test_harbor_rejects_mixed_trials_and_unbound_scientific_score(self):
        harbor = self.harbor_evidence()
        with self.assertRaises(c.EvidenceError):
            c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        self.bind_seed_trial('trial-one')
        other = self.harbor_evidence('trial-two')
        harbor['reward'] = other['reward']
        with self.assertRaises(c.EvidenceError) as error:
            c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        self.assertEqual(error.exception.code, 'HARBOR_TRIAL_MISMATCH')

    def test_harbor_checks_the_actual_priority_reward_file(self):
        self.bind_seed_trial('trial-one')
        harbor = self.harbor_evidence()
        (self.evidence / 'trial-one/verifier/reward.txt').write_text('99\n')
        with self.assertRaises(c.EvidenceError) as error:
            c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        self.assertEqual(error.exception.code, 'HARBOR_REWARD_PRIORITY')
        harbor['reward_priority'] = ['reward.txt', 'reward.json']
        with self.assertRaises(c.EvidenceError):
            c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
        harbor['reward_priority'] = ['reward.json', 'reward.txt']
        c.write_score_result(self.contract, .5, self.rows, harbor=harbor)

    def test_harbor_rejects_malformed_verifier_and_different_reward_metric(self):
        self.bind_seed_trial('trial-one')
        harbor = self.harbor_evidence()
        path = self.evidence / 'trial-one/result.json'
        value = longrun.read_json(path)
        for verifier in (None, [], {'rewards': {'wrong_metric': .5}}):
            value['verifier_result'] = verifier
            longrun.atomic_json(path, value)
            harbor['result'] = c.artifact(self.evidence, 'trial-one/result.json')
            with self.assertRaises(c.EvidenceError) as error:
                c.write_score_result(self.contract, .5, self.rows, harbor=harbor)
            self.assertEqual(error.exception.code, 'HARBOR_REWARD_MISMATCH')

    def task_adapter(self):
        path = ROOT / 'tests/fixtures/batch_adapter.py'
        spec = importlib.util.spec_from_file_location('completion_batch_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_batch_adapter_requires_contract_before_any_work(self):
        adapter = self.task_adapter()
        with mock.patch.object(adapter.subprocess, 'Popen', side_effect=AssertionError('must not launch')):
            with self.assertRaises(ValueError):
                adapter.load_completion({'controller_release': str(ROOT)})
        contract_path = self.evidence / 'contract.json'
        longrun.atomic_json(contract_path, self.contract)
        config = {name: self.contract[name] for name in c.CONTRACT_FIELDS}
        config.update(controller_release=str(ROOT), completion_contract=str(contract_path),
                      completion_variant='candidate', jobs=[{'variant': 'candidate', 'seed': '0'}])
        with self.assertRaises(ValueError):
            adapter.load_completion(config)

    def test_batch_adapter_seals_process_records_and_private_models(self):
        adapter = self.task_adapter()
        self.manifest.update(models=[], checkpoints=[])
        longrun.atomic_json(self.evidence / 'candidate.manifest.json', self.manifest)
        jobs = []
        for index, seed in enumerate(self.contract['required_seeds']):
            output = self.evidence / f'candidate-seed-{seed}'
            (output / 'train').mkdir(parents=True)
            (output / 'reload').mkdir()
            (output / 'model.pt').write_bytes(f'private-model-{seed}'.encode())
            model_hash = c.file_hash(output / 'model.pt')
            longrun.atomic_json(output / 'result.json', {'accuracy': .5, 'model_sha256': model_hash})
            longrun.atomic_json(output / 'reload.log', {'accuracy': .5, 'model_sha256': model_hash})
            longrun.atomic_json(output / 'train/process.json', {'pid': 101 + index, 'returncode': 0})
            longrun.atomic_json(output / 'reload/process.json', {'pid': 201 + index, 'returncode': 0})
            jobs.append({'variant': 'candidate', 'seed': seed, 'output': str(output)})
        receipt = adapter.complete_scientific_batch(c, self.contract, {'completion_variant': 'candidate', 'jobs': jobs}, {})
        self.assertEqual(receipt['status'], 'COMPLETED')
        self.assertTrue(c.validate_receipt(self.contract, receipt)['ok'])
        self.assertEqual(receipt['bindings']['models'][0]['origin'], 'evidence')

    def test_formal_cpu_models_are_saved_and_reloaded_in_new_processes(self):
        state = self.controller_state()
        script = '''import hashlib,json,os,sys
from pathlib import Path
root,seed,phase=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
model=root/('cpu-model-'+seed+'.json')
if phase=='train':
    samples=[(-3,0),(-1,0),(1,1),(3,1)]
    centers=[sum(x for x,y in samples if y==label)/2 for label in (0,1)]
    model.write_text(json.dumps({'centers':centers,'seed':seed}))
centers=json.loads(model.read_text())['centers']
examples=[(-2,0),(-0.5,0),(0.5,1),(2,1)]
accuracy=sum(min((abs(x-c),label) for label,c in enumerate(centers))[1]==y for x,y in examples)/len(examples)
h=hashlib.sha256(model.read_bytes()).hexdigest()
result={'accuracy':accuracy,'model_sha256':h,'checkpoint_sha256':h,('evaluation_pid' if phase=='train' else 'reload_pid'):os.getpid()}
(root/(phase+'-'+seed+'.json')).write_text(json.dumps(result))
'''
        models, rows = [], []
        for seed in self.contract['required_seeds']:
            for phase in ('train', 'reload'):
                subprocess.run([sys.executable, '-B', '-c', script, str(self.evidence), seed, phase],
                               check=True, capture_output=True, timeout=5)
            trained = longrun.read_json(self.evidence / f'train-{seed}.json')
            loaded = longrun.read_json(self.evidence / f'reload-{seed}.json')
            model = c.artifact(self.evidence, f'cpu-model-{seed}.json')
            models.append({'seed': seed, 'origin': 'evidence', 'file': model})
            rows.append({'seed': seed, 'score': trained['accuracy'],
                'result_file': c.artifact(self.evidence, f'train-{seed}.json'), 'reload': {
                    'score': loaded['accuracy'], 'model_hash': model['sha256'], 'checkpoint_hash': model['sha256'],
                    'evaluation_pid': trained['evaluation_pid'], 'reload_pid': loaded['reload_pid'],
                    'file': c.artifact(self.evidence, f'reload-{seed}.json')}})
        self.manifest.update(models=models, checkpoints=models)
        longrun.atomic_json(self.evidence / 'candidate.manifest.json', self.manifest)
        c.write_score_result(self.contract, 1, rows)
        self.assertEqual(controller.completion_step(self.run, state, self.cfg), 'COMPLETED')
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'research_completion.py'), 'audit', '--run-dir', str(self.run)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_audit_missing_receipt_and_null_required_score(self):
        self.controller_state()
        state = longrun.load_state(self.run)
        state.update(status='COMPLETED', scientific_score=None)
        longrun.save_state(self.run, state)
        value = audit.audit_run(self.run)
        codes = {v['code'] for v in value['issues']}
        self.assertFalse(value['ok'])
        self.assertIn('SCIENTIFIC_SCORE_NULL', codes)
        self.assertIn('COMPLETION_RECEIPT_MISSING', codes)
        self.assertIn('RESULT_MISSING', codes)

    def test_audit_cli_is_read_only_and_returns_nonzero_on_invalid_formal(self):
        self.controller_state()
        before = {str(p): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'research_completion.py'), 'audit',
                                 '--run-dir', str(self.run)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(json.loads(result.stdout)['read_only'])
        after = {str(p): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_audit_rejects_null_scores_in_completion_and_batch_summary(self):
        state = self.controller_state()
        self.seal()
        controller.completion_step(self.run, state, self.cfg)
        state['completion']['scientific_score'] = None
        longrun.save_state(self.run, state)
        self.assertIn('SCIENTIFIC_SCORE_NULL', {v['code'] for v in audit.audit_run(self.run)['issues']})
        state['completion']['scientific_score'] = .5
        longrun.save_state(self.run, state)
        longrun.atomic_json(self.run / 'batch_summary.json', {'scientific_score': None})
        self.assertIn('SCIENTIFIC_SCORE_NULL', {v['code'] for v in audit.audit_run(self.run)['issues']})

    def test_audit_reports_malformed_state_without_a_traceback(self):
        root = self.base / 'malformed-state'
        root.mkdir()
        longrun.atomic_json(root / 'state.json', ['invalid'])
        self.assertEqual(audit.audit_run(root)['issues'][0]['code'], 'AUDIT_INPUT_INVALID')

    def test_diagnostic_explicit_not_expected_is_legal(self):
        config = copy.deepcopy(self.cfg)
        config.update(stage='diagnostic', score_expectation='not_expected', completion={}, required_seeds=[])
        contract = c.contract_for_run(config, 'diagnostic')
        receipt = c.diagnostic_receipt(contract)
        self.assertTrue(c.validate_receipt(contract, receipt)['ok'])
        self.assertNotIn('scientific_score', receipt)
        root = self.base / 'diagnostic-run'
        root.mkdir()
        longrun.atomic_json(root / 'config.json', config)
        longrun.atomic_json(root / 'state.json', {'run_id': 'diagnostic', 'status': 'COMPLETED'})
        longrun.atomic_json(root / 'completion.receipt.json', receipt)
        self.assertTrue(audit.audit_run(root)['ok'])

    def test_diagnostic_audit_cli_passes_without_a_scientific_score(self):
        self.cfg.update(stage='diagnostic', score_expectation='not_expected', completion={}, required_seeds=[])
        controller.init_run(None, self.base / 'diagnostic-state', 'diagnostic', config=self.cfg)
        run = self.base / 'diagnostic-state/runs/diagnostic'
        state = longrun.load_state(run)
        longrun.begin_run(state)
        state['budget']['active_seconds'] = state['budget']['window_seconds']
        contract = c.contract_for_run(self.cfg, 'diagnostic', state['budget']['hard_deadline_at'])
        longrun.atomic_json(run / 'completion.contract.json', contract)
        state['completion']['contract_hash'] = c.digest(contract)
        longrun.save_state(run, state)
        self.assertEqual(controller.LongRunController(run, with_guard=False).run(), 0)
        result = subprocess.run([sys.executable, '-B', '-m', 'tools.research_completion', 'audit', '--run-dir', str(run)],
                                cwd=ROOT.parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('scientific_score', longrun.read_json(run / 'completion.receipt.json'))

    def test_historical_completed_without_contract_fails_shadow_audit(self):
        root = self.base / 'historical-run'
        root.mkdir()
        longrun.atomic_json(root / 'config.json', {'version': 1, 'formal': True})
        longrun.atomic_json(root / 'state.json', {'run_id': 'old', 'status': 'COMPLETED', 'scientific_score': None})
        value = audit.audit_run(root)
        self.assertFalse(value['ok'])
        self.assertIn('COMPLETION_CONTRACT_MISSING', {v['code'] for v in value['issues']})


if __name__ == '__main__':
    unittest.main()
