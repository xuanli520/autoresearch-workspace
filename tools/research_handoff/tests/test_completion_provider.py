"""Provider hook tests with an explicit Harbor API stub; no Docker is started."""
import importlib
import inspect
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class CompletionProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, trusted=False, contract=None):
        modules = {name: types.ModuleType(name) for name in (
            'harbor', 'harbor.environments', 'harbor.environments.capabilities',
            'harbor.environments.docker', 'harbor.environments.docker.docker',
            'harbor.models', 'harbor.models.task', 'harbor.models.task.config')}
        class DockerStub:
            def __init__(self, *args, task_env_config=None, **kwargs):
                self.task_env_config = task_env_config or types.SimpleNamespace(gpus=0)

            def _write_resources_compose_file(self):
                return self.resources_path

            async def start(self, force_build):
                self.started_mounts = list(self._mounts)
                return 'started'

            async def stop(self, delete):
                self.stopped_delete = delete
        modules['harbor.environments.capabilities'].EnvironmentCapabilities = object
        modules['harbor.environments.docker.docker'].DockerEnvironment = DockerStub
        modules['harbor.environments.docker.docker']._sanitize_docker_compose_project_name = lambda value: value
        modules['harbor.models.task.config'].NetworkMode = types.SimpleNamespace(PUBLIC='public', ALLOWLIST='allowlist')
        name = 'tools.research_handoff.providers.harbor_docker'
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(sys.modules):
            sys.modules.pop(name, None)
            cls = importlib.import_module(name).ManagedDockerEnvironment
        environment = cls.__new__(cls)
        environment._trusted_verifier = trusted
        environment._enable_egress_control = True
        environment.model_host_addresses = {}
        environment.completion_contract = contract
        environment.gpu_memory_policy = None
        environment._gpu_memory_guard = None
        environment.task_env_config = types.SimpleNamespace(gpus=0)
        environment.gpu_attachment = 'cdi'
        environment.session_id = 'trial-tests' if trusted else 'trial-environment'
        environment.ownership_root = None
        environment.logger = mock.Mock()
        environment.public_image_digest = 'sha256:' + 'b' * 64
        environment.network_config = {'docker_host': 'unix:///private/docker.sock'}
        environment.network_policy = {'mode': 'no_network', 'allowlist': []}
        environment._apply_network_policy = mock.AsyncMock()
        environment.exec = mock.AsyncMock(return_value=types.SimpleNamespace(return_code=0))
        environment._mounts = [
            {'source': '/private/logs', 'target': '/logs/verifier', 'read_only': False},
            {'source': '/private/logs', 'target': '/logs/verifier/reward.json', 'read_only': False},
            {'source': '/public/workspace', 'target': '/workspace', 'read_only': False},
        ]
        environment._run_docker_compose_command = mock.AsyncMock(return_value=types.SimpleNamespace(stdout='a' * 64 + '\n'))
        return environment

    def test_cdi_uses_only_scheduler_device_without_legacy_request(self):
        environment = self.provider()
        environment.gpu_attachment = 'cdi'
        environment.task_env_config = types.SimpleNamespace(gpus=1)
        gpu = 'GPU-' + 'a' * 32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.json'
            path.write_text(json.dumps({'services': {'main': {'deploy': {'resources': {
                'limits': {'memory': '8g'}, 'reservations': {'devices': [
                    {'driver': 'nvidia', 'count': 1, 'capabilities': ['gpu']}]} }}}}}))
            environment.resources_path = path
            with mock.patch.dict('os.environ', {'CUDA_VISIBLE_DEVICES': gpu}):
                environment._write_resources_compose_file()
            main = json.loads(path.read_text())['services']['main']
            self.assertEqual(main['devices'], ['nvidia.com/gpu=' + gpu])
            self.assertNotIn('devices', main['deploy']['resources']['reservations'])
            self.assertEqual(main['environment']['NVIDIA_VISIBLE_DEVICES'], 'void')
            self.assertEqual(main['deploy']['resources']['limits']['memory'], '8g')

    def test_gpu_attachment_defaults_to_cdi(self):
        self.assertEqual(inspect.signature(type(self.provider()).__init__).parameters[
            'gpu_attachment'].default, 'cdi')

    def test_cpu_only_constructor_does_not_apply_gpu_contract(self):
        cls = type(self.provider())
        self.assertIsNotNone(cls(gpu_attachment='device-request'))

    def test_gpu_constructor_rejects_legacy_attachment(self):
        cls = type(self.provider())
        with self.assertRaisesRegex(ValueError, "require gpu_attachment='cdi'"):
            cls(gpu_attachment='device-request', task_env_config=types.SimpleNamespace(gpus=1))

    def test_legacy_device_request_is_rejected(self):
        environment = self.provider()
        environment.gpu_attachment = 'device-request'
        environment.task_env_config = types.SimpleNamespace(gpus=1)
        with self.assertRaisesRegex(ValueError, "require gpu_attachment='cdi'"):
            environment._write_resources_compose_file()

    async def test_both_gpu_roles_reject_legacy_before_container_start(self):
        for trusted in (False, True):
            with self.subTest(verifier=trusted):
                environment = self.provider(trusted=trusted)
                environment.task_env_config = types.SimpleNamespace(gpus=1)
                environment.gpu_attachment = 'device-request'
                with self.assertRaisesRegex(ValueError, "require gpu_attachment='cdi'"):
                    await environment.start(False)
                self.assertFalse(hasattr(environment, 'started_mounts'))

    async def test_both_gpu_roles_attest_device_identity_and_fresh_cuda(self):
        gpu = 'GPU-' + 'a' * 32
        cuda = {'driver_init_returncode': 0, 'device_count_returncode': 0, 'device_count': 1}
        info = {'HostConfig': {'DeviceRequests': None},
                'Config': {'Env': ['NVIDIA_VISIBLE_DEVICES=void']}}
        for trusted in (False, True):
            with self.subTest(verifier=trusted):
                environment = self.provider(trusted=trusted)
                environment._receipt = mock.Mock()
                environment.exec.side_effect = [
                    types.SimpleNamespace(return_code=0, stdout=gpu + '\n'),
                    types.SimpleNamespace(return_code=0, stdout=json.dumps(cuda))]
                with mock.patch('subprocess.run', return_value=types.SimpleNamespace(
                        stdout=json.dumps([info]))) as docker:
                    await environment._attest_gpu_access(gpu)
                self.assertEqual(docker.call_args.args[0][:4],
                                 ['docker', '--host', 'unix:///private/docker.sock', 'inspect'])
                self.assertEqual(environment.exec.await_count, 2)
                receipt = environment._receipt.call_args.args[1]
                self.assertEqual(receipt['role'], 'verifier' if trusted else 'research')
                self.assertEqual(receipt['visible_gpu_uuids'], [gpu])
                self.assertEqual(receipt['cuda'], cuda)
                self.assertEqual(len(receipt['provider_source_sha256']), 64)
                self.assertEqual(len(receipt['gpu_binding_sha256']), 64)

    async def test_runtime_legacy_device_requests_are_rejected(self):
        environment = self.provider()
        info = {'HostConfig': {'DeviceRequests': [{'Driver': 'nvidia'}]}}
        with mock.patch('subprocess.run', return_value=types.SimpleNamespace(stdout=json.dumps([info]))):
            with self.assertRaisesRegex(ValueError, 'legacy DeviceRequests'):
                await environment._attest_gpu_access('GPU-' + 'a' * 32)
        environment.exec.assert_not_awaited()

    async def test_runtime_legacy_environment_is_rejected(self):
        environment = self.provider()
        info = {'HostConfig': {'DeviceRequests': None},
                'Config': {'Env': ['NVIDIA_VISIBLE_DEVICES=all']}}
        with mock.patch('subprocess.run', return_value=types.SimpleNamespace(stdout=json.dumps([info]))):
            with self.assertRaisesRegex(ValueError, 'NVIDIA_VISIBLE_DEVICES=void'):
                await environment._attest_gpu_access('GPU-' + 'a' * 32)
        environment.exec.assert_not_awaited()

    async def test_runtime_extra_gpu_is_rejected_before_cuda(self):
        environment = self.provider()
        gpu = 'GPU-' + 'a' * 32
        info = {'HostConfig': {'DeviceRequests': None},
                'Config': {'Env': ['NVIDIA_VISIBLE_DEVICES=void']}}
        environment.exec.return_value = types.SimpleNamespace(
            return_code=0, stdout=gpu + '\nGPU-' + 'b' * 32 + '\n')
        with mock.patch('subprocess.run', return_value=types.SimpleNamespace(stdout=json.dumps([info]))):
            with self.assertRaisesRegex(RuntimeError, 'only the assigned GPU UUID'):
                await environment._attest_gpu_access(gpu)
        self.assertEqual(environment.exec.await_count, 1)

    async def test_nvml_success_does_not_hide_cuda_initialization_failure(self):
        environment = self.provider()
        environment._receipt = mock.Mock()
        gpu = 'GPU-' + 'a' * 32
        info = {'HostConfig': {'DeviceRequests': None},
                'Config': {'Env': ['NVIDIA_VISIBLE_DEVICES=void']}}
        environment.exec.side_effect = [
            types.SimpleNamespace(return_code=0, stdout=gpu + '\n'),
            types.SimpleNamespace(return_code=1, stdout='')]
        with mock.patch('subprocess.run', return_value=types.SimpleNamespace(stdout=json.dumps([info]))):
            with self.assertRaisesRegex(RuntimeError, 'fresh process could not initialize CUDA'):
                await environment._attest_gpu_access(gpu)
        environment._receipt.assert_not_called()

    async def test_gpu_start_executes_probe_for_both_roles(self):
        gpu = 'GPU-' + 'a' * 32
        for trusted in (False, True):
            with self.subTest(verifier=trusted):
                environment = self.provider(trusted=trusted)
                environment.task_env_config = types.SimpleNamespace(gpus=1)
                environment.ownership_root = Path('/trusted/owned')
                environment._attest_gpu_access = mock.AsyncMock()
                environment.exec.return_value = types.SimpleNamespace(return_code=0)
                with mock.patch.dict('os.environ', {'CUDA_VISIBLE_DEVICES': gpu}, clear=True):
                    self.assertEqual(await environment.start(False), 'started')
                environment._attest_gpu_access.assert_awaited_once_with(gpu)

    async def test_failed_gpu_start_cleans_only_its_environment(self):
        environment = self.provider(trusted=True)
        environment.task_env_config = types.SimpleNamespace(gpus=1)
        environment.ownership_root = Path('/trusted/owned')
        environment._attest_gpu_access = mock.AsyncMock(side_effect=RuntimeError('fresh CUDA unavailable'))
        with mock.patch.dict('os.environ', {'CUDA_VISIBLE_DEVICES': 'GPU-' + 'a' * 32}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'fresh CUDA unavailable'):
                await environment.start(False)
        self.assertTrue(environment.stopped_delete)
        environment.exec.assert_not_awaited()

    async def test_gpu_start_requires_receipt_storage_before_start(self):
        environment = self.provider(trusted=True)
        environment.task_env_config = types.SimpleNamespace(gpus=1)
        with mock.patch.dict('os.environ', {'CUDA_VISIBLE_DEVICES': 'GPU-' + 'a' * 32}, clear=True):
            with self.assertRaisesRegex(ValueError, 'trusted ownership_root'):
                await environment.start(False)
        self.assertFalse(hasattr(environment, 'started_mounts'))

    def test_scheduler_limits_use_compose_deploy_string_types(self):
        environment = self.provider()
        environment.gpu_attachment = 'cdi'
        environment.task_env_config = types.SimpleNamespace(gpus=1)
        limits = {'cgroup_parent': 'research-job.slice', 'mem_limit': 8589934592,
                  'memswap_limit': 8589934592, 'cpus': 2.0}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.json'
            path.write_text('{"services":{"main":{}}}')
            environment.resources_path = path
            with mock.patch.dict('os.environ', {'CUDA_VISIBLE_DEVICES': 'GPU-' + 'a' * 32,
                                               'GPU_SCHEDULER_JOB_DIR': directory}), \
                    mock.patch('tools.gpu_scheduler.resource_limits.docker_overrides', return_value=limits):
                environment._write_resources_compose_file()
            main = json.loads(path.read_text())['services']['main']
            self.assertEqual(main['mem_limit'], 8589934592)
            self.assertEqual(main['deploy']['resources']['limits'],
                             {'memory': '8589934592', 'cpus': '2.0'})
            self.assertEqual(main['cgroup_parent'], 'research-job.slice')

    async def test_solver_cannot_mount_verifier_directory_or_writable_reward(self):
        environment = self.provider()
        self.assertEqual(await environment.start(False), 'started')
        self.assertEqual([m['target'] for m in environment.started_mounts], ['/workspace'])
        environment._apply_network_policy.assert_awaited_once_with(environment.network_policy)
        environment.exec.assert_not_awaited()

    async def test_trusted_verifier_retains_its_private_output_mount(self):
        environment = self.provider(trusted=True)
        await environment.start(False)
        self.assertEqual([m['target'] for m in environment.started_mounts],
                         ['/logs/verifier', '/logs/verifier/reward.json', '/workspace'])
        environment.exec.assert_awaited_once_with(
            'chown root:root /logs/verifier && chmod 700 /logs/verifier', user='root')

    async def test_completion_attests_actual_container_ids_after_start(self):
        contract = {'trusted_fixture': True}
        environment = self.provider(contract=contract)
        with mock.patch('tools.research_handoff.core.completion.attest_docker_isolation') as attest:
            await environment.start(False)
        environment._run_docker_compose_command.assert_awaited_once_with(['ps', '-q', 'main'])
        attest.assert_called_once_with(contract, ['a' * 64], docker_host='unix:///private/docker.sock',
                                       public_image_digest=environment.public_image_digest)
