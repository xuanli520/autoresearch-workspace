# AutoResearch 通用 GPU 任务监控

本工具是正式双 Agent 长跑的受管巡检/登记入口，不另造轮询守护脚本。整体运行、OOM/无进展判断和资源边界见 [统一规范](../../双Agent长跑与题目包验收规范.md)，日志归档与临时文件规则见 [目录治理](../../notes/长程Agent脚本与目录治理.md)。监控观察不等于科研有效时间、GPU硬限额执行或平台验收。

共享 GPU 最新要求：在不影响他人的前提下继续自己的训练，其他计算进程出现本身不触发停训；监控只记录占用/余量/负载，不自动发停止指令。真正安全风险、用户停止和预算到期由对应任务合同处理，不干预他人。

用于专家和协作 Agent 在多个题目、多个 SSH 云主机间查询、轮询和接管任务。默认 **60 秒轮询、GPU 资源总览＋严格 JSON、轻量本地记录**：先显示主机数、GPU 数量，再按 GPU 显示显存/利用率/温度、已识别运行任务和未归属进程。只有手动调用 `stop-task` 才会写远端停止标记或发送定向 TERM。监控不启动、恢复、重试或追加训练。

依赖：本地和远端 Linux、Python **3.9+** 标准库；SSH 主机需 `ssh`，GPU 遥测使用已有 `nvidia-smi`。无需安装监控服务、修改训练器、安装 Python 包或上传脚本文件。只读探针通过 SSH 标准输入运行，一台主机每轮一次连接；多个主机并发查询。

## 直接使用

从项目根目录运行，命令与当前目录无关的部分均由脚本自身定位：

```bash
# 一次查询所有已登记任务；不改写监控缓存。
python3 tools/gpu_monitor/monitor.py status

# 查看任务的训练进度、日志和指标细节。
python3 tools/gpu_monitor/monitor.py status --view tasks

# 强制开启重点颜色；auto 模式在交互终端自动开启。
python3 tools/gpu_monitor/monitor.py status --color always

# 纯文本输出，适合日志、截图和不支持 ANSI 的终端。
python3 tools/gpu_monitor/monitor.py status --color never

# Agent 获取结构化数据；默认即使任务告警也成功返回 JSON。
python3 tools/gpu_monitor/monitor.py status --json

# 只看一个任务；--check 遇到任一告警退出 2。
python3 tools/gpu_monitor/monitor.py status --task <task-id> --json --check

# 前台轮询；Ctrl-C 只结束本地监控。
python3 tools/gpu_monitor/monitor.py watch

# 启动／维持后台只读监控，默认最长 24h。重复调用不会启动同目录的第二个监控器。
python3 tools/gpu_monitor/monitor.py maintain
python3 tools/gpu_monitor/monitor.py monitor-status

# 停止本地监控器，远端训练继续按自己的控制器运行。
python3 tools/gpu_monitor/monitor.py stop-monitor
```

`--config /path/to/tasks.json` 与 `--auth /path/to/auth.txt` 可用于以上全部命令。`--task ID` 可重复使用，选定子集有独立缓存目录和锁；查询/停止该子集监控时使用相同的 `--task` 集合。默认全量与子集可以并存，但会分别查询，不建议重复监控同一任务。

## 凭据：唯一的 auth.txt

所有 SSH 认证都来自工作区根目录的 [auth.txt](../../auth.txt)，不再有其它认证方式：配置里的 `password` 字段、`monitor.py` 内的明文密码表、`password_env` 环境变量、`identity_file` 密钥、SSH config 别名（`target`）均已移除。换机器或换密码时只改 `auth.txt`，`tasks.json` 里的主机条目无需改动。

`auth.txt` 用中文标签，每个标签的值写在同一行或下一行均可，标签内的空格会被忽略；`：` 与 `:` 都能识别：

```text
IP地址：
<remote-ip>
用户名：
ubuntu
密 码：
<password>
登录端口：
22
```

