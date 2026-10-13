# g03_conductor

出题前 G03 准备阶段的确定性编排器。设计依据见 [g03_conductor 设计](../../notes/g03_conductor设计.md)，本文是使用、扩展和验收的入口。

它的定位是专家侧证据工具：自动完成 Baseline 诊断、可行性预检、协议冻结、探索、正式确认和 G03 判定，产出 `comparison_summary.json`（v2）。它不是正式双 Agent 长跑，产物不能当作 best_method；正式长跑仍走 `research_handoff` / `gpu_scheduler` / `gpu_monitor`。

## 1. 组成

| 位置 | 职责 |
|---|---|
| `tools/g03_conductor/`（Python，仅 stdlib） | 状态机、协议校验、哈希锁、统计判定、账本、关卡、CLI。所有业务判定只在这里 |
| `extensions/g03-conductor/`（pi 扩展，TS 薄壳） | 注册 4 个工具、门控、`agent_settled` 驱动、reviewer 关卡、小组件、`/g03`。说明见 [扩展 README](../../extensions/g03-conductor/README.md) |
| `~/.pi/agent/agents/g03-{worker,reviewer}.md` | 子代理角色；reviewer 定义文件的 sha256 在 `init` 时写入账本 |
| `<私有区>/g03/` | `ledger.jsonl`、`protocol` 草案、`lock.json`、`control.json`、`candidates/`、`frozen/`、`runs/`、`reviews/`、`final/`。不进 Starter、镜像或 git |

入口统一为 `python3 -I tools/g03_conductor <cmd> --root <私有区>`（目录形式，`-I` 隔离下仍能找到包）。输出始终是一个 JSON 对象，`ok=false` 时带 `code`；未预期异常返回 `code=INTERNAL`，不输出裸 traceback。

## 2. 启动一次 G03 准备

1. 准备私有区：评分器、Baseline 目录、数据清单放在私有区内，公开 Starter 放在私有区之外（两者不能互相包含）。
2. 初始化，GPU 预算和截止就是事前授权，运行中不再询问：

   ```text
   python3 -I tools/g03_conductor init --root <私有区> --task-id <task> \
     --gpu-hours <H> --deadline <ISO8601含时区> \
     --executor gpu_scheduler --scheduler-root <已核验调度根> --python /usr/bin/python3 \
     --authorization "<授权原文或来源>"
   ```

   `--executor local_cpu` 只用于空转/CPU 任务。`--reviewer-cmd` 只用于测试和空转，正式运行用 pi reviewer。
3. 在 tmux 中以交互模式启动 pi（小组件需要交互模式）：

   ```text
   G03_ROOT=<私有区> pi --no-extensions -e ~/.pi/agent/extensions/subagent -e extensions/g03-conductor
   ```

4. 主会话按 conductor 下发的阶段指令工作；到 DONE 或 STOP_UNMET 时扩展写最终记录并退出。

接入 GPU（P4）前必须按 [GPU 调度器正确使用指南](../gpu_scheduler/USAGE.md) 核对已部署版本，并取得本题授权；文档和本工具不授予启动权限。

## 3. 状态机与工具

`S0 立题 → S1 Baseline 诊断 → S1_5 可行性预检 → S2 冻结 → S3 Baseline 正式 → S4 探索 ⇄ S5 正式确认 → S6 判定 → DONE | STOP_UNMET`，旁路状态 `WAITING_GPU`、`BLOCKED_INFRA`、`PAUSED`。

| 阶段 | LLM 可用 | conductor 自动做 |
|---|---|---|
| S0 | `g03_propose(kind=protocol)` | schema 与确定性规则校验 |
| S1 | `g03_run(kind=diag)`、`g03_note(tag=diagnosis)` | 诊断齐全后强制 reviewer（G02），固定 Baseline 哈希 |
| S1_5 | `g03_propose(kind=candidate)`、`g03_run(kind=dev)` | 窗口与 Δ_dev 判定、reviewer |
| S2 | 无 | 计算并写入 `lock.json`、reviewer |
| S3 | 无 | 自动跑 `formal_b`，用正式 σ_B 复核窗口 |
| S4 | `g03_propose(kind=candidate)`、`g03_run(kind=screen)` | 筛选判定、reviewer 审实现 |
| S5 | 无 | 冻结候选（`frozen/` 只读），自动跑 `formal_r` 并独立重载复评分；跑完前不显示中间统计 |
| S6 | 无 | 判定、生成 summary、终审；FAIL 时 k+1 并换下一组正式 seed |

