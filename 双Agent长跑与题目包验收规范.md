# 双 Agent 长跑与完整题目包校验、运行、监护规范

更新：2026-10-06。本文是工作区双 Agent 长跑的唯一通用执行规范，覆盖题包预检、受管部署、双流启动、监护、异常接管、证据回收和最终交付。科学协议见 [全流程指引](AutoResearch出题全流程指引.md)，文件治理见 [脚本与目录治理](notes/长程Agent脚本与目录治理.md)，压缩包验收见 [最终交付闭环](notes/最终打包与交付闭环.md)。GPU 操作须先读 [正确使用指南](tools/gpu_scheduler/USAGE.md)，核对 [公共部署进展](ops/gpu_scheduler/进展清单.md)；未部署的新协议不能仅靠客户端生效。旧部署手册改为跳转入口，历史复盘只保留事实，不再作为新运行指南。

沿用本题进展清单中的有效授权、预算和停止条件，不重复审批。编写规范、安装工具或准备配置不构成启动正式研究、付费、上传或提交授权。

## 1. 强制工具与职责

“官方工具”指本工作区受管源码及经过校验的发布版本，不表示外部平台认证。**新建、恢复或迁移的正式双 Agent 长跑必须使用以下工具，不得自建同功能控制器、GPU 队列、轮询守护脚本或私有运行器替代。**

| 职责 | 受管入口 | 边界 |
|---|---|---|
| Agent 启停、续轮、上下文交接、硬截止、独立 guard | [research_handoff](tools/research_handoff/README.md)：`controller.py` | 每组独立 run-id、配置、状态和退出证据；本地 CLI 控制执行主机常驻进程 |
| GPU 准入、持久排队、等待、取消与执行器对账 | [gpu_scheduler](tools/gpu_scheduler/README.md)：CLI、`Client` / `RemoteClient` | GPU 训练/评分/复验统一入队，默认阻塞 `submit`；已执行不重跑，状态透传 `gpu.state` |
| 主机资源、活动登记、只读巡检与归档 | [gpu_monitor](tools/gpu_monitor/README.md)：`monitor.py` / `registry.py` | 只读聚合已登记的官方 run/job；不启动、不恢复、不重试研究；run 和 job 仍由各自官方工具查询 |
| 模型研究、Harbor 与可信评分 | 实际可用的模型/Harness、Harbor 和题目薄适配器 | 只连接模型、合法候选、评分、事件、计时和资源取消，不复制控制状态机 |
| 内容和材料 QA | [总体 QA](materials/autoresearch-qa-skills/autoresearch-task-qa/SKILL.md)、[Baseline QA](materials/autoresearch-qa-skills/autoresearch-baseline-quality/SKILL.md) | 使用时先读 Skill，按真实接口只读检查；静态通过不代替动态运行 |

禁止用题目自建 `supervisor.py`、`research_controller.py`、无限循环、`nohup` 加定时 kill 替代控制合同，也不能直接启动训练绕过 GPU 队列。既有服务管理器或 tmux 可以托管官方 `serve` 进程，但不另实现调度、重试、预算或回收。CPU 任务仍用任务控制器，不虚构 GPU 请求。

先复用已有适配器；通用缺口在 `tools/` 唯一源码中修复、验证并生成新发布。未完成目标环境验收的能力列为启动阻塞项，不用临时脚本宣称满足。运行中源码不热覆盖，历史发布、旧包和失败证据不删除。

## 2. 冻结运行声明

直接维护本题已有进展清单和私有配置，不增设同义审批表。下表是信息要求，不是工具 JSON Schema；具体字段以当前工具示例、源码和 CLI 为准。

| 分类 | 必须记录 |
|---|---|
| 授权 | 阶段、两组模型、资源/费用上限、原绝对截止、停止条件、续轮与失败重试范围 |
| 版本 | task/release/protocol、公开题包/Starter/Prompt、数据和模型哈希、三个工具发布哈希、适配器版本 |
| 双流 | 各自 run-id、provider/model ID、推理档位、上下文容量、受限身份、独立候选/会话/输出 |
| 资源 | 主机/数据盘设备、GPU UUID、调度 session/root、CPU/RAM/线程/磁盘、专用运行时与评分并发 |
| 时间 | 每流有效目标、原墙钟硬截止、模型请求/完整 GPU 执行/单轮/清理上限；durable 排队按 deadline 准入；有时限 GPU 服务的剩余寿命 |
| 接管 | 实际启动/查询/停止/恢复命令、认证引用、状态/exit/日志、job/container 归属与 cleanup |
| 证据 | 原始事件、逐轮源码/评分、时间区间、模型重载、健康记录、回收位置与哈希清单 |

