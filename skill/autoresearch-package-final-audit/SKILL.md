---
name: autoresearch-package-final-audit
description: 独立只读汇总 A–H 层结论并审核题包长程准入，核心模式 pre_run；区分长程待生成与真实缺失，pre_submit 对接官方完整 QA，不运行题目代码。
---

# 题包终审

输入：题包路径、包外台账/清单/映射/缺口、A–H 报告及冻结清单；mode 默认 `pre_run`。读取 [审核合同](../references/audit-protocol.md)、[条款映射](../references/requirements-map.md)、[状态](../references/schemas-and-status.md)、[官方QA桥接](../references/official-qa-bridge.md) 和 [路由](../references/skill-routes.json)。独立触发时缺少上游报告应实际加载相应层 skill 补审；只读范围内不整理原包。

只读：不执行提交代码、不构建/启动 Docker、不训练/评分/安装、不反序列化模型；仅将报告写到明确的包外输出目录。动态项按 [核验单](../references/authorized-verification.md) 标“需授权执行”，未执行不能放行。

1. 先核 source manifest 的规范版本/哈希，候选文件白名单、三目录职责、空目录/权限、相对路径和符号链接，再核全文件哈希。台账和报告不得落进公开面。
2. 读取每个真实层报告、加载过的 skill 路径/哈希和上游回执，检查必需 ID 全覆盖、返修闭环、与冻结版本绑定。不同包版本只能按明确变更影响和未变证据哈希复用；缺报告、漏检查、缺语义依据均为证据不足。
3. 全局交叉核对 instruction ↔ task.toml 原生/实际适配 ↔ guard/评分器 ↔ 资源执行 ↔ Docker context/profile ↔ reward 合同。任何两处冲突回退责任层；静态关键词或文件存在不能提供通过依据。
4. 防伪造：严格 JSON/有限数值、摘要/seed/协议/hash一致；只读复算全 seed 均值、`n−1`样本标准差、改善方向、`Δ≥3σ_B`、Reference归一化 `[0.15,0.8]` 及 COMPLETE。引用官方方法/函数，不重写正式 summary。锚点事前依据必须有来源，数字达标不能证明事前固定。
5. 防泄露：核最终公开白名单以及镜像层/context/挂载/环境/缓存/Git历史/检索入口/受限身份探针回执。整个专家包含私有材料属正常，但绝不能整体挂给 solver；扫描线索需读上下文，不将依赖库 reference 命名空间判为解答。
6. 按下表逐项评审 P1–P7；汇总未完成与需授权动态核验。H01–H04必审，H05/H06有材料按同 Trial 对账；当前版本NOP必需，0分本身不失败。NOP不能替代完整Public/Dev和模型工具链。CDI、数据盘和不可覆盖资源硬限不得由旧成功Trial替代。

| 准入项 | 汇总层 | pre_run 通过依据 |
|---|---|---|
| P1 科学内容 | B/E + 官方G01–G03/B01–B08 | 原始全seed、公平协议/锚点、模型和真实重载 |
| P2 结构接口 | A/B/H | 清单、字段真实映射、路径/profile、哈希 |
| P3 隔离评分 | D/G | 可信保护、白名单与实际受限身份负向探针 |
| P4 环境资源 | C/H | 数据盘、目标资源、三工具版本、CDI与执行/取消证据 |
| P5 完整运行 | C/D/H | 当前可复用版本的干净构建/Starter/完整评分/reward/退出回收；另有NOP |
| P6 模型与故障 | H | 两模型真实工具链、单流STOP/超时/失联/上下文/外部清理；无变化旧工具验收可复用 |
| P7 启动信息 | F/H | 两份冻结配置、七类声明、原截止/登记/回收；不要求已启动 |

`pre_run` 只允许两真实轨迹、唯一 best_method、实测长程时长/轮数和 best 干净复验合理待生成；详见状态白名单。已有历史完成产物保留其事实。当前评分/模型重载/隔离/NOP/P6证据不能转为待生成。配置就绪与启动授权分列：没有新的长跑授权仍可判“题包准入通过，待授权启动”，不能声称可立即执行命令。

`pre_submit` 还必须实际加载官方总体 QA 和 Baseline skill，按它们完整终态口径审核 QA16/18/21、best_method 来源与干净复验、区间时长/轮数、结束清理及适用ZIP恢复；不接受合理待生成。本模式不提供实验执行能力，缺证据就退回。

先设置 `MODE` 为本次实际 `pre_run` 或 `pre_submit`，运行确定性观察与报告合同检查：

```bash
python3 -B "$SUITE/scripts/check_package.py" --root "$PACKAGE" --mode "$MODE" --ledger "$REVIEW/qa-ledger.json"
python3 -B "$SUITE/scripts/review_contract.py" final --root "$PACKAGE" --reports "$REVIEW" --mode "$MODE"
```

脚本结果只是确定性检查；必须完成以上语义步骤后出终审。输出 `final-audit.json` 和短报告，含 mode、长程准入结论（通过/不通过/证据不足）、P1–P7逐项状态、官方原结论及stage_bridge、层回执、全局交叉核对、可复算命令/文件/哈希/计算、未完成项、待生成清单、七类交接引用、启动授权状态、风险≤3条。通过必须有当前有效A–H及全局依据；不通过按固定返修格式回退；不能用总体统计通过率覆盖单个必达失败。仅更新授权的包外台账，源题包保持只读。
