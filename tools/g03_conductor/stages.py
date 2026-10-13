"""Deterministic stage evaluators and the `next` driver (design §4, §6, §7)."""
from __future__ import annotations

import json
import time

from . import protocol as P
from . import stats as S
from .engine import MAX_B_REVISIONS, G03Error, _atomic_write
from .hashing import sha256_file, sha256_obj, tree_hash
from .runs import copy_frozen
from .summary import build_summary, validity_checks
from .state import TERMINAL

STAGE_NUDGE_FACTOR = 5  # a stage that keeps "continuing" without exiting is paused even if notes reset the idle count
GUIDE = {
    "S0": "S0 立题：起草 protocol.yaml（参考 tools/g03_conductor/examples/protocol.example.yaml），用 g03_propose(kind=protocol, content=...) 提交。"
          "三组 seed 互不重叠，正式组 n≥3（默认 5），U 必须写事前依据。",
    "S1": "S1 Baseline 诊断（G02）：派 g03-worker 核对/实现 Baseline（不得弱化），用 g03_run(kind=diag) 跑开发 seed 标准预算与加倍预算诊断，"
          "然后 g03_note(tag=diagnosis) 写训练充分性结论。conductor 会强制 reviewer 审查。",
    "S1_5": "S1.5 可行性预检：Baseline 已固定。g03_propose(kind=candidate) 建 Reference 候选，派 g03-worker 在候选目录实现，"
            "再 g03_run(kind=dev, method=vN)。只能改 Reference 方法和 budget_per_run（只增不减）。",
    "S4": "S4 探索：g03_propose(kind=candidate) 提出新候选，派 g03-worker 实现，g03_run(kind=screen, method=vN) 筛选。"
          "达标候选由 conductor 自动提名并强制审查后进入正式确认。负结果用 g03_note(tag=negative) 记录。",
}