两组默认分别为 Codex + GPT-5.6 Sol、Codex + Seed 2.1 Turbo，按当前项目要求核验实际 provider 映射，不静默换模型。Seed 是模型名，不是训练随机 seed。两组使用相同公开起点、任务哈希、协议、Prompt 和资源口径，不能继承读过 Reference 的专家上下文。

每流默认至少 10h 有效研究，每个长任务/阶段墙钟硬上限不超过 12h。正式配置 `budget.hard_limit_seconds <= 43200`，不启用 `allow_extended_hard_limit`；单轮执行不超过工具上限 5400 秒或父级剩余预算。GPU 排队/对账不计单轮执行时间，但不延长 run 原墙钟硬截止；完整评分须同时满足轮内与 GPU 执行合同，不能入队后才发现时限冲突。不得用换 run-id 重置原阶段预算。持久 GPU 服务无服务级截止，各 job 仍受原截止；有时限服务须核对剩余寿命，不为此重启共享服务。

## 3. 完整题目包预检顺序

长跑前冻结完整可运行的公开题包；最终专家三目录在真实研究后补齐。尚未产生的轨迹和 best_method 标为待生成，不能填造。

| 顺序 | 校验内容 | 通过依据 |
|---|---|---|
| P1 科学内容 | G01 方法空间、G02 合理 Baseline、G03 充分提升；全正式 seed 成对、预算公平、锚点事前固定 | 原始指标、可复算比较、训练模型和新进程重载；短测不能顶替 |
| P2 结构接口 | 平台 Schema、必需文件、相对路径、哈希、唯一 Docker/Harbor profile；题面/API/guard/资源一致 | 文件白名单、解析、路径和实际入口检查 |
| P3 隔离评分 | 白名单公开快照、受限身份、镜像层/构建 context/挂载/缓存/环境/Git 历史 | 实际 solver 身份负向访问探针；Reference、Hidden、专家材料、另一组历史和密钥不可见 |
| P4 环境资源 | 数据盘全链路、现有占用、三个工具、资源限额和取消能力 | 设备、工具状态、真实目标设备正常/越限和定向清理证据 |
| P5 完整运行 | 干净构建 → Starter → 完整 Public/Dev → reward → 退出回收；私有 Harness 另验 Reference | 真实 Harbor Trial 与版本绑定；nop 不证明模型研究链路 |
| P6 模型与故障 | 每模型响应正常终结、工具调用、候选编辑、评分；单流 STOP、超时、失联、上下文交接、外部清理 | 无变化的已有工具测试可复用；新增适配在目标环境核验 |
| P7 正式启动 | 两份冻结配置、独立 run-id、预算/原截止、活动登记与回收路径 | 接管记录完整后按有效授权启动；探针不计研究时间 |

verifier 和评分输入由可信侧控制，候选不能改评分器、标签、计时或 reward。清除旧结果，由可信 verifier 原子写 `/logs/verifier/reward.txt` 或数值 `reward.json`。合法负分、Hard Gate、格式、超时、资源越限和基础设施故障分别记录，不信任候选自报分数。

H01–H04 必查，有运行材料核对 H05–H06。要求 H06 时，同一 Trial 留 `config.json`、`result.json`、优先 reward 文件及非空 `trial.log` 或 `verifier/test-stdout.txt`；核对版本、配置、正常结束、日志调用链和 rewards 逐值一致。不能拼不同 Trial；reward 文件并存时按目标 Harbor 实际优先级对账。

## 4. 数据盘、隔离与 GPU 合同

