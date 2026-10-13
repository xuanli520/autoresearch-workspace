"""Run executors. GPU work goes only through tools.gpu_scheduler blocking submit (design §1)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUNNER = Path(__file__).resolve().parent / "runner.py"
INFRA_STATES = {"INFEASIBLE", "EXPIRED", "UNKNOWN", "CANCELLED", "CANCELLING"}
WAIT_STATES = {"QUEUED", "STARTING"}
SLACK_SECONDS = 120


class InfraBlocked(RuntimeError):
    """Scheduler/infrastructure outcome: not a method failure, never consumes k (design §4)."""

    def __init__(self, state: str, detail: str, job: dict | None = None):
        super().__init__(f"{state}: {detail}")
        self.state = state
        self.detail = detail
        self.job = job or {}


def job_runtime(max_runtime_seconds: int, rescore: bool) -> int:
    """Train + score (+ rescore) share one job; reserve the declared budget plus fixed slack."""
    return int(max_runtime_seconds) + SLACK_SECONDS * (2 if rescore else 1)


class LocalExecutor:
    """CPU-only executor for dry runs. Refuses nothing itself; the engine only allows it when declared at init."""

    kind = "local_cpu"

    def __init__(self, config: dict):
        self.config = config

    def execute(self, plan_path: Path, plan: dict, on_state=None) -> dict:
        timeout = job_runtime(plan["max_runtime_seconds"], plan.get("rescore", False)) + 30
        proc = subprocess.run([sys.executable, "-I", "-B", str(RUNNER), str(plan_path)],
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"runner failed exit={proc.returncode}: {proc.stderr[-2000:]}")
        return {"executor": self.kind, "job": None}


class SchedulerExecutor:
    """Submit the stage runner as one gpu_scheduler job and block until a terminal state."""

    kind = "gpu_scheduler"

    def __init__(self, config: dict, client_factory=None):
        self.config = config
        self._client_factory = client_factory

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory(self.config)
        from tools.gpu_scheduler import Client  # imported lazily: only needed for real GPU runs
        return Client(self.config["scheduler_root"])

    def spec(self, plan_path: Path, plan: dict, template: dict, deadline_at: str) -> dict:
        spec = {k: v for k, v in template.items()}
        spec.update({
            "request_id": plan["request_id"],
            "command": [self.config.get("python", "/usr/bin/python3"), "-I", "-B", str(RUNNER), str(plan_path)],
            "cwd": plan["cwd"],
            "max_runtime_seconds": job_runtime(plan["max_runtime_seconds"], plan.get("rescore", False)),
            "deadline_at": deadline_at,
        })
        return spec

    def execute(self, plan_path: Path, plan: dict, on_state=None, *, template: dict, deadline_at: str) -> dict:
        from tools.gpu_scheduler.common import JobWaitInterrupted, JobWaitTimeout
        from tools.gpu_scheduler.client import SchedulerError

        spec = self.spec(plan_path, plan, template, deadline_at)
        if spec["max_runtime_seconds"] > 43200:
            raise InfraBlocked("INFEASIBLE", "job runtime exceeds 43200s scheduler cap")
        client = self._client()
        last = {"state": None}

        def on_update(job):
            state = job.get("state")
            if state != last["state"]:
                last["state"] = state
                if on_state is not None:
                    on_state(job)

        try:
            job = client.submit(spec, on_update=on_update)
        except (JobWaitTimeout, JobWaitInterrupted) as exc:
            job = getattr(exc, "job", None) or {}
            raise InfraBlocked("UNKNOWN", f"wait interrupted ({type(exc).__name__}); query original request", job) from exc
        except SchedulerError as exc:
            raise InfraBlocked("UNKNOWN", f"scheduler error code={exc.code}: {exc}") from exc
        state = job.get("state")
        if state in INFRA_STATES or job.get("reconciling"):
            raise InfraBlocked(state or "UNKNOWN", job.get("reason") or "scheduler infrastructure outcome", job)
        return {"executor": self.kind, "job": {k: job.get(k) for k in ("id", "request_id", "state", "exit_code", "session_id",
                                                                     "origin_session_id", "reason")}}


def make_executor(config: dict, client_factory=None):
    if config["kind"] == "local_cpu":
        return LocalExecutor(config)
    if config["kind"] == "gpu_scheduler":
        return SchedulerExecutor(config, client_factory)
    raise ValueError(f"unknown executor {config['kind']!r}")
