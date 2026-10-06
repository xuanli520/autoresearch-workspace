# AutoResearch 通用 GPU 任务监控

本工具是正式双 Agent 长跑的受管巡检/登记入口，不另造轮询守护脚本。整体运行、OOM/无进展判断和资源边界见 [统一规范](../../双Agent长跑与题目包验收规范.md)，日志归档与临时文件规则见 [目录治理](../../notes/长程Agent脚本与目录治理.md)。监控观察不等于科研有效时间、GPU硬限额执行或平台验收。

登记表只接受官方任务身份：`research_handoff` 的 `run_id` 与 `gpu_scheduler` 的 `job_id`/`request_id` 必须可回查。监控是只读聚合视图，不启动、恢复、重试或追加训练；任务停止仍由对应官方控制器执行。旧 `stop`、`marker`、`process_groups` 字段只要出现就拒绝，即使值为空也不接受；应先修订登记并通过 `validate`。

共享 GPU 最新要求：在不影响他人的前提下继续自己的训练，其他计算进程出现本身不触发停训；监控只记录占用/余量/负载，不自动发停止指令。真正安全风险、用户停止和预算到期由对应任务合同处理，不干预他人。

用于专家和协作 Agent 在多个题目、多个 SSH 云主机间查询、轮询和接管任务。默认 **60 秒轮询、GPU 资源总览＋严格 JSON、轻量本地记录**：先显示主机数、GPU 数量，再按 GPU 显示显存/利用率/温度、已识别运行任务和未归属进程。`stop-task` 仅返回官方停止入口提示并记录本地回执，不向远端或控制器发送停止请求。

依赖：本地和远端 Linux、Python **3.9+** 标准库；SSH 主机需 `ssh`，GPU 遥测使用已有 `nvidia-smi`。无需安装监控服务、修改训练器、安装 Python 包或上传脚本文件。只读探针通过 SSH 标准输入运行，一台主机每轮一次探测（暂时性错误可重试）；多个主机并发查询。

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

# 持续轮询所有已登记的官方长时间 Agent 任务；Ctrl-C 只结束本地监控。
python3 tools/gpu_monitor/monitor.py watch --view agents --interval 60 --max-hours 12

# 启动／维持后台只读监控，默认最长 24h。重复调用不会启动同目录的第二个监控器。
python3 tools/gpu_monitor/monitor.py maintain
python3 tools/gpu_monitor/monitor.py monitor-status

