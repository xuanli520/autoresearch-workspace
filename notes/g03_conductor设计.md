# g03-conductor 设计：pi 全自动 G03 探究编排

状态：设计已确认（2026-10-12）；P1–P3 已实现并验收（2026-10-13），P4 执行器合同已用 fake Client 测试，真实 gpu_scheduler 接入待核对部署版本并取得本题授权。使用与扩展见 [tools/g03_conductor/README.md](../tools/g03_conductor/README.md)。

## 1. 定位与边界

- 用途：出题包之前的必备准备。自动完成 Baseline 诊断、可行性预检、协议冻结、探索、正式确认、G03 判定，最后自动停止。它不是正式双 Agent 长跑，不套用 `research_handoff` 的长跑和 QA16 约束。
- 产出：专家侧的 G02/G03 证据和 `comparison_summary.json`（`autoresearch-comparison-summary.v2`），Reference 方法也包括在内。产出不能当作 best_method 使用。
- 核心原则：阶段推进、统计、停止判定只由确定性 Python 代码决定。LLM 只在当前阶段内工作，不能推进、跳过或回退阶段。
- GPU 只走 `tools/gpu_scheduler` 的阻塞 `submit`。
- 已废弃：aqm、pi-autoresearch。

## 2. 角色

| 角色 | 模型 | 职责 | 工具 |
|---|---|---|---|
| 主会话 | `anthropic/claude-opus-5-5`，thinking high | 提议方案，在阶段内做决策，派发任务给子代理 | conductor 工具加 subagent，不直接写实验代码 |
| `g03-worker` | `openai/gpt-6.1-sol:xhigh` | 实现、研究、实验、测试 | 全部默认工具 |
| `g03-reviewer` | `openai/gpt-6-astra:xhigh` | 在关卡处做审查和判断 | read、grep、find、ls、bash（只读） |

- 角色定义在 `~/.pi/agent/agents/g03-*.md`，子代理通过官方 subagent 示例扩展（`~/.pi/agent/extensions/subagent/`）调起。
- astra 的审查由 conductor 在关卡处强制触发，主会话不能跳过。审查的结论只是关卡条件之一，不能替代统计判定。
- 不做文件系统隔离。锁定文件靠运行前的哈希校验来发现篡改，属于检测，不是防止。

## 3. 目录

| 位置 | 内容 | git |
|---|---|---|
| `extensions/g03-conductor/` | pi 扩展（TS 薄壳）：工具门控、关卡调度、TUI 小组件、`/g03` 命令 | 追踪（需在 `.gitignore` 放行） |
| `tools/g03_conductor/` | Python：状态机、协议 schema 与校验、哈希锁、统计、判定、账本、CLI | 追踪 |
| `tools/g03_conductor/tests/` | pytest，用 `materials/example/optimization_evidence/comparison_summary.json` 作 golden | 追踪 |
| `<题目私有区>/g03/` | `protocol.yaml`、`lock.json`、`ledger.jsonl`、`control.json`、`candidates/vN/`、`runs/`、`reviews/`、pi 会话 | 不追踪，不进 Starter/镜像 |

TS 层不实现业务逻辑，所有判定通过 `python3 -I -B tools/g03_conductor <cmd> --root <私有区>` 调用（目录入口，`-I` 隔离下可用），输出 JSON 后由 TS 解析。

## 4. 状态机

```
S0 立题 → S1 Baseline 诊断 → S1_5 可行性预检 → S2 冻结 → S3 Baseline 正式
   → S4 探索 ⇄ S5 正式确认 → S6 判定 → DONE | STOP_UNMET
旁路状态：WAITING_GPU、BLOCKED_INFRA、PAUSED
```

| 阶段 | 工作内容 | 允许的工具 | 出口条件（脚本判定） |
|---|---|---|---|
| S0 立题 | Claude 起草 `protocol.yaml` | 读写协议草案 | schema 校验通过；三组 seed 互不重叠；正式 n≥3（默认 5） |
| S1 Baseline 诊断 | sol 实现或核对 Baseline，做训练充分性和预算诊断（G02），使用开发 seed | `g03_run(kind=diag)` | 诊断证据齐全，astra 判 PASS；Baseline 代码哈希写入草案 |
| S1_5 可行性预检 | 在开发 seed 上探索 Reference 候选。只能改 Reference 方法和预算，Baseline 已由 S1 固定 | `g03_run(kind=dev)` | 存在候选满足 Δ_dev ≥ (3+m)σ_B,dev，且可行窗口有效（§6）。astra 判 PASS |
| S2 冻结 | 自动计算协议、评分器、Baseline、数据的 sha256，写入 `lock.json` | 无 LLM 写操作 | 校验通过，astra 判 PASS |
| S3 Baseline 正式 | 正式 seed 全部跑完，得到 B、σ_B（n−1） | `g03_run(kind=formal_b)` | 全部 seed 有效；用正式 σ_B 复核可行窗口，不通过判为类型 C（§7） |
| S4 探索 | 循环：Claude 提议 → sol 实现 `candidates/vN/` → 跑筛选 seed → 脚本统计 → Claude 决定下一步 | `g03_run(kind=screen)` | Δ_screen ≥ (3+m)σ_B 且落在窗口内，astra 审实现（评分器未改动、无泄漏、无作弊）后进入 S5 |
| S5 正式确认 | 冻结候选哈希，跑全部正式 seed 与 B 配对，每个 checkpoint 独立重载并复评分。不提前停止 | `g03_run(kind=formal_r)` | 全部 seed 跑完 |
| S6 判定 | 按 §6 规则判定 | 无 | PASS：生成 summary，astra 终审证据与泄漏 → DONE。FAIL：保留为负证据，k+1；若 k<K，换新正式 seed 回到 S3/S4，否则 STOP_UNMET |

