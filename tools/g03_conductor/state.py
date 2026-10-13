"""Ledger replay: the ledger is the only source of truth; state is always rebuilt from it."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

STAGES = ("S0", "S1", "S1_5", "S2", "S3", "S4", "S5", "S6", "DONE", "STOP_UNMET")
TERMINAL = ("DONE", "STOP_UNMET")
TRANSITIONS = {
    "S0": {"S1", "STOP_UNMET"},
    "S1": {"S1_5", "S0", "STOP_UNMET"},
    "S1_5": {"S2", "S0", "S1", "STOP_UNMET"},
    "S2": {"S3", "S0", "S1", "STOP_UNMET"},
    "S3": {"S4", "S0", "S1", "STOP_UNMET"},
    "S4": {"S5", "S0", "S1", "STOP_UNMET"},
    "S5": {"S6", "S1", "STOP_UNMET"},
    "S6": {"DONE", "S3", "S0", "S1", "STOP_UNMET"},
    "DONE": set(),
    "STOP_UNMET": set(),
}
# Conductor tools allowed per stage, with the permitted sub-kinds. Phase transitions have no tool.
ALLOWED = {
    "S0": {"g03_status": None, "g03_note": None, "g03_propose": {"protocol"}},
    "S1": {"g03_status": None, "g03_note": None, "g03_propose": {"protocol"}, "g03_run": {"diag"}},
    "S1_5": {"g03_status": None, "g03_note": None, "g03_propose": {"protocol", "candidate", "infeasible", "defect"},
             "g03_run": {"dev"}},
    "S2": {"g03_status": None, "g03_note": None},
    "S3": {"g03_status": None, "g03_note": None, "g03_propose": {"defect"}, "g03_run": {"formal_b"}},
    "S4": {"g03_status": None, "g03_note": None, "g03_propose": {"candidate", "infeasible", "defect"},
           "g03_run": {"screen"}},
    "S5": {"g03_status": None, "g03_note": None, "g03_run": {"formal_r"}},
    "S6": {"g03_status": None, "g03_note": None},
    "DONE": {"g03_status": None},
    "STOP_UNMET": {"g03_status": None},
}
PROGRESS_FREE = {"nudge", "gpu_wait", "review_request"}


class IllegalTransition(RuntimeError):
    pass


def allowed(stage: str, tool: str, kind: str | None = None) -> tuple[bool, str]:
    table = ALLOWED.get(stage, {})
    if tool not in table:
        return False, f"{tool} is not allowed in stage {stage}"
    kinds = table[tool]
    if kinds is not None and kind not in kinds:
        return False, f"{tool}(kind={kind}) is not allowed in stage {stage}; allowed kinds: {sorted(kinds)}"
    return True, "ok"


@dataclass
class State:
    config: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    stage: str = "S0"
    stage_since: float = 0.0
    terminal: dict | None = None
    paused: dict | None = None
    blocked: dict | None = None
    major: int = 1
    draft_n: int = 0
    draft: dict | None = None
    draft_sha: str | None = None
    draft_id: str | None = None
    draft_major: int = 0
    drafts: list = field(default_factory=list)
    pending_c: dict | None = None
    last_revision: str | None = None
    rev_c: int = 0
    rev_b: int = 0
    baseline_fixed: str | None = None
    lock: dict | None = None
    lock_id: str | None = None
    group: dict | None = None
    consumed_seeds: set = field(default_factory=set)
    formal_b_stats: dict | None = None
    attempts_a: int = 0
    candidates: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)
    frozen: dict | None = None
    runs: dict = field(default_factory=dict)
    run_starts: dict = field(default_factory=dict)
    gpu_waits: dict = field(default_factory=dict)
    invalidated: set = field(default_factory=set)
    voided_confirms: set = field(default_factory=set)
    reviews: list = field(default_factory=list)
    review_requests: list = field(default_factory=list)
    signoffs: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    confirms: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    revisions: list = field(default_factory=list)
    claims: dict = field(default_factory=dict)
    lock_violations: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    control_seq: int = 0
    nudges: int = 0
    stage_nudges: int = 0
    gpu_seconds: float = 0.0
    summary: dict | None = None

    # -- derived helpers -------------------------------------------------
    @property
    def task_id(self) -> str:
        return self.config["task_id"]

    def valid_runs(self, **match) -> list[dict]:
        out = []
        for rid, r in self.runs.items():
            if rid in self.invalidated:
                continue
            if all(r.get(k) == v for k, v in match.items()):
                out.append(r)
        return out

    def by_seed(self, runs: list[dict]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for r in sorted(runs, key=lambda r: r["seq"]):
            out[r["seed"]] = r
        return out

    def open_claims(self) -> list[dict]:
        return [c for c in self.claims.values() if c.get("outcome") is None and c["major"] == self.major]

    def signed_off(self, kind: str, **scope) -> dict | None:
        for s in reversed(self.signoffs):
            if s["kind"] == kind and all(s.get(k) == v for k, v in scope.items()):
                return s
        return None

    def side(self) -> str | None:
        if self.paused:
            return "PAUSED"
        if self.blocked:
            return "BLOCKED_INFRA"
        for rid, start in self.run_starts.items():
            if rid not in self.runs and self.gpu_waits.get(rid) in ("QUEUED", "STARTING"):
                return "WAITING_GPU"
        return None


def replay(records: list[dict]) -> State:
    st = State(events=records)
    for rec in records:
        t = rec["type"]
        if t == "init":
            st.config = rec["config"]
            st.stage_since = rec["ts"]
        elif t == "draft":
            st.draft = rec["proto"]
            st.draft_sha = rec["sha"]
            st.draft_id = rec["draft_id"]
            st.draft_n = rec["n"]
            st.draft_major = rec["major"]
            st.drafts.append({k: rec[k] for k in ("draft_id", "sha", "path", "major", "n", "stage", "at")})
        elif t == "transition":
            st.stage_nudges = 0
            st.stage = rec["to"]
            st.stage_since = rec["ts"]
            st.transitions.append(rec)
        elif t == "baseline_fixed":
            st.baseline_fixed = rec["content_hash"]
        elif t == "candidate":
            st.candidates[rec["version"]] = rec
        elif t == "run_start":
            st.run_starts[rec["request_id"]] = rec
        elif t == "gpu_wait":
            st.gpu_waits[rec["request_id"]] = rec["state"]
        elif t == "infra":
            st.blocked = {"request_id": rec["request_id"], "state": rec["state"], "detail": rec["detail"], "at": rec["at"]}
        elif t == "run":
            st.runs[rec["request_id"]] = rec
            st.gpu_seconds += float(rec.get("elapsed") or 0)
        elif t == "review_request":
            st.review_requests.append(rec)
        elif t == "review":
            st.reviews.append(rec)
        elif t == "lock":
            st.lock = rec["lock"]
            st.lock_id = rec["lock_id"]
        elif t == "lock_violation":
            st.lock_violations.append(rec)
        elif t == "group_start":
            st.group = {"major": rec["major"], "idx": rec["idx"], "seeds": rec["seeds"], "closed": False,
                        "lock_id": rec["lock_id"]}
            st.consumed_seeds |= set(rec["seeds"])
            st.formal_b_stats = None
        elif t == "formal_b_stats":
            st.formal_b_stats = rec
        elif t == "freeze":
            st.frozen = rec
        elif t == "reject":
            st.rejected[rec["version"]] = rec
        elif t == "confirm":
            st.confirms.append(rec)
            if st.group is not None:
                st.group["closed"] = True
        elif t == "failure":
            st.failures.append(rec)
            if rec["class"] == "A":
                st.attempts_a += 1
        elif t == "revision":
            st.revisions.append(rec)
            st.last_revision = rec["class"]
            st.major = rec["to_major"]
            st.baseline_fixed = None
            st.lock = None
            st.lock_id = None
            st.frozen = None
            st.formal_b_stats = None
            if st.group is not None:
                st.group["closed"] = True
            if rec["class"] == "C":
                st.rev_c += 1
                st.pending_c = rec["pending"]
                st.draft = None
                st.draft_sha = None
                st.draft_id = None
            else:
                st.rev_b += 1
        elif t == "revision_c_approved":
            st.pending_c = None
        elif t == "invalidate":
            st.invalidated |= set(rec["request_ids"])
            # Type-A failures measured under an invalidated (buggy) protocol no longer count toward k.
            voided = set(rec.get("voided_confirms", []))
            st.voided_confirms |= voided
            if voided:
                st.attempts_a -= sum(1 for f in st.failures if f["class"] == "A" and f.get("confirm_seq") in voided
                                     and not f.get("voided"))
                for f in st.failures:
                    if f.get("confirm_seq") in voided:
                        f["voided"] = True
        elif t == "claim":
            st.claims[rec["claim_id"]] = dict(rec, outcome=None)
        elif t == "claim_resolved":
            if rec["claim_id"] in st.claims:
                st.claims[rec["claim_id"]]["outcome"] = rec["outcome"]
        elif t == "note":
            st.notes.append(rec)
        elif t == "signoff":
            st.signoffs.append(rec)
        elif t == "control":
            st.control_seq = max(st.control_seq, rec["cseq"])
            if rec["action"] == "pause":
                st.paused = {"reason": rec.get("reason") or "user_pause", "at": rec["at"]}
            elif rec["action"] == "resume":
                st.paused = None
                st.blocked = None
        elif t == "pause":
            st.paused = {"reason": rec["reason"], "at": rec["at"]}
        elif t == "summary":
            st.summary = rec
        elif t == "terminal":
            st.terminal = rec
            st.stage = rec["status"]
        if t == "nudge":
            st.nudges += 1
            st.stage_nudges += 1
        elif t not in PROGRESS_FREE:
            st.nudges = 0
    return st


def g03_dir(root: str | Path) -> Path:
    return Path(root).resolve() / "g03"
