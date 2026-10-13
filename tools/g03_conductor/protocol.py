"""protocol.yaml schema, validation and deterministic revision rules (design §5, §7)."""
from __future__ import annotations

import copy
import math
import re
import subprocess
from pathlib import Path

from . import yamlish
from .hashing import sha256_obj, tree_hash

DIRECTIONS = ("maximize", "minimize")
TOP_KEYS = {
    "task_id", "metric", "U", "margin_sigma", "K", "max_protocol_revisions", "seeds", "budget_per_run",
    "budget_total", "data", "paths", "commands", "result", "diag_budget_multipliers",
    "aggregate_quality_gate", "gpu_spec_template", "validity_declarations", "notes",
}
REQUIRED = ("task_id", "metric", "U", "seeds", "budget_per_run", "data", "paths", "commands")
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
STATIC_PLACEHOLDERS = {"python", "seed", "kind", "method", "method_dir", "out_dir", "score_out", "checkpoint",
                       "private", "starter", "baseline", "run_dir"}
GPU_SPEC_KEYS = {"owner", "job_class", "memory_mib", "compute_units", "cpu_cores", "ram_mib",
                 "memory_max_mib", "memory_high_mib"}
QUALITY_OPS = {">=", "<=", ">", "<"}
MAX_GPU_RUNTIME = 43200
# Fields a draft may change while the protocol is still open (design §4 S1 / S1_5).
S1_MUTABLE = {"budget_per_run", "commands", "result", "diag_budget_multipliers", "data", "gpu_spec_template",
              "notes", "validity_declarations"}
S1_5_MUTABLE = {"budget_per_run", "notes"}