class Stages:
    # -- result helpers ---------------------------------------------------
    def _act(self, action: str, message: str, **extra) -> dict:
        return {"action": action, "stage": self.st.stage, "message": message, **extra}

    def _auto(self, kind: str, method: str, seeds: list[int], why: str) -> dict:
        return self._act("auto_run", why, auto_run={"kind": kind, "method": method, "seeds": seeds})

    def _pause(self, reason: str, detail: str = "") -> dict:
        self.log("pause", reason=reason, detail=detail, stage=self.st.stage)
        return self._act("paused", f"已暂停：{reason} {detail}".strip())

    def _gate(self, gate: str, subject: str, context: dict):
        """Return ('pass'|'fail', review) or an action dict to return immediately."""
        status, rev = self.gate_status(gate, subject)
        if status == "pending":
            return self.request_review(gate, subject, context)
        if status == "exhausted":
            return self._pause("review_undecidable", f"{gate} 连续 {len(self.st.reviews)} 次无法判定，需专家处理")
        return status, rev

    def _route_fail(self, rev: dict, default: str | None, reason: str):
        """Map a FAIL verdict to a failure class; returns an action dict or None for the caller default."""
        d = rev.get("defect")
        if d == "implementation":
            return self.enter_b(reason, rev)
        if d == "protocol":
            return self.enter_c(reason, rev)
        if default == "B":
            return self.enter_b(reason, rev)
        if default == "C":
            return self.enter_c(reason, rev)
        if default == "pause":
            return self._pause("review_fail", f"{reason}；reviewer 未给出可执行分类，需专家判断")
        return None

    def _runs_for(self, kinds, method, mhash, seeds, proto, mult=1) -> tuple[dict, list[int]]:
        found, missing = {}, []
        for s in seeds:
            r = self.find(kinds, method, mhash, s, proto, mult)
            if r is None:
                missing.append(s)
            else:
                found[s] = r
        return found, missing

    @staticmethod
    def _invalid(found: dict) -> list[dict]:
        return [r for r in found.values() if r["status"] != "VALID"]

    def _metrics(self, found: dict, seeds: list[int]) -> list[float]:
        return [found[s]["metric"] for s in seeds]

    # -- failure classes (design §7) ----------------------------------------
    def observed_R(self) -> list[float]:
        groups: dict = {}
        for rid, r in self.st.runs.items():
            if r["method"] == "base" or r["status"] != "VALID" or rid in self.st.invalidated or r["major"] != self.st.major:
                continue
            groups.setdefault((r["kind"], r["method"], r["method_hash"]), []).append(r["metric"])
        return [S.mean(v) for v in groups.values()]

    def enter_c(self, reason: str, rev: dict | None = None) -> dict:
        st = self.st
        if st.rev_c >= 1:
            self.terminate("STOP_UNMET", "protocol_revision_limit", detail=reason)
            return self._act("terminal", f"协议修订次数已用完：{reason}")
        old = self.proto()
        pending = {"old_proto": old, "old_sha": st.draft_sha, "observed_R": self.observed_R(),
                   "baseline_hash": st.baseline_fixed or tree_hash(self.baseline_dir()),
                   "used_formal": sorted(st.consumed_seeds)}
        self.log("failure", **{"class": "C", "reason": reason, "review_seq": rev and rev["seq"], "major": st.major})
        self.log("revision", **{"class": "C", "from_major": st.major, "to_major": st.major + 1, "reason": reason,
                                "pending": pending})
        self.transition("S0", f"type C: {reason}")
        return self._act("continue", f"类型 C 协议修订（{st.rev_c}/1）：{reason}。" + GUIDE["S0"] +
                         " 修订须满足：不削弱 Baseline 预算/实现；U 只按事前依据变动且不向已观测 R 移动；正式 seed 全部换新。")

    def enter_b(self, reason: str, rev: dict | None = None) -> dict:
        st = self.st
        if st.stage == "S1" and not st.baseline_fixed:
            self.log("failure", **{"class": "B", "reason": reason, "review_seq": rev and rev["seq"], "major": st.major,
                                   "in_place": True})
            return self._act("continue", f"类型 B（S1 内修复）：{reason}。修复 Baseline/评分器/数据后重跑诊断。")
        if st.rev_b >= MAX_B_REVISIONS:
            self.terminate("STOP_UNMET", "implementation_revision_cap", detail=reason)
            return self._act("terminal", "类型 B 修订达到工程上限")
        draft, sha = st.draft, st.draft_sha
        rids = [rid for rid, r in st.runs.items() if r["major"] == st.major and rid not in st.invalidated]
        confirms = [c["seq"] for c in st.confirms if c["major"] == st.major]
        self.log("failure", **{"class": "B", "reason": reason, "review_seq": rev and rev["seq"], "major": st.major})
        self.log("revision", **{"class": "B", "from_major": st.major, "to_major": st.major + 1, "reason": reason,
                                "pending": None})
        self.log("invalidate", request_ids=rids, voided_confirms=confirms, reason=f"type B: {reason}")
        n = self.st.draft_n + 1
        self.log("draft", proto=draft, sha=sha, draft_id=f"p{self.st.major}d{n}", n=n, major=self.st.major,
                 stage=self.st.stage, path=f"drafts/p{self.st.major}d{n}.carried", file_sha256=None)
        self.transition("S1", f"type B: {reason}")
        return self._act("continue", f"类型 B 实现缺陷，已生成 p{self.st.major} 并作废 {len(rids)} 条受影响证据（保留不删）：{reason}。"
                         + GUIDE["S1"])

    # -- shared pre-checks --------------------------------------------------
    def _claims(self):
        for c in self.st.open_claims():
            gate = "defect_claim" if c["kind"] == "defect" else "infeasible_claim"
            g = self._gate(gate, sha256_obj([c["claim_id"], c["description"]]), {"claim": c})
            if isinstance(g, dict):
                return g
            status, rev = g
            self.log("claim_resolved", claim_id=c["claim_id"], outcome=status, review_seq=rev["seq"])
            if status == "pass":
                return self.enter_b(f"confirmed defect claim {c['claim_id']}", rev) if c["kind"] == "defect" \
                    else self.enter_c(f"confirmed infeasibility claim {c['claim_id']}", rev)
        return None

    def _violations(self):
        bad = self.lock_mismatches(full=False)
        if self.st.baseline_fixed and not self.st.lock and self.baseline_mismatch():
            bad = ["baseline"]
        if not bad:
            return None
        key = sha256_obj([self.st.major, sorted(bad)])
        if not any(v["key"] == key for v in self.st.lock_violations):
            self.log("lock_violation", key=key, items=bad, major=self.st.major)
        g = self._gate("lock_violation", key, {"mismatched": bad, "lock": self.st.lock and {
            k: self.st.lock[k] for k in ("baseline_hash", "scorer_hashes")}, "baseline_fixed": self.st.baseline_fixed})
        if isinstance(g, dict):
            return g
        status, rev = g
        if status == "pass" or rev.get("defect") == "implementation":
            return self.enter_b(f"locked files changed as a legitimate fix: {bad}", rev)
        return self._pause("lock_violation", f"锁定文件 {bad} 被改动且审查未认定为合法修复；恢复到锁定哈希后 /g03 resume")

    # -- S0 / S1 ---------------------------------------------------------------
    def eval_S0(self):
        st = self.st
        if st.draft is None or st.draft_major != st.major:
            return self._act("continue", GUIDE["S0"] + (" （这是类型 C 修订后的新协议）" if st.pending_c else ""))
        if st.pending_c:
            g = self._gate("C_revision", st.draft_sha, {"old_sha": st.pending_c["old_sha"], "new_sha": st.draft_sha,
                                                         "observed_R": st.pending_c["observed_R"],
                                                         "used_formal": st.pending_c["used_formal"]})
            if isinstance(g, dict):
                return g
            status, rev = g
            if status == "fail":
                return self._act("continue", f"类型 C 修订被 reviewer 驳回，见 {rev['report']}；请重新提交修订草案。")
            self.log("revision_c_approved", draft_sha=st.draft_sha, review_seq=rev["seq"])
        self.transition("S1", "protocol draft schema-valid", [st.draft_id])
        return None

    def eval_S1(self):
        st, proto = self.st, self.proto()
        bh = tree_hash(self.baseline_dir())
        dev = proto["seeds"]["dev"]
        found, missing = self._runs_for(("diag", "dev"), "base", bh, dev, proto)
        extra = {}
        for m in proto["diag_budget_multipliers"]:
            f2, miss2 = self._runs_for(("diag",), "base", bh, dev[:1], proto, m)
            extra[m] = f2
            missing += [f"{s}@x{m}" for s in miss2]
        if missing:
            return self._act("continue", GUIDE["S1"] + f" 缺少诊断运行：{missing}。")
        bad = self._invalid(found) + [r for f in extra.values() for r in self._invalid(f)]
        if bad:
            return self._act("continue", "诊断运行无效：" + "; ".join(f"{r['request_id']}: {r['reason']}" for r in bad)
                             + "。修复 Baseline/命令后重跑（哈希变化会生成新的 request_id）。")
        notes = [n for n in st.notes if n["tag"] == "diagnosis" and n["major"] == st.major]
        if not notes:
            return self._act("continue", "诊断运行齐全。请用 g03_note(tag=diagnosis) 写训练充分性与预算诊断结论（G02）。")
        rids = sorted(r["request_id"] for r in list(found.values()) + [r for f in extra.values() for r in f.values()])
        subject = sha256_obj([bh, rids, st.draft_sha, notes[-1]["seq"]])
        vals = [found[s]["metric"] for s in dev]
        ctx = {"baseline_hash": bh, "draft": st.draft_id, "dev_metrics": dict(zip(dev, vals)),
               "dev_mean": S.mean(vals), "dev_std": S.sample_std(vals),
               "budget_multiplier_runs": {str(m): {s: r["metric"] for s, r in f.items()} for m, f in extra.items()},
               "diagnosis_note": notes[-1]["path"], "runs": rids}
        g = self._gate("S1_baseline", subject, ctx)
        if isinstance(g, dict):
            return g
        status, rev = g
        if status == "fail":
            routed = self._route_fail(rev, None, "S1 baseline review failed")
            return routed or self._act("continue", f"G02 审查未通过，见 {rev['report']}。修复后重跑诊断/更新诊断说明。")
        self.log("baseline_fixed", content_hash=bh, review_seq=rev["seq"], major=st.major)
        self.transition("S1_5", "G02 baseline diagnosis passed", rids)
        return None

    # -- S1_5 feasibility on dev seeds ---------------------------------------
    def _cand_table(self, kind: str, seeds: list[int], proto, B: float, sigma: float, win: dict) -> list[dict]:
        rows = []
        margin = proto["margin_sigma"]
        direction = proto["metric"]["direction"]
        for v in sorted(self.st.candidates, key=lambda x: int(x[1:])):
            if v in self.st.rejected:
                continue
            if self.st.frozen and self.st.frozen["version"] == v and kind == "screen":
                continue
            h = tree_hash(self.candidate_dir(v))
            found, missing = self._runs_for((kind,), v, h, seeds, proto)
            row = {"version": v, "hash": h, "missing": missing, "invalid": [r["request_id"] for r in self._invalid(found)]}
            if not missing and not row["invalid"]:
                R = S.mean(self._metrics(found, seeds))
                d = S.signed_delta(direction, B, R)
                row.update(mean=R, delta=d, sigma_multiple=(d / sigma if sigma else None),
                           passed=S.screen_pass(d, sigma, margin, win), runs=sorted(r["request_id"] for r in found.values()))
            rows.append(row)
        return rows

    @staticmethod
    def _fmt_rows(rows: list[dict]) -> str:
        parts = []
        for r in rows:
            if "delta" in r:
                parts.append(f"{r['version']}: Δ={r['delta']:.6g} ({'达标' if r['passed'] else '未达标'})")
            elif r["invalid"]:
                parts.append(f"{r['version']}: 运行无效 {r['invalid']}")
            else:
                parts.append(f"{r['version']}: 缺 seed {r['missing']}")
        return "；".join(parts) or "暂无候选"

    def eval_S1_5(self):
        st, proto = self.st, self.proto()
        for chk in (self._violations, self._claims):
            r = chk()
            if r:
                return r
        dev = proto["seeds"]["dev"]
        found, missing = self._runs_for(("dev", "diag"), "base", st.baseline_fixed, dev, proto)
        if missing:
            return self._auto("dev", "base", missing, "当前预算下的 Baseline 开发 seed 运行")
        bad = self._invalid(found)
        if bad:
            g = self._gate("invalid_runs", sha256_obj(sorted(r["request_id"] for r in bad)), {"invalid": bad})
            if isinstance(g, dict):
                return g
            return self._route_fail(g[1], "B" if g[0] == "fail" else "pause", "baseline dev runs invalid")
        vals = self._metrics(found, dev)
        B, sig = S.mean(vals), S.sample_std(vals)
        win = S.window(proto["metric"]["direction"], B, sig, proto["U"]["value"])
        if not win["valid"]:
            return self.enter_c(f"dev window infeasible: {win['reason']} (B={B:.6g}, σ={sig:.6g})")
        rows = self._cand_table("dev", dev, proto, B, sig, win)
        passing = sorted((r for r in rows if r.get("passed")), key=lambda r: (-r["delta"], int(r["version"][1:])))
        if not passing:
            return self._act("continue", GUIDE["S1_5"] + f" 开发窗口 B={B:.6g} σ_dev={sig:.6g} "
                             f"Δ∈[{win['d_min']:.6g},{win['d_max']:.6g}]，需 Δ≥{(3 + proto['margin_sigma'])}σ。"
                             f"候选：{self._fmt_rows(rows)}。若预算下确实无法区分，用 g03_propose(kind=infeasible)。")
        best = passing[0]
        subject = sha256_obj([st.draft_sha, best["version"], best["hash"], best["runs"]])
        ctx = {"B_dev": B, "sigma_dev": sig, "window": win, "candidate": best, "draft": st.draft_id,
               "baseline_hash": st.baseline_fixed}
        g = self._gate("S1_5_feasibility", subject, ctx)
        if isinstance(g, dict):
            return g
        status, rev = g
        if status == "fail":
            routed = self._route_fail(rev, None, "feasibility review failed")
            if routed:
                return routed
            self.log("reject", version=best["version"], reason="feasibility review failed", review_seq=rev["seq"])
            return self._act("continue", f"{best['version']} 被 reviewer 驳回（{rev['report']}），已标负结果。")
        self.log("feasible", version=best["version"], content_hash=best["hash"], delta=best["delta"], window=win, review_seq=rev["seq"])
        self.transition("S2", f"feasibility: {best['version']} Δ_dev={best['delta']:.6g}", best["runs"])
        return None

    # -- S2 lock -------------------------------------------------------------
    def eval_S2(self):
        st = self.st
        if not st.lock or st.lock["major"] != st.major:
            proto = st.draft
            bh = tree_hash(self.baseline_dir())
            if bh != st.baseline_fixed:
                return self._violations() or self.enter_b("baseline changed before lock")
            bad = [n for n, e in proto["data"].items()
                   if not self.rpath(e["path"]).exists() or tree_hash(self.rpath(e["path"])) != e["sha256"]]
            if bad:
                return self.enter_b(f"data hash mismatch at lock: {bad}")
            scorer = {s: tree_hash(self.rpath(s)) for s in P.scorer_paths(proto)}
            draft_file = self.dir / [d for d in st.drafts if d["draft_id"] == st.draft_id][-1]["path"]
            lock = {"major": st.major, "draft_id": st.draft_id, "draft_sha": st.draft_sha, "proto": proto,
                    "protocol_file_sha256": sha256_file(draft_file) if draft_file.is_file() else None,
                    "baseline_hash": bh, "scorer_hashes": scorer,
                    "data": {n: e["sha256"] for n, e in proto["data"].items()}, "locked_at": time.time()}
            lock_id = sha256_obj({k: v for k, v in lock.items() if k != "locked_at"})
            text = json.dumps(dict(lock, lock_id=lock_id), indent=1, ensure_ascii=False)
            _atomic_write(self.dir / "locks" / f"lock-p{st.major}.json", text)
            _atomic_write(self.dir / "lock.json", text)
            self.log("lock", lock=lock, lock_id=lock_id, major=st.major)
        g = self._gate("S2_lock", self.st.lock_id, {"lock_id": self.st.lock_id, "lock_file": str(self.dir / "lock.json"),
                                                    "baseline_hash": self.st.lock["baseline_hash"],
                                                    "scorer_hashes": self.st.lock["scorer_hashes"], "data": self.st.lock["data"]})
        if isinstance(g, dict):
            return g
        status, rev = g
        if status == "fail":
            return self._route_fail(rev, "pause", "lock review failed")
        self.transition("S3", "protocol locked", [self.st.lock_id])
        return None

    # -- S3 formal Baseline ---------------------------------------------------
    def _group_open(self) -> bool:
        g = self.st.group
        return bool(g) and not g["closed"] and g["major"] == self.st.major and g["lock_id"] == self.st.lock_id

    def _formal_b(self, proto):
        g = self.st.group
        return self._runs_for(("formal_b",), "base", self.st.lock["baseline_hash"], g["seeds"], proto)

    def eval_S3(self):
        st, proto = self.st, self.proto()
        r = self._violations() or self._claims()
        if r:
            return r
        if not self._group_open():
            bad = self.lock_mismatches(full=True)
            if bad:
                return self._violations() or self.enter_b(f"data changed after lock: {bad}")
            pool = proto["seeds"]["formal_pool"]
            free = [(i, g) for i, g in enumerate(pool) if not (set(g) & st.consumed_seeds)]
            if not free:
                self.terminate("STOP_UNMET", "formal_pool_exhausted")
                return self._act("terminal", "正式 seed 池已用尽")
            idx, seeds = free[0]
            self.log("group_start", idx=idx, seeds=seeds, major=st.major, lock_id=st.lock_id, k=st.attempts_a + 1)
        found, missing = self._formal_b(proto)
        if missing:
            return self._auto("formal_b", "base", missing, f"正式 Baseline（第 {self.st.attempts_a + 1} 次确认，组 {self.st.group['idx']}）")
        bad = self._invalid(found)
        if bad:
            g = self._gate("invalid_runs", sha256_obj(sorted(r["request_id"] for r in bad)), {"invalid": bad})
            if isinstance(g, dict):
                return g
            return self._route_fail(g[1], "B" if g[0] == "fail" else "pause", "formal baseline runs invalid")
        seeds = self.st.group["seeds"]
        vals = self._metrics(found, seeds)
        B, sig = S.mean(vals), S.sample_std(vals)
        if sig == 0 and not self.st.signed_off("deterministic", lock_id=self.st.lock_id, group_idx=self.st.group["idx"]):
            return self._act("wait_expert", "σ_B=0：标为 DETERMINISTIC，不套用 σ 规则。需专家签字：python3 -I tools/g03_conductor "
                             f"signoff --root {self.root} --kind deterministic --expert <姓名> --note <理由>")
        win = S.window(proto["metric"]["direction"], B, sig, proto["U"]["value"])
        if not win["valid"]:
            return self.enter_c(f"formal window infeasible: {win['reason']} (B={B:.6g}, σ_B={sig:.6g})")
        self.log("formal_b_stats", B=B, sigma=sig, window=win, group_idx=self.st.group["idx"], lock_id=self.st.lock_id,
                 runs=sorted(r["request_id"] for r in found.values()))
        self.transition("S4", f"formal B={B:.6g} σ_B={sig:.6g}")
        return None

    # -- S4 exploration ----------------------------------------------------------
    def eval_S4(self):
        st, proto = self.st, self.proto()
        r = self._violations() or self._claims()
        if r:
            return r
        fb = st.formal_b_stats
        screen = proto["seeds"]["screen"]
        found, missing = self._runs_for(("screen",), "base", st.lock["baseline_hash"], screen, proto)
        if missing:
            return self._auto("screen", "base", missing, "Baseline 筛选 seed 运行")
        bad = self._invalid(found)
        if bad:
            g = self._gate("invalid_runs", sha256_obj(sorted(r["request_id"] for r in bad)), {"invalid": bad})
            if isinstance(g, dict):
                return g
            return self._route_fail(g[1], "B" if g[0] == "fail" else "pause", "baseline screen runs invalid")
        B_s = S.mean(self._metrics(found, screen))
        rows = self._cand_table("screen", screen, proto, B_s, fb["sigma"], fb["window"])
        tried = {c["version"] for c in st.confirms if c["seq"] not in st.voided_confirms}
        passing = sorted((r for r in rows if r.get("passed") and r["version"] not in tried),
                         key=lambda r: (-r["delta"], int(r["version"][1:])))
        if not passing:
            w = fb["window"]
            return self._act("continue", GUIDE["S4"] + f" 正式 B={fb['B']:.6g} σ_B={fb['sigma']:.6g} 窗口 Δ∈[{w['d_min']:.6g},"
                             f"{w['d_max']:.6g}]，筛选需 Δ≥{3 + proto['margin_sigma']}σ_B 且 ≤Δ_max；Baseline 筛选均值 {B_s:.6g}。"
                             f"候选：{self._fmt_rows(rows)}。")
        best = passing[0]
        subject = sha256_obj([st.lock_id, best["version"], best["hash"], best["runs"]])
        ctx = {"lock_id": st.lock_id, "candidate": best, "B_screen": B_s, "formal_b": fb,
               "candidate_dir": str(self.candidate_dir(best["version"])),
               "description": st.candidates[best["version"]]["description"]}
        g = self._gate("S4_candidate", subject, ctx)
        if isinstance(g, dict):
            return g
        status, rev = g
        if status == "fail":
            routed = self._route_fail(rev, None, "candidate review failed")
            if routed:
                return routed
            self.log("reject", version=best["version"], reason="S4 candidate review failed", review_seq=rev["seq"])
            return self._act("continue", f"{best['version']} 审查未通过（{rev['report']}），已标负结果。")
        dest = self.dir / "frozen" / f"{best['version']}-p{st.major}-{best['hash'][:12]}"
        if not dest.exists():
            copy_frozen(self.candidate_dir(best["version"]), dest)
        if tree_hash(dest) != best["hash"]:
            raise G03Error("frozen copy hash mismatch", code="TAMPERED")
        self.log("freeze", version=best["version"], content_hash=best["hash"], frozen_path=str(dest.relative_to(self.dir)),
                 screen_delta=best["delta"], review_seq=rev["seq"], group_idx=st.group["idx"])
        self.transition("S5", f"candidate {best['version']} frozen", best["runs"])
        return None

    # -- S5 formal confirmation (no early stop) -------------------------------------
    def eval_S5(self):
        st, proto = self.st, self.proto()
        r = self._violations()
        if r:
            return r
        fr = st.frozen
        if tree_hash(self.dir / fr["frozen_path"]) != fr["content_hash"]:
            return self._pause("lock_violation", "冻结候选被改动")
        _, missing = self._runs_for(("formal_r",), fr["version"], fr["content_hash"], st.group["seeds"], proto)
        if missing:
            return self._auto("formal_r", fr["version"], missing, f"正式确认 {fr['version']}：全部正式 seed，不提前停止")
        self.transition("S6", "all formal seeds finished")
        return None

    # -- S6 verdict ---------------------------------------------------------------
    def _fail_a(self, conf: dict, rev: dict, reason: str):
        K = self.proto()["K"]
        self.log("reject", version=conf["version"], reason=reason, review_seq=rev["seq"])
        self.log("failure", **{"class": "A", "reason": reason, "confirm_seq": conf["seq"], "review_seq": rev["seq"],
                               "major": self.st.major})
        if self.st.attempts_a >= K:
            self.terminate("STOP_UNMET", "K_exhausted", detail=f"{self.st.attempts_a}/{K} confirmations failed")
            return self._act("terminal", f"确认 {self.st.attempts_a}/{K} 次均未达标")
        self.transition("S3", f"type A ({self.st.attempts_a}/{K}): new formal seeds, Baseline reruns")
        return None

    def eval_S6(self):
        st, proto = self.st, self.proto()
        fr, grp = st.frozen, st.group
        seeds = grp["seeds"]
        bf, _ = self._formal_b(proto)
        rf, _ = self._runs_for(("formal_r",), fr["version"], fr["content_hash"], seeds, proto)
        conf = next((c for c in st.confirms if c["major"] == st.major and c["group_idx"] == grp["idx"]
                     and c["version"] == fr["version"] and c["seq"] not in st.voided_confirms), None)
        if conf is None:
            invalid = sorted(r["request_id"] for r in list(bf.values()) + list(rf.values()) if r["status"] != "VALID")
            validity = validity_checks(self, proto, bf, rf, seeds)
            verdict = None
            if not invalid:
                B, sig = S.mean(self._metrics(bf, seeds)), S.sample_std(self._metrics(bf, seeds))
                R = S.mean(self._metrics(rf, seeds))
                verdict = S.final_verdict(proto["metric"]["direction"], B, sig, R, proto["U"]["value"], validity)
            conf = self.log("confirm", version=fr["version"], content_hash=fr["content_hash"], group_idx=grp["idx"], seeds=seeds,
                            major=st.major, lock_id=st.lock_id, k=st.attempts_a + 1, invalid=invalid,
                            validity=validity, verdict=verdict,
                            b_runs=sorted(r["request_id"] for r in bf.values()),
                            r_runs=sorted(r["request_id"] for r in rf.values()))
        subj = sha256_obj(["confirm", conf["seq"], conf["content_hash"]])
        if conf["invalid"]:
            g = self._gate("S6_invalid", subj, {"confirm": conf})
            if isinstance(g, dict):
                return g
            _, rev = g
            return self._route_fail(rev, None, "formal runs invalid") or self._fail_a(conf, rev, "formal runs invalid")
        if not conf["verdict"]["passed"]:
            g = self._gate("S6_fail", subj, {"confirm": conf})
            if isinstance(g, dict):
                return g
            _, rev = g
            return self._route_fail(rev, None, "confirmation failed") or \
                self._fail_a(conf, rev, f"confirmation failed: {[k for k, v in conf['verdict']['checks'].items() if not v]}")
        summary = build_summary(self, proto, bf, rf, conf)
        text = json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True)
        path = self.dir / "summary" / f"comparison_summary-p{st.major}-g{grp['idx']}.json"
        if not path.exists() or path.read_text() != text:
            _atomic_write(path, text)
        sha = sha256_file(path)
        g = self._gate("S6_final", sha, {"summary": str(path), "verdict": conf["verdict"], "frozen": fr})
        if isinstance(g, dict):
            return g
        status, rev = g
        if status == "fail":
            if rev.get("defect") == "candidate":
                return self._fail_a(conf, rev, "final review: candidate leak/cheat")
            return self._route_fail(rev, "pause", "final review failed")
        _atomic_write(self.dir / "final" / "comparison_summary.json", text)
        self.log("summary", path="final/comparison_summary.json", sha256=sha, review_seq=rev["seq"])
        self.terminate("DONE", f"G03 PASS ({conf['verdict']['label']})", summary="final/comparison_summary.json",
                       label=conf["verdict"]["label"], sigma_multiple=conf["verdict"]["sigma_multiple"],
                       score=conf["verdict"]["score"])
        return None

    def eval_DONE(self):
        return self._terminal()

    eval_STOP_UNMET = eval_DONE

    def _terminal(self) -> dict:
        t = self.st.terminal or {}
        msg = "G03 完成：DONE" if t.get("status") == "DONE" else "G03 停止：STOP_UNMET（未达标/待补证，交由专家决定）"
        return {"action": "terminal", "stage": self.st.stage, "message": f"{msg}；原因：{t.get('reason')}", "terminal": t}

    # -- driver -------------------------------------------------------------------
    def next(self) -> dict:
        with self.locked():
            for _ in range(80):
                self.reload()
                self.apply_control()
                st = self.st
                if st.terminal:
                    return self._terminal()
                if st.paused:
                    return self._act("paused", f"已暂停：{st.paused['reason']}；处理后 /g03 resume", side="PAUSED")
                if st.blocked:
                    b = st.blocked
                    return self._act("blocked", f"基础设施阻塞 {b['state']}：{b['detail']}。按 gpu_scheduler USAGE 查询原 request "
                                     f"{b['request_id']}，不重提新 ID；处理后 /g03 resume。", side="BLOCKED_INFRA")
                stop = self.budget_stop()
                if stop:
                    self.terminate("STOP_UNMET", stop)
                    continue
                res = getattr(self, f"eval_{st.stage}")()
                if res is None:
                    continue
                if res["action"] == "terminal":
                    return self._terminal()
                res["stage"] = self.st.stage
                res["side"] = self.st.side()
                if res["action"] == "continue":
                    cap = self.cfg["max_idle_nudges"]
                    if self.st.nudges >= cap or self.st.stage_nudges >= cap * STAGE_NUDGE_FACTOR:
                        return self._pause("stalled", f"连续 {self.st.nudges} 次催促无进展（本阶段累计 {self.st.stage_nudges}）")
                    self.log("nudge", stage=self.st.stage)
                return res
            raise G03Error("driver did not converge", code="INTERNAL")
