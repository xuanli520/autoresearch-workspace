"""Conservative static Docker path checks. Never builds or executes task files.

The teaching profile comes from the actual 2026-09-18 example attachment, not
from an assumption about Harbor's provider build context.
"""
import fnmatch
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex


LIMIT = 4 * 1024 * 1024
TEACHING = "teaching-task-root-v1"
NATIVE = "harbor-environment-v1"
PROFILES = (TEACHING, NATIVE, "custom")
TEACHING_SOURCE = "https://bytedance.larkoffice.com/docx/P1J5dgIw1oKchcxn29GcvzlMnSd"
SOURCE_NOTE = "2026-09-18 读取教学文档 example.zip：workspace/harbor_task/environment/Dockerfile、task.toml、README.md 与入口；未验证平台 adapter 部署。"


def read_text(path):
    if path.stat().st_size > LIMIT:
        raise ValueError("path inspection text exceeds 4 MiB")
    return path.read_text(encoding="utf-8")


def instructions(raw):
    """Handle ordinary Docker continuations, but never pretend to parse heredocs."""
    pending, start = "", 0
    for number, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if not pending:
            start = number
        pending += stripped[:-1] + " " if stripped.endswith("\\") else stripped
        if stripped.endswith("\\"):
            continue
        match = re.match(r"([A-Za-z]+)\s+(.*)", pending)
        if match:
            yield start, match.group(1).upper(), match.group(2)
        else:
            yield start, "UNKNOWN", pending
        pending = ""
    if pending:
        yield start, "UNKNOWN", pending


def copy_args(raw):
    flags = []
    while raw.startswith("--"):
        match = re.match(r"(--[^\s]+)\s+(.*)", raw, re.S)
        if not match:
            raise ValueError("unresolved COPY/ADD flag")
        flags.append(match.group(1))
        raw = match.group(2).lstrip()
    parts = json.loads(raw) if raw.startswith("[") else shlex.split(raw)
    if not isinstance(parts, list) or len(parts) < 2 or not all(isinstance(p, str) for p in parts):
        raise ValueError("COPY/ADD requires source and destination strings")
    return flags, parts[:-1], parts[-1]


