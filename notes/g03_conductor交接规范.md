# G03 初始化 Agent 与主 pi 交接规范

更新：2026-10-13。适用范围：初始化题目资产并移交给 [g03_conductor](../tools/g03_conductor/README.md) 做专家侧 G02/G03 准备。本文定义首轮交接，不启动正式双 Agent 长跑、不产生 best_method。

**初始化 Agent 负责把资产、协议草案、账本和启动命令准备好，停在 S0；用户只执行一行带首条消息的主 pi 命令。** 主 pi 先核对交接材料，首轮结束后由现有 `agent_settled → next` 驱动进入 S1，随后在原授权内自动推进。三模型按职责分时调用，不是三个会话同时对话。

## 1. 交接边界与职责

| 角色 | 交接前/后的职责 | 停止边界 |
|---|---|---|
| 初始化 Agent | 理解任务与方法空间，准备可运行的 Baseline、可信 train/score 入口、数据、公开 Starter；起草协议；核验环境；执行 init、propose(protocol)、status、verify；生成私有交接材料和完整启动命令 | 首次交接停在 S0，已有 schema 合法草案；尚未执行 next、diag/dev/screen/formal 或 reviewer |
| 主 pi | 首轮只读核对交接；其后按 conductor 指令诊断 Baseline、提出方法、派发 worker、调用 g03_run、分析和记负结果 | DONE / STOP_UNMET 时结束；paused / blocked / wait_expert 时交由专家处理 |
| g03-worker | 接收主会话给出的具体假设、目录和验收条件；核对/实现 Baseline 或候选，执行必要的 CPU 技术检查 | 本次实现完成即返回主会话，不自行推进阶段或另开 GPU 实验 |
| g03-reviewer | conductor 在关卡通过 review-run 强制启动；只读审查实现、公平性、证据和泄漏 | 返回机器可解析结论，不修改文件或运行实验 |
| 用户 | 提供任务目标及研究/模型/GPU 预算授权，运行准备好的唯一启动命令 | 正常阶段无需逐步确认；专家签字、未知基础设施状态或授权缺失时介入 |

初始化 Agent 可以把必要的 train/score 薄适配补到可执行状态，但不能先实现 Reference、挑选最优候选或自行宣布 G02/G03 通过。既有专家研究笔记可私有引用并说明来源，不能转成公开 Agent 的答案线索。Baseline 的训练充分性和公平性仍由 S1 的真实诊断及 reviewer 判断。

## 2. 开始准备前确认什么

先读本题进展清单、[全流程指引](../AutoResearch出题全流程指引.md)、[工具 README](../tools/g03_conductor/README.md)、[协议模板](../tools/g03_conductor/examples/protocol.example.yaml) 和 [目录治理](长程Agent脚本与目录治理.md)。沿用有效授权，不重复审批；没有本题路径、预算、截止或模型调用授权时记录缺口，不能填造。

必须在**主 pi 与 conductor 实际执行的主机**上完成预检，命令中的路径都是该主机绝对路径：

- pi/Node 可执行且版本、CLI 参数与扩展兼容；官方 subagent 扩展及两个 user-scope 角色文件存在。用 `pi --help`、`pi --list-models` 核验主模型及 worker/reviewer 模型的可解析 ID，认证沿用受控配置，不把密钥放 argv。
- 主模型按 [设计](g03_conductor设计.md) 配置为 `anthropic/claude-opus-5-5`、thinking high；worker 为 `openai/gpt-6.1-sol:xhigh`，reviewer 为 `openai/gpt-6-astra:xhigh`。模型不可用是准备阻塞，不静默替换。角色定义在 `~/.pi/agent/agents/g03-{worker,reviewer}.md`；reviewer 文件在 init 后不再修改。
- 数据、解释器、依赖、Baseline、train/score 入口确实存在。评分器接受 checkpoint，在**新进程实际加载模型**后评分并写有限数值 JSON；只有重新调用进程不保证真的重载，须核查实现。
- 私有区与 Starter 互不包含；私有区在 Git 工作树内时必须被忽略且没有已追踪私有内容；构建 context、COPY、公开挂载/环境/历史中没有 Reference、Hidden、专家证据或密钥。
- 任务数据、日志、模型、pi 会话、TMPDIR 和新增缓存写已核验数据盘。远端容器任务另核 Docker/containerd 根、可写层和日志，不能仅设置 TMPDIR 就宣称迁移完成。

GPU 模式还必须阅读 [正确使用指南](../tools/gpu_scheduler/USAGE.md) 与 [接口 README](../tools/gpu_scheduler/README.md)，从根 AGENTS.md 指定的公共 ops 进展清单核对目标机器实际服务、客户端与源码版本。缺少部署记录不能凭 README 推定已接入。

