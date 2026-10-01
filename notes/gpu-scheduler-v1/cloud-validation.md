# GPU 调度第一版：云端实测与数据盘扩容

2026-09-30 已实际登录用户指定云 GPU 服务器，完成数据盘扩容与调度模块真实 CUDA 短时验收。调度核心无需修复，验证均通过。

## 数据盘

数据盘 `/dev/vdb` 已由云平台扩展为 **500 GiB**，但原 ext4 文件系统尚未扩展。核对挂载、文件系统、UUID 及 fstab 后，在线执行 `sudo -n resize2fs /dev/vdb`，真实退出码 0。

- 挂载点保持 `/mnt/autoresearch`，无需卸载。
- 扩展后 `df` 显示约 **492 GiB 总容量、351 GiB 可用**。
- 原 fstab 的 UUID 挂载已正确保留；未格式化磁盘或重启服务。
- [扩容前原始盘点](evidence/cloud-disk-before.json) · [扩容命令与结果](evidence/cloud-disk-resize.json)

## 真实 GPU 验证

设备：RTX 4090 24 GiB，UUID `GPU-dddea253-44d0-ac0a-77e1-8d22f8070439`。系统 Python 3.10 运行调度器，数据盘已有 Python 3.11 / PyTorch 2.6.0+cu124 执行合成张量矩阵乘法，没有下载或使用题目数据、模型。

测试时间：02:43:27–02:44:01 UTC，约 33.71 秒；服务硬窗口 180 秒，单作业硬上限不超过 40 秒。每份作业声明 2048 MiB、30 计算份额、1 CPU、2048 MiB RAM。

| 验证 | 实测结果 |
|---|---|
| 单个真实 CUDA 作业 | 正常完成，真实退出码 0 |
| 两份作业共享 GPU | NVIDIA 遥测同时观察到两个计算进程 |
| 第三个作业 | 保持 QUEUED，原因为 global_concurrency_limit；前项释放后运行 |
| 定向取消 | 只取消目标作业，同卡另一作业继续运行 |
| 执行超时 | 原 6 秒上限触发终止，子进程及 GPU 资源回收 |
| 调度器 SIGKILL | 执行器继续按原截止回收；旧执行器存活期间拒绝新服务启动 |
| 正常停止 | GPU 作业被取消，清理成功 |

共 7 个作业、142 次遥测；最大同时 GPU 计算进程数 **2**，显存采样峰值 **1135 MiB**。全部作业 `cleanup_ok=true`，stderr 均为空；退出码 -15/-9 对应本次主动取消/超时注入。最终确认本次进程全部退出、GPU 计算进程为空、显存 **1 MiB**、利用率 **0%**。

## 证据与清理

远端独立目录：`/mnt/autoresearch/work/gpu-sched-20260930-024324`。已部署文件逐一核对 SHA-256，所有新增源码、运行记录与缓存均在真实数据盘。公共 Docker/containerd 未用于本次测试，也未修改。

- [部署与版本回执](evidence/cloud-launch-01.json)
- [原始验收报告](evidence/cloud-run-01/evidence/report.json)
- [逐作业退出与遥测汇总](evidence/cloud-summary-01.json)
- [完整证据归档](evidence/cloud-run-01.tar.gz)
- [收尾检查与清理清单](evidence/cloud-cleanup-01.json)

归档 SHA-256：`77a5ac9cce9b2d1e0ec10464166aee567733c0dcbd8cd2fd0f728e0bc107573c`。本地回收后校验一致，远端唯一原件没有删除；仅清理可再生缓存和作业 scratch。

脱离 SSH 的 harness 启动器没有保存操作系统 wait 退出码，该项为 `null`；7 份作业回执均保留实际退出码，原始 case 结果为通过。报告中的 `jobs` 是初始提交回执；终态应读取逐作业 `exit.json`。

本次证明真实设备上的基本调度、共享、排队和故障回收链路可运行。没有开展正式 Agent 研究、长训练、容器/Harbor 验收或吞吐基准，不能据此承诺效率提升、长期稳定性或 GPU 显存硬隔离。

## 后续协议更新（2026-10-01）

本历史报告记录的是 2026-09-30 原始发布版本的真实云端短测，不能用来证明后续阻塞提交协议已经在云端重新验收。当前工作区源码新增服务端事件等待：本地控制 Agent 默认使用阻塞式 `submit`，资源暂不可用时校验通过的请求进入 `QUEUED`；需要持续跟踪或并行编排时使用 `submit_async`。后续真实部署和验收必须使用新发布副本并重新记录源码哈希、命令、状态与退出证据。
