# GPU 调度工具：阻塞提交与资源队列

本工具是 [统一双 Agent 长跑规范](../../双Agent长跑与题目包验收规范.md) 强制采用的 GPU 入口；题目不得另建队列或直接启动训练绕过调度。任务生命周期由 research_handoff 管理，观测/登记由 gpu_monitor 管理；资源预约不等于硬隔离，容器/provider 取消适配须另行完成真实验收。

单机、单个常驻 Python 进程、数据盘持久请求账本，**所有接入 Agent 的全局并发由 `max_running` 配置（1–32，兼容默认 2）**。实际准入须满足显存、计算份额、CPU、RAM 与遥测检查。支持同卡共享、多 GPU 选卡，每作业使用一张物理 GPU。默认提交阻塞到终态，异步接口用于多任务编排。重启恢复排队意图，永远不自动重跑已经启动的执行。

正常停止仍定向取消并清理作业。异常退出后，`QUEUED` 保留原 request/job ID、submitted_at、bypasses、绝对截止；`STARTING/RUNNING/CANCELLING` 恢复成 `UNKNOWN` 对账状态，依据原 exit/scope 确认终态，绝不重新执行。独立执行器继续原截止监督，继承的单例锁在执行器退出前阻止第二个调度进程启动。

`requests.jsonl` 是追加、fsync、哈希链账本，每次状态迁移及插队计数变化落盘。残缺最后一行先归档再恢复有效前缀；完整行损坏拒绝启动。首次启动导入旧 session 记录，重复 request_id 对应不同执行或缺状态时拒绝迁移。跨题部署入口位于 [公共运行资料](../../ops/gpu_scheduler/README.md)。

要求 Linux 5.3+、Python 3.10+、`pidfd`、cgroup v2、system manager transient unit 权限，以及 `nvidia-smi`。Python 仅用标准库。运行可信 host adapter，候选须由 provider 隔离；Docker/provider 必须接入官方容器归属及共享 job slice，不能直接提交无归属的 daemon/远端任务。

## 1. 配置与启动

复制 [config.example.json](config.example.json) 到本次私有运行配置目录，修改真实数据挂载、GPU UUID、可分配资源。主机、题号、解释器差异全部放配置/作业 JSON。

```bash
# 在目标 GPU 主机上只读核对设备和挂载。
nvidia-smi --query-gpu=uuid,name,memory.total --format=csv
findmnt -T /mnt/data

# 以下 /mnt/data 路径为示例，必须换成已经核验的数据盘路径。
python3 -B tools/gpu_scheduler/cli.py validate --config /mnt/data/ops/gpu-config.json

# 服务前台运行；按既有主机服务管理方式放入持久会话并将日志写到数据盘。
python3 -B tools/gpu_scheduler/cli.py serve --config /mnt/data/ops/gpu-config.json
```

`cli serve` 前台运行，不启动训练。持久服务使用下述官方 lifecycle 安装入口；有时限的临时服务也可通过数据盘日志的 tmux 会话或既有受管启动器运行；服务端阻塞等待不会占用 GPU，但会保持本地 Unix/SSH 请求直到作业终态。启动输出 `ready: true` 只证明接口已就绪，GPU 遥测和可调度性看 `status`。模型、数据、镜像准备应在占用 GPU 前完成。

`root` 是私有运行根，要求本用户拥有且权限 0700。正式模式强制 `data_mount` 是不同于系统盘的真实挂载，运行根和作业 `cwd` 均在该设备。运行中持续核验挂载及磁盘余量。模块设置常见临时、Python/模型/编译缓存目录到各作业的数据盘目录；自定义程序的绝对输出路径仍须由适配器检查。它不是文件系统沙箱，不会迁移公共 Docker/containerd。

一个主机用户只能启动一个正式调度服务，即使更换 `root` 也不能再开第二个。多个 Agent 使用同一服务用户和 socket，才能共同遵守全局上限。不同 Unix 用户、其他网络命名空间和调度器外部作业不属于该全局上限；部署时应统一入口。

服务最多保留 32 个阻塞等待请求、64 个总请求，额外容量留给查询、取消和停止；等待连接断开后释放请求槽，但不取消作业。这些是控制连接保护，不改变 `max_running` 指定的 GPU 并发上限，也不把控制连接数量当作研究 Agent 数量。

