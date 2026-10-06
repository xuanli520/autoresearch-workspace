"""One-shot cleanup of Docker projects recorded by a trusted job adapter."""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import stat
import subprocess
import time
from pathlib import Path

from .common import atomic_json


def load_projects(root):
    projects = []
    for path in sorted(root.glob("*.json")):
        if path.is_symlink():
            raise ValueError("ownership receipt cannot be a symlink")
        row = json.loads(path.read_text())
        project = row["project"]
        if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", project):
            raise ValueError("invalid exact Docker project")
        if path.stem != project:
            raise ValueError("ownership receipt filename and project disagree")
        projects.append(project)
    if len(set(projects)) != len(projects):
        raise ValueError("duplicate ownership projects")
    return projects


def cleanup(root, docker, output):
    with (root / ".cleanup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        projects = load_projects(root)
        removed = []
        for project in projects:
            query = ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]
            ids = docker(*query).split()
            if any(not re.fullmatch(r"[a-f0-9]{12,64}", item) for item in ids):
                raise ValueError("Docker returned an invalid container ID")
            if ids:
                docker("rm", "-f", *ids)
                removed.extend(ids)
            if docker(*query).strip():
                raise RuntimeError("owned containers remain; cleanup unconfirmed")
        receipt = {"cleanup_ok": True, "projects": projects, "removed": removed, "at_epoch": time.time()}
        atomic_json(output, receipt)
        return receipt


def validate_storage(mount, socket, root, output_parent, docker, *, allow_existing_system_socket=False):
    """A pre-existing IPC socket may be off disk; task storage may not be."""
    for path in (root, output_parent):
        if not path.is_relative_to(mount) or path.stat().st_dev != mount.stat().st_dev:
            raise ValueError("cleanup storage must stay on the data device")
    if not stat.S_ISSOCK(socket.stat().st_mode):
        raise ValueError("cleanup endpoint must be an actual Unix socket")
    if socket.is_relative_to(mount) and socket.stat().st_dev == mount.stat().st_dev:
        return
    if not allow_existing_system_socket:
        raise ValueError("off-disk IPC socket requires explicit verified existing-runtime opt-in")
    # Reading an existing socket creates no task files on its device. Confirm
    # the actual daemon stores every container layer under the data mount.
    actual = Path(json.loads(docker("info", "--format", "{{json .DockerRootDir}}"))).resolve(strict=True)
    if not actual.is_relative_to(mount) or actual.stat().st_dev != mount.stat().st_dev:
        raise ValueError("existing Docker daemon storage is not on the data device")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ownership-root", required=True, type=Path)
    p.add_argument("--docker-host", required=True)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--data-mount", required=True, type=Path)
    p.add_argument("--allow-existing-system-socket", action="store_true",
                   help="Use a pre-existing IPC socket after checking its daemon's data-root; no daemon mutation")
    a = p.parse_args()
    mount = a.data_mount.resolve(strict=True)
    if not mount.is_mount() or mount.stat().st_dev == Path("/").stat().st_dev:
        raise ValueError("cleanup requires the mounted data device")
    if not a.docker_host.startswith("unix://"):
        raise ValueError("cleanup requires an explicit local Docker socket")
    socket = Path(a.docker_host[7:]).resolve(strict=True)
    root = a.ownership_root.resolve(strict=True)
    def docker(*argv):
        result = subprocess.run(["docker", "--host", a.docker_host, *argv], capture_output=True,
                                text=True, timeout=20, check=True)
        return result.stdout

    validate_storage(mount, socket, root, a.output.parent.resolve(strict=True), docker,
                     allow_existing_system_socket=a.allow_existing_system_socket)
    print(json.dumps(cleanup(root, docker, a.output)), flush=True)


if __name__ == "__main__":
    main()
