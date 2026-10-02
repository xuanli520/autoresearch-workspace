# GPU 调度工具：阻塞提交与资源队列

本工具是 [统一双 Agent 长跑规范](../../双Agent长跑与题目包验收规范.md) 强制采用的 GPU 入口；题目不得另建队列或直接启动训练绕过调度。任务生命周期由 research_handoff 管理，观测/登记由 gpu_monitor 管理；资源预约不等于硬隔离，容器/provider 取消适配须另行完成真实验收。

单机、单个常驻 Python 进程、内存队列，**所有接入 Agent 合计最多同时运行 2 个 GPU 作业**。支持两份作业共享同一张 GPU，也支持从配置的多张卡中选择一张。每个作业只用一张物理 GPU。多个本地客户端可通过 SSH 共用该服务；8 个长期 Agent 会话不受接入数量限制，GPU 请求统一排队。默认提交调用会阻塞到作业终态；只有需要持续跟踪进度或同时编排多个任务时才使用异步入队接口。无数据库，无自动恢复队列，无自动重试训练。

用户确认的退出策略：正常退出清理本服务的作业；调度器异常退出时，已经启动的独立执行器继续监督原作业，在既定截止内完成或回收。排队请求丢失，日志和退出回执保留。

要求 Linux 5.3+、Python 3.10+、`pidfd`、同用户 `/proc` 可读、目标主机已有 `nvidia-smi`；Python 仅用标准库。只运行可信同用户、前台本机命令及保留进程归属标记的子进程。当前没有 Docker/Harbor/provider 取消适配器，不应直接提交脱离本机进程管理的容器或远端任务。

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

服务不自动后台化、不安装系统服务、不启动训练。建议通过数据盘日志的 tmux 会话或既有受管启动器运行；服务端阻塞等待不会占用 GPU，但会保持本地 Unix/SSH 请求直到作业终态。启动输出 `ready: true` 只证明接口已就绪，GPU 遥测和可调度性看 `status`。模型、数据、镜像准备应在占用 GPU 前完成。

`root` 是私有运行根，要求本用户拥有且权限 0700。正式模式强制 `data_mount` 是不同于系统盘的真实挂载，运行根和作业 `cwd` 均在该设备。运行中持续核验挂载及磁盘余量。模块设置常见临时、Python/模型/编译缓存目录到各作业的数据盘目录；自定义程序的绝对输出路径仍须由适配器检查。它不是文件系统沙箱，不会迁移公共 Docker/containerd。

一个主机用户只能启动一个正式调度服务，即使更换 `root` 也不能再开第二个。多个 Agent 使用同一服务用户和 socket，才能共同遵守全局上限。不同 Unix 用户、其他网络命名空间和调度器外部作业不属于该全局上限；部署时应统一入口。

服务最多保留 32 个阻塞等待请求、64 个总请求，额外容量留给查询、取消和停止；等待连接断开后释放请求槽，但不取消作业。这些是控制连接保护，不改变全局 GPU 并发 2，也不把控制连接数量当作研究 Agent 数量。

| 配置 | 含义 |
|---|---|
| `gpus[].uuid` | 允许调度的物理 GPU UUID，不支持 GPU index、MIG |
| `gpus[].memory_mib` | 该卡可分配显存，建议低于实际容量并预留余量 |
| `gpus[].compute_units` | 1–100 的计算预约总份额；通常设置为 100 |
| `cpu_cores / ram_mib` | 全服务 CPU、RAM 声明额度，留出宿主余量 |
| `poll_seconds` | 调度与遥测间隔，0.05–10 秒；默认 1 秒 |
| `service_seconds` | 本次服务阶段硬截止，默认/最大 43200 秒；重启不接续旧队列 |
| `max_bypass` | 暂时不能运行的早到作业最多被后续作业越过次数，默认 2；0 为严格 FIFO |
| `max_jobs` | 单次服务会话最多接收的不同请求，默认 10000；保留历史以维持幂等 |
| `min_free_disk_mib` | 数据盘空余阈值，低于时拒绝新作业并停止已有作业；属于采样保护，不是磁盘硬 quota |
| `external_process_policy` | 默认 `exclusive_admission` 对未知计算进程暂停新作业；显式 `shared` 允许按实时余量与预约额度共享，不停止外部进程 |
| `shared_headroom_mib` | `shared` 准入额外保留的显存余量，默认 2048 MiB、最小 1024 MiB；不是显存硬隔离 |

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