- 每次阶段转移都写账本，记录 from、to、reason 和证据路径。
- 非法转移由 Python 抛出拒绝，TS 层只负责透传。
- `WAITING_GPU` 和 `BLOCKED_INFRA` 对应 gpu_scheduler 的 `QUEUED`、`UNKNOWN`、`INFEASIBLE`、`EXPIRED`。这些状态不算失败，不消耗 k，也不改变原截止时间。

## 5. 协议 `protocol.yaml`（S2 锁定）

必填：`task_id`、`metric`（name、direction、score_formula）、`data`（路径和哈希）、`budget_per_run`、`seeds`（`dev`、`screen`、`formal_pool`，三组互不重叠；`formal_pool` 至少包含 K 组 n 个 seed，第 k 次确认使用第 k 组）、`U`（值、`basis` 事前依据）、`margin_sigma`（默认 1）、`K`（默认 2）、`max_protocol_revisions`（固定 1）、`budget_total`（GPU 小时、墙钟截止）、`aggregate_quality_gate`（可选）、`paths`（私有区、Starter、评分器、Baseline）、`gpu_spec_template`（owner、job_class、资源）。

- 启动命令里的 GPU 预算和截止时间就是事前授权，运行中不再询问专家。
- 每次 `g03_run` 生成确定性 request_id：`{task}-{protocol_ver}-{kind}-{cand_ver}-{seed}`。

## 6. 统计与判定（确定性）

maximize 方向下 Δ = R − B，minimize 方向下 Δ = B − R。符号统一后：

```text
B = mean(B_i)      σ_B = sqrt(Σ(B_i − B)² / (n − 1))      R = mean(R_i)
Δ_min = max(3σ_B, 0.15·|U − B|)
Δ_max = min(|U − B| − 3σ_B, 0.8·|U − B|)
窗口有效 ⇔ Δ_max − Δ_min ≥ 1σ_B        # 小于 1σ_B 直接判定不可行
```

| 判定 | 规则 |
|---|---|
| 筛选/预检达标 | Δ ≥ (3 + m)σ_B 且 Δ ≤ Δ_max |
| S6 PASS | Δ ≥ 3σ_B；score(R) ∈ [0.15, 0.8]；\|U − R\| ≥ 3σ_B；validity 全为真（同协议、同 seed 集、重载复评分全部通过、checkpoint 齐全、质量门通过）。3–5σ_B 标“需平台复核”，≥5σ_B 标“强证据” |
| σ_B = 0 | 标为 DETERMINISTIC，不套用 σ 规则，只要求真实改善和归一化区间。需专家签字，conductor 在此处停下等待 |

- 全部正式 seed 保留，不裁剪，不重排。
- 输出 `comparison_summary.json`（v2），附加 `g03_conductor` 段，记录全部协议版本、全部确认尝试及其结果、全部回退理由。

## 7. 失败分类与回退

| 类型 | 识别方式 | 处理 |
|---|---|---|
| A 方法不足 | S6 FAIL，且实现与协议审查无异常 | k+1，换新正式 seed 回到 S3（B 在新 seed 上重跑）后进入 S4。k=K 时 STOP_UNMET |
| B 实现缺陷 | astra 或校验器发现评分器、Baseline、数据有 bug | 生成新版本号，受影响的证据标记作废（保留不删），修复后回到 S1 重新判定 G02，Baseline 重跑。修订次数不计入类型 C 上限 |
| C 协议不可行 | 窗口无效（S1_5 或 S3 复核），或预算下无法区分 | 最多修订 1 次，回到 S0 或 S1。修订须先过确定性规则：Baseline 的训练预算和实现不得削弱；U 只能按事前依据变动，不得向已观测的 R 移动；正式 seed 必须换新。之后由 astra 审查。超出上限则 STOP_UNMET |

- 所有作废和回退都是追加记录，从不覆盖。
- 任一预算（GPU 小时、墙钟）耗尽时进入 STOP_UNMET，报告“未达标/待补证”。
- STOP_UNMET 通常意味着题目或论文本身存在问题，交由专家决定。

## 8. pi 扩展 `extensions/g03-conductor/`

