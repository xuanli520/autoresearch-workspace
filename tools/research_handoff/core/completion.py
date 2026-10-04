"""Trusted scientific completion contracts, signed evidence and read-only checks.

This module belongs on the controller/verifier host, never in a candidate image.
The scheduler interface deliberately implements only GET; it cannot submit jobs.
"""
from __future__ import annotations

import datetime
import contextlib
import contextvars
import base64
import hashlib
import hmac
import json
import math
import os
import re
import socket
import stat
import subprocess
import time
from pathlib import Path

try:
    from .longrun import ControllerError, atomic_json, atomic_write, file_lock, read_json, utc_now
except ImportError:
    from longrun import ControllerError, atomic_json, atomic_write, file_lock, read_json, utc_now

STAGES = frozenset({"diagnostic", "screen", "formal", "final"})
COMPLETION_FAILURES = frozenset({
    "INCOMPLETE_FINAL_SCORE", "EVALUATION_PENDING", "EVALUATION_FAILED",
    "EVALUATION_UNKNOWN", "FINAL_SCORE_INVALID", "CANDIDATE_BINDING_MISMATCH",
    "PROTOCOL_BINDING_MISMATCH", "COMPLETION_RECEIPT_MISSING",
})
JOB_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "EXPIRED"})
CONTRACT_FIELDS = ("stage", "score_expectation", "metric", "direction", "required_seeds",
                   "deadline", "candidate_manifest", "protocol_hash")
HASH = re.compile(r"[a-f0-9]{64}\Z")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HEARTBEAT = contextvars.ContextVar("completion_heartbeat", default=lambda: None)


@contextlib.contextmanager
def evidence_heartbeat(callback):
    token = _HEARTBEAT.set(callback)
    try:
        yield
    finally:
        _HEARTBEAT.reset(token)


