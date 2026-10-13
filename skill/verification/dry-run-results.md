# 两个真实成品包只读 dry-run 汇总

完成：2026-10-12T15:04:55.513894+08:00。已真实加载主Skill、A–H层与终审共10个文件，完整按49个固定ID审查两个包；未执行题包代码、Docker、训练、评分、安装或反序列化。

|层|auto0861局部|MoE r8局部|两包含上游结论|
|---|---|---|---|
|A|证据不足|证据不足|证据不足|
|B|证据不足|证据不足|证据不足|
|C|不通过|不通过|不通过|
|D|通过|证据不足|证据不足|
|E|通过|通过|证据不足|
|F|通过|通过|证据不足|
|G|证据不足|通过|证据不足|
|H|不通过|不通过|不通过|

两包当前长程准入均为**不通过**，是现行CDI实际接入反证与当前冻结配置/材料映射缺口；保留旧包正式科学/H06/长程事实，不改写原包历史合格性。

- 两包20条最终只读命令全部exit0：check_package带包外ledger、8层report contract、final contract各10条。报告合同合法且layers_allow_admission=false，符合保守语义。
- 官方统计接口真实重放两次matches_saved_result=true：auto0861 Δ/σB=6.0199081217，归一化=0.5788565891；MoE Δ/σB=37.3881783112，归一化=0.2930546130。正式summary未被覆盖。
- auto0861原包441文件、摘要66295f12d0d090e45a9401d3992abcbeef0783ece6ab479797eea4aa5f5c3018；MoE432文件、摘要503fea801fdaa2bea3a02fcef871ace8621baba2424f7d2db801bc90daaaf8a4。结束时摘要与初始完全相同。

真实问题与证据不足：

- 两包legacy GPU注入直接不满足当前CDI；auto0861是实际main/verifier device-request，MoE为显式/dev/nvidia Compose和旧provider。
- 新白名单/源目标转化、当前七类运行声明/两配置、数据盘全链、当前双模型故障处理索引不足；未补签历史初始化。
- auto公开全历史镜像层与环境token能力范围不足；MoE完整科学反馈仍绑定RTX4090，RTX5090 integration只认证质量/接入；部分失败分类消费映射待证。

观察与误报处理：

- 未发现AppleDouble；两公开tests/hidden_assets为空。私有专家Hidden非空、库reference命名空间不判泄漏。
- 非训练MoE没有model/artifact/reload完全合法；VALID_MEASUREMENT采用真实语义映射，checker原manual警示保留，未造成假阳性“不通过”。
- auto10份artifact/model与实际新进程重载齐；historical null有原因/替代Trial/callback记录。
- 两原包已有真实轨迹/best，保留来源不清空；同seed B/R共享真实配对日志不判重复训练。
- 本次审查初版ledger误用@review作为包内target/sources，checker正确拒绝；已修复到包内target与包外引用分开，初始拒绝归档为initial-ledger-report-errors.json，明确是审查产物错误。

复算命令：

```sh
python3 -B materials/autoresearch-package-skills/verification/recompute_statistics.py --review materials/autoresearch-package-skills/verification/auto0861/official-statistics.json
python3 -B materials/autoresearch-package-skills/verification/recompute_statistics.py --review materials/autoresearch-package-skills/verification/moe-r8/official-statistics.json
```

逐项报告、P1–P7、七类交接、命令/版本/哈希回执与返修材料分别在两个子目录；forward-boundary-independent-review.json另记独立14项边界核对通过。
