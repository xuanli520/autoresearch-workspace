"""Audit native session intervals; an interrupted session needs real feedback."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def texts(value):
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


def merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def subtract(interval, excluded):
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


def audit_session(path, lower=None, upper=None, *, allow_partial=False,
                  feedback_reader=lambda value: (), idle_limit=300):
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
    pending, excluded, feedback, tools, tool_results = {}, [], [], [], []
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
                last_usage = payload.get("info", {}).get("last_token_usage")
        if last is not None and at - last > idle_limit and not pending:
            excluded.append([last, at])
        last = at
        if event["type"] != "response_item":
            continue
        call_id = payload.get("call_id")
        if kind in ("function_call", "custom_tool_call"):
            pending[call_id] = at
            tools.append({"at_epoch": at, "call_id": call_id, "tool": payload.get("name")})
        if kind in ("function_call_output", "custom_tool_call_output"):
            began = pending.pop(call_id, None)
            if began is None:
                continue
            tool_results.append({"start_epoch": began, "end_epoch": at, "call_id": call_id})
            output = payload.get("output", "")
            rows = list(feedback_reader(output))
            for row in rows:
                feedback.append({"at_epoch": at, "call_id": call_id, "row": row})
            content = texts(output)
            if not rows and any(term in content for term in (
                    "No available channel", "Temporary failure in name resolution", "Connection timed out",
                    "Failed to connect", "ModuleNotFoundError", "job_state\": \"EXPIRED")):
                excluded.append([began, at])
    completed = end is not None
    eligible = start is not None and bool(feedback) and bool(tool_results) and (completed or allow_partial)
    boundary = end if completed else last
    if eligible and boundary is not None:
        # An unfinished call is not evidence of useful work after its launch.
        if pending:
            boundary = min(boundary, min(pending.values()))
        included = subtract((start, boundary), excluded)
    else:
        included = []
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "conversation_id": conversation, "start_epoch": start, "end_epoch": end,
            "observed_end_epoch": last, "completed": completed,
            "partial": bool(included) and not completed, "partial_last_line": partial_line,
            "has_real_feedback": bool(feedback), "credited_seconds": sum(b-a for a,b in included),
            "intervals": included, "excluded_intervals": merge(excluded), "feedback": feedback,
            "tool_calls": len(tools), "tool_results": len(tool_results), "last_token_usage": last_usage,
            "reason": "verified tool results and research feedback; unfinished calls and idle gaps excluded"
                      if eligible else "no eligible session with tool results and research feedback"}
