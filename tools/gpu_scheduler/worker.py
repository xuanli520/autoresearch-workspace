"""Independent local executor: survives scheduler loss until its fixed deadline."""
from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "tools.gpu_scheduler"

from .common import atomic_json, check_storage, processes, read_json
from . import resource_limits


def run(directory, lock_fd=None):
    directory = Path(directory)
    launch = read_json(directory / "launch.json")
    cfg, spec = launch["config"], launch["spec"]
    # Legacy launchers may pass the service FD. Drop it immediately: the journal
    # preserves this job's reservation while a replacement scheduler reconciles.
    if lock_fd is not None:
        os.close(lock_fd)
    check_storage(cfg, directory)
    executor_lock = (directory / "executor.lock").open("a")
    try:
        fcntl.flock(executor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        executor_lock.close()
        return
    if (directory / "executor.json").exists() or (directory / "exit.json").exists():
        executor_lock.close()
        return  # A launch is a one-shot execution claim, even after worker loss.
    requested = False

    def stop(signum, frame):
        nonlocal requested
        requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    child = None
    started = None
    returncode = None
    reason = "executor_error"
    cleanup_ok = False
    boundary = None
    sampler = None
    resource_usage = None
    boundary_cleanup = {"cleanup_ok": True, "backend": None}
    try:
        check_storage(cfg, directory, spec["cwd"])
        atomic_json(directory / "executor.json", {
            "job_id": launch["id"], "token": launch["token"], "at": time.time(),
            "pid": os.getpid(), "start_ticks": processes.process_start_ticks(os.getpid()),
            "boot_id": processes.boot_id(),
        })
        if launch.get("boot_id", processes.boot_id()) != processes.boot_id():
            reason = "launch_boot_changed"
            return
        if time.monotonic() >= launch["deadline_monotonic"] or time.time() >= launch["deadline_epoch"]:
            reason = "deadline"
            return
        if requested or (directory / "STOP.json").exists():
            reason = "cancelled"
            return
        env = os.environ.copy()
        env.update({"AUTORESEARCH_PROCESS_TOKEN": launch["token"],
                    "GPU_SCHEDULER_JOB_ID": launch["id"],
                    "GPU_SCHEDULER_JOB_DIR": str(directory),
                    "CUDA_VISIBLE_DEVICES": launch["gpu_uuid"],
                    "OMP_NUM_THREADS": str(spec["cpu_cores"]),
                    "MKL_NUM_THREADS": str(spec["cpu_cores"]),
                    "OPENBLAS_NUM_THREADS": str(spec["cpu_cores"]),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        for name in ("TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                     "PIP_CACHE_DIR", "HF_HOME", "TORCH_HOME", "CUDA_CACHE_PATH", "TRITON_CACHE_DIR",
                     "NUMBA_CACHE_DIR", "npm_config_cache", "UV_CACHE_DIR"):
            target = directory / "scratch" / name
            target.mkdir(parents=True, exist_ok=True)
            env[name] = str(target)
        boundary = resource_limits.prepare(cfg, spec, launch["id"])
        atomic_json(directory / "resource-limits.json", boundary)
        sampler = resource_limits.ResourceSampler(launch, boundary)
        sampler.start()
        if requested or (directory / "STOP.json").exists():
            reason = "cancelled"
            return
        if time.monotonic() >= launch["deadline_monotonic"] or time.time() >= launch["deadline_epoch"]:
            reason = "deadline"
            return
        with (directory / "stdout.log").open("ab") as stdout, (directory / "stderr.log").open("ab") as stderr:
            child = subprocess.Popen(resource_limits.command(cfg, spec, boundary), cwd=spec["cwd"], env=env,
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     start_new_session=True)
            started = time.monotonic()
            atomic_json(directory / "started.json", {
                "at": time.time(), "pid": child.pid, "start_ticks": processes.process_start_ticks(child.pid),
                "boot_id": processes.boot_id(), "worker_pid": os.getpid(),
                "worker_start_ticks": processes.process_start_ticks(os.getpid()),
                "resource_limits": boundary,
            })
            next_check = 0
            while child.poll() is None:
                if requested or (directory / "STOP.json").exists():
                    reason = "cancelled"
                    break
                if time.monotonic() >= launch["deadline_monotonic"] or time.time() >= launch["deadline_epoch"]:
                    reason = "deadline"
                    break
                if time.monotonic() >= next_check:
                    check_storage(cfg, directory, spec["cwd"])
                    if shutil.disk_usage(directory).free < cfg["min_free_disk_mib"] * 1024**2:
                        reason = "disk_space"
                        break
                    next_check = time.monotonic() + 1
                time.sleep(.05)
            else:
                reason = "process_exit"
    except BaseException as exc:
        reason = "resource_limit_error" if isinstance(exc, resource_limits.ResourceLimitError) else "executor_error"
        print(f"executor error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        if sampler is not None:
            try:
                resource_usage = sampler.finish()
                check_storage(cfg, directory)
                atomic_json(directory / "resource-usage.json", resource_usage)
            except Exception as exc:
                resource_usage = {"measurement_errors": [type(exc).__name__ + ": " + str(exc)]}
        try:
            boundary_cleanup = {"cleanup_ok": resource_limits.cleanup(cfg, boundary),
                                "backend": None if boundary is None else boundary["backend"]}
        except Exception as exc:
            boundary_cleanup = {"cleanup_ok": False, "error": type(exc).__name__ + ": " + str(exc)}
        remaining = min(launch["deadline_monotonic"] - time.monotonic(), launch["deadline_epoch"] - time.time())
        cleanup_ok = processes.terminate_scope(launch["token"], max(0, min(1, remaining))) and boundary_cleanup["cleanup_ok"]
        if child is not None:
            try:
                returncode = child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cleanup_ok = False
        try:
            from .container_ownership import cleanup_job
            container_cleanup = cleanup_job(directory)
            cleanup_ok = cleanup_ok and container_cleanup["cleanup_ok"]
        except Exception as exc:
            container_cleanup = {"cleanup_ok": False, "error": type(exc).__name__ + ": " + str(exc)}
            cleanup_ok = False
        if (reason == "process_exit" and resource_usage and
                resource_usage.get("memory_events", {}).get("oom_kill", 0)):
            reason = "memory_limit_oom"
        receipt = {"at": time.time(), "reason": reason, "returncode": returncode,
                   "cleanup_ok": cleanup_ok,
                   "container_cleanup": container_cleanup,
                   "resource_boundary_cleanup": boundary_cleanup, "resource_usage": resource_usage,
                   "runtime_seconds": 0 if started is None else time.monotonic() - started}
        # Avoid writing through a vanished data mount onto the system disk.
        check_storage(cfg, directory)
        atomic_json(directory / "exit.json", receipt)
        executor_lock.close()


if __name__ == "__main__":
    run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
