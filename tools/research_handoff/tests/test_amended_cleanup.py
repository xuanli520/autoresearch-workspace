"""Recovery uses audited amended hooks while preserving immutable turn launch."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
import cleanup


class AmendedCleanupTests(unittest.TestCase):
    def test_original_hook_without_amendment(self):
        with tempfile.TemporaryDirectory() as temporary:
            turn = Path(temporary) / 'turns/000001'
            turn.mkdir(parents=True)
            launch = dict(cleanup=dict(command=['original']))
            self.assertEqual(cleanup.amended_cleanup(turn, launch), (launch['cleanup'], {}, None))

    def test_verified_amendment_selects_new_hook_and_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            turn = root / 'turns/000001'
            turn.mkdir(parents=True)
            folder = root / 'amendments/000001'
            folder.mkdir(parents=True)
            target = folder / 'config.json'
            target.write_text('{}')
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(dict(paths=dict(config='amendments/000001/config.json'), config_sha256=digest)))
            (folder / 'receipt.json').write_text(json.dumps(dict(config_sha256=digest)))
            config = dict(cleanup=dict(command=['fixed']), env=dict(PYTHONPATH='fixed-source'))
            with patch.object(cleanup, 'load_config', return_value=config):
                hook, env, source = cleanup.amended_cleanup(turn, dict(cleanup=dict(command=['original'])))
            self.assertEqual(hook, config['cleanup'])
            self.assertEqual(env, config['env'])
            self.assertEqual(source, str(target))

    def test_changed_config_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            turn = root / 'turns/000001'
            turn.mkdir(parents=True)
            folder = root / 'amendments/000001'
            folder.mkdir(parents=True)
            (folder / 'config.json').write_text('{}')
            (root / 'state.json').write_text(json.dumps(dict(paths=dict(config='amendments/000001/config.json'), config_sha256='wrong')))
            (folder / 'receipt.json').write_text(json.dumps(dict(config_sha256='wrong')))
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                cleanup.amended_cleanup(turn, dict(cleanup=dict(command=['original'])))


if __name__ == '__main__':
    unittest.main()
