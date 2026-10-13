# 所有层共用的只读审核合同

本文件必须由每个层 skill 实际读取。条款细则见 requirements-map.md，模板见 schemas-and-status.md。层只检查层内内容/一致性与官方结论对接，不重写 QA21 项、H01–H06、G01–G03 或 B01–B08。

1. 解析输入包根、台账、上游报告、输出目录。输出必须在包外；拒绝与源证据同路径、覆盖已有报告、写入未知符号链接。审核中的“回写台账”是返回结构化 ledger_delta，由主 skill 在已授权包外台账追加版本；独立只读审核默认输出 delta，不原地改台账。真实成品 dry-run 可在包外建本次审查台账，不修改原包。
2. 加载本层真实 SKILL.md、对应条款与所引用官方方法；记录各文件SHA-256。先初始化本层 definition，判断实际 task_type/profile/target_schema，不能硬套示例。缺上游报告=证据不足；可继续只读查本层但不能越层放行。
3. 对本层全部 ID 逐项读内容，证据写相对路径+SHA-256+行/JSON pointer/Trial定位；source/ops 的包外证据根单独声明，不能把其绝对路径写进公开题包。每项包含事实、原因、动作与重验材料。文件存在只能证明存在，日志非空只能证明非空，关键字仅是线索；动态回执要读命令、调用链、版本、结束与数值。
4. `check_package.py` 只产生确定性 facts/findings；对发现的 schema/path/hash/seed等问题逐项核实，保留误报原输出和修正理由。不能把此脚本称为层 skill 已完成，也不能直接按facts拼接全通过报告。
5. 状态只有：通过、不通过、证据不足、不适用、合理待生成。必达项有反证=不通过，无充分证据=证据不足；不适用须解释任务事实；合理待生成仅白名单长程产物且未发生（已有真实值保留）。层汇总：有不通过→不通过，否则必达有证据不足→证据不足，否则适用必达均过→通过；全不适用方可层不适用。合理待生成单列，不与必达混算。
6. 返修项固定：“事实及文件位置 → 不满足原因 → 具体修改动作 → 重验材料”。发现上游问题写 `return_to_layer`；不改原件、不运行代码，动态操作写准确待授权核验单。风险最多3条。
7. 输出 layer-X.json 与 ledger_delta，包含调用回执 `skill:{path,sha256}`、`source_basis`、`commands`、`definition`、`checks`、`upstream`、`conclusion`、`risks`。运行 review_contract.py 校验结构/hash；语义结论由审核 Agent承担。该脚本不会验证动态事件真实性，不是防伪签名系统。

通用命令（参数必须替换为实际路径；MODE取本次pre_run或pre_submit；本套件脚本不执行题目代码）：

```bash
python3 -B "$SUITE/scripts/check_package.py" --root "$PACKAGE" --mode "$MODE"
python3 -B "$SUITE/scripts/review_contract.py" layer --root "$PACKAGE" --report "$REVIEW/layer-X.json"
```

报告在候选后续变化时不可悄悄更新 hash。以新报告版本绑定当前冻结清单；仅受影响层重审。复用未变层须保留原报告哈希、`reuse`原因/变更影响以及其全部证据哈希一致回执；无法限定影响范围则扩大审查。冻结后任何更改都创建新版本，不热改冻结文件。
