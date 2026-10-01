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

from .common import (TERMINAL, WAITABLE_TERMINAL, PROCESS_SOURCE, JobWaitInterrupted,
                     JobWaitTimeout, atomic_json, check_storage, processes, read_json,
                     validate_job, validate_wait_timeout)
from .resources import choose_gpu


class Scheduler:
    def __init__(self, config, lock_fd):
        self.config, self.lock_fd = config, lock_fd
        self.id = uuid.uuid4().hex
        self.root = Path(config["root"])
        self.directory = self.root / "sessions" / self.id
        self.directory.mkdir(parents=True)
        self.jobs, self.requests = {}, {}
        self._job_events = {}
        self.snapshot = None
        self.probe_error = None
        self.quarantined = set()
        self.closing = False
        self.shutdown_reason = None
        self.deadline_epoch = time.time() + config["service_seconds"]
        self.deadline_mono = time.monotonic() + config["service_seconds"]
        sources = sorted(Path(__file__).parent.glob("*.py")) + [PROCESS_SOURCE]
        atomic_json(self.directory / "service.json", {
            "session_id": self.id, "pid": os.getpid(), "at": time.time(), "config": config,
            "deadline_epoch": self.deadline_epoch,
            "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        })

    def active(self):
        return [j for j in self.jobs.values() if j["state"] not in TERMINAL | {"QUEUED"}]

    def view(self, job):
        result = {k: job[k] for k in ("id", "state", "reason", "gpu_uuid", "submitted_at", "started_at",
                                      "finished_at", "bypasses", "directory", "revision")}
        result.update(session_id=self.id, request_id=job["spec"]["request_id"], owner=job["spec"]["owner"],
                      resources={k: job["spec"][k] for k in ("memory_mib", "compute_units", "cpu_cores", "ram_mib")},
                      exit=job.get("exit"))
        return result

    def transition(self, job, state, reason):
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

    def submit(self, raw):
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
        deadline = min(spec.get("deadline_epoch", self.deadline_epoch), self.deadline_epoch)
        if deadline - time.time() < spec["max_runtime_seconds"]:
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
                   deadline_mono=time.monotonic() + max(0, deadline - time.time()),
                   started_at=None, finished_at=None, bypasses=0, revision=0, directory=str(directory),
                   token=uuid.uuid4().hex, process=None)
        self.jobs[job_id] = job
        self.requests[request] = job_id
        self._job_events[job_id] = asyncio.Event()
        self.transition(job, "QUEUED", "awaiting_dispatch")
        return self.view(job)

    async def wait(self, job_id, *, timeout=None):
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

    def get(self, job_id=None, request_id=None):
        if request_id is not None:
            job_id = self.requests.get(request_id)
        if job_id not in self.jobs:
            raise ValueError("unknown job/request in this in-memory session; no automatic replay")
        return self.jobs[job_id]

    def cancel(self, job_id, reason="user_cancel"):
        job = self.get(job_id)
        if job["state"] == "QUEUED":
            self.transition(job, "CANCELLED", reason)
        elif job["state"] not in TERMINAL | {"UNKNOWN"}:
            check_storage(self.config, job["directory"])
            atomic_json(Path(job["directory"]) / "STOP.json", {"at": time.time(), "reason": reason})
            self.transition(job, "CANCELLING", reason)
        return self.view(job)

    def begin_shutdown(self, reason="service_stop"):
        if self.closing:
            return
        self.closing = True
        self.shutdown_reason = reason
        for event in self._job_events.values():
            event.set()
        for job in self.jobs.values():
            self.cancel(job["id"], reason)

    def launch(self, job, gpu_uuid):
        directory = Path(job["directory"])
        check_storage(self.config, directory, job["spec"]["cwd"])
        job["gpu_uuid"] = gpu_uuid
        seconds = job["spec"]["max_runtime_seconds"]
        launch = {"id": job["id"], "token": job["token"], "gpu_uuid": gpu_uuid,
                  "spec": job["spec"], "config": self.config,
                  "deadline_epoch": min(time.time() + seconds, job["deadline_epoch"]),
                  "deadline_monotonic": min(time.monotonic() + seconds, job["deadline_mono"]),
                  "at": time.time()}
        atomic_json(directory / "launch.json", launch)
        self.transition(job, "STARTING", "executor_starting")
        env = os.environ.copy()
        env.pop("AUTORESEARCH_PROCESS_TOKEN", None)
        try:
            with (directory / "executor.log").open("ab") as log:
                job["process"] = subprocess.Popen(
                    [sys.executable, "-B", str(Path(__file__).with_name("worker.py")),
                     str(directory), str(self.lock_fd)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                    start_new_session=True, pass_fds=(self.lock_fd,))
        except OSError as exc:
            job["exit"] = {"returncode": None, "cleanup_ok": True, "reason": "spawn_error"}
            atomic_json(directory / "exit.json", {"at": time.time(), **job["exit"]})
            self.transition(job, "FAILED", f"spawn_error:{type(exc).__name__}")

    def reconcile(self):
        for job in self.active():
            if job["state"] == "UNKNOWN":
                continue
            proc = job["process"]
            directory = Path(job["directory"])
            if proc.poll() is None:
                if job["state"] == "STARTING" and (directory / "started.json").exists():
                    job["started_at"] = read_json(directory / "started.json")["at"]
                    self.transition(job, "RUNNING", "executing")
                continue
            try:
                receipt = read_json(directory / "exit.json")
            except (OSError, ValueError):
                # Unexpected executor loss: reclaim only this exact token.
                receipt = {"reason": "executor_lost", "returncode": None,
                           "cleanup_ok": processes.terminate_scope(job["token"], 1)}
                atomic_json(directory / "recovery-exit.json", {"at": time.time(), **receipt})
            job["exit"] = receipt
            if not receipt.get("cleanup_ok") or processes.scope_members(job["token"]):
                self.quarantined.add(job["gpu_uuid"])
                self.transition(job, "UNKNOWN", "cleanup_unconfirmed_gpu_quarantined")
                continue
            reason = receipt["reason"]
            state = ("TIMED_OUT" if reason == "deadline" else "CANCELLED" if reason == "cancelled"
                     else "SUCCEEDED" if reason == "process_exit" and receipt["returncode"] == 0 else "FAILED")
            self.transition(job, state, reason)

    def tick(self, snapshot=None, error=None):
        self.snapshot, self.probe_error = snapshot, error
        self.reconcile()
        if time.monotonic() >= self.deadline_mono or time.time() >= self.deadline_epoch:
            self.begin_shutdown("service_deadline")
        if self.closing:
            return
        check_storage(self.config, self.directory)
        if shutil.disk_usage(self.directory).free < self.config["min_free_disk_mib"] * 1024**2:
            self.begin_shutdown("disk_space")
            return
        queue = [j for j in self.jobs.values() if j["state"] == "QUEUED"]
        for job in queue:
            remaining = min(job["deadline_epoch"] - time.time(), job["deadline_mono"] - time.monotonic())
            if time.monotonic() - job["submitted_mono"] >= job["spec"]["queue_timeout_seconds"]:
                self.transition(job, "EXPIRED", "queue_timeout")
            elif remaining < job["spec"]["max_runtime_seconds"]:
                self.transition(job, "EXPIRED", "insufficient_remaining_budget")
        queue = [j for j in queue if j["state"] == "QUEUED"]
        skipped = []
        for job in queue:
            gpu, reason = choose_gpu(job["spec"], self.active(), self.config, snapshot, self.quarantined)
            if gpu is not None:
                self.launch(job, gpu)
                for earlier in skipped:
                    earlier["bypasses"] += 1
                # Each bypass counts even when two slots can be filled this tick.
                if any(j["bypasses"] >= self.config["max_bypass"] for j in skipped):
                    break
            else:
                self.transition(job, "QUEUED", reason)
                if job["bypasses"] >= self.config["max_bypass"]:
                    for later in queue[queue.index(job) + 1:]:
                        if later["state"] == "QUEUED":
                            self.transition(later, "QUEUED", "waiting_for_protected_earlier_job")
                    break
                skipped.append(job)

    def status(self):
        return {"session_id": self.id, "stopping": self.closing, "max_running": 2,
                "active_count": len(self.active()), "queued_count": sum(j["state"] == "QUEUED" for j in self.jobs.values()),
                "quarantined_gpus": sorted(self.quarantined), "telemetry": self.snapshot,
                "telemetry_error": self.probe_error, "deadline_epoch": self.deadline_epoch,
                "directory": str(self.directory), "local_test": self.config["local_test"]}
