---
name: autoresearch-package-layer-c-environment
description: 独立只读审核题包第 C 层（环境与资源），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 C 层：环境与资源

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 C 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 C01–C06 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：自包含环境/依赖锁、公开资产语义、Starter 身份、反馈时限和目标数据盘/资源证据。
2. 逐项审 C01–C06，按出处核内容与来源：读 Docker COPY/context/依赖与已有冷构建日志；train/dev边界不能互换；区分Starter=Baseline与Scaffold+私有正式Baseline；单轮≤2h有真实耗时/全流程依据；数据盘覆盖Docker/containerd/临时/缓存/日志/可写层；资源预约与硬限分别核对。
3. 对接 官方QA04/05/06/09/10/11/13；G02/B01–B08；统一规范P4 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-C.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：不靠文件存在断言冷构建成功；Scaffold例外需真实B映射，不把可运行脚手架算合理Baseline；不主动构建。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
