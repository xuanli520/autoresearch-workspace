"""Fresh telemetry plus conservative declared resource accounting."""
from __future__ import annotations

import csv
import io
import subprocess
import time
from typing import Any

from .common import processes
from .container_ownership import container_process_map


Telemetry = dict[str, Any]
Job = dict[str, Any]
DEFAULT_SHARED_HEADROOM_MIB = 2048


def _query(fields: str) -> list[list[str]]:
    result = subprocess.run(["nvidia-smi", fields, "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=3, check=True)
    return list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True))


def probe() -> Telemetry:
    gpus = {}
    for row in _query("--query-gpu=uuid,memory.total,memory.used,utilization.gpu"):
        if len(row) != 4:
            raise ValueError("invalid GPU telemetry")
        uuid, total, used, util = row
        total, used, util = int(total), int(used), int(util)
        if uuid in gpus or total <= 0 or not 0 <= used <= total or not 0 <= util <= 100:
            raise ValueError("invalid GPU telemetry values")
        gpus[uuid] = dict(total_mib=total, used_mib=used, utilization=util, processes=[])
    for row in _query("--query-compute-apps=gpu_uuid,pid,used_gpu_memory"):
        if len(row) != 3 or row[0] not in gpus:
            raise ValueError("invalid compute-process telemetry")
        pid, memory = int(row[1]), int(row[2])
        live = gpus[row[0]]
        if pid <= 0 or memory < 0 or memory > live["total_mib"] or any(p["pid"] == pid for p in live["processes"]):
            raise ValueError("invalid compute-process telemetry values")
        live["processes"].append({"pid": pid, "memory_mib": memory})
    return {"at": time.time(), "gpus": gpus}


def simulated_probe(config: dict[str, Any]) -> Telemetry:
    return {"at": time.time(), "gpus": {g["uuid"]: {
        "total_mib": g["memory_mib"], "used_mib": 0, "utilization": 0, "processes": []
    } for g in config["gpus"]}}


def process_map(active: list[Job], snapshot: Telemetry | None = None) -> dict[str, set[int]]:
    pid_sets = {j["id"]: {pid for pid, _ in processes.scope_members(j["token"])} for j in active}
    gpu_pids = {p["pid"] for gpu in (snapshot or {}).get("gpus", {}).values()
                for p in gpu["processes"]}
    for job_id, pids in container_process_map(active, gpu_pids).items():
        pid_sets[job_id].update(pids)
    return pid_sets


def choose_gpu(spec: dict[str, Any], active: list[Job], config: dict[str, Any],
               snapshot: Telemetry | None, quarantined: set[str], *,
               pid_sets: dict[str, set[int]] | None = None) -> tuple[str | None, str]:
    if len(active) >= config.get("max_running", 2):
        return None, "global_concurrency_limit"
    for key in ("cpu_cores", "ram_mib"):
        if sum(job["spec"][key] for job in active) + spec[key] > config[key]:
            return None, f"insufficient_{key}"
    if not snapshot or time.time() - snapshot["at"] > max(10, config["poll_seconds"] * 3):
        return None, "telemetry_unavailable_or_stale"
    # Host token scopes plus attested Docker cgroups; unknown processes stay external.
    pid_sets = process_map(active, snapshot) if pid_sets is None else pid_sets
    reasons, choices = [], []
    for gpu in config["gpus"]:
        uuid = gpu["uuid"]
        if spec.get("gpu_uuid", uuid) != uuid:
            continue
        live = snapshot["gpus"].get(uuid)
        if uuid in quarantined:
            reasons.append(f"{uuid}:quarantined")
            continue
        if live is None or gpu["memory_mib"] > live["total_mib"]:
            reasons.append(f"{uuid}:missing_or_capacity_mismatch")
            continue
        assigned = [j for j in active if j["gpu_uuid"] == uuid]
        owners: dict[int, list[str]] = {}
        for job in active:
            for pid in pid_sets.get(job["id"], set()):
                owners.setdefault(pid, []).append(job["id"])
        # Ambiguous or wrong-GPU ownership cannot reduce any job's reservation.
        assigned_ids = {job["id"] for job in assigned}
        known_pids = {pid for pid, jobs in owners.items() if len(jobs) == 1 and jobs[0] in assigned_ids}
        external = any(p["pid"] not in known_pids for p in live["processes"])
        shared = config.get("external_process_policy", "exclusive_admission") == "shared"
        if external and not shared:
            reasons.append(f"{uuid}:external_or_unidentified_process")
            continue
        reserved_compute = sum(j["spec"]["compute_units"] for j in assigned)
        # Both queries report FB memory, including framework allocator caches.
        # They are sampled separately: never subtract a newer process reading
        # from an older, smaller GPU total. Driver overhead is in memory.used.
        committed = max(live["used_mib"], sum(p["memory_mib"] for p in live["processes"]))
        if shared:
            committed += config.get("shared_headroom_mib", DEFAULT_SHARED_HEADROOM_MIB)
        for job in assigned:
            measured = sum(p["memory_mib"] for p in live["processes"]
                           if p["pid"] in known_pids and owners[p["pid"]] == [job["id"]])
            committed += max(0, job["spec"]["memory_mib"] - measured)
        if committed + spec["memory_mib"] > gpu["memory_mib"]:
            reasons.append(f"{uuid}:insufficient_memory_reservation")
        elif reserved_compute + spec["compute_units"] > gpu["compute_units"]:
            reasons.append(f"{uuid}:insufficient_compute_units")
        else:
            choices.append((live["utilization"], committed, uuid))
    if not choices:
        return None, ";".join(reasons) or "no_matching_gpu"
    return min(choices)[2], "ready"
