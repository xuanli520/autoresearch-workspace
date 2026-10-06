# GPU 调度器更新记录

## 2026-10-06：持久请求、截止准入与轮内 GPU 等待

发布标识：`v5-20261006-durable-r2`。已上传到 `/mnt/data/autoresearch/ops/gpu_scheduler/releases/v5-20261006-durable-r2`，63 个文件的 SHA256、权限、白名单及 plan 已在云端校验。Manifest SHA256 为 `65d68b8eb2daa82ff2122b1ce9766d1cbe66b9c8d4a35f5b65422174e5666342`。

**状态是已测试、已上传、未安装/未切换/未启动新版。** 用户要求等待后续安装部署指令；旧 v4 和其研究任务未被本次发布替换。当前事实以 [公共 ops 进展清单](../../ops/gpu_scheduler/进展清单.md) 和真实回执为准。本文记录发布，不授予启动或部署权限。

| 更新 | 新合同 | 调用方必须调整 |
|---|---|---|
| 持久请求账本 | 数据盘追加/fsync/哈希链；QUEUED 原位恢复，已启动执行转 UNKNOWN 对账且不重跑 | 保存原 request/job/spec/deadline；评分绑定 origin_session_id，不按新 session 重建实验 |
| 截止准入 | latest_start=deadline-max_runtime；预测来不及立即 INFEASIBLE，队列持续复核 | 新请求去掉 queue_timeout；传原父级截止与完整执行上限，处理 INFEASIBLE/EXPIRED |
| 队首预约/回填 | 按公平调度选出的队首立即预约，后续回填不得推迟预测开跑 | 不低报运行上限或更换 owner；未知外部负载时不凭空保证启动 |
| 官方 SDK 等待 | submit 默认阻塞，on_update 透传最长 10 秒长轮询快照，断线只续读原作业 | 删除私有 ensure_job/轮询/重提循环，服务端与客户端一起验收 |
| 控制器 WAITING_GPU | 等待留在原轮，不计等待信用、不耗 Agent retry；墙钟截止不变 | 接入 gpu.state；基础设施终态单独处理，不伪装 agent_exit_nonzero |
| RAM/CPU 强制边界 | systemd slice/scope 与 cgroup v2 硬限，Docker main/sidecar 共用 job slice | 明确预约/high/max，使用可信 provider 注册并验目标环境权限与真实限制 |
| 资源画像 | 成功完整样本校准新预约，cold start 沿用保守声明，原硬限保留 | job_class 随工作负载版本变化；GPU/CU 不宣称硬隔离或实测 CU 峰值 |
| 公共发布布局 | plan/config/source manifest 放公共 ops，固定文件白名单、不可覆盖 | 不再依赖单题事故目录承载公共服务；旧证据和 release 保留 |

异常重启现在可以在原执行器仍运行时恢复服务：调度进程持有唯一服务锁，执行器有 job 锁和持久身份；账本预约和 UNKNOWN 隔离避免重复调度。正常停止仍主动取消队列和执行，不能用普通停止冒充无损迁移。完整行账本损坏拒绝启动；残缺尾行先保全再恢复。旧 session 导入有重复身份或缺状态时必须先对账。

本次目标配置为真实 16 核、30720 MiB RAM、max_running 8，GPU 保留 30000 MiB/100 CU。它是未部署的目标配置，不代表当前服务 CPU 额度已改为 16；并发上限也不保证真实达到 5-6 个作业。RAM 硬限包含 cache，memory.high 节流总内存；GPU 显存硬隔离仍未提供。

### 验证证据

- 冻结调度器 137 项、控制器相关 148 项及发布生成器 5 项通过；覆盖排队恢复、立即重启、只执行一次、迟到启动、丢失执行器、定向取消、deadline、WAITING_GPU、评分来源 session 与独立 bundle。
- 本地真实 user-systemd 64 MiB 限额下申请 120 MiB：退出 -9，oom_kill=1，memory.peak=67108864，清理成功。
- 云端只做上传、逐文件校验、plan/数据盘校验及旧服务只读复查；未执行新版 Docker/GPU 动态实验。旧服务 MainPID 577422、ExecStart 与 plan SHA 在上传后保持不变。
- 原候选 `v5-20261006-durable` 未上传；冻结验收发现 completion CLI 缺失及联合 release 进程模块身份不一致，已在 r2 修复并重新验证。

原始测试、上传和复查回执位于 `ops/gpu_scheduler/evidence/`，不进入公开题包。2026-09-30 的 GPU 烟测和 2026-10-01 的阻塞协议记录仅证明各自历史版本，不能充作此次 cgroup/Docker/GPU 验收。

### 迁移入口

日常正确使用见 [使用指南](USAGE.md)，字段见 [接口 README](README.md)，授权、旧服务/旧 plan、预检和后续动态验收见 [公共运维入口](../../ops/gpu_scheduler/README.md)。必须核对客户端、服务端、控制器 bundle 和题目适配器，不只替换客户端；已结束或活动中的旧 run/release 不回写新哈希。

文档更新只修改工作区源文档，不修改已经上传的不可变 release。安装前阅读本指南与源码核对后的当前文档；release 内历史 README 的旧锁/资源措辞不覆盖已测试源码和本记录。
