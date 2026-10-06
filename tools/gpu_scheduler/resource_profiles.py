"""Explainable resource suggestions derived only from completed measured jobs."""
from __future__ import annotations

import hashlib
import json
import math


MIB = 1024**2


def profile_key(spec):
    job_class = spec.get("job_class", "generic")
    # A named class is a caller-declared workload/version identity. Generic
    # legacy jobs require an exact command match before sharing measurements.
    identity = {"owner": spec["owner"], "job_class": spec.get("job_class", "generic"),
                "command": spec["command"] if job_class == "generic" else spec["command"][:1]}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def suggest(spec, jobs, *, min_samples=3):
    """Do not mutate an accepted spec or confuse total peaks with anon peaks.

    Named classes must change when data shapes, method or protocol changes.
    Generic legacy commands are deliberately kept in separate profiles.
    """
    candidates = []
    for job in jobs:
        if job.get("state") != "SUCCEEDED" or profile_key(job["spec"]) != profile_key(spec):
            continue
        receipt = job.get("exit") or {}
        usage = receipt.get("resource_usage") or job.get("resource_usage")
        if (receipt.get("cleanup_ok") and usage and usage.get("sample_count", 0) > 0 and
                not usage.get("measurement_errors") and not usage.get("memory_events", {}).get("oom", 0)):
            candidates.append(usage)
    result = {"profile_key": profile_key(spec), "sample_jobs": len(candidates),
              "owner": spec["owner"], "job_class": spec.get("job_class", "generic"),
              "applied": False, "cpu_cores": spec["cpu_cores"], "compute_units": spec["compute_units"],
              "compute_basis": "declared; per-job GPU CU peak is not measurable by device-wide utilization"}
    if len(candidates) < min_samples:
        return {**result, "ram_mib": spec["ram_mib"], "memory_mib": spec["memory_mib"],
                "memory_max_mib": spec.get("memory_max_mib", spec["ram_mib"]),
                "minimum_sample_jobs": min_samples, "basis": "cold_start_declaration"}
    anon = max(row.get("ram_anon_peak_bytes") or 0 for row in candidates)
    shmem = max(row.get("ram_shmem_peak_bytes") or 0 for row in candidates)
    kernel = max(row.get("ram_kernel_peak_bytes") or 0 for row in candidates)
    total = max(row.get("ram_total_peak_bytes") or 0 for row in candidates)
    reserve = math.ceil((anon + shmem + kernel) * 1.25 / MIB) + 256
    maximum = max(reserve, math.ceil(total * 1.2 / MIB) + 256)
    gpu = [row["gpu_memory_peak_mib"] for row in candidates if row.get("gpu_sample_count", 0) > 0
           and row.get("gpu_memory_peak_mib") is not None and row["gpu_memory_peak_mib"] > 0]
    return {**result, "ram_mib": reserve, "memory_max_mib": maximum,
            "memory_mib": math.ceil(max(gpu) * 1.1) + 256 if gpu else spec["memory_mib"],
            "basis": "max_observed_anon_shmem_kernel_plus_25pct_256MiB; total_limit_plus_20pct_256MiB",
            "cache_policy": "file cache remains charged to memory.max; memory.high throttles total usage",
            "measurement_scope": "sampled peaks; command fingerprint does not attest unchanged input data"}


def suggestions(spec, jobs, config=None):
    return suggest(spec, jobs, min_samples=(config or {}).get("profile_min_samples", 3))