class EvidenceError(ControllerError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def fail(status, code, message):
    raise EvidenceError(status, code, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
            _HEARTBEAT.get()()
    return hasher.hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ControllerError("deadline/timestamp must be an explicit timezone-aware ISO timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone missing")
        result = parsed.timestamp()
        if not math.isfinite(result):
            raise ValueError("non-finite timestamp")
        return result
    except (ValueError, OverflowError) as exc:
        raise ControllerError("invalid timezone-aware timestamp") from exc


def score(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        fail("FINAL_SCORE_INVALID", "SCORE_NOT_FINITE", "scientific score must be a finite number")
    return value


def relative(value):
    if (not isinstance(value, str) or not value or "\0" in value or
            Path(value).is_absolute() or ".." in Path(value).parts or value == "." or
            Path(value).as_posix() != value):
        raise ControllerError("evidence paths must be non-empty relative paths without traversal")
    return value


def within(root, value):
    root = Path(root).resolve(strict=True)
    path = root / relative(value)
    if not path.resolve().is_relative_to(root):
        raise ControllerError("evidence path escapes its root")
    current = path
    while current != root:
        if current.is_symlink():
            raise ControllerError("symlinks are not accepted as completion evidence")
        current = current.parent
    return path


def hashed(value, name):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ControllerError(f"{name} must be a lowercase SHA-256")
    return value


def seeds(value, *, required=False):
    if (not isinstance(value, list) or any(not isinstance(v, str) or not LABEL.fullmatch(v) for v in value)
            or len(value) != len(set(value)) or (required and not value)):
        raise ControllerError("required_seeds must contain unique explicit string seed IDs")
    return list(value)


def validate_contract(value):
    if not isinstance(value, dict):
        raise ControllerError("completion contract must be an object")
    missing = set(CONTRACT_FIELDS) - value.keys()
    if missing:
        raise ControllerError(f"explicit completion contract fields missing: {sorted(missing)}")
    if (not isinstance(value["stage"], str) or value["stage"] not in STAGES or
            value["score_expectation"] not in ("required", "not_expected")):
        raise ControllerError("invalid stage or score_expectation")
    if value["score_expectation"] == "not_expected" and value["stage"] != "diagnostic":
        raise ControllerError("only diagnostic may explicitly declare score_expectation=not_expected")
    if not isinstance(value["metric"], str) or not LABEL.fullmatch(value["metric"]):
        raise ControllerError("metric must be an explicit metric name")
    if value["direction"] not in ("min", "max"):
        raise ControllerError("direction must be min or max")
    seeds(value["required_seeds"], required=value["score_expectation"] == "required")
    timestamp(value["deadline"])
    relative(value["candidate_manifest"])
    hashed(value["protocol_hash"], "protocol_hash")
    completion = value.get("completion", {})
    if not isinstance(completion, dict):
        raise ControllerError("completion must be an object")
    required = {"evidence_root", "signing_key", "protocol_manifest", "data_hash", "evaluator_hash",
                "training", "jobs_manifest", "result", "receipt", "isolation", "private_roots"}
    if value["score_expectation"] == "required":
        if set(completion) != required:
            raise ControllerError(f"completion fields must be exactly {sorted(required)}")
        for name in ("evidence_root", "signing_key"):
            if not isinstance(completion[name], str) or not Path(completion[name]).is_absolute():
                raise ControllerError(f"completion.{name} must be absolute")
        for name in ("protocol_manifest", "jobs_manifest", "result", "receipt", "isolation"):
            relative(completion[name])
        if 'completion.pending.json' in {value['candidate_manifest'], *(completion[n] for n in
                ("protocol_manifest", "jobs_manifest", "result", "receipt", "isolation"))}:
            raise ControllerError("completion.pending.json is reserved for progress evidence")
        if any(Path(p).parts[0] == 'completion.late' for p in (value['candidate_manifest'], *(completion[n] for n in
                ("protocol_manifest", "jobs_manifest", "result", "receipt", "isolation")))):
            raise ControllerError("completion.late is reserved for immutable late observations")
        if len({completion[n] for n in ("protocol_manifest", "jobs_manifest", "result", "receipt", "isolation")}
               | {value["candidate_manifest"]}) != 6:
            raise ControllerError("completion evidence files must have distinct paths")
        for name in ("data_hash", "evaluator_hash"):
            hashed(completion[name], name)
        if type(completion["training"]) is not bool:
            raise ControllerError("completion.training must explicitly be boolean")
        if (not isinstance(completion["private_roots"], list) or
                any(not isinstance(p, str) or not Path(p).is_absolute() for p in completion["private_roots"])):
            raise ControllerError("completion.private_roots must list absolute hidden/reference/key roots")
    elif completion:
        raise ControllerError("unscored diagnostics do not accept scientific completion settings")
    return {name: value[name] for name in CONTRACT_FIELDS} | {"completion": completion}


def contract_for_run(config, run_id, deadline=None):
    value = validate_contract(config)
    value.update(version=1, run_id=run_id, task_id=config["task_id"], candidate_root=config["root"],
                 declared_deadline=config["deadline"])
    if deadline is not None:
        if timestamp(deadline) > timestamp(config["deadline"]):
            raise ControllerError("effective deadline cannot exceed the declared deadline")
        value["deadline"] = deadline
    return value


def private_directory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ControllerError("trusted evidence directory must exist and cannot be a symlink")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ControllerError("trusted evidence directory must be controller-owned with mode 0700")
    current = path.resolve()
    while current != current.parent:
        info = current.stat()
        if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and info.st_uid == 0):
            raise ControllerError("trusted evidence has a writable ancestor")
        current = current.parent


def trust_boundary(contract):
    validate_contract(contract)
    if contract["score_expectation"] == "not_expected":
        return
    cfg = contract["completion"]
    public = Path(contract["candidate_root"]).resolve(strict=True)
    evidence = Path(cfg["evidence_root"]).resolve(strict=True)
    key = Path(cfg["signing_key"])
    protected = [evidence, key.resolve(), *(Path(p).resolve() for p in cfg["private_roots"])]
    if any(p.is_relative_to(public) or public.is_relative_to(p) for p in protected):
        raise ControllerError("private evidence, reference assets and keys must be outside the candidate root")
    private_directory(Path(cfg["evidence_root"]))
    private_directory(key.parent)
    if key.is_symlink() or not key.is_file():
        raise ControllerError("completion signing key must be a private regular file")
    info = key.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size < 32:
        raise ControllerError("completion signing key must be owner-only and at least 32 bytes")


def signed(contract, payload):
    trust_boundary(contract)
    value = dict(payload)
    value.pop("signature", None)
    key = Path(contract["completion"]["signing_key"]).read_bytes()
    value["signature"] = {"algorithm": "hmac-sha256", "value": hmac.new(key, canonical(value), hashlib.sha256).hexdigest()}
    return value


def verify_signed(contract, value):
    trust_boundary(contract)
    if not isinstance(value, dict):
        fail("COMPLETION_RECEIPT_MISSING", "UNTRUSTED_EVIDENCE", "trusted evidence must be a signed object")
    sig = value.get("signature", {})
    expected = signed(contract, value)["signature"]
    if (not isinstance(sig, dict) or sig.get("algorithm") != "hmac-sha256" or
            not isinstance(sig.get("value"), str) or not hmac.compare_digest(sig["value"], expected["value"])):
        fail("COMPLETION_RECEIPT_MISSING", "UNTRUSTED_EVIDENCE", "trusted evidence signature is missing or invalid")


def artifact(root, path):
    target = within(root, path)
    if not target.is_file() or target.stat().st_size == 0:
        raise ControllerError("evidence file is missing or empty")
    return {"path": relative(path), "size": target.stat().st_size, "sha256": file_hash(target)}


def verify_artifact(root, item, status, code):
    if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
        fail(status, code, "invalid evidence file descriptor")
    try:
        actual = artifact(root, item["path"])
    except (OSError, ValueError) as exc:
        fail(status, code, "evidence file is missing, empty or unsafe")
    if type(item["size"]) is not int or item["size"] <= 0 or actual != item:
        fail(status, code, "evidence size or SHA-256 does not match")
    return actual


def identity(contract):
    return {name: contract[name] for name in ("run_id", "task_id", "stage", "metric", "direction")}


def check_identity(contract, value):
    if not isinstance(value, dict):
        fail("FINAL_SCORE_INVALID", "RUN_BINDING_MISMATCH", "evaluation must be an object")
    if any(value.get(k) != v for k, v in identity(contract).items()):
        fail("FINAL_SCORE_INVALID", "RUN_BINDING_MISMATCH", "evaluation belongs to a different run, stage or metric")
    if value.get("contract_hash") != digest(contract):
        fail("PROTOCOL_BINDING_MISMATCH", "CONTRACT_BINDING_MISMATCH", "evaluation contract binding differs")


def bindings(contract):
    root = contract["completion"]["evidence_root"]
    try:
        manifest_path = within(root, contract["candidate_manifest"])
        manifest = read_json(manifest_path)
        if (not isinstance(manifest, dict) or set(manifest) != {"version", "source", "models", "checkpoints"}
                or manifest["version"] != 1 or not isinstance(manifest["source"], list) or not manifest["source"]):
            raise ControllerError("invalid candidate manifest")
        for item in manifest["source"]:
            verify_artifact(contract["candidate_root"], item, "CANDIDATE_BINDING_MISMATCH", "SOURCE_HASH_MISMATCH")
        if len({v["path"] for v in manifest["source"]}) != len(manifest["source"]):
            raise ControllerError("duplicate candidate sources")
        for kind in ("models", "checkpoints"):
            items = manifest[kind]
            if not isinstance(items, list):
                raise ControllerError("invalid candidate models/checkpoints")
            if any(not isinstance(v, dict) or set(v) not in ({"seed", "file"}, {"seed", "file", "origin"})
                   or v.get("origin", "candidate") not in ("candidate", "evidence") for v in items):
                raise ControllerError("invalid model/checkpoint descriptor")
            coverage = [v["seed"] for v in items]
            seeds(coverage)
            if contract["completion"]["training"] and set(coverage) != set(contract["required_seeds"]):
                fail("INCOMPLETE_FINAL_SCORE", "MODEL_SEED_MISSING", "model/checkpoint seed coverage is incomplete")
            for item in items:
                origin = root if item.get("origin") == "evidence" else contract["candidate_root"]
                verify_artifact(origin, item["file"], "CANDIDATE_BINDING_MISMATCH", "CHECKPOINT_HASH_MISMATCH")
    except EvidenceError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        fail("CANDIDATE_BINDING_MISMATCH", "CANDIDATE_MANIFEST_INVALID", "candidate manifest is missing or malformed")
    try:
        protocol_path = within(root, contract["completion"]["protocol_manifest"])
        if file_hash(protocol_path) != contract["protocol_hash"]:
            fail("PROTOCOL_BINDING_MISMATCH", "PROTOCOL_HASH_MISMATCH", "current protocol manifest hash differs")
        protocol = read_json(protocol_path)
        if not isinstance(protocol, dict) or set(protocol) != {"version", "protocol", "data", "evaluator"} or protocol["version"] != 1:
            raise ControllerError("invalid protocol manifest")
        for category in ("protocol", "data", "evaluator"):
            if not isinstance(protocol[category], list) or not protocol[category]:
                raise ControllerError("empty protocol/data/evaluator manifest")
            for item in protocol[category]:
                verify_artifact(root, item, "PROTOCOL_BINDING_MISMATCH", "PROTOCOL_ASSET_HASH_MISMATCH")
        if digest(protocol["data"]) != contract["completion"]["data_hash"] or digest(protocol["evaluator"]) != contract["completion"]["evaluator_hash"]:
            fail("PROTOCOL_BINDING_MISMATCH", "VERSION_HASH_MISMATCH", "data/evaluator version binding differs")
    except EvidenceError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        fail("PROTOCOL_BINDING_MISMATCH", "PROTOCOL_MANIFEST_INVALID", "protocol manifest is missing or malformed")
    return {"candidate_manifest_hash": file_hash(manifest_path), "source": manifest["source"],
            "models": manifest["models"], "checkpoints": manifest["checkpoints"],
            "protocol_hash": contract["protocol_hash"], "data_hash": contract["completion"]["data_hash"],
            "evaluator_hash": contract["completion"]["evaluator_hash"]}


def protected_roots(contract, jobs=None):
    """Roots from the signed ledger, or from proposed jobs before they are frozen."""
    cfg = contract["completion"]
    if jobs is None:
        ledger = read_json(within(cfg["evidence_root"], cfg["jobs_manifest"]))
        verify_signed(contract, ledger)
        check_identity(contract, ledger)
        jobs = ledger["jobs"]
    validate_jobs(contract, jobs)
    return sorted({str(Path(p).resolve()) for p in (
        cfg["evidence_root"], str(Path(cfg["signing_key"]).parent), *cfg["private_roots"],
        *(job["scheduler_root"] for job in jobs))})


def attest_docker_isolation(contract, containers, *, docker_host, public_image_digest):
    """Inspect actual solver containers; only store safe IDs/hashes, never raw inspect."""
    trust_boundary(contract)
    if not containers or not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in containers):
        raise ControllerError("isolation requires full actual container IDs")
    if not isinstance(docker_host, str) or not docker_host.startswith("unix://"):
        raise ControllerError("isolation inspections require the local managed Docker socket")
    if not isinstance(public_image_digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", public_image_digest):
        raise ControllerError("pin the previously audited public image digest")
    checked = subprocess.run(["docker", "--host", docker_host, "inspect", *containers],
                             capture_output=True, text=True, timeout=10, check=True)
    inspections = json.loads(checked.stdout)
    if len(inspections) != len(containers) or {v["Id"] for v in inspections} != set(containers):
        raise ControllerError("Docker inspection identity mismatch")
    protected = [Path(p) for p in protected_roots(contract)]
    for item in inspections:
        host = item["HostConfig"]
        if (item["Image"] != public_image_digest or host.get("Privileged") or
                host.get("PidMode") == "host" or host.get("IpcMode") == "host" or
                host.get("NetworkMode") == "host" or host.get("Devices") or
                host.get("SecurityOpt") and any("unconfined" in v for v in host["SecurityOpt"]) or
                set(host.get("CapAdd") or []) & {"ALL", "SYS_ADMIN", "SYS_PTRACE", "NET_ADMIN", "DAC_OVERRIDE", "DAC_READ_SEARCH"}):
            raise ControllerError("candidate image or privilege boundary is unsafe")
        for mount in item.get("Mounts", []):
            source = Path(mount["Source"]).resolve()
            target = Path(mount["Destination"])
            if any(source.is_relative_to(p) or p.is_relative_to(source) for p in protected):
                raise ControllerError("private verifier/reference/key assets are mounted into a candidate")
            if source.suffix == ".sock" or target.suffix == ".sock":
                raise ControllerError("candidate cannot mount host service sockets")
            verifier = Path("/logs/verifier")
            if target == verifier or verifier.is_relative_to(target) or target.is_relative_to(verifier):
                if target not in (verifier / "reward.txt", verifier / "reward.json") or mount.get("RW", True):
                    raise ControllerError("candidate may only read an allowed scalar reward file")
        key = Path(contract["completion"]["signing_key"]).read_bytes()
        for env in item["Config"].get("Env") or []:
            if (any(str(p) in env for p in protected) or key.hex() in env or
                    base64.b64encode(key).decode() in env):
                raise ControllerError("private verifier settings are exposed to a candidate")
    payload = {"version": 1, **identity(contract), "contract_hash": digest(contract), "generated_at": utc_now(),
               "backend": "docker", "containers": list(containers), "public_image_digest": public_image_digest,
               "inspection_hash": digest(inspections), "protected_roots_hash": digest([str(p) for p in protected]),
               "isolated": True}
    atomic_json(within(contract["completion"]["evidence_root"], contract["completion"]["isolation"]), signed(contract, payload))
    return payload


def check_isolation(contract):
    try:
        value = read_json(within(contract["completion"]["evidence_root"], contract["completion"]["isolation"]))
        verify_signed(contract, value)
        check_identity(contract, value)
        if value.get("isolated") is not True or value.get("backend") != "docker" or not value.get("containers"):
            raise ControllerError("isolation attestation is incomplete")
        if value.get("protected_roots_hash") != digest(protected_roots(contract)):
            raise ControllerError("scheduler/private isolation binding differs")
        if timestamp(value["generated_at"]) > timestamp(contract["deadline"]):
            raise ControllerError("late isolation attestation")
        return value
    except (OSError, ValueError, KeyError, TypeError):
        fail("COMPLETION_RECEIPT_MISSING", "ISOLATION_UNVERIFIED", "trusted candidate isolation evidence is missing or invalid")


def register_jobs(contract, jobs):
    """Freeze original request/job IDs before waiting; never enqueue or replay."""
    trust_boundary(contract)
    validate_jobs(contract, jobs)
    payload = {"version": 1, **identity(contract), "contract_hash": digest(contract), "jobs": jobs}
    path = within(contract["completion"]["evidence_root"], contract["completion"]["jobs_manifest"])
    with file_lock(path.with_name(".completion-jobs.lock")):
        if path.exists():
            existing = read_json(path)
            verify_signed(contract, existing)
            if {k: v for k, v in existing.items() if k != "signature"} != payload:
                raise ControllerError("original scoring IDs are frozen; do not replace or resubmit")
        else:
            atomic_json(path, signed(contract, payload))
    return payload


def adopt_evaluation(contract, source_contract):
    """Bind an already certified experiment to the research run that selected it."""
    trust_boundary(contract)
    trust_boundary(source_contract)
    for name in ("task_id", "candidate_root", "stage", "score_expectation", "metric", "direction",
                 "required_seeds", "protocol_hash", "deadline"):
        if contract[name] != source_contract[name]:
            raise ControllerError("selected experiment belongs to a different scientific contract")
    for name in ("evidence_root", "signing_key", "data_hash", "evaluator_hash", "training", "private_roots"):
        if contract["completion"][name] != source_contract["completion"][name]:
            raise ControllerError("selected experiment has a different trust boundary")
    receipt = issue_receipt(source_contract, live=True)
    if receipt["status"] != "COMPLETED":
        raise ControllerError("only a fully certified experiment can be selected")
    root = contract["completion"]["evidence_root"]
    ledger = read_json(within(root, source_contract["completion"]["jobs_manifest"]))
    source_manifest = read_json(within(root, source_contract["candidate_manifest"]))
    result = read_json(within(root, source_contract["completion"]["result"]))
    # Validate everything before the first write, so a rejected source leaves
    # no frozen jobs or manifest behind and another source can still be selected.
    isolation = check_isolation(source_contract)
    if isolation["protected_roots_hash"] != digest(protected_roots(contract, ledger["jobs"])):
        raise ControllerError("selected experiment does not protect the research run's private roots")
    target_manifest = within(root, contract["candidate_manifest"])
    if target_manifest.exists() and read_json(target_manifest) != source_manifest:
        raise ControllerError("selected candidate manifest is immutable")
    target_isolation = within(root, contract["completion"]["isolation"])
    payload = {k: v for k, v in isolation.items() if k != "signature"}
    payload.update(**identity(contract), contract_hash=digest(contract),
                   source_contract_hash=digest(source_contract), source_receipt_hash=digest(receipt))
    if target_isolation.exists():
        existing = read_json(target_isolation)
        verify_signed(contract, existing)
        if {k: v for k, v in existing.items() if k != "signature"} != payload:
            raise ControllerError("selected isolation evidence is immutable")
    register_jobs(contract, ledger["jobs"])
    atomic_json(target_manifest, source_manifest)
    if not target_isolation.exists():
        atomic_json(target_isolation, signed(contract, payload))
    write_score_result(contract, result["scientific_score"], result["seeds"], harbor=result.get("harbor"))
    return issue_receipt(contract, live=True)


def validate_jobs(contract, jobs):
    if not isinstance(jobs, list) or not jobs or len(jobs) > 256:
        raise ControllerError("scored completion requires 1..256 original scoring jobs")
    seen, coverage = set(), {"score": set(), "reload": set()}
    for job in jobs:
        if not isinstance(job, dict) or set(job) != {"scheduler_root", "session_id", "request_id", "job_id", "seeds", "roles"}:
            raise ControllerError("invalid scoring job binding")
        for name in ("session_id", "request_id", "job_id"):
            pattern = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}" if name == "request_id" else LABEL
            if not isinstance(job[name], str) or not re.fullmatch(pattern, job[name]):
                raise ControllerError("scoring jobs require explicit original IDs")
        if not isinstance(job["scheduler_root"], str) or not Path(job["scheduler_root"]).is_absolute():
            raise ControllerError("scheduler root must be absolute")
        public = Path(contract["candidate_root"]).resolve()
        root = Path(job["scheduler_root"]).resolve()
        private_directory(Path(job["scheduler_root"]))
        if root.is_relative_to(public) or public.is_relative_to(root):
            raise ControllerError("scheduler evidence cannot be candidate writable")
        key = (job["scheduler_root"], job["session_id"], job["job_id"])
        if key in seen:
            raise ControllerError("duplicate scoring job")
        seen.add(key)
        seeds(job["seeds"], required=True)
        roles = job["roles"]
        if (not isinstance(roles, list) or not roles or any(not isinstance(v, str) for v in roles)
                or len(roles) != len(set(roles)) or set(roles) - coverage.keys()):
            raise ControllerError("job roles must be score and/or reload")
        for role in roles:
            coverage[role].update(job["seeds"])
    for role in ("score", "reload") if contract["completion"]["training"] else ("score",):
        if coverage[role] != set(contract["required_seeds"]):
            fail("INCOMPLETE_FINAL_SCORE", "JOB_SEED_MISSING", "required scoring/reload job seed coverage is incomplete")


def get_job(job, *, timeout=1):
    """One bounded read-only query of the original scheduler session and IDs."""
    request = {"op": "get", "session_id": job["session_id"], "id": job["job_id"], "request_id": job["request_id"]}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(Path(job["scheduler_root"]) / "scheduler.sock"))
        connection.sendall(canonical(request) + b"\n")
        with connection.makefile("rb") as stream:
            data = stream.readline(1024 * 1024 + 1)
    if len(data) > 1024 * 1024 or not data.endswith(b"\n"):
        raise ControllerError("incomplete scheduler GET response")
    reply = json.loads(data)
    if reply.get("ok") is not True:
        raise ControllerError("original scheduler job cannot be queried")
    return reply["result"]


