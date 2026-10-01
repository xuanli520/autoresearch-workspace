# GPU 调度器阻塞提交与资源排队变更

日期：2026-10-01。范围是 `tools/gpu_scheduler` API、CLI、测试和相关使用规范；本轮不部署云端、不启动真实 GPU 训练、不热覆盖既有 release，不修改公共服务。

## 已确认的合同

- 本地控制 Agent 优先使用阻塞式 `Client.submit`、`RemoteClient.submit` 或 CLI `submit`，等待作业完成或关键中断；只有需要持续跟踪进度或同时编排多个任务时才使用 `submit_async`/CLI `enqueue`。
- 静态 spec、资源总容量、父级预算与安全前置条件校验失败即拒绝，不创建作业；校验成功后进入 `QUEUED`，当前资源不足只更新排队原因。既有全局并发 2、预约、公平队列、独立执行器和清理合同保留。
- 排队继续受 `queue_timeout_seconds` 与原父级/service deadline 约束；等待连接或 `--timeout` 不改变 GPU 作业预算，不取消已接受任务，不自动重放变更请求。
- 服务端异步事件唤醒等待者，普通状态/进度变化只写日志；终态、UNKNOWN、等待超时、用户中断、服务停止/截止才结束等待。
- `JobWaitTimeout` 提供真实作业快照；`JobWaitInterrupted` 提供原因和已知身份。提交结果尚未知时 `job=null`，须先按同一 session/request 查询。CANCELLING 不代表已确认回收。
- 客户端与服务端须同步采用新发布版本。历史原件、2026-09-30 云端验收、旧发布及源码哈希不改写。

## 实现与验证进展

- 已更新调度器状态 revision/事件等待、SDK/SSH 阻塞协议、显式异步入口、等待断线释放、超时校验和可恢复中断。
- 已通过 29 项本地测试，使用模拟 GPU 遥测与真实 CPU 子进程；包含各类资源排队、静态校验拒绝、阻塞、幂等多等待者、CLI 中断、SSH RPC 本地桥接、取消、超时、崩溃和回收。
- 临时测试目录仍为 `notes/gpu-scheduler-v1/scratch/`，用途是 CPU 协议/故障注入；每次测试仅清理自己的临时目录及进程 token。必要测试输出与版本哈希归档在本目录 `evidence/`；本轮没有可再生中间模型。
- 资源原因矩阵、远程等待超时/服务中断断言、最终测试命令/起止/退出码、源码哈希和文档引用清单均已保存：详见 [验证记录](evidence/validation-20261001T032010Z.json)、[标准输出](evidence/validation-20261001T032010Z.stdout.log)、[标准错误](evidence/validation-20261001T032010Z.stderr.log) 和 [文档清单](evidence/documentation-20261001T032010Z.json)。

## 规范同步范围

`AGENTS.md`、`AutoResearch出题全流程指引.md`、`部署与长程接管.md`、`tools/README.md`、`tools/gpu_scheduler/README.md`、`tools/research_handoff/README.md`、`notes/长程Agent脚本与目录治理.md`、`notes/gpu-scheduler-v1/incident.md`、`notes/gpu-scheduler-v1/cloud-multiclient.md`、`notes/gpu-scheduler-v1/cloud-validation.md`、`autoresearch_三期/auto0803/research/运行说明.md` 和 `autoresearch_三期/auto0803/research/实验方案.md`。

历史清理报告、原始运行命令/回执、唯一故障证据、冻结发布副本以及非作业 spec 的研究声明不回填新用法。

## 后续部署验收

本地测试与 SSH RPC 本地桥接不等于真实云端新协议验收。后续须在新数据盘发布路径按已有授权部署同步版本，核验挂载和源文件哈希，测试真实阻塞提交、资源排队、关键中断、同请求恢复、并发上限及退出回收；不得重启或热改公共/活动服务。

## 最终收尾（2026-10-01）

- 本地验证命令退出码为 0，29 项测试全部通过，耗时 19.836 秒；验证范围是模拟 GPU 遥测、真实本地 CPU 子进程和本地 SSH RPC 桥接，不包含云端 GPU 训练。
- 13 份规范文档链接检查缺失数为 0，阻塞优先和异步例外已同步到实现涉及的使用文档；清单见 [文档验证记录](evidence/documentation-20261001T032010Z.json)。
- 已生成本地、未部署的白名单发布副本 [gpu-scheduler-v2-blocking-20261001T032010Z](releases/gpu-scheduler-v2-blocking-20261001T032010Z/)，包含 21 个文件；[MANIFEST.json](releases/gpu-scheduler-v2-blocking-20261001T032010Z/MANIFEST.json) 记录源副本与发布副本哈希一致。
- 收尾复核确认发布副本哈希/语法/JSON 均通过，证据索引目标齐全，`notes/gpu-scheduler-v1/scratch/` 为空，未发现本轮测试遗留进程。
- 当前未部署云端，未做新协议的真实 GPU 验收；2026-09-30 的旧版云端证据仍按历史版本保留，不能作为本次阻塞协议的云端证明。
