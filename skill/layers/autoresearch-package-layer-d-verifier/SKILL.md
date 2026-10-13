---
name: autoresearch-package-layer-d-verifier
description: 独立只读审核题包第 D 层（评分与 verifier），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 D 层：评分与 verifier

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 D 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 D01–D06 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：测试入口、可信评分与 reward、错误分类、Hidden注入与保护恢复、约束执行表。
2. 逐项审 D01–D06，按出处核内容与来源：从test.sh逐跳定位实际评分器和可信原子reward写入，不依赖cwd；检查旧结果清除、候选自报不可信、有限数值/合法负分/>1、Hard Gate−1与其他错误分开；公开tests/hidden_assets为空并核既有生成/注入调用证据；保护面从可信hash版本恢复；题面逐约束找到不可覆盖的执行点。
3. 对接 官方QA03/05/06/13/14/17/19/20；H04及hidden_review；统一规范P3 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-D.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：空Hidden目录不代表注入实现；只在候选代码设置allocator不证明显存硬上限；trusted verifier声明不代替身份/权限证据。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
