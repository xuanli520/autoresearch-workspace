#!/usr/bin/env python3
"""Build a self-contained generic AutoResearch long-run controller bundle.

The bundle has no task adapters and no model/provider credentials. It is safe
to copy to a cloud task root, then pair with a task-owned JSON configuration.
Existing files are never overwritten.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import stat
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST = "CONTROLLER_MANIFEST.json"
EXECUTABLES = {"controller.py", "bundle.py", "research_completion.py", "templates/launch_controller.sh"}
FILES = (
    "controller.py",
    "bundle.py",
    "research_completion.py",
    "COMPLETION.md",
    "experiment_batch.py",
    "README.md",
    "core/__init__.py",
    "core/processes.py",
    "core/cleanup.py",
    "core/credit.py",
    "core/completion.py",
    "core/docker_network.py",
    "core/worker.py",
    "core/rpc.py",
    "core/longrun.py",
    "core/research_time.py",
    "core/remote.py",
    "providers/__init__.py",
    "providers/harbor_docker.py",
    "providers/codex_transport.py",
    "templates/agent_protocol.py",
    "templates/launch_controller.sh",
    "templates/public_docker_network.service.in",
    "templates/public_docker_network.timer.in",
    "controller.example.json",
    "connection.example.json",
    "templates/demo_agent.py",
    "templates/demo.config.json",
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def render() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for relative in FILES:
        source = ROOT / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"missing or symlinked bundle source: {relative}")
        files[relative] = source.read_bytes()
    for relative, payload in files.items():
        if relative.endswith(".py"):
            compile(payload, relative, "exec")
    return files


def make_manifest(files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "controller": "autoresearch-longrun",
        "generated_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "files": {relative: _sha256(payload) for relative, payload in files.items()},
        "executables": sorted(EXECUTABLES),
        "launch_performed": False,
        "credentials_included": False,
    }


def build(output: Path) -> dict[str, Any]:
    if Path(output).is_symlink():
        raise ValueError('bundle output must not be a symlink')
    output = Path(output).resolve()
    files = render()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be empty or absent")
    collisions = [relative for relative in [*files, MANIFEST] if (output / relative).exists()]
    if collisions:
        raise ValueError("managed files already exist; choose a new staging directory: " + ", ".join(collisions))
    output.mkdir(parents=True, exist_ok=True)
    for relative, payload in files.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        if relative in EXECUTABLES:
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    manifest = make_manifest(files)
    (output / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(output: Path) -> dict[str, Any]:
    output = Path(output).resolve()
    if (output / MANIFEST).is_symlink():
        raise ValueError('manifest must not be a symlink')
    manifest = json.loads((output / MANIFEST).read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("controller") != "autoresearch-longrun":
        raise ValueError("invalid controller manifest")
    files = manifest.get("files")
    if not isinstance(files, dict):
        return {"valid": False, "mismatched_or_missing": ["manifest.files"],
                "files_checked": 0, "launch_performed": False,
                "credentials_included": False}
    if set(files) != set(FILES):
        errors.append("manifest file whitelist differs")
    if manifest.get("executables") != sorted(EXECUTABLES):
        errors.append("manifest executable whitelist differs")
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            errors.append(f"invalid manifest entry: {relative!r}")
            continue
        target = output / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts or
                not target.resolve().is_relative_to(output) or
                not target.is_file() or target.is_symlink()):
            errors.append(relative)
            continue
        if _sha256(target.read_bytes()) != expected:
            errors.append(relative)
    for relative in EXECUTABLES:
        target = output / relative
        if not target.is_file() or not target.stat().st_mode & stat.S_IXUSR:
            errors.append(f"{relative} (not executable)")

    allowed = set(files) | {MANIFEST}
    allowed_dirs = {str(parent) for name in FILES for parent in Path(name).parents if str(parent) != '.'}
    for target in output.rglob("*"):
        if (target.is_symlink() or
                not target.is_dir() and str(target.relative_to(output)) not in allowed or
                target.is_dir() and str(target.relative_to(output)) not in allowed_dirs):
            relative = str(target.relative_to(output))
            if relative not in errors:
                errors.append(f"unexpected: {relative}")
    return {"valid": not errors, "mismatched_or_missing": errors,
            "files_checked": len(files),
            "launch_performed": bool(manifest.get("launch_performed")),
            "credentials_included": bool(manifest.get("credentials_included"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--check", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output:
            result = build(args.output)
            print(json.dumps({"output": str(Path(args.output).resolve()), "files": len(result["files"]),
                              "launch_performed": False, "credentials_included": False}, indent=2))
            return 0
        result = verify(args.check)
        print(json.dumps(result, indent=2))
        return 0 if result["valid"] else 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
