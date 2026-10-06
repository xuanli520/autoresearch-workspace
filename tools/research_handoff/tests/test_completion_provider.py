"""Provider hook tests with an explicit Harbor API stub; no Docker is started."""
import importlib
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
            def _write_resources_compose_file(self):
                return self.resources_path

            async def start(self, force_build):
                self.started_mounts = list(self._mounts)
                return 'started'
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

    def test_device_request_keeps_exact_scheduler_uuid(self):
        environment = self.provider()
        environment.gpu_attachment = 'device-request'
        environment.task_env_config = types.SimpleNamespace(gpus=1)
        gpu = 'GPU-' + 'b' * 32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.json'
            path.write_text('{"services":{"main":{}}}')
            environment.resources_path = path
            with mock.patch.dict('os.environ', {'CUDA_VISIBLE_DEVICES': gpu}):
                environment._write_resources_compose_file()
            main = json.loads(path.read_text())['services']['main']
            self.assertEqual(main['deploy']['resources']['reservations']['devices'], [
                {'driver': 'nvidia', 'device_ids': [gpu], 'capabilities': ['gpu']}])
            self.assertNotIn('devices', main)

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