阻塞 `submit` 返回终态的 `id`、`session_id`、状态、退出回执及日志目录；`enqueue` 返回当前状态（通常是 `QUEUED`）。将返回值保存到本题运行记录；后续自动请求携带 `--session <session_id>`，拒绝跨服务重启误重放。

```bash
# 尖括号需替换为真实响应字段；不是可直接执行的 shell 值。
python3 -B tools/gpu_scheduler/cli.py wait --root /mnt/data/autoresearch/gpu-queue --session <session_id> --id <job_id> --timeout 3600
python3 -B tools/gpu_scheduler/cli.py cancel --root /mnt/data/autoresearch/gpu-queue --session <session_id> --id <job_id> --reason '本轮停止'
python3 -B tools/gpu_scheduler/cli.py stop --root /mnt/data/autoresearch/gpu-queue --session <session_id>
```

`submit`/`wait` 的客户端等待超时、Ctrl-C 或 SIGTERM 只结束等待，不取消已接受的作业。等待超时返回 `JobWaitTimeout`（继承 `TimeoutError`），其 `job`/`job_id` 保留真实状态与身份；服务退出或客户端中断返回 `JobWaitInterrupted`，含原因、session/request/job 身份和可获取的快照。客户端在收到提交结果前被中断时 `job` 可为 `null`，必须按原 `request_id` 查询，不能假装已取消或重提。

`cancel` 只停止指定作业，`stop` 取消排队并停止本服务全部运行作业；对 `serve` 进程发送 Ctrl-C/SIGTERM 也执行服务级清理。`CANCELLING` 和等待中断不表示清理完成，须检查真实 `exit.cleanup_ok`；服务停止时正在运行的作业可先通知等待者，再由独立执行器完成回收。

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
    'queue_timeout_seconds': 3600,
    # 如属于已有长程 run，应传入其原始绝对截止：
    # 'deadline_epoch': parent_run_deadline_epoch,
})
# submit 默认已等待到终态；不要再重复启动同一个 request_id。
assert result['state'] in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'EXPIRED', 'UNKNOWN'}

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
    'queue_timeout_seconds': 3600,
})
result = client.wait(queued['id'], timeout=4000)
```

同一服务会话内 `request_id` + 相同完整 spec 返回原作业；同一 ID 改参数会拒绝。通信结果 UNKNOWN 时先 `client.get(request_id=...)`，不自动重放提交。Client 自动绑定初次连接的 session；跨重启拒绝旧 Client 请求。新 Client 新会话不继承幂等历史；再次提交旧实验前必须核对原回执，防止重复实验。

可选 `gpu_uuid` 固定卡；不填则选择满足条件且当前利用率较低的卡。可选 `deadline_epoch` 为父任务的 UTC epoch 截止；实际截止同时受本次服务窗口和执行硬上限约束。等待期间若剩余预算不足以容纳完整执行上限，作业进入 `EXPIRED`，不会偷偷缩短正式协议或延长预算。

接入现有 `research_handoff` 时，在题目私有适配器里提交训练/评分并等待反馈；Agent 对话本身不持有 GPU。适配器需持续发送真实研究心跳、单独记录排队区间，并在本轮清理 hook 中按保存的 job ID 取消未结束作业。SDK 等待不是研究心跳，GPU 运行时长也不是 QA16 有效研究时间。工具接口可用不表示既有题目已接入；正式启动前须核对适配器的真实提交、计时和清理调用链。

### 两个本地客户端通过 SSH 共用调度

两端均使用相同的云端 Linux 账号，连接同一 `root` 和服务发布版本；双方都能查询、提交、取消任意作业或停止整个服务。这是用户确认的可信协作模式，不做提交者权限隔离。云端只启动一次 `serve`，本地客户端发起控制请求或阻塞等待，不启动自己的调度进程。作业由云端独立执行器监督，执行不依赖等待连接存活。

```text
本地客户端 A ── SSH 控制/等待 ─┐
                             ├─ 云端唯一调度服务 ─ 内存队列 ─ 最多 2 个 GPU 作业
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

`request_id` 应包含 project/run/agent/evaluation 身份，并在同一次提交重试时保持不变。两个客户端不得为不同实验使用同一个 ID；同 ID 不同 spec 会拒绝。所有变更请求不自动重试，连接失败记录 UNKNOWN，先按 request_id 查询。Client 绑定会话及首次连接的主机/账号；云端服务重启或 auth.txt 被改到另一台主机时拒绝继续操作。