| 配置 | 含义 |
|---|---|
| `gpus[].uuid` | 允许调度的物理 GPU UUID，不支持 GPU index、MIG |
| `gpus[].memory_mib` | 该卡可分配显存，建议低于实际容量并预留余量 |
| `gpus[].compute_units` | 1–100 的计算预约总份额；通常设置为 100 |
| `cpu_cores / ram_mib` | 全服务 CPU、RAM 声明额度，留出宿主余量 |
| `poll_seconds` | 调度与遥测间隔，0.05–10 秒；默认 1 秒 |
| `service_seconds` | 普通服务阶段硬截止，默认/最大 43200 秒；持久服务无服务级截止，恢复请求不延长原截止 |
| `persistent` | 显式启用无服务级截止的 GPU 队列；应由受管 systemd 单元以 `Restart=always` 管理，作业仍受自身预算约束 |
| `max_running` | 全局并发上限 1–32，兼容默认 2；资源不足时仍排队 |
| `scheduling_policy` | `fifo` 保持原队列策略；`fair_share` 按 owner 当前最大资源占比和轮转次序选择，保护久等请求 |
| `starvation_seconds` | 排队达到该秒数后优先保护，默认 300 秒；不延长排队/研究截止 |
| `max_bypass` | 早到请求被越过多少次后获得保护，默认 2；0 严格 FIFO。fair_share 模式保护后仅允许不推迟预测开跑时刻的回填 |
| `max_jobs` | 持久账本最多不同请求，默认 10000；历史不自动删除，容量按运行周期配置 |
| `min_free_disk_mib` | 数据盘空余阈值，低于时拒绝新作业并停止已有作业；属于采样保护，不是磁盘硬 quota |
| `external_process_policy` | 默认 `exclusive_admission` 对未知计算进程暂停新作业；显式 `shared` 允许按实时余量与预约额度共享，不停止外部进程 |
| `shared_headroom_mib` | `shared` 准入额外保留的经验余量，默认 2048 MiB、最小 1024 MiB；补偿外部负载增长、采样间隔和碎片风险，须按本机峰值调整，不是驱动计费值或显存硬隔离 |

### 无需续期的持久服务

GPU 配置设置 `persistent: true`，省略 `service_seconds` 和 `infrastructure_lease`。`status` 返回 `persistent: true`、`deadline_epoch: null`。服务没有续期定时器，各作业仍受原绝对截止和运行上限（最多43200秒）约束。

通过 `lifecycle.py install-gpu-plan --config <plan.json>` 安装唯一 systemd 单元。plan 包含 `version: 1`、数据盘上的 `root` 和 `gpu_config`、绝对 `python` 路径、`unit_name`、明确 `authorization`、全套调度源码和 GPU 配置的 `source_sha256`。先校验 GPU 配置；plan 的 root、tmp、cache 预先由服务用户创建。单元使用 `RequiresMountsFor`、开机自启、`Restart=always`；仅 systemd 控制元数据写入 `/etc/systemd/system`，全部任务日志和缓存保留在数据盘。

持久服务维护通过 systemd。显式 stop 不会自动重启，CLI stop 仍取消本服务作业；异常重启恢复账本，原客户端可继续查询/等待原 job。服务级 stop 保留 session 校验，防止旧请求误停新服务。未知 cleanup 保留资源隔离和 UNKNOWN，等待不等于重跑授权。升级使用新不可变 release，旧 v4 首轮迁移必须先取得空闲窗口。

## 2. 提交、查询、取消

复制 [job.example.json](job.example.json)，填写实际 argv、工作目录和资源需求。程序默认不经 shell。命令和作业配置会留证，不能把密钥写入 argv 或配置。

本地控制 Agent **优先使用阻塞式 `submit`**：校验通过后立即入队，资源不足时保持 `QUEUED`，调用不会让 Agent 提前进入下一步，直到作业成功、失败、取消、超时、排队过期或进入 `UNKNOWN`；等待超时、用户中断或服务退出等关键中断则返回可恢复异常。只有需要持续跟踪进度或同时编排多个任务时，才使用显式异步 `enqueue`/`submit_async`，之后再调用 `wait`。