- 必填：`IP地址`、`用户名`、`密码`；可选：`登录端口`（缺省 22）。也可用 `ip` / `user` / `password` / `port` 等英文标签。
- 标签单独成行时，下一行整行作为取值，因此密码中可以包含冒号。
- 密码值含 `=`、`:` 等字符不受影响；缺失必填字段或端口非法时启动即报错，不会静默连错主机。
- 换凭据后无需任何手工 `ssh-keyscan` 或删除 known_hosts：脚本每次启动对外部主机跑一次 `ssh-keygen -R` 清掉旧记录，再用 `StrictHostKeyChecking=accept-new` 与托管在 `.state/known_hosts` 的文件重新学习；运行中的 `watch` 每轮会重新读取 `auth.txt`，检测到 IP/用户/端口变化也会清理旧主机密钥，凭据写坏时保留上一份可用值不中断。
- 可用 `--auth /path/to/auth.txt` 或环境变量 `AUTORESEARCH_AUTH_FILE` 指定别的凭据文件；`validate`、`monitor-status`、`stop-monitor` 不连接远端，`auth.txt` 不存在时也能运行。

密码通过进程内临时 FIFO 交给 OpenSSH askpass（OpenSSH 在 exec askpass 前会 `closefrom()`，继承的文件描述符无法传递密码，故按路径重开命名管道），不写配置或常规文件、不落日志，也不弹交互提示；askpass 与临时 FIFO 在每次查询后清理。**`auth.txt` 是明文口令，不是安全实践**；该文件已加入 `.gitignore`，若被复制、上传或纳入版本库，请立即在远端轮换密码。

**只允许密码认证**：SSH 只尝试 `keyboard-interactive`/`password` 且 `PubkeyAuthentication=no`。密码认证需要服务器允许 password/keyboard-interactive；若同时要求密码加 MFA、OTP 或多次提示，当前单密码 askpass 不适用。SSH_ASKPASS 必须指向可执行文件路径，不能写成“python3 askpass.py”这样的命令串。超时／暂时性错误按 `connection_attempts` 重试，失败后下轮还会重新查询；认证错误直接报告。无脚本能保证互联网或远端主机永远可连，`UNREACHABLE` 表示当次未知，不代表训练停止。不自动续费或变更远端服务。未配置消息推送或对话自动唤醒，告警只出现在本地记录与输出。

默认人类视图以 GPU 为中心：每台主机单独分组，每张 GPU 单独一块，只列当前能通过 PID 归属确认的运行任务；没有归属的显存进程显示为“其他”。`--view tasks` 才展开训练阶段、步数、指标和日志年龄。`RUNNING` 使用青色，`COMPLETED`/`STOPPED` 使用绿色，`FAILED`/`UNKNOWN`/告警使用红色，暂停和建议使用黄色；颜色只存在于终端渲染，不会进入 `--json`。设置环境变量 `NO_COLOR=1` 或使用 `--color never` 可关闭颜色。长错误、细节和建议会按终端宽度换行，避免把一整条 JSON 日志挤在一行。

轮询可设置 `--interval 60 --max-hours 12`；`--max-polls 2` 便于短验证，`--until-terminal` 在所选任务全部有终态且无残留进程时退出。`UNREACHABLE`、`UNKNOWN`、`EXITED_WITHOUT_RESULT` 不会被当作完成。监控时限是**本地采集器寿命**，最多另加一次查询超时；它不延长或执行训练预算。`maintain` 恢复的只有采集器。

已有登记在 [tasks.json](tasks.json)，仅作为当前工作区的配置示例。远端路径、预算字段及日志映射位于配置，主机地址与凭据位于根目录 `auth.txt`，脚本不应写死题目。登记不是新实验授权，也不是自动发现所有云端工作负载；新增运行须登记到配置。历史 PID 只属于对应原运行，后续新运行应使用新的 `launch.json`。

