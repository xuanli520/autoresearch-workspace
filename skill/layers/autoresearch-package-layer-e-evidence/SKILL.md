---
name: autoresearch-package-layer-e-evidence
description: 独立只读审核题包第 E 层（优化证据整理），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 E 层：优化证据整理

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 E 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 E01–E07 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：原样全正式seed的B/R目录、原stdout/stderr、result、模型/独立重载、可复算comparison及原G03结论。
2. 逐项审 E01–E07，按出处核内容与来源：不抽样不改seed；逐seed核协议/数据/预算/锚点/质量门与状态，摘要result字段对照原始日志；训练型真模型/artifact/hash/新进程重载实分逐一对应；非训练NOT_APPLICABLE且不造空model；统计仅在审查输出内复算，不能重写冻结正式summary；COMPLETE须全seed有效与适用模型重载全过。
3. 对接 官方submission-format/content_gates及G01–G03；Baseline B01–B08；规范§2 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-E.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：非空reload只是一项事实，仍读真实加载与复评分；等价非空记录可用；同一配对进程共享日志不是重复训练反证；历史null需原因与替代来源。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
