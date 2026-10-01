"""Durable primitives for a generic long-running AutoResearch controller.

This module is the dependency-free control plane used by ``controller.py``. It keeps
the accounting rules in one place so a controller can be stopped, reopened or
replaced without guessing how much time or context has already been consumed.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from processes import boot_id, pid_matches


SCHEMA_VERSION = 2
CONTROLLER_NAME = "autoresearch-longrun"
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
TERMINAL_STATES = frozenset({"COMPLETED", "STOPPED", "FAILED", "EXPIRED"})


class ControllerError(ValueError):
    """A user-actionable configuration or state error."""


def utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def epoch(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return _datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ControllerError(f"invalid timestamp: {value!r}") from exc


def finite_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ControllerError(f"{name} must be a number")
    if not math.isfinite(float(value)) or float(value) < minimum:
        raise ControllerError(f"{name} must be finite and >= {minimum}")
    return float(value)


def positive_number(value: Any, name: str) -> float:
    result = finite_number(value, name)
    if result <= 0:
        raise ControllerError(f"{name} must be > 0")
    return result


def run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise ControllerError("run_id must contain only lowercase letters, digits, '.', '_' or '-' (max 128)")
    return value


def _relative_path(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ControllerError(f"{name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ControllerError(f"{name} must stay within the configured task root")
    return value


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ControllerError(f"{name} must be an integer >= {minimum}")
    return value


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Strict versioned configuration; unknown keys are mistakes, not defaults."""
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise ControllerError("config requires version=1")
    allowed = {"version", "task_id", "root", "workdir", "command", "env", "budget", "turn",
               "context", "heartbeat", "policy", "output", "storage", "cleanup"}
    unknown = set(config) - allowed
    if unknown:
        raise ControllerError(f"unknown config fields: {sorted(unknown)}; use --remote for SSH control")
    def section(name, defaults):
        value = config.get(name, {})
        if not isinstance(value, dict) or set(value) - set(defaults):
            raise ControllerError(f"invalid or unknown fields in {name}")
        return {**defaults, **value}
    def boolean(value, name):
        if type(value) is not bool:
            raise ControllerError(f"{name} must be boolean")
        return value
    task = config.get("task_id", "task")
    if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task):
        raise ControllerError("task_id must be a simple name")
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and v and "\0" not in v for v in command):
        raise ControllerError("command must be a non-empty argv list (use ['bash', '-lc', ...] explicitly for shell)")
    root = config.get("root", ".")
    workdir = config.get("workdir", ".")
    for name, value in (("root", root), ("workdir", workdir)):
        if not isinstance(value, str) or not value or "\0" in value:
            raise ControllerError(f"{name} must be a path")
    _relative_path(workdir, "workdir")
    env = config.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
            and not k.startswith("AUTORESEARCH_") and isinstance(v, str) and "\0" not in v for k, v in env.items()):
        raise ControllerError("env requires valid string keys/values; AUTORESEARCH_* is reserved")
    budget = section("budget", {"mode": "active", "window_seconds": 39600,
                                "hard_limit_seconds": 43200, "credit_policy": "running"})
    window = positive_number(budget["window_seconds"], "budget.window_seconds")
    hard = positive_number(budget["hard_limit_seconds"], "budget.hard_limit_seconds")
    if not window <= hard <= 43200:
        raise ControllerError("0 < window_seconds <= hard_limit_seconds <= 43200 is required")
    if budget["mode"] not in ("active", "wall") or budget["credit_policy"] not in ("running", "successful_turn"):
        raise ControllerError("invalid budget mode/credit_policy")
    budget.update(window_seconds=window, hard_limit_seconds=hard)
    turn = section("turn", {"seconds": min(5400, hard), "grace_seconds": 1})
    turn["seconds"] = positive_number(turn["seconds"], "turn.seconds")
    turn["grace_seconds"] = finite_number(turn["grace_seconds"], "turn.grace_seconds")
    if turn["seconds"] > min(5400, hard) or turn["grace_seconds"] > 30:
        raise ControllerError("turn.seconds must be <= 5400 and fit hard limit; grace_seconds must be <= 30")
    context = section("context", {"max_tokens": None, "compact_at_tokens": None,
        "reserve_tokens": None, "required": True, "auto_compact": False, "summary_seconds": 60,
        "max_summary_bytes": 65536})
    # Context capacity belongs to the selected provider/adapter, never a model default.
    for name in ("max_tokens", "compact_at_tokens", "reserve_tokens"):
        if context[name] is None:
            raise ControllerError(f"context.{name} must be explicitly configured for the chosen model")
    for name in ("max_tokens", "compact_at_tokens", "reserve_tokens", "max_summary_bytes"):
        integer(context[name], "context." + name, 1)
    if context["compact_at_tokens"] + context["reserve_tokens"] > context["max_tokens"]:
        raise ControllerError("compact_at_tokens + reserve_tokens must fit max_tokens")
    for name in ("required", "auto_compact"):
        boolean(context[name], "context." + name)
    context["summary_seconds"] = positive_number(context["summary_seconds"], "context.summary_seconds")
    heartbeat = section("heartbeat", {"required": True, "interval_seconds": 1, "stale_after_seconds": 300,
                                     "controller_stale_seconds": 30})
    boolean(heartbeat["required"], "heartbeat.required")
    for name in ("interval_seconds", "stale_after_seconds", "controller_stale_seconds"):
        heartbeat[name] = positive_number(heartbeat[name], "heartbeat." + name)
    if heartbeat["interval_seconds"] > 5 or heartbeat["stale_after_seconds"] < heartbeat["interval_seconds"] or heartbeat["controller_stale_seconds"] < heartbeat["interval_seconds"] * 3:
        raise ControllerError("poll interval <= 5; heartbeat stale >= interval; controller stale >= 3 * interval")
    policy = section("policy", {"max_turns": 10000, "max_turn_retries": 1,
                                 "retry_backoff_seconds": 5})
    integer(policy["max_turns"], "policy.max_turns", 1)
    integer(policy["max_turn_retries"], "policy.max_turn_retries", 0)
    if policy["max_turn_retries"] > 5:
        raise ControllerError("policy.max_turn_retries must be <= 5")
    policy["retry_backoff_seconds"] = finite_number(policy["retry_backoff_seconds"],
                                                     "policy.retry_backoff_seconds")
    if not 0 <= policy["retry_backoff_seconds"] <= 300:
        raise ControllerError("policy.retry_backoff_seconds must be between 0 and 300")
    output = section("output", {"max_run_bytes": 1024**3, "min_free_bytes": 128*1024**2})
    for name in output:
        integer(output[name], "output." + name, 1)
    storage = section("storage", {"data_mount": None})
    mount = storage["data_mount"]
    if mount is not None and (not isinstance(mount, str) or not Path(mount).is_absolute()):
        raise ControllerError("storage.data_mount must be an absolute verified data mount")
    cleanup = section('cleanup', {'command': None, 'timeout_seconds': 10})
    if cleanup['command'] is not None and (not isinstance(cleanup['command'], list) or not cleanup['command'] or
            not all(isinstance(part, str) and part and '\0' not in part for part in cleanup['command'])):
        raise ControllerError('cleanup.command must be an argv list')
    cleanup['timeout_seconds'] = positive_number(cleanup['timeout_seconds'], 'cleanup.timeout_seconds')
    if cleanup['timeout_seconds'] > 30:
        raise ControllerError('cleanup.timeout_seconds must be <= 30')
    return dict(version=1, task_id=task, root=root, workdir=workdir, command=list(command), env=dict(env),
                budget=budget, turn=turn, context=context, heartbeat=heartbeat, policy=policy, output=output,
                storage=storage, cleanup=cleanup)


