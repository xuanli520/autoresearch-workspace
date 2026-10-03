"""Explicit Responses transport for compatible non-OpenAI model endpoints."""
import json
import shlex
from urllib.parse import urlsplit


def transport_flags(config):
    if config is None:
        return ""
    allowed = {"base_url", "request_max_retries", "stream_max_retries", "stream_idle_timeout_ms"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("invalid HTTPS Responses transport configuration")
    url = config.get("base_url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("model transport requires a credential-free HTTPS base URL")
    provider = {"name": "Managed HTTPS Responses", "base_url": url, "env_key": "OPENAI_API_KEY",
                "wire_api": "responses", "supports_websockets": False,
                "request_max_retries": 1, "stream_max_retries": 1, "stream_idle_timeout_ms": 90000}
    for key in allowed - {"base_url"}:
        if key in config:
            value = config[key]
            maximum = 300000 if key == "stream_idle_timeout_ms" else 5
            minimum = 1000 if key == "stream_idle_timeout_ms" else 0
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"invalid model transport {key}")
            provider[key] = value
    table = "{" + ",".join(key + "=" + json.dumps(value) for key, value in provider.items()) + "}"
    return " -c " + shlex.quote('model_provider="research_https"') + " -c " + shlex.quote("model_providers.research_https=" + table)