```bash
# 默认阻塞到终态；排队、训练和清理均包含在这次调用中。
python3 -B tools/gpu_scheduler/cli.py submit --root /mnt/data/autoresearch/gpu-queue --spec /mnt/data/ops/job.json

# 显式异步入队；只有需要自行编排多个任务时使用。
python3 -B tools/gpu_scheduler/cli.py enqueue --root /mnt/data/autoresearch/gpu-queue --spec /mnt/data/ops/job.json
python3 -B tools/gpu_scheduler/cli.py status --root /mnt/data/autoresearch/gpu-queue
python3 -B tools/gpu_scheduler/cli.py list --root /mnt/data/autoresearch/gpu-queue
python3 -B tools/gpu_scheduler/cli.py get --root /mnt/data/autoresearch/gpu-queue --request-id task-a/run-001/agent-a/eval-001
```

阻塞 `submit` 返回终态的 `id`、当前 `session_id`、不可变 `origin_session_id`、状态、回执及日志目录；`enqueue` 返回当前状态。后续按原 request/job 查询，评分证据绑定 origin_session_id 对应的原目录。

```bash
# 尖括号需替换为真实响应字段；不是可直接执行的 shell 值。
python3 -B tools/gpu_scheduler/cli.py wait --root /mnt/data/autoresearch/gpu-queue --session <session_id> --id <job_id> --timeout 3600
python3 -B tools/gpu_scheduler/cli.py cancel --root /mnt/data/autoresearch/gpu-queue --session <session_id> --id <job_id> --reason '本轮停止'
python3 -B tools/gpu_scheduler/cli.py stop --root /mnt/data/autoresearch/gpu-queue --session <session_id>
```

`submit`/`wait` 的客户端等待超时、Ctrl-C 或 SIGTERM 只结束等待，不取消已接受的作业。等待超时返回 `JobWaitTimeout`（继承 `TimeoutError`），其 `job`/`job_id` 保留真实状态与身份；服务退出或客户端中断返回 `JobWaitInterrupted`，含原因、session/request/job 身份和可获取的快照。客户端在收到提交结果前被中断时 `job` 可为 `null`，必须按原 `request_id` 查询，不能假装已取消或重提。

`cancel` 只停止指定作业，`stop` 取消排队并停止本服务全部运行作业；对 `serve` 进程发送 Ctrl-C/SIGTERM 也执行服务级清理。`CANCELLING` 和等待中断不表示清理完成，须检查真实 `exit.cleanup_ok`；服务停止时正在运行的作业可先通知等待者，再由独立执行器完成回收。

`wait`/`cancel` 的 `--job-id` 与 `--id` 等价；也支持 `python3 -B -m tools.gpu_scheduler.cli cancel ...`。本地 Unix socket 和 SSH `rpc` 均提供同一 `cancel` 操作，无 HTTP 依赖。监控器保持只读，停止仍使用本工具已有连接配置和原 `session_id`。

CLI 退出码：0 表示请求成功；阻塞 `submit`/`wait` 收到非成功终态或 `UNKNOWN` 返回 1；可恢复的等待超时或关键中断返回 3，stderr 含 JSON 身份/状态；配置、通信和服务错误返回 2。通信失败的 outcome 为 `UNKNOWN`，先查询原请求，不自动重放。进程真实退出码在 `exit.returncode`，`SUCCEEDED` 仅指进程正常退出及清理成功，不证明评分或科研目标完成。

`submit`/`wait` 的 `--timeout` 只限制本次等待，允许 0–43200 秒，不改变作业的排队/执行上限或父级截止。默认不设置客户端等待超时，但作业仍受原始预算约束。普通排队原因、STARTING/RUNNING 和进度更新不唤醒 Agent 进入下一步；这些更新留在 `status.json`/`events.jsonl` 中，关键中断和终态才结束调用。

本地控制 Agent 的工具封装须把阻塞调用保持为 pending；如果外层执行工具先返回子进程/会话编号，应继续等待同一调用，而不是把该编号当作训练完成后开始下一步。不能用周期性模型请求或 `sleep` 消耗 Agent 回合来模拟等待。SDK/CLI 负责 GPU 调度合同，不会自动改写外层 Agent 框架。

## 3. Python 接入

从工作区根导入（部署时把该根放在 Python 模块搜索路径）：

