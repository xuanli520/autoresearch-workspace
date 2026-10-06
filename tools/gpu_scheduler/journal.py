"""Fsync'd request snapshots with hash chaining and explicit torn-tail recovery."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


class Journal:
    def __init__(self, root: Path) -> None:
        self.path = root / "requests.jsonl"
        self.sequence = 0
        self.previous = "0" * 64
        self.latest: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            with self.path.open("rb") as stream:
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        # Preserve the damaged bytes before removing a torn final write.
                        archive = root / ("journal-torn-tail-" + str(time.time_ns()) + ".bin")
                        with archive.open("xb") as output:
                            output.write(line)
                            output.flush()
                            os.fsync(output.fileno())
                        with self.path.open("r+b") as output:
                            output.truncate(offset)
                            output.flush()
                            os.fsync(output.fileno())
                        break
                    try:
                        record = json.loads(line)
                        checksum = record.pop("sha256")
                        if (record["version"] != 1 or record["sequence"] != self.sequence + 1
                                or record["previous"] != self.previous
                                or hashlib.sha256(encoded(record)).hexdigest() != checksum):
                            raise ValueError("invalid journal chain")
                        job = record["job"]
                        key = job["spec"]["request_id"]
                        old = self.latest.get(key)
                        if old and (old["id"] != job["id"] or old["spec"] != job["spec"]
                                    or old.get("requested_spec") != job.get("requested_spec")):
                            raise ValueError("journal request identity changed")
                    except (ValueError, TypeError, KeyError) as error:
                        raise ValueError(f"request journal corrupt at byte {offset}; inspect original evidence") from error
                    self.latest[key] = job
                    self.sequence = record["sequence"]
                    self.previous = checksum

    def append(self, job: dict[str, Any], session_id: str) -> None:
        record = {"version": 1, "sequence": self.sequence + 1, "previous": self.previous,
                  "at_epoch": time.time(), "session_id": session_id, "job": job}
        checksum = hashlib.sha256(encoded(record)).hexdigest()
        data = encoded({**record, "sha256": checksum}) + b"\n"
        created = not self.path.exists()
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("journal write failed")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            descriptor = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.sequence += 1
        self.previous = checksum
        self.latest[job["spec"]["request_id"]] = job
