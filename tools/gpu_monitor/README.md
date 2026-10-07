# AutoResearch 通用 GPU 任务监控

本工具是正式双 Agent 长跑的受管巡检/登记入口，不另造轮询守护脚本。整体运行、OOM/无进展判断和资源边界见 [统一规范](../../双Agent长跑与题目包验收规范.md)，日志归档与临时文件规则见 [目录治理](../../notes/长程Agent脚本与目录治理.md)。监控观察不等于科研有效时间、GPU硬限额执行或平台验收。

登记表只接受官方任务身份：`research_handoff` 的 `run_id` 与 `gpu_scheduler` 的 `job_id`/`request_id` 必须可回查。监控是只读聚合视图，不启动、恢复、重试或追加训练；任务停止仍由对应官方控制器执行。旧 `stop`、`marker`、`process_groups` 字段只要出现就拒绝，即使值为空也不接受；应先修订登记并通过 `validate`。

共享 GPU 最新要求：在不影响他人的前提下继续自己的训练，其他计算进程出现本身不触发停训；监控只记录占用/余量/负载，不自动发停止指令。真正安全风险、用户停止和预算到期由对应任务合同处理，不干预他人。

用于专家和协作 Agent 在多个题目、多个 SSH 云主机间查询、轮询和接管任务。人类只有一个 `watch` 入口和一个统一终端空间；机器读取使用 `status --json`。默认 **60 秒采集、最近 12 小时 48 个 15 分钟格、轻量本地记录**，一次快照同时包含 endpoint/GPU、scheduler、全部 Agent 流、时间、队列和告警。`stop-task` 仅返回官方停止入口提示并记录本地回执，不向远端或控制器发送停止请求。

依赖：机器接口及远端探针使用 Linux、Python **3.9+** 标准库；本地交互界面使用 Python **3.10+** 与锁定版本的 **Textual 8.2.8**。SSH 主机需 `ssh`，GPU 遥测使用已有 `nvidia-smi`。远端无需安装 Python 包、监控服务或上传脚本文件。只读探针通过 SSH 标准输入运行，一台主机每轮一次探测（暂时性错误可重试）；多个主机并发查询。

## 直接使用

从项目根目录运行，命令与当前目录无关的部分均由脚本自身定位：

首次使用 TUI，在本地专用环境安装依赖并激活（机器接口不需要此步骤）：

```bash
python3 -m venv tools/gpu_monitor/.venv
tools/gpu_monitor/.venv/bin/python -m pip install -r tools/gpu_monitor/requirements-tui.txt
source tools/gpu_monitor/.venv/bin/activate
```

若系统 Python 没有 `ensurepip`，可使用已有 `uv venv tools/gpu_monitor/.venv` 与 `uv pip install --python tools/gpu_monitor/.venv/bin/python -r tools/gpu_monitor/requirements-tui.txt`。也可直接使用 `tools/gpu_monitor/.venv/bin/python` 执行以下命令。

```bash
# Agent 获取结构化脱敏快照；默认即使任务告警也成功返回 JSON。
python3 tools/gpu_monitor/monitor.py status --json

# 只看一个任务；--check 遇到任一告警退出 2。
python3 tools/gpu_monitor/monitor.py status --task <task-id> --json --check

# 唯一人类监控入口；已有后台采集器时直接复用快照。
python3 tools/gpu_monitor/monitor.py watch

# 持续轮询所有已登记的官方长时间 Agent 任务；配置每 60 秒独立检查。
python3 tools/gpu_monitor/monitor.py watch --interval 60 --config-check-interval 60 --max-hours 12

# 启动／维持后台只读监控，最长 12h。重复调用不会启动同目录的第二个监控器。
python3 tools/gpu_monitor/monitor.py maintain
python3 tools/gpu_monitor/monitor.py monitor-status

# 停止本地监控器，远端训练继续按自己的控制器运行。
python3 tools/gpu_monitor/monitor.py stop-monitor
```