def check_storage(mount: str | None, *paths: Path) -> dict[str, Any]:
    if mount is None:
        return {"verified": False, "reason": "local mode: data mount not configured"}
    base = Path(mount).resolve(strict=True)
    if not base.is_mount() or base.stat().st_dev == Path("/").stat().st_dev:
        raise ControllerError("data_mount must be a mounted data device distinct from the system disk")
    for path in paths:
        path = Path(path).resolve()
        if not path.is_relative_to(base):
            raise ControllerError(f"path outside data_mount: {path}")
        ancestor = path
        while not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.stat().st_dev != base.stat().st_dev:
            raise ControllerError(f"unexpected device at {path}")
    return {"verified": True, "mount": str(base), "device": base.stat().st_dev}


def atomic_write(path: Path, payload: str | bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload.encode() if isinstance(payload, str) else payload
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


@contextlib.contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(stream.fileno(), flags)
        yield stream
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def state_path(state_root: Path, run: str) -> Path:
    run = run_id(run)
    return Path(state_root).resolve() / "runs" / run


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise
    except json.JSONDecodeError as exc:
        raise ControllerError(f"invalid JSON at {path}: {exc}") from exc


def load_config(path: Path) -> dict[str, Any]:
    config = validate_config(read_json(Path(path)))
    config["root"] = str((Path(path).resolve().parent / Path(config["root"]).expanduser()).resolve())
    return config


def new_state(config: dict[str, Any], run: str, run_dir: Path) -> dict[str, Any]:
    config = validate_config(config)
    run = run_id(run)
    created = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "controller": CONTROLLER_NAME,
        "task_id": config["task_id"],
        "run_id": run,
        "status": "READY",
        "created_at": created,
        "updated_at": created,
        "controller_pid": None,
        "controller_start_ticks": None,
        "guard_pid": None,
        "guard_start_ticks": None,
        "stop_reason": None,
        "attempt": 0,
        "resume_required": False,
        "error": None,
        "budget": {
            "mode": config["budget"]["mode"],
            "window_seconds": config["budget"]["window_seconds"],
            "hard_limit_seconds": config["budget"]["hard_limit_seconds"],
            "credit_policy": config["budget"]["credit_policy"],
            "started_at": None,
            "hard_deadline_at": None,
            "active_seconds": 0.0,
            "runtime_seconds": 0.0,
            "boot_id": None,
            "started_monotonic": None,
            "active_started_at": None,
            "active_monotonic": None,
        },
        "context": {
            "generation": 0,
            "conversation_id": None,
            "max_tokens": config["context"]["max_tokens"],
            "compact_at_tokens": config["context"]["compact_at_tokens"],
            "reserve_tokens": config["context"]["reserve_tokens"],
            "used_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "state": "OPEN",
            "last_report_at": None,
            "last_snapshot": None,
            "last_reopen_at": None,
        },
        "turn": {
            "number": 0,
            "status": "IDLE",
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "pid_start_ticks": None,
            "dir": None,
            "returncode": None,
            "reason": None,
        },
        "retry": {"anchor_turn": None, "used": 0},
        "heartbeat": {
            "last_at": None,
            "last_source": None,
            "last_event_at": None,
            "stale": False,
        },
        "paths": {
            "run_dir": str(run_dir),
            "config": "config.json",
            "events": "events.jsonl",
            "context_dir": "context",
            "turns_dir": "turns",
        },
    }


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state = dict(state)
    state["updated_at"] = utc_now()
    atomic_json(Path(run_dir) / "state.json", state)


