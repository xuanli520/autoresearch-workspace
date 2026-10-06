import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.gpu_scheduler import runtime


class RuntimeRestoreTests(unittest.TestCase):
    def test_shared_process_sources_must_be_pinned(self):
        shared = Path(runtime.processes.__file__).resolve()
        self.assertIn('process_control', shared.parts)
        source = Path(runtime.__file__).resolve()
        root = '/mnt/task/restore'
        plan = {'version': 1, 'root': root, 'data_mount': '/mnt/task',
                'runtime_contract': root + '/contract.json', 'runtime_launch': root + '/launch.json',
                'docker_config': root + '/docker.json', 'containerd_config': root + '/containerd.toml',
                'deadline_epoch': 1000, 'authorization': 'explicit test authorization',
                'unit_prefix': 'task-runtime'}
        pinned = {str(item) for item in source.parent.glob('*.py')}
        pinned.update(str(source.parents[1] / 'research_handoff/core' / name)
                      for name in ('processes.py', 'longrun.py'))
        pinned.update(plan[key] for key in ('runtime_contract', 'runtime_launch',
                                          'docker_config', 'containerd_config'))
        for missing in (shared, shared.with_name('__init__.py')):
            hashes = {name: 'hash' for name in pinned | {str(shared), str(shared.with_name('__init__.py'))}}
            hashes.pop(str(missing))
            with self.subTest(missing=missing), \
                 mock.patch.object(runtime, 'read', return_value=dict(plan, source_sha256=hashes)), \
                 mock.patch.object(runtime.time, 'time', return_value=900), \
                 mock.patch.object(runtime, 'check_storage') as check_storage:
                with self.assertRaisesRegex(ValueError, 'must pin code'):
                    runtime.load_plan(root + '/plan.json')
                check_storage.assert_not_called()

    def test_runtime_deadline_is_shared_and_not_reset_between_daemons(self):
        plan = {'deadline_epoch': 1000, 'root': '/mnt/task/restore', 'data_mount': '/mnt/task',
                'unit_prefix': 'test-runtime', 'docker_config': '/mnt/task/docker.json',
                'containerd_config': '/mnt/task/containerd.toml'}
        first = runtime.unit_command(plan, 'containerd', now=100)
        second = runtime.unit_command(plan, 'dockerd', now=150)
        self.assertIn('--property=RuntimeMaxSec=900.000000s', first)
        self.assertIn('--property=RuntimeMaxSec=850.000000s', second)
        self.assertIn('--property=Restart=no', first)
        self.assertIn('--property=StandardOutput=append:/mnt/task/restore/dockerd.log', second)
        with self.assertRaisesRegex(ValueError, 'expired'):
            runtime.unit_command(plan, 'dockerd', now=1000)

    def test_live_original_wrapper_blocks_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            launch = Path(directory) / 'launch.json'
            launch.write_text(json.dumps({'processes': [
                {'name': name, 'pid': 1, 'start_ticks': 1, 'boot_id': 'boot'}
                for name in ('containerd', 'dockerd')]}))
            with mock.patch.object(runtime.processes, 'pid_matches', return_value=True):
                with self.assertRaisesRegex(ValueError, 'remains alive'):
                    runtime.ensure_stopped({'runtime_launch': str(launch)}, {}, {})

    def test_same_store_adopted_by_new_daemon_blocks_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launch = root / 'launch.json'
            launch.write_text(json.dumps({'processes': [
                {'name': name, 'pid': 1, 'start_ticks': 1, 'boot_id': 'boot'}
                for name in ('containerd', 'dockerd')]}))
            config = root / 'daemon.json'
            config.write_text(json.dumps({'data-root': str(root / 'store')}))
            proc = root / '123'
            proc.mkdir()
            (proc / 'cmdline').write_bytes(b'/usr/bin/dockerd\0--config-file\0' + str(config).encode() + b'\0')
            with mock.patch.object(runtime.processes, 'pid_matches', return_value=False), \
                    mock.patch.object(Path, 'glob', return_value=[proc]):
                with self.assertRaisesRegex(ValueError, 'already uses'):
                    runtime.ensure_stopped({'runtime_launch': str(launch)},
                                           {'data-root': str(root / 'store')}, {})

    def test_cleanup_resets_failed_unit_even_if_stop_times_out(self):
        unit = 'task-owned-runtime-dockerd.service'
        with mock.patch.object(runtime.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired(['systemctl', 'stop', unit], timeout=20),
                subprocess.CompletedProcess([], 0, '', '')]) as run:
            receipt = runtime._cleanup_unit(unit)
        self.assertIsNone(receipt['returncode'])
        self.assertEqual(receipt['reset_failed_returncode'], 0)
        self.assertIn('stop_error', receipt)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['systemctl', 'stop', unit], ['systemctl', 'reset-failed', unit]])

    def test_cleanup_preserves_reset_failed_error(self):
        with mock.patch.object(runtime.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CompletedProcess([], 1, '', 'unit did not exist\n')]):
            receipt = runtime._cleanup_unit('task-owned-runtime-dockerd.service')
        self.assertEqual(receipt['returncode'], 0)
        self.assertEqual(receipt['reset_failed_returncode'], 1)
        self.assertEqual(receipt['reset-failed_error'], 'unit did not exist')

    def test_launch_failure_records_stop_and_reset_without_masking_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'plan.json'
            config.write_text('{}')
            plan = {'root': directory, 'unit_prefix': 'task-owned-runtime',
                    'deadline_epoch': 1000}
            launch_error = subprocess.CalledProcessError(1, ['systemd-run', 'dockerd'])
            commands = []

            def run(command, **kwargs):
                commands.append(command)
                if command[:2] == ['systemctl', 'show']:
                    return subprocess.CompletedProcess(command, 0, 'not-found\n', '')
                if command == ['launch-dockerd']:
                    raise launch_error
                if command[:2] == ['systemctl', 'stop']:
                    raise subprocess.TimeoutExpired(command, timeout=20)
                return subprocess.CompletedProcess(command, 0, '', '')

            with mock.patch.object(runtime.os, 'geteuid', return_value=0), \
                 mock.patch.object(runtime, 'load_plan', return_value=(plan, {}, {}, {})), \
                 mock.patch.object(runtime, 'ensure_stopped'), \
                 mock.patch.object(runtime.os, 'chown'), \
                 mock.patch.object(runtime, 'prepare_bridge'), \
                 mock.patch.object(runtime, 'unit_command', side_effect=lambda plan, name: ['launch-' + name]), \
                 mock.patch.object(runtime.subprocess, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    runtime.restart_plan(config)
                self.assertIs(caught.exception, launch_error)

            receipt = json.loads((root / 'failure.json').read_text())
            self.assertEqual(receipt['installed_units'], ['task-owned-runtime-containerd.service'])
            self.assertEqual([entry['unit'] for entry in receipt['cleanup']], [
                'task-owned-runtime-dockerd.service', 'task-owned-runtime-containerd.service'])
            self.assertTrue(all(entry['reset_failed_returncode'] == 0 for entry in receipt['cleanup']))
            self.assertTrue(all(entry['returncode'] is None for entry in receipt['cleanup']))
            self.assertEqual([command for command in commands if command[1:2] == ['reset-failed']], [
                ['systemctl', 'reset-failed', 'task-owned-runtime-dockerd.service'],
                ['systemctl', 'reset-failed', 'task-owned-runtime-containerd.service']])
            self.assertTrue((root / 'planned.json').exists())
            self.assertFalse((root / 'installed.json').exists())


if __name__ == '__main__':
    unittest.main()
