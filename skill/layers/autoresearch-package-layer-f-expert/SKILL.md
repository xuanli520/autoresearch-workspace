---
name: autoresearch-package-layer-f-expert
description: 独立只读审核题包第 F 层（专家证据骨架），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 F 层：专家证据骨架

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 F 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 F01–F05 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：专家说明/expert_annotation/run_summary的真实字段、长程空值与后补清单。
2. 逐项审 F01–F05，按出处核内容与来源：用真实材料填方法/协议/B/R/U与自验来源；按目标Schema处理空值，扩展不被允许则阶段登记留包外；未运行轮次/真实时长null+原因，rounds可为空且非成功轨迹；best_method不得由Reference顶替；只登记长程后必补内容与来源接口。
3. 对接 官方QA16/18/21；教程§4；全流程§8.1/§9–10 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-F.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：已有历史真实长跑产物保留并核来源，不清空；QA16/18/21的终态要求只按官方stage_bridge延期，不篡改官方结论。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
