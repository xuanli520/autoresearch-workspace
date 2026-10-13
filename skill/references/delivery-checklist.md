# 交付清单、现状映射与冻结模板

清单是每道题的实例化输入，不能用通用目录示意替代本题实际合同。先在原材料外建立工作副本；原始运行、模型、数据、失败日志、旧发布全部只读保留。补齐只能引用真实原始记录，新增转化说明应记录源路径/哈希与转换动作。禁止重写历史run.log、冻结result/comparison或覆盖真实成品包。

## 目标路径与条件

以下路径相对题包根。`pre_run`为本阶段必达；`post_run`为长跑后才产生；可选/条件文件要写真实适用依据。算法模块名可按本题变化，不能机械强制grader.py/model.pt等示例名。

| 目标路径/对象 | 阶段与必选口径 | 内容验收 |
|---|---|---|
| `workspace/harbor_task/instruction.md` | pre_run必需 | 八章节完整语义、公开命令/提交物/约束/评分一致、无答案线索 |
| `workspace/harbor_task/task.toml` | pre_run必需 | 目标Harbor原生字段或真实适配器；显式separate及真实artifacts路径 |
| `workspace/harbor_task/environment/Dockerfile` | pre_run必需 | 选定profile/context；只封装公开内容；依赖/权限/入口齐备 |
| `workspace/harbor_task/environment/requirements.txt` | pre_run必需或平台认可依赖锁等价映射 | 真实固定依赖；不靠本机挂载/内部服务 |
| `workspace/harbor_task/environment/starter/` | pre_run必需 | 真实起点、只读公开版本与候选初始化映射；Scaffold另说明正式B |
| `workspace/harbor_task/environment/public_assets/` | pre_run按公开资产实际需要 | train/dev语义与manifest/指纹；没有训练环节不造空train |
| `workspace/harbor_task/environment/public_eval/`或等价公开入口 | pre_run必需接口 | 可公开迭代；不混最终私有Verifier；不用固定目录名否定等价实现 |
| 运行时 `/workspace/solution`或已声明等价候选路径 | pre_run必需合同 | 由公开Starter初始化、唯一允许提交面，artifacts移交包含所需模型/配置 |
| `workspace/harbor_task/solution/` | profile决定；可选源码Oracle | 不与运行时候选目录混淆；双context默认不作为Agent构建输入 |
| `workspace/harbor_task/tests/Dockerfile` | pre_run必需 | 独立Verifier，context=tests，本镜像依赖完整 |
| `workspace/harbor_task/tests/test.sh` | pre_run必需或目标平台等价入口 | 不依赖启动cwd，真实评分并可信原子写reward |
| `workspace/harbor_task/tests/grader.py`与约束/指标模块 | pre_run必需实际实现，名字可等价 | 不信自报分；约束执行、保护恢复、候选隔离、失败分类齐备 |
| `workspace/harbor_task/tests/hidden_assets/` | 本任务交付为空；等价方式须声明 | 空目录/无私有样本；私有生成/安全注入与评分调用证据在平台私有侧 |
| `workspace/reference/` | pre_run必需、仅私有 | 已有可执行Reference；入口/依赖/方法版本与正式证据一致 |
| `expert_evidence/专家作业说明文档.md` | pre_run骨架必需 | 真实任务/构建/评分/隔离/资源/限制；长程结论明确待生成 |
| `expert_evidence/expert_annotation.json` | pre_run必需 | 精简任务语义、方向/split/协议、真实B/R/U、隔离声明；无官方批准字段 |
| `expert_evidence/run_summary.json` | pre_run骨架必需 | 已知自验真实值；未运行时长/轮数/最佳复验为目标Schema空值，不伪造成功 |
| `expert_evidence/trajectory_codex.json` | post_run真实产物 | pre_run可不建文件或`{"rounds":[]}`，待生成原因放台账；不能有假轮次 |
| `expert_evidence/trajectory_seed.json` | post_run真实产物 | 同上；两流独立，不能用B/R seed记录顶替 |
| `expert_evidence/best_method/` | post_run真实产物 | pre_run可不存在/空目录/仅待生成说明；不得放Reference或专家候选代码 |
| `expert_evidence/`内既有自验材料 | pre_run必需证据索引 | 当前版本完整运行、NOP同Trial config/result/reward/非空日志、健康/隔离/资源/故障接入记录；不擅自新增动态运行 |
| `optimization_evidence/训练证据说明.md` | pre_run必需 | 方法/优化面/正式seed/协议/资源/gate/汇总/缺口说明 |
| `optimization_evidence/baseline_runs/seed_<真实seed>/result.json` | pre_run每正式seed必需 | 最小字段、原始指标、真实命令时间等及缺口null原因、产物索引 |
| 同seed `run.log` | pre_run每正式seed必需 | 原始stdout/stderr，warning/失败保留，非人工摘要替代 |
| 同seed `model/<实际模型格式>`、`artifact.json`、独立重载记录 | 训练型每seed必需；非训练不适用 | 模型真实、哈希与来源一致；重载记录非空且确有新进程加载/复评分；可追溯等价日志可映射 |
| `optimization_evidence/reference_runs/seed_<同一组真实seed>/` | pre_run全部正式seed必需 | 与Baseline同结构、一一配对，不抽样/改名/复制checkpoint |
| `optimization_evidence/comparison_summary.json` | pre_run必需 | 全seed、均值/std(n−1)/改善/显著性/gate/模型完整性/源路径；COMPLETE有依据 |
| 各目录 `README.md` | 可选推荐 | 可承载路径/状态/哈希规则，不重复私有证据到公开面 |

