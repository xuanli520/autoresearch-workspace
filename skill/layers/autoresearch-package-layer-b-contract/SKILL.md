---
name: autoresearch-package-layer-b-contract
description: 独立只读审核题包第 B 层（题面与机器合同），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 B 层：题面与机器合同

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 B 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 B01–B06 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：完整八章节题面、目标原生 task.toml 或真实适配映射及四方合同。
2. 逐项审 B01–B06，按出处核内容与来源：按 B01 的八章所有语义子项阅读全文；路径、API、单位/聚合/方向、质量门和资源逐条与解析器/guard/真实评分调用链比对；核 Schema 版本实际支持字段及适配入口；排查论文标题/arXiv/仓库名等答案线索。
3. 对接 官方QA01/02/03/05/07/08/13/14与G01；H02/H03 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-B.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：八标题齐全不等于内容合格；不虚构不存在的 JSON 提交格式；题面不写专家侧双 Agent 总时长。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