`--config /path/to/tasks.json` 与 `--auth /path/to/auth.txt` 可用于以上全部命令。`--task ID` 可重复使用；一个配置只有一个采集锁和状态目录。交互 `watch` 遇到已有采集器时打开快照视图，不读取 SSH 凭据、不重复探测远端；非交互采集继续拒绝重复持锁。前台采集范围在启动时固定；TUI 的 `/` 筛选只改变显示。`stop-task` 必须恰好指定一个 `--task`。

## TUI 操作与布局

交互 `watch` 使用终端备用屏幕与局部更新，刷新不追加滚屏。`watch --json`、输出重定向和 `maintain` 使用脱敏 JSON，不导入 Textual。缺少交互依赖时明确提示安装，不回退到整页打印。

默认资源摘要在上方，正式长时间 Agent 每个登记任务一行，选中任务显示时间线、计时、身份、作业、日志流及告警。Agent 只包含 `controller.type=research_handoff` 且有 `run_id` 的官方长运行登记；训练、评分、队列等其他任务放入默认折叠的**通用任务**，计数分别统计。身份暂不可验证的已登记 Agent 仍保留并显示对应告警。机器快照 `agents` 采用相同分类，完整 `tasks` 继续包含所有登记任务。

≥120 列左右布局，80–119 列上下布局，小于 80 列使用简化任务表并通过 Enter 打开完整详情。长 ID 在表格省略，详情可查看完整值；48 格时间线在窄详情中横向滚动。窗口缩放与采集更新保留任务选择、焦点和可用滚动位置。

| 按键 | 操作 |
|---|---|
| ↑↓ / 鼠标 | 选择任务、滚动；点击通用任务标题展开或折叠 |
| Tab | 切换焦点，包括折叠标题、任务表、详情和滚动区 |
| Enter / Esc | 打开详情 / 返回；Esc 也可清除筛选 |
| `/` | 按任务名称、ID、主机筛选；匹配通用任务时自动展开 |
| `r` | 前台请求采集（进行中的请求合并）；后台模式重读本地快照 |
| `g` | 查看完整 GPU、归属、预约资源与队列 |
| `?` | 查看帮助与图例 |
| `q` / Ctrl-C | 退出本地界面 |

图例：`■` 运行（含待结算），`W` 等待/排队/有依据的对账等待，`!` 失败/重试，`S` 已停止，`·` 未启动，`?` 未知/数据缺失，`=` 完成或轮次结束。运行活动与结算数字分别显示，不再将“计时中”列为独立状态。颜色与符号使用统一大小写映射，`--color never` / `NO_COLOR` 可关闭颜色。

计时标为“本轮经过”“本轮待确认估计”“累计已确认”“距有效目标”“距硬截止”；数字只更新自真实快照，不通过界面动画增加信用。“本轮非等待时长估计”不表示真实 GPU 执行用时。RAM 标为可用量/总量，GPU 实测利用率与预约 CU 分列。分数趋势按优化方向显示改善、退步或持平。

顶栏显示采集模式、间隔、更新时间和数据年龄；年龄超过 `max(2×间隔, 间隔+上次采集耗时)` 标记过期。后台停止、快照损坏或暂不可读时保留最后有效画面并标记状态，不自动接管或恢复采集。正文默认突出当前告警，已恢复告警和时间格事件保留在详情。

前台退出会结束自己持锁的本地采集，在当前探测截止内完成清理；后台快照模式退出只关闭界面。后台模式的 `--interval` / 配置检查参数不覆盖已有采集器；`--max-hours` 限制本地界面寿命，`--max-polls` 统计不同快照，`--until-terminal` 只按新鲜且满足既有终态合同的快照退出。快照中的其他任务不会被界面筛选暂停或停止。

私有分数与 B/R 只在**前台直接采集的操作者 TTY** 中通过单次快照内存 overlay 显示；后台快照模式明确显示不可用，不另开探测补齐分数。JSON、状态文件、events 和日志继续脱敏。

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
- 换凭据后无需手工 `ssh-keyscan` 或删除 known_hosts：启动时使用托管 `.state/known_hosts`；`watch` 默认每 60 秒独立检查认证文件指纹，变化后才解析，IP/用户/端口变化时刷新对应主机密钥，凭据写坏时保留上一份可用值。
- 可用 `--auth /path/to/auth.txt` 或环境变量 `AUTORESEARCH_AUTH_FILE` 指定别的凭据文件；`validate`、`monitor-status`、`stop-monitor` 不连接远端，`auth.txt` 不存在时也能运行。

