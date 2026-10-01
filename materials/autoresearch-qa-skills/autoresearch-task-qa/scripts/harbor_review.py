"""Harbor evidence inventory and semantic-review contract, never a task runner.

This module does not import Harbor or package code, build images, or run scripts.
Structural observations are not a substitute for version-specific schema review.
"""
import json
import math
from pathlib import Path
import re

import docker_paths

try:
    import tomllib
except ImportError:
    tomllib = None

LIMIT = 4 * 1024 * 1024
RULESET = "harbor-compatibility-2026-09-18"
INTERNAL_SOURCE = "https://bytedance.larkoffice.com/wiki/A7FUwJBALici6Gku3rWc4EsinLf"
OFFICIAL_SOURCE = "https://www.harborframework.com/docs/tasks"
H_IDS = [f"H{i:02d}" for i in range(1, 7)]
H_TITLES = ["任务目录", "版本与配置", "环境与运行路径", "测试入口与 reward", "Job/Trial 调用配置", "Harness 运行证据"]


def bounded_text(path):
    if path.stat().st_size > LIMIT:
        raise ValueError("file exceeds the 4 MiB inspection limit")
    return path.read_text(encoding="utf-8")


def collect(root, files):
    cfg = None
    configs = [p for p in files if Path(p).name == "task.toml" and ".git" not in Path(p).parts]
    configs = sorted(configs, key=lambda p: (len(Path(p).parts), p))
    selected = None
    if configs:
        depth = len(Path(configs[0]).parts)
        nearest = [p for p in configs if len(Path(p).parts) == depth]
        if len(nearest) == 1:
            selected = Path(nearest[0]).parent.as_posix()
    observations = {"task_candidates": [Path(p).parent.as_posix() for p in configs],
                    "task_root": selected, "toml_parser": "tomllib" if tomllib else "unavailable",
                    "config_sections": [], "schema_version": None, "parse_error": None}
    if selected is not None:
        try:
            raw = bounded_text(root / selected / "task.toml")
            if tomllib:
                cfg = tomllib.loads(raw)
                observations["config_sections"] = list(cfg)
                observations["schema_version"] = cfg.get("schema_version", cfg.get("version"))
            else:
                observations["config_sections"] = re.findall(r"(?m)^\[([^\[\]\n]+)\]", raw)
        except (OSError, ValueError, UnicodeError) as exc:
            observations["parse_error"] = str(exc)
    observations["job_configs"] = [p for p in files if Path(p).name in ("job.yaml", "job.yml", "trial.yaml", "trial.yml")]
    observations["trial_results"] = [p for p in files if Path(p).name == "result.json"]
    return {"ruleset": RULESET, "sources": [INTERNAL_SOURCE, OFFICIAL_SOURCE],
            "target_version": None, "provider": None, "observations": observations,
            "path_contract": docker_paths.inspect(root, selected, cfg),
            "checks": [], "static_status": "manual", "runtime_status": "not_run"}


def resolve_ref(root, ref):
    if not isinstance(ref, str):
        raise ValueError("Harbor evidence must be a string")
    name = re.sub(r":\d+$", "", ref.split("#", 1)[0])
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Harbor evidence is not a file inside the inspected bundle: {ref}")
    return path