当前 `SchedulerExecutor` 使用本机 `Client(scheduler_root)`，没有接受 `--remote` 的 G03 执行器。主 pi、G03 CLI、runner、数据和 scheduler socket 必须在能访问同一组路径的执行主机上；远端 GPU 场景应把交接完成在该 GPU 主机，并在启动说明中指明先登录哪台机器。不要把本地 root、远端连接 JSON 或远端 Python 路径混进同一 init。

NVIDIA 容器任务必须核验 main 与可信 verifier 的官方 CDI 注入合同；无 CDI 或独立 CUDA/reload 回执时记为接入阻塞。GPU 显存/CU 是预约而非硬隔离，CPU/RAM 硬限和资源画像按真实调度合同填写。此次准备不允许重启公共服务、重写调度器或热改运行发布。

## 3. 准备资产与协议草案

建议布局如下；沿用已有等价目录也可以，所有实际路径写入交接材料：

```text
<任务目录>/
  进展清单.md                         # 授权、状态和证据索引
  starter/                            # 公开起点；不包含 private/
  private/
    baseline/                         # 合理正式对照实现
    scorer/                           # 固定训练/评分入口及其全部受保护依赖
    data/                             # 数据与清单
    ops/g03_handoff/
      handoff.md                      # 初始化事实和接管边界
      main.md                         # 主 pi 持续角色指令
      protocol.draft.yaml             # 待冻结草案
      preflight/                      # 非秘密检查输出与哈希清单
      launch.txt                      # 一行真实启动命令
      tmp/                            # 临时目录，检查结束可清理
    g03/                              # init 创建；conductor 管理
```

不要手写 `g03/ledger.jsonl`、`lock.json`、`control.json`、`protocol.yaml` 或伪造运行回执。初始化事实放 `ops/g03_handoff/`，不与 conductor 自己管理的路径混用。

起草 `protocol.draft.yaml` 时核对：

- task_id 与 init 一致；指标方向明确；U 有未观察 Reference 结果前的依据。
- dev、screen 各不少于 3 个唯一整数 seed；formal_pool 至少 K 组，组大小相同且不少于 3（推荐 5）；所有集合及正式各组互不重叠。
- Baseline/候选使用共同 budget_per_run；默认诊断倍数为 2，工具会对 dev 第一 seed 跑倍数诊断，标准预算覆盖全部 dev seed。预算须留出标准诊断、倍数诊断、预检、筛选、正式配对及重载的总费用与墙钟余量。
- train/score 用 argv 数组；train 引用 `{method_dir}` 和 `{out_dir}`，score 引用 `{checkpoint}` 与 `{score_out}`；checkpoint glob 相对 out_dir 且恰好匹配一个模型。
- result.metric_key 与 score JSON 一致；正式 train、score、rescore 共用 max_runtime_seconds，必须留足重载时间。
- paths.private 等于 root；Starter 独立；scorer 列表包含全部可信实现依赖。数据用真实文件/目录哈希；目录哈希按 `tools.g03_conductor.hashing.tree_hash`，不能用文件 sha256sum 代替目录算法。
- GPU 模式填写 gpu_spec_template。当前 executors.py 的 SLACK_SECONDS=120：调度 job 上限在单次预算外另加非正式 120 秒、正式 240 秒；倍数诊断先放大 budget_per_run。完整 job 不超过 43200 秒且能在原截止前完成。工具发布变化时以该发布的 job_runtime 为准。
- 协议 YAML 使用工具支持的映射、列表和标量；不要添加 schema 不支持的主机/认证/交接字段。机器差异放受校验部署配置，交接文字放 handoff.md。

参考可复制的 [交接模板](../tools/g03_conductor/examples/handoff.example.md) 和 [主 pi 指令模板](../tools/g03_conductor/examples/main.example.md)。模板复制到本题私有区后必须填写真实值，不能保留占位符交付用户。

## 4. 初始化 Agent 必须事先执行的命令

下列 `<...>` 是说明用占位符，由初始化 Agent 填好并实际执行。用户不负责执行这些准备命令。命令从核验过的工具发布根运行，输出与退出码存到私有 preflight/；不抓取完整环境变量或认证配置。

**先判断是否已有账本。** 如存在 `g03/ledger.jsonl`，先 status/verify 并核对真实任务、授权、截止、活动进程和原 request/job，不重复 init，不换 root/ID 重置预算。已有运行按原账本接管，不能冒充本规范的“首次 S0 就绪”。

新 GPU 题目初始化：

```bash
python3 -I -B tools/g03_conductor init --root '<私有区绝对路径>' --task-id '<task-id>' --gpu-hours <已授权H> --deadline '<原绝对截止，含时区>' --executor gpu_scheduler --scheduler-root '<目标机已核验调度根>' --python '<训练评分解释器绝对路径>' --pi-path '<pi绝对路径>' --reviewer-agent '<g03-reviewer.md绝对路径>' --authorization '<授权原文或来源>'
```

