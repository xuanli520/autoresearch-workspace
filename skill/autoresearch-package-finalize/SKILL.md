---
name: autoresearch-package-finalize
description: 将已经通过 G01–G03 的既有研究材料盘点、整理、补齐并冻结为长程双 Agent 准入题包；逐层实际加载审核 skill 并终审，不重跑科学、不启动长跑。
---

# 已过 G03 的题包整理

输入：题目源目录、已有 G03 结论及原始证据索引、目标 Harbor/Schema、拟生成的新候选目录和包外私有审查目录。缺少关键输入先盘点，其余工作继续；不得凭“已过 G03”跳过证据挂接。若原始证据推翻起点，退回科学阶段，不在本 skill 中补实验。

先读取仓库 `AGENTS.md`、`AutoResearch出题全流程指引.md`、`双Agent长跑与题目包验收规范.md` 及 [来源映射](../references/requirements-map.md)。资料优先级与冲突采用口径以映射为准。再读 [交付清单](../references/delivery-checklist.md)、[Schema与状态](../references/schemas-and-status.md)、[审核合同](../references/audit-protocol.md) 和 [官方QA桥接](../references/official-qa-bridge.md)。此套件为专家侧工具，不能放入 solver 公开快照。

## 不可跨越的边界

- 只处理既有材料。禁止任何 B/R/训练/评分/正式 Agent 运行、安装、上传或提交；不修改冻结正式证据、原始目录、历史发布、官方 QA 或已有规范。
- 新建候选副本，逐文件记录源→目标、转换与 SHA-256。原始日志和模型原字节保留；格式变化写派生 JSON 并引用原件。只有无法恢复的历史元数据可 `null` + 原因；必达证据缺失仍阻塞。
- 干净构建、NOP、solver 探针等动态核验只生成 [待授权核验单](../references/authorized-verification.md)，准确列命令、资源、退出/回收和判定标准；本次不执行。已有授权也不能扩大本 skill 的禁跑范围，执行应交给独立阶段。
- 未开始的两条轨迹、best_method、真实时长/轮数和最终复验仅待生成；不从 Reference/专家实验填值。已完成历史题包保留真实产物并核验来源，不清空、不误判为预填。

## 初始化必须先完成

1. 读取本题进展清单中的最后有效授权、预算、停止条件、来源运行及现状；声明本次仅整理。建立唯一 Docker/Harbor profile 和目标 Schema 映射，不照搬示例 `schema_version`。
2. 按交付清单建立本题目标树与必/选/阶段清单，逐项记录目标路径、状态、来源、动作。扫描既有 G03 材料，先输出“已有材料→目标路径→原样复制/格式对齐/补字段/保留原日志”的映射，再进行整理。
3. 建缺口清单，记录补齐路径、原始记录、授权需求及不可补原因；登记 result 缺字段、摘要日志、空 reload、Hidden、题面章节和路径等真实缺口。不得为凑文件补造内容。
4. 在包外私有审查目录维护 `qa-ledger.json`、`delivery-checklist.json`、`material-map.json`、`gaps.json`；使用状态模板，长程项单列。台账不自动成为标准三目录内的新成员。
5. **立即加载并执行 A 层 skill**；A 通过后才能进入 B。不能先整理全包后补签 A。

## 每层的强制调用

调用表由 [skill-routes.json](../references/skill-routes.json) 固定。先运行下面只读命令列出真实路径和 SHA-256；文件缺失或读取失败立即阻塞，不能凭记忆模拟调用。

```bash
python3 -B "$SUITE/scripts/review_contract.py" routes
```

`SUITE` 是本文件上一级的套件绝对路径；`PACKAGE` 是新候选包根；`REVIEW` 是包外私有审查目录。所有命令参数用实际路径替换，不能将变量示例当作已配置。

对 A→B→C→D→E→F→G→H 依次：

1. 初始化本层 `definition`：本题交付物、验收口径、重点风险；先读路由所指的真实 `SKILL.md` 及其指定条款。
2. 仅在候选副本整理该层产物；更新清单、源目标映射及台账。涉及训练器、数据、预算、评分意义的实质修改则停止本分支并登记科学重审，不能修代码后沿用旧 G03。
3. **立即执行该层 skill 的全文流程**，保存独立层报告。执行文件不是运行 Markdown shell：须实际读取该 skill，逐项审内容与证据，再调用确定性脚本；记录 skill 路径/哈希、检查命令和来源定位。
4. 运行 `review_contract.py layer --root "$PACKAGE" --report "$REVIEW/layer-X.json"` 校验报告合同；脚本成功不代替语义审核。只在层结论通过（或有理由的不适用）时进入下一层；合理待生成仅用于长程后项。
5. 不通过或证据不足按“事实及位置→原因→修改→重验材料”返修，受影响项重审；需上游修改回退相应层并重审依赖层，不越层放行。动态缺口输出核验单，保持未完成，不能把“命令已给”写成通过。

各层读取入口不可省略：

| 层 | 必须加载 |
|---|---|
| A | [结构初始化](../layers/autoresearch-package-layer-a-structure/SKILL.md) |
| B | [题面与机器合同](../layers/autoresearch-package-layer-b-contract/SKILL.md) |
| C | [环境与资源](../layers/autoresearch-package-layer-c-environment/SKILL.md) |
| D | [评分与verifier](../layers/autoresearch-package-layer-d-verifier/SKILL.md) |
| E | [优化证据](../layers/autoresearch-package-layer-e-evidence/SKILL.md) |
| F | [专家骨架](../layers/autoresearch-package-layer-f-expert/SKILL.md) |
| G | [隔离可见性](../layers/autoresearch-package-layer-g-isolation/SKILL.md) |
| H | [接入冻结](../layers/autoresearch-package-layer-h-freeze/SKILL.md) |

## 终审与交接

全部层通过后，**实际加载并执行** [终审 skill](../autoresearch-package-final-audit/SKILL.md)，mode=`pre_run`；记录同样的调用回执。终审失败先修复、重审受影响层，再终审。证据不足时交付未完成清单，不宣布准入。

只有终审通过才能冻结候选：包外保留文件/空目录/权限/SHA-256清单、版本和带时区时间、层报告与终审哈希、来源运行和变更影响；再更新本题已有进展清单（本任务若只获只读授权则返回可应用补丁）。交接七类运行声明及两份真实可校验配置的引用，注明 `launch_authorization=NOT_GRANTED` 或引用既有有效授权；题包就绪不等于允许执行。长跑由官方三工具在另行授权阶段完成，本 skill 到此结束。
