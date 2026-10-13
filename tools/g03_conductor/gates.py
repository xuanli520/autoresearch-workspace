"""Mandatory reviewer gates (design §2, §8). The conductor spawns the reviewer; its verdict is one gate condition only."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .engine import G03Error, _atomic_write, _frontmatter
from .hashing import sha256_bytes, sha256_file

MAX_REVIEW_ATTEMPTS = 2
VERDICT_RE = re.compile(r"G03_VERDICT:\s*(\{.*?\})\s*$", re.M)
DEFECTS = {"implementation", "protocol", "candidate", "method", "none"}

GATES = {
    "S1_baseline": ("G02 Baseline 合理性", [
        "Baseline 实现健全、来源可解释、适配任务；无错误实现或刻意弱化",
        "训练充分性：标准预算与加倍预算诊断的差异是否说明标准预算已基本收敛或合理",
        "诊断证据（命令、退出码、日志、score.json）真实存在且与 ledger 记录一致",
        "协议草案的数据、评分器、seed、U 及其事前依据是否合理；U 不能依据已观测结果设定"]),
    "S1_5_feasibility": ("可行性预检", [
        "候选在开发 seed 上的提升来自真实方法改动，而非改评分器、读取 hidden/测试数据或泄漏",
        "Baseline 与候选使用同一预算、数据和评测命令；Baseline 未被修改",
        "可行窗口与 Δ_dev 的确定性统计与原始 score.json 一致"]),
    "S2_lock": ("协议冻结", [
        "lock.json 中协议、评分器、Baseline、数据哈希与实际文件一致",
        "冻结的协议在出正式结果前确定；seed 三组互不重叠；U 有事前依据",
        "私有区（Reference、证据、ledger）不在 Starter/公开路径内，不会进入镜像或 git"]),
    "S4_candidate": ("候选实现审查", [
        "候选目录未改动评分器、数据或 Baseline；没有读取筛选/正式 seed 的评测标签或 hidden 资产",
        "没有针对 seed 的特判、硬编码分数或绕过训练（作弊）",
        "预算与 Baseline 相同；实现与 CANDIDATE.md 描述一致",
        "筛选统计与原始 score.json 一致"]),
    "S6_final": ("终审：证据与泄漏", [
        "comparison_summary.json 与每个 seed 的 receipt/score/rescore 原始数值逐一一致",
        "全部正式 seed 保留、成对、同协议；每个 checkpoint 独立重载复评分通过",
        "Reference（冻结候选）与私有证据没有泄漏到 Starter/公开快照；verifier/评分器可信且未被改动"]),
    "S6_fail": ("确认失败归因", [
        "确认失败是否仅因方法不足（类型 A），还是存在评分器/Baseline/数据缺陷（implementation）或协议不可行（protocol）",
        "全部正式 seed 已跑完且未提前停止"]),
    "S6_invalid": ("正式结果无效归因", [
        "无效的正式运行由候选自身失败导致（candidate/method），还是基础实现缺陷（implementation）或协议问题（protocol）"]),
    "invalid_runs": ("无效运行归因", [
        "Baseline 运行无效的根因：实现缺陷（implementation）、协议/预算不可行（protocol），或可忽略的偶发问题（none）"]),
    "lock_violation": ("锁定文件变更", [
        "锁定文件的变更是否是对真实缺陷的合法修复（PASS 并标 implementation）；否则判 FAIL 并要求恢复到锁定哈希",
        "变更是否可能为了让候选达标而修改评分器/Baseline/数据"]),
    "defect_claim": ("实现缺陷声明", [
        "主会话声称的评分器/Baseline/数据缺陷是否真实存在（PASS 将生成新版本并作废受影响证据）"]),
    "infeasible_claim": ("协议不可行声明", [
        "当前协议在预算下是否确实无法区分 Baseline 与可行改进（PASS 将消耗唯一一次协议修订）"]),
    "C_revision": ("类型 C 协议修订", [
        "修订理由成立；Baseline 训练预算与实现未被削弱",
        "U 仅按事前依据变动且没有向已观测 R 移动；正式 seed 全部更换",
        "修订没有引入事后挑 seed 或放宽判定"]),
}


def parse_verdict(text: str) -> dict | None:
    matches = VERDICT_RE.findall(text or "")
    if not matches:
        return None
    try:
        v = json.loads(matches[-1])
    except ValueError:
        return None
    verdict = str(v.get("verdict", "")).strip()
    v["verdict"] = {"PASS": "PASS", "FAIL": "FAIL", "无法判断": "UNDECIDABLE", "UNDECIDABLE": "UNDECIDABLE"}.get(verdict)
    if v["verdict"] is None:
        return None
    v["defect"] = v.get("defect") if v.get("defect") in DEFECTS else "none"
    return v


def final_text_from_events(stdout: str) -> tuple[str, str | None]:
    """Extract the last assistant text and model from pi --mode json output (or plain text fallback)."""
    text, model = "", None
    saw_json = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        saw_json = True
        msg = ev.get("message") if isinstance(ev, dict) else None
        if ev.get("type") == "message_end" and isinstance(msg, dict) and msg.get("role") == "assistant":
            parts = msg.get("content") or []
            if isinstance(parts, str):
                text = parts
            else:
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
            model = msg.get("model") or model
    return (text if saw_json else stdout), model


class Gates:
    def gate_status(self, gate: str, subject: str) -> tuple[str, dict | None]:
        revs = [r for r in self.st.reviews if r["gate"] == gate and r["subject"] == subject]
        if not revs:
            return "pending", None
        last = revs[-1]
        if last["verdict"] in ("PASS", "FAIL"):
            return last["verdict"].lower(), last
        if len(revs) >= MAX_REVIEW_ATTEMPTS:
            return "exhausted", last
        return "pending", last

    def review_prompt(self, gate: str, subject: str, context: dict) -> str:
        title, checklist = GATES[gate]
        lines = [
            f"你是 G03 conductor 在关卡「{gate}：{title}」强制调起的独立审查者。",
            f"任务：{self.cfg['task_id']}；阶段：{self.st.stage}；协议版本：p{self.st.major}；私有区：{self.root}",
            "只读审查：只能用 read/grep/find/ls 和只读 bash（cat、ls、sha256sum、git log/diff/show、python3 -I 只读解析）。"
            "不得修改任何文件，不得启动训练、评分或 GPU 作业。",
            "", "## 审查清单", *[f"- {c}" for c in checklist],
            "", "## 确定性证据（由 conductor 计算，可对照原始文件复核；统计判定以脚本为准）",
            "```json", json.dumps(context, indent=1, ensure_ascii=False, default=str)[:20000], "```",
            "", f"ledger: {self.dir / 'ledger.jsonl'}；运行目录：{self.dir / 'runs'}；候选：{self.dir / 'candidates'}",
            "", "## 输出格式",
            "按 Verdict / Critical / Warnings / Evidence Checked 汇报，最后一行必须严格为：",
            f'G03_VERDICT: {{"gate": "{gate}", "subject": "{subject}", "verdict": "PASS|FAIL|无法判断", '
            '"defect": "implementation|protocol|candidate|method|none"}',
            "defect 含义：implementation=评分器/Baseline/数据缺陷；protocol=协议不可行；candidate=候选实现作弊/泄漏/错误；"
            "method=方法本身不足；none=无缺陷。",
        ]
        return "\n".join(lines)

    def request_review(self, gate: str, subject: str, context: dict) -> dict:
        prompt = self.review_prompt(gate, subject, context)
        psha = sha256_bytes(prompt.encode())
        reqs = [r for r in self.st.review_requests if r["gate"] == gate and r["subject"] == subject]
        attempt = len([r for r in self.st.reviews if r["gate"] == gate and r["subject"] == subject]) + 1
        if not reqs or reqs[-1]["attempt"] != attempt:
            path = self.dir / "reviews" / f"{gate}-{subject[:12]}-a{attempt}.prompt.md"
            _atomic_write(path, prompt)
            self.log("review_request", gate=gate, subject=subject, attempt=attempt, prompt_sha256=psha,
                     prompt_path=str(path.relative_to(self.dir)), stage=self.st.stage)
        return {"action": "review", "gate": gate, "subject": subject, "title": GATES[gate][0],
                "message": f"关卡 {gate}（{GATES[gate][0]}）需要 reviewer 审查"}

    def _reviewer_argv(self, prompt_file: Path, sys_file: Path, task: str) -> tuple[list[str], dict]:
        rv = self.cfg["reviewer"]
        env = dict(os.environ)
        env["G03_ROOT"] = str(self.root)
        env["G03_ROLE"] = "reviewer"
        guard = Path(__file__).resolve().parents[2] / "extensions" / "g03-conductor" / "guard" / "index.ts"
        if rv.get("argv"):
            subs = {"{task_file}": str(prompt_file), "{system_prompt_file}": str(sys_file), "{model}": rv.get("model") or "",
                    "{root}": str(self.root)}
            argv = []
            for a in rv["argv"]:
                for k, v in subs.items():
                    a = a.replace(k, v)
                argv.append(a)
            return argv, env
        pi = rv["pi_path"]
        env["PATH"] = str(Path(pi).resolve().parent) + os.pathsep + env.get("PATH", "")
        argv = [pi, "--mode", "json", "-p", "--no-session", "--no-extensions", "-e", str(guard),
                "--model", rv["model"], "--tools", rv["tools"], "--append-system-prompt", str(sys_file), task]
        return argv, env

    def review_run(self, gate: str, subject: str) -> dict:
        with self.locked():
            self.reload()
            reqs = [r for r in self.st.review_requests if r["gate"] == gate and r["subject"] == subject]
            done = [r for r in self.st.reviews if r["gate"] == gate and r["subject"] == subject]
            if not reqs or reqs[-1]["attempt"] != len(done) + 1:
                raise G03Error(f"no pending review request for {gate}/{subject}", code="NO_PENDING_REVIEW")
            req = reqs[-1]
            prompt_file = self.dir / req["prompt_path"]
            if sha256_file(prompt_file) != req["prompt_sha256"]:
                raise G03Error("review prompt was modified", code="TAMPERED")
            rv = self.cfg["reviewer"]
            agent = Path(rv["agent_file"])
            if not agent.is_file() or sha256_file(agent) != rv["agent_sha256"]:
                raise G03Error(f"reviewer agent definition changed since init: {agent}", code="TAMPERED")
            _, body = _frontmatter(agent)
            with tempfile.TemporaryDirectory(prefix="g03-review-") as tmp:
                sys_file = Path(tmp) / "reviewer.md"
                sys_file.write_text(body)
                task = "Task: " + prompt_file.read_text()
                argv, env = self._reviewer_argv(prompt_file, sys_file, task)
                try:
                    proc = subprocess.run(argv, cwd=str(self.root), env=env, capture_output=True, text=True,
                                          timeout=rv.get("timeout_seconds", 3600))
                    stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
                except subprocess.TimeoutExpired as exc:
                    stdout, stderr, code = exc.stdout or "", "reviewer timeout", None
                    stdout = stdout.decode() if isinstance(stdout, bytes) else stdout
                except OSError as exc:
                    stdout, stderr, code = "", f"spawn failed: {exc}", None
            base = self.dir / "reviews" / f"{gate}-{subject[:12]}-a{req['attempt']}"
            _atomic_write(base.with_suffix(".transcript.jsonl"), stdout)
            _atomic_write(base.with_suffix(".stderr.log"), stderr or "")
            text, model = final_text_from_events(stdout)
            _atomic_write(base.with_suffix(".md"), text)
            v = parse_verdict(text)
            verdict, defect, why = "UNDECIDABLE", "none", None
            if code != 0:
                why = f"reviewer exit={code}"
            elif v is None:
                why = "no machine-readable G03_VERDICT line"
            elif v.get("gate") != gate or v.get("subject") != subject:
                why = "verdict gate/subject mismatch"
            elif rv.get("model") and model and not rv.get("argv") and model not in rv["model"]:
                why = f"unexpected reviewer model {model}"
            else:
                verdict, defect = v["verdict"], v["defect"]
            rec = self.log("review", gate=gate, subject=subject, attempt=req["attempt"], verdict=verdict, defect=defect,
                           problem=why, exit_code=code, model=model, stage=self.st.stage,
                           report=str(base.with_suffix(".md").relative_to(self.dir)),
                           report_sha256=sha256_file(base.with_suffix(".md")),
                           transcript_sha256=sha256_file(base.with_suffix(".transcript.jsonl")))
            return {"ok": True, "gate": gate, "verdict": verdict, "defect": defect, "problem": why, "seq": rec["seq"]}
