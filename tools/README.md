# 工作区通用工具

- [research_handoff](research_handoff/README.md)：通用local/SSH轮次控制、进程级预算、上下文交接与guard；通过bundle生成，不含历史题目profile、模型网关或科研评分适配。
- [gpu_monitor](gpu_monitor/README.md)：跨题只读监控、定向停止，以及活动登记表 `tasks.json` 的归档维护。
- [gpu_scheduler](gpu_scheduler/README.md)：单机内存队列，全局最多两个 GPU 作业、可配置同卡共享，本机或多客户端 SSH CLI/SDK 共用队列及独立执行器超时回收；默认 `submit` 阻塞到终态，只有需要跟踪进度或并行编排多个任务时才用 `submit_async`/`enqueue`；资源预约不等于硬隔离。

先核对对应题目进展清单中的授权、协议、资源和原截止。工具安装、组件生成或读取文档都不构成新的实验授权。当前任务写入活动登记表；已完成、过期或被替代条目及时归档，历史证据保留。

共享 GPU 最新要求见根 AGENTS.md：在不影响他人的前提下继续训练，不因其他PID出现自动停止。工具的当前准入能力、监控和硬截止分别声明，不能把共享观察冒充硬隔离；不热改历史发布。

全程执行见 [部署手册](../部署与长程接管.md)、[脚本与目录治理](../notes/长程Agent脚本与目录治理.md)和[最终交付闭环](../notes/最终打包与交付闭环.md)。通用源码单一维护，任务差异参数化；临时排障放本题指定目录，不写入tools或冻结题包。