def job_observations(contract, *, live=False):
    cfg = contract["completion"]
    try:
        ledger = read_json(within(cfg["evidence_root"], cfg["jobs_manifest"]))
        verify_signed(contract, ledger)
        check_identity(contract, ledger)
        validate_jobs(contract, ledger["jobs"])
    except EvidenceError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        fail("EVALUATION_UNKNOWN", "JOB_LEDGER_MISSING", "original scoring request/job ledger is missing or invalid")
    results = []
    for job in ledger["jobs"]:
        try:
            folder = within(job["scheduler_root"], f"sessions/{job['session_id']}/jobs/{job['job_id']}")
            snapshot = None
            if live:
                try:
                    snapshot = get_job(job, timeout=max(.01, min(1, timestamp(contract["deadline"]) - time.time())))
                except (OSError, ValueError):
                    pass
            if snapshot is None:
                snapshot = read_json(within(folder, "status.json"))
                # Offline audit can describe pending records. A failed live query
                # may only rely on a durable terminal record.
                if live and snapshot.get("state") not in JOB_TERMINAL:
                    snapshot = {"state": "UNKNOWN"}
            if not isinstance(snapshot, dict):
                raise ControllerError("invalid scheduler snapshot")
            if snapshot.get("state") != "UNKNOWN" and any(snapshot.get(k) != job[v] for k, v in
                    (("id", "job_id"), ("request_id", "request_id"), ("session_id", "session_id"))):
                raise ControllerError("scheduler identity differs")
            state = snapshot["state"]
            if state not in JOB_TERMINAL | {"QUEUED", "STARTING", "RUNNING", "CANCELLING", "UNKNOWN"}:
                state = "UNKNOWN"
            record = {k: job[k] for k in ("session_id", "request_id", "job_id", "seeds", "roles")}
            record.update(state=state, finished_at=snapshot.get("finished_at"), reason=snapshot.get("reason"))
            if state in JOB_TERMINAL:
                durable = read_json(within(folder, "status.json"))
                for field in ("id", "request_id", "session_id", "state", "finished_at", "reason"):
                    if durable.get(field) != snapshot.get(field):
                        raise ControllerError("scheduler terminal snapshot differs from durable evidence")
                score(snapshot["finished_at"])
                record["status_file"] = artifact(folder, "status.json")
            if state == "SUCCEEDED":
                exited = read_json(within(folder, "exit.json"))
                if not isinstance(exited, dict):
                    raise ControllerError("invalid scheduler executor exit")
                if exited.get("returncode") != 0 or exited.get("cleanup_ok") is not True or exited.get("reason") != "process_exit":
                    raise ControllerError("SUCCEEDED without a successful executor exit")
                record["exit_file"] = artifact(folder, "exit.json")
                record["finished_at"] = max(score(exited["at"]), score(snapshot["finished_at"]))
            results.append(record)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            results.append({k: job[k] for k in ("session_id", "request_id", "job_id", "seeds", "roles")} |
                           {"state": "UNKNOWN", "finished_at": None, "reason": "job_evidence_unavailable"})
    return results


