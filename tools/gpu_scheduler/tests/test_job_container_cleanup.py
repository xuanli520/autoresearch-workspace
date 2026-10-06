"""GPU slots must remain held when registered Docker cleanup is unconfirmed."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.gpu_scheduler.container_ownership import cleanup_job, register_project
from tools.gpu_scheduler.common import read_json


class JobContainerCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.launch = {'id': 'job', 'token': 'a' * 32, 'config': {'local_test': True}}
        (self.root / 'launch.json').write_text(json.dumps(self.launch))
        self.receipts = self.root / 'container-projects'
        self.receipts.mkdir(mode=0o700)
        self.row = {'version': 1, 'job_id': 'job', 'token': 'a' * 32,
                    'project': 'owned', 'docker_host': 'unix:///var/run/docker.sock'}
        self.write_receipt()

    def write_receipt(self):
        (self.receipts / 'owned.json').write_text(json.dumps(self.row))

    def test_prestart_registration_reclaims_resources_after_interrupted_start(self):
        # No main-container receipt exists: startup may have created a sidecar.
        with patch('tools.gpu_scheduler.container_ownership.subprocess.run') as run:
            run.return_value.stdout = ''
            run.side_effect = [type('R', (), {'stdout': 'a' * 64})(),
                               type('R', (), {'stdout': ''})(),
                               type('R', (), {'stdout': ''})()]
            result = cleanup_job(self.root)
            self.assertEqual(result['removed'], ['a' * 64])
            self.assertIn('label=com.docker.compose.project=owned', run.call_args_list[0].args[0])
            self.assertTrue(read_json(self.root / 'container-cleanup.json')['cleanup_ok'])

    def test_surviving_container_blocks_success_receipt(self):
        with patch('tools.gpu_scheduler.container_ownership.subprocess.run') as run:
            run.side_effect = [type('R', (), {'stdout': 'a' * 64})(),
                               type('R', (), {'stdout': ''})(),
                               type('R', (), {'stdout': 'b' * 64})()]
            with self.assertRaisesRegex(RuntimeError, 'remain'):
                cleanup_job(self.root)
            self.assertFalse((self.root / 'container-cleanup.json').exists())

    def test_wrong_job_and_writable_receipts_never_call_docker(self):
        with patch('tools.gpu_scheduler.container_ownership.subprocess.run') as run:
            self.row['job_id'] = 'another'
            self.write_receipt()
            with self.assertRaisesRegex(ValueError, 'identity'):
                cleanup_job(self.root)
            self.row['job_id'] = 'job'
            self.write_receipt()
            (self.receipts / 'owned.json').chmod(0o666)
            with self.assertRaisesRegex(ValueError, 'invalid'):
                cleanup_job(self.root)
            run.assert_not_called()

    def test_symlink_receipt_and_changed_endpoint_are_rejected(self):
        source = self.root / 'outside.json'
        source.write_text(json.dumps(self.row))
        (self.receipts / 'owned.json').unlink()
        (self.receipts / 'owned.json').symlink_to(source)
        with self.assertRaisesRegex(ValueError, 'invalid'):
            cleanup_job(self.root)
        (self.receipts / 'owned.json').unlink()
        self.write_receipt()
        old = self.root / 'containers'
        old.mkdir(mode=0o700)
        (old / ('a' * 64 + '.json')).write_text(json.dumps({**self.row, 'docker_host': 'unix:///different.sock'}))
        with self.assertRaisesRegex(ValueError, 'binding'):
            cleanup_job(self.root)

    def test_cpu_job_with_no_docker_resources_still_completes(self):
        (self.receipts / 'owned.json').unlink()
        with patch('tools.gpu_scheduler.container_ownership.subprocess.run') as run:
            self.assertTrue(cleanup_job(self.root)['cleanup_ok'])
            run.assert_not_called()

    def test_recovery_uses_current_pinned_storage_without_changing_launch(self):
        original = (self.root / 'launch.json').read_bytes()
        config = {'local_test': True, 'data_mount': '/data', 'data_mounts': ['/data/queue']}
        with patch('tools.gpu_scheduler.container_ownership.check_storage') as validate:
            with patch('tools.gpu_scheduler.container_ownership.subprocess.run') as run:
                run.return_value.stdout = ''
                receipt = cleanup_job(self.root, storage_config=config)
        validate.assert_called_once_with(config, self.root)
        self.assertEqual(receipt['recovery_data_mounts'], ['/data', '/data/queue'])
        self.assertEqual((self.root / 'launch.json').read_bytes(), original)

    def test_unapproved_recovery_mount_still_blocks_cleanup(self):
        with patch('tools.gpu_scheduler.container_ownership.check_storage', side_effect=ValueError('unexpected device')):
            with patch('tools.gpu_scheduler.container_ownership.subprocess.run') as run:
                with self.assertRaisesRegex(ValueError, 'unexpected device'):
                    cleanup_job(self.root, storage_config={'data_mount': '/invalid'})
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
