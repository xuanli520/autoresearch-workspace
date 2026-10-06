"""Build and verify a private immutable tool release; never install or start it."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shlex
from pathlib import Path

from .common import absolute, atomic_json, validate_config
from tools.research_handoff import bundle


TOOL_FILES = tuple("tools/gpu_scheduler/" + name for name in (
    "__init__.py", "batch.py", "cli.py", "client.py", "common.py", "container_ownership.py",
    "docker_cleanup.py", "fairness.py", "journal.py", "lifecycle.py", "managed_service.py",
    "release.py", "remote.py", "resource_limits.py", "resource_profiles.py", "resources.py",
    "runtime.py", "scheduler.py", "server.py", "worker.py",
)) + ("tools/process_control/__init__.py", "tools/process_control/processes.py",
      "tools/research_completion/__init__.py", "tools/research_completion/__main__.py")
RELEASE_FILES = set(TOOL_FILES) | {"tools/research_handoff/" + name for name in bundle.FILES} | {
    "tools/research_handoff/CONTROLLER_MANIFEST.json", "config.json", "plan.json",
    "scheduler-README.md", "deployment-README.md",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(output, config_path, remote_directory, authorization):
    if Path(output).is_symlink():
        raise ValueError("release output must not be a symlink")
    output = Path(output).resolve()
    remote = Path(remote_directory)
    if not remote.is_absolute() or not authorization.strip():
        raise ValueError("absolute remote release directory and authorization are required")
    config = json.loads(Path(config_path).read_text())
    validated = validate_config(config, local_test=True)
    mount = Path(absolute(config["data_mount"], "data_mount"))
    if (not validated["persistent"] or config.get("execution_backend", "systemd") != "systemd"
            or config.get("systemd_user", False)):
        raise ValueError("release requires persistent production systemd configuration")
    if not remote.is_relative_to(mount):
        raise ValueError("remote release must be inside data_mount")
    if output.exists():
        raise FileExistsError("release already exists; verify it instead of overwriting")
    output.mkdir(parents=True, mode=0o700)
    source = Path(__file__).resolve().parents[2]
    targets = []
    for relative in TOOL_FILES:
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing or symlinked release source: " + relative)
        payload = path.read_bytes()
        compile(payload, relative, "exec")
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        targets.append(target)
    bundle.build(output / "tools/research_handoff")
    # This combined release shares process_control; standalone controller
    # bundles continue to embed the canonical implementation when rebuilt.
    controller_root = output / "tools/research_handoff"
    shim = controller_root / "core/processes.py"
    shim.write_bytes((source / "tools/research_handoff/core/processes.py").read_bytes())
    controller_manifest = json.loads((controller_root / bundle.MANIFEST).read_text())
    controller_manifest["files"]["core/processes.py"] = sha(shim)
    atomic_json(controller_root / bundle.MANIFEST, controller_manifest)
    atomic_json(output / "config.json", config)
    pinned = [*targets, output / "config.json", output / "tools/research_handoff/core/processes.py"]
    plan = {"version": 1, "root": config["root"], "gpu_config": str(remote / "config.json"),
            "python": "/usr/bin/python3", "unit_name": "autoresearch-gpu-scheduler",
            "authorization": authorization,
            "source_sha256": {str(remote / path.relative_to(output)): sha(path) for path in pinned}}
    atomic_json(output / "plan.json", plan)
    for path in (source / "tools/gpu_scheduler/README.md", source / "ops/gpu_scheduler/README.md"):
        (output / ("scheduler-README.md" if path.parent.name == "gpu_scheduler" and path.parent.parent.name == "tools"
                   else "deployment-README.md")).write_bytes(path.read_bytes())
    files = {str(path.relative_to(output)): {"sha256": sha(path), "mode": path.stat().st_mode & 0o777}
             for path in sorted(output.rglob("*")) if path.is_file()}
    atomic_json(output / "RELEASE_MANIFEST.json", {"version": 1, "remote_directory": str(remote),
        "files": files, "installation_performed": False,
        "excludes": ["credentials", "Reference", "Hidden", "task data", "scientific evidence"]})
    return verify(output)


def verify(directory):
    if Path(directory).is_symlink():
        raise ValueError("release directory must not be a symlink")
    root = Path(directory).resolve()
    manifest = json.loads((root / "RELEASE_MANIFEST.json").read_text())
    if manifest["version"] != 1:
        raise ValueError("unsupported release manifest")
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    expected = set(manifest["files"]) | {"RELEASE_MANIFEST.json"}
    directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != "."}
    if (set(manifest["files"]) != RELEASE_FILES or actual != expected or
            any(path.is_symlink() or path.is_dir() and str(path.relative_to(root)) not in directories
                for path in root.rglob("*"))):
        raise ValueError("release member list differs or contains a symlink")
    for relative, item in manifest["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root) or sha(path) != item["sha256"]:
            raise ValueError(f"release input changed: {relative}")
        if path.stat().st_mode & 0o777 != item["mode"]:
            raise ValueError(f"release permission changed: {relative}")
    if not bundle.verify(root / "tools/research_handoff")["valid"]:
        raise ValueError("controller bundle failed verification")
    return {"valid": True, "files": len(actual), "remote_directory": manifest["remote_directory"],
            "manifest_sha256": sha(root / "RELEASE_MANIFEST.json"), "installation_performed": False}


PUBLISH_CODE = r'''
import base64,hashlib,json,os,sys
from pathlib import Path
request=json.loads(sys.stdin.read())
destination=Path(request['remote_directory'])
config=request['config']
mount=Path(config['data_mount']).resolve(strict=True)
def require(condition,message):
    if not condition:
        raise ValueError(message)
require(mount.is_mount() and mount.stat().st_dev != Path('/').stat().st_dev,'invalid data mount')
require(destination.is_absolute() and not destination.is_symlink() and
        destination.resolve().is_relative_to(mount),'invalid release destination')
require(not destination.exists() or not any(p.is_symlink() for p in destination.rglob('*')),
        'release contains a symlink')
destination.mkdir(parents=True,exist_ok=True,mode=0o700)
require(destination.stat().st_dev == mount.stat().st_dev,'release device differs')
for item in request['files']:
    relative=Path(item['path'])
    require(not relative.is_absolute() and '..' not in relative.parts,'invalid release member')
    path=destination/relative
    require(not path.is_symlink() and path.resolve().is_relative_to(destination.resolve()),'invalid member path')
    payload=base64.b64decode(item['base64'],validate=True)
    require(hashlib.sha256(payload).hexdigest() == item['sha256'],'member digest differs')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    require(path.parent.stat().st_dev == mount.stat().st_dev,'member device differs')
    if path.exists():
        require(path.is_file() and path.read_bytes() == payload,'immutable release differs')
        require(path.stat().st_mode & 0o777 == item['mode'],'immutable mode differs')
    else:
        with path.open('xb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        path.chmod(item['mode'])
os.chdir(destination)
sys.path.insert(0,str(destination))
from tools.gpu_scheduler.release import verify
from tools.gpu_scheduler.managed_service import load_plan
result=verify(destination)
load_plan(destination/'plan.json')
result.update(plan_valid=True,device=destination.stat().st_dev,system_device=Path('/').stat().st_dev,
              installed=False,service_changed=False)
print(json.dumps(result))
'''


def publish(directory, auth_file, receipt):
    """Transfer only pinned source files and validate them without starting services."""
    from tools.gpu_monitor import monitor
    if Path(receipt).exists() or Path(receipt).is_symlink():
        raise FileExistsError("publish receipt already exists; inspect the original")
    verification = verify(directory)
    root = Path(directory).resolve()
    manifest = json.loads((root / "RELEASE_MANIFEST.json").read_text())
    members = dict(manifest["files"])
    members["RELEASE_MANIFEST.json"] = {"sha256": verification["manifest_sha256"],
                                      "mode": (root / "RELEASE_MANIFEST.json").stat().st_mode & 0o777}
    files = []
    for relative, item in sorted(members.items()):
        path = root / relative
        payload = path.read_bytes()
        if (path.is_symlink() or hashlib.sha256(payload).hexdigest() != item["sha256"] or
                path.stat().st_mode & 0o777 != item["mode"]):
            raise ValueError("release input changed before upload: " + relative)
        files.append({"path": relative, "sha256": item["sha256"],
                      "base64": base64.b64encode(payload).decode(), "mode": item["mode"]})
    verify(root)
    host = monitor.apply_auth({"connect_timeout_seconds": 10}, monitor.load_auth(auth_file))
    request = {"remote_directory": verification["remote_directory"], "files": files,
               "config": json.loads((root / "config.json").read_text())}
    result = monitor.run_ssh(host, monitor.ssh_command(host, shlex.join(["python3", "-B", "-c", PUBLISH_CODE]),
        password_auth=True), json.dumps(request), 60)
    if result.returncode:
        raise RuntimeError("release transfer/verification failed: " + result.stderr[-2000:])
    remote = json.loads(result.stdout)
    if remote["manifest_sha256"] != verification["manifest_sha256"]:
        raise ValueError("remote release manifest differs")
    Path(receipt).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(receipt, {"local": verification, "remote": remote, "installation_performed": False})
    return remote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output")
    mode.add_argument("--check")
    mode.add_argument("--publish")
    parser.add_argument("--config")
    parser.add_argument("--remote-directory")
    parser.add_argument("--authorization")
    parser.add_argument("--auth")
    parser.add_argument("--receipt")
    args = parser.parse_args()
    if args.publish:
        if not all((args.auth, args.receipt)):
            parser.error("publish requires --auth and --receipt")
        result = publish(args.publish, args.auth, args.receipt)
    elif args.check:
        result = verify(args.check)
    else:
        if not all((args.config, args.remote_directory, args.authorization)):
            parser.error("build requires --config, --remote-directory and --authorization")
        result = build(args.output, args.config, args.remote_directory, args.authorization)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