def job_problem(jobs):
    if any(v["state"] == "UNKNOWN" for v in jobs):
        return "EVALUATION_UNKNOWN"
    if any(v["state"] in JOB_TERMINAL - {"SUCCEEDED"} for v in jobs):
        return "EVALUATION_FAILED"
    if any(v["state"] not in JOB_TERMINAL for v in jobs):
        return "EVALUATION_PENDING"
    return None


def write_score_result(contract, scientific_score, seed_results, *, harbor=None):
    """Called by a trusted evaluator after isolated evaluation, never by the Agent."""
    check_isolation(contract)
    bound = bindings(contract)
    payload = {"version": 1, **identity(contract), "contract_hash": digest(contract),
               "generated_at": utc_now(), "scientific_score": score(scientific_score),
               "seeds": seed_results, "bindings": bound, "harbor": harbor}
    validate_result(contract, payload, bound)
    path = within(contract["completion"]["evidence_root"], contract["completion"]["result"])
    with file_lock(path.with_name(".completion-result.lock")):
        if path.exists():
            existing = read_json(path)
            verify_signed(contract, existing)
            if any(existing.get(k) != payload[k] for k in payload if k != "generated_at"):
                raise ControllerError("final scientific result is immutable")
            return existing
        atomic_json(path, signed(contract, payload))
    return payload


