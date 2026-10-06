# 工作区通用工具

正式双 Agent 长跑按 [统一规范](../双Agent长跑与题目包验收规范.md) 强制使用以下三工具。禁止题目自建控制器、GPU 队列和守护/轮询脚本替代；题目差异放配置与薄适配器，通用缺口回到 tools 唯一源码修复、验证和发布。

- [research_handoff](research_handoff/README.md)：通用local/SSH轮次控制、进程级预算、上下文交接与guard；通过bundle生成，不含历史题目profile、模型网关或科研评分适配。
- [research_handoff/COMPLETION.md](research_handoff/COMPLETION.md)：统一评分完成合同、可信原子 receipt、截止等待规则和只读审计 CLI。
- [gpu_monitor](gpu_monitor/README.md)：跨题只读监控、官方长时间 Agent 聚合轮询、定向停止，以及活动登记表 `tasks.json` 的归档维护。全量轮询命令：`python3 tools/gpu_monitor/monitor.py watch --view agents --interval 60 --max-hours 12`。
- [gpu_scheduler](gpu_scheduler/README.md)：数据盘持久请求账本、截止准入、队首预约/回填、owner 公平调度与同卡共享；默认 `submit` 阻塞，官方 SDK 透传状态，控制器按 `WAITING_GPU` 记录轮内等待。已启动执行只对账不重跑；RAM/CPU 生产 cgroup 强制合同与 GPU 显存/CU 预约分别声明。
- [GPU 调度器正确使用指南](gpu_scheduler/USAGE.md)：新旧版本核对、冻结 request/deadline、阻塞调用与事件透传、基础设施分类、查询/取消、画像及接入验收；GPU 操作前必读。
- [GPU 调度器更新记录](gpu_scheduler/CHANGELOG.md)与[公共运维入口](../ops/gpu_scheduler/README.md)：本次重大变更、测试/上传范围、实际部署状态和授权边界。上传 release 不等于安装部署。

先核对对应题目进展清单中的授权、协议、资源和原截止。工具安装、组件生成或读取文档都不构成新的实验授权。当前任务写入活动登记表；已完成、过期或被替代条目及时归档，历史证据保留。

共享 GPU 最新要求见根 AGENTS.md：在不影响他人的前提下继续训练，不因其他PID出现自动停止。工具的当前准入能力、监控和硬截止分别声明，不能把共享观察冒充硬隔离；不热改历史发布。

`gpu_monitor` 只接受可回查的官方 `research_handoff` run 与 `gpu_scheduler` job/request 身份。停止、恢复、重试和预算控制仍由对应官方工具负责；`marker`、进程组和自定义兼容任务语义已删除，禁止在题目目录重新实现。

全程执行见 [统一长跑与题包验收规范](../双Agent长跑与题目包验收规范.md)、[脚本与目录治理](../notes/长程Agent脚本与目录治理.md)和[最终交付闭环](../notes/最终打包与交付闭环.md)。通用源码单一维护，任务差异参数化；临时排障放本题指定目录，不写入tools或冻结题包。