任何阶段都可以用 `g03_propose(kind=defect|infeasible)` 提交缺陷或不可行声明，由 reviewer 在下一关卡审查后路由到类型 B（回 S1）或类型 C（回 S0，最多 1 次）。没有阶段转移工具；非法转移由 Python 拒绝。

## 4. CLI 速查

| 命令 | 用途 |
|---|---|
| `status [--detail]` | 阶段、窗口、预算、允许的工具、小组件行；`--detail` 加时间线和候选表 |
| `next` | 驱动一步，返回 `continue` / `review` / `auto_run` / `paused` / `blocked` / `wait_expert` / `terminal` |
| `propose --kind ...` | 协议草案（`--file -` 读 stdin）、候选目录、声明 |
| `run --kind ... [--method vN] [--seed N]... [--mult X]` | 唯一实验入口；正式 kind 只能由 `--auto` 在 S3/S5 运行 |
| `review-run --gate G --subject S` | 执行 reviewer 并记录结论 |
| `note --tag T --text X` | 写 ASI / 负结果 / 诊断说明 |
| `control --action pause\|resume\|stop` | 写 `control.json`，下一关卡生效 |
| `signoff --kind deterministic --expert 名字 --note 说明` | 仅 CLI：σ_B=0 时专家签字 |
| `verify` | 账本哈希链与运行回执一致性检查 |
| `allowed --tool T [--kind K]`、`guard` | 扩展门控用 |

## 5. 不变量

- `ledger.jsonl` 是唯一真源，追加写、哈希链校验；篡改会以 `LedgerCorrupt` 拒绝所有命令。作废和回退只追加记录，不删除证据。
- request_id 确定性：`{task}-p{major}-{kind}-{method}[xM].{h8}-{seed}`（h8 由候选、执行命令和预算哈希得到；xM 只在诊断倍数预算时出现）。已有有效回执的运行不会重提；`BLOCKED_INFRA` 恢复后复用原 request_id。
- 锁定文件（协议、评分器、Baseline、数据）每次运行前校验哈希。bash 写入无法拦截，靠哈希检测，属于检测而非防止。
- gpu_scheduler 路径：阻塞 `submit` 并透传 `on_update`，`deadline_at` 用 `init` 的原截止，不设 `queue_timeout_seconds`。`QUEUED/STARTING` 记 `WAITING_GPU`；`INFEASIBLE/EXPIRED/UNKNOWN/CANCELLED` 记 `BLOCKED_INFRA`，不消耗 k。
- 预算（GPU 小时或墙钟）耗尽、K 次确认用完、修订超限或用户 stop，都进入 STOP_UNMET，交由专家决定。

## 6. 扩展点

| 需求 | 改哪里 |
|---|---|
| 新协议字段或校验规则 | `protocol.py`（`validate`、S1 可变字段集）并补 `tests/test_protocol.py` |
| 统计或判定规则 | `stats.py`；golden 测试 `tests/test_stats.py` 对齐 `materials/example/optimization_evidence/comparison_summary.json` |
| 新关卡或审查上下文 | `gates.py`（`GATES` 表）与对应 `stages.py` 的 `eval_*` |
| 新执行后端 | `executors.py` 实现 `execute(plan_path, plan, on_state)`，在 `make_executor` 注册，并在 `init` 放行 |
| 小组件或 `/g03` 内容 | `views.py`（TS 只负责显示） |

规则改动属于工具版本变化：先改代码和测试，再在 [设计文档](../../notes/g03_conductor设计.md) 记录。进行中的题目账本不热改。

## 7. 验证

```text
python3 -m pytest -q tools/g03_conductor/tests           # 需要 pytest（本机用独立 venv）
python3 -I tools/g03_conductor/examples/dry_run.py --scenario done   # P3 空转：DONE
python3 -I tools/g03_conductor/examples/dry_run.py --scenario stop   # P3 空转：STOP_UNMET（K 用完）
extensions/g03-conductor/test/run.sh                     # P2：mock ExtensionAPI 驱动两条路径
```

空转任务位于 `examples/fake_task/`，是合成指标的 CPU 任务，不触碰 GPU；`frozen/` 目录是只读的，清理临时目录前需要 `chmod -R u+w`。
