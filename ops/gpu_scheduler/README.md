# 公共 GPU 调度器运行资料

本目录维护跨题调度器部署输入和证据，不放在单题事故目录。唯一源码仍为 `tools/gpu_scheduler/`。本次仅准备新配置与不可变发布；没有替换或重启远端服务。

2026-10-06 已测试并上传 `v5-20261006-durable-r2`，详情见 [进展清单](进展清单.md)。云端位置为 `/mnt/data/autoresearch/ops/gpu_scheduler/releases/v5-20261006-durable-r2`；未安装、切换或启动新版，等待用户后续部署指令。

## 2026-10-06 只读核验

证据见 `evidence/preupgrade-20261006.json`。当前主机为 16 核，`MemTotal=32821860 KiB`（约 31.30 GiB）；拟定总 RAM 预约 30720 MiB，保留约 1.30 GiB 系统余量。GPU 为 RTX 5090，物理显存 32607 MiB，保留原 30000 MiB 声明额度。CPU 修正为真实 16 核。GPU 并发提升仍依赖测量画像和实际负载，不预先保证 5-6 并发。

当前 `autoresearch-gpu-scheduler.service` 使用旧不可变 v4 发布，MainPID 577422；session 为 `12c988292a304b358604e4c68f8a32d8`。查询时有 2 个运行、3 个排队请求，涉及 auto0802、auto0804、auto1066。尚无可无损切换的空闲窗口。

当前计划仍引用单题路径 `/mnt/data/autoresearch/work/auto0804/ops/incidents/20261006-ram30-resume/vdc-gpu-plan.json`，源码位于 `/mnt/data/autoresearch/work/auto0804/releases/gpu-scheduler-v4-data-mounts-20261006/`。旧路径属于历史运行证据，不删除、不覆盖。

## 新发布布局

源配置为 `config/production.json`，发布后 GPU 配置、plan 和 manifest 统一位于 `/mnt/data/autoresearch/ops/gpu_scheduler/releases/<release-id>/`。所有 `source_sha256` 必须绑定该发布的真实源码和配置；生成 plan 后执行官方 `lifecycle.py` 的加载校验。已有单题事故目录只保留历史引用。

调度器 root 保持 `/mnt/data/autoresearch/gpu-queue`，保证持久 request_id、journal 和原作业证据可对账。该 root 实际为 `/dev/vdc` bind mount，和系统盘设备不同；主 mount `/mnt/data`、额外 mount 仍需发布时逐项重新核验。生成器不得把凭据、Reference、Hidden 或专家资产加入 release。

## 切换前验收

1. 从官方服务实时查询所有 RUNNING、STARTING、CANCELLING、UNKNOWN 和 QUEUED，请求身份和原截止逐项记录。
2. 在隔离副本预检旧 session 历史导入，发现重复 request_id 或缺少原状态时先对账，不能覆盖历史或重新执行。
3. 旧 v4 的停止会取消排队请求并终止其作业，因此首轮升级必须等待所有相关题目完成或取得明确的定向迁移合同。仅准备新 release 不改变已运行研究版本。
4. 空闲窗口由官方 lifecycle/systemd 切换调度器；不重启 Docker/containerd、研究 Agent 或其他任务。保留旧 plan、unit drop-in、release 和 rollback 路径。
5. 核验新 service 的实际 plan/config/source SHA、16 核/30720 MiB 额度、cgroup CPU/RAM 强制合同、journal 及 request_id 原位查询，再使用受管测试验证。

未来恢复排队意图和对账执行完全由官方调度器处理，适配器调用 `Client.submit`/`Client.wait` 并透传 `gpu.state`，不再创建排队控制器或自行重提 UNKNOWN。

持续只读监护命令：

```bash
python3 tools/gpu_monitor/monitor.py watch --view agents --interval 60 --max-hours 12
```

监护不启动、停止或恢复作业；定向停止始终使用对应官方控制器。