密码通过私有临时目录中的 FIFO 交给 OpenSSH askpass（OpenSSH 在 exec askpass 前会 `closefrom()`，继承的文件描述符无法传递密码，故按路径重开命名管道）。写入端和 askpass 读取端都会核验 FIFO 的设备、inode、类型及权限，拒绝被替换的管道或符号链接。密码不写配置或常规文件、不落日志，也不弹交互提示；FIFO 在每次查询后清理。**`auth.txt` 是明文口令，不是安全实践**；该文件已加入 `.gitignore`，若被复制、上传或纳入版本库，请立即在远端轮换密码。

**只允许密码认证**：SSH 只尝试 `keyboard-interactive`/`password` 且 `PubkeyAuthentication=no`。密码认证需要服务器允许 password/keyboard-interactive；若同时要求密码加 MFA、OTP 或多次提示，当前单密码 askpass 不适用。SSH_ASKPASS 必须指向可执行文件路径，不能写成“python3 askpass.py”这样的命令串。超时／暂时性错误按 `connection_attempts` 重试，失败后下轮还会重新查询；认证错误直接报告。无脚本能保证互联网或远端主机永远可连，`UNREACHABLE` 表示当次未知，不代表训练停止。不自动续费或变更远端服务。未配置消息推送或对话自动唤醒，告警只出现在本地记录与输出。

统一人类界面与快捷键见上文。`--view` 及人类 `status` 已取消并明确提示迁移；正式分数与冻结 B/R 不进入非 TTY、JSON、状态文件、events、Agent/handoff 和 workspace。

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
python3 tools/gpu_monitor/monitor.py status --task <task-id> --json
```

必须指定恰好一个 `--task` 和非空 `--reason`。有官方登记时，`stop-task` 返回 `OFFICIAL_STOP_REQUIRED`、退出码 2，并把原因和登记身份写入本地 `stop-requests.jsonl`；没有官方登记时拒绝执行。普通调用与 `--dry-run` 都不会向远端发信号、写文件或转交请求。回执包含保留实际 `--config`、`--auth` 的全量 `watch` 命令及带 `--task` 的复核命令。操作者使用该运行已有配置，调用 [research_handoff stop](../research_handoff/README.md#停止恢复与快速-debug) 或 [gpu_scheduler cancel](../gpu_scheduler/README.md) 实际停止指定任务，然后用 `status --json` 或统一 `watch` 复核。

## 保持 tasks.json 只包含当前任务

`tasks.json` 是活动登记表。每次新增/接续运行，登记唯一的官方 `run_id`，以及可回查的 scheduler `job_id`/`request_id`、真实进程、预算模式/有效目标和当前容器归属；同一研究 Agent 保留一个当前条目，两组按各自官方 run-id 分开登记。任务独立移交到新 run 后，更新对应条目；旧条目不得通过自定义停止字段继续接管新控制器。包含 `marker`、`process_groups` 或其他自定义停止语义的登记直接视为无效，不能迁移成隐式兼容模式。

任务完成、明确停止或过期且确认无存活进程后，及时移出活动表。历史条目保存到 `archive/tasks-<UTC>-<id>.json`，原始日志、`.state` 和远端证据不删除。`registry.py` 只操作本地配置，不连接 SSH，也不会停止训练。

```bash
# 先取得新的只读状态快照。
python3 tools/gpu_monitor/monitor.py status --json > <本任务事故目录>/monitor-status.json

# 预览可归档的终态/过期条目；默认不改 tasks.json。
python3 tools/gpu_monitor/registry.py prune --snapshot <本任务事故目录>/monitor-status.json

# 应用同一新鲜快照，保存历史配置，再移出活动表。
python3 tools/gpu_monitor/registry.py prune --snapshot <本任务事故目录>/monitor-status.json --apply

# 已核实被替代、旧云机或过期登记，可明确指定 ID 与原因。
python3 tools/gpu_monitor/registry.py archive --task <old-task-id> \
  --reason '该登记已由新的 run 替代，旧运行已结束' --apply

