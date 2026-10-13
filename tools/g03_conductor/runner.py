"""Stage runner executed as the job command: train -> score -> (formal) independent rescore.

Standalone on purpose (stdlib only, no package imports) so the same file runs locally or as a
gpu_scheduler job. Usage: python3 -I runner.py <plan.json>. Writes <run_dir>/receipt.json atomically.
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stage(name: str, argv: list[str], cwd: str, run_dir: Path, env: dict, deadline: float) -> dict:
    out = run_dir / f"{name}.stdout.log"
    err = run_dir / f"{name}.stderr.log"
    started = time.time()
    remaining = deadline - time.monotonic()
    rec = {"name": name, "argv": argv, "cwd": cwd, "stdout": out.name, "stderr": err.name, "started": started}
    if remaining <= 0:
        rec.update(exit_code=None, error="runtime budget exhausted before stage", elapsed=0.0)
        return rec
    with open(out, "wb") as o, open(err, "wb") as e:
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, stdout=o, stderr=e, timeout=remaining)
            rec["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired:
            rec.update(exit_code=None, error="timeout")
        except OSError as exc:
            rec.update(exit_code=None, error=f"spawn failed: {exc}")
    rec["elapsed"] = time.time() - started
    return rec


def _metric(path: Path, key: str):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, None, f"cannot read {path.name}: {exc}"
    value = data.get(key) if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, data, f"{path.name}: {key!r} is not a finite number"
    return float(value), data, None


def run(plan_path: str) -> int:
    plan = json.loads(Path(plan_path).read_text())
    run_dir = Path(plan["run_dir"])
    receipt_path = run_dir / "receipt.json"
    if receipt_path.exists():
        return 0
    out_dir = Path(plan["out_dir"])
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(f"refusing to reuse non-empty out_dir {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()}
    env.update(plan.get("env", {}))
    if plan.get("cpu_only"):
        env["CUDA_VISIBLE_DEVICES"] = ""
    deadline = time.monotonic() + float(plan["max_runtime_seconds"])
    started = time.time()
    stages = []
    status, reason = "VALID", None
    metric = rescore_metric = None
    quality = {}
    checkpoint = ckpt_sha = None

    train = _stage("train", plan["train_argv"], plan["cwd"], run_dir, env, deadline)
    stages.append(train)
    if train.get("exit_code") != 0:
        status, reason = "INVALID", f"train failed: exit={train.get('exit_code')} {train.get('error', '')}".strip()
    if status == "VALID":
        matches = sorted(glob.glob(str(out_dir / plan["checkpoint_glob"]), recursive=True))
        if len(matches) != 1 or not Path(matches[0]).is_file():
            status, reason = "INVALID", f"checkpoint glob matched {len(matches)} files (need exactly 1)"
        else:
            checkpoint = matches[0]
            ckpt_sha = _sha(Path(checkpoint))
    if status == "VALID":
        score_out = run_dir / "score.json"
        argv = [a.replace("{checkpoint}", checkpoint).replace("{score_out}", str(score_out)) for a in plan["score_argv"]]
        sc = _stage("score", argv, plan["cwd"], run_dir, env, deadline)
        stages.append(sc)
        if sc.get("exit_code") != 0:
            status, reason = "INVALID", f"score failed: exit={sc.get('exit_code')} {sc.get('error', '')}".strip()
        else:
            metric, data, err = _metric(score_out, plan["metric_key"])
            if err:
                status, reason = "INVALID", err
            else:
                quality = {k: v for k, v in data.items() if k != plan["metric_key"]}
    if status == "VALID" and plan.get("rescore"):
        if _sha(Path(checkpoint)) != ckpt_sha:
            status, reason = "INVALID", "checkpoint changed after scoring"
        else:
            rescore_out = run_dir / "rescore.json"
            argv = [a.replace("{checkpoint}", checkpoint).replace("{score_out}", str(rescore_out)) for a in plan["score_argv"]]
            rs = _stage("rescore", argv, plan["cwd"], run_dir, env, deadline)
            stages.append(rs)
            if rs.get("exit_code") != 0:
                status, reason = "INVALID", f"rescore failed: exit={rs.get('exit_code')}"
            else:
                rescore_metric, _, err = _metric(rescore_out, plan["metric_key"])
                if err:
                    status, reason = "INVALID", err
                elif abs(rescore_metric - metric) > float(plan["rescore_tolerance"]):
                    status, reason = "INVALID", f"reload/rescore mismatch {metric!r} vs {rescore_metric!r}"

    receipt = {
        "schema": "g03-run-receipt.v1", "request_id": plan["request_id"], "status": status, "reason": reason,
        "metric": metric, "rescore_metric": rescore_metric, "quality": quality,
        "checkpoint": checkpoint, "checkpoint_sha256": ckpt_sha, "stages": stages,
        "started": started, "finished": time.time(), "elapsed": time.time() - started,
        "plan_sha256": _sha(Path(plan_path)),
    }
    tmp = run_dir / ".receipt.json.tmp"
    tmp.write_text(json.dumps(receipt, indent=1, ensure_ascii=False, allow_nan=False))
    os.replace(tmp, receipt_path)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: runner.py <plan.json>")
    raise SystemExit(run(sys.argv[1]))