大模型只在平台认可的不可变资产入口有明确合同与可访问性证据时采用外部索引，记录版本、大小、SHA-256和重载来源；“模型太大”不是缺失豁免。`data_manifest.json`、`experiment_plan.json`、`surface_validation.json`、`surface_diff.patch`、`integrity_manifest.json`、`train_config.yaml`、`ablation/`、额外`runs/`层不属于通用最小必交，不为过检伪造。

## Docker profile 实例化

| 项目 | teaching-task-root-v1 | harbor-environment-v1 / 独立Verifier |
|---|---|---|
| Agent context | `workspace/harbor_task/` | `workspace/harbor_task/environment/` |
| Agent Dockerfile | `environment/Dockerfile` | `environment/Dockerfile` |
| Agent COPY源 | 相对任务根；需真实平台适配 | 相对environment；禁止COPY父目录 |
| 私有tests | 不得因task-root进入Agent层；必须安全拆分 | `tests/`独立context，不复制给Agent |
| Verifier context | 由实际已交付适配证明 | `workspace/harbor_task/tests/` |
| 候选路径 | 本题准确声明，通常`/workspace/solution` | 同；artifacts保持源绝对路径，不把host destination当容器重映射 |

唯一profile记录同时包括：目标Harbor版本/源码或文档基准、配置适配入口及哈希、两个context、COPY/WORKDIR、公开/正式入口、候选路径、artifacts、reward优先级。不能混用两套COPY规则；PCA `schema_version=1.3`不是通用固定值。

## 本题实例模板

交付物表：

| ID | 目标路径/对象 | 必选/条件/可选 | required_stage | 状态 | 真实来源/哈希/来源运行 | 适用依据 |
|---|---|---|---|---|---|---|
| `<layer-id>` | `<relative/path>` | `<必需>` | `<pre_run>` | `<证据不足>` | `<原始路径+hash+run>` | `<规范章节>` |

现状映射表（先输出，再开始整理）：

| 已有材料/哈希 | 目标路径 | 动作 | 转化规则/保留原件 | 来源运行与版本 | 验收检查 |
|---|---|---|---|---|---|
| `<source>` | `<target>` | 原样复制/格式对齐/补齐字段/引用原日志 | `<准确规则；不覆盖源>` | `<origin_run>` | `<A01..H07>` |

缺口表：

| 检查ID/缺失事实及位置 | 不满足原因 | 最小具体动作 | 重验材料 | 是否需授权 | 当前准入影响 |
|---|---|---|---|---|---|
| `<result.execution.command缺失>` | `<原元数据未保存>` | `<查原记录；不可恢复写null+原因>` | `<源日志/转化记录>` | 否/是+所需操作 | `<不足/不通过/不影响的已披露缺口>` |

每层定义：本题交付对象、验收口径、重点风险≤3条、上游结论/哈希。检查报告完成后更新台账；不通过回退对应层，不能越层宣布完成。控制材料放在题包外的本题已有进展/私有审计目录；不要为了套件台账增加未经白名单声明的第四个交付顶层目录。

冻结与后续接口表：

| 信息类 | 应引用的真实信息 | 尚未授权时的处理 |
|---|---|---|
| 授权 | 阶段、两模型、资源/费用上限、原绝对截止、停止/续轮/重试边界 | 标“启动未授权”，不伪造授权；不影响整理本身，阻止启动 |
| 版本 | task/release/protocol、公开包/Starter/Prompt/data/model/tool/adapter hash | 可读确定值写实；未确定为准入缺口 |
| 双流 | 两份配置、provider/model、档位、上下文、身份、独立目录 | planned run-id可声明计划；不得填成已启动run |
| 资源 | 设备/GPU UUID/session/root、CPU/RAM/线程/磁盘/评分并发 | 预约与硬限分别说明；目标环境验收缺失列不足 |
| 时间 | 每流目标、原墙钟截止、模型/GPU/单轮/清理上限 | 合同就绪；不伪造实际时长/轮数 |
| 接管 | 当前工具CLI/config、auth引用、查询/停止/恢复/exit/log/cleanup归属 | 凭据仅引用，不入公开包；不用自建控制器 |
| 证据 | 事件/轮源码/评分/计时/重载/健康/回收与哈希位置 | 明确未来产物路径与规则，实际产物NOT_RUN |

授权未到来不启动。后续长跑必须用`tools/research_handoff`、`tools/gpu_scheduler`、`tools/gpu_monitor`，沿用原预算/截止。交接注明只读聚合命令`python3 tools/gpu_monitor/monitor.py watch --interval 60 --max-hours 12`；本套件不执行持续监护或登记虚假活动。
