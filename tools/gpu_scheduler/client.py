"""Small synchronous SDK. Request failures never replay mutations automatically."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

from .common import MAX_MESSAGE, WAITABLE_TERMINAL, JobWaitInterrupted, JobWaitTimeout, validate_wait_timeout


_REQUEST_DEFAULT = object()


class SchedulerError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class Client:
    def __init__(self, root: str | Path, *, session_id: str | None = None, timeout: float = 10) -> None:
        self.socket_path = str(Path(root).resolve() / "scheduler.sock")
        self.timeout = timeout
        self.session_id = session_id or self._request("hello")["session_id"]

    def _wait_transport_timeout(self, timeout: float | None) -> float | None:
        if timeout is None:
            return None
        return max(self.timeout, timeout + 5)

    def _request(self, op: str, *, request_timeout: Any = _REQUEST_DEFAULT, **kwargs: Any) -> Any:
        request = dict(op=op, session_id=getattr(self, "session_id", None), **kwargs)
        data = json.dumps(request, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_MESSAGE:
            raise SchedulerError("request too large")
        transport_timeout = self.timeout if request_timeout is _REQUEST_DEFAULT else request_timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(data)
                connection.settimeout(transport_timeout)
                with connection.makefile("rb") as stream:
                    response = stream.readline(MAX_MESSAGE + 1)
            if len(response) > MAX_MESSAGE or not response.endswith(b"\n"):
                raise SchedulerError("invalid or incomplete response; outcome UNKNOWN")
            result = json.loads(response)
        except (OSError, ValueError) as exc:
            raise SchedulerError(f"connection/response failure ({type(exc).__name__}); outcome UNKNOWN; query before retrying") from exc
        if not result.get("ok"):
            if result.get("code") == "WAIT_TIMEOUT":
                raise JobWaitTimeout(result.get("error", "client wait timed out; the job was NOT cancelled"),
                                     result["job"])
            if result.get("code") == "WAIT_INTERRUPTED":
                raise JobWaitInterrupted(result["reason"], job=result["job"])
            raise SchedulerError(result.get("error", "request failed"), code=result.get("code", "REJECTED"))
        return result["result"]

    def ensure(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Persist an idempotent intent; an existing execution is never replayed."""
        return self.submit_async(spec)

    def submit(self, spec: dict[str, Any], *, timeout: float | None = None, on_update=None) -> dict[str, Any]:
        """Validate and enqueue, then block until completion or a resumable interruption."""
        timeout = validate_wait_timeout(timeout)
        try:
            started = time.monotonic()
            job = self.ensure(spec)
            return self.wait(job["id"], timeout=None if timeout is None else max(0, timeout - (time.monotonic() - started)),
                             on_update=on_update)
        except KeyboardInterrupt as exc:
            raise JobWaitInterrupted("client_interrupted", session_id=self.session_id,
                                     request_id=spec.get("request_id")) from exc
        except JobWaitInterrupted as exc:
            if exc.request_id is None:
                exc.request_id = spec.get("request_id")
            raise

    def submit_async(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Enqueue without waiting, for progress tracking or multi-job orchestration."""
        return self._request("submit", spec=spec, wait=False)

    def status(self) -> dict[str, Any]:
        return self._request("status")

    def jobs(self) -> list[dict[str, Any]]:
        return self._request("list")

    def get(self, job_id: str | None = None, *, request_id: str | None = None) -> dict[str, Any]:
        return self._request("get", id=job_id, request_id=request_id)

    def cancel(self, job_id: str, reason: str = "user_cancel") -> dict[str, Any]:
        return self._request("cancel", id=job_id, reason=reason)

    def stop(self) -> dict[str, Any]:
        return self._request("stop")

    def wait(self, job_id: str, *, timeout: float | None = None, interval: float = 1, on_update=None) -> dict[str, Any]:
        """Block on server events; interval is retained only for caller compatibility."""
        del interval
        timeout = validate_wait_timeout(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        revision = None
        job = None
        try:
            while True:
                try:
                    remaining = None if deadline is None else max(0, deadline - time.monotonic())
                    job = self._request("watch", id=job_id, revision=revision,
                                        timeout=10 if remaining is None else min(10, remaining),
                                        request_timeout=max(15, self.timeout))
                except SchedulerError as error:
                    if error.code is not None:
                        raise
                    if deadline is not None and time.monotonic() >= deadline:
                        if job is not None:
                            raise JobWaitTimeout("client wait timed out; the job was NOT cancelled", job)
                        raise
                    # Reconnect only a read of the same accepted job. No mutation.
                    if on_update is not None and job is not None:
                        on_update({**job, "state": "UNKNOWN", "reason": "transport_reconciliation", "reconciling": True})
                    if job is not None and job.get("deadline_epoch") is not None and time.time() >= job["deadline_epoch"] + 10:
                        raise JobWaitInterrupted("deadline_during_reconciliation", job=job) from error
                    time.sleep(min(1, max(0, deadline - time.monotonic())) if deadline else 1)
                    continue
                revision = job["revision"]
                self.session_id = job["session_id"]
                if on_update is not None:
                    on_update(job)
                if job["state"] in WAITABLE_TERMINAL and not job.get("reconciling"):
                    return job
                if deadline is not None and time.monotonic() >= deadline:
                    raise JobWaitTimeout("client wait timed out; the job was NOT cancelled", job)
        except KeyboardInterrupt as exc:
            raise JobWaitInterrupted("client_interrupted", session_id=self.session_id,
                                     job_id=job_id) from exc
