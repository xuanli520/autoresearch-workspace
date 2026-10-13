"""Deterministic file/tree hashing and canonical JSON."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

IGNORED_NAMES = {"__pycache__", ".pytest_cache", ".git"}
IGNORED_SUFFIXES = (".pyc", ".pyo")


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_obj(obj) -> str:
    return sha256_bytes(canonical(obj))


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_files(root: str | Path) -> list[tuple[str, Path]]:
    root = Path(root)
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_NAMES)
        for name in sorted(filenames):
            if name in IGNORED_NAMES or name.endswith(IGNORED_SUFFIXES):
                continue
            p = Path(dirpath) / name
            out.append((p.relative_to(root).as_posix(), p))
    return out


def tree_hash(path: str | Path) -> str:
    """sha256 of a file, or of the sorted (relpath, sha256) listing of a directory."""
    p = Path(path)
    if p.is_file():
        return sha256_file(p)
    if not p.is_dir():
        raise FileNotFoundError(str(p))
    listing = [[rel, "L:" + os.readlink(f)] if f.is_symlink() else [rel, sha256_file(f)] for rel, f in tree_files(p)]
    return sha256_obj({"tree": listing})
