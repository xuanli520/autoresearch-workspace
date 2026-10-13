"""CLI: every command prints one JSON object. Invoke as `python3 -I tools/g03_conductor <cmd> ...`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import Conductor, G03Error, init, write_control
from .hashing import sha256_file
from .ledger import LedgerCorrupt
from .state import IllegalTransition, allowed


def _verify(c: Conductor) -> dict:
    problems = []
    for r in c.st.runs.values():
        rc = c.dir / r["run_dir"] / "receipt.json"
        if r.get("receipt_sha256") and (not rc.exists() or sha256_file(rc) != r["receipt_sha256"]):
            problems.append(f"receipt changed: {r['request_id']}")
        if r.get("checkpoint") and r.get("checkpoint_sha256") and Path(r["checkpoint"]).exists() \
                and sha256_file(r["checkpoint"]) != r["checkpoint_sha256"]:
            problems.append(f"checkpoint changed: {r['request_id']}")
    for rv in c.st.reviews:
        rp = c.dir / rv["report"]
        if not rp.exists() or sha256_file(rp) != rv["report_sha256"]:
            problems.append(f"review report changed: {rv['report']}")
    if c.st.lock:
        lf = c.dir / "lock.json"
        if not lf.exists() or json.loads(lf.read_text()).get("lock_id") != c.st.lock_id:
            problems.append("lock.json differs from ledger lock")
        problems += [f"locked file changed: {m}" for m in c.lock_mismatches(full=True)]
    return {"ok": not problems, "records": len(c.st.events), "problems": problems}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="g03_conductor")
    ap.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(name, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("--root", required=True, help="题目私有区根目录（g03/ 在其下）")
        p.add_argument("--json", action="store_true")
        return p

    p = cmd("init", help="创建 g03/ 并写入启动授权（GPU 预算与截止即事前授权）")
    p.add_argument("--task-id", required=True)
    p.add_argument("--gpu-hours", type=float, required=True)
    p.add_argument("--deadline", required=True, help="ISO8601 with timezone")
    p.add_argument("--executor", choices=["local_cpu", "gpu_scheduler"], required=True)
    p.add_argument("--scheduler-root")
    p.add_argument("--python", help="interpreter for job commands (gpu_scheduler: host path)")
    p.add_argument("--reviewer-agent")
    p.add_argument("--reviewer-cmd", help="JSON argv overriding the pi reviewer (dry runs/tests only)")
    p.add_argument("--pi-path")
    p.add_argument("--max-idle-nudges", type=int, default=6)
    p.add_argument("--authorization", default="", help="授权原文/来源")

    p = cmd("status")
    p.add_argument("--detail", action="store_true")
    cmd("next")
    p = cmd("propose")
    p.add_argument("--kind", required=True, choices=["protocol", "candidate", "infeasible", "defect"])
    p.add_argument("--file", help="protocol file; '-' reads stdin")
    p.add_argument("--description", default="")
    p.add_argument("--source")
    p.add_argument("--evidence", action="append", default=[])
    p = cmd("run")
    p.add_argument("--kind", required=True, choices=["diag", "dev", "screen", "formal_b", "formal_r"])
    p.add_argument("--method", default="base")
    p.add_argument("--seed", type=int, action="append")
    p.add_argument("--mult", type=float, default=1)
    p.add_argument("--auto", action="store_true", help="extension-driven auto run (S1_5/S3/S4/S5 only)")
    p = cmd("review-run")
    p.add_argument("--gate", required=True)
    p.add_argument("--subject", required=True)
    p = cmd("note")
    p.add_argument("--tag", required=True)
    p.add_argument("--text", required=True)
    p = cmd("control")
    p.add_argument("--action", required=True, choices=["pause", "resume", "stop"])
    p.add_argument("--reason")
    p = cmd("signoff", help="专家签字（仅 CLI；σ_B=0 DETERMINISTIC）")
    p.add_argument("--kind", required=True)
    p.add_argument("--expert", required=True)
    p.add_argument("--note", required=True)
    cmd("verify")
    p = cmd("allowed")
    p.add_argument("--tool", required=True)
    p.add_argument("--kind")
    cmd("guard")
    return ap


def dispatch(a) -> dict:
    if a.cmd == "init":
        rc = json.loads(a.reviewer_cmd) if a.reviewer_cmd else None
        return init(a.root, task_id=a.task_id, gpu_hours=a.gpu_hours, deadline=a.deadline, executor=a.executor,
                    scheduler_root=a.scheduler_root, python=a.python, reviewer_agent=a.reviewer_agent,
                    reviewer_cmd=rc, pi_path=a.pi_path, max_idle_nudges=a.max_idle_nudges,
                    authorization=a.authorization)
    if a.cmd == "control":
        return write_control(a.root, a.action, a.reason)
    c = Conductor(a.root)
    if a.cmd == "status":
        s = c.status()
        if a.detail:
            s["detail"] = c.detail_lines()
        return s
    if a.cmd == "next":
        return {"ok": True, **c.next()}
    if a.cmd == "propose":
        content = None
        if a.kind == "protocol":
            if not a.file:
                raise G03Error("--file required for protocol")
            content = sys.stdin.read() if a.file == "-" else Path(a.file).read_text()
        return c.propose(a.kind, content=content, description=a.description, source=a.source, evidence=a.evidence)
    if a.cmd == "run":
        return c.run(a.kind, a.method, a.seed, a.mult, internal=a.auto)
    if a.cmd == "review-run":
        return c.review_run(a.gate, a.subject)
    if a.cmd == "note":
        return c.note(a.tag, a.text)
    if a.cmd == "signoff":
        return c.signoff(a.kind, expert=a.expert, note=a.note)
    if a.cmd == "verify":
        return _verify(c)
    if a.cmd == "allowed":
        ok, why = allowed(c.st.stage, a.tool, a.kind)
        if c.st.paused:
            ok, why = False, f"paused: {c.st.paused['reason']}"
        return {"ok": True, "allowed": ok, "reason": why, "stage": c.st.stage}
    if a.cmd == "guard":
        return {"ok": True, "stage": c.st.stage, **c.protected_paths()}
    raise G03Error(f"unknown command {a.cmd}")


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        out = dispatch(a)
        code = 0
    except G03Error as exc:
        out, code = exc.as_json(), 2
    except (IllegalTransition, LedgerCorrupt) as exc:
        out, code = {"ok": False, "code": type(exc).__name__, "error": str(exc)}, 3
    except Exception as exc:  # never emit a bare traceback to the TS layer
        import traceback
        out, code = {"ok": False, "code": "INTERNAL", "error": f"{type(exc).__name__}: {exc}",
                     "traceback": traceback.format_exc()[-4000:]}, 4
    print(json.dumps(out, ensure_ascii=False, default=str))
    return code
