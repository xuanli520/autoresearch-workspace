# 双本地客户端、8 个 Agent 请求：实测记录

用户确认两个客户端使用同一云端 Linux 账号，双方均可查看和控制全部任务。已增加本地 SSH CLI 与 `RemoteClient`，所有请求进入云端同一个内存队列，全局 GPU 作业上限仍为 2。

## 实测结果

2026-09-30，在已授权 RTX 4090 主机上，通过**两个独立本地 Python 进程和独立 SSH 短连接**模拟两个客户端。没有假称使用两台物理电脑；8 份请求模拟 8 个长期 Agent 的 GPU 阶段，没有启动正式研究 Agent 或模型 API。

| 检查 | 结果 |
|---|---|
| 两客户端会话 | 同一 session `cc08a63575e247c7a966ccdfa993ab56` |
| 同时提交 | 每个客户端 4 个独立请求，共 8 个不同 owner/Agent |
| 队列可见性 | 双方均查询到相同的 8 个作业 |
| 8 个真实 CUDA 作业 | 全部完成、实际退出码均为 0 |
| 双向控制 | 额外各提交 1 个 CUDA 作业，双方成功取消对方作业 |
| 并发上限 | 原始 started/exit 区间复算峰值 2；NVIDIA 遥测峰值也为 2 |
| 清理 | 10 个作业全部 cleanup_ok=true、全部实际初始化 CUDA，stderr 均为空 |
| 客户端/服务退出 | 两个客户端实际退出码均为 0，服务独立监督器记录退出码 0 |
| 最终 GPU | 无计算进程，显存 1 MiB、利用率 0%，无本次残留进程 |

采样显存峰值 1135 MiB。本次是调度和协作控制验收，不是吞吐加速、长时稳定性或显存硬隔离证明。

## 实现与用法

以下调用规范更新于 2026-10-01，2026-09-30 的实测表及原始哈希保持不变；旧云端发布尚不支持新增的服务端阻塞协议，不得将这份历史验收当作新协议的云端验收。

- `RemoteClient` 复用现有 gpu_monitor 的受管 SSH/askpass，命令用 argv 引用，作业通过 stdin JSON 发送，不在 SSH 命令行拼接候选代码或秘密。
- 连接配置指定云端 Python、CLI 发布路径和共同队列根；`auth_file` 只指向各本地客户端自己的凭据文件。
- 云端只启动一次服务；两个客户端都用 `--remote` 调用 submit/status/list/get/wait/cancel/stop。客户端断线不会持有或取消 GPU 资源。
- `request_id` 在整个服务会话中唯一；同 ID 同 spec 幂等，不同 spec 拒绝；保存 `session_id` 可拒绝跨重启误重放。变更请求不自动重试。
- 权限是用户选择的可信共享模式；`cancel` 只取消指定作业，`stop` 会停止全部作业。队列自动按现有有界 FIFO 规则编排；没有增加数据库、抢占或手工优先级系统。
- 本地控制 Agent 默认调用阻塞式 `submit`，直到 GPU 作业进入终态；只有需要持续跟踪状态或同时编排多个任务时才调用 `submit_async`，再对返回的 `job_id` 使用 `wait`。阻塞等待由云端服务端事件唤醒，不再依赖客户端高频轮询。
- 静态 spec 校验失败立即拒绝且不入队；当前显存、计算份额、CPU/RAM 或可信遥测暂不可用时，校验通过的请求进入 `QUEUED`，继续受 `queue_timeout_seconds` 和父级截止时间约束。
- [使用说明与双客户端配置](../../tools/gpu_scheduler/README.md) · [连接配置模板](../../tools/gpu_scheduler/remote.example.json)

## 证据与版本

远端根：`/mnt/autoresearch/work/gpu-multi-20260930-025437`，与首轮测试目录完全分离。服务窗口 180 秒，单作业最多 25 秒，真实测试结束后服务已停止。

- [本地双客户端原始报告](evidence/cloud-multiclient-01/report.json)
- [客户端命令、起止及退出码](evidence/cloud-multiclient-01-exit.json)
- [逐作业与实际并发复算](evidence/cloud-multiclient-summary-01.json)
- [服务监督器实际退出记录](evidence/cloud-multiclient-remote-01/service-process-exit.json)
- [部署/源码哈希](evidence/cloud-multiclient-launch-01.json)
- [云端原始记录归档](evidence/cloud-multiclient-remote-01.tar.gz)
- [最终可再生缓存清理](evidence/cloud-multiclient-cleanup-01.json)

归档 SHA-256：`2572ad918d55522fef20ef977424e9aacead6feaacccf60bb2c23b671ad5cea1`，回收校验一致。只清理本轮缓存和 scratch，所有配置、源码、原始日志及回执保留。

本地调度模块 **15 项测试通过**，包括 SSH stdin 桥接、未知结果不重试、双客户端八请求与真实执行区间复算；[命令与结果](evidence/test-remote-01.json)、[完整输出](evidence/test-remote-01.log)。为了直接复用 SSH 工具，只对 gpu_monitor 增加包导入兼容，认证和控制行为未改；其 **45 项测试通过**。