def validate_result(contract, value, bound):
    check_identity(contract, value)
    if value.get('harbor') is not None and not isinstance(value['harbor'], dict):
        fail('FINAL_SCORE_INVALID', 'HARBOR_TRIAL_MISMATCH', 'Harbor binding must be an object')
    if value.get("bindings") != bound:
        old = value.get("bindings")
        old = old if isinstance(old, dict) else {}
        candidate = any(old.get(k) != bound[k] for k in ("candidate_manifest_hash", "source", "models", "checkpoints"))
        fail("CANDIDATE_BINDING_MISMATCH" if candidate else "PROTOCOL_BINDING_MISMATCH",
             "RESULT_BINDING_MISMATCH", "scoring result is bound to a different candidate or protocol")
    if timestamp(value["generated_at"]) > timestamp(contract["deadline"]):
        fail("INCOMPLETE_FINAL_SCORE", "LATE_RESULT", "scientific result was generated after the original deadline")
    if timestamp(check_isolation(contract)["generated_at"]) > timestamp(value["generated_at"]):
        fail("FINAL_SCORE_INVALID", "ISOLATION_AFTER_RESULT", "isolation must be verified before evaluation is sealed")
    score(value.get("scientific_score"))
    rows = value.get("seeds")
    if not isinstance(rows, list) or any(not isinstance(v, dict) for v in rows):
        fail("INCOMPLETE_FINAL_SCORE", "SEED_MISSING", "seed results are missing")
    coverage = [v.get("seed") for v in rows]
    try:
        seeds(coverage, required=True)
    except ControllerError:
        fail("INCOMPLETE_FINAL_SCORE", "SEED_MISSING", "seed IDs must be unique explicit strings")
    if len(coverage) != len(set(coverage)) or set(coverage) != set(contract["required_seeds"]):
        fail("INCOMPLETE_FINAL_SCORE", "SEED_MISSING", "formal seed coverage is incomplete or duplicated")
    root = contract["completion"]["evidence_root"]
    for row in rows:
        score(row.get("score"))
        verify_artifact(root, row.get("result_file"), "FINAL_SCORE_INVALID", "RESULT_FILE_INVALID")
        raw = read_json(within(root, row["result_file"]["path"]))
        if not isinstance(raw, dict) or score(raw.get(contract["metric"])) != row["score"]:
            fail("FINAL_SCORE_INVALID", "RESULT_SCORE_MISMATCH", "per-seed scientific result differs from its file")
        if value.get('harbor') is not None and raw.get('trial_id') != value['harbor'].get('trial_id'):
            fail('FINAL_SCORE_INVALID', 'HARBOR_TRIAL_MISMATCH', 'final scientific score belongs to a different Harbor Trial')
        if contract["completion"]["training"]:
            model = next(v["file"]["sha256"] for v in bound["models"] if v["seed"] == row["seed"])
            checkpoint = next(v["file"]["sha256"] for v in bound["checkpoints"] if v["seed"] == row["seed"])
            reload = row.get("reload")
            if not isinstance(reload, dict):
                fail("INCOMPLETE_FINAL_SCORE", "RELOAD_MISSING", "independent reload evidence is missing")
            verify_artifact(root, reload.get("file"), "INCOMPLETE_FINAL_SCORE", "RELOAD_MISSING")
            loaded = read_json(within(root, reload["file"]["path"]))
            if not isinstance(loaded, dict):
                fail("INCOMPLETE_FINAL_SCORE", "RELOAD_MISSING", "reload evidence must be a JSON object")
            if (reload.get("model_hash") != model or reload.get("checkpoint_hash") != checkpoint or
                    loaded.get("model_sha256") != model or loaded.get("checkpoint_sha256") != checkpoint or
                    raw.get("model_sha256") != model or raw.get("checkpoint_sha256") != checkpoint):
                fail("CANDIDATE_BINDING_MISMATCH", "RELOAD_HASH_MISMATCH", "reload/result model or checkpoint binding differs")
            if (type(reload.get("evaluation_pid")) is not int or type(reload.get("reload_pid")) is not int or
                    reload["evaluation_pid"] <= 0 or reload["reload_pid"] <= 0 or
                    reload["evaluation_pid"] == reload["reload_pid"] or
                    loaded.get("reload_pid") != reload["reload_pid"] or raw.get("evaluation_pid") != reload["evaluation_pid"]):
                fail("INCOMPLETE_FINAL_SCORE", "RELOAD_NOT_INDEPENDENT", "reload must have evidence from a separate process")
            if score(reload.get("score")) != row["score"] or score(loaded.get(contract["metric"])) != row["score"]:
                fail("FINAL_SCORE_INVALID", "RELOAD_SCORE_MISMATCH", "independent reload score differs")
    expected = math.fsum(row["score"] for row in rows) / len(rows)
    if not math.isclose(value["scientific_score"], expected, rel_tol=1e-12, abs_tol=1e-12):
        fail("FINAL_SCORE_INVALID", "AGGREGATION_MISMATCH", "scientific score must equal the predeclared seed mean")
    if value.get("harbor") is not None:
        validate_harbor(contract, value["harbor"], value["scientific_score"])


