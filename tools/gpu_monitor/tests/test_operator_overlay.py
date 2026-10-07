"""Official signed completion evidence stays confined to a transient TTY view."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
import unittest

WORKSPACE = Path(__file__).resolve().parents[3]
MONITOR_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MONITOR_ROOT))
import monitor
import operator_overlay
from privacy import public_record

_fixture_spec = importlib.util.spec_from_file_location(
    '_official_completion_fixtures', WORKSPACE / 'tools/research_handoff/tests/test_completion.py')
_fixtures = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixtures)


class OperatorOverlayTests(unittest.TestCase):
    SCORE = 0.246813579
    BASELINE = 0.135792468
    REFERENCE = 0.975318642

    def setUp(self):
        # Compose the official fixture instead of copying its signing logic or
        # inheriting its test methods. Its scratch area is removed by tearDown.
        self.fixture = _fixtures.CompletionTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.rows = self.fixture.make_rows(self.SCORE)
        self.receipt = self.fixture.seal(self.SCORE)
        self.contract_path = self.fixture.base / 'completion.contract.json'
        self.write_contract()
        self.task = {'id': 'registered', 'host': 'local', 'root': str(self.fixture.base),
                     'controller': {'type': 'research_handoff', 'run_id': 'test'},
                     'processes': [], 'uses_gpu': False,
                     'operator_overlay': {'task_id': 'completion-test',
                         'records': [{'contract_path': self.contract_path.name}]}}
        self.addCleanup(monitor._OPERATOR_OVERLAY.clear)

    def write_contract(self):
        self.contract_path.write_text(json.dumps(self.fixture.contract), encoding='utf-8')

    def overlay(self, task=None):
        return operator_overlay.collect_operator_overlay([task or self.task])['registered']

    def stage(self, stage):
        fixture = self.fixture
        fixture.cfg['stage'] = stage
        fixture.cfg['completion'].update(
            jobs_manifest=f'{stage}.jobs.json', result=f'{stage}.result.json',
            receipt=f'{stage}.receipt.json', isolation=f'{stage}.isolation.json')
        fixture.contract = _fixtures.c.contract_for_run(fixture.cfg, 'test')
        _fixtures.c.register_jobs(fixture.contract, fixture.jobs)
        fixture.attest()
        self.receipt = fixture.seal(self.SCORE)
        self.write_contract()

    def assert_unavailable(self, task=None):
        self.assertEqual(self.overlay(task), {'state': 'UNAVAILABLE'})

    def test_real_formal_and_final_receipts_are_verified(self):
        for stage in ('formal', 'final'):
            with self.subTest(stage=stage):
                if stage != 'formal':
                    self.stage(stage)
                self.assertEqual(self.overlay(), {'state': 'VERIFIED', 'best': self.SCORE,
                    'latest': self.SCORE, 'direction': 'max', 'trend': '=', 'unchanged': False})

    def test_diagnostic_screen_and_proxy_results_are_never_displayed(self):
        for stage in ('diagnostic', 'screen'):
            with self.subTest(stage=stage):
                self.stage(stage)
                self.assertTrue(_fixtures.c.validate_receipt(self.fixture.contract, self.receipt)['ok'])
                self.assert_unavailable()
        self.fixture.contract['stage'] = 'proxy'
        self.write_contract()
        self.assert_unavailable()

    def test_run_and_task_identity_must_match_registered_task(self):
        for key, value in [('run_id', 'another-run'), ('task_id', 'another-task')]:
            task = copy.deepcopy(self.task)
            target = task['controller'] if key == 'run_id' else task['operator_overlay']
            target[key] = value
            self.assert_unavailable(task)

    def test_signature_and_signed_receipt_identity_must_match(self):
        path = self.fixture.evidence / self.fixture.contract['completion']['receipt']
        original = path.read_bytes()
        for mutation in ('score', 'signature', 'identity'):
            with self.subTest(mutation=mutation):
                receipt = copy.deepcopy(self.receipt)
                if mutation == 'score':
                    receipt['scientific_score'] += 0.01
                elif mutation == 'signature':
                    receipt.pop('signature')
                else:
                    receipt['run_id'] = 'another-run'
                    receipt = _fixtures.c.signed(self.fixture.contract, receipt)
                path.write_text(json.dumps(receipt))
                self.assert_unavailable()
                path.write_bytes(original)
        self.assertEqual(self.overlay()['state'], 'VERIFIED')

    def test_source_data_model_and_result_hashes_are_bound_to_receipt(self):
        artifacts = [self.fixture.public / 'method.py',
                     self.fixture.public / 'model-0.pt',
                     self.fixture.public / 'checkpoint-0.pt',
                     self.fixture.evidence / 'data.bin',
                     self.fixture.evidence / 'evaluator.py',
                     self.fixture.evidence / 'protocol.manifest.json',
                     self.fixture.evidence / 'result-0.json',
                     self.fixture.evidence / 'reload-0.json']
        for path in artifacts:
            with self.subTest(artifact=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                self.assert_unavailable()
                path.write_bytes(original)
        self.assertEqual(self.overlay()['state'], 'VERIFIED')

    def test_uncompleted_scheduler_job_cannot_become_verified(self):
        self.fixture.job_state('RUNNING')
        self.assert_unavailable()

    def test_frozen_anchors_require_matching_metadata_sha(self):
        path = self.fixture.base / 'task.metadata.json'
        path.write_text(json.dumps({'frozen_anchors': {'B': self.BASELINE, 'R': self.REFERENCE}}))
        settings = self.task['operator_overlay']
        settings.update(metadata_path=path.name, metadata_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        view = self.overlay()
        self.assertEqual(view['B'], self.BASELINE)
        self.assertEqual(view['R'], self.REFERENCE)
        original = path.read_bytes()
        path.write_text(json.dumps({'frozen_anchors': {'B': 0.001, 'R': 0.999}}))
        view = self.overlay()
        self.assertEqual(view['state'], 'VERIFIED')
        self.assertNotIn('B', view)
        self.assertNotIn('R', view)
        path.write_bytes(original)
        settings['metadata_sha256'] = 'f' * 64
        self.assertNotIn('B', self.overlay())

    def test_symlink_and_escaping_contract_or_metadata_are_rejected(self):
        alias = self.fixture.base / 'alias.contract.json'
        alias.symlink_to(self.contract_path)
        task = copy.deepcopy(self.task)
        task['operator_overlay']['records'] = [{'contract_path': alias.name}]
        self.assert_unavailable(task)
        task['operator_overlay']['records'] = [{'contract_path': '../outside.json'}]
        self.assert_unavailable(task)
        metadata = self.fixture.base / 'real.metadata.json'
        metadata.write_text(json.dumps({'frozen_anchors': {'B': self.BASELINE, 'R': self.REFERENCE}}))
        alias = self.fixture.base / 'alias.metadata.json'
        alias.symlink_to(metadata)
        self.task['operator_overlay'].update(metadata_path=alias.name,
            metadata_sha256=hashlib.sha256(metadata.read_bytes()).hexdigest())
        self.assertNotIn('B', self.overlay())

    def config(self, operator_tty):
        path = self.fixture.base / 'monitor.tasks.json'
        path.write_text(json.dumps({'version': 1, 'hosts': {'local': {'transport': 'local'}},
                                    'tasks': [self.task]}))
        config = monitor.load_config(path)
        config['_operator_tty'] = operator_tty
        return config

    def assert_no_scores(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        for token in (str(self.SCORE), str(self.BASELINE), str(self.REFERENCE),
                      'scientific_score', 'operator_overlay', 'frozen_anchors', 'score_evidence'):
            self.assertNotIn(token, text)

    def test_tty_snapshot_extracts_verified_overlay_before_public_json_and_persist(self):
        config = self.config(True)
        data = monitor.snapshot(config)
        self.assertEqual(monitor._OPERATOR_OVERLAY['registered']['latest'], self.SCORE)
        self.assert_no_scores(data)
        state = self.fixture.base / 'monitor-state'
        monitor.persist(state, data, {})
        for path in state.iterdir():
            self.assert_no_scores(path.read_text())

    def test_non_tty_probe_never_collects_or_transports_overlay(self):
        config = self.config(False)
        raw = monitor.probe_host(config['hosts']['local'], config['tasks'], config)
        self.assertNotIn('_operator_overlay', raw)
        self.assert_no_scores(raw)
        data = monitor.snapshot(config)
        self.assert_no_scores(data)
        self.assertEqual(monitor._OPERATOR_OVERLAY, {})

    def test_non_tty_watch_never_prints_or_persists_overlay(self):
        config = self.config(False)
        monitor._OPERATOR_OVERLAY['stale'] = {'latest': self.SCORE, 'B': self.BASELINE, 'R': self.REFERENCE}
        state = self.fixture.base / 'watch-state'
        output = StringIO()
        with redirect_stdout(output):
            monitor.watch(config, state, 1, 1, 1, False, json_output=True)
        self.assert_no_scores(output.getvalue())
        for path in state.iterdir():
            if path.is_file():
                self.assert_no_scores(path.read_text())
        self.assertEqual(monitor._OPERATOR_OVERLAY, {})

    def test_defense_in_depth_strips_scores_before_persist(self):
        data = {'collected_at': monitor.utc(1000), 'tasks': [], 'alerts': [],
                '_operator_overlay': {'registered': self.overlay()},
                'scientific_score': self.SCORE, 'B': self.BASELINE, 'R': self.REFERENCE,
                'score_evidence': {'result': str(self.fixture.evidence)}}
        self.assert_no_scores(public_record(data))
        state = self.fixture.base / 'injected-state'
        monitor.persist(state, data, {})
        for path in state.iterdir():
            self.assert_no_scores(path.read_text())

    def test_numeric_score_aliases_cannot_bypass_public_record(self):
        for name in ('score', 'formal_score', 'final_score', 'normalized_score'):
            with self.subTest(field=name):
                data = {'collected_at': monitor.utc(1000), 'tasks': [], 'alerts': [],
                        'source': {name: self.SCORE}}
                self.assert_no_scores(public_record(data))


if __name__ == '__main__':
    unittest.main()
