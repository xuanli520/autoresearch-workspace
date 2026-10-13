"""Conductor core: init, ledger access, transitions, control, budget, proposals (design §4, §5, §7)."""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import protocol as P
from .hashing import sha256_file, sha256_obj, tree_hash
from .ledger import Ledger, exclusive, now
from .state import ALLOWED, TERMINAL, TRANSITIONS, IllegalTransition, State, allowed, g03_dir, replay

VERSION = "g03-conductor.v1"
MAX_B_REVISIONS = 5
REPO = Path(__file__).resolve().parents[2]
DEFAULT_REVIEWER_AGENT = Path.home() / ".pi/agent/agents/g03-reviewer.md"
SUBDIRS = ("drafts", "locks", "candidates", "frozen", "runs", "reviews", "notes", "summary", "final")


class G03Error(RuntimeError):
    def __init__(self, message: str, code: str = "REJECTED", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra

    def as_json(self) -> dict:
        return {"ok": False, "code": self.code, "error": str(self), **self.extra}


def _parse_deadline(value: str) -> float:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise G03Error(f"deadline must be ISO8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise G03Error("deadline must include a timezone offset")
    return dt.timestamp()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    meta: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body


def init(root: str | Path, *, task_id: str, gpu_hours: float, deadline: str, executor: str,
         scheduler_root: str | None = None, python: str | None = None, reviewer_agent: str | None = None,
         reviewer_cmd: list[str] | None = None, pi_path: str | None = None, max_idle_nudges: int = 6,
         authorization: str = "") -> dict:
    root = Path(root).resolve()
    d = g03_dir(root)
    led = Ledger(d / "ledger.jsonl")
    if led.exists():
        raise G03Error(f"already initialized: {led.path}", code="EXISTS")
    if not root.is_dir():
        raise G03Error(f"private root does not exist: {root}")
    deadline_epoch = _parse_deadline(deadline)
    if deadline_epoch <= time.time():
        raise G03Error("deadline is in the past")
    if not (gpu_hours > 0):
        raise G03Error("gpu_hours must be > 0")
    if executor not in ("local_cpu", "gpu_scheduler"):
        raise G03Error("executor must be local_cpu or gpu_scheduler")
    exe = {"kind": executor, "python": python or ("/usr/bin/python3" if executor == "gpu_scheduler" else None)}
    if executor == "gpu_scheduler":
        if not scheduler_root or not Path(scheduler_root).is_dir():
            raise G03Error("--scheduler-root must be an existing directory for gpu_scheduler")
        exe["scheduler_root"] = str(Path(scheduler_root).resolve())
    agent = Path(reviewer_agent).expanduser().resolve() if reviewer_agent else DEFAULT_REVIEWER_AGENT
    if not agent.is_file():
        raise G03Error(f"reviewer agent file not found: {agent}")
    meta, _ = _frontmatter(agent)
    reviewer = {"agent_file": str(agent), "agent_sha256": sha256_file(agent), "model": meta.get("model"),
                "tools": meta.get("tools", "read,grep,find,ls,bash").replace(" ", ""), "argv": reviewer_cmd,
                "pi_path": pi_path or shutil.which("pi"), "timeout_seconds": 3600}
    if reviewer_cmd is None and not reviewer["pi_path"]:
        raise G03Error("pi not found on PATH; pass --pi-path or --reviewer-cmd")
    for sub in SUBDIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)
    config = {"task_id": task_id, "root": str(root), "version": VERSION,
              "budget_total": {"gpu_hours": float(gpu_hours), "deadline_at": deadline, "deadline_epoch": deadline_epoch},
              "executor": exe, "reviewer": reviewer, "max_idle_nudges": int(max_idle_nudges),
              "authorization": authorization}
    led.append("init", config=config)
    led.append("transition", **{"from": None, "to": "S0", "reason": "init", "evidence": []})
    return {"ok": True, "root": str(root), "ledger": str(led.path), "stage": "S0"}


class Core:
    """Ledger, state and transition plumbing shared by every mixin."""

    def __init__(self, root: str | Path, *, executor_factory=None):
        self.root = Path(root).resolve()
        self.dir = g03_dir(self.root)
        self.ledger = Ledger(self.dir / "ledger.jsonl")
        if not self.ledger.exists():
            raise G03Error(f"not initialized: {self.ledger.path}", code="NOT_INITIALIZED")
        self.executor_factory = executor_factory
        self.st: State = replay(self.ledger.read())

    # -- ledger --------------------------------------------------------
    def reload(self) -> State:
        self.st = replay(self.ledger.read())
        return self.st

    def log(self, type_: str, **fields) -> dict:
        self.ledger.append(type_, **fields)
        self.reload()
        # Return the replayed record so callers see exactly what the ledger holds (key order included).
        return self.st.events[-1]

    def locked(self):
        return exclusive(self.dir / ".conductor.lock")

    def transition(self, to: str, reason: str, evidence: list | None = None) -> None:
        frm = self.st.stage
        if to not in TRANSITIONS.get(frm, set()):
            raise IllegalTransition(f"illegal transition {frm} -> {to}")
        self.log("transition", **{"from": frm, "to": to, "reason": reason, "evidence": evidence or []})

    def terminate(self, status: str, reason: str, **extra) -> None:
        if self.st.terminal:
            return
        if status not in TERMINAL:
            raise IllegalTransition(f"{status} is not terminal")
        if self.st.stage not in TERMINAL:
            self.transition(status, reason)
        final = {"status": status, "reason": reason, "stage_before": self.st.transitions[-1]["from"],
                 "attempts_a": self.st.attempts_a, "rev_c": self.st.rev_c, "rev_b": self.st.rev_b,
                 "gpu_hours_used": self.st.gpu_seconds / 3600, **extra}
        _atomic_write(self.dir / "final" / "final_record.json", json.dumps(final, indent=1, ensure_ascii=False))
        self.log("terminal", **final)

    # -- protocol helpers ----------------------------------------------
    @property
    def cfg(self) -> dict:
        return self.st.config

    def proto(self) -> dict:
        if self.st.lock and self.st.lock["major"] == self.st.major:
            return self.st.lock["proto"]
        if self.st.draft is None:
            raise G03Error("no protocol draft yet", code="NO_PROTOCOL")
        return self.st.draft

    def rpath(self, value: str) -> Path:
        return P.resolve(self.root, value)

    def baseline_dir(self) -> Path:
        return self.rpath(self.proto()["paths"]["baseline"])

    def candidate_dir(self, version: str) -> Path:
        return self.dir / "candidates" / version

    # -- control and budget --------------------------------------------
    def apply_control(self) -> None:
        path = self.dir / "control.json"
        if not path.exists():
            return
        try:
            ctl = json.loads(path.read_text())
        except ValueError:
            return
        seq = ctl.get("seq", 0)
        if not isinstance(seq, int) or seq <= self.st.control_seq or ctl.get("action") not in ("pause", "resume", "stop"):
            return
        self.log("control", cseq=seq, action=ctl["action"], reason=ctl.get("reason"))
        if ctl["action"] == "stop":
            self.terminate("STOP_UNMET", "user_stop", detail=ctl.get("reason"))

    def budget_stop(self) -> str | None:
        total = self.cfg["budget_total"]
        if self.st.gpu_seconds / 3600 >= total["gpu_hours"]:
            return "gpu_budget_exhausted"
        if time.time() >= total["deadline_epoch"]:
            return "deadline_reached"
        return None

    def budget_allows(self, seconds: float) -> str | None:
        total = self.cfg["budget_total"]
        if (self.st.gpu_seconds + seconds) / 3600 > total["gpu_hours"]:
            return "gpu_budget_exhausted"
        if time.time() + seconds > total["deadline_epoch"]:
            return "deadline_reached"
        return None


def write_control(root: str | Path, action: str, reason: str | None = None) -> dict:
    """UI-side control write (/g03 pause|resume|stop). Takes effect at the next gate; never edits protocol."""
    if action not in ("pause", "resume", "stop"):
        raise G03Error("action must be pause, resume or stop")
    d = g03_dir(root)
    path = d / "control.json"
    seq = 0
    if path.exists():
        try:
            seq = int(json.loads(path.read_text()).get("seq", 0))
        except (ValueError, TypeError):
            seq = 0
    ctl = {"seq": seq + 1, "action": action, "reason": reason, "at": now()}
    _atomic_write(path, json.dumps(ctl))
    return {"ok": True, "control": ctl}


class Proposals:
    """g03_propose / g03_note / CLI-only signoff. All writes into the private area happen here."""

    def _check_allowed(self, tool: str, kind: str | None) -> None:
        ok, why = allowed(self.st.stage, tool, kind)
        if not ok:
            raise G03Error(why, code="STAGE_FORBIDDEN", stage=self.st.stage)
        if self.st.paused:
            raise G03Error(f"paused: {self.st.paused['reason']}", code="PAUSED")

    def propose(self, kind: str, *, content: str | None = None, description: str = "",
                source: str | None = None, evidence: list | None = None) -> dict:
        with self.locked():
            self.reload()
            self.apply_control()
            if self.st.terminal:
                raise G03Error("conductor is terminal", code="TERMINAL")
            self._check_allowed("g03_propose", kind)
            if kind == "protocol":
                return self._propose_protocol(content or "")
            if kind == "candidate":
                return self._propose_candidate(description, source)
            return self._propose_claim(kind, description, evidence or [])

    def _propose_protocol(self, text: str) -> dict:
        st = self.st
        try:
            raw = P.parse_text(text)
            new = P.validate(raw, self.root, task_id=self.cfg["task_id"],
                             budget_total={k: self.cfg["budget_total"][k] for k in ("gpu_hours", "deadline_at")},
                             executor=self.cfg["executor"]["kind"])
        except P.ProtocolError as exc:
            raise G03Error("protocol rejected", code="PROTOCOL_INVALID", errors=exc.errors) from exc
        errors: list[str] = []
        if st.stage == "S0" and st.pending_c:
            pend = st.pending_c
            errors = P.check_revision_c(pend["old_proto"], new, observed_R=pend["observed_R"],
                                        used_formal=set(pend["used_formal"]),
                                        baseline_hash_old=pend["baseline_hash"],
                                        baseline_hash_new=tree_hash(self.rpath(new["paths"]["baseline"])))
        elif st.draft is not None and st.stage != "S0":
            errors = P.check_draft_change(st.draft, new, st.stage)
            if st.major > 1 and st.last_revision == "B":
                errors = [e for e in errors if not e.startswith("S1 may not change seeds")]
                errors += P.check_revision_b(st.draft, new)
                old_pool = st.draft["seeds"]["formal_pool"]
                if new["seeds"]["formal_pool"][:len(old_pool)] != old_pool or \
                        new["seeds"]["dev"] != st.draft["seeds"]["dev"] or new["seeds"]["screen"] != st.draft["seeds"]["screen"]:
                    errors.append("after a type-B revision seeds may only append new formal groups")
        if errors:
            raise G03Error("protocol change rejected by deterministic rules", code="RULE_VIOLATION", errors=errors)
        sha = sha256_obj(new)
        if sha == st.draft_sha:
            return {"ok": True, "unchanged": True, "draft_id": st.draft_id, "sha": sha}
        n = st.draft_n + 1
        draft_id = f"p{st.major}d{n}"
        path = self.dir / "drafts" / f"{draft_id}.yaml"
        _atomic_write(path, text if text.endswith("\n") else text + "\n")
        _atomic_write(self.dir / "drafts" / f"{draft_id}.json", json.dumps(new, indent=1, ensure_ascii=False))
        _atomic_write(self.dir / "protocol.yaml", path.read_text())
        self.log("draft", proto=new, sha=sha, draft_id=draft_id, n=n, major=st.major, stage=st.stage,
                 path=str(path.relative_to(self.dir)), file_sha256=sha256_file(path))
        return {"ok": True, "draft_id": draft_id, "sha": sha, "path": str(path)}

    def _propose_candidate(self, description: str, source: str | None) -> dict:
        if not description.strip():
            raise G03Error("candidate needs a description (method hypothesis)")
        nums = [int(v[1:]) for v in self.st.candidates if v[1:].isdigit()]
        version = f"v{max(nums, default=0) + 1}"
        dest = self.candidate_dir(version)
        if dest.exists():
            raise G03Error(f"{dest} already exists", code="EXISTS")
        if source and source != "base":
            if source not in self.st.candidates:
                raise G03Error(f"unknown source candidate {source}")
            src = self.candidate_dir(source)
        else:
            src = self.baseline_dir()
        if src.is_dir():
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            dest.mkdir(parents=True)
            shutil.copy2(src, dest / src.name)
        (dest / "CANDIDATE.md").write_text(f"# {version}\n\n{description.strip()}\n")
        self.log("candidate", version=version, path=str(dest.relative_to(self.dir)), description=description.strip(),
                 source=source or "base", source_hash=tree_hash(src), major=self.st.major)
        return {"ok": True, "version": version, "path": str(dest),
                "hint": "让 g03-worker 在该目录内实现方法，然后调用 g03_run 筛选/预检。"}

    def _propose_claim(self, kind: str, description: str, evidence: list) -> dict:
        if not description.strip():
            raise G03Error("claim needs a description with concrete evidence")
        claim_id = f"{kind}-{self.st.major}-{len(self.st.claims) + 1}"
        self.log("claim", claim_id=claim_id, kind=kind, description=description.strip(), evidence=evidence,
                 major=self.st.major, stage=self.st.stage)
        return {"ok": True, "claim_id": claim_id, "hint": "conductor 将在下一关卡强制 reviewer 审查此声明。"}

    def note(self, tag: str, text: str) -> dict:
        if tag not in ("diagnosis", "asi", "negative", "general"):
            raise G03Error("tag must be diagnosis, asi, negative or general")
        if not text.strip():
            raise G03Error("empty note")
        with self.locked():
            self.reload()
            if self.st.terminal:
                raise G03Error("conductor is terminal", code="TERMINAL")
            self._check_allowed("g03_note", None)
            n = len(self.st.notes) + 1
            path = self.dir / "notes" / f"{n:04d}-{tag}.md"
            _atomic_write(path, text.strip() + "\n")
            self.log("note", tag=tag, path=str(path.relative_to(self.dir)), sha256=sha256_file(path),
                     preview=text.strip()[:200], major=self.st.major, stage=self.st.stage)
            return {"ok": True, "note": str(path)}

    def signoff(self, kind: str, *, expert: str, note: str) -> dict:
        """Expert-only (CLI). DETERMINISTIC sign-off for σ_B = 0 (design §6)."""
        if kind != "deterministic":
            raise G03Error("only 'deterministic' sign-off exists")
        if not expert.strip() or not note.strip():
            raise G03Error("expert name and note are required")
        with self.locked():
            self.reload()
            st = self.st
            if st.stage != "S3" or not st.group or not st.lock_id:
                raise G03Error("deterministic sign-off is only accepted in S3 with an open formal group")
            self.log("signoff", kind=kind, expert=expert.strip(), note=note.strip(), lock_id=st.lock_id,
                     group_idx=st.group["idx"], major=st.major)
            return {"ok": True}
