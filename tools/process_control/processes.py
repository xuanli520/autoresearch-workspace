"""Shared Linux process identity and scoped cleanup. Never signal by name."""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any


def boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def process_info(pid: int) -> tuple[str, int, int, int] | None:
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[0], int(fields[2]), int(fields[3]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def process_start_ticks(pid: int) -> int | None:
    info = process_info(pid)
    return info[3] if info else None


def pid_matches(pid: Any, ticks: Any, boot: str | None = None) -> bool:
    if type(pid) is not int or pid <= 1 or type(ticks) is not int:
        return False
    if boot is not None and boot != boot_id():
        return False
    info = process_info(pid)
    return bool(info and info[0] not in ('Z', 'X') and info[3] == ticks)


def signal_identity(pid: Any, ticks: Any, sig: int, boot: str | None = None) -> bool:
    if not pid_matches(pid, ticks, boot):
        return False
    try:
        # pidfd closes the final PID reuse race between verification and kill.
        fd = os.pidfd_open(pid)
        try:
            if not pid_matches(pid, ticks, boot):
                return False
            signal.pidfd_send_signal(fd, sig)
        finally:
            os.close(fd)
        return True
    except ProcessLookupError:
        return False


def scope_members(token: str) -> list[tuple[int, int]]:
    if not token or len(token) < 32:
        return []
    needle = b'AUTORESEARCH_PROCESS_TOKEN=' + token.encode()
    members = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            info = process_info(pid)
            if info and info[0] not in ('Z', 'X') and needle in (entry / 'environ').read_bytes().split(b'\0'):
                members.append((pid, info[3]))
        except (OSError, ProcessLookupError):
            continue
    return members


def terminate_scope(token: str, grace_seconds: float = 1.0) -> bool:
    """Include grandchildren/setsid children retaining the inherited run token.

    A token is an ownership marker, not a sandbox boundary. External schedulers
    and containers need their own task-owned cancellation adapter.
    """
    deadline = time.monotonic() + max(0, grace_seconds)
    sent = set()
    while True:
        members = scope_members(token)
        if not members:
            return True
        sig = signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL
        for identity in members:
            if sig == signal.SIGKILL or identity not in sent:
                signal_identity(*identity, sig)
                sent.add(identity)
        if time.monotonic() >= deadline + 2:
            return not scope_members(token)
        time.sleep(0.03)