- 启动：初始化 Agent 按 [交接规范](g03_conductor交接规范.md) 事先执行 init/propose/status/verify，停在 S0，生成带首条任务消息、持续主提示与私有会话路径的一行主 pi 命令。必须用交互模式，小组件才能显示。建议放在 tmux 中运行；仅加载扩展的裸 pi 命令仍等待用户输入。
- 注册工具：`g03_status`、`g03_propose`（提交协议草案或候选描述）、`g03_run`（唯一的实验入口，内部调用 Python，再走 gpu_scheduler 阻塞 submit）、`g03_note`（写入 ASI 和负结果说明）。阶段转移没有对应工具。
- 门控：
  - `tool_call` 拦截当前阶段不允许的 conductor 工具。
  - 锁定后，拦截对锁定路径的 `write`/`edit`；bash 写入无法可靠拦截，靠 §1 的哈希校验发现。
- 驱动：
  - `session_start` 只刷新状态，不调用 next。启动命令中的首条消息驱动主会话只读核对交接，首轮结束后才进入下方的 agent_settled 驱动；RPC 加载检查不推进阶段或启动实验。
  - `agent_settled` 时调用 Python `next`，由它返回下一步指令（继续当前阶段、强制审查、转移、终止）。
  - 扩展通过 `sendUserMessage` 下发指令。需要审查的关卡由扩展直接 spawn reviewer 子进程，结论写入 `reviews/`。
- 终止：到达 DONE 或 STOP_UNMET 时写最终记录，然后调用 `ctx.shutdown()`。

## 9. 监控（TUI，只读账本）

- `ctx.ui.setWidget` 常驻显示：
  - 当前阶段与停留时长
  - 可行窗口数轴（B、U、Δ_min/Δ_max、最佳候选位置）
  - 预算（GPU 小时、墙钟、k/K、修订次数 x/1）
  - 最近一条事件
- `/g03`：用 `ctx.ui.custom` 全屏显示阶段时间线、候选表（含负结果）、正式确认逐 seed 进度。正式确认跑完前不显示中间统计。
- `/g03 pause` 和 `/g03 stop` 写入 `control.json`，在下一个关卡生效。界面不能修改协议，也不能推进阶段。

## 10. 实现顺序与验收

1. **P1 Python 核心**：协议 schema、状态机、锁、统计、判定、账本、CLI。pytest 覆盖：
   - 合法与非法转移
   - 窗口计算的边界
   - σ_B=0
   - minimize 与 maximize 两个方向
   - 改动锁定文件后被拒
   - 确认 k 计数和修订上限
   - golden 与示例 summary 的数值一致
2. **P2 TS 扩展**：工具门控、驱动、reviewer 关卡、小组件、`/g03`。
3. **P3 空转联调**：使用 CPU 假任务（合成指标），从头跑完 DONE 和 STOP_UNMET 两条路径，不碰 GPU。
4. **P4 接入 gpu_scheduler**：先核对已部署版本，并获得授权后才进行。

### 实现与验收记录（2026-10-13）

| 阶段 | 结果 | 复现命令 |
|---|---|---|
| P1 Python 核心 | pytest 41 项通过：转移、窗口边界、σ_B=0、双方向、锁定篡改、k/修订上限、golden summary | `python3 -m pytest -q tools/g03_conductor/tests` |
| P2 TS 扩展 | mock ExtensionAPI 驱动 DONE/STOP_UNMET，门控断言全过；pi RPC 实际加载出 `/g03` 与小组件 | `extensions/g03-conductor/test/run.sh` |
| P3 空转联调 | CPU 假任务：good → DONE（强证据），overfit → STOP_UNMET（K_exhausted），`verify` 无问题 | `python3 -I tools/g03_conductor/examples/dry_run.py --scenario done\|stop` |
| P4 gpu_scheduler | fake Client 覆盖 spec（原截止、无 queue_timeout、模板资源）、WAITING_GPU、INFEASIBLE/EXPIRED/UNKNOWN → BLOCKED_INFRA 且复用原 request_id、有效回执不重提 | `tests/test_scheduler_executor.py`；真实服务未接入 |

实现偏差：账本记录的保留字段（seq/ts/at/type/prev/hash）不能用作事件字段，内容哈希统一叫 `content_hash`，控制序号叫 `cseq`；σ_B=0 的专家签字只能通过 CLI `signoff --kind deterministic`。

### 首轮交接补充（2026-10-13）

新增 [初始化与主 pi 交接规范](g03_conductor交接规范.md)、私有 [handoff 模板](../tools/g03_conductor/examples/handoff.example.md) 与 [持续主提示模板](../tools/g03_conductor/examples/main.example.md)。复用 pi 的位置消息参数和 --append-system-prompt，初始化 Agent 预执行合法草案提交，用户只运行一行主 pi 命令。没有修改扩展启动事件或 Python 状态机。worker 通过官方 subagent 实现后交回，实验统一由主会话 g03_run，强制 reviewer 仍由 review-run 执行。