```python
from tools.gpu_scheduler import Client

client = Client('/mnt/data/autoresearch/gpu-queue')
result = client.submit({
    'request_id': 'task-a/run-001/agent-a/eval-001',
    'owner': 'task-a/agent-a',
    'command': ['/mnt/data/envs/research/bin/python', 'evaluate.py'],
    'cwd': '/mnt/data/autoresearch/tasks/task-a/workspace',
    'memory_mib': 20000,
    'compute_units': 50,
    'cpu_cores': 8,
    'ram_mib': 16384,
    'max_runtime_seconds': 1800,
    # 如属于已有长程 run，应传入其原始绝对截止：
    # 'deadline_epoch': parent_run_deadline_epoch,
    # 或 'deadline_at': parent_run_hard_deadline_at（必须包含时区，两者择一）,
})
# submit 默认已等待到终态；不要再重复启动同一个 request_id。
assert result['state'] in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'EXPIRED', 'INFEASIBLE', 'UNKNOWN'}

# 只有需要自行编排时才异步入队，再显式等待；这里仍需传完整 spec。
queued = client.submit_async({
    'request_id': 'task-a/run-001/agent-a/eval-002',
    'owner': 'task-a/agent-a',
    'command': ['/mnt/data/envs/research/bin/python', 'evaluate.py'],
    'cwd': '/mnt/data/autoresearch/tasks/task-a/workspace',
    'memory_mib': 20000,
    'compute_units': 50,
    'cpu_cores': 8,
    'ram_mib': 16384,
    'max_runtime_seconds': 1800,
})
result = client.wait(queued['id'], timeout=4000)
```

跨 session 的 `request_id` + 相同完整声明返回原作业；同 ID 改参数拒绝。`Client.ensure(spec)` 是官方幂等意图入口，适配器不再实现 ensure_job。`wait(on_update=callback)` 通过服务端长轮询事件等待，最长10秒返回一次快照；状态不变时也可观察真实日志。连接中断仅重新读取同一个 job，服务重启不会重新提交。未知提交结果先查询原 request。

可选 `gpu_uuid` 固定卡；不填则选择满足条件且当前利用率较低的卡。可选 `deadline_epoch` 为父任务的 UTC epoch 截止；实际截止同时受本次服务窗口和执行硬上限约束。等待期间若剩余预算不足以容纳完整执行上限，作业进入 `EXPIRED`，不会偷偷缩短正式协议或延长预算。

接入现有 `research_handoff` 时，在题目私有适配器里提交训练/评分并等待反馈；Agent 对话本身不持有 GPU。适配器需持续发送真实研究心跳、单独记录排队区间，并在本轮清理 hook 中按保存的 job ID 取消未结束作业。SDK 等待不是研究心跳，GPU 运行时长也不是 QA16 有效研究时间。工具接口可用不表示既有题目已接入；正式启动前须核对适配器的真实提交、计时和清理调用链。

### 两个本地客户端通过 SSH 共用调度

两端均使用相同的云端 Linux 账号，连接同一 `root` 和服务发布版本；双方都能查询、提交、取消任意作业或停止整个服务。这是用户确认的可信协作模式，不做提交者权限隔离。云端只启动一次 `serve`，本地客户端发起控制请求或阻塞等待，不启动自己的调度进程。作业由云端独立执行器监督，执行不依赖等待连接存活。

```text
本地客户端 A ── SSH 控制/等待 ─┐
                             ├─ 云端唯一调度服务 ─ 持久账本 ─ 至多 max_running 个 GPU 作业
本地客户端 B ── SSH 控制/等待 ─┘
              8 个 Agent 按需提交，排队期间不占 GPU
```

两端复制 [remote.example.json](remote.example.json)，分别将 `auth_file` 指向本地受管凭据文件（相对路径按配置文件所在目录解析），将 `python / cli / root` 配成相同云端入口。`timeout_seconds` 只用于短控制请求，`wait_timeout_seconds` 覆盖阻塞 `submit`/`wait` 的最长 SSH 等待窗口。配置只放凭据引用，不放口令值；认证复用 gpu_monitor 的 `auth.txt` / askpass 接口。作业 spec 文件在本地读取，经 SSH stdin JSON 传输；其中 argv、cwd 和解释器路径均指云端。

```bash
python3 -B tools/gpu_scheduler/cli.py status --remote /local/ops/gpu-remote.json
python3 -B tools/gpu_scheduler/cli.py submit --remote /local/ops/gpu-remote.json --spec /local/ops/job.json
python3 -B tools/gpu_scheduler/cli.py list --remote /local/ops/gpu-remote.json
python3 -B tools/gpu_scheduler/cli.py get --remote /local/ops/gpu-remote.json --request-id project/run/agent/eval
python3 -B tools/gpu_scheduler/cli.py cancel --remote /local/ops/gpu-remote.json --session <session_id> --id <job_id> --reason '协作停止'
```

