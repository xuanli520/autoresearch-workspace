#!/usr/bin/env python3
"""OpenSSH askpass helper.

OpenSSH execs this helper after ``closefrom(STDERR_FILENO + 1)`` (see its
readpass.c), so an inherited descriptor cannot carry the secret. The monitor
instead creates a mode-0600 FIFO and passes its path in the environment; this
helper re-opens that path by name. No regular file is written.
"""
from __future__ import annotations
import os
from pathlib import Path
import stat
import sys

FIFO_PATH_ENV = 'AUTORESEARCH_SSH_PASSWORD_FIFO'
FIFO_IDENTITY_ENV = 'AUTORESEARCH_SSH_PASSWORD_FIFO_IDENTITY'
MAX_PASSWORD_BYTES = 4095


def fifo_identity(info: os.stat_result) -> str:
    return f'{info.st_dev}:{info.st_ino}:{info.st_uid}:{stat.S_IMODE(info.st_mode)}'


def inspect_fifo(path: str, expected: str | None = None) -> str:
    parent = Path(path).parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError('password FIFO directory must be private and owned by the current user')
    info = os.lstat(path)
    if (not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('password FIFO must have mode 0600 and the current owner')
    identity = fifo_identity(info)
    if expected is not None and identity != expected:
        raise ValueError('password FIFO identity changed')
    return identity


def open_password_fifo(path: str, expected: str, flags: int) -> int:
    inspect_fifo(path, expected)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISFIFO(info.st_mode) or fifo_identity(info) != expected:
            raise ValueError('opened password FIFO identity changed')
        inspect_fifo(path, expected)
    except BaseException:
        os.close(fd)
        raise
    return fd


def validate_helper(path: Path) -> None:
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or stat.S_IMODE(info.st_mode) & 0o022 or not os.access(path, os.X_OK)):
        raise ValueError('SSH askpass helper must be a trusted, executable regular file')
    for parent in path.parents:
        info = parent.lstat()
        writable = stat.S_IMODE(info.st_mode) & 0o022
        trusted_sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or (writable and not trusted_sticky)):
            raise ValueError('SSH askpass helper directory is not trusted')


def main() -> int:
    path = os.environ.get(FIFO_PATH_ENV)
    expected = os.environ.get(FIFO_IDENTITY_ENV)
    if not path or not expected:
        return 1
    try:
        fd = open_password_fifo(path, expected, os.O_RDONLY)
    except (OSError, ValueError):
        return 1
    try:
        secret = os.read(fd, MAX_PASSWORD_BYTES + 1)
    except OSError:
        return 1
    finally:
        os.close(fd)
    if not secret.endswith(b'\n') or not secret[:-1] or any(c in secret[:-1] for c in (b'\n', b'\r', b'\x00')):
        return 1
    sys.stdout.buffer.write(secret)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