def validate_harbor(contract, harbor, final_score):
    root = contract["completion"]["evidence_root"]
    fields = {"trial_id", "config", "result", "reward", "log"}
    if not isinstance(harbor, dict) or set(harbor) not in (fields, fields | {'reward_priority'}):
        fail("FINAL_SCORE_INVALID", "HARBOR_TRIAL_MISMATCH", "Harbor evidence requires one complete Trial")
    if not isinstance(harbor['trial_id'], str) or not LABEL.fullmatch(harbor['trial_id']):
        fail("FINAL_SCORE_INVALID", "HARBOR_TRIAL_MISMATCH", "Harbor evidence requires an explicit Trial ID")
    for name in ("config", "result", "reward", "log"):
        verify_artifact(root, harbor[name], "FINAL_SCORE_INVALID", "HARBOR_FILE_INVALID")
    parents = [Path(harbor[name]["path"]).parent for name in ("config", "result")]
    trial_root = parents[0]
    if (parents[1] != trial_root or Path(harbor["reward"]["path"]).parent != trial_root / "verifier" or
            not Path(harbor["log"]["path"]).is_relative_to(trial_root)):
        fail("FINAL_SCORE_INVALID", "HARBOR_TRIAL_MISMATCH", "Harbor reward and final result must come from the same Trial directory")
    config = read_json(within(root, harbor["config"]["path"]))
    result = read_json(within(root, harbor["result"]["path"]))
    if not isinstance(config, dict) or not isinstance(result, dict):
        fail('FINAL_SCORE_INVALID', 'HARBOR_TRIAL_MISMATCH', 'Harbor config/result must be objects')
    reward_path = within(root, harbor["reward"]["path"])
    if reward_path.name not in ("reward.txt", "reward.json"):
        fail("FINAL_SCORE_INVALID", "HARBOR_REWARD_INVALID", "unknown Harbor reward file")
    present = [name for name in ('reward.txt', 'reward.json') if (reward_path.parent / name).exists()]
    priority = harbor.get('reward_priority')
    if priority is not None or len(present) > 1:
        if (not isinstance(priority, list) or len(priority) != 2 or
                any(not isinstance(v, str) for v in priority) or set(priority) != {'reward.txt', 'reward.json'} or
                next((v for v in priority if v in present), None) != reward_path.name):
            fail('FINAL_SCORE_INVALID', 'HARBOR_REWARD_PRIORITY', 'bind the actual Harbor reward-file priority when both files exist')
    if Path(harbor['log']['path']) not in (trial_root / 'trial.log', trial_root / 'verifier/test-stdout.txt'):
        fail('FINAL_SCORE_INVALID', 'HARBOR_LOG_INVALID', 'Harbor requires a nonempty Trial or verifier execution log')
    text_reward = reward_path.read_text().strip()
    reward = json.loads(text_reward) if reward_path.suffix == ".json" else float(text_reward)
    reward_object = reward if isinstance(reward, dict) else None
    if isinstance(reward, dict):
        if len(reward) != 1:
            fail("FINAL_SCORE_INVALID", "HARBOR_REWARD_INVALID", "reward must contain one scalar metric")
        reward = next(iter(reward.values()))
    result_id = result.get("trial_id", result.get("id", result.get("trial_name")))
    config_bound = (config.get("trial_id") == harbor["trial_id"] or result.get("config") == config or
                    config.get("name") == result.get("trial_name") == trial_root.name)
    if (not config_bound or result_id != harbor["trial_id"] or
            result.get("exception_info") is not None):
        fail("FINAL_SCORE_INVALID", "HARBOR_TRIAL_MISMATCH", "Harbor Trial identity or exit status differs")
    verifier = result.get("verifier_result")
    rewards = verifier.get('rewards') if isinstance(verifier, dict) else None
    if (not isinstance(rewards, dict) or len(rewards) != 1 or
            score(next(iter(rewards.values()))) != score(reward) or reward != final_score or
            reward_object is not None and rewards != reward_object):
        fail("FINAL_SCORE_INVALID", "HARBOR_REWARD_MISMATCH", "Harbor reward does not match the final scientific score")


