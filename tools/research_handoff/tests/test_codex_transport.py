import shlex
import tomllib
import unittest

from tools.research_handoff.providers.codex_transport import transport_flags


class TransportTests(unittest.TestCase):
    def test_explicit_https_provider_disables_websockets_and_bounds_retries(self):
        argv = shlex.split(transport_flags({"base_url": "https://model.example/api/v3"}))
        config = tomllib.loads("\n".join(argv[index + 1] for index in range(0, len(argv), 2)))
        provider = config["model_providers"][config["model_provider"]]
        self.assertFalse(provider["supports_websockets"])
        self.assertEqual(provider["wire_api"], "responses")
        self.assertEqual(provider["env_key"], "OPENAI_API_KEY")
        self.assertEqual(provider["request_max_retries"], 8)
        self.assertEqual(provider["stream_max_retries"], 8)
        self.assertEqual(provider["stream_idle_timeout_ms"], 90000)
        self.assertEqual(provider["base_url"], "https://model.example/api/v3")

    def test_explicit_eight_retries(self):
        argv = shlex.split(transport_flags({"base_url": "https://model.example",
                                           "request_max_retries": 8, "stream_max_retries": 8,
                                           "stream_idle_timeout_ms": 90000}))
        config = tomllib.loads("\n".join(argv[index + 1] for index in range(0, len(argv), 2)))
        provider = config["model_providers"][config["model_provider"]]
        self.assertEqual(provider["request_max_retries"], 8)
        self.assertEqual(provider["stream_max_retries"], 8)
        self.assertEqual(provider["stream_idle_timeout_ms"], 90000)

    def test_nine_retries_is_rejected(self):
        for key in ("request_max_retries", "stream_max_retries"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                transport_flags({"base_url": "https://model.example", key: 9})

    def test_default_keeps_existing_transport(self):
        self.assertEqual(transport_flags(None), "")

    def test_invalid_or_secret_bearing_configuration_is_rejected(self):
        for config in ({"base_url": "http://model.example"}, {"base_url": "https://key@model.example"},
                       {"base_url": "https://model.example?key=secret"}, {"base_url": "https://model.example", "api_key": "secret"},
                       {"base_url": "https://model.example", "stream_max_retries": True},
                       {"base_url": "https://model.example", "stream_idle_timeout_ms": 999999}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                transport_flags(config)
