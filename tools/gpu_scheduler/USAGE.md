# GPU 调度器正确使用指南

更新：2026-10-06。本文是可信控制 Agent、题目薄适配器和接管者的操作规范；字段与 CLI 以 [接口 README](README.md)、配置 Schema 和实际冻结源码为准。正式长跑还须遵循 [工作区统一规范](../../双Agent长跑与题目包验收规范.md)。

## 1. 先核对实际运行版本

`v5-20261006-durable-r3` 已于北京时间 2026-10-07 00:00 按用户授权部署并完成云端 systemd/cgroup 验收；实际版本、会话和证据见 [公共 ops 进展清单](../../ops/gpu_scheduler/进展清单.md)。其他主机仍须核对自己的接口、客户端和运行配置。新版本的 `watch`、`INFEASIBLE`、持久请求账本、资源硬限和 `WAITING_GPU` 不能仅靠更新本地客户端对旧服务生效。不得以阅读本文、上传 release 或文档更新替代部署授权。

首次使用或接管先核对服务 `status`、systemd `ExecStart`、真实 plan/config/source SHA、客户端发布路径和控制器 bundle。匹配已验收版本后才采用本文 durable 合同；未知能力先报告阻塞。部署事实和用户授权只记录在 [公共 ops 进展清单](../../ops/gpu_scheduler/进展清单.md)，不把公共服务版本寄存在单题事故目录。

| 组件 | 必须承担的职责 |
|---|---|
| `gpu_scheduler` | 持久请求身份、资源准入、等待、截止预测、队首预约、执行监督与对账 |
| `research_handoff` | 原 run/轮次、有效时间、`WAITING_GPU`、失败分类、墙钟硬截止与定向停止 |
| 题目薄适配器 | 校验合法候选和完整评分协议，传入冻结声明，透传官方 SDK 状态，解析可信结果及登记资源 |
| `gpu_monitor` | 只读聚合已登记的 run/job/request；不启动、不恢复、不重试研究 |

适配器不得自建 `ensure_job`、固定排队超时、轮询队列、守护器或重提循环。通用能力缺失时修官方工具并验收，通过新不可变发布接入。

## 2. 提交前冻结声明

模型调用、镜像构建、安装和 CPU 分析应在 GPU 作业之外完成；提交范围覆盖真正的训练、评分和重载，不因瞬时利用率低借走未来峰值预约。完整正式协议不得为了凑准入而随意切片或缩短。

| 字段 | 正确填写 |
|---|---|
| `request_id` | 含 task/run/group/evaluation 和候选版本；同一次意图跨断线/session 始终相同 |
| `owner` | 稳定的题目/研究组身份；禁止更换 owner 规避公平调度 |
| `command` / `cwd` | 真实主机 argv 列表和已核验数据盘目录；秘密不得进入 argv、spec 或日志 |
| `deadline_at` 或 `deadline_epoch` | 原父级授权硬截止，两者择一；ISO8601 必须含时区；重连不按“现在+预算”重算 |
| `max_runtime_seconds` | 完整执行的强制上限，最多 43200 秒，并计入启动、评分/重载及必要余量；不得低报以获得回填 |
| `memory_mib` / `compute_units` | GPU 显存 MiB / 1-100 计算预约份额，来自声明与实测；不是显存或 CU 硬隔离 |
| `cpu_cores` / `ram_mib` | CPU/RAM 准入预约；CPUQuota 按 cpu_cores 执行，RAM 总内存硬限另列 |
| `memory_max_mib` / `memory_high_mib` | 总内存硬限 / 总内存节流阈值，high 不超过 max；硬限包含 file cache，禁用 swap |
| `job_class` | 同一 owner 的工作负载版本；方法、数据形状/规模、训练或评分协议变化必须换版本 |

新请求不得设置 `queue_timeout_seconds`；durable 服务仅为旧声明对账兼容解析该字段，忽略其排队截止。正式 run 必须显式传原父级截止，不能依赖服务省略字段时生成的默认窗口。父级预算、轮内执行上限和 GPU 完整执行上限须一致，GPU 入队成功不能覆盖更短的控制器时限。

同 `request_id` 与相同规范化完整声明返回原作业，包括原终态；改参数会被拒绝。保存提交前的原声明，不用校准后的资源快照重建 spec。已终态请求不能修改后“再排一次”。若控制器决定缩小实验，必须把它登记为另一个明确的实验意图，在原轮次/原截止与科学协议允许范围内使用新 ID；不自动重开研究轮或重复原实验。

## 3. 使用官方阻塞调用与事件透传

