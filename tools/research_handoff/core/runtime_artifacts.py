"""Check immutable local images before managed work enters the GPU queue."""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Iterable

from .completion import EvidenceError


def verify_local_images(docker_host: str, image_ids: Iterable[str], *,
                        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    images = list(dict.fromkeys(image_ids))
    if not images or any(not isinstance(i, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", i)
                         for i in images):
        raise ValueError("managed runtime requires immutable local image IDs")
    result = runner(["docker", "--host", docker_host, "image", "inspect", *images],
                    capture_output=True, text=True, timeout=20)
    if result.returncode:
        message = (result.stderr or result.stdout or "").strip()
        if any(marker in message.lower() for marker in ("no such image", "no such object")):
            raise EvidenceError("EVALUATION_FAILED", "RUNTIME_IMAGE_MISSING",
                                "Pinned runtime image is missing from the configured Docker daemon: " + message)
        raise RuntimeError("Docker image inspection failed: " + message)
    found = {item["Id"] for item in json.loads(result.stdout)}
    if not set(images).issubset(found):
        raise EvidenceError("EVALUATION_FAILED", "RUNTIME_IMAGE_MISSING",
                            "Docker inspection did not return every pinned image ID")
    return {"docker_host": docker_host, "image_ids": images, "verified": True}


def image_startup_failure(exception_info: dict[str, Any]) -> str | None:
    if not isinstance(exception_info, dict):
        return None
    message = str(exception_info.get("exception_message") or "")
    markers = ("pull access denied for sha256", "no such image: sha256", "no such object: sha256")
    return "RUNTIME_IMAGE_MISSING" if any(m in message.lower() for m in markers) else None