class ProtocolError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_text(text: str) -> dict:
    try:
        data = yamlish.loads(text)
    except yamlish.YamlError as exc:
        raise ProtocolError([f"parse: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ProtocolError(["protocol must be a mapping"])
    return data


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def resolve(root: Path, value: str) -> Path:
    p = Path(value)
    return (p if p.is_absolute() else root / p).resolve()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def git_ignored(path: Path) -> bool | None:
    """True/False when path is inside a git work tree, None otherwise."""
    probe = path if path.is_dir() else path.parent
    try:
        top = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if top.returncode != 0:
        return None
    res = subprocess.run(["git", "-C", top.stdout.strip(), "check-ignore", "-q", str(path)],
                         capture_output=True, timeout=10)
    return res.returncode == 0


def scorer_paths(proto: dict) -> list[str]:
    s = proto["paths"].get("scorer")
    return [s] if isinstance(s, str) else list(s or [])


def all_formal_seeds(proto: dict) -> set[int]:
    return {s for group in proto["seeds"]["formal_pool"] for s in group}


def validate(proto: dict, root: Path, *, task_id: str, budget_total: dict, executor: str,
             check_files: bool = True) -> dict:
    """Return a normalized copy or raise ProtocolError with every violation found."""
    p = copy.deepcopy(proto)
    errors: list[str] = []
    unknown = set(p) - TOP_KEYS
    if unknown:
        errors.append(f"unknown top-level keys: {sorted(unknown)}")
    for key in REQUIRED:
        if key not in p:
            errors.append(f"missing required field: {key}")
    if errors:
        raise ProtocolError(errors)

    if p["task_id"] != task_id:
        errors.append(f"task_id {p['task_id']!r} != authorized task {task_id!r}")

    m = p["metric"]
    if not isinstance(m, dict) or not isinstance(m.get("name"), str) or m.get("direction") not in DIRECTIONS:
        errors.append("metric needs name and direction in {maximize, minimize}")
    else:
        m.setdefault("score_formula", "(value - B) / (U - B)")

    U = p["U"]
    if not isinstance(U, dict) or not _num(U.get("value")) or not isinstance(U.get("basis"), str) or not U["basis"].strip():
        errors.append("U needs numeric value and non-empty ex-ante basis")

    p.setdefault("margin_sigma", 1)
    p.setdefault("K", 2)
    p.setdefault("max_protocol_revisions", 1)
    if not _num(p["margin_sigma"]) or p["margin_sigma"] < 0:
        errors.append("margin_sigma must be >= 0")
    if not isinstance(p["K"], int) or p["K"] < 1:
        errors.append("K must be an integer >= 1")
    if p["max_protocol_revisions"] != 1:
        errors.append("max_protocol_revisions is fixed at 1")

    seeds = p["seeds"]
    if not isinstance(seeds, dict) or set(seeds) != {"dev", "screen", "formal_pool"}:
        errors.append("seeds must contain exactly dev, screen, formal_pool")
    else:
        def ints(xs):
            return isinstance(xs, list) and xs and all(isinstance(x, int) and not isinstance(x, bool) for x in xs)
        dev, screen, pool = seeds["dev"], seeds["screen"], seeds["formal_pool"]
        if not ints(dev) or len(dev) < 3:
            errors.append("seeds.dev needs >= 3 integer seeds")
        if not ints(screen) or len(screen) < 3:
            errors.append("seeds.screen needs >= 3 integer seeds")
        if not isinstance(pool, list) or not pool or not all(ints(g) for g in pool):
            errors.append("seeds.formal_pool must be a list of integer seed groups")
        else:
            n = len(pool[0])
            if any(len(g) != n for g in pool):
                errors.append("all formal_pool groups must have the same size n")
            if n < 3:
                errors.append("formal group size n must be >= 3 (default 5)")
            if isinstance(p["K"], int) and len(pool) < p["K"]:
                errors.append(f"formal_pool needs at least K={p['K']} groups")
            flat = [s for g in pool for s in g]
            if len(flat) != len(set(flat)):
                errors.append("formal_pool seeds must be unique across groups")
            if ints(dev) and ints(screen):
                for a, b, name in ((dev, screen, "dev/screen"), (dev, flat, "dev/formal"), (screen, flat, "screen/formal")):
                    if set(a) & set(b):
                        errors.append(f"seed sets overlap: {name} {sorted(set(a) & set(b))}")
                if len(set(dev)) != len(dev) or len(set(screen)) != len(screen):
                    errors.append("dev/screen seeds must be unique")

    b = p["budget_per_run"]
    if not isinstance(b, dict) or not all(_num(v) for v in b.values()):
        errors.append("budget_per_run must be a mapping of numeric values")
    elif not isinstance(b.get("max_runtime_seconds"), int) or b["max_runtime_seconds"] <= 0:
        errors.append("budget_per_run.max_runtime_seconds must be a positive integer")
    elif executor == "gpu_scheduler" and b["max_runtime_seconds"] > MAX_GPU_RUNTIME:
        errors.append(f"max_runtime_seconds exceeds gpu_scheduler limit {MAX_GPU_RUNTIME}")

    if "budget_total" in p and p["budget_total"] != budget_total:
        errors.append("budget_total must equal the launch authorization (it cannot be changed by the protocol)")
    p["budget_total"] = budget_total

    mults = p.setdefault("diag_budget_multipliers", [2])
    if not isinstance(mults, list) or not all(_num(x) and x > 1 for x in mults):
        errors.append("diag_budget_multipliers must be numbers > 1")

    res = p.setdefault("result", {})
    res.setdefault("metric_key", "metric")
    res.setdefault("rescore_tolerance", 1e-9)
    if not isinstance(res.get("metric_key"), str) or not _num(res.get("rescore_tolerance")) or res["rescore_tolerance"] < 0:
        errors.append("result.metric_key must be str and rescore_tolerance >= 0")

    paths = p["paths"]
    if not isinstance(paths, dict) or not all(k in paths for k in ("private", "starter", "scorer", "baseline")):
        errors.append("paths needs private, starter, scorer, baseline")
        paths = None
    data = p["data"]
    if not isinstance(data, dict) or not data:
        errors.append("data must map names to {path, sha256}")
        data = None
    elif not all(isinstance(v, dict) and isinstance(v.get("path"), str) and isinstance(v.get("sha256"), str)
                 and re.fullmatch(r"[0-9a-f]{64}", v["sha256"]) for v in data.values()):
        errors.append("each data entry needs path and 64-hex sha256")
        data = None

    names = set()
    if paths is not None:
        private = resolve(root, paths["private"])
        if private != root.resolve():
            errors.append(f"paths.private must be the conductor root {root.resolve()}")
        starter = resolve(root, paths["starter"])
        if _inside(private, starter) or _inside(starter, private):
            errors.append("private area and starter must not contain each other")
        if check_files:
            ign = git_ignored(private)
            if ign is False:
                errors.append("private area is inside a git work tree but not ignored")
            for label, value in [("starter", paths["starter"]), ("baseline", paths["baseline"])] + \
                    [("scorer", s) for s in scorer_paths(p)]:
                if not resolve(root, value).exists():
                    errors.append(f"paths.{label} does not exist: {value}")
        names = {f"path.{k}" for k in paths if isinstance(paths[k], str)}
    if data is not None:
        names |= {f"data.{k}" for k in data}
        if check_files:
            for k, v in data.items():
                f = resolve(root, v["path"])
                if not f.exists():
                    errors.append(f"data.{k} missing: {v['path']}")
                elif tree_hash(f) != v["sha256"]:
                    errors.append(f"data.{k} sha256 mismatch")
    if isinstance(b, dict):
        names |= {f"budget.{k}" for k in b}

    cmds = p["commands"]
    if not isinstance(cmds, dict) or not all(isinstance(cmds.get(k), list) and cmds[k] and all(isinstance(x, str) for x in cmds[k])
                                             for k in ("train", "score")) or not isinstance(cmds.get("checkpoint"), str):
        errors.append("commands needs train/score argv lists and checkpoint glob")
    else:
        allowed = STATIC_PLACEHOLDERS | names
        for key in ("train", "score"):
            for arg in cmds[key]:
                for ph in PLACEHOLDER.findall(arg):
                    if ph not in allowed:
                        errors.append(f"commands.{key}: unknown placeholder {{{ph}}}")
        if not any("{out_dir}" in a for a in cmds["train"]):
            errors.append("commands.train must reference {out_dir}")
        if not any("{score_out}" in a for a in cmds["score"]) or not any("{checkpoint}" in a for a in cmds["score"]):
            errors.append("commands.score must reference {checkpoint} and {score_out}")
        if cmds["checkpoint"].startswith("/") or ".." in Path(cmds["checkpoint"]).parts:
            errors.append("commands.checkpoint must be a glob relative to {out_dir}")

    gate = p.get("aggregate_quality_gate")
    if gate is not None:
        if not isinstance(gate, dict) or not isinstance(gate.get("checks"), list) or not gate["checks"]:
            errors.append("aggregate_quality_gate needs a non-empty checks list")
        else:
            for c in gate["checks"]:
                if not isinstance(c, dict) or not isinstance(c.get("field"), str) or c.get("op") not in QUALITY_OPS or not _num(c.get("value")):
                    errors.append(f"bad aggregate_quality_gate check: {c!r}")

    if executor == "gpu_scheduler":
        g = p.get("gpu_spec_template")
        if not isinstance(g, dict) or set(g) - GPU_SPEC_KEYS or not {"owner", "job_class", "memory_mib", "compute_units"} <= set(g):
            errors.append(f"gpu_spec_template needs owner/job_class/memory_mib/compute_units (allowed: {sorted(GPU_SPEC_KEYS)})")

    vd = p.setdefault("validity_declarations", {})
    if not isinstance(vd, dict) or not all(isinstance(v, bool) for v in vd.values()):
        errors.append("validity_declarations must map names to booleans")

    if errors:
        raise ProtocolError(errors)
    return p


def norm_mult(multiplier) -> int | float:
    m = float(multiplier)
    return int(m) if m.is_integer() else m


def budget_hash(proto: dict, multiplier: float = 1) -> str:
    return sha256_obj({"budget": proto["budget_per_run"], "x": norm_mult(multiplier)})


def exec_hash(proto: dict) -> str:
    return sha256_obj({k: proto.get(k) for k in ("commands", "result", "data", "metric")} | {"scorer": scorer_paths(proto)})


def _diff_keys(old: dict, new: dict) -> set[str]:
    keys = set(old) | set(new)
    return {k for k in keys if old.get(k) != new.get(k)}


def check_draft_change(old: dict, new: dict, stage: str) -> list[str]:
    """Rules for re-drafting inside an open protocol (S1, S1_5)."""
    if stage == "S0":
        return []
    mutable = S1_MUTABLE if stage == "S1" else S1_5_MUTABLE
    errors = [f"{stage} may not change {k}" for k in sorted(_diff_keys(old, new) - mutable)]
    errors += budget_not_weakened(old, new)
    if stage == "S1" and old["paths"]["baseline"] != new["paths"]["baseline"]:
        errors.append("baseline path cannot change")
    return errors


def budget_not_weakened(old: dict, new: dict) -> list[str]:
    errors = []
    ob, nb = old["budget_per_run"], new["budget_per_run"]
    for k, v in ob.items():
        if k not in nb:
            errors.append(f"budget_per_run.{k} removed")
        elif nb[k] < v:
            errors.append(f"budget_per_run.{k} decreased {v} -> {nb[k]} (Baseline may not be weakened)")
    return errors


def check_revision_c(old: dict, new: dict, *, observed_R: list[float], used_formal: set[int],
                     baseline_hash_old: str, baseline_hash_new: str) -> list[str]:
    """Deterministic gate for the single type-C protocol revision (design §7)."""
    errors = budget_not_weakened(old, new)
    if old["paths"]["baseline"] != new["paths"]["baseline"] or baseline_hash_old != baseline_hash_new:
        errors.append("Baseline implementation must not change in a type-C revision")
    if old["metric"]["name"] != new["metric"]["name"] or old["metric"]["direction"] != new["metric"]["direction"]:
        errors.append("metric name/direction must not change")
    if new["K"] > old["K"]:
        errors.append("K may not increase")
    if new["margin_sigma"] < old["margin_sigma"]:
        errors.append("margin_sigma may not decrease")
    u_old, u_new = old["U"]["value"], new["U"]["value"]
    if u_new != u_old:
        if new["U"]["basis"].strip() == old["U"]["basis"].strip():
            errors.append("U changed without a new ex-ante basis")
        for r in observed_R:
            if abs(u_new - r) < abs(u_old - r):
                errors.append(f"U moved toward observed R={r:.6g}")
                break
    new_formal = all_formal_seeds(new)
    reused = new_formal & (used_formal | all_formal_seeds(old))
    if reused:
        errors.append(f"formal seeds must be new: reused {sorted(reused)}")
    return errors


def check_revision_b(old: dict, new: dict) -> list[str]:
    """After an implementation-defect revision only the implementation side may move."""
    errors = budget_not_weakened(old, new)
    for k in ("metric", "U", "K", "margin_sigma", "max_protocol_revisions"):
        if old.get(k) != new.get(k):
            errors.append(f"type-B revision may not change {k}")
    return errors