def report(status, code=None, message=None, *, jobs=None, receipt=None):
    return {"ok": status == "COMPLETED", "status": status, "issues": [] if code is None else
            [{"code": code, "message": message}], "jobs": jobs or [], "receipt": receipt}


def inspect_evaluation(contract, *, live=False):
    """Read scientific evidence. Pending/unknown jobs never imply a valid score."""
    jobs = []
    try:
        trust_boundary(contract)
        jobs = job_observations(contract, live=live)
        problem = job_problem(jobs)
        if problem:
            return report(problem, problem, "original scoring jobs have not all succeeded", jobs=jobs)
        if any(v["finished_at"] > timestamp(contract["deadline"]) for v in jobs):
            fail("INCOMPLETE_FINAL_SCORE", "LATE_JOB", "a scoring job finished after the original deadline")
        check_isolation(contract)
        bound = bindings(contract)
        root = contract["completion"]["evidence_root"]
        path = within(root, contract["completion"]["result"])
        if not path.is_file() or path.stat().st_size == 0:
            fail("FINAL_SCORE_INVALID", "RESULT_MISSING", "SUCCEEDED scoring jobs have no non-empty scientific result")
        value = read_json(path)
        verify_signed(contract, value)
        validate_result(contract, value, bound)
        return report("COMPLETED", jobs=jobs, receipt={"bindings": bound, "scientific_score": value["scientific_score"],
                      "seeds": value["seeds"], "harbor": value.get("harbor"),
                      "result_file": artifact(root, contract["completion"]["result"]),
                      "isolation_hash": file_hash(within(root, contract["completion"]["isolation"]))})
    except EvidenceError as exc:
        return report(exc.status, exc.code, str(exc), jobs=jobs)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, OverflowError):
        return report("FINAL_SCORE_INVALID", "EVIDENCE_MALFORMED", "scientific evidence is missing, malformed or unsafe", jobs=jobs)


