# 动态缺口只交核验单，本套件不执行

先复用当前未变版本的真实记录。缺记录时，审核仍为证据不足/不通过；“需授权执行”不是合理待生成。任何 B/R/训练补跑超出本套件，须退回原科学流程；不得把完整训练包装成极小核验。

每份核验单必须包含：用途/对应条款、明确授权缺口、准确 argv 数组、运行主机/已核验数据盘、目标 Harbor/provider/镜像digest、任务与公开快照hash、预计最长时间与资源、输出路径、查询/定向停止/回收命令、通过/失败标准。不得带秘密值或以 shell 拼接插入路径。授权执行由后续独立流程承担，核验单不能自动运行。

## 命令如何成为准确、可审核的命令

1. 读取本题真实CLI入口、源码、依赖版本和已有成功 config/command（不能直接执行题包脚本）。只读目标已安装工具的 `--help` 可作为后续方案准备材料，但本次验证不接远端或启动服务。
2. 为指定版本重建 argv、provider配置和合法路径。Harbor不同版本的 `nop`/environment/provider参数不能通用猜测；从 `harbor run --help` 与该版本真实 provider 文档确认，附 help/源码文件定位与hash。无此材料则核验单标 COMMAND_UNRESOLVED，不能交一条带占位符命令请求最终批准。
3. 输出的最终核验单不得留 `$VARIABLE`、`<待填>`、未解析运行路径或虚构工具字段。命令可直接复制执行但默认只呈示。未知密钥用受控 secret 引用由可信侧注入，公开报告不包含值。

## 干净构建/完整链路与 NOP

读取唯一 profile 的真实 build context、双 Dockerfile、受管 provider入口。计划必须使用数据盘专用运行时与新输出目录；不能依赖内部挂载、旧容器或未声明缓存。P5核验计划依次是构建→Starter→完整Public/Dev→可信reward→正常结束/清理/产物回收；如果 Starter 评分会进行训练，本 skill 不执行，标明超出极小核验范围并交给另行授权阶段。

NOP必须用已确认版本的真实 `nop` Agent、separate独立verifier与当前题包，保存同Trial config/result/reward/非空日志、版本绑定、镜像digest和回收状态。构建成功、Trial结束且无异常、独立verifier执行、有限reward与result逐键逐值一致即满足NOP；0分不自动失败。它不证明有效候选的完整评分或模型研究工具链。

GPU main及verifier均只用 `tools.research_handoff.providers.harbor_docker:ManagedDockerEnvironment(gpu_attachment="cdi")`。计划取证 OCI/inspect中的 `nvidia.com/gpu=<scheduler UUID>`、无legacy DeviceRequests、`NVIDIA_VISIBLE_DEVICES=void`、唯一GPU，以及资源同值更新后全新CUDA初始化和适用reload回执。不支持CDI则阻塞，不回退。GPU核验也经官方scheduler，不允许直接 `docker --gpus`。

## solver身份负向探针

在实际受限 solver 容器/身份内，通过可信provider探针入口逐项测试列出的禁读路径及访问渠道；不用宿主管理员执行后声称solver隔离。精确 argv 可采用目标已声明probe入口，或可信容器内 `test -r <实际禁读路径>` 并要求退出码1（不存在或不可读）；目录、symlink和API分别核验，不能只测一个文件。禁读清单包含Reference、Hidden、专家/优化证据、另流、秘密引用、宿主路径/socket；不给solver秘密内容。保存身份/UID、mount、env键名白名单、各路径预期和实得退出码、镜像层/context与Git扫描回执；标准错误不得打印秘密。

## P6与停止/故障准备

两个真实provider/model映射分别验证响应完整结束、工具调用、候选编辑和评分接口，不能仅HTTP 200。可复用无变化的工具目标环境验收；新增适配需后续授权小探针。定向STOP/超时/失联/上下文移交/外部容器清理记录真实run/job/回收范围。计划引用官方三工具真实命令，不自建守护/轮询/ensure_job重提，不执行正式研究。

后续长跑接管的只读统一观测命令是 `python3 -B tools/gpu_monitor/monitor.py watch --interval 60 --max-hours 12`；只有登记后的run/job才可见。停止仍由controller/scheduler执行，观察命令不授权或代替启动/停止。命令字段与实际工具Schema核对；durable父级原deadline、完整max_runtime、on_update/gpu.state及UNKNOWN不重提等按统一规范，不从此文推导新接口。