def inspect(root, task_root, config=None, declaration=None):
    root = root.resolve()
    config_unknown = config is None
    config = config or {}
    declaration = declaration or {}
    profile = declaration.get("profile", TEACHING)
    out = {"profile": profile, "profile_basis": declaration.get("profile_basis", SOURCE_NOTE),
           "sources": [TEACHING_SOURCE], "task_root": task_root, "dockerfile": None,
           "build_context": None, "runtime_task_root": "/workspace" if profile == TEACHING else declaration.get("runtime_task_root"),
           "test_entry": "/workspace/tests/test.sh" if profile == TEACHING else "/tests/test.sh",
           "copy_operations": [], "findings": [], "status": "manual"}

    def finding(code, status, message, evidence=None):
        out["findings"].append({"code": code, "status": status, "message": message,
                                "evidence": [evidence] if evidence else []})

    def finish():
        states = {f["status"] for f in out["findings"]}
        out["status"] = "fail" if "fail" in states else "manual" if "manual" in states else "pass"
        return out

    if profile not in PROFILES:
        finding("profile_unknown", "manual", "未支持的构建 profile；需明确构建上下文和容器路径。")
        return finish()
    if not isinstance(task_root, str):
        finding("task_root_unknown", "manual", "无法唯一确定 task.toml 所在任务根目录。")
        return finish()
    task = (root / task_root).resolve()
    if not task.is_relative_to(root):
        finding("task_root_escape", "fail", "任务根目录越出待检包。")
        return finish()
    relative = lambda p: p.relative_to(root).as_posix()
    dockerfile, context = task / "environment/Dockerfile", task if profile == TEACHING else task / "environment"
    if profile == "custom":
        for key in ("dockerfile", "build_context", "runtime_task_root"):
            if not isinstance(declaration.get(key), str) or not declaration[key].strip():
                finding("custom_mapping_missing", "manual", f"custom profile 缺少 {key} 的明确路径映射。")
        if any(f["status"] == "manual" for f in out["findings"]):
            return finish()
        dockerfile = (root / declaration["dockerfile"]).resolve()
        context = (root / declaration["build_context"]).resolve()
        out["runtime_task_root"] = declaration["runtime_task_root"]
        if not declaration.get("adapter_evidence"):
            finding("custom_adapter_unverified", "manual", "custom profile 需提供实际适配入口或配置映射的本地证据。")
    if not dockerfile.resolve().is_relative_to(root) or not context.resolve().is_relative_to(root):
        finding("build_path_escape", "fail", "Dockerfile 或 build context 越出待检包。")
        return finish()
    out.update(dockerfile=relative(dockerfile), build_context=relative(context))
    for key in ("dockerfile", "build_context", "runtime_task_root"):
        if key in declaration and profile != "custom" and declaration[key] != out[key]:
            finding("profile_path_mismatch", "fail", f"声明 {key}={declaration[key]} 与 {profile} 规定的 {out[key]} 不一致。")
    environment = config.get("environment", {})
    if not dockerfile.is_file():
        if profile == NATIVE and config_unknown:
            finding("environment_config_unparsed", "manual", "无 Dockerfile，且 TOML 尚未可信解析；需核实是否使用原生预构建镜像。", relative(task / "task.toml"))
            return finish()
        if profile == NATIVE and isinstance(environment, dict) and environment.get("docker_image"):
            out["status"] = "not_applicable"
            out["findings"].append({"code": "prebuilt_image", "status": "not_applicable",
                                    "message": "原生 profile 使用预构建镜像；镜像内路径须在 H03 语义复核。", "evidence": [relative(task / "task.toml")]})
            return out
        finding("dockerfile_missing", "fail", f"规定位置缺少 Dockerfile：{out['dockerfile']}。", relative(task / "task.toml"))
        return finish()
    if not context.is_dir():
        finding("context_missing", "fail", f"构建上下文不存在：{out['build_context']}。", out["dockerfile"])
        return finish()
    try:
        raw = read_text(dockerfile)
    except (OSError, UnicodeError, ValueError) as exc:
        finding("dockerfile_unreadable", "manual", str(exc), out["dockerfile"])
        return finish()
    rows = list(instructions(raw))
    dynamic_filesystem = any(cmd == "RUN" for _, cmd, _ in rows)
    multi_stage = sum(cmd == "FROM" for _, cmd, _ in rows) > 1
    if multi_stage:
        finding("multi_stage", "manual", "多阶段镜像需逐阶段核对最终文件映射；不把前一阶段文件当成最终镜像文件。", out["dockerfile"])
        return finish()
    if re.search(r"(?m)^#\s*escape\s*=\s*`", raw) or "<<" in raw:
        finding("dynamic_syntax", "manual", "非默认 escape 或 heredoc 语法需人工解析。", out["dockerfile"])
        return finish()

    ignore_file = dockerfile.with_name("Dockerfile.dockerignore")
    if not ignore_file.is_file():
        ignore_file = context / ".dockerignore"
    ignore_patterns = []
    complex_ignore = False
    if ignore_file.is_file():
        try:
            ignore_patterns = [p.strip().strip("/") for p in read_text(ignore_file).splitlines() if p.strip() and not p.lstrip().startswith("#")]
            complex_ignore = any(p.startswith("!") or "**" in p or "[" in p for p in ignore_patterns)
            if complex_ignore:
                finding("complex_dockerignore", "manual", "带否定、递归或字符集的 dockerignore 需按 Docker 语义人工核对。", relative(ignore_file))
        except (OSError, UnicodeError, ValueError) as exc:
            finding("dockerignore_unreadable", "manual", str(exc), relative(ignore_file))
            complex_ignore = True

    def ignored(name):
        if complex_ignore:
            return False
        prefixes = [name] + [p.as_posix() for p in PurePosixPath(name).parents if str(p) != "."]
        return any(fnmatch.fnmatchcase(candidate, pattern) for pattern in ignore_patterns for candidate in prefixes)

    copied = set()
    workdir = None
    uncertain_copy = False
    runtime_commands = []
    for lineno, cmd, arg in rows:
        ref = f"{out['dockerfile']}:{lineno}"
        if cmd == "FROM":
            copied = set()
            workdir = None
            runtime_commands = []
        elif cmd == "WORKDIR":
            if "$" in arg:
                workdir = None
                finding("dynamic_workdir", "manual", "WORKDIR 含变量，需核实最终展开路径。", ref)
            else:
                value = arg.strip('"\'')
                if not value.startswith("/") and workdir is None:
                    finding("relative_workdir", "manual", "相对 WORKDIR 依赖基础镜像 cwd。", ref)
                else:
                    workdir = posixpath.normpath(posixpath.join(workdir or "/", value))
        elif cmd == "ENV" and profile == TEACHING:
            for match in re.finditer(r"\b(?:[A-Za-z_][A-Za-z_0-9]*_)?TASK_ROOT(?:=|\s+)([^\s]+)", arg):
                value = match.group(1).strip('"\'')
                if "$" in value:
                    finding("dynamic_task_root", "manual", "TASK_ROOT 环境变量需核实展开结果。", ref)
                elif value != "/workspace":
                    finding("runtime_root_mismatch", "fail", f"TASK_ROOT={value} 与教学 profile 的 /workspace 不一致。", ref)
        elif cmd in ("COPY", "ADD"):
            try:
                flags, sources, destination = copy_args(arg)
            except (ValueError, json.JSONDecodeError) as exc:
                finding("copy_parse", "manual", str(exc), ref)
                uncertain_copy = True
                continue
            operation = {"instruction": cmd, "line": lineno, "sources": sources, "destination": destination, "resolved_sources": []}
            out["copy_operations"].append(operation)
            if any(flag.startswith("--from") for flag in flags):
                finding("copy_from_stage", "manual", "COPY --from 的源属于阶段或镜像，需核对该阶段文件。", ref)
                uncertain_copy = True
                continue
            if any(not (f.startswith("--chown=") or f.startswith("--chmod=") or f == "--link") for f in flags):
                finding("copy_flags", "manual", "COPY/ADD 的路径相关扩展参数需人工核对。", ref)
                uncertain_copy = True
                continue
            if "$" in destination or (not destination.startswith("/") and workdir is None):
                finding("dynamic_copy_destination", "manual", "复制目标依赖变量或基础镜像 cwd。", ref)
                uncertain_copy = True
                target = None
            else:
                target = posixpath.normpath(posixpath.join(workdir or "/", destination))
            if len(sources) > 1 and not destination.endswith("/"):
                finding("multiple_copy_destination", "fail", "多来源 COPY/ADD 的目标必须以 / 结尾。", ref)
            for source in sources:
                if "$" in source or source.startswith(("http://", "https://", "git@")):
                    finding("dynamic_copy_source", "manual", "COPY/ADD 来源为变量或远程资源，需核实。", ref)
                    uncertain_copy = True
                    continue
                parts = PurePosixPath(source).parts
                if source.startswith("/") or ".." in parts:
                    finding("copy_source_escape", "fail", f"来源必须相对 build context 且不得越界：{source}。", ref)
                    continue
                if profile == TEACHING and any(p in ("reference", "expert_evidence", "optimization_evidence") for p in parts):
                    finding("private_copy_source", "fail", f"不得复制 Reference 或专家/优化证据：{source}。", ref)
                    continue
                matches = list(context.glob(source)) if any(c in source for c in "*?[") else [context / source]
                matches = [p for p in matches if p.exists()]
                if not matches:
                    finding("copy_source_missing", "fail", f"{source} 在 build context={out['build_context']} 下不存在；不可按 Dockerfile 所在目录猜测。", ref)
                    continue
                available = []
                for local in matches:
                    if not local.resolve().is_relative_to(context.resolve()):
                        finding("copy_symlink_escape", "fail", f"复制来源链接越出 build context：{source}。", ref)
                        continue
                    if ignored(local.relative_to(context).as_posix()):
                        finding("copy_source_ignored", "fail", f"COPY/ADD 来源被 dockerignore 排除：{source}。", ref)
                        continue
                    available.append(local)
                    operation["resolved_sources"].append(relative(local))
                if target is None:
                    continue
                for local in available:
                    if cmd == "ADD" and local.is_file() and re.search(r"\.(tar|tgz|tar\.gz|tar\.bz2|tar\.xz)$", local.name):
                        finding("add_archive", "manual", "ADD 会解包归档，需人工核对归档成员目标。", ref)
                        uncertain_copy = True
                    elif local.is_dir():
                        for child in local.rglob("*"):
                            if not child.resolve().is_relative_to(context.resolve()):
                                finding("copy_symlink_escape", "fail", f"复制目录内链接越界：{relative(child)}。", ref)
                            elif child.is_file() and not ignored(child.relative_to(context).as_posix()):
                                copied.add(posixpath.join(target, child.relative_to(local).as_posix()))
                    else:
                        is_dir = destination.endswith("/") or len(available) > 1 or len(sources) > 1
                        copied.add(posixpath.join(target, local.name) if is_dir else target)
        elif cmd in ("ENTRYPOINT", "CMD"):
            try:
                tokens = json.loads(arg) if arg.startswith("[") else shlex.split(arg)
                if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
                    raise ValueError("command must contain strings")
                if "$" in arg or any(" " in t for t in tokens):
                    finding("dynamic_runtime_command", "manual", "启动命令包含变量或内嵌 shell，需沿调用链核对路径。", ref)
                runtime_commands.extend((token, ref) for token in tokens if token.startswith("/") and (token.endswith((".sh", ".py")) or token == tokens[0]))
            except (ValueError, json.JSONDecodeError):
                finding("runtime_command_parse", "manual", "启动命令无法静态解析。", ref)
        elif cmd == "UNKNOWN":
            finding("unknown_instruction", "manual", "无法解析 Dockerfile 指令。", ref)
    out["observed_workdir"] = workdir
    if profile == TEACHING and workdir != "/workspace":
        if workdir is None and (multi_stage or any(f["code"] in ("dynamic_workdir", "relative_workdir") for f in out["findings"])):
            pass
        else:
            finding("workdir_mismatch", "fail", f"最终 WORKDIR 必须为 /workspace，实际为 {workdir or '未声明'}。", out["dockerfile"])
    entries = config.get("entrypoint", {})
    entries = entries if isinstance(entries, dict) else {}
    required = {"test_script": "tests/test.sh"}
    if profile == TEACHING:
        required["solution_script"] = "solution/solve.sh"
    for name, value in entries.items():
        if name.endswith("_script"):
            if name in required and value != required[name]:
                finding("canonical_entry_mismatch", "fail", f"{profile} 固定入口 {name} 必须是 {required[name]}，声明为 {value}。", relative(task / "task.toml"))
            required[name] = value
    for key, value in required.items():
        config_ref = relative(task / "task.toml")
        if not isinstance(value, str) or "$" in value:
            finding("dynamic_entry", "manual", f"{key} 不是静态路径。", config_ref)
            continue
        if value.startswith("/") or ".." in PurePosixPath(value).parts:
            finding("entry_path_escape", "fail", f"入口必须为 task 根目录相对路径：{key}={value}。", config_ref)
            continue
        if not (task / value).resolve().is_relative_to(task):
            finding("entry_path_escape", "fail", f"入口链接越出 task 根目录：{key}={value}。", config_ref)
            continue
        if not (task / value).is_file():
            finding("entry_missing", "fail", f"入口文件不存在：{value}。", config_ref)
            continue
        if profile == TEACHING:
            expected = posixpath.join("/workspace", value)
            if expected not in copied:
                status = "manual" if dynamic_filesystem or uncertain_copy or complex_ignore or multi_stage else "fail"
                finding("runtime_entry_unmapped", status, f"入口 {value} 未被静态映射到 {expected}；需核实复制或生成链路。", out["dockerfile"])
    for path, ref in runtime_commands:
        if profile == NATIVE and (path.startswith("/tests/") or path.startswith("/solution/")):
            folder, remainder = path.lstrip("/").split("/", 1)
            if not (task / folder / remainder).is_file():
                finding("runtime_command_missing", "fail", f"启动命令引用的 Harness 注入文件不存在：{path}。", ref)
        elif path.startswith("/workspace/") and path not in copied:
            status = "manual" if dynamic_filesystem or uncertain_copy or multi_stage else "fail"
            finding("runtime_command_unmapped", status, f"启动命令路径未被复制到最终镜像：{path}。", ref)
    if not out["findings"]:
        finding("paths_consistent", "pass", "静态路径与所选 profile 一致；未构建镜像、未验证适配器或运行成功。", out["dockerfile"])
    return finish()
