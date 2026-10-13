---
name: autoresearch-package-layer-g-isolation
description: 独立只读审核题包第 G 层（隔离与可见性），用于已过 G03 的长程准入整理或单层返修；不执行题目代码和动态核验。
---

# 第 G 层：隔离与可见性

输入：本层产物路径、证据台账、上游层结论及包外输出目录；mode 默认 pre_run。单独说“对路径跑第 G 层审核”即可触发。

**必须先实际读取** [共用审核合同](../../references/audit-protocol.md)、[条款映射](../../references/requirements-map.md) 中 G01–G06 全部项、[Schema与状态](../../references/schemas-and-status.md)、[官方QA桥接](../../references/official-qa-bridge.md)。不能只读摘要或按文件存在签通过。

1. 初始化本题本层方向与 definition：白名单公开快照、solver受限身份、可信私有面、跨流隔离及真实负向探针/待授权步骤。
2. 逐项审 G01–G06，按出处核内容与来源：核允许清单到镜像层/context/mount/env/cache/Git remotes/tags/reflog/history/检索接口；Reference/Hidden/专家证据/另一组轨迹/密钥/本机路径不进入solver；只读公共缓存与独立写缓存分开；查symlink、权限、宿主socket/SSH等越界；读取既有solver身份负向访问回执，缺则写待授权步骤。
3. 对接 官方QA06/12/19/20及exposure-manifest方法；全流程§8.4；统一规范P3 的实际只读方法和既有结论；有矛盾先定位适用版本/阶段，保留官方原结论，不自设更弱标准。
4. 执行共用合同的确定性检查并逐项核实 findings；动态检查只给 [需授权步骤](../../references/authorized-verification.md)，不得执行提交代码、Docker、训练、评分、安装或反序列化模型。
5. 输出独立 `layer-G.json`、简短报告与 ledger_delta；各项给状态/出处/证据路径及hash/定位/事实/原因/动作/重验材料；风险≤3条。用 `review_contract.py layer` 验证报告。必达有失败或证据不足不得宣称本层完成，上游问题回退责任层。

本层易误判处：私有专家包含Reference正常但不可整体挂载；reference关键词与绝对路径仅线索，必须确认实际可见内容；删文件不消除历史层。

本层不修改源包或原台账；回写仅输出包外 delta。四档判定（通过/不通过/证据不足/不适用）加单列合理待生成，遵从共用状态白名单。证据不足不能被另一项通过抵消。
