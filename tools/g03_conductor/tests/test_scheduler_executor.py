"""gpu_scheduler executor path with an in-memory fake Client (no real service, no GPU).

Checks the durable contract from tools/gpu_scheduler/USAGE.md: blocking submit with on_update,
WAITING_GPU while QUEUED, infra outcomes -> BLOCKED_INFRA (no k, no new request_id), deadline passed through.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from conftest import EXAMPLES, dry_run

from tools.g03_conductor import Conductor, init
from tools.g03_conductor.engine import G03Error

GPU_TEMPLATE = ("gpu_spec_template: {owner: fake/g03, job_class: fake-v1, memory_mib: 1000, compute_units: 10, "
                "cpu_cores: 1, ram_mib: 1024, memory_max_mib: 2048, memory_high_mib: 1800}\n")


class FakeClient:
    """Executes the runner synchronously; scripted final state per call."""

    def __init__(self, script):
        self.script = script
        self.specs = []

    def submit(self, spec, *, timeout=None, on_update=None):
        self.specs.append(spec)
        final = self.script.pop(0) if self.script else "SUCCEEDED"
        base = {"id": f"job-{len(self.specs)}", "request_id": spec["request_id"], "session_id": "s1",
                "origin_session_id": "s1", "deadline_epoch": None, "reconciling": False}
        on_update({**base, "state": "QUEUED", "revision": 1})
        if final != "SUCCEEDED":
            return {**base, "state": final, "reason": f"fake {final}"}
        on_update({**base, "state": "RUNNING", "revision": 2})
        proc = subprocess.run(spec["command"], cwd=spec["cwd"], capture_output=True)
        return {**base, "state": "SUCCEEDED" if proc.returncode == 0 else "FAILED", "exit_code": proc.returncode}


@pytest.fixture
def gpu_task(tmp_path):
    root, proto = dry_run.setup(tmp_path)
    sched = tmp_path / "sched"
    sched.mkdir()
    deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    init(root, task_id="fake_g03", gpu_hours=1, deadline=deadline, executor="gpu_scheduler",
         scheduler_root=str(sched), python=sys.executable,
         reviewer_cmd=[sys.executable, "-I", str(EXAMPLES / "fake_task" / "fake_reviewer.py"), "{task_file}"],
         reviewer_agent=str(EXAMPLES / "reviewer.dry.md"))
    return root, proto + GPU_TEMPLATE, deadline


def _to_s1(root, proto):
    c = Conductor(root)
    c.propose("protocol", content=proto)
    c.next()
    assert Conductor(root).st.stage == "S1"


def test_submit_spec_and_waiting_gpu(gpu_task):
    root, proto, deadline = gpu_task
    _to_s1(root, proto)
    fake = FakeClient([])
    c = Conductor(root, client_factory=lambda cfg: fake)
    out = c.run("diag", "base", [11])
    assert out["results"][0]["status"] == "VALID"
    spec = fake.specs[0]
    assert spec["request_id"] == out["results"][0]["request_id"]
    assert spec["deadline_at"] == deadline                      # original parent deadline, not now+budget
    assert "queue_timeout_seconds" not in spec
    assert spec["owner"] == "fake/g03" and spec["job_class"] == "fake-v1"
    assert spec["command"][1:3] == ["-I", "-B"]
    waits = [e["state"] for e in Conductor(root).st.events if e["type"] == "gpu_wait"]
    assert waits == ["QUEUED", "RUNNING"]


@pytest.mark.parametrize("state", ["INFEASIBLE", "EXPIRED", "UNKNOWN"])
def test_infra_outcome_blocks_without_new_request(gpu_task, state):
    root, proto, _ = gpu_task
    _to_s1(root, proto)
    fake = FakeClient([state])
    c = Conductor(root, client_factory=lambda cfg: fake)
    with pytest.raises(G03Error) as e:
        c.run("diag", "base", [11])
    assert e.value.code == "BLOCKED_INFRA"
    st = Conductor(root).st
    assert st.side() == "BLOCKED_INFRA" and st.attempts_a == 0
    assert Conductor(root).next()["action"] == "blocked"
    # resume after the expert reconciled: the same request_id is reused, never a new one
    from tools.g03_conductor import write_control
    write_control(root, "resume", "reconciled")
    c = Conductor(root, client_factory=lambda cfg: fake)
    c.apply_control()
    c.run("diag", "base", [11])
    assert [s["request_id"] for s in fake.specs] == [fake.specs[0]["request_id"]] * 2
    assert json.loads((c.dir / "runs" / fake.specs[0]["request_id"] / "plan.json").read_text())["request_id"] == \
        fake.specs[0]["request_id"]


def test_valid_run_is_never_resubmitted(gpu_task):
    root, proto, _ = gpu_task
    _to_s1(root, proto)
    fake = FakeClient([])
    c = Conductor(root, client_factory=lambda cfg: fake)
    c.run("diag", "base", [11])
    c.run("diag", "base", [11])
    assert len(fake.specs) == 1
