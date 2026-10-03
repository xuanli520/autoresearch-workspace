"""Renew an existing shared runtime; use systemd for deadlines and continuation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import pwd
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.gpu_scheduler.common import atomic_json, check_storage, validate_config
from tools.research_handoff.core import processes


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(pid):
    return {"pid": pid, "start_ticks": processes.process_start_ticks(pid),
            "boot_id": processes.boot_id()}


def alive(item):
    return processes.pid_matches(item["pid"], item["start_ticks"], item["boot_id"])


def argv(pid):
    return [part.decode() for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]


def child(parent, executable):
    ids = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
    matches = [int(pid) for pid in ids if Path(argv(int(pid))[0]).name == executable]
    if len(matches) != 1:
        raise ValueError(f"expected one {executable} child of recorded process {parent}")
    return matches[0]


def load(path):
    c = read(path)
    cfg = validate_config(read(c["gpu_config"]))
    check_storage(cfg, path, c["root"], c["runtime_contract"], c["runtime_launch"],
                  c["gpu_launch"], c["gpu_root"])
    if c["gpu_root"] != cfg["root"] or not c["authorization"].strip():
        raise ValueError("renewal identity or authorization is invalid")
    if not 0 < c["renewed_deadline_epoch"] - c["original_deadline_epoch"] <= 172800:
        raise ValueError("renewal must be at most 48 hours after the original deadline")
    contract = read(c["runtime_contract"])
    for name, expected in c["source_sha256"].items():
        check_storage(cfg, name)
        if digest(name) != expected:
            raise ValueError(f"immutable renewal input changed: {name}")
    if contract["docker_host"] != c["docker_host"]:
        raise ValueError("shared Docker socket changed")
    return c, cfg


def runtime_identities(c):
    result = []
    for wrapper in read(c["runtime_launch"])["processes"]:
        if not alive(wrapper):
            raise ValueError("recorded shared runtime wrapper is no longer alive")
        name = wrapper["name"]
        timer = identity(child(wrapper["pid"], "timeout"))
        daemon = identity(child(timer["pid"], name))
        command = argv(daemon["pid"])
        flag = "--config" if name == "containerd" else "--config-file"
        expected = c["containerd_config"] if name == "containerd" else c["docker_config"]
        if command[command.index(flag) + 1] != expected:
            raise ValueError("recorded daemon config changed")
        result.append({"name": name, "timeout": timer, "daemon": daemon, "argv": command})
    if {item["name"] for item in result} != {"containerd", "dockerd"}:
        raise ValueError("both shared runtime daemons are required")
    return result


def run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=20, check=True).stdout


def calendar(epoch):
    return datetime.fromtimestamp(math.ceil(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def schedule(c, config_path, suffix, epoch, operation, *, user=None):
    unit = c["unit_prefix"] + "-" + suffix
    properties = ["--property=Type=oneshot", "--property=TimeoutStartSec=0",
                  "--property=StandardOutput=append:" + str(Path(c["root"]) / (suffix + ".log")),
                  "--property=StandardError=inherit", "--property=WorkingDirectory=" + c["root"],
                  "--property=Environment=PYTHONDONTWRITEBYTECODE=1",
                  "--property=Environment=TMPDIR=" + c["tmp"]]
    if user:
        properties.append("--property=User=" + user)
    command = ["systemd-run", "--unit=" + unit, "--on-calendar=" + calendar(epoch),
               "--timer-property=AccuracySec=1s", *properties, sys.executable, "-B",
               str(Path(__file__).resolve()), operation, "--config", str(config_path)]
    run(command)
    return {"unit": unit, "at_epoch": epoch, "argv": command}


def probe(root):
    if os.geteuid() != 0:
        raise ValueError("timeout detachment probe must run as root")
    proc = subprocess.Popen(["timeout", "--signal=TERM", "--kill-after=1", "2", "sleep", "30"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    daemon = None
    try:
        end = time.monotonic() + 2
        while daemon is None:
            try:
                daemon = identity(child(proc.pid, "sleep"))
            except (OSError, ValueError):
                if time.monotonic() >= end:
                    raise RuntimeError("probe child did not start")
                time.sleep(.02)
        processes.signal_identity(proc.pid, processes.process_start_ticks(proc.pid), signal.SIGKILL,
                                  processes.boot_id())
        proc.wait(timeout=2)
        time.sleep(2.2)
        result = {"timeout_exit_code": proc.returncode, "child_survived_original_deadline": alive(daemon),
                  "daemon": daemon, "at_epoch": time.time()}
        if not result["child_survived_original_deadline"]:
            raise RuntimeError("GNU timeout cannot be detached on this host")
    finally:
        if daemon:
            processes.signal_identity(daemon["pid"], daemon["start_ticks"], signal.SIGTERM, daemon["boot_id"])
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    atomic_json(Path(root) / "timeout-probe.json", result)
    return result


def install(path):
    if os.geteuid() != 0:
        raise ValueError("systemd renewal installation must run as root")
    c, cfg = load(path)
    root = Path(c["root"])
    if (root / "installed.json").exists() or time.time() >= c["original_deadline_epoch"] - 30:
        raise ValueError("already installed or original service is about to expire; inspect receipts")
    if not read(root / "timeout-probe.json")["child_survived_original_deadline"]:
        raise ValueError("required timeout detachment probe is missing")
    daemons = runtime_identities(c)
    if not alive(read(c["gpu_launch"])):
        raise ValueError("original GPU service identity is no longer alive")
    uid = Path(c["gpu_root"]).stat().st_uid
    user = pwd.getpwuid(uid).pw_name
    planned = {"config_sha256": digest(path), "daemons": daemons,
               "original_deadline_epoch": c["original_deadline_epoch"],
               "renewed_deadline_epoch": c["renewed_deadline_epoch"], "uid": uid}
    atomic_json(root / "planned.json", planned)
    (root / "planned.json").chmod(0o644)
    installed = []
    try:
        installed.append(schedule(c, path, "expiry", c["renewed_deadline_epoch"], "expire"))
        installed.append(schedule(c, path, "gpu", c["original_deadline_epoch"] + 30,
                                  "continue-gpu", user=user))
        for item in installed:
            run(["systemctl", "is-active", item["unit"] + ".timer"])
    except Exception:
        for item in installed:
            run(["systemctl", "stop", item["unit"] + ".timer"])
        raise
    # Arm the replacement hard deadline before removing only the old timeout supervisors.
    changes = []
    for item in daemons:
        if not alive(item["daemon"]) or not alive(item["timeout"]):
            raise RuntimeError("runtime identity changed before timeout detachment")
        if not processes.signal_identity(item["timeout"]["pid"], item["timeout"]["start_ticks"],
                                         signal.SIGKILL, item["timeout"]["boot_id"]):
            raise RuntimeError("recorded timeout wrapper could not be detached")
        changes.append(item["name"])
        atomic_json(root / "detachment.json", {"detached": changes, "at_epoch": time.time()})
    time.sleep(.2)
    if not all(alive(item["daemon"]) for item in daemons):
        raise RuntimeError("shared daemon did not survive timeout detachment")
    result = {**planned, "timers": installed, "runtime_live_renewal": True,
              "gpu_continuation_scheduled": True, "research_deadlines_changed": False,
              "at_epoch": time.time()}
    atomic_json(root / "installed.json", result)
    return result


def expire(path):
    c, cfg = load(path)
    root = Path(c["root"])
    planned = read(root / "planned.json")
    if digest(path) != planned["config_sha256"] or time.time() < c["renewed_deadline_epoch"]:
        raise ValueError("renewal expiry is premature or inputs changed")
    result = []
    for item in reversed(planned["daemons"]):
        d = item["daemon"]
        sent = processes.signal_identity(d["pid"], d["start_ticks"], signal.SIGTERM, d["boot_id"])
        result.append({"name": item["name"], "same_identity": sent})
    end = time.monotonic() + 10
    while any(alive(item["daemon"]) for item in planned["daemons"]) and time.monotonic() < end:
        time.sleep(.1)
    for item in planned["daemons"]:
        d = item["daemon"]
        if alive(d):
            processes.signal_identity(d["pid"], d["start_ticks"], signal.SIGKILL, d["boot_id"])
    atomic_json(root / "expiry.json", {"at_epoch": time.time(), "signals": result})
    return result


def continue_gpu(path):
    c, cfg = load(path)
    root = Path(c["root"])
    planned = read(root / "planned.json")
    if digest(path) != planned["config_sha256"] or time.time() < c["original_deadline_epoch"]:
        raise ValueError("GPU continuation is premature or inputs changed")
    if alive(read(c["gpu_launch"])):
        raise ValueError("original GPU server remains alive; refusing another queue")
    previous = Path(c["gpu_root"]) / "sessions" / c["gpu_session"] / "service-exit.json"
    if not read(previous)["cleanup_confirmed"]:
        raise ValueError("original GPU session did not confirm cleanup")
    remaining = c["renewed_deadline_epoch"] - time.time()
    if remaining <= 0:
        raise ValueError("renewed infrastructure deadline has expired")
    raw = read(c["gpu_config"])
    raw.update(service_seconds=min(172800, remaining),
               infrastructure_lease={"authorization": c["authorization"],
                                     "deadline_epoch": c["renewed_deadline_epoch"]})
    atomic_json(root / "gpu-config-renewed.json", raw)
    atomic_json(root / "gpu-continuation.json", {"at_epoch": time.time(), "identity": identity(os.getpid()),
                                                "old_session": c["gpu_session"], "gpu_root": c["gpu_root"],
                                                "renewed_deadline_epoch": c["renewed_deadline_epoch"]})
    from tools.gpu_scheduler.server import serve
    asyncio.run(serve(raw))
    return {"at_epoch": time.time(), "service_exited": True}


def status(path):
    c, cfg = load(path)
    root = Path(c["root"])
    files = ("timeout-probe.json", "planned.json", "detachment.json", "installed.json",
             "gpu-continuation.json", "expiry.json")
    receipts = {name: read(root / name) for name in files if (root / name).exists()}
    units = [c["unit_prefix"] + "-" + suffix + extension
             for suffix in ("expiry", "gpu") for extension in (".timer", ".service")]
    result = subprocess.run(["systemctl", "show", *units, "--no-pager",
                             "--property=Id,LoadState,ActiveState,SubState,NextElapseUSecRealtime,Result"],
                            capture_output=True, text=True, timeout=20)
    return {"at_epoch": time.time(), "receipts": receipts, "systemd": result.stdout,
            "runtime_daemons_alive": [alive(item["daemon"]) for item in receipts.get("planned.json", {}).get("daemons", [])]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("operation", choices=("probe", "install", "status", "expire", "continue-gpu",
                                        "install-gpu-plan", "serve-gpu-plan"))
    p.add_argument("--config", required=True, type=Path)
    args = p.parse_args()
    if args.operation in ("install-gpu-plan", "serve-gpu-plan"):
        from tools.gpu_scheduler.managed_service import install_plan, serve_plan
        try:
            result = {"install-gpu-plan": install_plan, "serve-gpu-plan": serve_plan}[args.operation](args.config)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(78) from exc
    elif args.operation == "probe":
        c, cfg = load(args.config)
        result = probe(c["root"])
    else:
        result = {"install": install, "status": status, "expire": expire,
                  "continue-gpu": continue_gpu}[args.operation](args.config)
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
