"""Small in-repository batch adapter fixture used by completion tests.

It exercises the same contract and evidence checks as a task adapter while
keeping tests independent of any private task checkout.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _write_json(path: Path, value: object) -> None:
    from core.longrun import atomic_json
    atomic_json(path, value)


def load_completion(config):
    release = config.get("controller_release") or os.environ.get("AUTORESEARCH_CONTROLLER_RELEASE")
    if not release or not Path(release).is_absolute():
        raise ValueError("A trusted absolute controller_release is required for new batches")
    release_path = Path(release).resolve(strict=True)
    import sys
    sys.path.insert(0, str(release_path))
    from core import completion
    from core.longrun import read_json

    filename = config.get("completion_contract") or os.environ.get("AUTORESEARCH_COMPLETION_CONTRACT")
    if not filename:
        raise ValueError("A frozen completion_contract is required before launching a batch")
    contract = read_json(Path(filename))
    completion.validate_contract(contract)
    for field in completion.CONTRACT_FIELDS:
        expected = contract.get("declared_deadline", contract[field]) if field == "deadline" else contract[field]
        if field not in config or config[field] != expected:
            raise ValueError(f"Batch must explicitly match completion contract: {field}")
    if config.get("formal") and contract["stage"] not in ("formal", "final"):
        raise ValueError("Formal batches must use the formal or final stage")
    completion.trust_boundary(contract)
    if contract["score_expectation"] == "required":
        variant = config.get("completion_variant")
        selected = [job for job in config["jobs"] if job["variant"] == variant]
        completion.seeds([str(job["seed"]) for job in selected], required=True)
        if {str(job["seed"]) for job in selected} != set(contract["required_seeds"]):
            raise ValueError("Batch completion_variant must cover all required seeds exactly once")
        completion.check_isolation(contract)
        root = Path(contract["completion"]["evidence_root"]).resolve(strict=True)
        for job in config["jobs"]:
            if not Path(job["output"]).resolve().is_relative_to(root):
                raise ValueError("Scoring batch outputs must stay in the trusted evidence root")
    return completion, contract


def complete_scientific_batch(api, contract, config, state):
    from core.longrun import read_json
    from experiment_batch import complete_batch

    root = Path(contract["completion"]["evidence_root"]).resolve(strict=True)
    rows, models = [], []
    for job in config["jobs"]:
        if job["variant"] != config["completion_variant"]:
            continue
        output, seed = Path(job["output"]), str(job["seed"])
        raw = read_json(output / "result.json")
        metric = api.score(raw.get(contract["metric"]))
        normalized = {contract["metric"]: metric,
                      "original_result": api.artifact(root, str((output / "result.json").relative_to(root)))}
        row = {"seed": seed, "score": metric}
        if contract["completion"]["training"]:
            evaluation = read_json(output / "train/process.json")
            reload_process = read_json(output / "reload/process.json")
            if evaluation["returncode"] != 0 or reload_process["returncode"] != 0:
                raise ValueError("Training and independent reload must both exit successfully")
            model = api.artifact(root, str((output / "model.pt").relative_to(root)))
            loaded = read_json(output / "reload.log")
            if (raw.get("model_sha256") != model["sha256"]
                    or loaded.get("model_sha256") != model["sha256"]
                    or api.score(loaded.get(contract["metric"])) != metric):
                raise ValueError("Independent reload differs from the bound model and final score")
            normalized.update(model_sha256=model["sha256"], checkpoint_sha256=model["sha256"],
                             evaluation_pid=evaluation["pid"])
            reload_normalized = {contract["metric"]: metric, "model_sha256": model["sha256"],
                "checkpoint_sha256": model["sha256"], "reload_pid": reload_process["pid"],
                "original_reload": api.artifact(root, str((output / "reload.log").relative_to(root)))}
            reload_path = output / "completion.reload.json"
            _write_json(reload_path, reload_normalized)
            row["reload"] = {"score": metric, "model_hash": model["sha256"],
                "checkpoint_hash": model["sha256"], "evaluation_pid": evaluation["pid"],
                "reload_pid": reload_process["pid"],
                "file": api.artifact(root, str(reload_path.relative_to(root)))}
            models.append({"seed": seed, "origin": "evidence", "file": model})
        result_path = output / "completion.result.json"
        _write_json(result_path, normalized)
        row["result_file"] = api.artifact(root, str(result_path.relative_to(root)))
        rows.append(row)
    manifest_path = api.within(root, contract["candidate_manifest"])
    manifest = read_json(manifest_path)
    if contract["completion"]["training"]:
        for name in ("models", "checkpoints"):
            if manifest[name] and manifest[name] != models:
                raise ValueError("Candidate model manifest is already bound to different outputs")
            manifest[name] = models
        _write_json(manifest_path, manifest)
    return complete_batch(contract, rows)
