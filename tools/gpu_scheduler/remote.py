"""SSH control and blocking job requests; no local queue or mutation retries."""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .client import Client, SchedulerError, _REQUEST_DEFAULT
from .common import MAX_MESSAGE, JobWaitInterrupted, JobWaitTimeout, fields, number, read_json


class RemoteClient(Client):
    def __init__(self, config_file, *, session_id=None):
        config_path = Path(config_file).resolve()
        config = read_json(config_path)
        fields(config, {"version", "auth_file", "python", "cli", "root"},
               {"timeout_seconds", "wait_timeout_seconds"})
        if type(config["version"]) is not int or config["version"] != 1:
            raise ValueError("remote config requires version=1")
        for key in ("python", "cli", "root"):
            value = config[key]
            if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
                raise ValueError(f"remote {key} must be an absolute path")
        if not isinstance(config["auth_file"], str) or not config["auth_file"]:
            raise ValueError("auth_file must be a path")
        self.auth_file = (config_path.parent / config["auth_file"]).resolve()
        self.config = config
        self.timeout = number(config.get("timeout_seconds", 20), "timeout_seconds", 1, 60)
        self.wait_timeout = number(config.get("wait_timeout_seconds", 43210),
                                   "wait_timeout_seconds", 1, 43210)
        self.session_id = session_id or self._request("hello")["session_id"]

    def _wait_transport_timeout(self, timeout):
        if timeout is None:
            return self.wait_timeout
        return min(self.wait_timeout, max(self.timeout, timeout + 5))

    def _request(self, op, *, request_timeout=_REQUEST_DEFAULT, **kwargs):
        from tools.gpu_monitor import monitor

        request = dict(op=op, session_id=getattr(self, "session_id", None), **kwargs)
        payload = json.dumps(request, allow_nan=False) + "\n"
        if len(payload.encode()) > MAX_MESSAGE:
            raise SchedulerError("request too large")
        host = monitor.apply_auth({"connect_timeout_seconds": min(10, int(self.timeout))},
                                  monitor.load_auth(self.auth_file))
        # Freeze host identity for this Client, even if its auth file is edited.
        identity = (host["hostname"], host["port"], host["user"])
        if hasattr(self, "host_identity") and self.host_identity != identity:
            raise SchedulerError("SSH target changed; create a new client after checking its session")
        self.host_identity = identity
        command = shlex.join([self.config["python"], "-B", self.config["cli"], "rpc",
                              "--root", self.config["root"]])
        transport_timeout = self.timeout if request_timeout is _REQUEST_DEFAULT else request_timeout
        try:
            result = monitor.run_ssh(host, monitor.ssh_command(host, command, password_auth=True),
                                     payload, transport_timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SchedulerError(f"SSH {type(exc).__name__}; outcome UNKNOWN; query before retrying") from exc
        if result.returncode:
            # No raw command/environment/credential echo on an authentication error.
            hint = monitor.ssh_error_hint(result.stderr, host)
            raise SchedulerError(f"SSH/RPC failed (exit {result.returncode}); outcome UNKNOWN. {hint}")
        if len(result.stdout.encode()) > MAX_MESSAGE:
            raise SchedulerError("SSH response too large; outcome UNKNOWN")
        try:
            reply = json.loads(result.stdout)
        except ValueError as exc:
            raise SchedulerError("invalid SSH response; outcome UNKNOWN") from exc
        if not isinstance(reply, dict) or not reply.get("ok"):
            if isinstance(reply, dict) and reply.get("code") == "WAIT_TIMEOUT":
                raise JobWaitTimeout(reply.get("error", "client wait timed out; the job was NOT cancelled"),
                                     reply["job"])
            if isinstance(reply, dict) and reply.get("code") == "WAIT_INTERRUPTED":
                raise JobWaitInterrupted(reply["reason"], job=reply["job"])
            raise SchedulerError(reply.get("error", "remote request failed") if isinstance(reply, dict)
                                 else "invalid SSH response")
        return reply["result"]