def issue_receipt(contract, *, live=False, now=None):
    """Trusted finalization is idempotent, and never submits a scoring job."""
    trust_boundary(contract)
    root = contract["completion"]["evidence_root"]
    final_path = within(root, contract["completion"]["receipt"])
    if final_path.exists():
        existing = read_json(final_path)
        verified = validate_receipt(contract, existing, live=live)
        if not verified['ok']:
            fail(verified['status'], 'EXISTING_RECEIPT_INVALID', 'preserve the invalid original receipt for audit')
        return existing
    verdict = inspect_evaluation(contract, live=live)
    now = utc_now() if now is None else now
    late_observation = verdict if timestamp(now) >= timestamp(contract['deadline']) else None
    if late_observation is not None:
        verdict = report("INCOMPLETE_FINAL_SCORE", "ORIGINAL_DEADLINE_REACHED", "original hard deadline reached", jobs=verdict["jobs"])
    payload = {"version": 1, **identity(contract), "contract_hash": digest(contract),
               "status": verdict["status"], "score_expectation": contract["score_expectation"],
               "generated_at": now, "original_deadline": contract["deadline"], "jobs": verdict["jobs"],
               "required_seeds": contract["required_seeds"], "issues": verdict["issues"]}
    if verdict["ok"]:
        payload.update(verdict["receipt"])
    elif late_observation is not None:
        payload['observed_evaluation'] = late_observation
    target = within(root, contract["completion"]["receipt"] if verdict["ok"] else "completion.pending.json")
    with file_lock(target.with_name(".completion-receipt.lock")):
        if target.exists() and verdict["ok"]:
            existing = read_json(target)
            verified = validate_receipt(contract, existing)
            if not verified["ok"]:
                raise ControllerError("existing completion receipt is invalid; preserve it for audit")
            return existing
        payload = signed(contract, payload)
        if late_observation is not None:
            archive = within(root, f'completion.late/{digest(payload)}.json')
            if not archive.exists():
                atomic_json(archive, payload)
        atomic_json(target, payload)
    return payload


def diagnostic_receipt(contract):
    validate_contract(contract)
    if contract["score_expectation"] != "not_expected":
        raise ControllerError("diagnostic receipt requires explicit not_expected")
    return {"version": 1, **identity(contract), "contract_hash": digest(contract), "status": "COMPLETED",
            "score_expectation": "not_expected", "generated_at": utc_now(), "original_deadline": contract["deadline"],
            "required_seeds": contract["required_seeds"], "jobs": [], "producer": "trusted-controller"}


def validate_receipt(contract, receipt, *, live=False):
    try:
        validate_contract(contract)
        if not isinstance(receipt, dict):
            raise ControllerError("receipt must be an object")
        if contract["score_expectation"] == "required":
            verify_signed(contract, receipt)
        check_identity(contract, receipt)
        if receipt.get("status") != "COMPLETED":
            status = receipt.get("status") if receipt.get("status") in COMPLETION_FAILURES else "FINAL_SCORE_INVALID"
            return report(status, "RECEIPT_NOT_COMPLETED", "completion receipt does not certify completion", jobs=receipt.get("jobs"))
        if receipt.get("original_deadline") != contract["deadline"] or timestamp(receipt["generated_at"]) > timestamp(contract["deadline"]):
            fail("INCOMPLETE_FINAL_SCORE", "LATE_RECEIPT", "receipt is not bound to the original deadline or was generated late")
        if receipt.get("score_expectation") != contract["score_expectation"] or receipt.get("required_seeds") != contract["required_seeds"]:
            fail("INCOMPLETE_FINAL_SCORE", "RECEIPT_CONTRACT_MISMATCH", "receipt score/seed contract differs")
        if contract["score_expectation"] == "not_expected":
            if receipt.get("jobs") != [] or "scientific_score" in receipt or receipt.get("producer") != "trusted-controller":
                raise ControllerError("invalid unscored diagnostic receipt")
            return report("COMPLETED", receipt=receipt)
        verdict = inspect_evaluation(contract, live=live)
        if not verdict["ok"]:
            return verdict
        if receipt.get("jobs") != verdict["jobs"]:
            fail("FINAL_SCORE_INVALID", "JOB_RECEIPT_MISMATCH", "receipt job IDs, terminal states or evidence changed")
        if any(receipt.get(k) != v for k, v in verdict["receipt"].items()):
            fail("FINAL_SCORE_INVALID", "RECEIPT_EVIDENCE_MISMATCH", "receipt differs from the current scientific evidence")
        return report("COMPLETED", jobs=verdict["jobs"], receipt=receipt)
    except EvidenceError as exc:
        return report(exc.status, exc.code, str(exc))
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return report("COMPLETION_RECEIPT_MISSING", "RECEIPT_INVALID", "completion receipt or trust contract is missing or invalid")


def publish_reward(contract, receipt, path):
    """Publish only a permitted scalar, atomically. The destination stays private."""
    verified = validate_receipt(contract, receipt)
    if not verified["ok"] or contract["score_expectation"] != "required":
        raise ControllerError("cannot publish a reward without validated scientific completion")
    path = Path(path)
    if path.name not in ("reward.txt", "reward.json"):
        raise ControllerError("use the established verifier scalar reward filename")
    if path.resolve().is_relative_to(Path(contract["candidate_root"]).resolve()):
        raise ControllerError("verifier reward must not be candidate writable")
    private_directory(path.parent)
    if path.name == "reward.json":
        atomic_json(path, {contract["metric"]: receipt["scientific_score"]})
    else:
        atomic_write(path, str(receipt["scientific_score"]) + "\n")