以下是运行在可信 host adapter 中的接入片段。调用前应完成父级 context/真实心跳协议、候选校验和冻结 spec；`scheduler_root` 来自经校验配置。provider stdout 必须把事件直接交给控制器，不能吞掉或混入评分 JSON。

```python
from tools.gpu_scheduler import Client
from tools.research_handoff.templates.agent_protocol import gpu_state

def run_gpu(spec, scheduler_root):
    client = Client(scheduler_root)

    def on_update(job):
        gpu_state(job, scheduler_root=scheduler_root)

    return client.submit(spec, on_update=on_update)
```

远端控制使用同一发布的 `RemoteClient(remote_config)`，继承相同 `submit` / `wait(on_update=...)` 合同；云端 RPC 指向已核验 release 的 CLI。认证仅引用本地受管 `auth.txt`，不得复制到 release 或公开候选。两端配置不可互换，路径中的 argv/cwd 指执行主机。

`submit` 默认等待终态；外层工具若返回子进程/会话编号，继续等待同一调用，不能当训练已完成。普通单任务不必设置客户端 `timeout`；设置时它只结束这次等待，不改变排队、执行或父级截止。只有多任务编排或持续进度跟踪需要异步时才调用 `submit_async` / CLI `enqueue`，随后用官方 `wait` 等原 job。

`Client.ensure(spec)` 是官方显式幂等意图入口，不是自动失败重试器。未知提交结果先查原 request；无法确认账本或主机身份时保留基础设施阻塞，不生成新 request。已接受作业断线后由 SDK 只读重连；正常连接下 `watch` 每次等待最长 10 秒并返回事件/快照，断线期间不保证事件时效。适配器只透传 `on_update`。状态快照不自动证明真实研究进展，`RUNNING` 时仍应提供真实工具/评分心跳。

## 4. 按真实状态处理

`latest_start = deadline_epoch - max_runtime_seconds`。预测基于运行作业强制上限、清理余量及前方队列预约；遥测缺失、未知外部负载或未确认清理时预测可为 `null`，不能将未知预测解释成准入保证。

| 调度状态/事件 | 控制与接管动作 |
|---|---|
| `QUEUED` / `STARTING` | 在原调用、原 request 内等待；控制器进入 `WAITING_GPU`，不计等待信用、不消耗 retry，不新建轮次 |
| `RUNNING` | 关闭相应等待区间；按原执行上限继续真实训练/评分，不把 GPU 时间直接等同 QA16 |
| `UNKNOWN` 且 `reconciling=true` | 留在 `WAITING_GPU` 等官方对账；查询原 request/job，不重新执行 |
| `UNKNOWN` 且不在对账 | 保留预约/隔离与证据；基础设施暂停，核 scope/container/exit，不能视为失败后自动重试 |
| `SESSION_CHANGED` | 控制层恢复事件，不是新的实验；继续原请求，评分仍绑定 `origin_session_id` |
| `INFEASIBLE` | 已持久受理但预测来不及启动；保留 `queue.projected_start_epoch/latest_start_epoch`，回到当前 Agent 做 CPU 工作或报告实验调整决策 |
| `EXPIRED` | 排队期间剩余预算不足以完成完整执行；停止等待并记基础设施原因，不扩截止或重置队列 |
| `CANCELLING` | 已请求取消，但资源未确认释放；继续查清理回执 |
| `SUCCEEDED` | 进程退出 0 且清理确认；仍须可信评分、完整 seed、模型重载和 completion 合同通过 |
| `FAILED` / `TIMED_OUT` / `CANCELLED` | 留真实退出与清理结果；核是否 OOM、执行超时或主动停止，再由原控制合同决定后续 |
| `JobWaitTimeout` / `JobWaitInterrupted` / SSH 断线 | 只表示本次等待未完成，作业可能仍运行；按异常中的身份或原 request 查询，不自动 cancel/重提 |

SDK 快照的 `id` 映射到 `gpu.state.job_id`，`revision` 映射到 `sequence`；预测值在 `queue` 内。`gpu_state` 只发送状态，不提交作业。多个请求并行时，每个都透传，不能用一个结束事件覆盖另一个仍在等待的请求。

控制器的 worker、controller、guard 都扣除已记录 GPU 等待判断轮内执行超时，run 原墙钟硬截止仍生效。若适配器因排队/对账终态退出，控制器归为 `failure_class: infrastructure` 并暂停；可用 `turn.failed` 报 `gpu_infeasible`、`gpu_expired`、`gpu_unknown`、`gpu_reconciling`、`gpu_session_changed` 和该 failure_class，不归入普通 `agent_exit_nonzero` 重试。控制器不会自动改实验规模、自动生成 CPU 工作或恢复已结束的 worker；这些必须在当前 Agent 调用或显式接管中处理。

