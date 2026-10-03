"""Non-preemptive owner fairness and backfill using enforced runtime bounds.

Forecasts assume external memory usage stays unchanged. They are diagnostics,
not promised start times. Allocation always uses a fresh real snapshot.
"""
from __future__ import annotations

from .resources import choose_gpu

# Executor cleanup plus a complete telemetry/dispatch interval before release.
CLEANUP_SECONDS = 10


def protected(job, config, now):
    return (job["bypasses"] >= config["max_bypass"] or
            now - job["submitted_mono"] >= config["starvation_seconds"])


def order_queue(queue, active, config, owner_dispatch, now):
    if config["scheduling_policy"] == "fifo" or config["max_bypass"] == 0:
        return sorted(queue, key=lambda j: j["submitted_mono"])
    totals = {k: sum(g[k] for g in config["gpus"]) for k in ("memory_mib", "compute_units")}
    totals.update(cpu_cores=config["cpu_cores"], ram_mib=config["ram_mib"])
    usage = {}
    for job in active:
        owner = job["spec"]["owner"]
        row = usage.setdefault(owner, {k: 0 for k in totals})
        for key in totals:
            row[key] += job["spec"][key] / totals[key]

    def key(job):
        if protected(job, config, now):
            return (0, job["submitted_mono"], 0, 0)
        owner = job["spec"]["owner"]
        share = max(usage.get(owner, {}).values(), default=0)
        return (1, share, owner_dispatch.get(owner, -1), job["submitted_mono"])

    return sorted(queue, key=key)


def release_at(job, now):
    if job.get("state") in ("UNKNOWN", "CANCELLING"):
        return None
    deadline = job.get("execution_deadline_mono")
    if deadline is None or deadline + CLEANUP_SECONDS <= now:
        return None
    return deadline + CLEANUP_SECONDS


def forecast_snapshot(snapshot, released, pid_sets):
    if snapshot is None:
        return None
    pids = set().union(*(pid_sets.get(j["id"], set()) for j in released)) if released else set()
    gpus = {}
    for uuid, live in snapshot["gpus"].items():
        freed = sum(p["memory_mib"] for p in live["processes"] if p["pid"] in pids)
        gpus[uuid] = {**live, "used_mib": max(0, live["used_mib"] - freed),
                      "processes": [p for p in live["processes"] if p["pid"] not in pids]}
    return {**snapshot, "gpus": gpus}


def projected_start(spec, active, config, snapshot, quarantined, pid_sets, now):
    releases = {j["id"]: release_at(j, now) for j in active}
    points = sorted({now, *(t for t in releases.values() if t is not None)})
    for at in points:
        released = [j for j in active if releases[j["id"]] is not None and releases[j["id"]] <= at]
        remaining = [j for j in active if j not in released]
        future = forecast_snapshot(snapshot, released, pid_sets)
        gpu, _ = choose_gpu(spec, remaining, config, future, quarantined, pid_sets=pid_sets)
        if gpu is not None:
            return at
    return None


def safe_backfill(candidate, gpu_uuid, blockers, active, config, snapshot, quarantined, pid_sets, now):
    hypothetical = {"id": "backfill/" + candidate["id"], "gpu_uuid": gpu_uuid,
                    "spec": candidate["spec"], "state": "RUNNING",
                    "execution_deadline_mono": now + candidate["spec"]["max_runtime_seconds"]}
    with_candidate = [*active, hypothetical]
    for blocker in blockers:
        before = projected_start(blocker["spec"], active, config, snapshot, quarantined, pid_sets, now)
        if before is not None:
            after = projected_start(blocker["spec"], with_candidate, config, snapshot, quarantined, pid_sets, now)
            if after is None or after > before:
                return False
        else:
            # No reliable release forecast (e.g. external use on another GPU).
            # Permit only work that leaves full declared capacity for the blocker.
            fits = False
            for target in config["gpus"]:
                if blocker["spec"].get("gpu_uuid", target["uuid"]) != target["uuid"]:
                    continue
                if (blocker["spec"]["memory_mib"] > target["memory_mib"] or
                        blocker["spec"]["compute_units"] > target["compute_units"]):
                    continue
                reservation = {"id": "reserved/" + blocker["id"], "gpu_uuid": target["uuid"],
                               "spec": blocker["spec"]}
                selected, _ = choose_gpu({**candidate["spec"], "gpu_uuid": gpu_uuid},
                                         [*active, reservation], config, snapshot, quarantined,
                                         pid_sets=pid_sets)
                if selected is not None:
                    fits = True
                    break
            if not fits:
                return False
    return True
