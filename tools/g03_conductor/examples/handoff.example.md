# G03 首轮初始化交接

这是私有模板。复制到 `<私有区>/ops/g03_handoff/handoff.md`，填写实际值并删除本段；不要提交到 Starter、公开镜像或 Git。本文仅记录初始化快照，运行后的阶段、预算和证据以 conductor 账本为准。

## 题目与任务

- task_id：<与 init 一致>
- 题目进展清单：<绝对路径>
- 问题定义：<固定输入、允许修改的方法接口、共同协议和目标指标>
- Baseline 来源及适配：<来源、固定版本、实现差异；不能写已通过 G02>
- train/score 接口说明：<真实入口、checkpoint 选择规则、评分 JSON 字段>
- 必须阅读的任务资料：<少量直接相关文件路径>
- 尚需主会话完成：S1 Baseline 充分性诊断、S1_5 可行性预检、协议冻结、筛选及正式配对；不把 SMOKE 计为科学证据。

## 授权与停止条件

- 有效授权原文或来源：<引用本题进展清单，不粘贴秘密>
- 允许调用的模型和 GPU/CPU 实验范围：<真实授权>
- 原 gpu_hours：<init 值>
- 原 deadline_at：<带时区 ISO8601，与 init 一致>
- init 时间：<真实时间>
- 预留正式双 Agent/最终复验的预算：<适用时引用；本次不启动>
- 本阶段停止条件：DONE、STOP_UNMET、原预算/截止；paused、blocked、wait_expert 需专家处理。
- 本次没有启动研究模型、reviewer 或 G03 实验。准备期检查是 SMOKE，证据见下方。

## 路径、版本与哈希

- 执行主机标识：<非秘密标识；用户在哪台机执行命令>
- private_root：<realpath>
- starter：<独立 realpath，不能与 private 互相包含>
- baseline：<realpath、tree_hash>
- scorer：<全部可信实现/依赖路径及 tree_hash>
- data：<逐项名称、路径及 tree_hash；与协议一致>
- 工具发布根、版本、源码哈希/未提交差异：<实际值>
- G03_PYTHON：<能加载 conductor 的解释器>
- 训练/评分 Python：<init --python，依赖已核查>
- pi 路径和版本：<实际值>
- subagent 扩展路径与哈希：<实际值>
- 主模型：anthropic/claude-opus-5-5，thinking high（须已验证可解析）
- worker：<角色文件路径、哈希、实际模型>
- reviewer：<init 记录的角色文件路径、哈希、实际模型>
- executor：<local_cpu 或 gpu_scheduler>
- scheduler_root / 服务客户端发布证据：<GPU 时本机真实根与版本；CPU 写不适用>
- 数据盘/临时/缓存/会话路径及设备证据：<真实路径和检查记录>
- NVIDIA 容器 main/verifier CDI、CUDA/reload 和归属回执：<适用时引用；没有不能写通过>

## 初始化边界快照

- 准备完成时间：<带时区>
- status.stage：S0
- status.draft_id：<propose 成功值>
- status.major：<真实值>
- paused/blocked/terminal：null / null / null
- init、propose、status、verify 的退出码及非秘密 JSON 输出：<preflight 路径>
- 准备期 ledger 最后序号及 SHA-256：<快照，正常推进后会改变>
- 原 protocol.draft.yaml SHA-256：<真实值>
- conductor g03/protocol.yaml SHA-256 与 draft 的语义核对：<真实值>
- verify：ok=true，problems=[]
- 初始账本无 run_start/run/review/nudge；本题无活动进程、未完成或未知 GPU request：<核查证据>
- 公私隔离、Git 历史、构建/mount、可信 scorer 核查：<证据或明确缺口>

## 预检与限制

逐项记录：检查内容、PASS/未执行/阻塞、时间、命令退出码和证据路径。模型真实联通与 CLI 可解析分别记录；无调用授权时不发付费探针。目录存在、RPC 加载或假 reviewer 的 PASS 不代替训练、隔离或三模型真实运行。

<实际预检记录；所有启动必要项须通过，未完成不能宣称 READY>

## 用户一行启动与异常处理

- 唯一启动命令文件：<ops/g03_handoff/launch.txt 绝对路径；与交付用户的那行完全一致>
- 主 pi 持续提示：<ops/g03_handoff/main.md 绝对路径>
- 首轮动作：读取本文件、g03_status(detail=true)、只读核对资产与授权；一致则结束首轮，扩展调用 next 进入 S1。
- 首轮差异：调用官方 CLI control --action pause，说明差异，不能只口头停下让扩展继续。
- 后续：主会话派 worker 实现，主会话 g03_run 统一实验；review-run/正式运行由扩展自动执行。
- pause/stop 在检查点生效；GPU 取消使用原 scheduler request/job，禁止停公共服务。
- σ_B=0 等专家签字；DONE/STOP_UNMET 后不自行追加预算、打包或启动正式双 Agent。

交接规范：<工具发布根>/notes/g03_conductor交接规范.md
