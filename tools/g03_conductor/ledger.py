"""Append-only, hash-chained JSONL ledger. Records are never rewritten."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .hashing import sha256_bytes, canonical

GENESIS = "0" * 64
RESERVED = {"seq", "ts", "at", "type", "prev", "hash"}


class LedgerCorrupt(RuntimeError):
    pass


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _record_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return sha256_bytes(canonical(body))


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise LedgerCorrupt(f"{self.path}: truncated final line; preserve the file and reconcile manually")
        records: list[dict] = []
        prev = GENESIS
        for no, line in enumerate(raw.splitlines(), 1):
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise LedgerCorrupt(f"{self.path}:{no}: invalid JSON") from exc
            if rec.get("seq") != no or rec.get("prev") != prev or rec.get("hash") != _record_hash(rec):
                raise LedgerCorrupt(f"{self.path}:{no}: hash chain broken (seq/prev/hash mismatch)")
            prev = rec["hash"]
            records.append(rec)
        return records

    def append(self, type_: str, **fields) -> dict:
        clash = RESERVED & set(fields)
        if clash:
            raise ValueError(f"reserved ledger field names: {sorted(clash)}")
        records = self.read()
        prev = records[-1]["hash"] if records else GENESIS
        ts = now()
        rec = {"seq": len(records) + 1, "ts": ts, "at": iso(ts), "type": type_, "prev": prev}
        rec.update(fields)
        rec["hash"] = _record_hash(rec)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return rec


@contextmanager
def exclusive(lock_path: str | Path):
    """Serialize mutating conductor commands across processes."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
