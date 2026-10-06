"""Independent local executor: survives scheduler loss until its fixed deadline."""
from __future__ import annotations

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


def run(directory, lock_fd):
    directory = Path(directory)
    launch = read_json(directory / "launch.json")
    cfg, spec = launch["config"], launch["spec"]
    # Keeping this FD prevents a replacement scheduler from starting while an
    # orphan executor still owns resources. It is never inherited by user jobs.
    os.fstat(lock_fd)
    os.set_inheritable(lock_fd, False)
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
    try:
        check_storage(cfg, directory, spec["cwd"])
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
        with (directory / "stdout.log").open("ab") as stdout, (directory / "stderr.log").open("ab") as stderr:
            child = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=env,
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     start_new_session=True)
            started = time.monotonic()
            atomic_json(directory / "started.json", {
                "at": time.time(), "pid": child.pid, "start_ticks": processes.process_start_ticks(child.pid),
                "boot_id": processes.boot_id(), "worker_pid": os.getpid(),
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
        reason = "executor_error"
        print(f"executor error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        remaining = min(launch["deadline_monotonic"] - time.monotonic(), launch["deadline_epoch"] - time.time())
        cleanup_ok = processes.terminate_scope(launch["token"], max(0, min(1, remaining)))
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
        receipt = {"at": time.time(), "reason": reason, "returncode": returncode,
                   "cleanup_ok": cleanup_ok,
                   "container_cleanup": container_cleanup,
                   "runtime_seconds": 0 if started is None else time.monotonic() - started}
        # Avoid writing through a vanished data mount onto the system disk.
        check_storage(cfg, directory)
        atomic_json(directory / "exit.json", receipt)
        os.close(lock_fd)


if __name__ == "__main__":
    run(sys.argv[1], int(sys.argv[2]))