def load_state(run_dir: Path) -> dict[str, Any]:
    state = read_json(Path(run_dir) / "state.json")
    if state.get("schema_version") != SCHEMA_VERSION or state.get("controller") != CONTROLLER_NAME:
        raise ControllerError("unsupported or foreign run state")
    run_id(state.get("run_id", ""))
    return state


def append_event(run_dir: Path, event: str, **fields: Any) -> dict[str, Any]:
    record = {"at": utc_now(), "event": event, **fields}
    run_dir = Path(run_dir)
    path = run_dir / 'events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(Path(run_dir) / ".events.lock"):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return record


def active_elapsed(state: dict[str, Any], now_epoch: float | None = None) -> float:
    b = state["budget"]
    pending = 0.0
    if b.get("active_started_at"):
        if now_epoch is None and b.get("boot_id") == boot_id() and b.get("active_monotonic") is not None:
            pending = max(0, time.monotonic() - b["active_monotonic"])
        else:
            pending = max(0, (time.time() if now_epoch is None else now_epoch) - epoch(b["active_started_at"]))
    return float(b["active_seconds"]) + pending


def budget_view(state: dict[str, Any], now_epoch: float | None = None) -> dict[str, Any]:
    now = time.time() if now_epoch is None else now_epoch
    b = state["budget"]
    started = epoch(b.get("started_at"))
    wall = 0 if started is None else max(0, now - started)
    if now_epoch is None and b.get("boot_id") == boot_id() and b.get("started_monotonic") is not None:
        wall = max(wall, time.monotonic() - b["started_monotonic"])
    active = active_elapsed(state, now_epoch)
    progress = active if b["mode"] == "active" else wall
    remaining = max(0, b["hard_limit_seconds"] - wall)
    if b.get('started_at') and b.get('boot_id') != boot_id():
        remaining = 0  # A reboot invalidates monotonic continuity; fail closed.
    return {"mode": b["mode"], "active_seconds": active, "credited_seconds": b["active_seconds"],
            "runtime_seconds": b["runtime_seconds"], "wall_age_seconds": wall,
            "window_seconds": b["window_seconds"], "hard_limit_seconds": b["hard_limit_seconds"],
            "remaining_seconds": remaining, "target_remaining_seconds": max(0, b["window_seconds"] - progress),
            "target_reached": progress >= b["window_seconds"], "hard_reached": remaining <= 0,
            "hard_deadline_at": b.get("hard_deadline_at"), "hard_deadline_passed": remaining <= 0}


