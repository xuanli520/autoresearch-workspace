import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
import remote

SCRATCH = ROOT / 'incidents/controller-hardening/scratch'


class RemoteAuthTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'connection.json'
        self.config = {
            'host': 'cloud.invalid', 'user': 'research',
            'controller_dir': '/data/controller', 'state_dir': '/data/state',
            'data_mount': '/data', 'auth_file': 'private/auth.txt',
        }

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        self.path.write_text(json.dumps(self.config))
        return remote.connection_config(self.path)

    def test_relative_auth_path_and_exclusive_identity(self):
        self.assertEqual(self.load()['auth_file'], str(self.root / 'private/auth.txt'))
        self.config['identity_file'] = '/private/key'
        with self.assertRaisesRegex(remote.ControllerError, 'choose auth_file or identity_file'):
            self.load()

    def test_password_transport_uses_managed_askpass_and_text_result(self):
        cfg = self.load()
        managed = mock.Mock()
        managed.apply_auth.return_value = {'transport': 'ssh', 'password': 'test-only'}
        managed.ssh_command.return_value = ['ssh', 'cloud.invalid', 'quoted command']
        managed.run_ssh.return_value = subprocess.CompletedProcess([], 0, '{"status":"RUNNING"}', '')
        with mock.patch.object(remote, '_monitor_module', return_value=managed):
            reply, code = remote.call(cfg, ['python3', '/data/a b/rpc.py'], {'action': 'status'})
        self.assertEqual((reply, code), ({'status': 'RUNNING'}, 0))
        managed.load_auth.assert_called_with(self.root / 'private/auth.txt')
        self.assertTrue(managed.ssh_command.call_args.kwargs['password_auth'])
        self.assertEqual(managed.ssh_command.call_args.args[1], "python3 '/data/a b/rpc.py'")
        call = managed.run_ssh.call_args
        self.assertNotIn('test-only', str(call.args[1:]))
        self.assertEqual(json.loads(call.args[2]), {'action': 'status'})

    def test_key_transport_without_optional_auth_field_and_bytes_result(self):
        cfg = self.load()
        cfg.pop('auth_file')
        result = subprocess.CompletedProcess([], 0, b'{"status":"READY"}', b'')
        with mock.patch.object(remote.subprocess, 'run', return_value=result):
            self.assertEqual(remote.call(cfg, ['python3'], {}), ({'status': 'READY'}, 0))

    def test_unknown_password_mutation_is_not_replayed(self):
        cfg = self.load()
        managed = mock.Mock()
        managed.run_ssh.side_effect = subprocess.TimeoutExpired('ssh', 2)
        with mock.patch.object(remote, '_monitor_module', return_value=managed):
            with self.assertRaisesRegex(remote.ControllerError, 'UNKNOWN'):
                remote.call(cfg, ['python3'], {'action': 'start'})
        self.assertEqual(managed.run_ssh.call_count, 1)


if __name__ == '__main__':
    unittest.main()
