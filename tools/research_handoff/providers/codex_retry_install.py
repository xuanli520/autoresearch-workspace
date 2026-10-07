"""Install the shared request retry layer into a fresh Harbor Agent container."""
import json
import math
from pathlib import Path
import shlex
import tempfile

from .codex_transport import transport_flags


async def install_retry(agent, environment, transport, max_seconds):
    if transport is None:
        return
    transport_flags(transport)
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("model transport requires the original positive iteration timeout")
    config = {"request_max_retries": 8, "stream_max_retries": 8, "stream_idle_timeout_ms": 90000, **transport,
              "max_seconds": max_seconds, "log_path": "/logs/agent/model-transport.jsonl"}
    directory = "/usr/local/lib/autoresearch-model"
    source = Path(__file__).with_name("codex_retry.py")
    result = await agent.exec_as_root(environment, "install -d -m 755 " + directory)
    if result.return_code:
        raise RuntimeError("cannot create managed model transport directory")
    await environment.upload_file(source, directory + "/codex_retry.py")
    with tempfile.TemporaryDirectory(prefix="codex-retry-install-") as tmp:
        policy = Path(tmp) / "policy.json"
        policy.write_text(json.dumps(config))
        await environment.upload_file(policy, directory + "/policy.json")
        launcher = Path(tmp) / "codex"
        launcher.write_text("#!/bin/sh\nexec python3 " + directory + "/codex_retry.py --config " + directory
                            + '/policy.json --binary /usr/local/bin/codex-real -- "$@"\n')
        # This installation only runs in a new container, after binary hash verification.
        checked = await agent.exec_as_root(environment, "test ! -e /usr/local/bin/codex-real && mv /usr/local/bin/codex /usr/local/bin/codex-real")
        if checked.return_code:
            raise RuntimeError("native Codex retry installation is not a fresh container")
        await environment.upload_file(launcher, "/usr/local/bin/codex")
    checked = await agent.exec_as_root(environment, "chown -R root:root " + directory
                                        + " /usr/local/bin/codex && chmod 555 /usr/local/bin/codex && chmod 444 "
                                        + shlex.quote(directory + "/codex_retry.py") + " " + shlex.quote(directory + "/policy.json"))
    if checked.return_code:
        raise RuntimeError("managed model transport protection failed")
    checked = await agent.exec_as_agent(environment, "umask 077; : >> /logs/agent/model-transport.jsonl")
    if checked.return_code:
        raise RuntimeError("managed model transport audit log is not writable by the Agent")