## 有效研究预算显示

研究运行声明 budget_mode=effective 时，监控从 status.json 读取各自 agents.sol/seed.effective_seconds、effective_target_seconds 和 effective_limit_seconds，显示累计有效时间、距目标差额及有效余额。忽略旧 deadline_at 与 wall_limit_seconds 的绝对截止推导；不会把墙钟 ETA 与有效余额直接比较。终态未达目标标记 EFFECTIVE_TARGET_NOT_REACHED，超过上限标记 EFFECTIVE_LIMIT_EXCEEDED；未知/非法计时显示观察错误，不能回填为墙钟时长。

实际停止由对应运行冻结的controller/guard执行，本采集器仍只读。历史supervisor/deadline_guard属于旧题目运行器；当前tools/research_handoff已经改为通用控制器，状态Schema不同，不自动适配旧登记。maintain/watch的max-hours仅控制本地采集寿命。旧run保留原模式事实，新运行须核对真实入口和新run-id。

## 显式停止一个任务

```bash
# 预览：只核验归属，不发送信号、不写远端文件。
python3 tools/gpu_monitor/monitor.py stop-task --task <task-id> \
  --reason '人工检查停止范围' --dry-run

# 确认方向需要调整后，由操作者明确调用。不会有自动停止。
python3 tools/gpu_monitor/monitor.py stop-task --task <task-id> \
  --reason '记录本次停止的真实原因'

# 发送请求不等于训练已退出，随后检查状态与子进程。
python3 tools/gpu_monitor/monitor.py status --task <task-id>
```

必须指定恰好一个 `--task` 和非空 `--reason`。配置没有 `stop` 合同时拒绝执行；`--dry-run` 可先审查。

| 停止模式 | 执行和边界 |
|---|---|
| `marker` | 在任务根内创建预先约定的停止标记，已有标记保留；由原控制器停止自己的任务。控制器若已退出，标记本身不会清理孤儿进程。 |
| `process_groups` | 按声明的 PID／launch 文件、命令特征、cwd、PGID=PID、可选 `start_ticks` 核验全部目标后，仅向这些组发送 CONT、TERM；发送前重验身份。身份不符拒绝操作，不使用全局 pkill、不自动 KILL。 |

新任务使用官方 research_handoff 的 stop 接口；下述 marker/process_groups 仅适用于已验证的兼容登记，不能为接入 monitor 另造控制器或假设官方控制器识别历史 STOP 路径。使用进程组时需要独立会话／进程组，并在配置中列全会派生新进程组的控制器；子进程若自行脱离该组，TERM 不能覆盖它，必须另登记。Linux 进程组与路径检查是操作保护，不是恶意多用户安全沙箱；发信号与进程退出存在系统级竞态，必须复查。具有串行后续任务的 supervisor 应优先停止其整体调度合同，再复查各方法进程。

停止合同只作用于配置中已核验的 task/job 和其声明的进程组；历史 PID 不适用于替代接管器。监控与停止记录都不能取代原实验的独立 watchdog。

## 保持 tasks.json 只包含当前任务

`tasks.json` 是活动登记表。每次新增/接续运行，登记新的 run ID、真实进程、预算模式/有效目标（或兼容墙钟截止）和当前容器归属；同一研究 Agent 保留一个当前条目，两组按各自官方 run-id 分开登记。停止使用该 run 的 controller stop；只有旧登记确实使用并验证过 marker 时才保留原 STOP 路径，不把历史 sol/seed/STOP 套到新控制器。任务独立移交到新 run 后，更新对应模型的条目；不要继续监控旧组级停止路径。

任务完成、明确停止或过期且确认无存活进程后，及时移出活动表。历史条目保存到 `archive/tasks-<UTC>-<id>.json`，原始日志、`.state` 和远端证据不删除。`registry.py` 只操作本地配置，不连接 SSH，也不会停止训练。

