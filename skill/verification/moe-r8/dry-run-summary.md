# moe-r8：真实包只读 pre_run dry-run

检查时间：2026-10-12T14:49:41.664651+08:00；未执行题包代码、Docker、评分、训练、安装或反序列化。
包摘要：`503fea801fdaa2bea3a02fcef871ace8621baba2424f7d2db801bc90daaaf8a4`（432个文件）。

|层|局部结论|含上游结论|
|---|---|---|
|A|证据不足|证据不足|
|B|证据不足|证据不足|
|C|不通过|不通过|
|D|证据不足|证据不足|
|E|通过|证据不足|
|F|通过|证据不足|
|G|通过|证据不足|
|H|不通过|不通过|

当前长程准入：**不通过**。实际legacy GPU回执不满足现行CDI；不否定旧H06或正式科学证据真实性。

|P项|结论|
|---|---|
|P1|通过|
|P2|证据不足|
|P3|证据不足|
|P4|不通过|
|P5|证据不足|
|P6|证据不足|
|P7|证据不足|

真实问题/缺口：

- CDI实际旧路径：Compose显式`/dev/nvidia*`和旧`OfflineGPUEnvironment`。
- 本次目标白名单/源目标转换、新冻结七类声明、工具故障准备索引不足；只读审查不得补签历史初始化。
- 5090 integration仅质量反馈，历史4090科学评分不当成当前科学链路。

避免误报：

- 无AppleDouble，公开`tests/hidden_assets`为空；专家私有Hidden可非空。
- `VALID_MEASUREMENT/inference_only/NOT_APPLICABLE`语义映射有效，无模型/重载适用性；配对日志重复合法。
- 原包实际轨迹/唯一best来源保留；没有把真实长跑改为NOT_RUN。

统计：实际调用官方`content_gates.assess_improvement`，status=pass，仅计算写审查报告、不覆盖冻结证据。

全部逐ID事实/返修动作/证据哈希见layer-A…H；P1…P7与七类交接见final-audit。