def context_view(state: dict[str, Any]) -> dict[str, Any]:
    context = state["context"]
    used = int(context.get("used_tokens", 0))
    maximum = int(context["max_tokens"])
    compact_at = int(context["compact_at_tokens"])
    return {
        "generation": context["generation"],
        "conversation_id": context.get("conversation_id"),
        "used_tokens": used,
        "max_tokens": maximum,
        "compact_at_tokens": compact_at,
        "reserve_tokens": context["reserve_tokens"],
        "remaining_tokens": max(0, maximum - used),
        "compaction_required": used >= compact_at or context.get("state") == "COMPACTION_REQUIRED",
        "state": context.get("state", "OPEN"),
        "last_snapshot": context.get("last_snapshot"),
    }


def begin_run(state: dict[str, Any], now: str | None = None) -> None:
    now = now or utc_now()
    b = state["budget"]
    if b.get("started_at") is None:
        b.update(started_at=now, started_monotonic=time.monotonic(), boot_id=boot_id())
        deadline = epoch(now) + b["hard_limit_seconds"]
        b["hard_deadline_at"] = _datetime.datetime.fromtimestamp(deadline, _datetime.timezone.utc).isoformat()
    state.update(status="RUNNING", stop_reason=None, error=None, attempt=state["attempt"] + 1)


def finish_active_interval(state: dict[str, Any], ended_at: str | None = None, *, credit: bool = True,
                           duration: float | None = None) -> float:
    b = state["budget"]
    if b.get("active_started_at") is None:
        return 0.0
    elapsed = max(0, active_elapsed(state, epoch(ended_at) if ended_at else None) - b["active_seconds"])
    if duration is not None:
        elapsed = min(elapsed, finite_number(duration, "duration"))
    b["runtime_seconds"] += elapsed
    if credit:
        b["active_seconds"] += elapsed
    b["active_started_at"] = b["active_monotonic"] = None
    return elapsed