```bash
# 先取得新的只读状态快照。
python3 tools/gpu_monitor/monitor.py status --json > /tmp/autoresearch-monitor-status.json

# 预览可归档的终态/过期条目；默认不改 tasks.json。
python3 tools/gpu_monitor/registry.py prune --snapshot /tmp/autoresearch-monitor-status.json

# 应用同一新鲜快照，保存历史配置，再移出活动表。
python3 tools/gpu_monitor/registry.py prune --snapshot /tmp/autoresearch-monitor-status.json --apply

# 已核实被替代、旧云机或过期登记，可明确指定 ID 与原因。
python3 tools/gpu_monitor/registry.py archive --task <old-task-id> \
  --reason '该登记已由新的 run 替代，旧运行已结束' --apply

# 通过归档文件查询历史登记；认证仍来自指定的 auth.txt。
python3 tools/gpu_monitor/monitor.py status --config tools/gpu_monitor/archive/<archive-file>.json \
  --task <old-task-id> --json
```

自动筛选要求快照默认不超过 300 秒、状态可观察且无匹配进程。`UNREACHABLE`/`UNKNOWN`、过旧观察、存活进程、身份/观察冲突不自动归档；截止过期但进程仍活着也必须保留并调查。归档预览输出配置 SHA256，可在应用时加 `--expected-sha256 <hash>`，防止覆盖预览后的配置变更。所有条目归档后允许空表，`maintain`/`watch` 会直接返回。

运行中的采集器保留启动时选定的任务集合，修改登记表不会自动刷新它。切换登记时，先用原来的 `--task` 集合执行 `stop-monitor`，更新并 `validate` 配置，再用当前集合执行 `maintain --interval 60 --max-hours <覆盖原截止的时长> --until-terminal`。这只切换本地采集器，远端研究照常执行。默认全量监控不需要 `--task`；已有子集监控须使用同一集合查询/停止。

接手时的顺序是：`monitor-status` 确认采集器 → `status --view tasks` 或 `--json` 查询当前远端 → 检查真实心跳、轮次/评分及原截止 → 更新题目进展清单 → 归档已完成或过期登记。告警不能直接替代故障判定；例如当前长评分尚未结束，轨迹暂不增加仍可能正常。

## 为新题目登记

复制 [examples/task.example.json](examples/task.example.json)，填写主机别名、远端根目录、状态/退出文件、进程特征、日志指标与总步数，然后：

```bash
python3 tools/gpu_monitor/monitor.py validate --config /path/to/my-tasks.json
python3 tools/gpu_monitor/monitor.py status --config /path/to/my-tasks.json --json
python3 tools/gpu_monitor/monitor.py maintain --config /path/to/my-tasks.json
```

也可向现有 `tasks.json` 的 `hosts`/`tasks` 增加条目。一个配置可以包含多题、多方法、多 seed；每个独立生命周期推荐一个 task ID。配置属于可信专家侧输入，不允许未受信任的候选代码修改。`state_dir` 相对配置文件目录解析；远端文件路径相对各自 `root`，拒绝 `..`、绝对文件路径和逃逸根目录的符号链接。

