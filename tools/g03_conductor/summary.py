"""comparison_summary.json (autoresearch-comparison-summary.v2) and S6 validity checks (design §6)."""
from __future__ import annotations

import operator
from pathlib import Path

from . import stats as S
from .hashing import sha256_file, tree_files

OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


def quality_gate(proto: dict, runs: dict, seeds: list[int]) -> dict | None:
    gate = proto.get("aggregate_quality_gate")
    if not gate:
        return None
    means, status = {}, "PASS"
    for c in gate["checks"]:
        vals = [runs[s]["quality"].get(c["field"]) for s in seeds]
        if any(v is None for v in vals):
            means[c["field"]] = None
            status = "FAIL"
            continue
        m = S.mean(vals)
        means[c["field"]] = m
        if not OPS[c["op"]](m, c["value"]):
            status = "FAIL"
    return {"means": means, "status": status}


def leak_check(cond, proto: dict) -> dict:
    """Files that are Reference-specific (differ from Baseline) must not appear in the Starter."""
    frozen = cond.dir / cond.st.frozen["frozen_path"]
    base = cond.rpath(proto["paths"]["baseline"])
    starter = cond.rpath(proto["paths"]["starter"])
    base_hashes = {sha256_file(p) for _, p in tree_files(base)} if base.is_dir() else {sha256_file(base)}
    ref_only = {sha256_file(p): rel for rel, p in tree_files(frozen)
                if rel != "CANDIDATE.md" and sha256_file(p) not in base_hashes}
    hits = [rel for rel, p in tree_files(starter) if p.is_file() and sha256_file(p) in ref_only] if starter.is_dir() else []
    private = cond.root
    try:
        private.relative_to(starter)
        nested = True
    except ValueError:
        nested = False
    return {"leaked_files": hits, "private_inside_starter": nested, "ok": not hits and not nested}


def validity_checks(cond, proto: dict, bf: dict, rf: dict, seeds: list[int]) -> dict:
    allr = list(bf.values()) + list(rf.values())
    v = {
        "same_protocol": len({r["lock_id"] for r in allr}) == 1 and allr[0]["lock_id"] == cond.st.lock_id,
        "same_seed_set": sorted(bf) == sorted(rf) == sorted(seeds),
        "all_runs_valid": all(r["status"] == "VALID" for r in allr),
        "all_checkpoints_present": all(r.get("checkpoint") and r.get("checkpoint_sha256")
                                       and Path(r["checkpoint"]).is_file()
                                       and sha256_file(r["checkpoint"]) == r["checkpoint_sha256"] for r in allr),
        "all_checkpoints_reload_and_rescore_passed": all(
            r.get("rescore_metric") is not None and r.get("metric") is not None
            and abs(r["rescore_metric"] - r["metric"]) <= proto["result"]["rescore_tolerance"] for r in allr),
        "locked_files_unchanged": not cond.lock_mismatches(full=True),
    }
    qb, qr = (quality_gate(proto, bf, seeds), quality_gate(proto, rf, seeds)) if v["all_runs_valid"] else (None, None)
    if proto.get("aggregate_quality_gate"):
        v["all_quality_gates_passed"] = bool(qb and qr and qb["status"] == qr["status"] == "PASS")
    v["no_reference_leak"] = leak_check(cond, proto)["ok"]
    for k, val in proto.get("validity_declarations", {}).items():
        v[k] = bool(val)
    return v


def build_summary(cond, proto: dict, bf: dict, rf: dict, conf: dict) -> dict:
    seeds = conf["seeds"]
    direction = proto["metric"]["direction"]
    U = proto["U"]["value"]
    b = [bf[s]["metric"] for s in seeds]
    r = [rf[s]["metric"] for s in seeds]
    imp = [S.signed_delta(direction, bi, ri) for bi, ri in zip(b, r)]
    B, R = S.mean(b), S.mean(r)
    sig = S.sample_std(b)
    verdict = conf["verdict"]
    st = cond.st
    out = {
        "schema_version": "autoresearch-comparison-summary.v2",
        "status": "COMPLETE",
        "task_id": cond.cfg["task_id"],
        "metric": {"name": proto["metric"]["name"], "direction": direction, "attainable_anchor": U},
        "seeds": seeds,
        "paired_results": {str(s): {"baseline": bi, "reference": ri, "paired_improvement": d}
                           for s, bi, ri, d in zip(seeds, b, r, imp)},
        "statistics": {
            "paired_run_count": len(seeds), "baseline_mean": B, "baseline_sample_std": sig,
            "reference_mean": R, "reference_sample_std": S.sample_std(r),
            "mean_paired_improvement": S.mean(imp), "paired_improvement_sample_std": S.sample_std(imp),
            "relative_improvement": S.signed_delta(direction, B, R) / abs(B) if B else None,
            "baseline_sigma_multiple": verdict["sigma_multiple"],
        },
        "significance_rule": {"required_baseline_sigma_multiple": 3.0, "passed": verdict["passed"],
                              "label": verdict["label"]},
        "normalized_score": {"baseline": 0.0, "reference": verdict["score"], "formula": proto["metric"]["score_formula"]},
        "validity": conf["validity"],
        "source_results": {
            "baseline": "g03/runs/<request_id>/receipt.json (kind=formal_b)",
            "reference": "g03/runs/<request_id>/receipt.json (kind=formal_r)",
            "baseline_runs": {str(s): bf[s]["run_dir"] for s in seeds},
            "reference_runs": {str(s): rf[s]["run_dir"] for s in seeds},
        },
    }
    qb, qr = quality_gate(proto, bf, seeds), quality_gate(proto, rf, seeds)
    if qb is not None:
        out["aggregate_quality_gate"] = {"rule": proto["aggregate_quality_gate"].get("rule", ""),
                                         "checks": proto["aggregate_quality_gate"]["checks"],
                                         "baseline": qb, "reference": qr}
    out["g03_conductor"] = {
        "version": cond.cfg["version"],
        "verdict": verdict,
        "window": st.formal_b_stats["window"] if st.formal_b_stats else None,
        "lock_id": st.lock_id,
        "frozen_candidate": {k: st.frozen[k] for k in ("version", "content_hash", "frozen_path")},
        "protocol_versions": [{"major": d["major"], "draft_id": d["draft_id"], "sha": d["sha"], "path": d["path"]}
                              for d in st.drafts],
        "confirmation_attempts": [{"seq": c["seq"], "k": c["k"], "version": c["version"], "group_idx": c["group_idx"],
                                   "seeds": c["seeds"], "major": c["major"], "invalid": c["invalid"],
                                   "passed": bool(c["verdict"] and c["verdict"]["passed"]),
                                   "voided": c["seq"] in st.voided_confirms} for c in st.confirms],
        "failures_and_rollbacks": [{"seq": f["seq"], "class": f["class"], "reason": f["reason"], "major": f["major"]}
                                   for f in st.failures],
        "revisions": [{"seq": v["seq"], "class": v["class"], "from_major": v["from_major"], "to_major": v["to_major"],
                       "reason": v["reason"]} for v in st.revisions],
        "rejected_candidates": {k: v["reason"] for k, v in st.rejected.items()},
        "deterministic_signoff": st.signed_off("deterministic", lock_id=st.lock_id, group_idx=conf["group_idx"]),
        "not_best_method": "专家侧 G02/G03 证据；不能作为 best_method 使用",
    }
    return out
