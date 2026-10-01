# 云 GPU 环境接管与迁移

适用于任何题目的云端环境迁移、重装和接管。本文不记录固定主机、题目、模型、PID、GPU UUID 或历史结果；这些信息必须写入对应题目的私有接管记录。

完整四并发、端点、超时、计时和停止合同见 [部署手册](../部署与长程接管.md)；缓存/临时目录与清理见 [目录治理](长程Agent脚本与目录治理.md)。本文保留环境接管速查，具体事故原件从题目进展清单查找。

共享 GPU 采用最新工作区口径：不因出现其他计算进程停止自己的训练，在不影响他人的前提下按余量、实测峰值、负载和调度约定继续共享；限制自身并发/资源，不干预他人。明确安全风险、用户停止和硬截止仍按本任务合同执行。

## 接管前

确认用户授权、任务ID、预算模式与对应截止/有效上限、停止条件、远端认证和共享资源规则。先用只读命令盘点；下列路径/变量由已核验配置提供，未赋值时不回退默认socket：

```bash
nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu --format=csv
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv
ps -eo user,pid,pgid,etime,args
free -h
df -h
findmnt -T "${TASK_ROOT:?先设置已核验任务根}"
docker --host "${TASK_DOCKER_HOST:?先设置任务专用socket}" info --format '{{.DockerRootDir}}'
```

“利用率为 0”不等于独占权。核对调度器/团队约定、现有进程归属、驱动与 CUDA 兼容性、CPU/RAM/磁盘余量，发现他人任务不得干扰。

## 安装与迁移

- 将环境、缓存、Docker 数据和实验输出放在明确的数据盘目录；不修改系统 Python 或公共依赖，除非授权且有回滚记录。
- 同时检查Docker的data-root/exec-root与日志、containerd的root/state/address/snapshotter、TMPDIR和各类下载/构建缓存。通过挂载设备和真实进程参数核验；公共服务仍写系统盘时另建数据盘专用运行时，不重启或迁移公共服务。
- 记录 OS、驱动、GPU、Python、CUDA、PyTorch、关键库、容器 digest、依赖锁和安装日志。版本能导入不等于题目镜像已通过 Harbor。
- 只传输运行所需的公开代码和公开资产；源端/目标端核验 SHA-256。Reference、Hidden、证据目录、密钥和整套工作区不得进入 Agent 可见镜像或挂载。
- 逐项复核 Docker context、COPY、WORKDIR、挂载、设备节点和入口。旧机器硬编码的 GPU UUID、驱动库、路径和 trusted 设置不能直接复用。

## 小规模验收

先做有限 SMOKE：设备可用、数据读取、无 NaN、显存/磁盘可控、保存成功、新进程重载一致、评分器写出可信 reward。记录命令、起止时间、退出码、日志和哈希；SMOKE 不等于正式 B/R 或平台验收。

## 运行与释放

每个任务使用独立目录、环境、缓存、日志和 task/job ID。绑定授权 GPU，限制 CPU 线程、显存、磁盘和预算；监控器只读观察，不自动启动、恢复、重试或追加训练。停止时先用 dry-run 核验身份和范围，再按任务合同发送停止请求，随后复查全部子进程、退出码、日志和 GPU 显存。

## 交接记录

至少保存：真实主机/任务标识、版本与哈希、资源盘点、安装日志、启动命令、预算与截止、状态/退出文件、日志位置、停止合同、未完成项和下一步验收标准。无法恢复的字段填 `null` 并解释，不凭旧快照补造。