def start_active_interval(state: dict[str, Any], started_at: str | None = None) -> None:
    b = state["budget"]
    if b.get("active_started_at") is not None:
        raise ControllerError("an active interval is already open")
    b.update(active_started_at=started_at or utc_now(), active_monotonic=time.monotonic())


def _rotate_context(run_dir: Path, summary: str, *, reason: str, conversation_id: str | None,
                    expected_generation: int, compact: bool, force: bool = False,
                    controller_owned: bool = False) -> dict[str, Any]:
    run_dir = Path(run_dir)
    ownership = contextlib.nullcontext() if controller_owned else file_lock(run_dir / ".controller.lock", blocking=False)
    with ownership, file_lock(run_dir / ".state.lock"):
        state = load_state(run_dir)
        cfg = validate_config(read_json(run_dir / "config.json"))
        ctx = state["context"]
        if ctx["generation"] != expected_generation:
            raise ControllerError("stale generation: reload context status before rotating")
        if not controller_owned and (state.get("controller_pid") or state["turn"].get("status") == "RUNNING"):
            raise ControllerError("stop/recover the controller before rotating context")
        if state["status"] in ("COMPLETED", "EXPIRED"):
            raise ControllerError("a completed or expired run cannot rotate context")
        if budget_view(state)['hard_reached']:
            raise ControllerError('wall deadline passed; a context boundary cannot extend it')
        if not compact and ctx["state"] == "COMPACTION_REQUIRED":
            raise ControllerError("compact the required context before reopening")
        if compact and not force and ctx["state"] != "COMPACTION_REQUIRED":
            raise ControllerError("compaction not requested; use --force for an intentional boundary")
        if not isinstance(summary, str) or not summary.strip():
            raise ControllerError("a non-empty handoff summary is required")
        if len(summary.encode()) > cfg["context"]["max_summary_bytes"]:
            raise ControllerError("handoff summary exceeds max_summary_bytes")
        if conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id.strip()):
            raise ControllerError("conversation_id must be non-empty")
        if conversation_id is not None and conversation_id == ctx.get("conversation_id"):
            raise ControllerError("a new generation must use a new conversation_id")
        generation = ctx["generation"] + 1
        # Skip an uncommitted snapshot left by a crash; never overwrite history.
        while (run_dir / "context" / f"snapshot-{generation:04d}.json").exists():
            generation += 1
        relative = f"context/snapshot-{generation:04d}.json"
        snapshot = {"schema_version": SCHEMA_VERSION, "created_at": utc_now(),
                    "source_generation": ctx["generation"], "generation": generation,
                    "conversation_id": ctx.get("conversation_id"), "source_tokens": ctx["used_tokens"],
                    "summary": summary, "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
                    "reason": reason, "budget": budget_view(state), "previous_snapshot": ctx.get("last_snapshot"),
                    "last_turn": state["turn"]["number"], "logs": "turns/"}
        atomic_json(run_dir / relative, snapshot)
        atomic_json(run_dir / "context/latest.json", snapshot)
        ctx.update(previous_conversation_id=ctx.get('conversation_id'),
                   generation=generation, conversation_id=conversation_id, used_tokens=0,
                   input_tokens=0, output_tokens=0, state="OPEN", last_snapshot=relative,
                   last_report_at=None, last_reopen_at=utc_now(), summary_deadline_monotonic=None)
        if state["status"] in ("WAITING_COMPACTION", "CONTEXT_COMPACTION_REQUIRED", "READY"):
            state.update(status="READY", stop_reason=None)
        save_state(run_dir, state)
        append_event(run_dir, "context.compacted" if compact else "context.reopened",
                     generation=generation, source_generation=expected_generation, snapshot=relative,
                     summary_sha256=snapshot["summary_sha256"])
        return snapshot


