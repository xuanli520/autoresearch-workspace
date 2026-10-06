"""Local authentication contract checks; no real credentials or remote SSH."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import askpass
import monitor


class SSHAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fifo = self.root / 'password.fifo'
        os.mkfifo(self.fifo, 0o600)
        self.identity = askpass.inspect_fifo(str(self.fifo))

    def test_fifo_identity_contains_device_inode_owner_and_permissions(self):
        info = self.fifo.lstat()
        self.assertEqual(self.identity,
                         f'{info.st_dev}:{info.st_ino}:{os.geteuid()}:{0o600}')
        for index in range(4):
            parts = self.identity.split(':')
            parts[index] = str(int(parts[index]) + 1)
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'identity changed'):
                askpass.inspect_fifo(str(self.fifo), ':'.join(parts))

    def test_fifo_validation_rejects_symlink_regular_file_and_wrong_permissions(self):
        for kind in ('symlink', 'regular', 'permissions', 'directory_permissions'):
            with self.subTest(kind=kind):
                self.fifo.unlink()
                os.chmod(self.root, 0o700)
                if kind == 'symlink':
                    target = self.root / 'target'
                    target.write_text('must not be read')
                    self.fifo.symlink_to(target)
                elif kind == 'regular':
                    self.fifo.write_text('must not be read')
                else:
                    os.mkfifo(self.fifo, 0o600)
                    os.chmod(self.fifo if kind == 'permissions' else self.root, 0o666 if kind == 'permissions' else 0o755)
                with self.assertRaises(ValueError):
                    askpass.inspect_fifo(str(self.fifo))
        os.chmod(self.root, 0o700)

    def test_fifo_validation_rejects_wrong_owner(self):
        real_lstat = os.lstat

        def wrong_owner(path):
            info = real_lstat(path)
            if str(path) == str(self.fifo):
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info

        with patch('askpass.os.lstat', side_effect=wrong_owner):
            with self.assertRaisesRegex(ValueError, 'current owner'):
                askpass.inspect_fifo(str(self.fifo))

    def test_fifo_replacement_between_check_and_open_is_rejected_without_blocking(self):
        real_open = os.open

        def replace_and_open(path, flags):
            self.fifo.unlink()
            self.fifo.write_text('replacement')
            return real_open(path, flags)

        with patch('askpass.os.open', side_effect=replace_and_open):
            with self.assertRaisesRegex(ValueError, 'opened password FIFO identity changed'):
                askpass.open_password_fifo(str(self.fifo), self.identity, os.O_RDONLY)

    def test_fifo_open_uses_nonblocking_nofollow_and_cloexec(self):
        real_open = os.open
        with patch('askpass.os.open', wraps=real_open) as opened:
            fd = askpass.open_password_fifo(str(self.fifo), self.identity, os.O_RDWR)
        try:
            flags = opened.call_args.args[1]
            for flag in (os.O_NONBLOCK, os.O_NOFOLLOW, os.O_CLOEXEC):
                self.assertTrue(flags & flag)
        finally:
            os.close(fd)

    def test_helper_rejects_missing_identity_and_an_empty_fifo_immediately(self):
        for identity in (None, self.identity):
            env = {askpass.FIFO_PATH_ENV: str(self.fifo)}
            if identity:
                env[askpass.FIFO_IDENTITY_ENV] = identity
            with self.subTest(identity=bool(identity)), patch.dict(os.environ, env, clear=True):
                self.assertEqual(askpass.main(), 1)

    def test_helper_subprocess_rejects_fifo_replaced_after_registration(self):
        helper = Path(askpass.__file__)
        env = os.environ.copy()
        env.update({askpass.FIFO_PATH_ENV: str(self.fifo), askpass.FIFO_IDENTITY_ENV: self.identity})
        # Keeping the original inode alive prevents inode reuse in this test.
        original_fd = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            for kind in ('fifo', 'symlink', 'regular', 'permissions'):
                with self.subTest(kind=kind):
                    self.fifo.unlink()
                    if kind in ('fifo', 'permissions'):
                        os.mkfifo(self.fifo, 0o600)
                        if kind == 'permissions':
                            os.chmod(self.fifo, 0o666)
                    elif kind == 'regular':
                        self.fifo.write_text('replacement-secret\n')
                    else:
                        target = self.root / 'replacement'
                        target.write_text('replacement-secret\n')
                        self.fifo.symlink_to(target)
                    result = subprocess.run([str(helper)], env=env, capture_output=True, timeout=2)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, b'')
        finally:
            os.close(original_fd)

    def test_helper_validation_rejects_symlink_writeable_and_nonexecutable_files(self):
        helper = self.root / 'helper.py'
        helper.write_text('#!/usr/bin/env python3\n')
        for mode in (0o644, 0o775):
            helper.chmod(mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'trusted, executable'):
                askpass.validate_helper(helper)
        helper.chmod(0o755)
        askpass.validate_helper(helper)
        link = self.root / 'helper-link.py'
        link.symlink_to(helper)
        with self.assertRaisesRegex(ValueError, 'trusted, executable'):
            askpass.validate_helper(link)

    def test_run_ssh_keeps_secret_out_of_argv_env_and_regular_files_and_cleans_up(self):
        captured = {}
        real_run = subprocess.run
        password = 'test-only-secret'

        def fake_ssh(command, **kwargs):
            env = kwargs['env']
            self.assertNotIn(password, json.dumps(command))
            self.assertNotIn(password, json.dumps(env))
            self.assertEqual(env['SSH_ASKPASS_REQUIRE'], 'force')
            fifo = Path(env[askpass.FIFO_PATH_ENV])
            captured['directory'] = fifo.parent
            self.assertEqual(list(fifo.parent.iterdir()), [fifo])
            self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
            self.assertEqual(stat.S_IMODE(fifo.parent.stat().st_mode), 0o700)
            answer = real_run([env['SSH_ASKPASS']], env=env, capture_output=True,
                              text=True, timeout=2)
            self.assertEqual(answer.returncode, 0, answer.stderr)
            self.assertEqual(answer.stdout, password + '\n')
            return subprocess.CompletedProcess(command, 0, 'verified', '')

        with patch('monitor.subprocess.run', side_effect=fake_ssh):
            result = monitor.run_ssh({'password': password}, ['ssh', 'test'], '', 3)
        self.assertEqual(result.stdout, 'verified')
        self.assertFalse(captured['directory'].exists())

    def test_run_ssh_cleans_fifo_on_timeout_or_launch_failure(self):
        for failure in (subprocess.TimeoutExpired('ssh', 1), OSError('launch failed')):
            captured = {}

            def fail(command, **kwargs):
                captured['directory'] = Path(kwargs['env'][askpass.FIFO_PATH_ENV]).parent
                raise failure

            with self.subTest(failure=type(failure).__name__), patch('monitor.subprocess.run', side_effect=fail):
                with self.assertRaises(type(failure)):
                    monitor.run_ssh({'password': 'test-secret'}, ['ssh', 'test'], '', 1)
            self.assertFalse(captured['directory'].exists())

    def test_run_ssh_rejects_tampered_fifo_before_launch_and_cleans_directory(self):
        real_mkfifo = os.mkfifo
        captured = {}

        def tamper(path, mode):
            real_mkfifo(path, mode)
            captured['directory'] = Path(path).parent
            os.chmod(path, 0o666)

        with patch('monitor.os.mkfifo', side_effect=tamper), patch('monitor.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'mode 0600'):
                monitor.run_ssh({'password': 'test-secret'}, ['ssh', 'test'], '', 1)
        run.assert_not_called()
        self.assertFalse(captured['directory'].exists())

    def test_ssh_command_ignores_user_config_and_disallows_alternate_auth_options(self):
        host = {'hostname': 'gpu.example', 'user': 'test', 'options': ['-o', 'Compression=yes']}
        command = monitor.ssh_command(host, 'python3 -', password_auth=True)
        self.assertEqual(command[command.index('-F') + 1], '/dev/null')
        self.assertIn('PubkeyAuthentication=no', command)
        self.assertIn('HostbasedAuthentication=no', command)
        self.assertIn('GSSAPIAuthentication=no', command)
        for options in (['-F', '/tmp/alternate'], ['-i', 'key'], ['-o', 'ProxyCommand=read-secret'],
                        ['-oIdentityAgent=socket'], ['-o', 'PreferredAuthentications=publickey'], [123]):
            with self.subTest(options=options), self.assertRaises(ValueError):
                monitor.ssh_command({**host, 'options': options}, 'python3 -', password_auth=True)

    def test_config_host_validation_rejects_credentials_and_legacy_auth(self):
        monitor.validate_ssh_host({'transport': 'ssh', 'options': []})
        for key in ('hostname', 'host', 'user', 'password', 'port', 'target', 'password_env', 'identity_file'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                monitor.validate_ssh_host({'transport': 'ssh', key: 'unused'})

    def test_password_bounds_fail_before_creating_fifo(self):
        for password in ('', 'x' * (askpass.MAX_PASSWORD_BYTES + 1), 'line\nbreak', 'nul\x00'):
            with self.subTest(password_length=len(password)), patch('monitor.tempfile.mkdtemp') as temp:
                with self.assertRaises(ValueError):
                    monitor.run_ssh({'password': password}, ['ssh', 'test'], '', 1)
            temp.assert_not_called()

    def test_auth_hot_reload_updates_only_ssh_hosts_and_preserves_valid_previous_credentials(self):
        auth_path = self.root / 'auth.txt'
        cfg = {'hosts': {'remote': {'transport': 'ssh'}, 'local': {'transport': 'local'}}}
        with patch('monitor.purge_host_key'):
            for password, address in (('first-test-password', '192.0.2.1'), ('second-test-password', '192.0.2.2')):
                auth_path.write_text(f'ip: {address}\nuser: test\npassword: {password}\n')
                monitor.refresh_auth(cfg, auth_path)
                self.assertEqual(cfg['hosts']['remote']['password'], password)
                self.assertEqual(cfg['hosts']['remote']['hostname'], address)
            auth_path.write_text('password: incomplete\n')
            monitor.refresh_auth(cfg, auth_path)
        self.assertEqual(cfg['hosts']['remote']['password'], 'second-test-password')
        self.assertNotIn('password', cfg['hosts']['local'])

    def test_hot_reload_rejects_invalid_password_without_replacing_usable_credentials(self):
        path = self.root / 'auth.txt'
        cfg = {'hosts': {'gpu': {'transport': 'ssh'}}}
        with patch('monitor.purge_host_key'):
            path.write_text('ip: 192.0.2.1\nuser: test\npassword: usable\n')
            monitor.refresh_auth(cfg, path)
            for invalid in ('nul\x00', 'x' * (askpass.MAX_PASSWORD_BYTES + 1)):
                path.write_text(f'ip: 192.0.2.2\nuser: test\npassword: {invalid}\n')
                monitor.refresh_auth(cfg, path)
                self.assertEqual(cfg['hosts']['gpu']['password'], 'usable')
                self.assertEqual(cfg['hosts']['gpu']['hostname'], '192.0.2.1')

    def test_hot_reload_refreshes_host_key_only_for_changed_connection_identity(self):
        path = self.root / 'auth.txt'
        cfg = {'hosts': {'gpu': {'transport': 'ssh'}}}
        with patch('monitor.purge_host_key') as purge:
            path.write_text('ip: 192.0.2.1\nuser: test\npassword: usable\n')
            monitor.refresh_auth(cfg, path)
            self.assertEqual(purge.call_args.args[0]['hostname'], '192.0.2.1')
            purge.reset_mock()
            monitor.refresh_auth(cfg, path)
            purge.assert_not_called()


if __name__ == '__main__':
    unittest.main()
