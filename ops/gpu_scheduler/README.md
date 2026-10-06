# 公共 GPU 调度器运行资料

本目录维护跨题调度器部署输入和证据，不放在单题事故目录。唯一源码仍为 `tools/gpu_scheduler/`。本次仅准备新配置与不可变发布；没有替换或重启远端服务。

2026-10-06 已测试并上传 `v5-20261006-durable-r2`，详情见 [进展清单](进展清单.md)。云端位置为 `/mnt/data/autoresearch/ops/gpu_scheduler/releases/v5-20261006-durable-r2`；未安装、切换或启动新版，等待用户后续部署指令。

提交/接管必读 [正确使用指南](../../tools/gpu_scheduler/USAGE.md)，接口以 [README](../../tools/gpu_scheduler/README.md) 为准，重大更新和证据边界见 [更新记录](../../tools/gpu_scheduler/CHANGELOG.md)。本轮仅同步工作区文档，不上传、覆盖或修改已发布 release；其内历史 README 以源码核对后的本指南补充纠正，原 manifest 保留。

## 2026-10-06 只读核验

证据见 `evidence/preupgrade-20261006.json`。当前主机为 16 核，`MemTotal=32821860 KiB`（约 31.30 GiB）；拟定总 RAM 预约 30720 MiB，保留约 1.30 GiB 系统余量。GPU 为 RTX 5090，物理显存 32607 MiB，保留原 30000 MiB 声明额度。CPU 修正为真实 16 核。GPU 并发提升仍依赖测量画像和实际负载，不预先保证 5-6 并发。

该次上传前核验中，`autoresearch-gpu-scheduler.service` 使用旧不可变 v4 发布，MainPID 577422；session 为 `12c988292a304b358604e4c68f8a32d8`，当时有 2 个运行、3 个排队请求。以上是历史快照，作业会继续变化；安装前必须重新查询，不能以该计数判断空闲窗口。

当前计划仍引用单题路径 `/mnt/data/autoresearch/work/auto0804/ops/incidents/20261006-ram30-resume/vdc-gpu-plan.json`，源码位于 `/mnt/data/autoresearch/work/auto0804/releases/gpu-scheduler-v4-data-mounts-20261006/`。旧路径属于历史运行证据，不删除、不覆盖。

## 新发布布局

源配置为 `config/production.json`，发布后 GPU 配置、plan 和 manifest 统一位于 `/mnt/data/autoresearch/ops/gpu_scheduler/releases/<release-id>/`。所有 `source_sha256` 必须绑定该发布的真实源码和配置；生成 plan 后执行官方 `lifecycle.py` 的加载校验。已有单题事故目录只保留历史引用。

调度器 root 保持 `/mnt/data/autoresearch/gpu-queue`，保证持久 request_id、journal 和原作业证据可对账。该 root 实际为 `/dev/vdc` bind mount，和系统盘设备不同；主 mount `/mnt/data`、额外 mount 仍需发布时逐项重新核验。生成器不得把凭据、Reference、Hidden 或专家资产加入 release。

## 生成、校验、上传与安装分别执行

以下是后续新 release 的受管接口示例，尖括号必须替换为已核验路径/ID，authorization 填实际已有授权；不是本轮执行指令。现有 r2 只使用 `--check` 核验，不覆盖、不重新生成，也不通过新名字绕过安装授权。

```text
python3 -B -m tools.gpu_scheduler.release --output <新本地release目录> --config ops/gpu_scheduler/config/production.json --remote-directory <数据盘公共release绝对目录> --authorization <实际授权原文>
python3 -B -m tools.gpu_scheduler.release --check <同一本地release目录>
python3 -B -m tools.gpu_scheduler.release --publish <同一本地release目录> --auth auth.txt --receipt <新本地上传回执路径>
```

`--output` 验 Schema、生成固定白名单和 plan/source SHA，已存在输出拒绝覆盖；`--check` 核全部成员/权限/哈希；`--publish` 只上传冻结成员并在目标数据盘验证同一 manifest 和 `load_plan`，不安装、启动或切换。配置只填真实硬件/挂载合同，不带凭据；凭据从本地 gpu_monitor 受管认证复用。

联合 release 包含 scheduler、共享 process_control、research_handoff bundle、completion CLI、config/plan/manifest 和当次 README，不包含 gpu_monitor、题目适配器、Reference/Hidden/模型/数据/证据或新运行授权。本地 SSH 客户端仍需要官方 gpu_monitor 认证组件；题目接入配置和 controller/adapter 必须另行冻结验收。新增使用文档与变更记录不回写已发布 bundle。

安装命令 `lifecycle.py install-gpu-plan` 是创建未存在唯一单元的入口，会拒绝覆盖已存在单元和安装回执，不能当旧服务升级脚本直接重跑。已有 v4 的切换必须先检查 unit/drop-in 与真实 plan，再按后续授权制定受管迁移；本轮没有安装或切换操作。CLI `stop` 会取消全部作业，在 systemd `Restart=always` 下服务仍可能重启；`systemctl stop` 是显式停止单元，但同样不是无损迁移。

## 切换前验收

1. 从官方服务实时查询所有 RUNNING、STARTING、CANCELLING、UNKNOWN 和 QUEUED，请求身份和原截止逐项记录。
2. 在隔离副本预检旧 session 历史导入，发现重复 request_id 或缺少原状态时先对账，不能覆盖历史或重新执行。
3. 旧 v4 的停止会取消排队请求并终止其作业，因此首轮升级必须等待所有相关题目完成或取得明确的定向迁移合同。仅准备新 release 不改变已运行研究版本。
4. 空闲窗口由官方 lifecycle/systemd 切换调度器；不重启 Docker/containerd、研究 Agent 或其他任务。保留旧 plan、unit drop-in、release 和 rollback 路径。
5. 核验新 service 的实际 plan/config/source SHA、16 核/30720 MiB 额度、cgroup CPU/RAM 强制合同、journal 及 request_id 原位查询，再使用受管测试验证。

还须在目标环境核 system manager transient unit 权限、Docker 主容器/sidecar 同 slice 限额和定向清理、UNKNOWN 对账/跨 session 完成评分、官方 on_update 到 WAITING_GPU 的完整调用链。现有本地回归与上传校验不能代替这些动态验收，不能预先承诺 5-6 并发。任何失败保留原件，不编辑 journal、旧 spec、终态或 request ID 绕过检查。

未来恢复排队意图和对账执行完全由官方调度器处理，适配器调用 `Client.submit`/`Client.wait` 并透传 `gpu.state`，不再创建排队控制器或自行重提 UNKNOWN。

持续只读监护命令：

```bash
python3 tools/gpu_monitor/monitor.py watch --view agents --interval 60 --max-hours 12
```

监护不启动、停止或恢复作业；定向停止始终使用对应官方控制器。