核对实际挂载设备、GPU UUID/进程及 CPU/RAM/磁盘余量。任务数据、模型、日志、发布、临时目录和缓存写已核验数据盘；同时检查 Docker data-root/exec-root、containerd root/state/snapshotter、socket、daemon 日志、可写层、Harbor jobs、TMPDIR 和 pip/HF/Torch/XDG/npm 缓存。仅改 Docker data-root 不够。公共服务仍写系统盘时使用数据盘独立运行时，不重启公共服务。

同主机各流使用同一受管 GPU 服务，全局并发以实际已部署配置的 `max_running` 和资源准入为准（工具范围 1-32，兼容默认 2），每作业一张物理 GPU。不将“两条研究流”或目标并发数当作全局硬上限或吞吐保证。会话数、模型请求数、CPU 评分数和 GPU 作业数分别配置；多题不得另开队列绕过上限。只读公开资产可共享，候选、会话、写缓存、计时及停止范围须隔离。

共享准入依据实测峰值、余量、计算/CPU 负载和公平预算；核验 `external_process_policy`，允许共享时显式采用受管 `shared` 策略。其他 PID 出现本身不要求停训；资源风险、明确干扰、用户停止或硬截止只处理自己的范围。

资源预约与执行硬限分别验收：GPU 显存/CU 是准入预约，RAM 用 `ram_mib` 预约、`memory_max_mib` 总内存硬限、`memory_high_mib` 总内存节流，CPU 用 CPUQuota；durable 生产经 systemd/cgroup v2 实际核验，Docker 主容器/sidecar 共用作业 slice。cache 计入 memory.max，实测 anon 来自采样；GPU 显存仍无硬隔离。题面如声明显存硬上限，须由不可修改的宿主/provider 边界执行并留越限证据；allocator、CUDA_VISIBLE_DEVICES、Docker RAM 限额或监控采样不能冒充显存硬限额。旧版本/未接入的 provider 不凭文档宣称已执行新 CPU/RAM 合同。

GPU 客户端可操作同服务其他作业，不是 solver 权限边界；socket、宿主 SSH、Docker socket、上游密钥和管理员权限仅留可信侧。solver 经限定的题目工具，由可信适配器校验候选、路径及资源后提交。

Docker/Harbor 必须通过已验收的官方 provider、容器归属注册及同 job slice 约束接入；任意外部 daemon/远端 provider 没有自动通用取消保证。目标环境真实清理未验收时不能直接提交会脱离管理的命令。任务控制器 cleanup hook 本身不等于 GPU 作业已回收，GPU 释放确认前不得归还其预约；确认不足则 UNKNOWN 并隔离对应卡。

### GPU 容器设备注入硬合同

所有 Codex 研究 main 容器和可信独立 verifier，只要声明 GPU，都必须通过 `tools.research_handoff.providers.harbor_docker:ManagedDockerEnvironment` 并使用 `gpu_attachment="cdi"`。官方 provider 默认值也是 `cdi`；`device-request`、Docker `--gpus`、Compose `reservations.devices` 的 NVIDIA legacy hook 路径禁止用于正式 GPU 容器。provider 在启动前拒绝显式 legacy 值，不能在 CDI 不可用时自动回退；若题包或目标环境不能提供 CDI，启动应阻塞并修复/验收 provider，而不是换注入方式继续跑。

接入验收必须同时覆盖研究 main 与独立 verifier：生成的 Compose/OCI 配置含 `nvidia.com/gpu=<scheduler UUID>`，没有 legacy DeviceRequests/`reservations.devices`，并设置 `NVIDIA_VISIBLE_DEVICES=void`；启动后 `docker inspect`、NVML 和全新 CUDA 进程探针逐项通过，回执记录角色、容器 ID、唯一 GPU UUID、设备注入模式和源码/配置哈希。只看配置文件、只测研究 main、只看到 `nvidia-smi` 或仅有目录/pytest 通过，都不能证明 verifier 和 reload 路径已受 CDI 保护。动态验收应在同值 Docker 资源更新后再次启动新 CUDA 进程；结果失败按基础设施接入失败处理，不产出科学分数。

## 5. 受管部署与双流启动

实际参数按工具 README、示例和 CLI 核验。下列命令从工作区根运行，`<...>` 必须替换为真实配置或 ID，不是可原样执行的脚本。

