---
name: autoresearch-package-layer-a-structure
description: 独立只读审核题包第 A 层（结构初始化），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 A 层：结构初始化

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 A 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 A01–A06 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：现有 G03 材料的目标树、原件映射、缺口与台账；此层必须在其余整理前审核。
2. 逐项审 A01–A06，按出处核内容与来源：核每项必/选/阶段/证据来源和动作；source 与 candidate 不同，原件/旧包不覆盖；每个缺口有处理路径；仅长程白名单占位；台账可复用且来源 hash/版本/运行明确。
3. 对接 官方 submission-format；全流程§8.1；统一规范P2 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-A.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：不将缺台账直接解释为历史题包不合格；作为本次整理未完成。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
