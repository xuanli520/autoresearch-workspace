# 验证记录

本目录只保存本套件自身的验证与只读演练记录，不是任何题目的正式证据，也不进入 solver 公开快照。原题包未被写入、删除、重打包或运行。

## 已执行

- `scripts/test_pre_run.py`：24 个合成夹具测试通过。覆盖待生成白名单、真实日志/模型/重载缺失、`NOT_RUN` 伪造时长（包括 0）和嵌套成功值、严格 JSON、路径逃逸、权限摘要、公开 `tests/hidden_assets`、非训练模型不适用、post_run 目标绑定、artifact声明哈希/大小、历史轨迹与待生成台账矛盾及台账一致性。
- `scripts/test_review_contract.py`：16 个合同/反例测试通过。覆盖 skill/source/hash 回执、非空命令与证据定位、全 ID 覆盖、上游递归、模式一致性、循环/槽位复用、deterministic contradiction 不能被手写报告遮蔽。
- `scripts/review_contract.py routes`：8 层加终审共9个硬路由存在且哈希可查；主skill另外通过frontmatter验证，共10个加载入口。
- `skill-creator/scripts/quick_validate.py`：10 个 SKILL.md frontmatter 和未完成占位检查通过。使用隔离的 `uv` 临时环境提供 PyYAML；未改动工作区依赖。
- `scripts/check_package.py` 两个真实成品包只读 dry-run：只报告事实观察；不将非训练题的模型缺失、私有 verifier 的同名 Hidden、依赖命名空间 `reference` 或历史真实轨迹判为伪造。两个包均没有本套件初始化台账，作为本次整理缺口记录；不改写其历史交付结论。
- `forward-test.md`：独立读取主/层/终审 skill 的边界演练，证明正式 E05 缺失不能由 post_run 延期，缺上游不能越层通过，无法解析动态命令不能放行。
- `validation.json`：命令、退出码、AST/链接和规范源 drift 快照。规范源与官方 QA 文件哈希未发生漂移。

## 解释边界

测试夹具只证明本套件在合成输入上的可观察行为；不证明真实日志诚实、模型重载语义、镜像物理隔离、CDI 设备或 Harbor 平台运行。真实题包的 layer/终审报告必须由审核者逐项读取内容和证据后生成，不能由脚本输出直接拼成通过。

两个参考包中，现行 CDI 合同和当前版本运行接入证据仍需按 H/C 层分别判定；历史 Harbor/NOP 证据只覆盖其绑定版本/Trial。成品已有真实双轨迹时保留历史事实，不把它们强制改写为本套件“未运行”。

## 两包只读演练最终结果

两包均实际读取10个skill及对应条款，完成49项A–H检查和P1–P7终审；每包带台账checker、8份层报告合同和终审合同共10命令，退出码全部0。`contract_valid=true`且`layers_allow_admission=false`：套件演练执行成功，真实题包当前准入有未完成项。

| 层／终审 | auto0861 | MoE r8 |
|---|---|---|
| A | 证据不足 | 证据不足 |
| B | 证据不足 | 证据不足 |
| C | 不通过 | 不通过 |
| D | 证据不足（局部通过） | 证据不足 |
| E | 证据不足（局部通过） | 证据不足（局部通过） |
| F | 证据不足（局部通过） | 证据不足（局部通过） |
| G | 证据不足 | 证据不足（局部通过） |
| H／终审 | 不通过 | 不通过 |

“局部通过”表示该层已有内容无相关反证，层结论仍因上游缺口保守阻塞，不能越层放行。逐项原始事实、哈希和返修材料见 [auto0861](auto0861/dry-run-summary.md) 与 [MoE r8](moe-r8/dry-run-summary.md)。

真实问题与不足：两包均保存legacy GPU注入反证，不符合现行官方CDI合同；八章题面仍有anytime/最近有效提交和真实运行来源语义不足；部分当前版本冷构建、数据盘全链路、Starter完整Public/Dev、P6故障准备及七类冻结配置未完整挂接。MoE r8另有内部默认基础镜像冷构建入口、RTX4090科学时延与RTX5090质量接入范围、错误分类和显存执行依据的未完成项。auto0861部分镜像历史层/缓存/环境权限范围仍需补证。两包均无AppleDouble。

官方G03只读计算由 `recompute_statistics.py` 调用原 `content_gates.assess_improvement`，两份输入/输出保存在各包 `official-statistics.json`，重放结果一致：auto0861约6.020σ、归一化0.57886；MoE约37.388σ、归一化0.29305。没有覆盖或重写正式summary。

误报修正：非训练不要求模型；VALID_MEASUREMENT按真实schema映射；专家私有Hidden和库reference不按词判漏；配对进程共用日志不冒充独立训练；历史null元数据保留原因；历史双轨保留真值。早期新审查台账误用`@review`作包内目标已修复，原观察留 `initial-ledger-report-errors.json`，属套件演练产物错误而非原包缺陷。接口初审IR01–IR06与commands模板差异均修复并加反例，详见 [接口记录](interface-review.md) 与 [前向验证](forward-test.md)。

## 临时文件

测试夹具位于 `$TMPDIR/zcode-scratch/autoresearch-package-skills/`，每个测试结束自动清理。未向题包、官方 skill 或公共 ops 目录写入临时文件；如系统遗留空目录，可由后续常规维护按工作区清理规则处理。

最终清理确认见 `cleanup.json`：本次scratch子项已空，纯合成回执与必要验证材料留本目录。Git既有忽略规则排除materials；文件真实存在但未注册个人skills、未纳入提交。