| 字段 | 用途 |
|---|---|
| `hosts.<name>` | `transport: ssh` 的地址与凭据**不再写进配置**，运行时一律从 `auth.txt` 读取覆盖（IP、用户、端口、密码都以该文件为准）；配置只保留 `transport`、`connect_timeout_seconds`、`python`、SSH argv `options`。也支持 `transport: local`。 |
| `connection_attempts` / `retry_delay_seconds` | SSH 查询遇到超时或暂时性错误时的总尝试次数和间隔。认证、主机密钥等确定性错误不会重试。重试不能保证网络、云主机或凭据永远可用。 |
| `id` / `host` / `root` | 唯一 ID、主机索引、远端绝对任务根。根目录应限定当前实验，不能用 `/`。 |
| `protocol` | 诊断／正式标签与冻结协议 ID，仅作索引，不自动判定 G01/G02/G03。 |
| `processes` | 使用 `pid_file`＋可选 `pid_key`（点分 JSON 字段），或固定 `pid`，或按命令查找；每项必须有 `contains` 非空特征列表，建议加 `cwd`、可选 `start_ticks`。已匹配进程的后代自动纳入。 |
| `status` / `exit` | `{"path":"status.json","key":"state"}`、`{"path":"worker.json","key":"exit_code"}`。没有退出码不补造；非零退出码不能被成功状态覆盖。 |
| `terminal_states` | 将项目终态精确映射到 `COMPLETED`、`FAILED`、`STOPPED`，例如 `{"COMPLETED":["COMPLETE_SMOKE"]}`。自定义值替换该类默认映射。 |
| `launch` / `budget_key` / `started_key` | 从真实 launch JSON 计算绝对截止；默认键为 `budget_seconds`、`started_at`。也可声明带时区的 `deadline_at` 或 `deadline_file` 的 path/key；后两者优先。缺失预算显示未知。 |
| `gpu_uuids` | 展示指定 GPU 的遥测与进程；空列表显示全部。`belongs_to_task` 区分任务已核验 PID 与其他 PID，其他进程不自动判定为冲突。 |
| `streams` | 明确日志路径、格式、指标字段、可选过滤、方向、总步数、停滞阈值。每文件默认只读末尾 64 KiB；不读 checkpoint。 |
| `stale_seconds` | 无日志／步数推进的阈值，按实际日志频率、长评估或保存耗时设置；流内覆盖任务值，0 禁用。默认任务 900 秒；长间隔日志应使用更大的预声明阈值。 |
| `streams[].complete_pattern` | 可选的阶段结束文本正则，例如进入 `features`/`probe` 后不再对适配步数报警；保留实际最后日志步数，不补造缺失的最终步。 |
| `min_disk_free_gib` | 输出盘低空间告警阈值，默认 2 GiB。 |
| `stop` | 可选的显式停止合同，轮询从不调用；见上节。 |

JSONL 日志示例：

```json
{"event":"progress","step":100,"loss":0.7,"elapsed_seconds":12.0}
```

流配置 `format: jsonl`，`fields: {"step":"step","metric":"loss","elapsed_seconds":"elapsed_seconds"}`，可用 `where: {"event":"progress"}` 筛选事件。字段支持点分嵌套；输出使用统一键 `step`、`metric`、`elapsed_seconds`，其他损失字段也可保留。

文本日志可用 `format: regex` 和带命名捕获组的 `pattern`，例如 `adapt (?P<step>\d+) / \d+ loss (?P<metric>\S+) elapsed (?P<elapsed_seconds>\S+)`；捕获值须是数字。`format: text` 只收集末尾文本、日志新鲜度和错误特征。输出 JSON 中 NaN/Inf 转为字符串并告警，保证机器可严格解析。

建议启动器保存 `launch.json` 的真实 PID、开始时间、预算、PGID、命令与 cwd；结束时原子写 `worker.json` 的退出码。监控不修改历史 launch/handoff 文件。匹配只面向 SSH 当前账号可观察的 Linux 进程；容器内 PID 不等于宿主 PID，Docker/Slurm/Kubernetes 作业应通过对应可信启动器导出宿主进程和状态合同后接入，当前工具不直接调用这些平台 API。

## 状态、告警与研究判断