阻塞 `submit`/`wait` 在云端保持一个 SSH 请求，由服务端状态事件唤醒，不通过客户端高频轮询；`remote.example.json` 的 `wait_timeout_seconds` 应覆盖允许的作业/服务等待窗口。网络断开只影响本次等待，不影响已经受理的作业；先用 `request_id` 查询，不要自动重提。CLI 自动化建议保存首次响应的 `session_id`，在后续命令显式传入 `--session`。`stop` 会停止双方作业，单项停止使用 `cancel`。

本地 SSH 客户端需同时保留 `tools/gpu_monitor/{monitor.py,probe.py,askpass.py}` 并保持 askpass 可执行；云端使用本机 RPC 桥接，无需复制凭据或 gpu_monitor。当前受管 SSH 客户端依赖 Linux/OpenSSH，不提供跨平台桌面应用。

2026-09-30 原发布版本通过两个独立本地进程的真实 SSH 验证：双方看到同一 session 和 8 个请求，8 个合成 CUDA 作业均成功；额外两个作业用于双方互相取消。实际执行区间峰值和 NVIDIA 进程峰值均为 2，10 个作业均清理成功。测试模拟两台客户端的独立进程/连接，没有声称使用两台物理电脑，也没有启动 8 个正式研究 Agent。此历史测试不证明 2026-10-01 新增阻塞协议已在云端重新验收。证据见 [双客户端验收记录](../../notes/gpu-scheduler-v1/cloud-multiclient.md)。

## 4. 同卡共享与队列规则

例如一张卡设置 44000 MiB、100 计算份额，两个作业分别请求 20000 MiB、50 份额，可以同时运行；第三个作业等待。若其中一个请求 100 份额，另一作业就要等它释放。CPU/RAM 声明总量也须满足配置。

作业校验分为两层：格式、路径、命令、非法 GPU、请求资源超过所有配置 GPU、预算不可能满足、数据盘或权限问题属于**静态校验失败**，立即返回错误且不入队；当前 GPU 槽位、可用显存、计算份额、CPU/RAM 或可信遥测暂时不足属于**运行时资源不足**，校验通过后创建 `QUEUED` 作业并记录排队原因。排队仍受 `queue_timeout_seconds` 和父级截止时间约束，超时返回 `EXPIRED`，不会把合法任务误报为校验失败。

分配检查同时考虑实时已用显存和每个运行作业尚未实际分配的预约显存，防止延迟分配的任务被提前借走显存。发现未知 GPU 计算进程、遥测过期/缺失、显存读数不可用、容量配置超过实际容量时，暂停向对应卡分配；不会杀死未知进程。遥测不与外部启动程序原子联动，仍应把授权任务统一接入本服务。

工作区最新共享口径允许在不影响他人的前提下继续正在运行的训练，不因其他计算进程出现本身停止自己的任务。默认未知进程规则仅限制新作业准入，不是终止已有训练的依据。显式 `external_process_policy: shared` 将外部实时占用、尚未分配的预约显存及 `shared_headroom_mib` 一起核算；未知或过期遥测仍拒绝准入。题目适配器还须落实峰值/负载观察与自身退出条件，不干预外部任务。

**显存 MiB、计算份额、CPU 和 RAM 均为合作式预约额度，不是硬隔离。** `CUDA_VISIBLE_DEVICES` 选择设备，线程变量提供默认线程数；它们不能阻止候选修改行为。没有 MPS/MIG/cgroup 显存强制限额，也不会凭瞬时低利用率撤销已有预约。作业超报/漏报峰值可能导致干扰/OOM，需先测量再配置。不能用本工具直接证明题面的进程显存硬上限已执行。

队列按到达顺序运行；队首暂时装不下时，允许后续可运行作业最多越过 `max_bypass` 次，此后保留队首机会。没有预估时长排序、预约开跑时间保证、抢占、按 Agent 权重分配或多卡 gang scheduling。`owner` 用于追踪归属，不限制 Agent 接入数量。

## 5. 退出、异常与边界

```text
QUEUED → STARTING → RUNNING → SUCCEEDED / FAILED / TIMED_OUT
   └→ CANCELLED / EXPIRED      └→ CANCELLING → CANCELLED
执行器/清理异常 → UNKNOWN（仍占额度，对应 GPU 隔离）
```

