import json
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from tools.gpu_scheduler.release import build, publish, verify


def production_config():
    return {'version': 1, 'root': '/mnt/data/gpu-queue', 'data_mount': '/mnt/data',
            'gpus': [{'uuid': 'GPU-test', 'memory_mib': 100, 'compute_units': 100}],
            'cpu_cores': 16, 'ram_mib': 30720, 'persistent': True,
            'execution_backend': 'systemd'}


class ReleaseTests(unittest.TestCase):
    def test_release_is_hash_pinned_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config.json'
            config.write_text(json.dumps(production_config()))
            output = root / 'release'
            result = build(output, config, '/mnt/data/ops/gpu_scheduler/releases/test', 'stage only')
            self.assertTrue(result['valid'])
            self.assertFalse(result['installation_performed'])
            self.assertTrue((output / 'tools/research_completion/__main__.py').is_file())
            shim = (output / 'tools/research_handoff/core/processes.py').read_text()
            self.assertIn('from tools.process_control import processes', shim)
            with self.assertRaises(FileExistsError):
                build(output, config, '/mnt/data/ops/gpu_scheduler/releases/test', 'stage only')
            path = output / 'tools/gpu_scheduler/scheduler.py'
            path.write_bytes(path.read_bytes() + b'\n# altered\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                verify(output)

    def test_release_rejects_extra_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config.json'
            config.write_text(json.dumps(production_config()))
            output = root / 'release'
            build(output, config, '/mnt/data/ops/gpu_scheduler/releases/test', 'stage only')
            (output / 'unexpected.txt').write_text('extra')
            with self.assertRaisesRegex(ValueError, 'member list'):
                verify(output)

    def test_unknown_config_field_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config.json'
            config.write_text(json.dumps({**production_config(), 'private_key': 'must not publish'}))
            with self.assertRaises(ValueError):
                build(root / 'release', config, '/mnt/data/ops/releases/test', 'stage only')
            self.assertFalse((root / 'release').exists())

    def test_manifest_cannot_expand_the_fixed_whitelist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config.json'
            config.write_text(json.dumps(production_config()))
            output = root / 'release'
            build(output, config, '/mnt/data/ops/releases/test', 'stage only')
            extra = output / 'auth.txt'
            extra.write_text('not allowed')
            manifest_path = output / 'RELEASE_MANIFEST.json'
            manifest = json.loads(manifest_path.read_text())
            from tools.gpu_scheduler.release import sha
            manifest['files']['auth.txt'] = {'sha256': sha(extra), 'mode': extra.stat().st_mode & 0o777}
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'member list'):
                verify(output)

    def test_existing_receipt_rejects_before_ssh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / 'receipt.json'
            receipt.write_text('{}')
            with patch('tools.gpu_monitor.monitor.run_ssh') as ssh:
                with self.assertRaises(FileExistsError):
                    publish(root / 'release', root / 'missing-auth', receipt)
                ssh.assert_not_called()
