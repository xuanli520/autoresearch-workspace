"""Audit native session intervals; an interrupted session needs real feedback."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("session timestamps must include timezone")
    return parsed.timestamp()


def token_usage(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Native token_count events can omit usage; retain the last real report."""
    info = payload.get("info")
    usage = info.get("last_token_usage") if isinstance(info, dict) else None
    return usage if isinstance(usage, dict) else previous


def texts(value: Any) -> str:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return texts(decoded) if isinstance(decoded, (dict, list)) else value
    if isinstance(value, list):
        return "\n".join(texts(item) for item in value)
    if isinstance(value, dict):
        keys = [key for key in ("text", "output", "content", "aggregated_output") if key in value]
        return "\n".join(texts(value[key]) for key in keys) if keys else json.dumps(value)
    return ""


def merge(intervals: Iterable[Iterable[float]]) -> list[list[float]]:
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def subtract(interval: tuple[float, float], excluded: Iterable[Iterable[float]]) -> list[list[float]]:
    pieces = [list(interval)]
    for left, right in merge(excluded):
        result = []
        for start, end in pieces:
            if right <= start or left >= end:
                result.append([start, end])
            else:
                if start < left:
                    result.append([start, left])
                if right < end:
                    result.append([right, end])
        pieces = result
    return pieces


def audit_session(path: str | Path, lower: float | None = None, upper: float | None = None,
                  *, allow_partial: bool = False,
                  feedback_reader: Callable[[Any], Iterable[Any]] = lambda value: (),
                  method_reader: Callable[[dict[str, Any], Any], bool] | None = None,
                  idle_limit: float = 300) -> dict[str, Any]:
    path = Path(path)
    raw = path.read_bytes()
    events = []
    partial_line = False
    for number, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            if number != len(raw.splitlines()) - 1 or raw.endswith(b"\n"):
                raise
            partial_line = True
    pending, calls, excluded, feedback, tools, tool_results, methods = {}, {}, [], [], [], [], []
    start, end, last, last_usage, conversation = None, None, None, None, None
    for event in events:
        at = timestamp(event["timestamp"])
        if lower is not None and at < lower or upper is not None and at > upper:
            continue
        payload = event.get("payload", {})
        kind = payload.get("type")
        if event["type"] == "session_meta":
            conversation = payload.get("id", payload.get("session_id"))
        if event["type"] == "event_msg":
            if kind in ("task_started", "turn_started"):
                start = at if start is None else min(start, at)
            if kind in ("task_complete", "turn_complete"):
                end = at
            if kind == "token_count":
                last_usage = token_usage(payload, last_usage)
        if last is not None and at - last > idle_limit and not pending:
            excluded.append([last, at])
        last = at
        if event["type"] != "response_item":
            continue
        call_id = payload.get("call_id")
        if kind in ("function_call", "custom_tool_call"):
            pending[call_id] = at
            calls[call_id] = payload
            tools.append({"at_epoch": at, "call_id": call_id, "tool": payload.get("name")})
        if kind in ("function_call_output", "custom_tool_call_output"):
            began = pending.pop(call_id, None)
            if began is None:
                continue
            tool_results.append({"start_epoch": began, "end_epoch": at, "call_id": call_id})
            output = payload.get("output", "")
            if method_reader is not None and method_reader(calls[call_id], output):
                methods.append({"start_epoch": began, "end_epoch": at, "call_id": call_id})
            rows = list(feedback_reader(output))
            for row in rows:
                feedback.append({"at_epoch": at, "call_id": call_id, "row": row})
            content = texts(output)
            if not rows and any(term in content for term in (
                    "No available channel", "Temporary failure in name resolution", "Connection timed out",
                    "Failed to connect", "ModuleNotFoundError", "job_state\": \"EXPIRED")):
                excluded.append([began, at])
    completed = end is not None
    eligible = start is not None and bool(feedback or methods) and bool(tool_results) and (completed or allow_partial)
    boundary = end if completed else last
    research_start = start
    if research_start is not None and boundary is not None and not feedback and methods:
        research_start = max(start, min(row['start_epoch'] for row in methods))
        boundary = min(boundary, max(row['end_epoch'] for row in methods))
    if eligible and boundary is not None:
        # An unfinished call is not evidence of useful work after its launch.
        if pending:
            boundary = min(boundary, min(pending.values()))
        included = subtract((research_start, boundary), excluded)
    else:
        included = []
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "conversation_id": conversation, "start_epoch": start, "end_epoch": end,
            "observed_end_epoch": last, "completed": completed,
            "partial": bool(included) and not completed, "partial_last_line": partial_line,
            "has_real_feedback": bool(feedback), "credited_seconds": sum(b-a for a,b in included),
            "has_method_evidence": bool(methods), "method_results": methods,
            "intervals": included, "excluded_intervals": merge(excluded), "feedback": feedback,
            "tool_calls": len(tools), "tool_results": len(tool_results), "last_token_usage": last_usage,
            "reason": "verified tool results and research evidence; unfinished calls and idle gaps excluded"
                      if eligible else "no eligible session with tool results and research evidence"}
