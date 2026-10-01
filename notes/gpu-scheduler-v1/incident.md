# GPU 调度模块第一版开发与验证

日期：2026-09-30。用途：按用户确认的范围实现单机、内存队列、全局最多两个 GPU 作业、可配置同卡共享、本机 CLI 提交。

最初授权范围为工具实现与必要本地测试；后续用户明确要求实际登录云 GPU 测试 debug，并指定根目录 `auth.txt`。同轮用户又授权处理数据盘扩容。当前授权包含代码部署、短 GPU 合成计算验收、故障注入和必要修复，不包含正式研究 Agent、题目训练、购买或续费。

- `scratch/`：正式测试创建的临时配置、模拟运行目录、CPU 子进程日志；每次测试退出后按本次 token 清理子进程并删除该测试目录。不包含研究数据或唯一实验记录。
- `evidence/`：保留测试命令、输出、实现版本哈希与完成记录。失败验证输出保留，后续运行不覆盖。
- 完成判据：并发上限、同卡预算、排队公平、幂等、停止与取消、超时后代清理、调度器崩溃后的独立截止和重启互斥均有本地测试。
- 下一步验收：在用户授权的目标 GPU 主机按 README 做数据盘预检与真实 GPU 运行验收；本地测试不证明显存硬隔离或实际吞吐收益。

当前状态：第一版实现、本地验证、真实云 GPU 短时验收及双 SSH 客户端八请求验证完成；数据盘扩容已生效，测试进程与可再生缓存已清理。

## 已完成

- 入口：[工具使用说明](../../tools/gpu_scheduler/README.md)，包括 CLI、Python SDK、配置字段、退出合同和现有长程控制器接法。
- 单进程内存队列，所有接入请求合计最多两个 GPU 作业；一张卡的显存/计算份额可供两个作业共享。CPU/RAM 同时做声明额度准入。
- 有界 FIFO 回填、排队超时、父任务截止、同会话幂等和跨会话拒绝、独立执行器、定向清理与不确定资源隔离。
- 复用现有 `research_handoff/core/processes.py` 的身份与清理实现，未更改既有控制器、监控器或题目适配器。

## 验证证据

- 初轮 12 项通过，终端摘要留于 [test-initial.txt](evidence/test-initial.txt)。
- 最终 13 项通过，开启 `ResourceWarning` 错误检查；[完整输出](evidence/test-final-01.log)、[真实命令/起止/退出码](evidence/test-final-01.json)。
- 覆盖声明预约、未知进程/陈旧遥测拒绝、非法配置、两个共享作业与第三个排队、并发幂等、排队与等待超时、单作业取消、公平回填、正常停止、执行器丢失清理、服务 SIGKILL 后截止回收与重启互斥/旧会话拒绝。
- Python AST、示例 JSON 解析和真实 CLI help 核对通过；测试 scratch 已清空。代码与文档哈希见 [manifest.json](evidence/manifest.json)。

## 边界与下一步

显存、计算份额、CPU/RAM 都是合作式预约，不是硬隔离。当前仅支持可信同用户的本机前台进程树，不支持经 Docker/Harbor/provider 启动的外部作业回收；没有数据库、队列恢复、自动重试、抢占或多主机功能。

目标主机验收须核对真实挂载和 GPU UUID，测量单作业与两作业同卡时的峰值、吞吐、OOM 和取消结果；未做这些验证前不声称真实 GPU 收益或资源硬限额成立。按具体运行授权接入题目适配器，并保留父 run 原截止及排队计时。

## 云端测试与数据盘扩容（2026-09-30）

