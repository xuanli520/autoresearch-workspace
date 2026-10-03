"""Persistent systemd lifecycle for the GPU queue alone, without lease renewal.

systemd restarts the service, never jobs. Unconfirmed cleanup fails closed.
Docker, containerd and research controllers are outside this module's scope.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import pwd
import re
import subprocess
import time
from pathlib import Path

from .common import (atomic_json, check_storage, fields, processes,
                     read_json, validate_config)
from .resources import probe


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_plan(path):
    plan = read_json(path)
    fields(plan, {"version", "root", "gpu_config", "python", "unit_name",
                  "authorization", "source_sha256"})
    if type(plan["version"]) is not int or plan["version"] != 1:
        raise ValueError("service plan version must be 1")
    if not isinstance(plan["authorization"], str) or not plan["authorization"].strip():
        raise ValueError("service plan requires explicit authorization")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", plan["unit_name"]):
        raise ValueError("invalid managed service unit name")
    for name in ("root", "gpu_config", "python"):
        # These paths also appear in systemd units; disallow specifier/env expansion.
        if not isinstance(plan[name], str) or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", plan[name]):
            raise ValueError(f"invalid systemd path: {name}")
    raw = read_json(plan["gpu_config"])
    config = validate_config(raw)
    if not config["persistent"]:
        raise ValueError("managed persistent service requires persistent: true")
    check_storage(config, path, plan["root"], plan["gpu_config"])
    hashes = plan["source_sha256"]
    required = {str(p.resolve()) for p in Path(__file__).parent.glob("*.py")}
    required.update({plan["gpu_config"], str(Path(__file__).resolve().parents[1] /
                                           "research_handoff/core/processes.py")})
    if not isinstance(hashes, dict) or not required.issubset(hashes):
        raise ValueError("service plan must pin its lifecycle code and GPU config")
    for source, expected in hashes.items():
        check_storage(config, source)
        if digest(source) != expected:
            raise ValueError(f"immutable service input changed: {source}")
    return plan, config, raw


def previous_session(config):
    latest = Path(config["root"]) / "service.json"
    if not latest.exists():
        return None
    sid = read_json(latest)["session_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", sid):
        raise ValueError("invalid previous GPU session identity")
    directory = Path(config["root"]) / "sessions" / sid
    service = read_json(directory / "service.json")
    exit_path = directory / "service-exit.json"
    receipt = read_json(exit_path) if exit_path.exists() else None
    if receipt and receipt.get("cleanup_confirmed"):
        return {"session_id": sid, "cleanup_confirmed": True}
    recorded = service.get("identity", {})
    old_boot = recorded.get("boot_id")
    if old_boot and old_boot != processes.boot_id():
        # A reboot ends local executors; preserve the missing exit, never invent it.
        return {"session_id": sid, "cleanup_confirmed": False,
                "previous_boot_ended": True, "old_boot_id": old_boot}
    if not old_boot:
        raise ValueError("previous GPU session has no verified process identity or cleanup receipt")
    if processes.pid_matches(recorded.get("pid"), recorded.get("start_ticks"), old_boot):
        raise ValueError("previous GPU service is still alive")
    checked = 0
    for launch_path in (directory / "jobs").glob("*/launch.json"):
        launch = read_json(launch_path)
        job_exit = launch_path.with_name("exit.json")
        if processes.scope_members(launch["token"]) or not job_exit.exists():
            # systemd may retry after the independent executor finishes. No signals.
            raise RuntimeError("previous GPU executor has not finished; waiting for its existing deadline")
        if not read_json(job_exit).get("cleanup_ok"):
            raise ValueError("previous GPU job cleanup failed; inspect it before starting")
        checked += 1
    return {"session_id": sid, "cleanup_confirmed": False, "executor_cleanup_verified": True,
            "checked_jobs": checked, "jobs_replayed": False}


def serve_plan(path):
    plan, config, raw = load_plan(path)
    previous = previous_session(config)
    snapshot = probe()
    for gpu in config["gpus"]:
        live = snapshot["gpus"].get(gpu["uuid"])
        if live is None or live["total_mib"] < gpu["memory_mib"]:
            raise ValueError(f"configured GPU is absent or too small: {gpu['uuid']}")
    root = Path(plan["root"])
    atomic_json(root / "current-launch.json", {
        "at_epoch": time.time(), "plan_sha256": digest(path), "previous": previous,
        "identity": {"pid": os.getpid(), "start_ticks": processes.process_start_ticks(os.getpid()),
                     "boot_id": processes.boot_id()},
        "persistent": True, "deadline_epoch": None,
        "jobs_replayed": False})
    from .server import serve
    asyncio.run(serve(raw))
    latest = read_json(Path(config["root"]) / "service.json")
    receipt = read_json(Path(config["root"]) / "sessions" / latest["session_id"] / "service-exit.json")
    if not receipt.get("cleanup_confirmed"):
        raise ValueError("GPU cleanup unconfirmed; automatic session continuation is disabled")
    if receipt.get("reason") == "disk_space":
        raise ValueError("GPU service stopped for low disk space; inspect before restarting")
    return {"session_id": latest["session_id"], "cleanup_confirmed": True}


def unit_files(plan, config, path):
    root, name = Path(plan["root"]), plan["unit_name"]
    user = pwd.getpwuid(Path(config["root"]).stat().st_uid).pw_name
    script = Path(__file__).with_name("lifecycle.py").resolve()
    for value in (str(path), str(script), str(config["data_mount"])):
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", value):
            raise ValueError("systemd unit paths must be absolute without expansion characters")
    command = f"{plan['python']} -B {script}"
    common = f"[Unit]\nRequiresMountsFor={config['data_mount']}\nAfter=time-sync.target\n"
    return {
        name + ".service": common + f"""Description=Persistent managed GPU queue