纯 CPU 任务把 executor 改为 `local_cpu` 并移除 scheduler-root；CPU 空转可用示例假 reviewer，真实科学任务不使用 `--reviewer-cmd` 代替独立审查。init 必须有正预算和未来截止；截止不超过本阶段已授权窗口及 12h 墙钟上限。绝对截止从 init 起消耗，等待用户启动也不会自动延长。

提交草案只做 schema/确定性规则校验，**不推进阶段、不启动实验**：

```bash
python3 -I -B tools/g03_conductor propose --root '<私有区绝对路径>' --kind protocol --file '<私有区绝对路径>/ops/g03_handoff/protocol.draft.yaml'
python3 -I -B tools/g03_conductor status --root '<私有区绝对路径>' --detail
python3 -I -B tools/g03_conductor verify --root '<私有区绝对路径>'
```

验收上述真实 JSON，而非只看 shell 返回码：`ok=true`，status.stage 为 `S0`、draft_id 非空、无 paused/blocked/terminal；verify 无 problems。查初始账本没有 run_start/run/review/next 的 nudge 记录，且没有本题活动进程或未知 GPU 请求。status/verify 不会推进阶段；`next` 会转移和写账本，不能拿它当只读预检。

写好 handoff.md、main.md、launch.txt，记录 init/propose 回执、源码版本/哈希、协议和资产哈希、reviewer 哈希及现有部署证据。交接模板的状态只是准备时快照；运行后状态以账本为准，不能拿准备时 ledger 哈希要求后续正常追加内容保持不变。

可复用同版本的已有技术检查。必要时做 RPC 离线加载检查（不调用模型、不执行 next），确认扩展加载、/g03 和小组件注册：

```bash
printf '%s\n' '{"id":"1","type":"get_commands"}' | G03_ROOT='<私有区绝对路径>' G03_PYTHON='<conductor解释器>' PI_OFFLINE=1 timeout 20 '<pi绝对路径>' --mode rpc --no-session --no-extensions -e '<受管工具发布根>/extensions/g03-conductor'
```

真实模型/工具调用烟测只能在已有相应授权内做；未验证写“未验证”，不能把 list-models、静态检查或假 reviewer 的 PASS 当三模型真实联通。检查失败保留日志并修复，全部启动必要项完成后再交付一行命令。

## 5. 用户唯一启动命令

初始化 Agent 必须根据实际主机生成**一行全部已替换占位符的命令**。下行是结构示例：

```bash
cd '<私有区绝对路径>' && G03_ROOT='<私有区绝对路径>' G03_PYTHON='<conductor解释器绝对路径>' TMPDIR='<私有区绝对路径>/ops/g03_handoff/tmp' '<pi绝对路径>' --model anthropic/claude-opus-5-5 --thinking high --no-extensions -e '<用户agent目录>/extensions/subagent' -e '<受管工具发布根>/extensions/g03-conductor' --session-dir '<私有区绝对路径>/g03/pi-sessions' --append-system-prompt '<私有区绝对路径>/ops/g03_handoff/main.md' '开始本题G03接管：先读ops/g03_handoff/handoff.md，调用g03_status(detail=true)，只读核对任务、授权、草案与准备证据。首轮只做接管核对；一致则结束本轮，让扩展按账本驱动下一步。差异则使用/g03 pause对应的CLI暂停并报告，不启动实验。'
```

这条命令把**首条用户消息**随 CLI 一起交给主模型。`--append-system-prompt` 保留持续角色规则，`--session-dir` 把主会话放私有数据盘，绝对扩展路径避免依赖当前目录。不要用 `-p`、JSON/RPC 模式替代交互 TUI，也不要恢复读过其他题 Reference 的旧会话。默认新会话；同一私有区只运行一个主驱动。

**当前源码的 session_start 仅刷新小组件，不能只给裸 pi 启动命令。** 带消息的首轮完成后，`agent_settled` 才调用 next；已提交草案会由 Python 从 S0 转到 S1，再发送诊断指令。后续关卡 review 与 auto_run 直接执行，continue 作为 follow-up 发给主模型。无需新增启动扩展、后台循环或私有控制器。

普通流程不需用户再输入“开始”、手动 `next` 或每阶段确认；这是在模型、授权、资产和部署已就绪的前提下成立。用户只负责在说明中的执行主机、交互终端（建议 tmux）运行该行。终端会显示阶段、预算、窗口和最近事件；`/g03` 可查看详细状态。

## 6. 主会话与 worker 的调用边界