def compact_context(run_dir: Path, summary: str, *, expected_generation: int,
                    conversation_id: str | None = None, force: bool = False,
                    controller_owned: bool = False) -> dict[str, Any]:
    return _rotate_context(run_dir, summary, reason="compaction", conversation_id=conversation_id,
                           expected_generation=expected_generation, compact=True, force=force,
                           controller_owned=controller_owned)


def reopen_context(run_dir: Path, *, expected_generation: int, conversation_id: str | None = None,
                   reason: str = "operator_reopen", summary: str | None = None) -> dict[str, Any]:
    return _rotate_context(run_dir, summary, reason=reason, conversation_id=conversation_id,
                           expected_generation=expected_generation, compact=False)


def apply_context_usage(state: dict[str, Any], used_tokens: int, *, generation: int,
                        input_tokens: int | None = None, output_tokens: int | None = None,
                        conversation_id: str | None = None) -> dict[str, Any]:
    integer(used_tokens, "used_tokens")
    ctx = state["context"]
    if type(generation) is not int or generation != ctx["generation"]:
        raise ControllerError("context generation is stale or missing")
    if used_tokens < ctx["used_tokens"]:
        raise ControllerError("tokens decreased without explicit compaction/reopen")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ControllerError("context report requires conversation_id")
    if conversation_id == ctx.get('previous_conversation_id'):
        raise ControllerError("new generation reused the previous conversation_id")
    if ctx.get("conversation_id") not in (None, conversation_id):
        raise ControllerError("conversation_id changed without explicit reopen")
    ctx.update(used_tokens=used_tokens, conversation_id=conversation_id, last_report_at=utc_now())
    for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if value is not None:
            ctx[name] = integer(value, name)
    if used_tokens >= ctx["compact_at_tokens"]:
        ctx["state"] = "COMPACTION_REQUIRED"
    return {"generation": ctx["generation"], "used_tokens": used_tokens,
            "compaction_required": ctx["state"] == "COMPACTION_REQUIRED"}


def record_context_usage(run_dir: Path, used_tokens: int, *, generation: int,
                         input_tokens: int | None = None, output_tokens: int | None = None,
                         conversation_id: str | None = None, source: str = "operator") -> dict[str, Any]:
    # All external state writers share the same lifecycle lock as the controller.
    run_dir = Path(run_dir)
    with file_lock(run_dir / ".controller.lock", blocking=False), file_lock(run_dir / ".state.lock"):
        state = load_state(run_dir)
        if state.get("controller_pid") or state["status"] in TERMINAL_STATES:
            raise ControllerError("stop/recover the controller before reporting context")
        result = apply_context_usage(state, used_tokens, generation=generation, input_tokens=input_tokens,
                                     output_tokens=output_tokens, conversation_id=conversation_id)
        if result["compaction_required"]:
            state["status"] = "WAITING_COMPACTION"
        save_state(run_dir, state)
        append_event(run_dir, "context.usage", source=source, **result)
        return result


def safe_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Return status data suitable for a terminal or API response."""
    result = dict(state)
    result["budget"] = {**state["budget"], "view": budget_view(state)}
    result["context"] = {**state["context"], "view": context_view(state)}
    result["controller_alive"] = pid_matches(state.get("controller_pid"), state.get("controller_start_ticks"), state.get("process_boot_id"))
    result["guard_alive"] = pid_matches(state.get("guard_pid"), state.get("guard_start_ticks"), state.get("process_boot_id"))
    result["worker_alive"] = pid_matches(state["turn"].get("pid"), state["turn"].get("pid_start_ticks"), state.get("process_boot_id"))
    if state.get("controller_pid") and not result["controller_alive"]:
        result["observed_status"] = "CONTROLLER_LOST"
    elif result["budget"]["view"]["hard_reached"] and state["status"] not in TERMINAL_STATES:
        result["observed_status"] = "EXPIRED"
    else:
        result["observed_status"] = state["status"]
    return result