- 已通过用户指定 `auth.txt` 和现有 gpu_monitor 的受管 SSH 接口实际登录，未输出凭据。新主机 GPU 为 RTX 4090，UUID `GPU-dddea253-44d0-ac0a-77e1-8d22f8070439`，与历史登记不同；启动前无 GPU 计算进程。Python 3.10 系统解释器运行控制服务，数据盘现有 Python 3.11 / PyTorch 2.6.0+cu124 运行合成 CUDA 作业。
- `/dev/vdb` 已扩大为 500 GiB，原 ext4 仍为 200 GiB。按用户指令核对 UUID、真实挂载和 fstab 后执行 `sudo -n resize2fs /dev/vdb`，退出 0；`/mnt/autoresearch` 保持在线挂载，文件系统约 492 GiB、可用约 351 GiB。证据：[扩容前](evidence/cloud-disk-before.json)、[实际扩容命令和结果](evidence/cloud-disk-resize.json)。未格式化、卸载或修改 fstab。
- 公共 Docker data-root 在数据盘，但系统 containerd 使用默认配置；本次使用本机前台 Python 作业，不使用或修改公共容器服务，不启动额外 Docker/containerd。所有测试新增源码、配置、日志和缓存都在数据盘独立根。
- 第一轮测试根：`/mnt/autoresearch/work/gpu-sched-20260930-024324`；只部署白名单工具源码与合成测试脚本，并逐文件核对 SHA-256。部署/启动命令留于 [cloud-launch-01.py](evidence/cloud-launch-01.py)，回执见 [cloud-launch-01.json](evidence/cloud-launch-01.json)。
- 测试 harness 初始 PID 5469、起始 02:43:27 UTC；总测试检查窗口 180 秒，单个服务 180 秒，单作业最多 40 秒。最多两个并发作业，各声明 2048 MiB 和 30 计算份额，服务使用一张实测空闲 GPU。仅合成张量矩阵乘法，不读取数据/模型。
- 查询：读取测试根 `harness.log`、`evidence/report.json`、`queue/sessions/*/jobs/*/exit.json`；停止服务使用本发布副本 CLI `stop --root <测试根>/queue`，按真实 PID/身份停止 harness，执行器保留原截止。不得按本条历史 PID 盲目信号操作。
- 最终结果：02:43:27–02:44:01 UTC，约 33.71 秒完成 5 类真实 GPU 验收、7 个作业，全部清理成功；无非空作业 stderr，无观察到的 OOM。142 次遥测，最多同时 2 个 GPU 计算进程，采样显存峰值 1135 MiB。主动取消/超时产生的 -15/-9 是预期故障注入结果，未冒充正常训练退出。
- 证据已回收到本地并核验 SHA-256，云端原件保留。归档 `evidence/cloud-run-01.tar.gz`，哈希 `77a5ac9cce9b2d1e0ec10464166aee567733c0dcbd8cd2fd0f728e0bc107573c`；[汇总](evidence/cloud-summary-01.json)、[原始报告](evidence/cloud-run-01/evidence/report.json)、[清理复核](evidence/cloud-cleanup-01.json)。原始报告 `jobs` 数组是提交回执，不是终态；实际终态以各作业 `exit.json` 及 case 断言为准。
- 收尾确认无本测试进程、无 GPU 计算进程，显存 1 MiB、GPU 利用率 0%。只删除本次 `cache/` 与 7 个作业的 `scratch/`，保留源码、配置、日志、失败注入记录和归档。没有遗留运行中的测试服务。
- harness 的操作系统级 wait 退出码未由脱离 SSH 的启动器留存，按规则记 `null`；不从 `passed=true` 编造退出码。7 份执行器作业回执含真实 returncode。此缺口不影响已直接核验的 GPU 进程、队列与回收结果。
- 核心模块无云端修复变更；新增可复用的显式真实 GPU 验收脚本。下一步可按具体题目授权接入可信评分适配器；本次没有正式 Agent、研究训练、容器/Harbor 或长时吞吐验收。

## 双客户端接入（同轮后续需求）

用户明确要求两个本地 SSH 客户端共同查询、控制、编排最多 8 个长期 Agent 的 GPU 请求，并通过控件确认同一云端 Linux 账号、双方可控制全部任务。保留云端单进程、内存队列和 GPU 全局并发 2，不增加数据库或跨用户权限系统。

新增 `RemoteClient`、CLI `--remote` 和 stdin RPC 桥接。复用 gpu_monitor 的 SSH/askpass；为包导入在 monitor.py 增加相对导入兼容，未修改认证语义。15 项调度测试及 45 项监控测试通过。

实际云端新发布/测试根 `/mnt/autoresearch/work/gpu-multi-20260930-025437`。两个独立本地进程各提交 4 份真实 CUDA 请求，8 个作业全成功；再通过两个 GPU 作业验证双方互相取消。所有 10 个作业实际初始化 CUDA、清理成功，无 stderr；执行区间峰值和 GPU 进程峰值均为 2。双方客户端退出码和服务退出码均为 0；测试服务已停止，无 GPU 残留。详见 [双客户端验收记录](cloud-multiclient.md)。

新增接入接口已可使用；具体题目仍需在自己的可信评分适配器中调用，保留原 run 截止、独立 owner/request_id 和排队计时。当前不包含 8 个正式研究 Agent 的长时运行或性能收益验收。

## 阻塞提交与资源排队更新（2026-10-01）

- `Client.submit`、`RemoteClient.submit` 和 CLI `submit` 现在是首选阻塞入口：服务端接受并校验请求后，资源不足时进入 `QUEUED`，调用保持等待，直到成功、失败、取消、超时、排队过期或 `UNKNOWN`。
- `Client.submit_async` 与 CLI `enqueue` 保留给需要持续跟踪进度或同时编排多个任务的本地控制 Agent；异步返回后必须用 `wait(job_id)` 或 `get(request_id=...)`，不应让 Agent 把入队回执当作训练完成。
- 静态校验错误不创建队列作业；运行时资源不足只改变排队原因，不把合法请求误报为失败。排队仍受 `queue_timeout_seconds` 和父级 deadline 约束。
- 阻塞等待使用服务端作业状态事件，远程 SSH 配置增加 `wait_timeout_seconds`；连接断开不自动取消或重放，必须先查询原 `request_id`。
- 本次只更新实现、测试和规范；2026-09-30 云端短测的原始事实和证据路径保持不变，未将新协议回填为旧实测能力。
