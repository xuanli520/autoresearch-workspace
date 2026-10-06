"""Single-writer in-memory queue; no database and no automatic replay."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .common import (TERMINAL, WAITABLE_TERMINAL, PROCESS_SOURCE, JobWaitInterrupted,
                     JobWaitTimeout, atomic_json, check_storage, processes, read_json,
                     validate_job, validate_wait_timeout)
from .resources import choose_gpu, process_map
from .fairness import order_queue, projected_start, protected, safe_backfill


class Scheduler:
    def __init__(self, config: dict[str, Any], lock_fd: int) -> None:
        self.config, self.lock_fd = config, lock_fd
        self.id = uuid.uuid4().hex
        self.root = Path(config["root"])
        self.directory = self.root / "sessions" / self.id
        self.directory.mkdir(parents=True)
        self.jobs, self.requests = {}, {}
        self._job_events = {}
        self._spawn_tasks: set[asyncio.Task[None]] = set()
        self._recovery_tasks: dict[str, asyncio.Task[tuple[dict[str, Any], bool]]] = {}
        self.snapshot = None
        self.probe_error = None
        self.quarantined = set()
        self.closing = False
        self.shutdown_reason = None
        self.owner_dispatch = {}
        self.dispatch_count = 0
        if config.get("persistent"):
            self.deadline_epoch = None
            self.deadline_mono = None
        else:
            seconds = config["service_seconds"]
            lease = config.get("infrastructure_lease")
            if lease is not None:
                seconds = min(seconds, lease["deadline_epoch"] - time.time())
                if seconds <= 0:
                    raise ValueError("infrastructure lease has expired")
            self.deadline_epoch = time.time() + seconds
            self.deadline_mono = time.monotonic() + seconds
        sources = sorted(Path(__file__).parent.glob("*.py")) + [PROCESS_SOURCE, PROCESS_SOURCE.with_name("__init__.py")]
        atomic_json(self.directory / "service.json", {
            "session_id": self.id, "pid": os.getpid(), "at": time.time(), "config": config,
            "identity": {"pid": os.getpid(), "start_ticks": processes.process_start_ticks(os.getpid()),
                         "boot_id": processes.boot_id()},
            "deadline_epoch": self.deadline_epoch,
            "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        })

    def active(self) -> list[dict[str, Any]]:
        return [j for j in self.jobs.values() if j["state"] not in TERMINAL | {"QUEUED"}]

    def view(self, job: dict[str, Any]) -> dict[str, Any]:
        result = {k: job[k] for k in ("id", "state", "reason", "gpu_uuid", "submitted_at", "started_at",
                                      "finished_at", "bypasses", "directory", "revision")}
        result.update(session_id=self.id, request_id=job["spec"]["request_id"], owner=job["spec"]["owner"],
                      resources={k: job["spec"][k] for k in ("memory_mib", "compute_units", "cpu_cores", "ram_mib")},
                      exit=job.get("exit"))
        if job["state"] == "QUEUED" and self.probe_error is not None:
            result["telemetry_error"] = self.probe_error
        if job["state"] == "QUEUED":
            now = time.monotonic()
            cutoff = job["submitted_at"] + job["spec"]["queue_timeout_seconds"]
            if job["deadline_epoch"] is not None:
                cutoff = min(cutoff, job["deadline_epoch"] - job["spec"]["max_runtime_seconds"])
            estimate = job.get("projected_start_mono")
            estimate_epoch = None if estimate is None else time.time() + max(0, estimate - now)
            result["queue"] = {"wait_seconds": max(0, now - job["submitted_mono"]),
                               "protected": protected(job, self.config, now),
                               "latest_start_epoch": cutoff, "projected_start_epoch": estimate_epoch,
                               "deadline_risk": None if estimate_epoch is None else estimate_epoch >= cutoff,
                               "projection_scope": "active_job_runtime_bounds; external_usage_may_change"}
        return result

    def transition(self, job: dict[str, Any], state: str, reason: str) -> None:
        if (job["state"], job["reason"]) == (state, reason):
            return
        job.update(state=state, reason=reason)
        job["revision"] += 1
        if state in TERMINAL:
            job["finished_at"] = time.time()
        check_storage(self.config, job["directory"])
        record = self.view(job)
        atomic_json(Path(job["directory"]) / "status.json", record)
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": time.time(), **record}, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        event = self._job_events[job["id"]]
        event.set()
        self._job_events[job["id"]] = asyncio.Event()

    def submit(self, raw: dict[str, Any]) -> dict[str, Any]:
        if self.closing:
            raise ValueError("scheduler is stopping")
        spec = validate_job(raw, self.config)
        request = spec["request_id"]
        if request in self.requests:
            old = self.jobs[self.requests[request]]
            if old["spec"] != spec:
                raise ValueError("request_id already exists with a different spec")
            return self.view(old)
        if len(self.jobs) >= self.config["max_jobs"]:
            raise ValueError("session max_jobs reached; history is retained for idempotency")
        requested_deadline = spec.get("deadline_epoch", self.deadline_epoch)
        deadline = (None if requested_deadline is None else
                    min(requested_deadline, self.deadline_epoch) if self.deadline_epoch is not None else requested_deadline)
        if deadline is not None and deadline - time.time() < spec["max_runtime_seconds"]:
            raise ValueError("remaining parent/service budget cannot fit max_runtime_seconds")
        check_storage(self.config, self.directory)
        if shutil.disk_usage(self.directory).free < self.config["min_free_disk_mib"] * 1024**2:
            raise ValueError("insufficient free disk space")
        job_id = uuid.uuid4().hex
        directory = self.directory / "jobs" / job_id
        directory.mkdir(parents=True)
        atomic_json(directory / "spec.json", spec)
        job = dict(id=job_id, spec=spec, state=None, reason=None, gpu_uuid=None,
                   submitted_at=time.time(), submitted_mono=time.monotonic(),
                   deadline_epoch=deadline,
                   deadline_mono=(None if deadline is None else
                                  time.monotonic() + max(0, deadline - time.time())),
                   started_at=None, finished_at=None, bypasses=0, revision=0, directory=str(directory),
                   token=uuid.uuid4().hex, process=None)
        self.jobs[job_id] = job
        self.requests[request] = job_id
        self._job_events[job_id] = asyncio.Event()
        self.transition(job, "QUEUED", "awaiting_dispatch")
        return self.view(job)

    async def wait(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        timeout = validate_wait_timeout(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job["state"] in WAITABLE_TERMINAL:
                return self.view(job)
            if self.closing:
                raise JobWaitInterrupted(self.shutdown_reason, job=self.view(job))
            event = self._job_events[job_id]
            if deadline is None:
                await event.wait()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JobWaitTimeout("client wait timed out; the job was NOT cancelled",
                                         self.view(job))
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except asyncio.TimeoutError as exc:
                    if job["state"] in WAITABLE_TERMINAL:
                        return self.view(job)
                    if self.closing:
                        raise JobWaitInterrupted(self.shutdown_reason, job=self.view(job)) from exc
                    raise JobWaitTimeout("client wait timed out; the job was NOT cancelled",
                                         self.view(job)) from exc

    def get(self, job_id: str | None = None, request_id: str | None = None) -> dict[str, Any]:
        if request_id is not None:
            job_id = self.requests.get(request_id)
        if job_id not in self.jobs:
            raise ValueError("unknown job/request in this in-memory session; no automatic replay")
        return self.jobs[job_id]

    def cancel(self, job_id: str, reason: str = "user_cancel") -> dict[str, Any]:
        job = self.get(job_id)
        if job["state"] == "QUEUED":
            self.transition(job, "CANCELLED", reason)
        elif job["state"] not in TERMINAL | {"UNKNOWN"}:
            check_storage(self.config, job["directory"])
            atomic_json(Path(job["directory"]) / "STOP.json", {"at": time.time(), "reason": reason})
            self.transition(job, "CANCELLING", reason)
        return self.view(job)

    def begin_shutdown(self, reason: str = "service_stop") -> None:
        if self.closing:
            return
        self.closing = True
        self.shutdown_reason = reason
        for event in self._job_events.values():
            event.set()
        for job in self.jobs.values():
            self.cancel(job["id"], reason)

    def _prepare_launch(self, job: dict[str, Any], gpu_uuid: str) -> tuple[Path, dict[str, str]]:
        directory = Path(job["directory"])
        check_storage(self.config, directory, job["spec"]["cwd"])
        job["gpu_uuid"] = gpu_uuid
        seconds = job["spec"]["max_runtime_seconds"]
        launch = {"id": job["id"], "token": job["token"], "gpu_uuid": gpu_uuid,
                  "spec": job["spec"], "config": self.config,
                  "deadline_epoch": (min(time.time() + seconds, job["deadline_epoch"])
                                     if job["deadline_epoch"] is not None else time.time() + seconds),
                  "deadline_monotonic": (min(time.monotonic() + seconds, job["deadline_mono"])
                                          if job["deadline_mono"] is not None else time.monotonic() + seconds),
                  "at": time.time()}
        job["execution_deadline_mono"] = launch["deadline_monotonic"]
        atomic_json(directory / "launch.json", launch)
        self.transition(job, "STARTING", "executor_starting")
        env = os.environ.copy()
        env.pop("AUTORESEARCH_PROCESS_TOKEN", None)
        return directory, env

    @staticmethod
    def _spawn_worker(directory: Path, lock_fd: int, env: dict[str, str]) -> subprocess.Popen:
        with (directory / "executor.log").open("ab") as log:
            return subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).with_name("worker.py")),
                 str(directory), str(lock_fd)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                start_new_session=True, pass_fds=(lock_fd,))

    async def _spawn_worker_async(self, job: dict[str, Any], directory: Path,
                                  env: dict[str, str]) -> None:
        # Cancellation can arrive while the executor is being prepared. Keep
        # the reservation's terminal receipt authoritative and avoid spawning
        # work after a stop request when the handoff has not reached Popen yet.
        if job["state"] == "CANCELLING" or self.closing:
            job["exit"] = {"returncode": None, "cleanup_ok": True, "reason": "cancelled"}
            atomic_json(directory / "exit.json", {"at": time.time(), **job["exit"]})
            self.transition(job, "CANCELLED", "cancelled")
            return
        try:
            job["process"] = await asyncio.to_thread(self._spawn_worker, directory, self.lock_fd, env)
        except OSError as exc:
            cancelled = job["state"] == "CANCELLING"
            job["exit"] = {"returncode": None, "cleanup_ok": True,
                           "reason": "cancelled" if cancelled else "spawn_error"}
            atomic_json(directory / "exit.json", {"at": time.time(), **job["exit"]})
            self.transition(job, "CANCELLED" if cancelled else "FAILED",
                            "cancelled" if cancelled else f"spawn_error:{type(exc).__name__}")

    def launch(self, job: dict[str, Any], gpu_uuid: str) -> None:
        directory, env = self._prepare_launch(job, gpu_uuid)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                job["process"] = self._spawn_worker(directory, self.lock_fd, env)
            except OSError as exc:
                job["exit"] = {"returncode": None, "cleanup_ok": True, "reason": "spawn_error"}
                atomic_json(directory / "exit.json", {"at": time.time(), **job["exit"]})
                self.transition(job, "FAILED", f"spawn_error:{type(exc).__name__}")
        else:
            task = asyncio.create_task(self._spawn_worker_async(job, directory, env))
            self._spawn_tasks.add(task)

    def _recover_receipt(self, job: dict[str, Any]) -> dict[str, Any]:
        directory = Path(job["directory"])
        try:
            return read_json(directory / "exit.json")
        except (OSError, ValueError):
            receipt = {"reason": "executor_lost", "returncode": None,
                       "cleanup_ok": processes.terminate_scope(job["token"], 1)}
            try:
                from .container_ownership import cleanup_job
                receipt["container_cleanup"] = cleanup_job(directory)
            except Exception as exc:
                receipt["container_cleanup"] = {"cleanup_ok": False, "error": type(exc).__name__ + ": " + str(exc)}
                receipt["cleanup_ok"] = False
            atomic_json(directory / "recovery-exit.json", {"at": time.time(), **receipt})
            return receipt

    def _finish_receipt(self, job: dict[str, Any], receipt: dict[str, Any], *,
                        scope_remaining: bool | None = None) -> None:
        job["exit"] = receipt
        if scope_remaining is None:
            scope_remaining = bool(processes.scope_members(job["token"]))
        if not receipt.get("cleanup_ok") or scope_remaining:
            self.quarantined.add(job["gpu_uuid"])
            self.transition(job, "UNKNOWN", "cleanup_unconfirmed_gpu_quarantined")
            return
        reason = receipt["reason"]
        state = ("TIMED_OUT" if reason == "deadline" else "CANCELLED" if reason == "cancelled"
                 else "SUCCEEDED" if reason == "process_exit" and receipt["returncode"] == 0 else "FAILED")
        self.transition(job, state, reason)

    def _recover_and_inspect(self, job: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        receipt = self._recover_receipt(job)
        return receipt, bool(processes.scope_members(job["token"]))

    def reconcile(self) -> None:
        for job in self.active():
            if job["state"] == "UNKNOWN":
                continue
            proc = job["process"]
            if proc is None:
                continue  # Resource reservation exists before async Popen completes.
            directory = Path(job["directory"])
            if proc.poll() is None:
                if job["state"] == "STARTING" and (directory / "started.json").exists():
                    job["started_at"] = read_json(directory / "started.json")["at"]
                    self.transition(job, "RUNNING", "executing")
                continue
            self._finish_receipt(job, self._recover_receipt(job))

    async def reconcile_async(self) -> None:
        """Reconcile without running process/container cleanup in the event loop."""
        for task in list(self._spawn_tasks):
            if task.done():
                self._spawn_tasks.remove(task)
                task.result()
        for job in self.active():
            if job["state"] == "UNKNOWN":
                continue
            proc = job["process"]
            if proc is None:
                continue
            directory = Path(job["directory"])
            if proc.poll() is None:
                if job["state"] == "STARTING" and (directory / "started.json").exists():
                    job["started_at"] = read_json(directory / "started.json")["at"]
                    self.transition(job, "RUNNING", "executing")
                continue
            task = self._recovery_tasks.get(job["id"])
            if task is None:
                self._recovery_tasks[job["id"]] = asyncio.create_task(
                    asyncio.to_thread(self._recover_and_inspect, job))
            elif task.done():
                del self._recovery_tasks[job["id"]]
                receipt, scope_remaining = task.result()
                self._finish_receipt(job, receipt, scope_remaining=scope_remaining)

    async def tick_async(self, snapshot: dict[str, Any] | None = None,
                         error: str | None = None) -> None:
        await self.reconcile_async()
        pid_sets = await asyncio.to_thread(process_map, self.active(), snapshot)
        self.tick(snapshot, error, reconciled=True, pid_sets=pid_sets)

    async def settle_background(self) -> None:
        """Retain references and the inherited service lock until spawn completes."""
        if self._spawn_tasks:
            await asyncio.gather(*list(self._spawn_tasks))
            self._spawn_tasks.clear()
        if self._recovery_tasks:
            await asyncio.gather(*list(self._recovery_tasks.values()))
            await self.reconcile_async()

    def tick(self, snapshot: dict[str, Any] | None = None, error: str | None = None, *,
             reconciled: bool = False, pid_sets: dict[str, set[int]] | None = None) -> None:
        self.snapshot, self.probe_error = snapshot, error
        if not reconciled:
            self.reconcile()
        if (self.deadline_mono is not None and time.monotonic() >= self.deadline_mono) or \
                (self.deadline_epoch is not None and time.time() >= self.deadline_epoch):
            self.begin_shutdown("service_deadline")
        if self.closing:
            return
        check_storage(self.config, self.directory)
        if shutil.disk_usage(self.directory).free < self.config["min_free_disk_mib"] * 1024**2:
            self.begin_shutdown("disk_space")
            return
        queue = [j for j in self.jobs.values() if j["state"] == "QUEUED"]
        for job in queue:
            remaining = (min(job["deadline_epoch"] - time.time(), job["deadline_mono"] - time.monotonic())
                         if job["deadline_epoch"] is not None else float("inf"))
            if time.monotonic() - job["submitted_mono"] >= job["spec"]["queue_timeout_seconds"]:
                self.transition(job, "EXPIRED", "queue_timeout")
            elif remaining < job["spec"]["max_runtime_seconds"]:
                self.transition(job, "EXPIRED", "insufficient_remaining_budget")
        queue = [j for j in queue if j["state"] == "QUEUED"]
        # Inspect /proc once per tick, not once for every candidate/forecast.
        pid_sets = process_map(self.active(), snapshot) if pid_sets is None else pid_sets
        while queue:
            now = time.monotonic()
            active = self.active()
            ordered = order_queue(queue, active, self.config, self.owner_dispatch, now)
            skipped = []
            for job in ordered:
                gpu, reason = choose_gpu(job["spec"], active, self.config, snapshot,
                                         self.quarantined, pid_sets=pid_sets)
                blockers = [j for j in skipped if protected(j, self.config, now)]
                if gpu is not None and blockers:
                    # A different eligible GPU may avoid delaying a reservation.
                    choices = [gpu] + [g["uuid"] for g in self.config["gpus"] if g["uuid"] != gpu
                                       and job["spec"].get("gpu_uuid", g["uuid"]) == g["uuid"]]
                    gpu = None
                    for candidate_gpu in choices:
                        fits, _ = choose_gpu({**job["spec"], "gpu_uuid": candidate_gpu}, active,
                                             self.config, snapshot, self.quarantined, pid_sets=pid_sets)
                        allow_backfill = self.config["scheduling_policy"] == "fair_share" and self.config["max_bypass"] > 0
                        if allow_backfill and fits is not None and safe_backfill(
                                job, fits, blockers, active, self.config,
                                snapshot, self.quarantined, pid_sets, now):
                            gpu = fits
                            break
                    if gpu is None:
                        reason = "waiting_for_protected_earlier_job"
                if gpu is None:
                    job["projected_start_mono"] = projected_start(job["spec"], active, self.config,
                                                                  snapshot, self.quarantined, pid_sets, now)
                    self.transition(job, "QUEUED", reason)
                    skipped.append(job)
                    continue
                self.launch(job, gpu)
                self.dispatch_count += 1
                self.owner_dispatch[job["spec"]["owner"]] = self.dispatch_count
                # Count every overtaken older request, including fairness reordering.
                for earlier in queue:
                    if earlier is not job and earlier["submitted_mono"] < job["submitted_mono"]:
                        earlier["bypasses"] += 1
                queue = [j for j in queue if j["state"] == "QUEUED"]
                break  # Recompute owner shares and protection after every allocation.
            else:
                break

    def status(self) -> dict[str, Any]:
        return {"session_id": self.id, "stopping": self.closing, "max_running": self.config["max_running"],
                "active_count": len(self.active()), "queued_count": sum(j["state"] == "QUEUED" for j in self.jobs.values()),
                "quarantined_gpus": sorted(self.quarantined), "telemetry": self.snapshot,
                "telemetry_error": self.probe_error, "deadline_epoch": self.deadline_epoch,
                "persistent": bool(self.config.get("persistent")),
                "scheduling_policy": self.config["scheduling_policy"],
                "starvation_seconds": self.config["starvation_seconds"],
                "directory": str(self.directory), "local_test": self.config["local_test"]}