公共 GPU 服务 plan/release/config 放 `ops/gpu_scheduler/`；先只读核验运行版本，不复制单题事故 plan 为公共新入口。安装/切换按该阶段授权与空闲窗口或明确迁移合同执行；生成或上传 release 不启动服务。旧 v4 正常停止会取消队列并结束作业，不能将普通 stop 当作无损迁移。

```text
# GPU 主机：校验；复用已有服务，仅服务不存在且获授权时启动 serve
python3 -B tools/gpu_scheduler/cli.py validate --config <gpu-config.json>
python3 -B tools/gpu_scheduler/cli.py serve --config <gpu-config.json>

# 本地查询，各工具连接/认证配置不能互换
python3 -B tools/gpu_scheduler/cli.py status --remote <gpu-remote.json>
python3 -B tools/gpu_monitor/monitor.py validate --config <tasks.json>
python3 -B tools/gpu_monitor/monitor.py status --config <tasks.json> --json

# 新不可变控制器发布只部署一次；两组各自 init/start
python3 -B tools/research_handoff/controller.py --remote <connection.json> deploy
python3 -B tools/research_handoff/controller.py --remote <connection.json> init --config <agent-config.json> --run-id <run-id>
python3 -B tools/research_handoff/controller.py --remote <connection.json> doctor --run-id <run-id>
python3 -B tools/research_handoff/controller.py --remote <connection.json> start --run-id <run-id> --background
python3 -B tools/research_handoff/controller.py --remote <connection.json> status --run-id <run-id>
```

`deploy` 仅部署控制器，数据、适配器、模型工具和运行时另行准备；已有校验发布直接复用。本地执行按 README 使用 `--state-dir`；正式运行保留 guard 和数据盘要求，不用 `--no-guard` 或 GPU `--local-test` 冒充正式验收。

每次评分先冻结稳定 `request_id`（task/run/group/evaluation/候选版本）、原 spec、候选/协议哈希，再用 `submit`。durable 请求传父级原 `deadline_at`/`deadline_epoch` 和完整 `max_runtime_seconds`，不设置 queue_timeout；保存当前 session、不可变 `origin_session_id`、job ID、原目录和回执。`on_update` 透传 `gpu.state`，排队/对账留原轮 WAITING_GPU，不计等待信用、不耗 retry；墙钟截止不变。外层工具先返回会话编号时继续同一调用。多任务编排/进度跟踪才用 `submit_async` / `enqueue` 加官方 `wait`，不得自写 ensure_job/轮询/重提循环。

控制器 `ACCEPTED` 只证明接管，GPU `SUCCEEDED` 只证明进程退出和清理，不等于评分或研究通过。启动后核对两流各自真实方法闭环、完整评分、guard、原截止和健康记录，再交接后台信息；失败如实报告。

## 6. 监护、重试与异常接管

controller/guard 执行预算和保护，GPU 服务管理作业，monitor 默认每 60 秒合并只读采集。记录采样间隙、最后进展和有界日志尾部，不重复拉完整模型；停止采集不停止研究。新控制器不能套用旧 supervisor 的 Sol/Seed 状态字段或 STOP 路径，适配缺失保留未知，用官方 `status/watch/doctor` 查实际状态。

长时间 Agent 的统一监护入口是：

```text
python3 -B tools/gpu_monitor/monitor.py watch --interval 60 --max-hours 12
```

该视图只聚合登记表中可回查的 `research_handoff` `run_id` 与 `gpu_scheduler` `job_id`/`request_id`，不会自动发现未登记任务。停止任务时仍须调用对应官方 `controller stop` 或 scheduler `cancel`，并把上述轮询命令告知接管者。`marker`、`process_groups` 和其他自定义兼容任务语义已删除；旧登记必须先修订并通过 `validate`，不能用监控器恢复旧停止路径。

完成登记和只读查询后，使用官方后台采集入口；按实际范围指定 `--task`，查询和停止采集时使用同一范围。采集寿命不代替训练硬截止。

```text
python3 -B tools/gpu_monitor/monitor.py maintain --config <tasks.json> --interval 60 --max-hours 12
python3 -B tools/gpu_monitor/monitor.py monitor-status --config <tasks.json>
python3 -B tools/gpu_monitor/monitor.py stop-monitor --config <tasks.json>
```