```python
from tools.gpu_scheduler import RemoteClient

client = RemoteClient('/local/ops/gpu-remote.json')
status = client.status()
jobs = client.jobs()
# submit 默认阻塞；submit_async、get、cancel、wait、stop 与本机 Client 接口一致。
```

`request_id` 应包含 project/run/agent/evaluation 身份，并在同一次提交重试时保持不变。两个客户端不得为不同实验使用同一个 ID；同 ID 不同 spec 会拒绝。所有变更请求不自动重试，连接失败记录 UNKNOWN，先按 request_id 查询。作业查询和等待跨 session 使用原身份，服务重启不重新排队；RemoteClient 仍绑定首次连接的主机/账号，auth.txt 被改到另一台主机时拒绝继续操作。

阻塞 SDK 通过官方 watch 长轮询等待，最多每10秒一次事件/心跳快照，可断线续读；适配器只透传 on_update，不能另写等待循环。网络断开不取消已受理作业，按原 request/job 读取，不能自动重提。`stop` 会停止全部作业并校验当前 session，单项停止用 `cancel`。

本地 SSH 客户端需同时保留 `tools/gpu_monitor/{monitor.py,probe.py,askpass.py}` 并保持 askpass 可执行；云端使用本机 RPC 桥接，无需复制凭据或 gpu_monitor。当前受管 SSH 客户端依赖 Linux/OpenSSH，不提供跨平台桌面应用。

2026-09-30 原发布版本通过两个独立本地进程的真实 SSH 验证：双方看到同一 session 和 8 个请求，8 个合成 CUDA 作业均成功；额外两个作业用于双方互相取消。实际执行区间峰值和 NVIDIA 进程峰值均为 2，10 个作业均清理成功。测试模拟两台客户端的独立进程/连接，没有声称使用两台物理电脑，也没有启动 8 个正式研究 Agent。此历史测试不证明 2026-10-01 新增阻塞协议已在云端重新验收。证据见 [双客户端验收记录](../../notes/gpu-scheduler-v1/cloud-multiclient.md)。

## 4. 同卡共享与队列规则

例如一张卡设置 44000 MiB、100 计算份额，两个作业分别请求 20000 MiB、50 份额，可以同时运行；第三个作业等待。若其中一个请求 100 份额，另一作业就要等它释放。CPU/RAM 声明总量也须满足配置。

格式、路径、命令、非法 GPU、声明超过配置容量、数据盘或权限错误立即拒绝。合法请求按 `latest_start = deadline_epoch - max_runtime_seconds` 准入：已有作业强制运行上限及前方排队预约预测超出 latest_start，立即返回持久 `INFEASIBLE` 和预测值；遥测缺失或未知外部使用不凭空判定不可行。排队期间持续复核，真正剩余预算不足才 `EXPIRED`。队首立即预约，回填必须不推迟其预测启动。新请求声明绝对 `deadline_at` 或 `deadline_epoch`，省略时由服务固定不超过12h的窗口；旧 `queue_timeout_seconds` 仅兼容解析，不参与截止。

生产执行使用 system manager 的 job slice/scope，执行前核验 cgroup v2 `memory.max/high/swap.max`、`cpu.max`；Docker main/sidecars 通过可信 provider 加入同一父 slice，注册时核验真实 cgroup。`ram_mib` 是准入预约，`memory_max_mib` 是独立总内存硬限，`memory_high_mib` 是总内存节流阈值，CPU 使用 CPUQuota。内存硬限包含 page cache；anon 只有采样峰值，`memory.peak` 是内核总峰值。GPU 显存预约与 CU 仍不是硬隔离，设备整体利用率不冒充逐作业 CU 峰值。

至少 `profile_min_samples`（默认3）个成功且测量完整、无 OOM 的同 owner/job_class 作业，后续提交按 anon+shmem+kernel 采样峰值加25%与256MiB余量校准 RAM；GPU实测峰值加10%与256MiB余量。校准只在受理前减少预约，原声明单独存储用于幂等，执行总内存硬限保留。显式 job_class 必须在方法/数据规模/协议变化时换版本；generic 旧作业只共享完整 command 相同的画像。样本不足采用保守声明。建议值与是否应用通过 suggested_resources 留证，不预先声称5–6并发。

