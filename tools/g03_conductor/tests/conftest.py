"""Shared fixtures: a CPU fake task (synthetic metric) driven through the real CLI, no GPU, no LLM."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
EXAMPLES = REPO / "tools" / "g03_conductor" / "examples"
sys.path.insert(0, str(EXAMPLES))

import dry_run  # noqa: E402

CLI = [sys.executable, "-I", "-B", str(REPO / "tools" / "g03_conductor")]


def cli(*args, stdin=None, env=None):
    proc = subprocess.run(CLI + [str(a) for a in args], input=stdin, capture_output=True, text=True, env=env)
    return json.loads(proc.stdout)


@pytest.fixture
def task(tmp_path):
    """Initialized private root plus its schema-valid protocol text."""
    root, proto = dry_run.setup(tmp_path)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    reviewer = json.dumps([sys.executable, "-I", str(EXAMPLES / "fake_task" / "fake_reviewer.py"), "{task_file}"])
    out = cli("init", "--root", root, "--task-id", "fake_g03", "--gpu-hours", 1, "--deadline", deadline,
              "--executor", "local_cpu", "--reviewer-cmd", reviewer, "--reviewer-agent", EXAMPLES / "reviewer.dry.md")
    assert out["ok"], out
    return {"root": root, "proto": proto, "tmp": tmp_path}


def drive_until(root: Path, proto: str, stop_stage: str, method: str = "good", max_steps: int = 200) -> dict:
    """Drive with the dry-run policy until `next` reports stop_stage (or a terminal)."""
    for _ in range(max_steps):
        res = cli("next", "--root", root)
        if res["stage"] == stop_stage or res["action"] in ("terminal", "paused", "wait_expert", "blocked"):
            return res
        if res["action"] == "review":
            cli("review-run", "--root", root, "--gate", res["gate"], "--subject", res["subject"])
        elif res["action"] == "auto_run":
            ar = res["auto_run"]
            args = ["run", "--root", root, "--auto", "--kind", ar["kind"], "--method", ar["method"]]
            for s in ar["seeds"]:
                args += ["--seed", s]
            cli(*args)
        else:
            dry_run.policy(root, res["stage"], proto, method)
    raise AssertionError("did not reach stage")


def copy_method(dest: Path, method: str) -> None:
    shutil.copy(EXAMPLES / "fake_task" / "methods" / method / "method.json", dest / "method.json")