- 只在执行器退出、清理回执有效且归属子进程消失后释放资源；成功主进程遗留的后台子进程也会清理。
- 进程身份和定向清理复用 `research_handoff/core/processes.py`，按随机归属 token 和 PID 启动身份操作，覆盖保留标记的 setsid 后代；不使用名称匹配或无范围 pkill。
- 服务被 SIGKILL 后，已启动执行器继续按绝对截止和单调时钟上限回收。执行器持有主机用户级互斥锁，完成前新服务拒绝启动，即使更换运行根也一样。
- 服务仍存活而执行器意外退出时，服务尝试定向清理其子进程，单独保存 `recovery-exit.json`；无法确认则 `UNKNOWN` 并隔离该 GPU，不自动重试。
- 调度器与执行器同时被强杀、进程清除归属标记、跨 UID 或经外部 daemon 启动的作业超出本机合作式保护范围。不能把本工具当作恶意代码沙箱。服务 socket 不应直接暴露给公开 solver；由可信题目适配器代理。所有同用户客户端可查看/取消所有作业，没有租户权限隔离。
- 清理一般需要少量时间；发出超时信号不等于内核立即回收。正常服务退出最多等 8 秒，残留执行器继续持锁，直到原截止及清理完成。

每次启动创建独立 `session_id`，不读旧文件重建队列。`status.json` 是历史证据，调度器死亡后其中的 QUEUED/RUNNING 可能已过时；原执行器的真实 `exit.json` 更晚写入。旧排队请求必须由调用方在核对后重新提交。

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

测试通过模拟 GPU 遥测和真实本地 CPU 子进程验证阻塞提交、事件唤醒、资源排队、校验拒绝、幂等、取消、超时、断线释放、可恢复中断、后代清理、服务崩溃与旧会话拒绝；临时文件集中于 `notes/gpu-scheduler-v1/scratch/` 并由测试清理。`serve/validate --local-test` 仅用于 CPU 诊断，跳过数据盘校验并模拟 GPU，不能用于真实实验。真实 GPU/SSH 验收脚本需要持续采样和并行故障注入，因此显式使用 `submit_async`，不代表普通控制 Agent 应默认异步提交。

部署保持相对结构：`tools/gpu_scheduler/*.py` 与唯一依赖 `tools/research_handoff/core/processes.py`，另带 README 和示例。复制到新的不可变数据盘发布目录，核对文件哈希；运行服务的 `service.json` 自动记录实际代码哈希。不要热覆盖已有执行器的源码。没有自动部署、购买或云 API 调用。

升级时同步发布客户端与服务端：`submit` 由异步受理改为默认阻塞，是调用行为变更；旧的“提交后逐项监控/并行提交”调用需改为 `submit_async`（CLI `enqueue`）。旧服务不支持新增等待协议，不能只换客户端。既有 run/release 与历史哈希不改，当前作业结束后再按授权采用新发布路径。2026-10-01 变更、测试与发布索引见 [阻塞协议记录](../../notes/gpu-scheduler-v2-blocking/incident.md)。

2026-09-30 已在真实 RTX 4090 上完成短时验收：单作业、两个 CUDA 作业同卡及第三个排队、定向取消、执行超时、服务 SIGKILL 后独立截止/重启互斥、正常停止均通过。共执行 7 个合成作业，142 次遥测观测到最多 2 个 GPU 计算进程、显存采样峰值 1135 MiB；收尾无 GPU 计算进程，显存回到 1 MiB。没有发现需要修改调度核心的云端兼容问题。

实测脚本 [cloud_smoke.py](tests/cloud_smoke.py) 只在显式执行时运行，不随 unittest 自动启动 GPU。它需要真实数据盘配置、未占用的授权 GPU、现有可用 CUDA PyTorch 解释器及一个新的输出目录：

```bash
python3 -B tools/gpu_scheduler/tests/cloud_smoke.py --config /mnt/data/ops/smoke-config.json --output /mnt/data/ops/smoke-evidence-new --python /mnt/data/envs/research/bin/python
```

使用合成张量、不下载模型或数据；总检查窗口 180 秒，默认单作业硬上限 40 秒，并执行主动取消和强杀调度器故障注入。脚本会启动、停止自己的服务，因此只应使用独立测试配置和明确测试授权。测试配置应把服务窗口也限定为 180 秒，并为两个作业各预留至少 2048 MiB。

完整结果、原始退出回执及部署哈希见 [云端验收报告](../../notes/gpu-scheduler-v1/cloud-validation.md) 和 [开发与验证记录](../../notes/gpu-scheduler-v1/incident.md)。这次短测不证明训练吞吐提升、长时稳定性、显存硬隔离或容器/Harbor 兼容性。
