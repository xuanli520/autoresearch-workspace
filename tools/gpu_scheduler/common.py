"""Validation and the existing workspace's process/storage primitives."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
from pathlib import Path

# Load the existing dependency-free process helpers without changing their source
# or injecting generic module names ("processes", "longrun") into sys.modules.
PROCESS_SOURCE = Path(__file__).resolve().parents[1] / "research_handoff/core/processes.py"
_spec = importlib.util.spec_from_file_location("_gpu_scheduler_processes", PROCESS_SOURCE)
processes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(processes)

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "EXPIRED"}
WAITABLE_TERMINAL = TERMINAL | {"UNKNOWN"}
MAX_MESSAGE = 1024 * 1024


class JobWaitTimeout(TimeoutError):
    def __init__(self, message, job):
        super().__init__(message)
        self.job = job

    @property
    def job_id(self):
        return self.job["id"]


class JobWaitInterrupted(RuntimeError):
    def __init__(self, reason, *, job=None, session_id=None, job_id=None, request_id=None):
        super().__init__(f"wait interrupted: {reason}; inspect the original job before continuing")
        self.reason = reason
        self.job = job
        self.session_id = job["session_id"] if job is not None else session_id
        self.job_id = job["id"] if job is not None else job_id
        self.request_id = job["request_id"] if job is not None else request_id


def validate_wait_timeout(timeout):
    return None if timeout is None else number(timeout, "timeout", 0, 43200)


def number(value, name, low=0, high=float("inf")):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return value


def integer(value, name, low=1, high=2**31):
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return number(value, name, low, high)


def fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    missing, extra = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"missing fields: {sorted(missing)}; unknown fields: {sorted(extra)}")


def label(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}", value):
        raise ValueError(f"invalid {name}")
    return value


def absolute(value, name):
    if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return str(Path(value).resolve())


def check_storage(config, *paths):
    """Reject the system device and changed/nested mounts before new writes."""
    if config["local_test"]:
        return
    mount = Path(config["data_mount"]).resolve(strict=True)
    if not mount.is_mount() or mount.stat().st_dev == Path("/").stat().st_dev:
        raise ValueError("data_mount must be a mounted data device distinct from /")
    if config.get("data_device") not in (None, mount.stat().st_dev):
        raise ValueError("data mount device changed")
    for item in paths:
        path = Path(item).resolve()
        if not path.is_relative_to(mount):
            raise ValueError(f"path is outside data_mount: {path}")
        ancestor = path
        while not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.stat().st_dev != mount.stat().st_dev:
            raise ValueError(f"unexpected device: {path}")


def atomic_json(path, value):
    """Unique temporary file, replace, fsync; no shared queue is stored here."""
    import tempfile
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temp).unlink(missing_ok=True)


def validate_config(raw, *, local_test=False):
    fields(raw, {"version", "root", "data_mount", "gpus", "cpu_cores", "ram_mib"},
           {"poll_seconds", "service_seconds", "max_bypass", "max_jobs", "min_free_disk_mib"})
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("version must be 1")
    cfg = dict(raw, root=absolute(raw["root"], "root"), local_test=local_test)
    cfg["data_mount"] = None if local_test else absolute(raw["data_mount"], "data_mount")
    cfg["poll_seconds"] = number(raw.get("poll_seconds", 1), "poll_seconds", .05, 10)
    cfg["service_seconds"] = number(raw.get("service_seconds", 43200), "service_seconds", .1, 43200)
    cfg["max_bypass"] = integer(raw.get("max_bypass", 2), "max_bypass", 0, 100)
    cfg["max_jobs"] = integer(raw.get("max_jobs", 10000), "max_jobs", 1, 100000)
    cfg["min_free_disk_mib"] = integer(raw.get("min_free_disk_mib", 1024), "min_free_disk_mib", 0)
    for key in ("cpu_cores", "ram_mib"):
        integer(cfg[key], key)
    if not isinstance(cfg["gpus"], list) or not cfg["gpus"]:
        raise ValueError("gpus must be a nonempty list")
    seen = set()
    for gpu in cfg["gpus"]:
        fields(gpu, {"uuid", "memory_mib", "compute_units"})
        if not isinstance(gpu["uuid"], str) or not re.fullmatch(r"GPU-[a-zA-Z0-9-]+", gpu["uuid"]):
            raise ValueError("use physical GPU UUIDs; MIG is not supported in v1")
        if gpu["uuid"] in seen:
            raise ValueError("duplicate GPU UUID")
        seen.add(gpu["uuid"])
        integer(gpu["memory_mib"], "memory_mib")
        integer(gpu["compute_units"], "compute_units", 1, 100)
    check_storage(cfg, cfg["root"])
    cfg["data_device"] = None if local_test else Path(cfg["data_mount"]).stat().st_dev
    return cfg


def validate_job(raw, config):
    fields(raw, {"request_id", "owner", "command", "cwd", "memory_mib", "compute_units",
                 "max_runtime_seconds"},
           {"cpu_cores", "ram_mib", "queue_timeout_seconds", "deadline_epoch", "gpu_uuid"})
    spec = dict(raw)
    for key in ("request_id", "owner"):
        label(spec[key], key)
    cmd = spec["command"]
    if not isinstance(cmd, list) or not cmd or any(not isinstance(x, str) or "\0" in x for x in cmd):
        raise ValueError("command must be an argv list")
    if not cmd[0]:
        raise ValueError("command executable must be nonempty")
    spec["cwd"] = absolute(spec["cwd"], "cwd")
    if not Path(spec["cwd"]).is_dir():
        raise ValueError("cwd must exist")
    check_storage(config, spec["cwd"])
    for key, default in (("cpu_cores", 1), ("ram_mib", 1024)):
        spec[key] = integer(spec.get(key, default), key)
        if spec[key] > config[key]:
            raise ValueError(f"job exceeds configured {key} capacity")
    integer(spec["memory_mib"], "memory_mib")
    integer(spec["compute_units"], "compute_units", 1, 100)
    number(spec["max_runtime_seconds"], "max_runtime_seconds", .1, 43200)
    spec["queue_timeout_seconds"] = number(spec.get("queue_timeout_seconds", 3600),
                                            "queue_timeout_seconds", .1, 43200)
    if "deadline_epoch" in spec:
        number(spec["deadline_epoch"], "deadline_epoch", 1)
    eligible = [g for g in config["gpus"] if spec.get("gpu_uuid", g["uuid"]) == g["uuid"]]
    if not any(spec["memory_mib"] <= g["memory_mib"] and spec["compute_units"] <= g["compute_units"]
               for g in eligible):
        raise ValueError("no configured GPU can fit this request")
    return spec


def read_json(path):
    return json.loads(Path(path).read_text())