| 维度 | 必看证据 |
|---|---|
| 身份/生命周期 | run/attempt、PID 启动身份、容器 ID/StartedAt、exit、cleanup、guard |
| 资源/增长 | GPU UUID/归属/显存，主机及 cgroup RAM/OOM，CPU/线程、磁盘/inode、日志/模型/FD/PID 增长 |
| 研究 | 当前轮、最后模型/工具/评分、排队原因、有效区间、剩余目标与原截止 |
| 请求 | 实际模型、请求 ID、完整终结、延迟、错误、退避、费用/调用量 |

这是核验要求，不宣称 monitor 已自动覆盖全部字段；缺项由可信适配器补采，未采到写未知。心跳须反映真实模型/工具/评分进展；SDK 不代发研究心跳。durable 等待时以真实 gpu.state 记录 WAITING_GPU，控制器按等待合同监督并保留墙钟截止；RUNNING 仍需真实进度，不能用假心跳掩盖卡死。

**重试按现有能力声明：** 任务控制器对可恢复单轮失败默认退避重试，不设次数上限，但受原目标和墙钟截止约束；目前公开 `policy.retry_backoff_seconds`，不得编造 `max_retries` 等字段。正式授权须覆盖此策略，并核验请求总窗口、费用和无进展边界；本题要求更严次数/错误分类而尚未实现时，先补受管能力再启动。终态恢复和新实验不能由 monitor 自动执行。

模型请求重试和控制器续轮分别留证，避免多层隐式重试相乘。认证、模型或协议错误应定位配置，持续失败不算研究。每次失败 attempt 独立保留；重试前核对 GPU/外部资源已回收，不重放结果未知的已执行操作。

GPU 排队不属于普通 Agent 失败重试。`INFEASIBLE`/`EXPIRED` 回到当前 Agent 决策，不重开轮次或重复原请求；可继续 CPU 工作，改变实验必须符合原协议/预算并登记新意图。未解决 UNKNOWN、session/对账故障单列 `failure_class: infrastructure`；适配器退出后控制器暂停，不能声称已自动恢复 worker 或改实验规模。

| 事件 | 处理 |
|---|---|
| SSH 断线、等待超时 | 查原 run/request/job；只结束本次等待，不等于取消/失败，不重提变更请求 |
| QUEUED/STARTING、UNKNOWN 对账中或 session 变更 | 官方 SDK 续读同一作业，透传 gpu.state，留在原轮 WAITING_GPU；不计等待信用、不耗 retry |
| INFEASIBLE/EXPIRED、未解决 UNKNOWN | 留预测/退出证据，当前 Agent 做基础设施决策或暂停；原终态请求不重排，不重置原截止 |
| 单流停止/故障 | 官方 `controller stop` 定向停止该流；依登记 job `cancel`，不停止同伴或共享服务 |
| controller 丢失 | 查 guard/attempt/exit 和真实进程；确认旧控制器死亡、资源处理完成后，按有效授权 `recover` / `start --resume` |
| 上下文将满 | 报真实 token，保留本流摘要/transcript，由官方 compact/reopen 管理 generation，不重置信用或截止 |
| OOM、挂载/磁盘异常、清理失败 | 留证、核归属、按合同停止自己的范围；未确认回收不继续，不擅改正式协议 |
| 硬截止/预算耗尽 | 停止、回收、记录缺口；不延时、不改目标、不自动换 run 续命 |

```text
python3 -B tools/research_handoff/controller.py --remote <connection.json> stop --run-id <run-id> --reason <真实原因>
python3 -B tools/gpu_scheduler/cli.py get --remote <gpu-remote.json> --session <session-id> --request-id <request-id>
python3 -B tools/gpu_scheduler/cli.py cancel --remote <gpu-remote.json> --session <session-id> --id <job-id> --reason <真实原因>
python3 -B tools/research_handoff/controller.py --remote <connection.json> doctor --run-id <run-id>
```

