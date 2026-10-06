"""Validation and the existing workspace's process/storage primitives."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable

try:
    from tools.process_control import processes
except ImportError:  # standalone scheduler release
    import importlib.util
    _process_path = Path(__file__).resolve().parents[1] / "research_handoff/core/processes.py"
    _process_spec = importlib.util.spec_from_file_location("_gpu_scheduler_processes", _process_path)
    processes = importlib.util.module_from_spec(_process_spec)
    _process_spec.loader.exec_module(processes)

PROCESS_SOURCE = Path(processes.__file__).resolve()

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "EXPIRED", "INFEASIBLE"}
WAITABLE_TERMINAL = TERMINAL | {"UNKNOWN"}
MAX_MESSAGE = 1024 * 1024


class JobWaitTimeout(TimeoutError):
    def __init__(self, message: str, job: dict[str, Any]) -> None:
        super().__init__(message)
        self.job = job

    @property
    def job_id(self) -> str:
        return self.job["id"]


class JobWaitInterrupted(RuntimeError):
    def __init__(self, reason: str, *, job: dict[str, Any] | None = None, session_id: str | None = None,
                 job_id: str | None = None, request_id: str | None = None) -> None:
        super().__init__(f"wait interrupted: {reason}; inspect the original job before continuing")
        self.reason = reason
        self.job = job
        self.session_id = job["session_id"] if job is not None else session_id
        self.job_id = job["id"] if job is not None else job_id
        self.request_id = job["request_id"] if job is not None else request_id


def validate_wait_timeout(timeout: float | None) -> float | None:
    return None if timeout is None else number(timeout, "timeout", 0, 43200)


def fit_job_time_limits(max_runtime_seconds: float, queue_timeout_seconds: float, remaining_seconds: float,
                        *, queue_reserve_seconds: float = 600, grace_seconds: float = 30,
                        min_runtime_seconds: float = 120) -> dict[str, float]:
    """Fit a new request's queue and execution limits inside its parent budget.

    Use only before submitting: accepted/unknown requests must retain their spec.
    Reserving a queue window also avoids immediate expiry at admission.
    """
    runtime = number(max_runtime_seconds, "max_runtime_seconds", .1, 43200)
    queue = number(queue_timeout_seconds, "queue_timeout_seconds", 1, 43200)
    remaining = number(remaining_seconds, "remaining_seconds")
    reserve = min(queue, number(queue_reserve_seconds, "queue_reserve_seconds", 1),
                  max(1, remaining * .25))
    grace = number(grace_seconds, "grace_seconds")
    minimum = number(min_runtime_seconds, "min_runtime_seconds")
    runtime = min(runtime, math.floor(remaining - reserve - grace))
    if runtime <= minimum:
        raise ValueError("remaining parent budget cannot fit GPU setup and execution")
    queue = min(queue, remaining - runtime - grace)
    if queue < 1:
        raise ValueError("remaining parent budget cannot fit a queued GPU iteration")
    return {"max_runtime_seconds": runtime, "queue_timeout_seconds": queue}


def number(value: Any, name: str, low: float = 0, high: float = float("inf")) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return value


def integer(value: Any, name: str, low: int = 1, high: int = 2**31) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return number(value, name, low, high)


def fields(value: Any, required: Iterable[str], optional: Iterable[str] = ()) -> None:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    missing, extra = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"missing fields: {sorted(missing)}; unknown fields: {sorted(extra)}")


def label(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}", value):
        raise ValueError(f"invalid {name}")
    return value


def absolute(value: str, name: str) -> str:
    if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return str(Path(value).resolve())


def check_storage(config: dict[str, Any], *paths: str | Path) -> None:
    """Reject the system device and changed/nested mounts before new writes."""
    if config["local_test"]:
        return
    mounts = [Path(name).resolve(strict=True) for name in
              [config["data_mount"], *config.get("data_mounts", [])]]
    for mount in mounts:
        if not mount.is_mount() or mount.stat().st_dev == Path("/").stat().st_dev:
            raise ValueError("data_mount must be a mounted data device distinct from /")
        expected = config.get("data_devices", {}).get(str(mount))
        if mount == mounts[0]:
            expected = config.get("data_device", expected)
        if expected not in (None, mount.stat().st_dev):
            raise ValueError("data mount device changed")
    for item in paths:
        path = Path(item).resolve()
        matching = [mount for mount in mounts if path.is_relative_to(mount)]
        if not matching:
            raise ValueError(f"path is outside data_mount: {path}")
        mount = max(matching, key=lambda p: len(p.parts))
        ancestor = path
        while not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.stat().st_dev != mount.stat().st_dev:
            raise ValueError(f"unexpected device: {path}")


def atomic_json(path: str | Path, value: Any) -> None:
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


def validate_config(raw: dict[str, Any], *, local_test: bool = False) -> dict[str, Any]:
    fields(raw, {"version", "root", "data_mount", "gpus", "cpu_cores", "ram_mib"},
           {"poll_seconds", "service_seconds", "persistent", "max_running", "scheduling_policy",
            "starvation_seconds", "max_bypass", "max_jobs", "min_free_disk_mib",
            "external_process_policy", "shared_headroom_mib", "infrastructure_lease", "data_mounts",
            "execution_backend", "systemd_user", "resource_sample_seconds", "profile_min_samples"})
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("version must be 1")
    cfg = dict(raw, root=absolute(raw["root"], "root"), local_test=local_test)
    cfg["data_mount"] = None if local_test else absolute(raw["data_mount"], "data_mount")
    extra_mounts = raw.get("data_mounts", [])
    if not isinstance(extra_mounts, list) or len(extra_mounts) > 32:
        raise ValueError("data_mounts must be an array of at most 32 explicit data mounts")
    if extra_mounts:
        cfg["data_mounts"] = [absolute(name, "data_mounts") for name in extra_mounts]
        if len(set(cfg["data_mounts"])) != len(extra_mounts):
            raise ValueError("duplicate data_mounts")
    cfg["poll_seconds"] = number(raw.get("poll_seconds", 1), "poll_seconds", .05, 10)
    persistent = raw.get("persistent", False)
    if type(persistent) is not bool:
        raise ValueError("persistent must be a boolean")
    if persistent and raw.get("service_seconds") is not None:
        raise ValueError("omit service_seconds for a persistent service")
    lease = raw.get("infrastructure_lease")
    if persistent and lease is not None:
        raise ValueError("persistent service cannot carry an infrastructure lease")
    maximum = 43200
    if lease is not None:
        fields(lease, {"authorization", "deadline_epoch"})
        if not isinstance(lease["authorization"], str) or not lease["authorization"].strip():
            raise ValueError("infrastructure lease requires explicit authorization")
        number(lease["deadline_epoch"], "infrastructure_lease.deadline_epoch", 1)
        maximum = 172800
    cfg["persistent"] = persistent
    cfg["execution_backend"] = raw.get("execution_backend", "process" if local_test else "systemd")
    if cfg["execution_backend"] not in {"process", "systemd"}:
        raise ValueError("execution_backend must be process or systemd")
    if not local_test and cfg["execution_backend"] != "systemd":
        raise ValueError("production execution requires systemd resource enforcement")
    cfg["systemd_user"] = raw.get("systemd_user", False)
    if type(cfg["systemd_user"]) is not bool:
        raise ValueError("systemd_user must be a boolean")
    cfg["resource_sample_seconds"] = number(raw.get("resource_sample_seconds", 1),
                                              "resource_sample_seconds", .05, 10)
    cfg["profile_min_samples"] = integer(raw.get("profile_min_samples", 3), "profile_min_samples", 1, 100)
    cfg["service_seconds"] = (None if persistent else
                               number(raw.get("service_seconds", 43200), "service_seconds", .1, maximum))
    cfg["max_bypass"] = integer(raw.get("max_bypass", 2), "max_bypass", 0, 100)
    cfg["max_running"] = integer(raw.get("max_running", 2), "max_running", 1, 32)
    cfg["scheduling_policy"] = raw.get("scheduling_policy", "fifo")
    if cfg["scheduling_policy"] not in ("fifo", "fair_share"):
        raise ValueError("scheduling_policy must be fifo or fair_share")
    cfg["starvation_seconds"] = number(raw.get("starvation_seconds", 300), "starvation_seconds", 0, 43200)
    cfg["max_jobs"] = integer(raw.get("max_jobs", 10000), "max_jobs", 1, 100000)
    cfg["min_free_disk_mib"] = integer(raw.get("min_free_disk_mib", 1024), "min_free_disk_mib", 0)
    cfg["external_process_policy"] = raw.get("external_process_policy", "exclusive_admission")
    if cfg["external_process_policy"] not in ("exclusive_admission", "shared"):
        raise ValueError("invalid external_process_policy")
    cfg["shared_headroom_mib"] = integer(raw.get("shared_headroom_mib", 2048), "shared_headroom_mib", 0)
    if cfg["external_process_policy"] == "shared" and cfg["shared_headroom_mib"] < 1024:
        raise ValueError("shared admission requires at least 1024 MiB headroom")
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
    if not local_test and extra_mounts:
        cfg["data_devices"] = {name: Path(name).stat().st_dev for name in cfg["data_mounts"]}
    return cfg


def deadline_timestamp(value: str) -> float:
    """Convert an explicit timezone offset to UTC epoch without host timezone."""
    if not isinstance(value, str):
        raise ValueError("deadline_at must be an ISO8601 timestamp with timezone")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return number(parsed.timestamp(), "deadline_at", 1)
    except (ValueError, OverflowError) as exc:
        raise ValueError("deadline_at must be an ISO8601 timestamp with timezone") from exc


def validate_job(raw: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    fields(raw, {"request_id", "owner", "command", "cwd", "memory_mib", "compute_units",
                 "max_runtime_seconds"},
           {"cpu_cores", "ram_mib", "queue_timeout_seconds", "deadline_epoch", "deadline_at", "gpu_uuid",
            "job_class", "memory_max_mib", "memory_high_mib"})
    spec = dict(raw)
    if "deadline_at" in spec:
        if "deadline_epoch" in spec:
            raise ValueError("specify exactly one of deadline_at and deadline_epoch")
        spec["deadline_epoch"] = deadline_timestamp(spec.pop("deadline_at"))
    for key in ("request_id", "owner"):
        label(spec[key], key)
    spec["job_class"] = label(spec.get("job_class", "generic"), "job_class")
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
    # Legacy declarations remain readable for immutable request reconciliation.
    # Only the original absolute deadline limits queue admission.
    if "queue_timeout_seconds" in spec:
        number(spec["queue_timeout_seconds"], "queue_timeout_seconds", .1, 43200)
    spec["memory_max_mib"] = integer(spec.get("memory_max_mib", spec["ram_mib"]), "memory_max_mib")
    spec["memory_high_mib"] = integer(spec.get("memory_high_mib", max(1, int(spec["memory_max_mib"] * .9))),
                                     "memory_high_mib")
    if spec["memory_high_mib"] > spec["memory_max_mib"]:
        raise ValueError("memory_high_mib must not exceed memory_max_mib")
    if "deadline_epoch" in spec:
        number(spec["deadline_epoch"], "deadline_epoch", 1)
    eligible = [g for g in config["gpus"] if spec.get("gpu_uuid", g["uuid"]) == g["uuid"]]
    headroom = config.get("shared_headroom_mib", 2048) if config.get("external_process_policy") == "shared" else 0
    if not any(spec["memory_mib"] + headroom <= g["memory_mib"] and spec["compute_units"] <= g["compute_units"]
               for g in eligible):
        raise ValueError("no configured GPU can fit this request including required shared headroom")
    return spec


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())
