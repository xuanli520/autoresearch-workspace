---
name: autoresearch-package-layer-h-freeze
description: 独立只读审核题包第 H 层（运行接入与冻结），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 H 层：运行接入与冻结

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 H 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 H01–H07 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：完整链路、当前版本NOP、同Trial、资源/CDI/健康、模型故障准备、冻结清单及七类声明/两份配置。
2. 逐项审 H01–H07，按出处核内容与来源：读干净构建→Starter→完整Public/Dev→可信reward→退出回收证据；当前包版本NOP正常构建/Trial/separate/verifier/有效reward，0本身不失败；H01–H04必查，H05已有即查、H06同Trial绑定；GPU main/verifier官方CDI唯一UUID/void/新CUDA进程/资源更新后回执；P6两模型工具/STOP/超时/失联/接续/清理证据；冻结版本/带时区时间/全hash与两份可直接引用的配置/七类运行声明。
3. 对接 官方QA05/17；H01–H06；统一规范§2/§3(P4–P7)/§4；交付闭环§4/§8 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-H.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：NOP不代替完整评分/模型链路；历史H06不等于现行CDI就绪；不要求专家12h soak；未获长跑授权不能启动，配置就绪与执行授权分列。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