# 通过归档文件查询历史登记；认证仍来自指定的 auth.txt。
python3 tools/gpu_monitor/monitor.py status --config tools/gpu_monitor/archive/<archive-file>.json \
  --task <old-task-id> --json
```

自动筛选要求快照默认不超过 300 秒、状态可观察且无匹配进程。`UNREACHABLE`/`UNKNOWN`、过旧观察、存活进程、身份/观察冲突不自动归档；截止过期但进程仍活着也必须保留并调查。归档预览输出配置 SHA256，可在应用时加 `--expected-sha256 <hash>`，防止覆盖预览后的配置变更。所有条目归档后允许空表，`maintain`/`watch` 会直接返回。

运行中的采集器保存固定的 `--task` 筛选，但按独立低频时钟热重载 `tasks.json`；稳定指纹未变化时只做 `stat()`，半写、损坏或暂缺候选保留上一份有效配置并记录脱敏事件。`state_dir`、配置大版本、路径和任务筛选变化提示重启，不迁移状态。认证文件也按独立指纹低频刷新，损坏时保留上一份凭据。

接手时的顺序是：`monitor-status` 确认采集器 → `status --json` 或统一 `watch` 查询当前远端 → 检查真实心跳、轮次/三类时间及原截止 → 更新题目进展清单 → 归档已完成或过期登记。告警不能直接替代故障判定；例如当前长评分尚未结束，轨迹暂不增加仍可能正常。

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

流配置 `format: jsonl`，`fields: {"step":"step","metric":"loss","elapsed_seconds":"elapsed_seconds"}`，可用 `where: {"event":"progress"}` 筛选事件。字段支持点分嵌套；解析器内部可计算窗口统计，公开快照只保留步数/时间/事件等运维字段，移除指标值和日志原文。

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

解析器的窗口统计不是正式成绩，不参与操作者评分列，不写共享快照。不同方法、阶段、步数或协议不自动排名；达标、换方向或停止正式实验仍按各题授权与冻结协议判断。

## 记录、接管和故障恢复

后台目录默认为本工具的 `.state/`，包含：

- `latest.json`：原子更新的脱敏快照，含版本、配置 SHA/revision、生效轮次、资源/归属、三类时间、告警历史与时间带；读取缓存须核对时间，实时机器接口为 `status --json`。
- `observations-YYYY-MM-DD.jsonl`：按 UTC 日期追加每轮轻量观察。
- `events-YYYY-MM-DD.jsonl`：首次观察、状态或告警集合变化；不会每轮重复相同事件。
- `watch.json`、`watch.lock`、`collector.log`：本地采集器身份、截止、最后轮询、退出原因、互斥锁及终端输出。
- `stop-requests.jsonl`：停止意图／预览的本地回执，记录 `OFFICIAL_STOP_REQUIRED`；不表示控制器已收到停止请求。

这些是监控记录，**不代替原始训练证据**。默认不拉模型、数据集或完整日志；模型回收仍使用各题既有 `pull` 合同，并核验哈希。记录按日分文件，不自动删除；长期复用时按项目证据保留规则归档。不要把含私有研究结果的监控目录挂载给正式 solver。

当前auth.txt口令模式使用BatchMode=no与受控askpass；每次查询禁用ControlMaster/ControlPath复用，避免旧socket卡住采集。主机地址与凭据取自auth.txt，配置只保留transport、connect_timeout_seconds、python、options等。该认证合同与通用控制器的SSH key/agent不同。

退出码：`0` 命令成功，`1` 配置／采集器操作错误或旧显示入口，`2` 为 `status --json --check` 发现告警或 `stop-task` 提示 `OFFICIAL_STOP_REQUIRED`。机器接口用 JSON 表达远端不可达，不因一个离线主机隐藏其他任务。后台轮询持续记录离线情况；其时限、终态条件和本地采集器停止请求各自生效。

## 新版只读数据合同

快照 `schema_version: 2` 保留任务/hosts/agents 等机器兼容投影，增加 `config`、`summary`、`display`、`alerts` 和可恢复的 `diagnostics`。同地址、端口、用户及 `auth_profile` 的别名合并为一个无密码 endpoint；不同身份不合并。GPU/进程按 endpoint、UUID、PID、boot 和启动 ticks 去重。

归属顺序为官方可信容器 receipt 与 boot/PID/cgroup 身份、登记容器 ID、普通已核验 PID。每个结果给出 `source/confidence/reason`，冲突或权限不足为 unknown；不猜测 containerd-shim 子进程属于哪个任务。实测遥测在 `measured`，scheduler 显存/CU/RAM/CPU 预约在 `reserved`，实际主机资源在 `host_resources`；预约不代表硬隔离。

登记 `scheduler.root/session_id/job_ids/request_ids` 后，探针只读该范围内的 status、受限 events 尾部与 durable 账本，关联当前 attempt。请求模板只改变数字轮次，保留 group 和 `/research` 后缀。`configured_job_ids` 永远是历史登记，`active_job` 来自实际状态；终态进入 history。冲突或损坏账本为 unknown，UNKNOWN 对账/INFEASIBLE/EXPIRED 使用各自运维语义，监视器不做恢复、重提或取消。

`timing` 区分实时 `live_elapsed_seconds`、待结算 `pending_seconds` 和官方已确认 `credited_effective_seconds`。reported 的 pending 只是估计，不提前增加正式信用；WAITING_GPU 增加 live 并排除等待，原墙钟截止不延长。每条流 `timeline_12h` 固定 48 格并记录事实、事件类别和空洞原因。跨轮告警覆盖时间不可达/未知、Agent retry、交替坏事件、summary 停滞、队列反复过期、容器 OOM/内存上限与共享资源阻塞；恢复/重开和计数在重启后继续，空洞不立即清零。

阈值在 `diagnostics.thresholds` 中配置，例如 `retry_warning: 3`、`retry_critical: 5`、`summary_warning_generations: 10`、`summary_critical_generations: 20`。summary 本身保持 warning，只有经验证的正式进展也停滞才升级。配置解析会校验阈值；`--interval` 优先于热重载设置，`--max-hours` 的原本地截止固定且不超过 12 小时。`--config-check-interval` / `--auth-check-interval` 默认各 60 秒，不随高频采集读取文件。

操作者评分来源可在可信私有登记中增加 `operator_overlay`：`task_id`、`records: [{"contract_path":"relative/completion.contract.json"}]`、可选 `metadata_path` 和预先冻结的 `metadata_sha256`。元数据的 `frozen_anchors` 包含 B/R；不把数值写进 tasks.json。只接受官方 completion 验证通过的 formal/final 完整 seed、身份/源码/数据/模型 hash 和签名记录；proxy、diagnostic、screen、失配、缺证据不显示数值。验证代码在同一个 probe 进程内执行，无需在 SSH 主机安装包或写入代码。缺少可信登记时显示 unavailable；监控不搜私有 evidence，也不根据当前分数调整 B/R。

只有操作者 TTY 临时合并分数、B/R 和按改进方向解释的趋势。基础 JSON、非 TTY 日志、latest/watch/observations/events 不含评分、指标数值、原始日志、私有完整路径或验证内容。控制器不调用 gpu_monitor，不捕获这一屏幕到 Agent 上下文；不得把私有 `.state` 或旧历史备份挂给 solver。旧记录保留原件，隔离不能靠新脱敏假装旧历史已安全。

## 验证

```bash
tools/gpu_monitor/.venv/bin/python -B -m unittest discover -s tools/gpu_monitor/tests -v
python3 tools/gpu_monitor/monitor.py validate
```

本地检查覆盖断连、状态优先级与冲突、权威预算与身份校验、整轮超时、增量日志与半字符、轮转、可配置错误窗口、FIFO 篡改、SSH 重试、凭据热重载、路径边界及只读停止指引；Textual Pilot 另验小窗口、动态缩放、Agent/通用分类、筛选、详情刷新、选择/滚动保持、后台复用和退出。标准库环境可运行机器接口测试，缺少 Textual 时会跳过交互用例。旧云端查询和停止预览记录见 [verification.json](verification.json)，这些历史记录不证明当前源码已完成动态 SSH 复验。