分配检查同时考虑实时已用显存和每个运行作业尚未实际分配的预约显存，防止延迟分配的任务被提前借走显存。发现未知 GPU 计算进程、遥测过期/缺失、显存读数不可用、容量配置超过实际容量时，暂停向对应卡分配；不会杀死未知进程。遥测不与外部启动程序原子联动，仍应把授权任务统一接入本服务。

遥测使用 NVIDIA 的 FB 显存读数 `memory.used` 与逐进程 `used_gpu_memory`，框架缓存池已实际占用的显存也在读数内；它们不是 PyTorch `memory_allocated()`。两次查询并非原子采样，因此总占用取 GPU 读数与逐进程合计的较大值，再补足各作业尚未消费的预约。只有当前 GPU 上唯一归属的 PID 才能抵扣该作业预约；未知、重复归属或运行在另一张卡上的 PID 保持外部占用，预约不会因此释放。不查询 NVIDIA CLI 未保证支持的 `reserved_memory` 字段。

工作区最新共享口径允许在不影响他人的前提下继续正在运行的训练，不因其他计算进程出现本身停止自己的任务。默认未知进程规则仅限制新作业准入，不是终止已有训练的依据。显式 `external_process_policy: shared` 将外部实时占用、尚未分配的预约显存及 `shared_headroom_mib` 一起核算；未知或过期遥测仍拒绝准入。题目适配器还须落实峰值/负载观察与自身退出条件，不干预外部任务。

**显存 MiB、计算份额、CPU 和 RAM 均为合作式预约额度，不是硬隔离。** `CUDA_VISIBLE_DEVICES` 选择设备，线程变量提供默认线程数；它们不能阻止候选修改行为。没有 MPS/MIG/cgroup 显存强制限额，也不会凭瞬时低利用率撤销已有预约。作业超报/漏报峰值可能导致干扰/OOM，需先测量再配置。不能用本工具直接证明题面的进程显存硬上限已执行。

`fifo` 保留原有到达顺序和越过次数限制。`fair_share` 在每次分配后重新计算 owner 的显存/计算/CPU/RAM 占比，优先当前最大占比较低者，占比相同时优先最久未获调度者。稳定的 owner 由可信适配器提供，不能每次换 owner 规避公平规则。

等待超过 `starvation_seconds` 或被后续请求越过 `max_bypass` 次的作业进入优先保护，受保护作业之间按到达时间排序。资源尚未释放时，后续任务仍可回填，但必须按已执行的硬运行上限和清理余量计算，证明不会推迟受保护请求的预测开跑时间；资源互不影响的其他 GPU 也可继续使用。没有可信释放预测（外部任务、UNKNOWN、清理未确认）时，只放行仍为受保护请求保留完整预约空间的任务，不借走未知资源。未知外部负载、虚报资源或物理容量不足都不能保证开跑时刻；无抢占，不取消其他任务。

`get/list` 提供 `queue.wait_seconds`、`protected`、`latest_start_epoch`、`projected_start_epoch`、`deadline_risk`。预测包含运行作业硬上限、清理余量及前方排队预约；未来外部负载可能变化，没有可信预测时为 null。预测不满足截止产生 INFEASIBLE，截止剩余不足产生 EXPIRED，均不延长原预算。

增加并发必须同时核验主机资源。建议共享部署先设 `max_running: 4`，按实测设置 CPU/RAM/GPU 总额；4 只是上限。例如 32 GiB 主机给调度器 24 GiB RAM 后，8 GiB 的请求最多 3 份，剩余槽可供较小请求。仅改并发数而保持计算总份额50、每作业25和RAM总额16GiB、每作业8GiB，仍只能运行2份。

当前调用方如果将模型请求、容器准备、CPU 分析和训练全部打包为一个70分钟GPU作业，会在无GPU计算时仍占预约。调度器不能仅凭瞬时低利用率释放其未来显存峰值。后续接入应把受管提交缩小到真正训练/评分阶段，并让排队预算覆盖合理等待；15分钟排队上限无法保证等到70分钟的前序任务完成。调整调用粒度和预算须在新配置/适配器上实施，不改写活动作业或旧失败结果。

## 5. 退出、异常与边界

```text
QUEUED → STARTING → RUNNING → SUCCEEDED / FAILED / TIMED_OUT
   └→ CANCELLED / EXPIRED      └→ CANCELLING → CANCELLED
执行器/清理异常 → UNKNOWN（仍占额度，对应 GPU 隔离）
```

