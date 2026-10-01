# Harbor Harness 兼容性检查

本规范落实用户新增的 Harness 要求，作为 QA17 的六个子项，同时关联 QA06、QA18、QA21。默认仍是静态实现检查，不执行待检代码，不把“有文件”当作“能运行”。

## 依据与版本

- 内部：[Harbor Framework 调研分析](https://bytedance.larkoffice.com/wiki/A7FUwJBALici6Gku3rWc4EsinLf)，读取修订 3790。重点采用 Task/Job/Trial 分层、配置覆盖、测试/结果约定；文档中的研究建议、未来平台目标和示例不是全部强制交付项。
- 官方：2026-09-18重新核对 [Environment](https://docs.harborframework.com/core-concepts/tasks/environment)、[Verifier](https://docs.harborframework.com/core-concepts/tasks/verifier) 与 [Configuration](https://docs.harborframework.com/core-concepts/tasks/configuration)。旧Task Structure链接现已重定向至文档主页。
- 配置与结果的精确类型以目标版本的官方 `src/harbor/models/task/config.py`、`models/trial/config.py`、`models/job/config.py`、`models/trial/result.py`、`models/verifier/result.py` 及目标 provider 实现为准，可从 [官方仓库](https://github.com/harbor-framework/harbor) 读取。不要假定 main 永远等于已部署版本。

先查包内版本锁定、运行配置或平台说明。未声明版本不直接判失败，可以明确采用 `official-docs@2026-09-18` 作为静态对照版本，并在 version_basis 写规范 URL 和“文档静态核对，未做原生 schema 加载”；只有实际读过源码才注明源码。provider 可从明确 Docker 配置等材料推定，注明推定依据；不能确认则 unknown 和未完成。不能把文档快照说成已安装版本。

不要求最新 schema_version 必填；按目标版本默认值、别名与实际类型判断。内部旧示例的 memory/storage、当前 memory_mb/storage_mb 等是否可用须核对版本，不能见旧字段就判失败。原生不识别的自定义元数据本身不一定报错，但不能把 [resources] 或 [entrypoint] 当成原生生效的资源限制或入口配置，除非有实际适配映射。

## 教学示例与严格路径

先读 [Docker路径契约](docker-path-contract.md)。2026-09-18实际example.zip要求task-root构建上下文与/workspace容器根；这不等于已确认原生Harbor接受其自定义task.toml。采用teaching-task-root-v1或harbor-environment-v1时记录来源，不能混用。harbor.path_contract的确定路径失败自动使H03失败；动态语法未解析须补语义复核。示例tests/test.sh仅pytest，正式验收入口还必须实际评分并写reward，不能将合同测试成功当任务评分。

## 六项复核

| ID | 阅读与判断 |
|---|---|
| H01 任务目录 | 选定真正 task 根目录，确认 instruction.md、task.toml、环境实现、测试入口及所引用本地代码齐全。普通 Linux 单步任务使用 tests/test.sh；solution/solve.sh 是可选 Oracle 入口，不因缺它单独判 Harbor 不兼容。说明多层外包装、分卷依赖，不把半包当独立完整任务。多步/Windows 以对应版本规范检查，不生硬套单步模板。 |
| H02 版本与配置 | 读完整 TOML，核对语法、字段类型、原生配置位置与目标版本；[task] 等可选块的内部必填字段按该版本处理。检查配置真正指向已交付文件/有效适配，不把任意 TOML 可解析误写为原生 schema 验证成功。脚本有 tomllib 时自动检查语法，无解析器须使用可信 TOML 解析器或保留未完成；不能执行包内提供的解析器。 |
| H03 环境与运行路径 | 确认目标 provider 能使用交付的 Dockerfile、Compose、预构建镜像或有证据的其他适配方式。读构建上下文、COPY、WORKDIR、启动命令及所引用文件，核对运行时路径与服务依赖。Docker 原生构建上下文通常是 environment/；教学example使用task-root是明确的另一profile，须提供适配依据。不能默认能COPY外层任务包。tests 被投放到 /tests、Oracle 到 /solution；测试启动 cwd 不保证是 /tests。缺 Dockerfile 但合法使用镜像不失败；依赖不预构建或使用挂载本身不失败。这里只查接入可行性，不查 CPU/GPU 上限或 12h 压测；提交前不要求专家提供连续 12h 容器运行证据。 |
| H04 测试入口与 reward | 沿 tests/test.sh（或目标版本等价入口）读取实际调用链，确认无未由 Harness 提供的必填参数，路径可解析，评估结束能写 Harness 读取的 reward。普通 Linux 接口为 /logs/verifier/reward.txt 单一有限数值，或 reward.json 非空数值对象；不能用自定义 result.json、stdout score、字符串 status 代替。两者并存时检查优先的 reward.json，避免旧文件遮蔽新分数。接受合法负数或 >1；业务归一化范围另由 QA02/03 判断。QA06 还要求可识别的标量主指标。仅注释/死代码中的写入不算实现。 |
| H05 Job/Trial 调用配置 | 有 job.yaml/trial.yaml 或实际运行 config.json 时检查所选 task、provider、agent、有效配置覆盖、必要启动条件和 verifier 未被禁用。检查 YAML 重复键、错误路径、空任务选择、资源/网络覆盖与任务声明明显冲突；不是要求所有默认字段显式填写。支持单任务 CLI，不强制提交 job.yaml。未提供调用配置且不声称已有运行时可不适用；自定义适配须有入口/字段映射证据。 |
| H06 Harness 运行证据 | 有对应运行材料则核对同一 Trial 的 config.json、result.json、verifier/reward.txt 或 reward.json、trial.log 或 verifier/test-stdout.txt。读任务标识/校验信息、运行参数、时间、异常和最终 rewards，确认对应本次交付与有效 verifier，而非旧任务或只有训练日志。无材料则不适用并明确“未验证运行”；有材料不完整/版本不适配则未完成，不强行套格式判失败。有明确任务接入错误则不通过；纯基础设施/凭证障碍记未完成并说明，不能误判任务实现必然错误。 |

H01–H04 必查，H05/H06 仅符合上述条件可不适用。任何适用项失败使 QA17 不通过；其余情况下存在待确认项使 QA17 未完成；全部适用项通过才通过。尚未复跑不妨碍“静态接口通过”，但不能写“完整 Harness 验证通过”。

H06 当前自动一致性校验支持单步 Trial 格式：结束时间非空、无 exception_info、verifier_result.rewards 与优先 reward 文件数值一致，配置对象/日志非空，且证据同目录。还必须人工核对任务身份、配置覆盖和日志上下文；自动校验不鉴定日志真伪。多步 step_results 或其他版本若未被当前校验器支持，标为未完成并说明需版本适配，不伪造单步证据，不仅凭奖励为 0 判接口失败。

运行目录中 agent/trajectory.json 若为 ATIF，按 ATIF 版本识别；不能要求 AutoResearch 专家填写的 JSON/JSONL 自动变成 ATIF，也不能用 ATIF 替代缺失的专家注解。声明 artifacts 收集路径不等于已成功收集文件，已有记录应检查实际产物。根目录轨迹可能被用作 Agent 前置上下文，不能未经说明把私有参考轨迹写入该位置；仅在本次阅读发现明确证据时记录风险，不扩大 QA07/08 的检查范围。

## 运行验证与报告

提交前不要求专家提供连续 12 小时容器运行证据或 12h soak；专家侧只需保留实际运行期间的健康观察。连续 12 小时稳定性由平台质检/最终验收按合同后验。

报告保留 21 行，不增加六行到主表。QA17 汇总结论，表前一行区分格式/接口与运行状态；六项证据写入 report.json.harbor。已有材料一致只称“已有运行证据一致，非独立复跑”。

如用户进一步要求实际跑通：先确认目标 Harbor 版本、provider、隔离环境和必要模型/凭证及费用范围，再依该版本 CLI help 制定执行命令。只在用户授权的可丢弃隔离环境执行；不能在用户宿主机直接运行未知 test.sh/solve.sh、构建上下文脚本或导入包内 Python 模块。按目标版本选择单任务或 Job/Trial 入口，保留任务版本/校验、命令、配置、日志、reward 和 Trial 结果，再走 H06 复核。本流程未运行时不得声称完成该阶段。