# 停止本地监控器，远端训练继续按自己的控制器运行。
python3 tools/gpu_monitor/monitor.py stop-monitor
```

`--config /path/to/tasks.json` 与 `--auth /path/to/auth.txt` 可用于以上全部命令。查询与采集器命令的 `--task ID` 可重复使用，选定子集有独立缓存目录和锁；查询/停止该子集监控时使用相同的 `--task` 集合。`stop-task` 必须恰好指定一个 `--task`。默认全量与子集可以并存，但会分别查询，不建议重复监控同一任务。

## 凭据：唯一的 auth.txt

所有 SSH 认证都来自工作区根目录的 [auth.txt](../../auth.txt)。配置里的地址、用户名、端口或认证字段（包括 `hostname`、`host`、`user`、`port`、`password`、`password_env`、`identity_file`、SSH config 别名 `target`）出现即校验失败，不会被凭据文件静默覆盖；SSH `options` 也不能改写认证、askpass 或连接目标。换机器或换密码时只改 `auth.txt`，`tasks.json` 里的主机条目无需改动。

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

密码通过私有临时目录中的 FIFO 交给 OpenSSH askpass（OpenSSH 在 exec askpass 前会 `closefrom()`，继承的文件描述符无法传递密码，故按路径重开命名管道）。写入端和 askpass 读取端都会核验 FIFO 的设备、inode、类型及权限，拒绝被替换的管道或符号链接。密码不写配置或常规文件、不落日志，也不弹交互提示；FIFO 在每次查询后清理。**`auth.txt` 是明文口令，不是安全实践**；该文件已加入 `.gitignore`，若被复制、上传或纳入版本库，请立即在远端轮换密码。

**只允许密码认证**：SSH 只尝试 `keyboard-interactive`/`password` 且 `PubkeyAuthentication=no`。密码认证需要服务器允许 password/keyboard-interactive；若同时要求密码加 MFA、OTP 或多次提示，当前单密码 askpass 不适用。SSH_ASKPASS 必须指向可执行文件路径，不能写成“python3 askpass.py”这样的命令串。超时／暂时性错误按 `connection_attempts` 重试，失败后下轮还会重新查询；认证错误直接报告。无脚本能保证互联网或远端主机永远可连，`UNREACHABLE` 表示当次未知，不代表训练停止。不自动续费或变更远端服务。未配置消息推送或对话自动唤醒，告警只出现在本地记录与输出。

默认人类视图以 GPU 为中心：每台主机单独分组，每张 GPU 单独一块，只列当前能通过 PID 归属确认的运行任务；没有归属的显存进程显示为“其他”。`--view tasks` 才展开训练阶段、步数、指标和日志年龄。`RUNNING` 使用青色，`COMPLETED`/`STOPPED` 使用绿色，`FAILED`/`UNKNOWN`/告警使用红色，暂停和建议使用黄色；颜色只存在于终端渲染，不会进入 `--json`。设置环境变量 `NO_COLOR=1` 或使用 `--color never` 可关闭颜色。长错误、细节和建议会按终端宽度换行，避免把一整条 JSON 日志挤在一行。

轮询可设置 `--interval 60 --max-hours 12`；`--max-polls 2` 便于短验证，`--until-terminal` 在所选任务全部有终态且无残留进程时退出。`UNREACHABLE`、`UNKNOWN`、`EXITED_WITHOUT_RESULT` 不会被当作完成。每轮最多同时探测 8 台主机，所有主机和重试共用 `probe_round_timeout_seconds` 整体截止；默认值为 `timeout_seconds * connection_attempts + retry_delay_seconds * (connection_attempts - 1)`。超时主机报告 `UNREACHABLE`，已完成结果保留。超过 8 台主机时分批仍共用原截止。监控时限是**本地采集器寿命**，最多另加一次整体查询超时；它不延长或执行训练预算。`maintain` 恢复的只有采集器。

已有登记在 [tasks.json](tasks.json)，仅作为当前工作区的配置示例。远端路径、预算字段及日志映射位于配置，主机地址与凭据位于根目录 `auth.txt`，脚本不应写死题目。登记不是新实验授权，也不是自动发现所有云端工作负载；新增运行须登记到配置。历史 PID 只属于对应原运行，后续新运行应使用新的 `launch.json`。

## 有效研究预算显示

登记的 `research_handoff` 必须与状态文件的 `controller: autoresearch-longrun` 和 `run_id` 一致，才读取官方 `budget`。`mode: active` 使用 `active_seconds` 和 `window_seconds` 显示累计活动时间及距目标差额；有效时间上限保持未知（`effective_limit_seconds` 为 `null`）。`hard_limit_seconds` 与 `hard_deadline_at` 只表示墙钟硬上限，结果以 `wall_limit_seconds` 展示；若 `budget.started_at` 存在，监控取 `started_at + hard_limit_seconds` 与 `hard_deadline_at` 的较早者。没有真实启动时间且没有官方截止时，截止保持未知。活动时间的含义由运行冻结的 `credit_policy` 决定，不能单凭监控显示认定 QA16 有效研究时间。

历史 `budget_mode: effective` 登记只在没有官方登记身份且状态文件无官方控制器标记时读取 `agents.sol/seed.effective_seconds`、`effective_target_seconds` 和 `effective_limit_seconds`，要求 `0 < target <= limit`，忽略旧墙钟截止推导。一次观察只选择一个预算来源，官方登记不会混入旧 `agents` 字段。控制器类型或 run 身份不一致会标记 `CONTROLLER_TYPE_MISMATCH` 或 `CONTROLLER_IDENTITY_MISMATCH`，其预算显示为未知，不能从旧格式回填。终态未达目标标记 `EFFECTIVE_TARGET_NOT_REACHED`，超过有效上限标记 `EFFECTIVE_LIMIT_EXCEEDED`；非法计时标记 `OBSERVATION_ERROR`。

实际停止由对应运行冻结的 controller/guard 执行，本采集器仍只读。`maintain`/`watch` 的 `max-hours` 仅控制本地采集寿命。旧运行保留原模式事实，新运行须核对真实入口和新 run-id。

## 查阅任务停止入口

```bash
# 预览登记的官方停止入口；返回 OFFICIAL_STOP_REQUIRED，退出码 2。
python3 tools/gpu_monitor/monitor.py stop-task --task <task-id> \
  --reason '人工检查停止范围' --dry-run