## 5. 查询、取消与重启

以下命令使用实际已验收客户端与私有连接配置；尖括号均为占位符。

```text
python3 -B tools/gpu_scheduler/cli.py status --remote <gpu-remote.json>
python3 -B tools/gpu_scheduler/cli.py list --remote <gpu-remote.json>
python3 -B tools/gpu_scheduler/cli.py get --remote <gpu-remote.json> --request-id <原request-id>
python3 -B tools/gpu_scheduler/cli.py wait --remote <gpu-remote.json> --id <原job-id>
python3 -B tools/gpu_scheduler/cli.py cancel --remote <gpu-remote.json> --id <自己的job-id> --reason <真实原因>
python3 tools/gpu_monitor/monitor.py watch --view agents --interval 60 --max-hours 12
```

durable 服务支持作业 get/wait/cancel/幂等受理跨 session；服务级 `stop` 仍校验当前 session，并取消全部排队、停止全部运行作业。单题结束只取消自己的 job，不能停共享服务。旧 v4 查询仍须其 session 合同，使用该运行原 CLI/配置；不要将新示例直接套在旧服务。

异常服务重启恢复 `QUEUED` 的原位置、时间、bypasses 和截止；`STARTING/RUNNING/CANCELLING` 恢复 `UNKNOWN` 后对账，执行器继续原固定截止。服务单例只由调度进程持有，执行器有 job 级锁和持久身份，新服务可立即恢复但不重跑。正常 `stop`、SIGTERM、SIGINT 会主动取消/清理，不等于无损升级；禁止以停止旧 v4 来“测试自动恢复”。

账本 `requests.jsonl` 必须和原 `sessions/`、job 证据一并保留。残缺尾行归档后只恢复有效前缀；完整行损坏、旧 session 缺状态或同 request 对应多次执行时拒绝启动，先只读保全和对账。不删除账本、修改终态、换 root/session/request 来绕过故障或 `max_jobs`。`status.json`/监控 JSON 是快照，存活与完成必须核真实 PID、scope、容器和 exit。

## 6. 资源画像和硬限的正确含义

生产采用 `execution_backend: systemd`，要求 cgroup v2 与 system manager transient unit 权限，实际核验 `memory.max/high/swap.max` 和 `cpu.max`。`process`、`--local-test`、user-systemd 烟测不能冒充生产资源验收。Docker 主容器和 sidecar 必须经官方 provider 加入并注册同一 job slice；逃离边界的 daemon/远程任务不能直接受理。

至少默认 3 个成功、测量完整且无 OOM 的同画像样本后，后续受理以 anon+shmem+kernel 采样峰值加 25% 和 256 MiB、GPU 显存峰值加 10% 和 256 MiB 生成建议。显式 job_class 还按可执行入口分组；generic 只共享完整 command 相同的样本。校准仅在新受理前减少 RAM/GPU 预约，原声明用于幂等，原 `memory_max_mib` 硬限不自动降低或提高；CPU/CU 仍按声明。建议值、应用标志和测量范围通过 `suggested_resources`/资源回执留证。

`memory.peak` 是总内存峰值；anon 是 `memory.stat` 采样峰值，不能直接从总峰值拆出准确 anon 峰值。`memory.high` 节流总内存，不是只节流 page cache；cache 仍计入 `memory.max`。降低预约不保证同时触顶的多个作业不会挤压宿主，容量、余量、总内存峰值和并发必须实测验收。GPU VRAM 和 CU 没有硬隔离，不能以本工具证明题面的显存硬上限。

## 7. 接入完成的必要条件

1. 服务、客户端、控制器和适配器的真实发布/配置哈希匹配，目标环境权限、数据盘和 provider 限额已验收。
2. 排队状态透传到当前轮的 `gpu-wait.json`，等待不计信用、不耗 retry，硬截止和取消确实生效。
3. 断线/异常重启后原 request/job 查询成功，QUEUED 身份与位置不丢，已执行作业只对账且没有第二次启动。
4. `origin_session_id`、评分输出目录、候选/协议/模型哈希、容器归属与 completion 证据一致。
5. 非成功终态不解析成科学成绩，清理未确认不归还槽位，监控登记可回查且不代替官方取消。

本次 release 的测试范围、云端上传和未完成动态验收见 [更新记录](CHANGELOG.md)。安装或切换只按后续明确授权、空闲窗口/迁移合同执行；已经上传的 release 和其 manifest 不因本次文档更新被修改。
