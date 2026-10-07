import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tools.research_handoff.providers.codex_retry_install import install_retry


class InstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_container_receives_protected_policy_and_wrapper(self):
        uploaded = {}
        async def upload(source, destination):
            uploaded[destination] = Path(source).read_text()
        environment = SimpleNamespace(upload_file=AsyncMock(side_effect=upload))
        agent = SimpleNamespace(exec_as_root=AsyncMock(return_value=SimpleNamespace(return_code=0)),
                                exec_as_agent=AsyncMock(return_value=SimpleNamespace(return_code=0)))
        await install_retry(agent, environment, {"base_url": "https://model.example/v1"}, 1500)
        policy = json.loads(uploaded["/usr/local/lib/autoresearch-model/policy.json"])
        self.assertEqual(policy["request_max_retries"], 8)
        self.assertEqual(policy["stream_max_retries"], 8)
        self.assertEqual(policy["stream_idle_timeout_ms"], 90000)
        self.assertEqual(policy["max_seconds"], 1500)
        self.assertIn('"$@"', uploaded["/usr/local/bin/codex"])
        commands = [call.args[1] for call in agent.exec_as_root.call_args_list]
        self.assertTrue(any("test ! -e /usr/local/bin/codex-real" in command for command in commands))
        self.assertTrue(any("chmod 444" in command and "root:root" in command for command in commands))

    async def test_none_keeps_native_transport_and_invalid_config_writes_nothing(self):
        environment = SimpleNamespace(upload_file=AsyncMock())
        agent = SimpleNamespace(exec_as_root=AsyncMock())
        await install_retry(agent, environment, None, 1500)
        for transport, timeout in (({"base_url": "https://model.example", "request_max_retries": 9}, 1500),
                                   ({"base_url": "https://model.example"}, 0)):
            with self.assertRaises(ValueError):
                await install_retry(agent, environment, transport, timeout)
        environment.upload_file.assert_not_awaited()
        agent.exec_as_root.assert_not_awaited()
