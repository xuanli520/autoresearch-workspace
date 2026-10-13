"""g03_run: the only experiment entry (design §5, §8). Deterministic request_id, no replay of recorded runs."""
from __future__ import annotations

import json
import math
import re
import shutil
import sys
import time
from pathlib import Path

from . import protocol as P
from .engine import G03Error, _atomic_write
from .executors import InfraBlocked, job_runtime, make_executor
from .hashing import sha256_file, sha256_obj, tree_hash

RID_OK = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}")
BASE_KINDS = {"dev": ("dev", "diag"), "diag": ("diag", "dev")}


AUTO_STAGE = {"formal_b": "S3", "formal_r": "S5", "dev": "S1_5", "screen": "S4"}


def _scale(v, mult):
    x = v * mult
    return int(x) if isinstance(v, int) and float(x).is_integer() else x


def _mult_tag(mult: float) -> str:
    return "" if mult == 1 else "x" + (str(int(mult)) if float(mult).is_integer() else str(mult).replace(".", "p"))


class Runs:
    # -- hashing / identity ---------------------------------------------
    def method_path(self, method: str, kind: str) -> Path:
        if method == "base":
            return self.baseline_dir()
        if kind == "formal_r":
            fr = self.st.frozen
            if not fr or fr["version"] != method:
                raise G03Error(f"{method} is not the frozen candidate")
            return self.dir / fr["frozen_path"]
        if method not in self.st.candidates:
            raise G03Error(f"unknown candidate {method}")
        return self.candidate_dir(method)

    def method_hash(self, method: str, kind: str) -> str:
        if method == "base" and self.st.lock and self.st.lock["major"] == self.st.major:
            return self.st.lock["baseline_hash"]
        if kind == "formal_r":
            return self.st.frozen["content_hash"]
        return tree_hash(self.method_path(method, kind))

    def request_id(self, proto: dict, kind: str, method: str, mhash: str, seed: int, mult: float) -> str:
        h8 = sha256_obj([mhash, P.exec_hash(proto), P.budget_hash(proto, mult)])[:8]
        cand = f"{'base' if method == 'base' else method}{_mult_tag(mult)}.{h8}"
        rid = f"{self.cfg['task_id']}-p{self.st.major}-{kind}-{cand}-{seed}"
        if not RID_OK.fullmatch(rid):
            raise G03Error(f"request_id not schedulable: {rid!r} (shorten task_id)")
        return rid

    def find(self, kinds, method: str, mhash: str, seed: int, proto: dict, mult: float = 1) -> dict | None:
        """Most recent recorded (valid or invalid, not invalidated) run matching identity in this major."""
        eh, bh = P.exec_hash(proto), P.budget_hash(proto, mult)
        best = None
        for rid, r in self.st.runs.items():
            if rid in self.st.invalidated or r["major"] != self.st.major or r["kind"] not in kinds:
                continue
            if (r["method"], r["method_hash"], r["seed"], r["exec_hash"], r["budget_hash"]) != (method, mhash, seed, eh, bh):
                continue
            if best is None or r["seq"] > best["seq"]:
                best = r
        return best

    # -- lock integrity --------------------------------------------------
    def lock_mismatches(self, full: bool = False) -> list[str]:
        lk = self.st.lock
        if not lk or lk["major"] != self.st.major:
            return []
        bad = []
        if tree_hash(self.rpath(lk["proto"]["paths"]["baseline"])) != lk["baseline_hash"]:
            bad.append("baseline")
        for path, h in lk["scorer_hashes"].items():
            p = self.rpath(path)
            if not p.exists() or tree_hash(p) != h:
                bad.append(f"scorer:{path}")
        if full:
            for name, ent in lk["proto"]["data"].items():
                p = self.rpath(ent["path"])
                if not p.exists() or tree_hash(p) != ent["sha256"]:
                    bad.append(f"data:{name}")
        return bad

    def baseline_mismatch(self) -> bool:
        return bool(self.st.baseline_fixed) and tree_hash(self.baseline_dir()) != self.st.baseline_fixed

    # -- targets ----------------------------------------------------------
    def targets(self, kind: str, method: str, seeds: list[int] | None, mult: float) -> list[tuple]:
        proto = self.proto()
        st = self.st
        if kind == "diag":
            if method != "base":
                raise G03Error("diag runs are Baseline-only")
            mults = [1] + [P.norm_mult(m) for m in proto["diag_budget_multipliers"]]
            if mult not in mults:
                raise G03Error(f"multiplier must be one of {mults}")
            pool = proto["seeds"]["dev"] if mult == 1 else proto["seeds"]["dev"][:1]
        elif kind in ("dev", "screen"):
            mult = 1
            pool = proto["seeds"][kind]
            if method != "base" and (method in st.rejected or method not in st.candidates):
                raise G03Error(f"{method} is unknown or rejected")
        elif kind in ("formal_b", "formal_r"):
            mult = 1
            if not st.group or st.group["closed"] or st.group["major"] != st.major:
                raise G03Error("no open formal group")
            pool = st.group["seeds"]
            if kind == "formal_b" and method != "base":
                raise G03Error("formal_b is Baseline-only")
            if kind == "formal_r" and (not st.frozen or method != st.frozen["version"]):
                raise G03Error("formal_r only runs the frozen candidate")
        else:
            raise G03Error(f"unknown kind {kind}")
        chosen = pool if not seeds else seeds
        extra = sorted(set(chosen) - set(pool))
        if extra:
            raise G03Error(f"seeds {extra} are not in the {kind} seed set {pool}")
        out = []
        if kind in ("dev", "screen") and method != "base":
            out += [("base", s, 1) for s in pool]  # paired Baseline runs are always present first
        out += [(method, s, mult) for s in chosen]
        return out

    # -- public entry -----------------------------------------------------
    def run(self, kind: str, method: str = "base", seeds: list[int] | None = None, mult: float = 1,
            *, internal: bool = False) -> dict:
        mult = P.norm_mult(mult)
        with self.locked():
            self.reload()
            self.apply_control()
            st = self.st
            if st.terminal:
                raise G03Error("conductor is terminal", code="TERMINAL")
            if st.paused:
                raise G03Error(f"paused: {st.paused['reason']}", code="PAUSED")
            if not internal:
                self._check_allowed("g03_run", kind)
            elif AUTO_STAGE.get(kind) != st.stage:
                raise G03Error(f"auto run {kind} not allowed in {st.stage}", code="STAGE_FORBIDDEN")
            results = []
            for m, seed, x in self.targets(kind, method, seeds, mult):
                self.apply_control()
                if self.st.terminal or self.st.paused:
                    break
                results.append(self._run_one(kind, m, seed, x))
            return {"ok": True, "stage": self.st.stage, "results": [self._public(r) for r in results]}

    def _public(self, r: dict) -> dict:
        out = {k: r.get(k) for k in ("request_id", "kind", "method", "seed", "mult", "status", "reason", "metric", "reused")}
        if r.get("kind") == "formal_r" and self.st.stage not in ("S6", "DONE", "STOP_UNMET"):
            out["metric"] = "hidden-until-S6"
        return out

    def _run_one(self, kind: str, method: str, seed: int, mult: float) -> dict:
        st = self.st
        proto = self.proto()
        bad = self.lock_mismatches() or (["baseline"] if self.baseline_mismatch() else [])
        if bad:
            raise G03Error("locked files changed; conductor will route this at the next gate", code="LOCK_VIOLATION",
                           mismatches=bad)
        mhash = self.method_hash(method, kind)
        if kind == "formal_r" and tree_hash(self.method_path(method, kind)) != mhash:
            raise G03Error("frozen candidate changed", code="LOCK_VIOLATION")
        if method == "base" and kind in BASE_KINDS and mult == 1:
            prior = self.find(BASE_KINDS[kind], method, mhash, seed, proto)
            if prior:
                return dict(prior, reused=True)
        rid = self.request_id(proto, kind, method, mhash, seed, mult)
        if rid in st.runs and rid not in st.invalidated:
            return dict(st.runs[rid], reused=True)
        budget = {k: _scale(v, mult) for k, v in proto["budget_per_run"].items()}
        rescore = kind in ("formal_b", "formal_r")
        need = job_runtime(budget["max_runtime_seconds"], rescore)
        stop = self.budget_allows(need)
        if stop:
            self.terminate("STOP_UNMET", stop, detail=f"next run needs {need}s")
            raise G03Error(f"budget: {stop}", code="BUDGET")
        run_dir = self.dir / "runs" / rid.replace("/", "_")
        if run_dir.exists() and not (run_dir / "receipt.json").exists() and self.cfg["executor"]["kind"] == "local_cpu":
            dest = run_dir.with_name(run_dir.name + f".abandoned-{int(time.time())}")
            run_dir.rename(dest)
            self.log("run_abandoned", request_id=rid, moved_to=str(dest.relative_to(self.dir)))
        run_dir.mkdir(parents=True, exist_ok=True)
        plan = self._plan(proto, kind, method, seed, mult, budget, rid, run_dir, rescore)
        plan_path = run_dir / "plan.json"
        text = json.dumps(plan, indent=1, ensure_ascii=False, sort_keys=True)
        if plan_path.exists() and plan_path.read_text() != text:
            raise G03Error(f"existing plan for {rid} differs; refusing to change a recorded intent", code="PLAN_CONFLICT")
        if not plan_path.exists():
            _atomic_write(plan_path, text)
        if rid not in st.run_starts:
            self.log("run_start", request_id=rid, kind=kind, method=method, seed=seed, mult=mult,
                     plan_sha256=sha256_file(plan_path), major=st.major)
        if not (run_dir / "receipt.json").exists():
            exe = make_executor(self.cfg["executor"], getattr(self, "client_factory", None))

            def on_state(job):
                self.log("gpu_wait", request_id=rid, state=job.get("state"), job_id=job.get("id"),
                         reason=job.get("reason"))

            try:
                if exe.kind == "gpu_scheduler":
                    job = exe.execute(plan_path, plan, on_state, template=proto.get("gpu_spec_template") or {},
                                      deadline_at=self.cfg["budget_total"]["deadline_at"])
                else:
                    job = exe.execute(plan_path, plan, on_state)
            except InfraBlocked as exc:
                self.log("infra", request_id=rid, state=exc.state, detail=exc.detail,
                         job_id=(exc.job or {}).get("id"))
                raise G03Error(f"infrastructure: {exc}", code="BLOCKED_INFRA", request_id=rid) from exc
            except Exception as exc:  # runner crash without receipt: record, never replay
                job = {"executor": exe.kind, "error": f"{type(exc).__name__}: {exc}"[:2000]}
        else:
            job = {"executor": self.cfg["executor"]["kind"], "ingested": True}
        return self._record(rid, kind, method, seed, mult, mhash, proto, run_dir, plan_path, job)

    def _plan(self, proto, kind, method, seed, mult, budget, rid, run_dir, rescore) -> dict:
        exe = self.cfg["executor"]
        python = exe.get("python") or sys.executable
        mdir = self.method_path(method, kind)
        values = {"python": python, "seed": str(seed), "kind": kind, "method": method, "method_dir": str(mdir),
                  "out_dir": str(run_dir / "out"), "private": str(self.root), "run_dir": str(run_dir),
                  "starter": str(self.rpath(proto["paths"]["starter"])), "baseline": str(self.baseline_dir())}
        for k, v in proto["paths"].items():
            if isinstance(v, str):
                values[f"path.{k}"] = str(self.rpath(v))
        for k, v in proto["data"].items():
            values[f"data.{k}"] = str(self.rpath(v["path"]))
        for k, v in budget.items():
            values[f"budget.{k}"] = str(v)

        def render(arg: str) -> str:
            def sub(m):
                key = m.group(1)
                if key in ("checkpoint", "score_out"):
                    return m.group(0)
                if key not in values:
                    raise G03Error(f"unknown placeholder {{{key}}}")
                return values[key]
            return P.PLACEHOLDER.sub(sub, arg)

        cmds = proto["commands"]
        return {"request_id": rid, "run_dir": str(run_dir), "out_dir": str(run_dir / "out"),
                "cwd": render(cmds.get("cwd", "{private}")), "train_argv": [render(a) for a in cmds["train"]],
                "score_argv": [render(a) for a in cmds["score"]], "checkpoint_glob": cmds["checkpoint"],
                "metric_key": proto["result"]["metric_key"], "rescore": rescore,
                "rescore_tolerance": proto["result"]["rescore_tolerance"],
                "max_runtime_seconds": int(budget["max_runtime_seconds"]),
                "cpu_only": exe["kind"] == "local_cpu",
                "env": {"G03_SEED": str(seed), "G03_KIND": kind, "G03_METHOD": method}}

    def _record(self, rid, kind, method, seed, mult, mhash, proto, run_dir, plan_path, job) -> dict:
        receipt_path = run_dir / "receipt.json"
        fields = dict(request_id=rid, kind=kind, method=method, seed=seed, mult=mult, method_hash=mhash,
                      exec_hash=P.exec_hash(proto), budget_hash=P.budget_hash(proto, mult), major=self.st.major,
                      lock_id=self.st.lock_id if self.st.lock and self.st.lock["major"] == self.st.major else None,
                      draft_sha=self.st.draft_sha, run_dir=str(run_dir.relative_to(self.dir)),
                      plan_sha256=sha256_file(plan_path), executor=job,
                      group_idx=self.st.group["idx"] if kind.startswith("formal") and self.st.group else None)
        if not receipt_path.exists():
            fields.update(status="INVALID", reason=f"no receipt ({(job or {}).get('error') or 'runner produced none'})",
                          metric=None, rescore_metric=None, quality={}, checkpoint=None, checkpoint_sha256=None,
                          receipt_sha256=None, elapsed=0.0)
            return self.log("run", **fields)
        rc = json.loads(receipt_path.read_text())
        status, reason = rc.get("status"), rc.get("reason")
        if rc.get("request_id") != rid or rc.get("plan_sha256") != fields["plan_sha256"]:
            status, reason = "INVALID", "receipt does not match plan"
        metric = rc.get("metric")
        if status == "VALID" and (not isinstance(metric, (int, float)) or not math.isfinite(metric)):
            status, reason = "INVALID", "non-finite metric"
        if status == "VALID" and kind.startswith("formal") and rc.get("rescore_metric") is None:
            status, reason = "INVALID", "formal run lacks independent reload/rescore"
        quality = {k: v for k, v in (rc.get("quality") or {}).items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
        fields.update(status=status, reason=reason, metric=metric, rescore_metric=rc.get("rescore_metric"),
                      quality=quality, checkpoint=rc.get("checkpoint"), checkpoint_sha256=rc.get("checkpoint_sha256"),
                      receipt_sha256=sha256_file(receipt_path), elapsed=float(rc.get("elapsed") or 0.0))
        return self.log("run", **fields)


def copy_frozen(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for p in sorted(dest.rglob("*"), reverse=True):
        p.chmod(0o444 if p.is_file() else 0o555)
    dest.chmod(0o555)
