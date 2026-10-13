#!/usr/bin/env python3
"""题目包的确定性只读观察；不导入、执行或反序列化待检包内容。

stdout 是 JSON。退出码 1 表示发现确定性矛盾，2 表示输入错误。
没有发现矛盾不等于准入通过；语义层审核与最终审查另行进行。
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


SCHEMA = "package-check.v1"
STATUSES = {"通过", "不通过", "证据不足", "不适用", "合理待生成"}
POST_IDS = {
    "trajectory_codex", "trajectory_seed", "best_method",
    "agent_effective_duration", "agent_round_count", "best_method_clean_revalidation",
}
RESULT_FIELDS = {
    "schema_version", "status", "role", "seed", "task_type", "method",
    "protocol", "training", "execution", "metrics", "quality_gate", "artifacts",
}
PLACEHOLDERS = {"NOT_RUN", "待生成"}
REAL_FIELDS = {
    "duration_hours", "effective_seconds", "effective_hours", "actual_duration_seconds",
    "actual_duration_hours", "packaged_rounds", "effective_rounds", "scored_rounds",
    "actual_rounds", "round_count", "completed_rounds", "real_round_count", "typical_round_minutes",
}
MAX_JSON = 16 * 1024 * 1024
CLAUSES = {
    "A": "用户任务§4.1；工作区AGENTS.md§6；打包闭环白名单",
    "E": "用户任务§5 E；官方QA references/submission-format.md§2",
    "D": "用户任务§5 D；requirements-map.md D04：交付hidden_assets为空并有生成/注入证据",
    "F": "用户任务§0/§4.1/§5 F/§7：长程产物不得预填",
    "G": "用户任务§5 G；官方QA references/exposure-manifest.md",
    "H": "用户任务§5 H/§7：冻结哈希与可复核信息",
}


def strict_json(raw):
    def constant(_):
        raise ValueError("JSON 含 NaN/Infinity 等非法常量")

    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise ValueError("JSON 含重复键（键内容不回显）")
            out[key] = value
        return out

    def finite(node):
        if isinstance(node, float) and not math.isfinite(node):
            raise ValueError("JSON 数值溢出为非有限值")
        if isinstance(node, dict):
            for value in node.values():
                finite(value)
        elif isinstance(node, list):
            for value in node:
                finite(value)

    value = json.loads(raw, parse_constant=constant, object_pairs_hook=pairs)
    finite(value)
    return value


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Inspection:
    def __init__(self, root, mode, ledger=None):
        self.root = Path(root).expanduser().resolve()
        self.mode = mode
        self.ledger_path = Path(ledger).expanduser().resolve() if ledger else None
        self.findings = []
        self.files = {}
        self.docs = {}
        self.pending_ids = set()
        self.facts = {"snapshot": {}, "json": {}, "runs": {}, "trajectories": {}, "manifests": []}

    def finding(self, check, status, path, detail):
        layer = check[0]
        self.findings.append({"check": check, "layer": layer, "status": status,
                              "clause": CLAUSES.get(layer, CLAUSES["A"]),
                              "path": str(path), "detail": detail})

    def relative(self, path):
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return "@external-ledger"

    def safe_path(self, value):
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            return None
        value = re.sub(r":\d+$", "", value.split("#", 1)[0])
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts:
            return None
        target = self.root.joinpath(*pure.parts)
        try:
            if not target.resolve().is_relative_to(self.root):
                return None
        except (OSError, RuntimeError):
            return None
        return target

    def inventory(self):
        snapshot = []
        structure = []
        for base, dirs, names in os.walk(self.root, followlinks=False):
            dirs.sort()
            names.sort()
            for name in dirs[:] + names:
                path = Path(base) / name
                rel = self.relative(path)
                try:
                    info = path.lstat()
                except OSError:
                    self.finding("A02", "证据不足", rel, "无法读取文件元数据。")
                    continue
                if name.startswith("._") or "__MACOSX" in path.relative_to(self.root).parts:
                    self.finding("A02", "不通过", rel, "AppleDouble/__MACOSX 打包杂项不属于冻结白名单；只在新交付副本清理。")
                if stat.S_ISLNK(info.st_mode):
                    try:
                        link_hash = hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
                    except OSError:
                        link_hash = None
                    structure.append({"path": rel, "type": "symlink", "mode": stat.S_IMODE(info.st_mode),
                                      "uid": info.st_uid, "gid": info.st_gid, "target_text_sha256": link_hash})
                    if name in dirs:
                        dirs.remove(name)
                    try:
                        escapes = not path.resolve().is_relative_to(self.root)
                    except (OSError, RuntimeError):
                        escapes = True
                    self.finding("G02", "不通过" if escapes else "证据不足", rel,
                                 "符号链接越出包根或无法解析；未读取目标。" if escapes else
                                 "存在包内符号链接，未跟随；最终白名单与目标身份须语义复核。")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    structure.append({"path": rel, "type": "directory", "mode": stat.S_IMODE(info.st_mode),
                                      "uid": info.st_uid, "gid": info.st_gid})
                    continue
                if not stat.S_ISREG(info.st_mode):
                    structure.append({"path": rel, "type": "special", "mode": info.st_mode,
                                      "uid": info.st_uid, "gid": info.st_gid})
                    self.finding("G02", "不通过", rel, "特殊文件不能作为普通交付文件；未读取。")
                    continue
                try:
                    digest = sha256(path)
                    after = path.stat()
                    if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        self.finding("H04", "证据不足", rel, "散列期间文件发生变化，不能确认冻结快照。")
                    self.files[rel] = {"size": info.st_size, "sha256": digest}
                    snapshot.append([rel, info.st_size, digest])
                    structure.append({"path": rel, "type": "file", "mode": stat.S_IMODE(info.st_mode),
                                      "uid": info.st_uid, "gid": info.st_gid, "size": info.st_size, "sha256": digest})
                except OSError:
                    self.finding("H04", "证据不足", rel, "文件散列失败。")
        encoded = json.dumps(sorted(snapshot), ensure_ascii=False, separators=(",", ":")).encode()
        self.facts["snapshot"] = {
            "file_count": len(snapshot), "bytes": sum(row[1] for row in snapshot),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "algorithm": "sha256(UTF-8 JSON sorted [[relative_path,bytes,file_sha256]], ensure_ascii=false,separators=(',',':'))",
            "files": self.files,
        }
        structure.sort(key=lambda row: row["path"])
        encoded_structure = json.dumps(structure, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.facts["structure_manifest"] = {"sha256": hashlib.sha256(encoded_structure).hexdigest(),
            "algorithm": "sha256(UTF-8 JSON path-sorted entries, sort_keys=true,ensure_ascii=false,separators=(',',':')); symlink target text is separately hashed and never followed",
            "entries": structure}

    def json_files(self):
        checked = 0
        for rel, info in self.files.items():
            if not rel.endswith(".json") or Path(rel).name.startswith("._"):
                continue
            if info["size"] > MAX_JSON:
                self.finding("A04", "证据不足", rel, "JSON 超过16MiB只读解析上限，需分段人工复核。")
                continue
            try:
                self.docs[rel] = strict_json((self.root / rel).read_text(encoding="utf-8"))
                checked += 1
            except (OSError, UnicodeError, ValueError, RecursionError):
                self.finding("A04", "不通过", rel, "JSON 不能严格解析（语法、重复键、非有限值、编码或递归深度）；不回显内容。")
        self.facts["json"] = {"parsed_count": checked, "limit_bytes": MAX_JSON}

    def structure(self):
        mandatory = ["workspace", "expert_evidence", "optimization_evidence",
                     "workspace/harbor_task", "workspace/reference"]
        required_files = ["workspace/harbor_task/instruction.md", "workspace/harbor_task/task.toml",
                          "workspace/harbor_task/environment/Dockerfile", "workspace/harbor_task/tests/Dockerfile",
                          "workspace/harbor_task/tests/test.sh", "expert_evidence/专家作业说明文档.md",
                          "expert_evidence/expert_annotation.json", "expert_evidence/run_summary.json",
                          "optimization_evidence/训练证据说明.md", "optimization_evidence/comparison_summary.json"]
        for rel in mandatory + required_files:
            path = self.safe_path(rel)
            expected = path and (path.is_dir() if rel in mandatory else path.is_file())
            if not expected or path.is_symlink():
                self.finding("A02", "不通过", rel, "本阶段必需结构/文件缺失或不是普通目标；等价路径须由台账明确映射后复核。")
        self.facts["structure"] = {"required_directories": mandatory, "required_files": required_files}

    def public_view(self):
        public_roots = [self.root / "workspace/harbor_task/environment"]
        manifest_paths = ["qa_exposure_manifest.json", "workspace/qa_exposure_manifest.json"]
        for rel in manifest_paths:
            doc = self.docs.get(rel)
            if not isinstance(doc, dict):
                continue
            base = (self.root / rel).parent
            visible = doc.get("agent_visible_paths", [])
            if not isinstance(visible, list):
                self.finding("G01", "不通过", rel, "agent_visible_paths 必须是数组。")
                continue
            for value in visible:
                if not isinstance(value, str):
                    self.finding("G01", "不通过", rel, "可见路径条目不是字符串。")
                    continue
                # Manifest paths are relative to its own workspace root, per official QA.
                path = base / value
                if Path(value).is_absolute() or ".." in PurePosixPath(value).parts or not path.resolve().is_relative_to(self.root):
                    self.finding("G01", "不通过", rel, "Agent 可见清单路径越界或不是相对路径。")
                else:
                    public_roots.append(path)
        checked = set()
        for rel in self.files:
            path = self.root / rel
            if "hidden_assets" not in path.parts:
                continue
            if any(path == p or path.is_relative_to(p) for p in public_roots):
                checked.add(rel)
                self.finding("G02", "不通过", rel, "公开源/显式Agent可见面中的 hidden_assets 非空；私有Verifier同名目录不受本项全树禁令。")
        self.facts["public_view"] = {"declared_public_roots": sorted({self.relative(p) for p in public_roots}),
                                     "nonempty_public_hidden_files": sorted(checked),
                                     "isolation_certified": False}

    def artifact_references(self, doc, locator):
        """仅检查明确声明的包内artifact路径/hash，不猜任意字符串语义。"""
        if not isinstance(doc, dict):
            self.finding("E04", "证据不足", locator, "artifact索引不是已识别对象，需按真实schema映射。")
            return
        refs = []
        if isinstance(doc.get("path"), str):
            refs.append((doc["path"], doc.get("sha256"), doc.get("bytes", doc.get("size_bytes"))))
        if isinstance(doc.get("reload_log"), str):
            refs.append((doc["reload_log"], doc.get("reload_log_sha256"), None))
        for key, value in doc.items():
            if isinstance(key, str) and "/" in key and isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value):
                refs.append((key, value, None))
            elif isinstance(value, dict) and key not in {"reload_metrics", "metrics"} and ("path" in value or "reload_log" in value):
                self.artifact_references(value, locator + "." + key)
        for value, digest, size in refs:
            target = self.safe_path(value)
            if target is None:
                self.finding("E04", "不通过", locator, "明确artifact路径不是安全包根相对引用，未读取目标。")
                continue
            info = self.files.get(self.relative(target))
            if info is None:
                self.finding("E04", "证据不足", locator, "artifact引用不在当前普通文件快照，需核平台认可不可变外部资产/路径映射。")
                continue
            if digest is not None and (not isinstance(digest, str) or digest.lower() != info["sha256"]):
                self.finding("E04", "不通过", locator, "artifact声明SHA-256与实际普通文件不符。")
            elif digest is None:
                self.finding("E04", "证据不足", locator, "artifact路径缺SHA-256声明。")
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size != info["size"]):
                self.finding("E04", "不通过", locator, "artifact声明字节数与实际文件不符。")
        # 用户本次另有交付空目录要求；这不是把私有Verifier目录说成Agent可见。
        delivered_hidden = "workspace/harbor_task/tests/hidden_assets/"
        private_delivered = [rel for rel in self.files if rel.startswith(delivered_hidden)]
        if private_delivered:
            self.finding("D04", "不通过", delivered_hidden,
                         "当前任务明确要求交付hidden_assets为空；tests/hidden_assets含文件。此项不推断Agent已能访问私有文件。")
        self.facts["public_view"]["delivered_tests_hidden_files"] = private_delivered

    def optimization(self):
        opt = self.root / "optimization_evidence"
        role_seeds = {}
        for role in ("baseline", "reference"):
            parent = opt / (role + "_runs")
            seeds = {}
            if not parent.is_dir() or parent.is_symlink():
                self.finding("E01", "不通过", self.relative(parent), "正式运行角色目录缺失。")
            else:
                for path in sorted(parent.iterdir()):
                    if not path.name.startswith("seed_") or not path.is_dir() or path.is_symlink():
                        continue
                    label = path.name[5:]
                    rel = self.relative(path)
                    result = self.docs.get(rel + "/result.json")
                    log = self.files.get(rel + "/run.log")
                    observations = {"result_parsed": isinstance(result, dict),
                                    "log_bytes": log["size"] if log else None}
                    if not isinstance(result, dict):
                        self.finding("E02", "不通过", rel + "/result.json", "正式seed的result.json缺失、不可解析或不是对象。")
                    else:
                        missing = sorted(RESULT_FIELDS - set(result))
                        if missing:
                            self.finding("E02", "证据不足", rel + "/result.json", "最小字段待映射/补齐：" + ", ".join(missing))
                        if str(result.get("seed")) != label:
                            self.finding("E01", "不通过", rel + "/result.json", "result.seed 与目录真实seed标签不一致；不得重命名规避。")
                        if result.get("role") != role:
                            self.finding("E02", "不通过", rel + "/result.json", "result.role 与正式角色目录不一致。")
                        status = result.get("status")
                        observations["declared_status"] = status
                        self.artifact_references(result.get("artifacts"), rel + "/result.json#artifacts")
                        if isinstance(status, str) and status.upper() in {"NOT_RUN", "INVALID", "FAILED", "ERROR"}:
                            self.finding("E01", "不通过", rel + "/result.json", "正式G03 seed尚未有效完成，不能以长程待生成豁免。")
                        elif not isinstance(status, str) or status not in {"COMPLETE", "VALID", "SUCCESS", "OK", "complete", "success", "ok"}:
                            self.finding("E02", "证据不足", rel + "/result.json", "运行状态采用其他schema；需在台账映射真实状态语义，不自动判失败。")
                        kind = result.get("task_type")
                        training = isinstance(kind, str) and kind in {"training", "TRAINING", "model_training", "train", "fine_tuning", "finetuning"}
                        nontraining = isinstance(kind, str) and kind in {"non_training", "NON_TRAINING", "inference_only", "no_training"}
                        model_rel = rel + "/model/"
                        payloads = [p for p, v in self.files.items() if p.startswith(model_rel)
                                    and Path(p).suffix.lower() in {".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".onnx", ".npz", ".pkl", ".joblib", ".h5", ".keras", ".weights"}
                                    and v["size"] > 0]
                        artifact = self.docs.get(model_rel + "artifact.json")
                        if isinstance(artifact, dict):
                            self.artifact_references(artifact, model_rel + "artifact.json")
                            if artifact.get("role") not in (None, role) or str(artifact.get("seed", label)) != label:
                                self.finding("E04", "不通过", model_rel + "artifact.json", "artifact角色/seed与本次真实目录不一致。")
                        reload_info = self.files.get(model_rel + "reload.log")
                        observations["model"] = {"training": training, "non_training": nontraining,
                                                 "payloads": payloads, "artifact_parsed": isinstance(artifact, dict),
                                                 "reload_bytes": reload_info["size"] if reload_info else None}
                        if training:
                            if not payloads:
                                self.finding("E04", "证据不足", rel + "/model", "没有已识别的非空模型载荷；可能需映射其他格式，不能反序列化验证。")
                            if not isinstance(artifact, dict):
                                self.finding("E04", "不通过", model_rel + "artifact.json", "训练型正式seed缺少可解析artifact.json。")
                            if not reload_info or not reload_info["size"]:
                                self.finding("E05", "证据不足", model_rel + "reload.log", "重载文件缺失/为空；须核对其他非空且可追溯的实际独立重载记录，不能据文件名直接判重载失败。")
                        elif nontraining:
                            observations["model"]["not_applicable_marker"] = result.get("training") == "NOT_APPLICABLE" or (
                                isinstance(result.get("training"), dict) and result["training"].get("status") == "NOT_APPLICABLE")
                            if not observations["model"]["not_applicable_marker"]:
                                self.finding("E05", "证据不足", rel + "/result.json", "非训练任务需将模型/训练适用性映射为NOT_APPLICABLE；不据此要求空model目录。")
                        else:
                            self.finding("E04", "证据不足", rel + "/result.json", "训练适用性尚未映射，未机械施加模型要求。")
                    if not log or not log["size"]:
                        self.finding("E03", "不通过", rel + "/run.log", "正式运行原始日志缺失或为空。")
                    seeds[label] = observations
            if not seeds:
                self.finding("E01", "不通过", self.relative(parent), "没有正式seed运行目录。")
            role_seeds[role] = seeds
        b, r = set(role_seeds["baseline"]), set(role_seeds["reference"])
        if b != r:
            self.finding("E01", "不通过", "optimization_evidence", "Baseline/Reference seed全集不一致。")
        summary = self.docs.get("optimization_evidence/comparison_summary.json")
        if isinstance(summary, dict) and isinstance(summary.get("seeds"), list):
            declared = [str(seed) for seed in summary["seeds"]]
            if len(declared) != len(set(declared)) or set(declared) != b or set(declared) != r:
                self.finding("E06", "不通过", "optimization_evidence/comparison_summary.json#seeds", "汇总正式seed清单与逐seed目录不一致或重复。")
        else:
            self.finding("E06", "证据不足", "optimization_evidence/comparison_summary.json#seeds", "正式seed声明需要schema映射；不凭目录数量推定全部seed。")
        self.facts["runs"] = {"roles": role_seeds, "paired_seeds": sorted(b & r),
                              "counts": {k: len(v) for k, v in role_seeds.items()},
                              "scientific_metrics_recomputed": False}

    def ledger(self):
        path = self.ledger_path
        if path is None:
            for rel in ("qa-ledger.json", "expert_evidence/package_preparation/qa-ledger.json", "expert_evidence/qa-ledger.json"):
                if (self.root / rel).is_file():
                    path = self.root / rel
                    break
        if path is None:
            self.finding("A05", "证据不足", "qa-ledger.json", "未提供新套件台账；历史成品包不因此判失败，需建立映射后才能形成新准入结论。")
            self.facts["ledger"] = {"present": False}
            return False
        rel = self.relative(path)
        try:
            doc = strict_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            self.finding("A05", "不通过", rel, "台账不能严格解析。")
            return False
        if not isinstance(doc, dict) or doc.get("schema_version") != "package-ledger.v1" or not isinstance(doc.get("items"), list):
            self.finding("A05", "不通过", rel, "台账必须是package-ledger.v1对象，含items数组。")
            return False
        declared_digest = doc.get("package_digest")
        if declared_digest is None:
            self.finding("A05", "证据不足", rel, "台账尚未绑定package_digest；生产台账应在包外，避免摘要自包含循环。")
        elif declared_digest != self.facts["snapshot"]["sha256"]:
            self.finding("H04", "不通过", rel + "#package_digest", "台账package_digest与当前完整文件快照不一致。")
        seen = set()
        pending_ids = set()
        post_pending = False
        for index, row in enumerate(doc["items"]):
            ref = rel + "#items[" + str(index) + "]"
            required = {"id", "layer", "target", "required_stage", "status", "reason", "sources", "action", "authorization_required"}
            if not isinstance(row, dict) or required - set(row):
                self.finding("A05", "不通过", ref, "台账条目缺少契约必填字段。")
                continue
            if not isinstance(row["id"], str) or not row["id"].strip():
                self.finding("A05", "不通过", ref, "台账id必须是非空字符串。")
                continue
            if row["id"] in seen:
                self.finding("A05", "不通过", ref, "台账id重复。")
            seen.add(row["id"])
            if (not isinstance(row["layer"], str) or row["layer"] not in set("ABCDEFGH")
                    or not isinstance(row["status"], str) or row["status"] not in STATUSES
                    or not isinstance(row["required_stage"], str) or row["required_stage"] not in {"pre_run", "post_run", "optional"}
                    or not isinstance(row["authorization_required"], bool)):
                self.finding("A05", "不通过", ref, "台账层/状态/阶段/授权字段不符合契约。")
            target = self.safe_path(row["target"])
            if target is None:
                self.finding("A05", "不通过", ref, "台账目标不是安全包根相对路径。")
            post = row["required_stage"] == "post_run"
            if post and row["id"] not in POST_IDS:
                self.finding("A06", "不通过", ref, "非白名单项不能设为post_run；评分/隔离/模型重载不能被待生成豁免。")
            if post and row["id"] in POST_IDS and target is not None:
                expected = {
                    "trajectory_codex": "expert_evidence/trajectory_codex.json",
                    "trajectory_seed": "expert_evidence/trajectory_seed.json",
                    "best_method": "expert_evidence/best_method",
                    "agent_effective_duration": "expert_evidence/run_summary.json",
                    "agent_round_count": "expert_evidence/run_summary.json",
                    "best_method_clean_revalidation": "expert_evidence/run_summary.json",
                }[row["id"]]
                if self.relative(target) != expected:
                    self.finding("A06", "不通过", ref, "post_run ID与目标对象不符，不能将评分/其他材料改名为长程占位。")
            pending = row["status"] == "合理待生成"
            if pending:
                pending_ids.add(row["id"])
                if row["id"] in {"trajectory_codex", "trajectory_seed"}:
                    existing = self.docs.get("expert_evidence/" + row["id"] + ".json")
                    if isinstance(existing, dict) and isinstance(existing.get("rounds"), list) and existing["rounds"]:
                        self.finding("F04", "不通过", ref, "台账将已有真实轮次的轨迹标为待生成；须保留真值并区分历史与本次版本。")
                if not post or row["id"] not in POST_IDS or not isinstance(row["reason"], str) or not row["reason"].strip() or "value" not in row or row["value"] is not None:
                    self.finding("A06", "不通过", ref, "合理待生成只允许白名单post_run项，value必须null且reason非空。")
                elif self.mode == "pre_submit":
                    self.finding("F05", "不通过", ref, "pre_submit仍有必交长程产物待生成。")
                else:
                    post_pending = True
                    self.finding("F04", "合理待生成", ref, "合法长程后补项：值为null且已记录未运行原因。")
            if row["required_stage"] == "pre_run" and (not target or not target.exists()):
                self.finding("A03", "不通过", ref, "本阶段必需目标缺失，不能使用长程待生成口径。")
            if row["status"] == "通过" and (not target or not target.exists()):
                self.finding("A05", "不通过", ref, "台账声称通过但目标不存在。")
            if not isinstance(row["sources"], list):
                self.finding("A05", "不通过", ref, "sources必须是来源对象数组。")
                continue
            for source in row["sources"]:
                if not isinstance(source, dict) or not isinstance(source.get("path"), str):
                    self.finding("A05", "不通过", ref, "来源对象须含path；建议sha256/origin_run/version，历史别名可映射。")
                    continue
                source_path = self.safe_path(source["path"])
                digest = source.get("sha256", source.get("hash"))
                if source_path is None:
                    self.finding("A05", "证据不足", ref, "来源为包外原始档案/非相对引用；未读取，需提供包内交付映射。")
                elif not source_path.exists():
                    self.finding("A05", "不通过", ref, "台账引用的包内来源不存在。")
                elif digest is not None:
                    file = self.files.get(self.relative(source_path))
                    if not re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", str(digest)):
                        self.finding("A05", "不通过", ref, "来源SHA-256格式无效。")
                    elif file and file["sha256"] != str(digest).removeprefix("sha256:").lower():
                        self.finding("H04", "不通过", ref, "台账来源SHA-256与只读快照不一致。")
        global_pending = doc.get("pending_long_run")
        if not isinstance(global_pending, list) or not all(isinstance(value, str) for value in global_pending):
            self.finding("A06", "证据不足", rel + "#pending_long_run", "缺少可解析的全局待生成id清单。")
        elif len(global_pending) != len(set(global_pending)) or set(global_pending) != pending_ids:
            self.finding("A06", "不通过", rel + "#pending_long_run", "全局待生成清单与items中合理待生成id不一致或重复。")
        self.facts["ledger"] = {"present": True, "path": rel, "item_count": len(doc["items"]), "sha256": sha256(path)}
        self.pending_ids = pending_ids
        return post_pending

    def longrun(self, pending):
        historical = False
        for name in ("trajectory_codex", "trajectory_seed"):
            rel = "expert_evidence/" + name + ".json"
            doc = self.docs.get(rel)
            if isinstance(doc, dict) and isinstance(doc.get("rounds"), list) and doc["rounds"]:
                historical = True
                self.facts["trajectories"][name] = {"state": "HISTORICAL_PRESENT", "round_count": len(doc["rounds"])}
                if isinstance(doc.get("status"), str) and doc["status"] in PLACEHOLDERS:
                    self.finding("F04", "不通过", rel, "NOT_RUN/待生成轨迹却含真实轮次声明，状态矛盾。")
            elif isinstance(doc, dict) and isinstance(doc.get("status"), str) and doc["status"] in PLACEHOLDERS:
                self.facts["trajectories"][name] = {"state": "NOT_RUN", "round_count": 0}
                if not isinstance(doc.get("reason"), str) or not doc["reason"].strip() or doc.get("rounds") != []:
                    self.finding("F04", "不通过", rel, "未运行轨迹必须含空rounds数组和非空原因。")
                if self.mode == "pre_submit":
                    self.finding("F04", "不通过", rel, "最终提交不能使用NOT_RUN轨迹。")
            else:
                self.facts["trajectories"][name] = {"state": "UNMAPPED_OR_ABSENT", "round_count": None}
                if self.mode == "pre_submit":
                    self.finding("F04", "不通过", rel, "最终提交缺少可映射的真实轨迹。")
                elif not pending:
                    self.finding("F04", "证据不足", rel, "没有真实轨迹或已登记的post_run待生成映射；pre_run不凭缺轨迹判失败。")
        rel = "expert_evidence/run_summary.json"
        doc = self.docs.get(rel)
        if not isinstance(doc, dict):
            return
        explicitly_pending = (isinstance(doc.get("status"), str) and doc["status"] in PLACEHOLDERS) or (pending and not historical)
        self.facts["longrun_stage"] = "NOT_RUN" if explicitly_pending else "HISTORICAL_OR_UNMAPPED"
        if explicitly_pending:
            def visit(node, trail, real_scope=True):
                if isinstance(node, list):
                    for i, value in enumerate(node):
                        visit(value, trail + "[" + str(i) + "]", real_scope)
                elif isinstance(node, dict):
                    for key, value in node.items():
                        if real_scope and key in REAL_FIELDS and value is not None:
                            self.finding("F03", "不通过", rel + "#" + trail + "." + key,
                                         "未运行阶段的真实时长/轮数必须null；计划预算与rounds空数组长度另列。")
                        if key in {"best_method_rerun_passed", "best_method_revalidated", "clean_revalidation_passed"} and value is not None:
                            self.finding("F03", "不通过", rel + "#" + trail + "." + key,
                                         "未运行阶段不能预填最佳方法复验结果，必须null并登记原因。")
                        if isinstance(value, (list, dict)) and key not in {"plan", "planned", "budget", "config", "configuration", "targets"}:
                            scope = key in {"agent_execution", "agents", "trajectories", "longrun", "gpt", "seed", "codex"}
                            visit(value, trail + "." + key, scope)
            visit(doc, "")
            if historical:
                    self.finding("F04", "不通过", rel, "run_summary声明NOT_RUN但同级轨迹已有轮次；需明确历史与本次版本映射。")
        if explicitly_pending or "best_method" in self.pending_ids:
            for candidate in self.files:
                if not candidate.startswith("expert_evidence/best_method/"):
                    continue
                name = Path(candidate).name
                if name not in {"README.md", ".gitkeep", ".keep", "NOT_RUN.md"}:
                    self.finding("F04", "不通过", candidate, "本次未运行却已放置best_method代码/产物；不得用Reference/Starter或专家实验顶替。")
                elif name.endswith(".md"):
                    try:
                        raw = (self.root / candidate).read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        raw = ""
                    if "NOT_RUN" not in raw and "待生成" not in raw:
                        self.finding("F04", "证据不足", candidate, "best_method占位说明没有明确未运行标记；这只是结构观察，需语义确认。")

    def manifests(self):
        names = {"SHA256SUMS", "SHA256SUMS.txt", "manifest.sha256", "files.sha256", "sha256_manifest.txt", "hashes.sha256"}
        for rel in self.files:
            if Path(rel).name not in names:
                continue
            if self.files[rel]["size"] > MAX_JSON:
                self.finding("H04", "证据不足", rel, "哈希清单超过解析上限。")
                continue
            count, unknown = 0, 0
            try:
                lines = (self.root / rel).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                self.finding("H04", "证据不足", rel, "哈希清单不能以UTF-8读取。")
                continue
            for line in lines:
                if not line.strip() or line.startswith("#"):
                    continue
                match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line)
                if not match:
                    unknown += 1
                    continue
                digest, target = match.groups()
                resolved = self.safe_path(target)
                if resolved is None:
                    self.finding("H04", "不通过", rel, "哈希清单引用越界路径；未读取目标。")
                    continue
                info = self.files.get(self.relative(resolved))
                if info is None:
                    # Some historical manifests deliberately use their parent as base.
                    alternative = self.safe_path((Path(rel).parent / target).as_posix())
                    info = self.files.get(self.relative(alternative)) if alternative else None
                if info is None:
                    self.finding("H04", "证据不足", rel, "哈希清单来源基准/目标未映射；不可用历史路径直接断言当前包损坏。")
                elif info["sha256"] != digest.lower():
                    self.finding("H04", "不通过", rel, "哈希清单的文件SHA-256与当前快照不一致。")
                else:
                    count += 1
            self.facts["manifests"].append({"path": rel, "matched_entries": count, "unparsed_lines": unknown})
            if unknown:
                self.finding("H04", "证据不足", rel, "部分清单行不是已支持的sha256sum语法，需映射。")

    def run(self):
        if not self.root.is_dir():
            raise ValueError("--root 必须是存在的题目包目录")
        self.inventory()
        self.json_files()
        self.structure()
        self.public_view()
        self.optimization()
        pending = self.ledger()
        self.longrun(pending)
        self.manifests()
        counts = Counter(row["status"] for row in self.findings)
        return {"schema_version": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(),
                "root": str(self.root), "mode": self.mode, "read_only": True,
                "facts": self.facts, "findings": self.findings,
                "summary": {"status": "DETERMINISTIC_CONTRADICTIONS" if counts["不通过"] else "OBSERVATIONS_ONLY",
                            "finding_counts": dict(counts), "admission_decision": None},
                "limitations": ["不执行候选/评分/训练/Docker，不加载模型，不写入题目包。",
                                "哈希和计数是事实；来源真实性、方法语义、独立重载、物理隔离和准入须由各层审核判断。",
                                "历史schema与等价路径必须显式映射；本工具不改动、重算或覆盖冻结科学证据。"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--mode", choices=("pre_run", "pre_submit"), default="pre_run")
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args(argv)
    try:
        report = Inspection(args.root, args.mode, args.ledger).run()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"schema_version": SCHEMA, "error": type(exc).__name__,
                          "detail": "输入或文件系统状态无法检查；未运行待检代码。"}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return int(any(row["status"] == "不通过" for row in report["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