主会话先 read handoff.md 和所引用的任务说明，再调用 g03_status。协议已经是草案而非锁定结果；主会话可按 S0/S1 合法字段审阅修订，但只能通过 g03_propose，不直接写 conductor 文件。

主会话使用官方 subagent 的 user scope 派发 `g03-worker`，示意调用：

```json
{"agent":"g03-worker","agentScope":"user","cwd":"<私有区绝对路径>","task":"当前为S4；实现候选vN，唯一可写目录为<g03/candidates/vN绝对路径>。先读该目录CANDIDATE.md及指定接口文件，按假设实现并记录改动。只做CPU技术检查；不要启动GPU训练/评分、直接调用scheduler或修改账本/协议/评分器/数据/Baseline。完成后汇报文件、命令与退出码，交回主会话调用g03_run(kind=screen,method=vN)。"}
```

subagent 子进程不自动继承父进程通过 `-e` 加载的 G03 工具；不能要求它调用不存在的 `g03_run`。当前 worker 定义的“GPU 只走 scheduler”规则需要由主提示进一步收紧：**本流程所有实验统一由主会话调用 g03_run，worker 不自行提交 GPU job。** 独立 reviewer 由 conductor review-run 启动，无需主会话再派一次 reviewer。

guard 是可加载的角色/路径门控，锁哈希是篡改检测；官方 subagent 的当前调用没有显式加载 guard，不能宣称 worker 已受文件系统沙箱保护。主会话必须限定读写范围，可信 scorer 与公开/私有隔离须按实际环境验收，不用提示词代替可信 verifier。

## 7. 停止、异常与完成后交接

- 首轮发现交接与实际账本/资产不一致，主会话调用官方 CLI `control --action pause --reason ...`，报告差异并结束本轮；扩展下一次 next 应返回 paused，不能单靠口头“暂停”阻止自动驱动。
- `/g03 pause|resume|stop [原因]` 在下一检查点生效；不会即时取消正在阻塞调用的 GPU 作业。必要时按原 job/request 用官方 scheduler cancel 定向停止，只操作本题，不停止公共服务。
- Esc 只中止当前 Agent 轮次，不等于取消已提交 GPU job。确认原 request/job 状态后使用 `/g03 resume`；UNKNOWN、INFEASIBLE、EXPIRED 不换 ID 自动重跑、不延期。
- σ_B=0 时会 wait_expert；专家通过 CLI `signoff --kind deterministic --expert ... --note ...` 签字，再 `/g03 next` 继续。reviewer 连续无法判断、stalled、锁异常等 paused 状态处理后用 `/g03 resume`。
- 截止、GPU 总预算、K 或协议修订额度耗尽，或用户 stop 后，工具进入 STOP_UNMET。主会话不自行重启、延长或进入打包/正式双 Agent；DONE 也只代表本次 G03 证据通过。
- 扩展进入终态会 shutdown，不能假定主模型还有额外一轮更新文档。接管专家/Agent 根据真实 `g03/final/final_record.json`、`comparison_summary.json`（成功时）、运行回执、模型重载和 reviewer 报告更新题目进展清单；旧失败证据保留。

长 GPU 作业使用官方 job/request 身份登记并提供只读观测命令：

```bash
python3 tools/gpu_monitor/monitor.py watch --interval 60 --max-hours 12
```

monitor 只聚合已登记的官方身份，不能把 pi PID 或临时 marker 冒充 research_handoff run_id；G03 不虚构正式长跑 run。任务控制和定向取消仍由 conductor/官方 scheduler 完成，不让 monitor 自动启动或恢复研究。

## 8. 初始化 Agent 交付验收

交付给用户的内容控制在：**当前进展清单入口、私有交接文件路径、一行启动命令、剩余原截止及必要限制。** 同时必须有下列事实证据：

1. 目标/方法接口明确，可运行 Baseline 和可信 checkpoint 重载评分链路已核查，数据和依赖可用。
2. 主/worker/reviewer 角色、模型 ID、CLI 和版本可核验；所有路径、缓存与会话位置已准备。
3. init/propose/status/verify 成功；新题停在 S0，有合法草案，无实验/reviewer/活动请求。
4. 私有/公开和可信 verifier 边界核验通过；未验证项有说明，不从目录齐全推断隔离成功。
5. 授权、预算、原截止、资源和目标机部署可查；不包含秘密，不重置预算。
6. 启动命令含首条消息和持续主提示，所有占位符已替换、路径正确引用；用户无需执行初始化命令或另发首条提示。

缺少启动必要条件时完成能做的准备，并在进展清单写“初始化待补证/阻塞”，不宣称“一行即可正常研究”。本文和模板是通用流程授权边界，本次编写文档不会自动启动任何题目实验或模型调用。
