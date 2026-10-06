"""Trusted Docker receipts for GPU accounting; never signal container processes."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

from .common import atomic_json, check_storage, processes, read_json


def _trusted(path, *, directory=False):
    info = path.lstat()
    return (info.st_uid == os.getuid() and not info.st_mode & 0o022
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))


def _cgroups(pid):
    rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    return [tuple(row.split(":", 2)[1:]) for row in rows if len(row.split(":", 2)) == 3]


def _container_cgroups(pid, container_id):
    # Never accept a common ancestor such as /system.slice or the cgroup root.
    names = {container_id, f"docker-{container_id}.scope"}
    return [(controller, path) for controller, path in _cgroups(pid)
            if names.intersection(Path(path).parts)]


def register_container(container_id, docker_host, project):
    """Called by a trusted host provider after Docker starts its exact main container.

    The receipt stays in the private scheduler job directory. No ownership token,
    host path or Docker socket is passed into the solver container.
    """
    job_dir = os.environ.get("GPU_SCHEDULER_JOB_DIR")
    if not job_dir:
        return None  # Standalone Harbor execution is not a scheduler job.
    directory = Path(job_dir)
    if not directory.is_absolute() or directory.resolve(strict=True) != directory or not _trusted(directory, directory=True):
        raise ValueError("container registration requires the trusted scheduler job directory")
    if not _trusted(directory / "launch.json"):
        raise ValueError("container registration requires a trusted launch record")
    launch = read_json(directory / "launch.json")
    check_storage(launch["config"], directory, directory / "containers")
    if (launch["id"] != os.environ.get("GPU_SCHEDULER_JOB_ID")
            or launch["token"] != os.environ.get("AUTORESEARCH_PROCESS_TOKEN")):
        raise ValueError("container registration does not match the launching job")
    if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
        raise ValueError("invalid exact Docker container ID")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", project):
        raise ValueError("invalid exact Docker project")
    if not docker_host.startswith("unix://"):
        raise ValueError("container registration requires a local Docker socket")
    socket = Path(docker_host[7:])
    if not socket.is_absolute() or not stat.S_ISSOCK(socket.stat().st_mode):
        raise ValueError("container registration requires an actual local Docker socket")
    result = subprocess.run(["docker", "--host", docker_host, "inspect", container_id],
                            capture_output=True, text=True, timeout=10, check=True)
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise ValueError("ambiguous Docker ownership")
    inspected = rows[0]
    exact_id = inspected["Id"]
    if not re.fullmatch(r"[a-f0-9]{64}", exact_id) or not exact_id.startswith(container_id):
        raise ValueError("Docker container identity changed")
    if (inspected["Config"].get("Labels") or {}).get("com.docker.compose.project") != project:
        raise ValueError("Docker container does not belong to the provider project")
    pid = inspected["State"]["Pid"]
    ticks = processes.process_start_ticks(pid)
    boot = processes.boot_id()
    if (not inspected["State"].get("Running") or not processes.pid_matches(pid, ticks, boot)
            or not _container_cgroups(pid, exact_id)):
        raise ValueError("Docker container has no verified live process scope")
    receipts = directory / "containers"
    receipts.mkdir(mode=0o700, exist_ok=True)
    if not _trusted(receipts, directory=True):
        raise ValueError("container receipts require a trusted host directory")
    target = receipts / f"{exact_id}.json"
    if target.is_symlink():
        raise ValueError("container receipt cannot be a symlink")
    atomic_json(target, {"version": 1, "job_id": launch["id"], "token": launch["token"],
                         "container_id": exact_id, "project": project, "docker_host": docker_host,
                         "init_pid": pid, "init_start_ticks": ticks, "boot_id": boot})
    return {"registered": True, "job_id": launch["id"], "container_id": exact_id}


def container_process_map(active, gpu_pids):
    """Attribute only live GPU PIDs in an attested container's current cgroup."""
    result = {job["id"]: set() for job in active}
    if not gpu_pids:
        return result
    boot = processes.boot_id()
    scopes = []
    for job in active:
        if "directory" not in job:
            continue
        directory = Path(job["directory"])
        receipts = directory / "containers"
        try:
            if not _trusted(directory, directory=True) or not _trusted(receipts, directory=True):
                continue
            for path in receipts.glob("*.json"):
                try:
                    if not _trusted(path) or path.stat().st_size > 16384:
                        continue
                    row = read_json(path)
                    cid = row["container_id"]
                    if (row["version"] != 1 or row["job_id"] != job["id"] or row["token"] != job["token"]
                            or not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{64}", cid)
                            or path.name != f"{cid}.json" or row["boot_id"] != boot
                            or not processes.pid_matches(row["init_pid"], row["init_start_ticks"], boot)):
                        continue
                    groups = _container_cgroups(row["init_pid"], cid)
                    if groups:
                        scopes.append((job["id"], row, groups))
                except (OSError, ValueError, TypeError, KeyError):
                    continue  # Invalid/stale receipts never make a PID known.
        except OSError:
            continue
    for pid in gpu_pids:
        try:
            ticks = processes.process_start_ticks(pid)
            groups = _cgroups(pid)
            owners = set()
            for job_id, row, roots in scopes:
                if (any(controller == root_controller and (path == root or path.startswith(root + "/"))
                        for controller, path in groups for root_controller, root in roots)
                        and processes.pid_matches(row["init_pid"], row["init_start_ticks"], boot)):
                    owners.add(job_id)
            if len(owners) == 1 and processes.pid_matches(pid, ticks, boot):
                result[owners.pop()].add(pid)
        except (OSError, ValueError, TypeError):
            continue
    return result
