#!/usr/bin/env python3
"""只读校验真实 skill 路由、层报告覆盖/证据哈希与终审的硬调用合同。

仅执行本套件检查器；不导入待检包、不运行题目/评分/Docker。
结构合法不是语义通过；stdout JSON，exit 1=合同失败，2=输入错误。
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from check_package import Inspection, strict_json, sha256

SUITE = Path(__file__).resolve().parent.parent
STATES = {"通过", "不通过", "证据不足", "不适用", "合理待生成"}


def read(path):
    return strict_json(Path(path).read_text(encoding="utf-8"))


def routes():
    doc = read(SUITE / "references/skill-routes.json")
    out = []
    for row in doc["layers"] + [{"layer": "FINAL", "path": doc["final"]}]:
        path = (SUITE / row["path"]).resolve()
        if not path.is_relative_to(SUITE) or not path.is_file():
            raise ValueError("路由缺失或越界")
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n") or "\nname:" not in content or "\ndescription:" not in content:
            raise ValueError("skill frontmatter不完整")
        out.append(dict(row, sha256=sha256(path)))
    return out


def path_info(value, root, report_dir, external_roots):
    """包证据相对包根；报告引用可明确 @review/；包外源只能在声明根。"""
    root = root.resolve()
    report_dir = report_dir.resolve()
    if not isinstance(value, str) or not value:
        return None
    base = value.split("#", 1)[0]
    base = re.sub(r":\d+$", "", base)
    if base.startswith("@review/"):
        path = (report_dir / base[8:]).resolve()
        return path if path.is_relative_to(report_dir) else None
    path = Path(base)
    if not path.is_absolute():
        if ".." in path.parts:
            return None
        resolved = (root / path).resolve()
        return resolved if resolved.is_relative_to(root) else None
    resolved = path.resolve()
    return resolved if any(resolved.is_relative_to(r) for r in external_roots) else None


def check_layer(doc, report_path, root, snapshot, route_rows, visited=None):
    issues = []
    visited = set() if visited is None else set(visited)
    report_path = report_path.resolve()
    if report_path in visited:
        return ["上游回执存在循环"], None
    visited.add(report_path)
    def fail(detail):
        issues.append(detail)
    needed = {"schema_version", "mode", "layer", "package_digest", "definition", "checks", "conclusion", "risks", "upstream", "skill", "source_basis", "commands"}
    if not isinstance(doc, dict) or needed - set(doc):
        return ["层报告缺必填字段"], None
    layer = doc["layer"]
    route = next((r for r in route_rows if r.get("layer") == layer), None)
    if route is None:
        return ["未知层"], None
    if doc["schema_version"] != "package-layer-review.v1" or doc["mode"] not in {"pre_run", "pre_submit"}:
        fail("schema/mode不合法")
    if doc["package_digest"] != snapshot["sha256"]:
        fail("package_digest与当前全文件清单不符，报告已过期或未绑定")
    skill = doc["skill"]
    if not isinstance(skill, dict) or skill.get("path") != route["path"] or skill.get("sha256") != route["sha256"]:
        fail("实际skill路径/hash回执缺失或不是当前文件")
    if not isinstance(doc["source_basis"], list) or not doc["source_basis"]:
        fail("缺规范/方法来源版本回执")
    else:
        for source in doc["source_basis"]:
            if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not source["path"].strip():
                fail("规范来源回执缺path/hash")
                continue
            candidates = [Path(source["path"])] if Path(source["path"]).is_absolute() else [SUITE / source["path"], SUITE.parent / source["path"]]
            existing = next((p for p in candidates if p.is_file()), None)
            if not existing or source.get("sha256") != sha256(existing):
                fail("规范来源文件/hash缺失或变化")
    if not isinstance(doc["commands"], list) or not doc["commands"] or any(not isinstance(c, str) or not c.strip() for c in doc["commands"]):
        fail("commands必须是非空已执行只读命令记录数组")
    definition = doc["definition"]
    if not isinstance(definition, dict) or any(not isinstance(definition.get(k), list) or not definition[k] for k in ("deliverables", "acceptance")):
        fail("缺本题本层初始化定义/验收方向")
    external_roots = []
    for value in doc.get("evidence_roots", []):
        if isinstance(value, str) and Path(value).is_absolute():
            external_roots.append(Path(value).resolve())
        else:
            fail("evidence_roots必须是已声明绝对私有根")
    checks = doc["checks"]
    if not isinstance(checks, list):
        return issues + ["checks不是数组"], None
    ids = [c.get("id") for c in checks if isinstance(c, dict)]
    if sorted(ids) != sorted(route["checks"]):
        fail("固定检查ID遗漏/重复/多余；不能以摘要替代全项")
    for row in checks:
        if not isinstance(row, dict):
            fail("检查条目不是对象")
            continue
        required = {"id", "status", "clause", "evidence", "fact", "reason", "action", "recheck"}
        if required - set(row) or row.get("status") not in STATES:
            fail(str(row.get("id")) + " 字段/状态不合法")
            continue
        if not isinstance(row["clause"], str) or not row["clause"].strip() or not isinstance(row["fact"], str) or not row["fact"].strip():
            fail(row["id"] + " 缺出处/真实事实")
        if row["status"] in {"不通过", "证据不足"} and any(not isinstance(row[k], str) or not row[k].strip() for k in ("reason", "action", "recheck")):
            fail(row["id"] + " 返修三字段缺失")
        if row["status"] in {"不适用", "合理待生成"} and (not isinstance(row["reason"], str) or not row["reason"].strip()):
            fail(row["id"] + " 适用性/待生成缺理由")
        if row["status"] == "合理待生成" and (layer != "F" or row["id"] not in {"F03", "F04", "F05"} or doc["mode"] != "pre_run"):
            fail(row["id"] + " 非长程产物不能延期")
        evidence = row["evidence"]
        if not isinstance(evidence, list) or (row["status"] == "通过" and not evidence):
            fail(row["id"] + " 通过项缺证据")
            continue
        for ev in evidence:
            if not isinstance(ev, dict) or not {"path", "sha256", "locator"}.issubset(ev):
                fail(row["id"] + " 证据缺path/hash/locator")
                continue
            if not isinstance(ev["locator"], str) or not ev["locator"].strip():
                fail(row["id"] + " 证据缺实际内容定位")
            path = path_info(ev["path"], root, report_path.parent, external_roots)
            if path is None or not path.exists():
                fail(row["id"] + " 证据不存在/越界/包外根未声明")
                continue
            if path.is_file():
                # Hash bytes only. No source file is imported or executed.
                if ev["sha256"] != sha256(path):
                    fail(row["id"] + " 证据文件SHA-256缺失或不一致")
            elif row["status"] == "通过" and not ev["locator"]:
                fail(row["id"] + " 目录证据缺manifest/内容定位")
    statuses = [r.get("status") for r in checks if isinstance(r, dict)]
    local = "不通过" if "不通过" in statuses else "证据不足" if "证据不足" in statuses else "不适用" if statuses and all(s == "不适用" for s in statuses) else "通过"
    upstream = doc["upstream"]
    if not isinstance(upstream, list):
        fail("upstream不是数组")
        upstream = []
    previous = chr(ord(layer) - 1) if layer in "BCDEFGH" else None
    if previous and not any(isinstance(u, dict) and u.get("layer") == previous for u in upstream):
        if doc["conclusion"] not in {"证据不足", "不通过"}:
            fail("缺前层结论却越层通过")
    upstream_bad = []
    for u in upstream:
        if not isinstance(u, dict) or not {"layer", "path", "sha256", "conclusion"}.issubset(u):
            fail("上游回执不完整")
            continue
        path = path_info(u["path"], root, report_path.parent, external_roots)
        if not path or not path.is_file() or sha256(path) != u["sha256"]:
            fail("上游报告hash不一致/未声明路径")
            continue
        source = read(path)
        if source.get("layer") != u["layer"] or source.get("conclusion") != u["conclusion"] or source.get("package_digest") != doc["package_digest"] or source.get("mode") != doc["mode"]:
            fail("上游结论/版本与回执不一致")
        problems, _ = check_layer(source, path, root, snapshot, route_rows, visited)
        if problems:
            fail("上游报告本身不符合硬调用合同")
        if u["conclusion"] not in {"通过", "不适用"}:
            upstream_bad.append(u["conclusion"])
    effective = "不通过" if local == "不通过" else "证据不足" if upstream_bad or (previous and not upstream) else local
    if doc["conclusion"] != effective:
        fail("汇总结论不等于本层逐项与上游的保守判定")
    if not isinstance(doc["risks"], list) or len(doc["risks"]) > 3:
        fail("risks必须≤3条")
    return issues, effective


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("routes")
    for cmd in ("layer", "final"):
        child = sub.add_parser(cmd)
        child.add_argument("--root", required=True, type=Path)
        child.add_argument("--report" if cmd == "layer" else "--reports", required=True, type=Path)
        if cmd == "final":
            child.add_argument("--mode", choices=("pre_run", "pre_submit"), default="pre_run")
    args = parser.parse_args(argv)
    try:
        route_rows = routes()
        if args.command == "routes":
            result = {"schema_version": "package-routing-receipt.v1", "suite": str(SUITE), "routes": route_rows}
        else:
            root = args.root.resolve()
            output_path = (args.report if args.command == "layer" else args.reports).resolve()
            if output_path.is_relative_to(root):
                raise ValueError("报告必须放包外")
            observation = Inspection(root, "pre_run").run()
            snapshot = observation["facts"]["snapshot"]
            paths = [args.report] if args.command == "layer" else [args.reports / ("layer-" + r["layer"] + ".json") for r in route_rows if r["layer"] != "FINAL"]
            reviews = []
            for index, path in enumerate(paths):
                if not path.is_file():
                    reviews.append({"path": str(path), "issues": ["缺层报告"], "conclusion": "证据不足"})
                    continue
                doc = read(path)
                problems, conclusion = check_layer(doc, path.resolve(), root, snapshot, route_rows)
                if args.command == "final" and doc.get("layer") != route_rows[index]["layer"]:
                    problems.append("报告文件槽位与真实层不一致；不能重复A层替代B–H")
                if args.command == "final" and doc.get("mode") != args.mode:
                    problems.append("mode与终审不一致")
                reviews.append({"path": str(path), "sha256": sha256(path), "layer": doc.get("layer"), "issues": problems, "conclusion": conclusion})
            contradictions = [f for f in observation["findings"] if f["status"] == "不通过"]
            result = {"schema_version": "package-review-contract.v1", "read_only": True, "package_digest": snapshot["sha256"], "reports": reviews,
                      "contract_valid": all(not r["issues"] for r in reviews),
                      "deterministic_contradictions": contradictions,
                      "layers_allow_admission": not contradictions and all(not r["issues"] and r["conclusion"] in {"通过", "不适用"} for r in reviews),
                      "final_semantic_admission": None,
                      "limitation": "仅合同/哈希检查；尚需终审实际核全局一致性、P1–P7和真实事件证据。"}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return int(result.get("contract_valid") is False)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, RecursionError):
        print(json.dumps({"error": "输入/合同不能安全读取；未执行题目代码"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
