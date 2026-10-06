"""Bounded fan-out through the official blocking submit/wait API."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import subprocess
import time
from copy import copy
from pathlib import Path
from typing import Any, Callable

from .client import Client, SchedulerError
from .common import JobWaitInterrupted, JobWaitTimeout, WAITABLE_TERMINAL, atomic_json
from .remote import RemoteClient

TERMINAL = WAITABLE_TERMINAL


def execute_one(client: Client, spec: dict[str, Any], existing: dict[str, dict[str, Any]]) -> dict[str, Any]:
    job = existing.get(spec["request_id"])
    while True:
        try:
            if job is None:
                job = client.submit(spec, timeout=45)
            elif job["state"] in TERMINAL and not job.get("reconciling"):
                return client.get(job["id"])
            else:
                job = client.wait(job["id"], timeout=45)
        except (JobWaitTimeout, JobWaitInterrupted) as error:
            job = error.job or recover_request(client, spec)
        except SchedulerError:
            # A transport failure never authorizes replaying a submit.
            job = recover_request(client, spec)
        if job["state"] in TERMINAL and not job.get("reconciling"):
            return job
        if time.time() >= spec.get("deadline_epoch", float("inf")):
            raise RuntimeError("batch deadline reached; query existing job before continuing")


def recover_request(client: Client, spec: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(3):
        try:
            return client.get(request_id=spec["request_id"])
        except SchedulerError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", required=True, type=Path)
    parser.add_argument("--session", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--specs", type=Path)
    source.add_argument("--stages", type=Path)
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--concurrent", type=int, default=1)
    parser.add_argument("--completed-command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 1 <= args.concurrent <= 4:
        raise ValueError("batch concurrent must be between 1 and 4")
    if args.stages:
        return run_stages(args)
    return submit_stage(args)


def run_stages(args: argparse.Namespace) -> int:
    stages = json.loads(args.stages.read_text())
    identities = [name for stage in stages for name in stage["candidate_ids"]]
    if len(identities) != len(set(identities)):
        raise ValueError("stage candidate sets must not overlap")
    for stage in stages:
        if not 0 < stage["max_wall_seconds"] <= 43200:
            raise ValueError("each scoring stage must be at most 12 hours")
    for stage in stages:
        subprocess.run(stage["prepare_command"], check=True)
        child = copy(args)
        child.stages = None
        child.specs = Path(stage["specs"])
        child.receipts = Path(stage["receipts"])
        result = submit_stage(child)
        if result:
            return result
    return 0


def submit_stage(args: argparse.Namespace) -> int:
    if args.receipts is None:
        raise ValueError("single batch requires --receipts")
    specs = json.loads(args.specs.read_text())
    ids = [spec["request_id"] for spec in specs]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate request IDs in batch")
    args.receipts.mkdir(parents=True, exist_ok=True)
    with (args.specs.parent / ".gpu-batch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return run_stage_locked(args, specs)


def run_stage_locked(args: argparse.Namespace, specs: list[dict[str, Any]]) -> int:
    started = time.time()
    atomic_json(args.receipts / "batch-launch.json", {
        "pid": os.getpid(), "started_at_epoch": started, "concurrent": args.concurrent,
        "specs": str(args.specs.resolve()), "session_id": args.session,
        "deadline_epoch": min((s["deadline_epoch"] for s in specs if s.get("deadline_epoch") is not None), default=None),
    })
    base = RemoteClient(args.remote, session_id=args.session)
    existing = {job["request_id"]: job for job in base.jobs()}
    jobs, errors = [], []

    def status(state: str) -> None:
        atomic_json(args.receipts / "batch-status.json", {
            "state": state, "updated_at_epoch": time.time(), "completed": len(jobs),
            "total": len(specs), "concurrent": args.concurrent, "interruptions": errors,
        })

    status("RUNNING")

    def run(spec: dict[str, Any]) -> dict[str, Any]:
        client = RemoteClient(args.remote, session_id=args.session)
        return execute_one(client, spec, existing)

    def completed(spec: dict[str, Any], job: dict[str, Any]) -> None:
        path = args.receipts / (job["id"] + ".json")
        atomic_json(path, job)
        jobs.append(job)
        if job["state"] == "UNKNOWN" or (job.get("exit") and not job["exit"].get("cleanup_ok")):
            raise RuntimeError("job cleanup is unconfirmed; batch admission stopped")
        if args.completed_command:
            subprocess.run([*args.completed_command, "--job-receipt", str(path.resolve())], check=True)
        status("RUNNING")
        print(json.dumps({"request_id": spec["request_id"], "state": job["state"],
                          "completed": len(jobs), "total": len(specs)}), flush=True)

    def failed(spec: dict[str, Any], error: Exception) -> None:
        errors.append({"request_id": spec["request_id"], "error": str(error)})
        atomic_json(args.receipts / "interruptions.json", errors)
        status("INTERRUPTED")
        print(json.dumps(errors[-1]), flush=True)

    run_batch(specs, args.concurrent, run, completed, failed)
    atomic_json(args.receipts / "batch-result.json", {"jobs": jobs, "interruptions": errors})
    status("INTERRUPTED" if errors else "COMPLETED")
    return 1 if errors else 0


def run_batch(specs: list[dict[str, Any]], concurrency: int,
              run: Callable[[dict[str, Any]], dict[str, Any]],
              completed: Callable[[dict[str, Any], dict[str, Any]], None],
              failed: Callable[[dict[str, Any], Exception], None]) -> None:
    """Admit replacements only after the completion/cleanup hook succeeds."""
    pending = iter(specs)
    stopped = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}

        def admit() -> None:
            spec = next(pending, None)
            if spec is not None:
                futures[pool.submit(run, spec)] = spec

        for _ in range(concurrency):
            admit()
        while futures:
            ready, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in ready:
                spec = futures.pop(future)
                try:
                    completed(spec, future.result())
                except Exception as error:
                    stopped = True
                    failed(spec, error)
            if not stopped:
                for _ in ready:
                    admit()


if __name__ == "__main__":
    raise SystemExit(main())
