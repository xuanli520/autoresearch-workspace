"""P3 dry run: CPU fake task, synthetic metric, fake reviewer, no GPU, no LLM.

A scripted policy plays the main session; every stage decision still comes from `next`.
Usage: python3 -I tools/g03_conductor/examples/dry_run.py --scenario done|stop [--workdir DIR]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
FAKE = HERE / "fake_task"
CLI = [sys.executable, "-I", "-B", str(REPO / "tools" / "g03_conductor")]


def sh(*args, stdin: str | None = None, check=True) -> dict:
    proc = subprocess.run(CLI + list(args), input=stdin, capture_output=True, text=True)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(f"non-JSON output from {args}: {proc.stdout!r} {proc.stderr!r}")
    if check and not out.get("ok", False):
        raise SystemExit(f"command failed {args}: {out}")
    return out


def setup(work: Path) -> tuple[Path, str]:
    private, starter = work / "private", work / "starter"
    (private / "scorer").mkdir(parents=True)
    starter.mkdir()
    for f in ("train.py", "score.py"):
        shutil.copy(FAKE / f, private / "scorer" / f)
    shutil.copytree(FAKE / "baseline", private / "baseline")
    shutil.copytree(FAKE / "data", private / "data")
    shutil.copytree(FAKE / "baseline", starter / "baseline")  # public starter = Baseline only
    import hashlib
    data_sha = hashlib.sha256((private / "data" / "eval.txt").read_bytes()).hexdigest()
    proto = f"""
task_id: fake_g03
metric: {{name: synthetic_score, direction: maximize}}
U: {{value: 0.6, basis: "合成任务的已知上界 0.6（事前设定）"}}
margin_sigma: 1
K: 2
seeds:
  dev: [11, 12, 13, 14]
  screen: [21, 22, 23, 24]
  formal_pool:
    - [31, 32, 33, 34, 35]
    - [41, 42, 43, 44, 45]
budget_per_run: {{max_runtime_seconds: 60, steps: 10}}
diag_budget_multipliers: [2]
data:
  eval: {{path: data/eval.txt, sha256: "{data_sha}"}}
paths: {{private: ., starter: ../starter, scorer: [scorer], baseline: baseline}}
commands:
  train: ["{{python}}", "scorer/train.py", "--method-dir", "{{method_dir}}", "--seed", "{{seed}}", "--kind", "{{kind}}",
          "--steps", "{{budget.steps}}", "--out", "{{out_dir}}"]
  checkpoint: model.json
  score: ["{{python}}", "scorer/score.py", "--checkpoint", "{{checkpoint}}", "--data", "{{data.eval}}", "--out", "{{score_out}}"]
result: {{metric_key: metric, rescore_tolerance: 0}}
aggregate_quality_gate:
  rule: "mean psnr_db >= 29"
  checks:
    - {{field: psnr_db, op: ">=", value: 29}}
validity_declarations: {{train_from_initialization: true}}
"""
    return private, proto


def drive(root: Path, proto: str, scenario: str, max_steps: int = 300) -> dict:
    method = "overfit" if scenario == "stop" else "good"
    log = []
    for _ in range(max_steps):
        res = sh("next", "--root", str(root))
        act, stage = res["action"], res["stage"]
        log.append(f"{stage}:{act}:{res.get('gate') or (res.get('auto_run') or {}).get('kind') or ''}")
        if act == "terminal":
            return {"terminal": res["terminal"], "trace": log}
        if act == "review":
            sh("review-run", "--root", str(root), "--gate", res["gate"], "--subject", res["subject"])
        elif act == "auto_run":
            ar = res["auto_run"]
            args = ["run", "--root", str(root), "--auto", "--kind", ar["kind"], "--method", ar["method"]]
            for s in ar["seeds"]:
                args += ["--seed", str(s)]
            sh(*args)
        elif act == "continue":
            policy(root, stage, proto, method)
        else:
            raise SystemExit(f"unexpected action {res}")
    raise SystemExit("dry run did not terminate")


def policy(root: Path, stage: str, proto: str, method: str) -> None:
    st = sh("status", "--root", str(root))
    if stage == "S0":
        sh("propose", "--root", str(root), "--kind", "protocol", "--file", "-", stdin=proto)
    elif stage == "S1":
        sh("run", "--root", str(root), "--kind", "diag")
        sh("run", "--root", str(root), "--kind", "diag", "--mult", "2")
        sh("note", "--root", str(root), "--tag", "diagnosis",
           "--text", "合成任务：加倍预算与标准预算指标一致，训练已收敛；Baseline 未被弱化。")
    elif stage in ("S1_5", "S4"):
        kind = "dev" if stage == "S1_5" else "screen"
        cand = _new_candidate(root, method, st)
        sh("run", "--root", str(root), "--kind", kind, "--method", cand)
    else:
        raise SystemExit(f"policy has no action for {stage}")


def _new_candidate(root: Path, method: str, st: dict) -> str:
    out = sh("propose", "--root", str(root), "--kind", "candidate", "--description", f"synthetic {method} method")
    cdir = Path(out["path"])
    shutil.copy(FAKE / "methods" / method / "method.json", cdir / "method.json")  # the "worker" implements it
    return out["version"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["done", "stop"], required=True)
    ap.add_argument("--workdir")
    ap.add_argument("--init-only", action="store_true", help="set up + init, write protocol.yaml, do not drive (TS harness)")
    a = ap.parse_args(argv)
    work = Path(a.workdir) if a.workdir else Path(tempfile.mkdtemp(prefix="g03-dry-"))
    work.mkdir(parents=True, exist_ok=True)
    root, proto = setup(work)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    reviewer = json.dumps([sys.executable, "-I", str(FAKE / "fake_reviewer.py"), "{task_file}"])
    sh("init", "--root", str(root), "--task-id", "fake_g03", "--gpu-hours", "1", "--deadline", deadline,
       "--executor", "local_cpu", "--reviewer-cmd", reviewer, "--reviewer-agent", str(HERE / "reviewer.dry.md"),
       "--authorization", "P3 dry run (CPU only)")
    if a.init_only:
        (work / "protocol.yaml").write_text(proto)
        print(json.dumps({"root": str(root), "protocol": str(work / "protocol.yaml")}))
        return 0
    result = drive(root, proto, a.scenario)
    result["workdir"] = str(work)
    result["verify"] = sh("verify", "--root", str(root), check=False)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