StartLimitIntervalSec=0

[Service]
Type=simple
User={user}
WorkingDirectory={root}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TMPDIR={root}/tmp
Environment=XDG_CACHE_HOME={root}/cache
ExecStart={command} serve-gpu-plan --config {path}
Restart=always
RestartPreventExitStatus=78
RestartSec=15s
KillMode=process
TimeoutStopSec=20s
UMask=0077
StandardOutput=append:{root}/service.log
StandardError=inherit

[Install]
WantedBy=multi-user.target
"""}


def install_plan(path):
    if os.geteuid() != 0:
        raise ValueError("persistent GPU unit installation must run as root")
    path = Path(path).resolve()
    plan, config, _ = load_plan(path)
    previous_session(config)
    root = Path(plan["root"])
    if (root / "installed.json").exists():
        raise ValueError("service plan already installed; query its status")
    units = unit_files(plan, config, path)
    targets = [Path("/etc/systemd/system") / name for name in units]
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError("systemd unit already exists; refusing to overwrite it")
    # Only systemd control metadata lives in /etc; task output stays on data_mount.
    for target in targets:
        with target.open("x") as stream:
            stream.write(units[target.name])
        target.chmod(0o644)
    commands = [
        ["systemd-analyze", "verify", *map(str, targets)],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", plan["unit_name"] + ".service"],
        ["systemctl", "start", plan["unit_name"] + ".service"],
    ]
    completed = []
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        completed.append({"argv": command, "returncode": result.returncode,
                          "stdout": result.stdout, "stderr": result.stderr})
        atomic_json(root / "install-steps.json", completed)
        result.check_returncode()
    receipt = {"at_epoch": time.time(), "plan_sha256": digest(path),
               "persistent": True, "deadline_epoch": None, "unit_name": plan["unit_name"],
               "units": {name: hashlib.sha256(data.encode()).hexdigest() for name, data in units.items()},
               "jobs_replayed": False, "runtime_services_changed": False}
    atomic_json(root / "installed.json", receipt)
    return receipt