# 记录停止意图与原因；仍只提示入口，不提交停止请求，退出码 2。
python3 tools/gpu_monitor/monitor.py stop-task --task <task-id> \
  --reason '记录本次停止的真实原因'

# 在官方控制器中实际停止后，检查状态与子进程。
python3 tools/gpu_monitor/monitor.py status --task <task-id>
```

必须指定恰好一个 `--task` 和非空 `--reason`。有官方登记时，`stop-task` 返回 `OFFICIAL_STOP_REQUIRED`、退出码 2，并把原因和登记身份写入本地 `stop-requests.jsonl`；没有官方登记时拒绝执行。普通调用与 `--dry-run` 都不会向远端发信号、写文件或转交请求。回执包含保留实际 `--config`、`--auth` 的全量轮询命令及带 `--task` 的单任务复核命令。操作者必须使用该运行已有的连接/状态配置，调用 [research_handoff stop](../research_handoff/README.md#停止恢复与快速-debug) 或 [gpu_scheduler cancel](../gpu_scheduler/README.md) 实际停止指定任务，然后用同一任务范围执行 `status` 或 `watch --view agents` 复核。

## 保持 tasks.json 只包含当前任务

`tasks.json` 是活动登记表。每次新增/接续运行，登记唯一的官方 `run_id`，以及可回查的 scheduler `job_id`/`request_id`、真实进程、预算模式/有效目标和当前容器归属；同一研究 Agent 保留一个当前条目，两组按各自官方 run-id 分开登记。任务独立移交到新 run 后，更新对应条目；旧条目不得通过自定义停止字段继续接管新控制器。包含 `marker`、`process_groups` 或其他自定义停止语义的登记直接视为无效，不能迁移成隐式兼容模式。

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
| `hosts.<name>` | `transport: ssh` 的地址与凭据来自 `auth.txt`；配置中的旧地址或认证字段出现即拒绝。配置只保留 `transport`、`connect_timeout_seconds`、`python`、受限 SSH argv `options`。也支持 `transport: local`。 |
| `connection_attempts` / `retry_delay_seconds` | SSH 查询遇到超时或暂时性错误时的总尝试次数和间隔。认证、主机密钥等确定性错误不会重试。重试不能保证网络、云主机或凭据永远可用。 |
| `timeout_seconds` / `probe_round_timeout_seconds` | 前者限制单次连接/探测，后者限制一轮全部主机及重试的整体时长。未填写时默认值为 `timeout_seconds * connection_attempts + retry_delay_seconds * (connection_attempts - 1)`，分批等待也计入整体时长。 |
| `id` / `host` / `root` | 唯一 ID、主机索引、远端绝对任务根。根目录应限定当前实验，不能用 `/`。 |
| `protocol` | 诊断／正式标签与冻结协议 ID，仅作索引，不自动判定 G01/G02/G03。 |
| `processes` | 使用 `pid_file`＋可选 `pid_key`（点分 JSON 字段），或固定 `pid`，或按命令查找；每项必须有 `contains` 非空特征列表，建议加 `cwd`、可选 `start_ticks`。已匹配进程的后代自动纳入。 |
| `status` / `exit` | `{"path":"status.json","key":"state"}`、`{"path":"worker.json","key":"exit_code"}`。没有退出码不补造；非零退出码不能被成功状态覆盖。 |
| `terminal_states` | 将项目终态精确映射到 `COMPLETED`、`FAILED`、`STOPPED`，例如 `{"COMPLETED":["COMPLETE_SMOKE"]}`。自定义值替换该类默认映射。 |
| `launch` / `budget_key` / `started_key` | 从真实 launch JSON 计算绝对截止；默认键为 `budget_seconds`、`started_at`。也可声明带时区的 `deadline_at` 或 `deadline_file` 的 path/key；后两者优先。缺失预算显示未知。 |
| `gpu_uuids` | 展示指定 GPU 的遥测与进程；空列表显示全部。`belongs_to_task` 区分任务已核验 PID 与其他 PID，其他进程不自动判定为冲突。 |
| `streams` | 明确日志路径、格式、指标字段、可选过滤、方向、总步数、停滞阈值。每文件默认只读末尾 64 KiB；相同文件的连续观察复用已解析行，仅解析新增或变化的部分，缓存随尾部窗口淘汰，轮转/截断重置。不读 checkpoint。 |
| `stale_seconds` | 无日志／步数推进的阈值，按实际日志频率、长评估或保存耗时设置；流内覆盖任务值，0 禁用。默认任务 900 秒；长间隔日志应使用更大的预声明阈值。 |
| `streams[].complete_pattern` | 可选的阶段结束文本正则，例如进入 `features`/`probe` 后不再对适配步数报警；保留实际最后日志步数，不补造缺失的最终步。 |
| `streams[].error_patterns` | 可选错误规则列表，每项为 `{"pattern":"正则","severity":"warning/error/critical"}`。未配置时使用内置 Python/PyTorch/NCCL 错误特征；空列表禁用错误特征扫描。匹配详情保留规则及严重程度，仍以 `ERROR_LOG` 统一告警。 |
| `streams[].error_window_lines` | 错误扫描限定在尾部最近 N 行，默认 100，须为正整数。旧错误离开窗口后不再触发当前告警。 |
| `min_disk_free_gib` | 输出盘低空间告警阈值，默认 2 GiB。 |

JSONL 日志示例：

```json
{"event":"progress","step":100,"loss":0.7,"elapsed_seconds":12.0}
```

流配置 `format: jsonl`，`fields: {"step":"step","metric":"loss","elapsed_seconds":"elapsed_seconds"}`，可用 `where: {"event":"progress"}` 筛选事件。字段支持点分嵌套；输出使用统一键 `step`、`metric`、`elapsed_seconds`，其他损失字段也可保留。

文本日志可用 `format: regex` 和带命名捕获组的 `pattern`，例如 `adapt (?P<step>\d+) / \d+ loss (?P<metric>\S+) elapsed (?P<elapsed_seconds>\S+)`；捕获值须是数字。JSONL 与 regex 只解析以换行结束的完整记录，末尾半行在后续追加后解析；错误特征仍扫描半行。`format: text` 只收集末尾文本、日志新鲜度和错误特征。输出 JSON 中 NaN/Inf 转为字符串并告警，保证机器可严格解析。增量缓存仅保留在本地进程内，最多 256 个有界尾部窗口；重启后首轮重新解析，观测文件只保存缓存标识，不保存缓存原文。

建议启动器保存 `launch.json` 的真实 PID、开始时间、预算、PGID、命令与 cwd；结束时原子写 `worker.json` 的退出码。监控不修改历史 launch/handoff 文件。匹配只面向 SSH 当前账号可观察的 Linux 进程；容器内 PID 不等于宿主 PID，Docker/Slurm/Kubernetes 作业应通过对应可信启动器导出宿主进程和状态合同后接入，当前工具不直接调用这些平台 API。

## 状态、告警与研究判断

| 状态／告警 | 含义与处理 |
|---|---|
| `RUNNING` / `PAUSED` | 至少一个进程身份匹配；不等于梯度仍推进，结合日志和指标。 |
| `COMPLETED` / `FAILED` / `STOPPED` | 声明停止且退出码为 0 或缺失时保留 `STOPPED`；其他情况采用可用退出码（0 为完成，非零为失败），没有退出码才采用声明终态。保留原始声明及退出码；二者冲突或终态仍有进程存活时额外 `STATUS_CONFLICT`。 |
| `NOT_STARTED` | 没有可见进程或已有运行记录，不证明其他路径没有任务。 |
| `EXITED_WITHOUT_RESULT` | 已有运行记录，但无匹配进程、无可信终态；可能是中断、控制器崩溃或登记错误。 |
| `UNREACHABLE` / `UNKNOWN` | SSH 查询失败／进程权限不足；不以旧快照报告仍运行。JSON 保留最后成功查询时间和历史状态。 |
| `STALE_LOG` / `STALLED_PROGRESS` | 日志过旧／跨轮询步数没推进；确认是故障还是正常长阶段。重启／日志轮转会重置步数停滞窗口。 |
| `NONFINITE` / `ERROR_LOG` | 日志末尾有数值或错误特征，需看上下文；不是自动停止依据。 |
| `DEADLINE_EXCEEDED` / `ETA_OVER_BUDGET` | 原截止已到且尚未确认结束／近期吞吐预计超时；检查原控制器时限。 |
| `IDENTITY_MISMATCH` / `OBSERVATION_ERROR` | PID 复用或身份不符／部分来源不可读。 |
| `CONTROLLER_TYPE_MISMATCH` / `CONTROLLER_IDENTITY_MISMATCH` | 状态文件控制器类型或 run 身份与登记不一致；不接受该来源的声明终态和预算，也不自动归档或触发 `--until-terminal`。 |

ETA 只估当前流到其 `total_steps` 的训练时间，优先使用日志中两个进度点的真实 elapsed 差；缺少时使用连续两次观察的步数差。首轮无法估计就显示 `?`，无活进程时不显示完成预期。ETA 不包括后续 probe、重载、排队；短期吞吐会波动。

`best_in_tail`、`metric_delta_in_tail` 是**有界日志窗口**的描述统计，不是全程最佳 checkpoint，更不能用来挑正式 seed。不同方法、阶段、步数或协议不自动排名。学习信号足以触发人工检查，但达标、换方向或停止正式实验仍按各题授权与冻结协议判断。

## 记录、接管和故障恢复

后台目录默认为本工具的 `.state/`，包含：

- `latest.json`：原子更新的最近快照，含采样时间、主机/GPU分组、任务归属、指标、告警与建议；直接读缓存须核对时间和采集器存活，优先运行实时 `status`。
- `observations-YYYY-MM-DD.jsonl`：按 UTC 日期追加每轮轻量观察。
- `events-YYYY-MM-DD.jsonl`：首次观察、状态或告警集合变化；不会每轮重复相同事件。
- `watch.json`、`watch.lock`、`collector.log`：本地采集器身份、截止、最后轮询、退出原因、互斥锁及终端输出。
- `stop-requests.jsonl`：停止意图／预览的本地回执，记录 `OFFICIAL_STOP_REQUIRED`；不表示控制器已收到停止请求。

这些是监控记录，**不代替原始训练证据**。默认不拉模型、数据集或完整日志；模型回收仍使用各题既有 `pull` 合同，并核验哈希。记录按日分文件，不自动删除；长期复用时按项目证据保留规则归档。不要把含私有研究结果的监控目录挂载给正式 solver。

当前auth.txt口令模式使用BatchMode=no与受控askpass；每次查询禁用ControlMaster/ControlPath复用，避免旧socket卡住采集。主机地址与凭据取自auth.txt，配置只保留transport、connect_timeout_seconds、python、options等。该认证合同与通用控制器的SSH key/agent不同。

退出码：`0` 命令成功，`1` 配置／采集器操作错误，`2` 为 `status --check` 发现告警或 `stop-task` 提示 `OFFICIAL_STOP_REQUIRED`。默认 `status` 通过 JSON 表达远端不可达，不因一个离线主机隐藏其他任务。后台轮询持续记录离线情况；其时限、终态条件和本地采集器停止请求各自生效。

## 验证

```bash
python3 -m unittest discover -s tools/gpu_monitor/tests -v
python3 tools/gpu_monitor/monitor.py validate
```

本地检查覆盖断连、状态优先级与冲突、权威预算与身份校验、整轮超时、增量日志与半字符、轮转、可配置错误窗口、FIFO 篡改、SSH 重试、凭据热重载、路径边界及只读停止指引。旧云端查询和停止预览记录见 [verification.json](verification.json)，这些历史记录不证明当前源码已完成动态 SSH 复验。