- 只在执行器退出、清理回执有效且归属子进程消失后释放资源；成功主进程遗留的后台子进程也会清理。
- 进程身份和定向清理复用 `tools/process_control/processes.py`，按随机归属 token 和 PID 启动身份操作，覆盖保留标记的 setsid 后代；不使用名称匹配或无范围 pkill。
- 服务在事件循环内先持久化 STARTING 预约和固定截止，再由后台线程启动独立执行器；慢启动期间仍计入全部资源和并发上限，查询与取消可继续处理。取消记录通过 STOP.json 交给执行器，启动延迟不会重置执行截止。
- 执行器丢失时，进程/容器回收与归属扫描在后台完成，状态提交仍由事件循环单写者处理。确认回收前继续占预约；有余量的其他作业仍可分配，失败则保持 UNKNOWN 并隔离对应卡。
- 服务被 SIGKILL 后，已启动执行器继续按绝对截止和单调时钟上限回收。执行器持有主机用户级互斥锁，完成前新服务拒绝启动，即使更换运行根也一样。
- 服务仍存活而执行器意外退出时，服务尝试定向清理其子进程，单独保存 `recovery-exit.json`；无法确认则 `UNKNOWN` 并隔离该 GPU，不自动重试。
- 调度器与执行器同时被强杀、进程清除归属标记、跨 UID 或经外部 daemon 启动的作业超出本机合作式保护范围。不能把本工具当作恶意代码沙箱。服务 socket 不应直接暴露给公开 solver；由可信题目适配器代理。所有同用户客户端可查看/取消所有作业，没有租户权限隔离。
- 清理一般需要少量时间；发出超时信号不等于内核立即回收。服务退出先等待已经发出的执行器启动与异常回收确定结果，保留继承锁；随后对正常执行器最多观察 8 秒，残留执行器继续持锁，直到原截止及清理完成。

每次启动有新的 session_id，并从持久账本恢复排队。status.json 是快照，执行终态依据真实 exit.json 和 scope 对账；UNKNOWN 对账中不重跑。历史评分与轨迹始终绑定 origin_session_id，不把新 session 当新实验。

```text
<root>/
  scheduler.sock                  # 本机 API，0600
  service.json                    # 最近启动入口（不作为存活证据）
  sessions/<session_id>/
    service.json                  # 配置、原截止、源码哈希
    events.jsonl                  # 状态变化与原因
    service-exit.json              # 正常服务收尾
    jobs/<job_id>/
      spec.json / status.json     # 作业声明和历史状态
      launch.json / started.json  # 真实启动与身份
      stdout.log / stderr.log / executor.log
      STOP.json / exit.json       # 停止请求与实际执行回执
      recovery-exit.json           # 仅执行器意外退出时
      scratch/                    # 本作业临时及缓存；收尾后按运行记录清理
```

## 6. 验证与部署范围

```bash
python3 -B -W error::ResourceWarning -m unittest discover -s tools/gpu_scheduler/tests -v
```

测试通过模拟 GPU 遥测和真实本地 CPU 子进程验证四并发、owner 公平、等待老化、持续短作业下的大作业保护、多GPU回填、持久服务与作业预算隔离，以及阻塞提交、事件唤醒、资源排队、校验拒绝、幂等、取消、超时、断线释放、可恢复中断、后代清理、服务崩溃与旧会话拒绝；临时文件集中于 `notes/gpu-scheduler-v1/scratch/` 并由测试清理。`serve/validate --local-test` 仅用于 CPU 诊断，跳过数据盘校验并模拟 GPU，不能用于真实实验。真实 GPU/SSH 验收脚本需要持续采样和并行故障注入，因此显式使用 `submit_async`，不代表普通控制 Agent 应默认异步提交。

部署保持相对结构：`tools/gpu_scheduler/*.py` 和 `tools/process_control/{__init__,processes}.py`，另带 README 和示例。共享进程实现只维护在 `tools/process_control/`；research_handoff bundle 将同一源码嵌入 `core/processes.py`，保持独立可运行。复制到新的不可变数据盘发布目录，核对文件哈希；运行服务的 `service.json` 自动记录实际代码哈希。不要热覆盖已有执行器的源码。没有自动部署、购买或云 API 调用。

