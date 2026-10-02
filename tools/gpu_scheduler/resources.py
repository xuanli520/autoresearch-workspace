"""Fresh telemetry plus conservative declared resource accounting."""
from __future__ import annotations

import csv
import io
import subprocess
import time

from .common import processes


def _query(fields):
    result = subprocess.run(["nvidia-smi", fields, "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=3, check=True)
    return list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True))


def probe():
    gpus = {}
    for row in _query("--query-gpu=uuid,memory.total,memory.used,utilization.gpu"):
        if len(row) != 4:
            raise ValueError("invalid GPU telemetry")
        uuid, total, used, util = row
        gpus[uuid] = dict(total_mib=int(total), used_mib=int(used), utilization=int(util), processes=[])
    for row in _query("--query-compute-apps=gpu_uuid,pid,used_gpu_memory"):
        if len(row) != 3 or row[0] not in gpus:
            raise ValueError("invalid compute-process telemetry")
        gpus[row[0]]["processes"].append({"pid": int(row[1]), "memory_mib": int(row[2])})
    return {"at": time.time(), "gpus": gpus}


def simulated_probe(config):
    return {"at": time.time(), "gpus": {g["uuid"]: {
        "total_mib": g["memory_mib"], "used_mib": 0, "utilization": 0, "processes": []
    } for g in config["gpus"]}}


def choose_gpu(spec, active, config, snapshot, quarantined):
    if len(active) >= 2:
        return None, "global_concurrency_limit"
    for key in ("cpu_cores", "ram_mib"):
        if sum(job["spec"][key] for job in active) + spec[key] > config[key]:
            return None, f"insufficient_{key}"
    if not snapshot or time.time() - snapshot["at"] > max(10, config["poll_seconds"] * 3):
        return None, "telemetry_unavailable_or_stale"
    # Associate only observable same-UID descendants retaining our random token.
    pid_sets = {j["id"]: {pid for pid, _ in processes.scope_members(j["token"])} for j in active}
    known_pids = set().union(*pid_sets.values()) if pid_sets else set()
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
        external = any(p["pid"] not in known_pids for p in live["processes"])
        shared = config.get("external_process_policy", "exclusive_admission") == "shared"
        if external and not shared:
            reasons.append(f"{uuid}:external_or_unidentified_process")
            continue
        assigned = [j for j in active if j["gpu_uuid"] == uuid]
        reserved_compute = sum(j["spec"]["compute_units"] for j in assigned)
        # Start with all measured usage (including driver overhead). Add every
        # unconsumed reservation, so a lazy allocator cannot lend its future peak.
        committed = live["used_mib"]
        if shared:
            committed += config.get("shared_headroom_mib", 2048)
        for job in assigned:
            measured = sum(p["memory_mib"] for p in live["processes"] if p["pid"] in pid_sets[job["id"]])
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