durable GPU 服务使用数据盘持久账本，异常重启原位恢复 QUEUED 的 request/job、submitted_at、bypasses 和截止；已启动执行恢复 UNKNOWN 按原执行器/scope/container/exit 对账，绝不重跑。评分始终绑定 origin_session_id。旧 v4 仍为旧队列合同，重启不能套用新恢复保证；先核实际版本并保全旧证据。任何版本“查无作业”都不能证明旧执行已退出；不删账本/换 root 绕过故障，不隐式换模型或追加预算。

## 7. 双轨迹、有效时间与 best_method

各流保存 transcript、方法快照、请求/job/评分索引、失败和时间区间。控制器轮、请求 attempt 与研究轮可以不同，但须有映射和计数解释，重试次数不能充作方法轮数。

QA16 按每组区间去重求和，排除排队、安装、构建故障、长阻塞及无研究的请求失败；方法设计、实现、正常训练/评估和分析须有事件证据。`active_seconds` / `credited_seconds` 不自动证明科研有效。可用 `credit_policy: reported` 和非空 `credit_evidence` 接入审计计时，每轮只报新增信用；与 `run_summary.json` 对账，两组不能合并凑 10h。

训练/微调任务没有 7h 例外。完全无训练且单轮很短时，每组至少 7h 的例外还须有每条至少三个真实方法闭环、连续演进与未耗尽方向、平台时间记录、best_method 干净复验及完整论证。未满足写未完成，不把空睡/存活时间算研究。

轨迹采用顶层 `rounds` 数组，每轮含 `round`、`policy_name`、`method_summary`、`status`、`score`、`failure_reason`、`retained_best`、`time`。摘要去空白非空；未成功出分为 `null`；失败如实说明尝试，纯运行器/请求失败明确“未执行方法改动”。时间注明时区，round 连续且与汇总对账；不改真实状态/失败原因，不用 Reference 补轨迹。

结束后按同一完整协议选唯一有效 best_method，绑定来源轮次和源码哈希；从干净公开快照替换合法 solution 完整复跑，保留真实分数、质量门及适用模型/重载证据。专家改算法不能仍称原 Agent 最佳方法，Reference 不能顶替。

## 8. 回收、打包与最终门禁

官方状态和退出证据须确认两流、GPU job、外部容器及子进程结束，cleanup 成功且资源释放。只关自己的资源，单题结束不停止共享 GPU 服务。按清单增量回收日志、源码、评分、模型、计时/健康、配置及哈希；校验后归档活动登记。UNKNOWN 或残留进程不能当完成。

题目运行原件、私有配置和事故档案留在题目私有目录；跨题 GPU 服务发布/plan/源配置及运维证据留在公共 `ops/gpu_scheduler/`，不随清理单题目录删除。不塞公开镜像或随意增加最终成员。从已质检原件生成新副本，按平台白名单严格保留 `workspace/`、`expert_evidence/`、`optimization_evidence/` 及要求内容；旧包和失败证据保留。

- [ ] G01–G03、全 seed 正式成对、模型重载和评分锚点有证据。
- [ ] 官方三工具版本与运行记录可查，无自建替代控制链；资源限额和取消能力按真实合同验收。
- [ ] 双流隔离，Reference/Hidden/密钥无泄漏，verifier 可信且候选不可修改。
- [ ] 双轨迹、QA16 独立区间、QA21 摘要/计数、唯一 best_method 干净复验成立。
- [ ] Harbor H01–H04 及适用 H05–H06 对账，动态材料绑定实际题包版本。
- [ ] 实际运行无未处理的 OOM/泄漏/卡死/异常退出/资源耗尽，健康观察如实留证；专家提交前不要求连续 12h soak。
- [ ] 三目录、必需文件/空目录、JSON/相对路径/权限、哈希与 ZIP 成员严格一致。
- [ ] 全套 ZIP 空目录安全解压核验；受影响构建/Trial 已补验，包外索引指向实际检查版本。
- [ ] 资源回收或明确交接，原件和失败未删，所有适用缺项列为未完成。

静态 QA、Harbor 接入、专家自验、平台最终验收分别记录。文档/索引修改只做相关静态检查；训练器、数据、预算、评分或资源合同变更补受影响实验。只有真实平台结论才能写平台通过；上传和提交沿用有效授权边界。