| 状态／告警 | 含义与处理 |
|---|---|
| `RUNNING` / `PAUSED` | 至少一个进程身份匹配；不等于梯度仍推进，结合日志和指标。 |
| `COMPLETED` / `FAILED` / `STOPPED` | 来自明确终态或退出码；终态仍有进程存活时额外 `STATUS_CONFLICT`。 |
| `NOT_STARTED` | 没有可见进程或已有运行记录，不证明其他路径没有任务。 |
| `EXITED_WITHOUT_RESULT` | 已有运行记录，但无匹配进程、无可信终态；可能是中断、控制器崩溃或登记错误。 |
| `UNREACHABLE` / `UNKNOWN` | SSH 查询失败／进程权限不足；不以旧快照报告仍运行。JSON 保留最后成功查询时间和历史状态。 |
| `STALE_LOG` / `STALLED_PROGRESS` | 日志过旧／跨轮询步数没推进；确认是故障还是正常长阶段。重启／日志轮转会重置步数停滞窗口。 |
| `NONFINITE` / `ERROR_LOG` | 日志末尾有数值或错误特征，需看上下文；不是自动停止依据。 |
| `DEADLINE_EXCEEDED` / `ETA_OVER_BUDGET` | 原截止已到且尚未确认结束／近期吞吐预计超时；检查原控制器时限。 |
| `IDENTITY_MISMATCH` / `OBSERVATION_ERROR` | PID 复用或身份不符／部分来源不可读。 |

ETA 只估当前流到其 `total_steps` 的训练时间，优先使用日志中两个进度点的真实 elapsed 差；缺少时使用连续两次观察的步数差。首轮无法估计就显示 `?`，无活进程时不显示完成预期。ETA 不包括后续 probe、重载、排队；短期吞吐会波动。

`best_in_tail`、`metric_delta_in_tail` 是**有界日志窗口**的描述统计，不是全程最佳 checkpoint，更不能用来挑正式 seed。不同方法、阶段、步数或协议不自动排名。学习信号足以触发人工检查，但达标、换方向或停止正式实验仍按各题授权与冻结协议判断。

## 记录、接管和故障恢复

后台目录默认为本工具的 `.state/`，包含：

- `latest.json`：原子更新的最近快照，含采样时间、主机/GPU分组、任务归属、指标、告警与建议；直接读缓存须核对时间和采集器存活，优先运行实时 `status`。
- `observations-YYYY-MM-DD.jsonl`：按 UTC 日期追加每轮轻量观察。
- `events-YYYY-MM-DD.jsonl`：首次观察、状态或告警集合变化；不会每轮重复相同事件。
- `watch.json`、`watch.lock`、`collector.log`：本地采集器身份、截止、最后轮询、退出原因、互斥锁及终端输出。
- `stop-requests.jsonl`：显式停止／预览回执；超时表示结果未知，先查询再决定后续。

这些是监控记录，**不代替原始训练证据**。默认不拉模型、数据集或完整日志；模型回收仍使用各题既有 `pull` 合同，并核验哈希。记录按日分文件，不自动删除；长期复用时按项目证据保留规则归档。不要把含私有研究结果的监控目录挂载给正式 solver。

当前auth.txt口令模式使用BatchMode=no与受控askpass；每次查询禁用ControlMaster/ControlPath复用，避免旧socket卡住采集。主机地址与凭据取自auth.txt，配置只保留transport、connect_timeout_seconds、python、options等。该认证合同与通用控制器的SSH key/agent不同。

退出码：`0` 命令成功，`1` 配置／采集器／停止操作错误，`2` 为 `status --check` 发现告警。默认 `status` 通过 JSON 表达远端不可达，不因一个离线主机隐藏其他任务。后台轮询持续记录离线情况；其时限、终态条件和本地停止请求各自生效。

## 验证

```bash
python3 -m unittest discover -s tools/gpu_monitor/tests -v
python3 tools/gpu_monitor/monitor.py validate
```

本地检查覆盖断连、旧状态、退出码、残留进程、NaN、半行日志、轮转、停滞、预算、路径边界、PID 身份与显式停止；真实信号仅用于测试创建的本地睡眠进程组。云端验证仅查询和停止预览，没有启动、停止或恢复训练。云端记录见 [verification.json](verification.json)。
