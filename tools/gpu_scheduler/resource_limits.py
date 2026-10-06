"""Kernel-enforced CPU/RAM limits shared by a job's host and Docker scopes."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .common import read_json


CGROUP_ROOT = Path("/sys/fs/cgroup")
MIB = 1024**2


class ResourceLimitError(RuntimeError):
    pass


def names(job_id):
    suffix = hashlib.sha256(job_id.encode()).hexdigest()[:24]
    return f"gpujob{suffix}.slice", f"gpujob{suffix}.scope"


def _systemctl(config, *argv):
    return ["systemctl", *(["--user"] if config.get("systemd_user", False) else []), *argv]


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = (getattr(exc, "stderr", None) or str(exc)).strip()
        raise ResourceLimitError(f"resource controller failed: {detail}") from exc


def _values(path):
    result = {}
    for line in path.read_text().splitlines():
        key, value = line.split()
        result[key] = int(value)
    return result


def unit_cgroup(config, unit):
    output = _run(_systemctl(config, "show", unit, "--property=ControlGroup", "--value")).stdout.strip()
    if not output.startswith("/") or output == "/" or ".." in Path(output).parts:
        raise ResourceLimitError("resource unit has no exact non-root cgroup")
    path = (CGROUP_ROOT / output.lstrip("/")).resolve(strict=True)
    if not path.is_relative_to(CGROUP_ROOT.resolve()) or path == CGROUP_ROOT.resolve():
        raise ResourceLimitError("resource cgroup escaped the controller root")
    return path


def verify_cgroup(path, spec):
    """Read the kernel contract, including swap, rather than trusting CLI success."""
    try:
        maximum = int((path / "memory.max").read_text().strip())
        high = int((path / "memory.high").read_text().strip())
        swap = int((path / "memory.swap.max").read_text().strip())
        quota, period = (path / "cpu.max").read_text().split()
        if (maximum != spec["memory_max_mib"] * MIB or
                high != spec["memory_high_mib"] * MIB or swap != 0 or
                quota == "max" or int(quota) <= 0 or int(period) <= 0 or
                int(quota) / int(period) != spec["cpu_cores"]):
            raise ResourceLimitError("kernel CPU/RAM limits differ from the accepted contract")
        return {"memory_max_bytes": maximum, "memory_high_bytes": high,
                "memory_swap_max_bytes": swap, "cpu_quota_us": int(quota), "cpu_period_us": int(period)}
    except (OSError, ValueError) as exc:
        raise ResourceLimitError(f"cannot verify cgroup-v2 CPU/RAM limits: {exc}") from exc


def prepare(config, spec, job_id):
    """Create a transient parent slice before any candidate code can execute.

    Docker daemon children escape the launcher scope, so all provider services
    must join this same slice. No system-disk unit/drop-in files are created.
    """
    backend = config.get("execution_backend", "process" if config.get("local_test") else "systemd")
    if backend == "process":
        if not config.get("local_test"):
            raise ResourceLimitError("unlimited process execution is only available in local-test mode")
        return {"backend": "process", "enforced": False, "scope": None, "slice": None, "cgroup": None}
    if backend != "systemd" or not (CGROUP_ROOT / "cgroup.controllers").exists():
        raise ResourceLimitError("systemd execution requires cgroup v2")
    if config.get("systemd_user", False) and not config.get("local_test"):
        raise ResourceLimitError("production Docker jobs require a system-manager resource slice")
    parent, scope = names(job_id)
    properties = [("Description", "s", "Managed GPU job resource boundary"),
                  ("MemoryAccounting", "b", "true"), ("CPUAccounting", "b", "true"),
                  ("MemoryMax", "t", str(spec["memory_max_mib"] * MIB)),
                  ("MemoryHigh", "t", str(spec["memory_high_mib"] * MIB)),
                  ("MemorySwapMax", "t", "0"),
                  ("CPUQuotaPerSecUSec", "t", str(spec["cpu_cores"] * 1000000))]
    command = ["busctl", *(["--user"] if config.get("systemd_user", False) else []),
               "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
               "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
               parent, "fail", str(len(properties))]
    for prop in properties:
        command.extend(prop)
    command.append("0")
    _run(command)
    try:
        path = unit_cgroup(config, parent)
        contract = verify_cgroup(path, spec)
    except Exception:
        _run(_systemctl(config, "stop", parent))
        raise
    return {"backend": "systemd", "enforced": True, "slice": parent, "scope": scope,
            "cgroup": str(path), "verified_at_epoch": time.time(), **contract}


def command(config, spec, boundary):
    if boundary["backend"] == "process":
        return spec["command"]
    return ["systemd-run", *(["--user"] if config.get("systemd_user", False) else []),
            "--scope", "--quiet", "--unit=" + boundary["scope"], "--slice=" + boundary["slice"],
            "--property=MemoryMax=" + str(spec["memory_max_mib"] * MIB),
            "--property=MemoryHigh=" + str(spec["memory_high_mib"] * MIB),
            "--property=MemorySwapMax=0", "--property=CPUQuota=" + str(spec["cpu_cores"] * 100) + "%",
            "--", "env", "GPU_SCHEDULER_RESOURCE_SLICE=" + boundary["slice"], *spec["command"]]


def cleanup(config, boundary):
    if boundary is None or boundary["backend"] == "process":
        return True
    # Stop only the exact scheduler-created job slice; this includes daemon
    # children even when a candidate erased the inherited process token.
    _run(_systemctl(config, "stop", boundary["slice"]))
    path = Path(boundary["cgroup"])
    stopped = not path.exists() or _values(path / "cgroup.events").get("populated") == 0
    if stopped:
        # Failed transient scopes otherwise retain empty unit metadata forever.
        try:
            _run(_systemctl(config, "reset-failed", boundary["scope"], boundary["slice"]))
        except ResourceLimitError:
            pass  # Already unloaded scopes have no resources left to reclaim.
    return stopped


def current_boundary():
    """Read the private host launch/limit receipts; never trust candidate env limits."""
    from .container_ownership import _trusted
    value = os.environ.get("GPU_SCHEDULER_JOB_DIR")
    if not value:
        return None
    directory = Path(value)
    if (not directory.is_absolute() or directory.resolve(strict=True) != directory or
            not _trusted(directory, directory=True) or not _trusted(directory / "launch.json")):
        raise ResourceLimitError("resource limits require private scheduler-owned receipts")
    launch = read_json(directory / "launch.json")
    if (launch["id"] != os.environ.get("GPU_SCHEDULER_JOB_ID") or
            launch["token"] != os.environ.get("AUTORESEARCH_PROCESS_TOKEN")):
        raise ResourceLimitError("resource limits do not match the launching job")
    if (launch["config"].get("local_test") and
            launch["config"].get("execution_backend", "process") == "process"):
        return None
    if not _trusted(directory / "resource-limits.json"):
        raise ResourceLimitError("resource limits require a verified private receipt")
    receipt = read_json(directory / "resource-limits.json")
    if receipt["backend"] == "process" and launch["config"].get("local_test"):
        return None
    parent, scope = names(launch["id"])
    if (not receipt.get("enforced") or receipt.get("backend") != "systemd" or
            receipt.get("slice") != parent or receipt.get("scope") != scope):
        raise ResourceLimitError("resource slice receipt was not verified")
    expected = unit_cgroup(launch["config"], parent)
    if str(expected) != receipt["cgroup"]:
        raise ResourceLimitError("resource slice identity changed")
    verify_cgroup(expected, launch["spec"])
    return launch, receipt


def docker_overrides(service=None):
    boundary = current_boundary()
    if boundary is None:
        return {}
    launch, receipt = boundary
    spec = launch["spec"]
    # The shared parent bounds the aggregate main + sidecars + host launcher.
    maximum, cpu = spec["memory_max_mib"] * MIB, spec["cpu_cores"]
    service = service or {}
    limits = service.get("deploy", {}).get("resources", {}).get("limits", {})
    for value in (service.get("mem_limit"), limits.get("memory")):
        if value is None:
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            parsed = value
        else:
            matched = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?(?:i?b)?)", str(value).lower())
            if matched is None:
                raise ResourceLimitError("cannot reconcile provider memory limit")
            unit = matched[2][:1]
            parsed = int(float(matched[1]) * 1024**({"k": 1, "m": 2, "g": 3, "t": 4}.get(unit, 0)))
        if parsed <= 0:
            raise ResourceLimitError("provider memory limit must be positive")
        maximum = min(maximum, parsed)
    for value in (service.get("cpus"), limits.get("cpus")):
        if value is not None:
            parsed = float(value)
            if not 0 < parsed <= 1024:
                raise ResourceLimitError("provider CPU limit must be positive and finite")
            cpu = min(cpu, parsed)
    return {"cgroup_parent": receipt["slice"], "mem_limit": maximum,
            "memswap_limit": maximum, "cpus": cpu}


def attest_container(inspected, groups):
    boundary = current_boundary()
    if boundary is None:
        return None
    launch, receipt = boundary
    spec, host = launch["spec"], inspected.get("HostConfig", {})
    maximum = host.get("Memory", 0)
    cpu = host.get("NanoCpus", 0) / 1000000000
    if not cpu and host.get("CpuPeriod", 0) > 0:
        cpu = host.get("CpuQuota", 0) / host["CpuPeriod"]
    if (host.get("CgroupParent") != receipt["slice"] or type(maximum) is not int or
            not 0 < maximum <= spec["memory_max_mib"] * MIB or
            host.get("MemorySwap") != maximum or not 0 < cpu <= spec["cpu_cores"] or
            host.get("Privileged") or (host.get("CapAdd") or [])):
        raise ResourceLimitError("Docker resource limits or privileges violate the job contract")
    relative = "/" + str(Path(receipt["cgroup"]).relative_to(CGROUP_ROOT))
    if not any(controller == "" and path.startswith(relative + "/") for controller, path in groups):
        raise ResourceLimitError("Docker container escaped the shared job resource slice")
    verify_cgroup(Path(receipt["cgroup"]), spec)
    return {"enforced": True, "slice": receipt["slice"], "aggregate_cgroup": receipt["cgroup"],
            "memory_max_bytes": maximum, "cpu_cores": cpu, "verified_at_epoch": time.time()}


class ResourceSampler:
    """Sample anon/file/shmem/kernel separately; memory.peak is total only."""
    def __init__(self, launch, boundary):
        self.launch, self.boundary = launch, boundary
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.usage = {"version": 1, "sample_count": 0, "gpu_sample_count": 0,
                      "ram_anon_peak_bytes": None, "ram_file_peak_bytes": None,
                      "ram_shmem_peak_bytes": None, "ram_kernel_peak_bytes": None,
                      "ram_total_peak_bytes": None, "gpu_memory_peak_mib": None,
                      "cpu_usage_usec": None, "memory_events": {}, "measurement_errors": [],
                      "gpu_compute_units_measured": False, "backend": boundary["backend"],
                      "sampling_interval_seconds": launch["config"].get("resource_sample_seconds", 1),
                      "memory_peak_basis": "memory.peak is kernel total; anon/file/shmem/kernel are sampled memory.stat maxima"}

    def sample(self, *, gpu=True):
        update = {}
        path = self.boundary.get("cgroup")
        if path:
            path = Path(path)
            stats = _values(path / "memory.stat")
            update = {f"ram_{kind}_peak_bytes": stats.get(kind, 0) for kind in ("anon", "file", "shmem", "kernel")}
            update["ram_total_peak_bytes"] = int((path / "memory.peak").read_text())
            update["cpu_usage_usec"] = _values(path / "cpu.stat").get("usage_usec")
            update["memory_events"] = _values(path / "memory.events")
        if gpu:
            try:
                from .resources import _query
                rows = _query("--query-compute-apps=gpu_uuid,pid,used_gpu_memory")
                pids = set()
                if path:
                    for entry in path.rglob("cgroup.procs"):
                        try:
                            pids.update(int(line) for line in entry.read_text().splitlines())
                        except FileNotFoundError:
                            continue
                else:
                    from .resources import process_map
                    job = {**self.launch, "directory": os.environ.get("GPU_SCHEDULER_JOB_DIR", "")}
                    snapshot = {"gpus": {self.launch["gpu_uuid"]: {"processes": [
                        {"pid": int(row[1])} for row in rows if row[0] == self.launch["gpu_uuid"]]}}}
                    pids = process_map([job], snapshot)[job["id"]]
                measured = 0
                for uuid, pid, memory in rows:
                    if uuid == self.launch["gpu_uuid"] and int(pid) in pids:
                        value = int(memory)
                        if value < 0:
                            raise ValueError("negative GPU memory sample")
                        measured += value
                update["gpu_memory_peak_mib"] = measured
                update["gpu_sample_count"] = 1
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self._error(exc)
        with self.lock:
            for key, value in update.items():
                if key == "gpu_sample_count":
                    self.usage[key] += value
                elif key.endswith("peak_bytes") or key == "gpu_memory_peak_mib":
                    self.usage[key] = max(self.usage[key] or 0, value)
                else:
                    self.usage[key] = value
            if path:
                self.usage["sample_count"] += 1

    def _error(self, exc):
        message = type(exc).__name__ + ": " + str(exc)
        with self.lock:
            if message not in self.usage["measurement_errors"] and len(self.usage["measurement_errors"]) < 8:
                self.usage["measurement_errors"].append(message)

    def _loop(self):
        interval = self.launch["config"].get("resource_sample_seconds", 1)
        while not self.stop_event.is_set():
            try:
                self.sample(gpu=not self.launch["config"].get("local_test"))
            except (OSError, ValueError) as exc:
                self._error(exc)
            self.stop_event.wait(interval)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="job-resource-sampler", daemon=True)
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3.1)
        try:
            self.sample(gpu=False)
        except (OSError, ValueError) as exc:
            self._error(exc)
        with self.lock:
            return {**self.usage, "observed_seconds": max(0, time.monotonic() - self.started)}