def numeric_reward(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_reward(path):
    raw = bounded_text(path)
    if path.name == "reward.txt":
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ValueError("reward.txt must contain a single numeric value") from exc
        reward = {"reward": value}
    else:
        reward = json.loads(raw)
    if not isinstance(reward, dict) or not reward or not all(numeric_reward(v) for v in reward.values()):
        raise ValueError("reward must be a nonempty numeric map with finite values, not status strings/bools")
    return reward


def validate_trial_evidence(root, refs):
    """Cross-check a same-trial config/result/reward/log set; no authenticity claim."""
    paths = [resolve_ref(root, ref) for ref in refs]
    results = [p for p in paths if p.name == "result.json"]
    for result_path in results:
        trial = result_path.parent
        config = trial / "config.json"
        json_reward = trial / "verifier" / "reward.json"
        text_reward = trial / "verifier" / "reward.txt"
        reward_path = json_reward if json_reward.is_file() else text_reward
        logs = [p for p in paths if p in (trial / "trial.log", trial / "verifier" / "test-stdout.txt")]
        if config not in paths or reward_path not in paths or not logs:
            continue
        result = json.loads(bounded_text(result_path))
        config_value = json.loads(bounded_text(config))
        if not isinstance(config_value, dict) or not config_value or not isinstance(result, dict):
            raise ValueError("Harbor trial config/result must be nonempty objects")
        if result.get("exception_info"):
            raise ValueError("Harbor trial has exception_info; cannot claim successful run evidence")
        if not result.get("finished_at"):
            raise ValueError("Harbor trial is missing finished_at")
        verifier = result.get("verifier_result")
        rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
        expected = read_reward(reward_path)
        if not isinstance(rewards, dict) or not rewards or not all(numeric_reward(v) for v in rewards.values()):
            raise ValueError("Harbor result.verifier_result.rewards must contain finite numeric rewards")
        if set(rewards) != set(expected) or any(not math.isclose(rewards[k], v, rel_tol=1e-9, abs_tol=1e-12) for k, v in expected.items()):
            raise ValueError("Harbor result rewards disagree with the selected reward file")
        if not any(bounded_text(log).strip() for log in logs):
            raise ValueError("Harbor trial log evidence is empty")
        return {"trial_dir": trial.relative_to(root.resolve()).as_posix(), "rewards": expected}
    raise ValueError("H06 pass requires config.json, result.json, verifier/reward.*, and trial.log or verifier/test-stdout.txt from the same trial")


def aggregate(rows):
    statuses = {row["status"] for row in rows}
    return "fail" if "fail" in statuses else "manual" if "manual" in statuses else "pass"


def apply_review(current, review, root):
    if not isinstance(review, dict):
        raise ValueError("review.harbor must include the six Harbor compatibility conclusions")
    rows = review.get("checks")
    if not isinstance(rows, list) or len(rows) != 6 or not all(isinstance(r, dict) for r in rows) or {r.get("id") for r in rows} != set(H_IDS):
        raise ValueError("Harbor review must contain H01–H06 exactly once")
    for field in ("target_version", "provider", "version_basis"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError(f"Harbor review requires {field}; use unknown and manual when it cannot be established")
    selected = review.get("task_root", current["observations"].get("task_root"))
    task_root = (root / selected).resolve() if isinstance(selected, str) else None
    if task_root and not task_root.is_relative_to(root.resolve()):
        raise ValueError("Harbor task_root cannot escape the inspected bundle")
    declaration = review.get("path_contract")
    if not isinstance(declaration, dict) or declaration.get("profile") not in docker_paths.PROFILES:
        raise ValueError("Harbor path_contract requires an explicit supported profile")
    if not isinstance(declaration.get("profile_basis"), str) or not declaration["profile_basis"].strip():
        raise ValueError("Harbor path_contract requires profile_basis")
    adapter_refs = declaration.get("adapter_evidence", [])
    if not isinstance(adapter_refs, list):
        raise ValueError("Harbor path_contract.adapter_evidence must be an array")
    for ref in adapter_refs:
        resolve_ref(root, ref)
    cfg = None
    if task_root and (task_root / "task.toml").is_file() and tomllib:
        try:
            cfg = tomllib.loads(bounded_text(task_root / "task.toml"))
        except (OSError, ValueError, UnicodeError):
            pass  # H02 owns TOML syntax; never infer config fields from malformed text.
    path_contract = docker_paths.inspect(root, selected, cfg, declaration)
    manual_resolution = declaration.get("manual_resolution")
    if manual_resolution is not None:
        if not isinstance(manual_resolution, dict):
            raise ValueError("path_contract.manual_resolution must be an object")
        reason, refs = manual_resolution.get("summary"), manual_resolution.get("evidence")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or not isinstance(refs, list) or not refs:
            raise ValueError("path_contract.manual_resolution requires concise summary and real evidence")
        for ref in refs:
            resolve_ref(root, ref)
        path_contract["manual_resolution"] = manual_resolution
    path_contract["adapter_evidence"] = adapter_refs
    final = []
    for row in sorted(rows, key=lambda r: r["id"]):
        check_id, status = row["id"], row.get("status")
        if status not in ("pass", "fail", "manual", "not_applicable"):
            raise ValueError(f"{check_id}: invalid Harbor status")
        if status == "not_applicable" and check_id not in ("H05", "H06"):
            raise ValueError(f"{check_id}: required Harbor check cannot be skipped")
        reason, refs = row.get("summary"), row.get("evidence", [])
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 220 or not isinstance(refs, list):
            raise ValueError(f"{check_id}: concise summary and evidence array required")
        paths = [resolve_ref(root, ref) for ref in refs]
        if status == "pass" and not refs:
            raise ValueError(f"{check_id}: passing requires evidence")
        if check_id in ("H01", "H02") and status == "pass":
            if not task_root or not (task_root / "task.toml").is_file():
                raise ValueError(f"{check_id}: no task.toml at the selected task root")
            if task_root / "task.toml" not in paths:
                raise ValueError(f"{check_id}: cite the selected task.toml")
        if check_id == "H02" and status == "pass":
            if review["target_version"] == "unknown" or review["version_basis"] == "unknown":
                raise ValueError("H02: unknown version basis cannot pass")
            if tomllib:
                tomllib.loads(bounded_text(task_root / "task.toml"))
        if check_id == "H03" and status == "pass" and review["provider"] == "unknown":
            raise ValueError("H03: unknown provider cannot pass")
        if check_id == "H03":
            if path_contract["status"] == "fail":
                status = "fail"
                problems = [f for f in path_contract["findings"] if f["status"] == "fail"]
                reason = ("路径检查不通过：" + "；".join(p["message"] for p in problems))[:220]
                refs = sorted(set(refs + [r for p in problems for r in p["evidence"]]))
            elif path_contract["status"] == "manual" and status == "pass" and (not manual_resolution or (declaration["profile"] == "custom" and not adapter_refs)):
                status = "manual"
                reason = "路径存在静态无法解析项；需提供 path_contract.manual_resolution 的真实证据后才能通过。"
        if check_id == "H06" and status == "pass":
            validate_trial_evidence(root, refs)
        if check_id == "H06" and status == "not_applicable" and refs:
            raise ValueError("H06: supplied runtime evidence must be reviewed, not skipped")
        final.append({"id": check_id, "title": H_TITLES[H_IDS.index(check_id)], "status": status,
                      "summary": reason, "evidence": refs})
    current = dict(current)
    current.update({k: review[k] for k in ("target_version", "provider", "version_basis")})
    current["task_root"] = selected
    current["path_contract"] = path_contract
    current["checks"] = final
    current["static_status"] = aggregate(final[:5])
    runtime = final[5]["status"]
    current["runtime_status"] = {"pass": "evidence_consistent", "fail": "failed", "manual": "unverified", "not_applicable": "not_run"}[runtime]
    current["qa17_status"] = aggregate(final)
    return current


def summary(harbor):
    static = {"pass": "通过", "fail": "不通过", "manual": "未完成"}[harbor["static_status"]]
    runtime = {"not_run": "未验证运行", "evidence_consistent": "已有运行证据一致，非独立复跑",
               "failed": "运行证据显示失败", "unverified": "运行证据待核实"}[harbor["runtime_status"]]
    return f"Harbor 格式/接口：{static}；{runtime}。"