作业的 `deadline_at` 接受带时区的 ISO8601 字符串，校验时统一为 `deadline_epoch`，与父级/服务截止取较早者。例如 `2026-10-07T00:00:00+08:00` 与 `2026-10-06T16:00:00Z` 均为 `1791302400`。无时区或同时指定两种截止均拒绝；相同截止的不同表示规范化后不破坏 request 幂等。历史数值时间戳和已有记录无需迁移。

升级时同步发布客户端与服务端：`submit` 由异步受理改为默认阻塞，是调用行为变更；旧的“提交后逐项监控/并行提交”调用需改为 `submit_async`（CLI `enqueue`）。旧服务不支持新增等待协议，不能只换客户端。既有 run/release 与历史哈希不改，当前作业结束后再按授权采用新发布路径。2026-10-01 变更、测试与发布索引见 [阻塞协议记录](../../notes/gpu-scheduler-v2-blocking/incident.md)。

2026-09-30 已在真实 RTX 4090 上完成短时验收：单作业、两个 CUDA 作业同卡及第三个排队、定向取消、执行超时、服务 SIGKILL 后独立截止/重启互斥、正常停止均通过。共执行 7 个合成作业，142 次遥测观测到最多 2 个 GPU 计算进程、显存采样峰值 1135 MiB；收尾无 GPU 计算进程，显存回到 1 MiB。没有发现需要修改调度核心的云端兼容问题。

实测脚本 [cloud_smoke.py](tests/cloud_smoke.py) 只在显式执行时运行，不随 unittest 自动启动 GPU。它需要真实数据盘配置、未占用的授权 GPU、现有可用 CUDA PyTorch 解释器及一个新的输出目录：

```bash
python3 -B tools/gpu_scheduler/tests/cloud_smoke.py --config /mnt/data/ops/smoke-config.json --output /mnt/data/ops/smoke-evidence-new --python /mnt/data/envs/research/bin/python
```

使用合成张量、不下载模型或数据；总检查窗口 180 秒，默认单作业硬上限 40 秒，并执行主动取消和强杀调度器故障注入。脚本会启动、停止自己的服务，因此只应使用独立测试配置和明确测试授权。测试配置应把服务窗口也限定为 180 秒，并为两个作业各预留至少 2048 MiB。

完整结果、原始退出回执及部署哈希见 [云端验收报告](../../notes/gpu-scheduler-v1/cloud-validation.md) 和 [开发与验证记录](../../notes/gpu-scheduler-v1/incident.md)。这次短测不证明训练吞吐提升、长时稳定性、显存硬隔离或容器/Harbor 兼容性。

### 已到期专用容器运行时恢复

已有 GNU timeout 启动的专用运行时续期前，`lifecycle.py probe --config <不可变续期配置>` 在相同主机运行一对短时 timeout：一个仅终止其监督进程，另一个保留作为截止对照。只有对照按截止退出、其子进程消失且脱离子进程仍存活，才记录 detachment 成功；全部 probe 进程均定向清理，失败也写出 `timeout-probe.json`。安装续期必须使用本次 boot 中含 `control_deadline_verified: true` 的成功回执。该 probe 核验原 GNU timeout 启动方式，不能用新 systemd scope 替代原进程的监督关系验证。

明确授权延长研究截止时，必须同步核对专用 Docker/containerd 的期限。已停止的专用运行时可用 `lifecycle.py restart-runtime-plan --config <不可变计划>` 恢复，`runtime-plan-status` 查询同一计划。此入口使用 Python 3.11+ 和 systemd，保留既有 socket、隔离桥、Docker/containerd 数据与状态目录，核验原启动身份死亡、无其他 daemon 接管同一数据根、所有源码/输入哈希及真实数据盘。计划须声明授权、绝对截止（未来最多48h）、原运行合同/launch/config、独立 unit 前缀及证据目录。

systemd 分别托管 containerd 与 dockerd，按剩余绝对期限设置 RuntimeMaxSec、准备/退出超时、数据盘日志和缓存；不自动重启 daemon，不修改 GPU 队列或公共服务。`planned.json` 在启动前持久化，`installed.json` 或 `failure.json` 留证。失败清理对两个专用单元分别执行 `stop` 和 `reset-failed`，逐项记录返回码和异常；即使停止超时，也继续清理 failed state 并保留原始启动错误。已尝试计划不能重放；未知结果先查询同一 unit。恢复后须核验网络、容器隔离和实际模型工具调用，启动受理不等于科研恢复。仅调用该运行时入口不会修改 Agent 截止，Agent 仍由官方 handoff 管理。
