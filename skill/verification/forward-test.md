# 最小合成请求的独立前向验证

结论：实际加载主 skill、第 E/F 层及终审全文后，最小合成请求保持正确边界：长程未发生可待生成；正式模型重载缺失仍阻塞；无法确定准确核验命令仍阻塞；缺上游与失败层不能越层放行。该夹具不是真实 G03 科研证据，也没有通过题包准入。

初次验证完成于2026-10-12 14:35:59 +08:00；生产脚本冻结后于 **2026-10-12 14:46:44 +08:00** 完成最后回验，十条只读检查的预期均保持成立。解释器为已安装 Python 3.9.6。未运行题目代码、Docker、训练、评分或模型反序列化。全部临时材料位于 `$TMPDIR/zcode-scratch/autoresearch-package-skills/forward-*`，只把合成回执复制到本目录；没有读取或复制真实题目私有内容。

## 实际加载与调用

完整读取了下列真实文件及其引用的共用审核合同、要求映射、状态口径、官方桥接和待授权核验规则；路径及加载 SHA-256 保存在 [summary.json](forward-test/summary.json) 的 `skills`，检查器与共用规则哈希见 [source-manifest.json](forward-test/source-manifest.json)。

- `autoresearch-package-finalize/SKILL.md`
- `layers/autoresearch-package-layer-e-evidence/SKILL.md`
- `layers/autoresearch-package-layer-f-expert/SKILL.md`
- `autoresearch-package-final-audit/SKILL.md`

主 skill 要求 A→H 实际加载、每层立即审核、报告合同检查后才进入下层；终审缺上游会补审或保持不足。测试只验证 E/F 与终审边界，不伪造 A–D/G 的通过回执来完成主流程。路由实查成功：8层加终审共9个路由，连同主skill共10个入口，固定ID与条款一致。

合成请求构造三目录最小树、三个合成 seed 的两角色结果、非空但明确“合成非模型”的载荷、空轨迹、实际长程统计 null；**没有 reload 或等价记录**。任何 COMPLETE、分数、日志均明确为测试输入，审核者不得当成真实科研证据。包文件摘要为 `bb69dd738c28e82bf7b917a41a60fc76e245042b1ff05abb3ef5fe8174243e16`；目录/权限独立摘要为 `f8ee232b8255e35b3655df2226e639ff20e66ce4733116b722d3b4647de4a1e9`。

## 预期与真实行为

| 场景 | 真实行为 | 判定 |
|---|---|---|
| 六类 post_run 项、`value:null`、双轨 `rounds=[]`、run_summary实测统计null | `check_package.py`登记6条合理待生成，没有F层“不通过”；未把缺真实长程轮次当失败 | 符合pre_run阶段 |
| 3 seeds × 2 roles均缺reload且无等价记录 | 脚本给6条E05证据不足；完整读取E层后人工报告E05/E07不通过，其余合成科学材料不足 | 正式G03必达证据不能延期 |
| 将E05缺reload错误设为post_run/合理待生成 | 台账检查退出1，A06明确非白名单不能延期 | 阻止错误待生成 |
| 将E层报告E05直接写合理待生成 | 报告合同退出1，`E05 非长程产物不能延期` | 层内延期不能绕过台账 |
| F层诚实待生成但F01/F02及上游未核 | 报告合同合法（退出0），结论证据不足且`layers_allow_admission=false` | 合同合法不等于本层通过 |
| 无E上游回执却强行将F结论写通过 | 报告合同退出1，缺前层越层通过与保守汇总矛盾 | 阻止调用断链 |
| 缺目标Harbor版本/CLI help/provider映射，无法得到准确NOP argv | 按已加载核验规则生成`COMMAND_UNRESOLVED`、`argv:null`、`executed:false`；H报告证据不足，合同合法但不得准入 | 不猜命令、不执行、不豁免 |
| 终审只有E/F/H报告，E不通过且A–D/G缺失 | 终审合同退出1，`contract_valid=false`、`layers_allow_admission=false`、`final_semantic_admission=null` | 未伪造终审通过 |

确定性观察退出0只代表未发现其支持范围内的直接矛盾，不表示科学真实性或准入。夹具的非空合成model.bin恰好说明：文件存在和哈希稳定不能证明是真模型；E层语义审核仍拒绝把它作为有效模型/重载证据。

## 命令、证据与返修格式

全部准确argv、退出码、stdout路径/SHA-256见 [summary.json](forward-test/summary.json) 的 `receipts`。直接结果依次为 [pending.json](forward-test/pending.json)、[illegal.json](forward-test/illegal.json)、[contract-E.json](forward-test/contract-E.json)、[contract-F.json](forward-test/contract-F.json)、[contract-illegal-E.json](forward-test/contract-illegal-E.json)、[contract-bypass-F.json](forward-test/contract-bypass-F.json)、[contract-H-unresolved.json](forward-test/contract-H-unresolved.json)、[final-contract.json](forward-test/final-contract.json)。层报告本身也在同目录保留。

复现仅生成合成临时树及运行套件检查器，可在仓库根执行：

```bash
python3 -B materials/autoresearch-package-skills/verification/forward-test/fixture-driver.py
```

此驱动不执行fixture内文件；输出仍在TMPDIR。它会断言所有上述负向场景保持阻塞，不把退出1当测试失败。驱动不是生产审核器。

驱动注册`atexit`，正常/断言异常退出时均只删除本次`mkdtemp`树。需保留纯合成回执时使用`--archive-out <新报告目录>`；本次最后回验归档命令为：

```bash
python3 -B materials/autoresearch-package-skills/verification/forward-test/fixture-driver.py --archive-out materials/autoresearch-package-skills/verification/forward-test
```

最后回验后已另行确认本次`forward-9li369h1`树不存在；确认记录在`summary.json.cleanup.confirmed_removed=true`。生产脚本最终SHA-256：`check_package.py=befaeb9a88c2f5d0448e5ccd086f683379ed4d0034869b0a2aecd882450a583f`、`review_contract.py=711bfc2aeb4fb7989d913ff539627ff1df16544d0be3d1a27bc10b8273560ead`；实际终审skill哈希为`e40fe9eccc4952cfd46422ff7e795128e25644433629725195684ba9ae71c89f`。回验已覆盖最终版本的观察/报告合同，无需沿用旧哈希。

E层返修项：合成输入的 `optimization_evidence/{baseline,reference}_runs/seed_{1,2,3}/model/reload.log` 全部缺失且无等价记录 → 无法满足独立重载与COMPLETE → 回到已有真实运行挂接实际新进程重载复评分记录，不在本套件补跑 → 重验逐seed来源、模型hash、重载调用与实际指标。

H层返修项：核验计划缺目标版本、CLI/provider证据 → 无法给准确命令 → 补挂实际版本/源码/help/既有配置，核验单仍不执行 → 重验准确argv及当前版本同Trial构建/NOP/独立reward记录。

## 发现与修正

1. 初始Schema示例使用结构化 `commands` 对象数组，而报告合同采用非空字符串数组，导致合法不足报告被合同拒绝。已改为字符串命令数组，精确argv/退出码/输出hash另放 `command_receipts`；修正后E/F/H不足报告均合同合法、仍不准入。原始拒绝的原因是`commands必须是非空已执行只读命令记录数组`，属于套件接口误报，不是夹具问题。
2. 台账模板最初给post_run ID加F04后缀，与白名单不符；已对齐六个固定ID和`value:null`。`package_digest`也已改为普通文件`[[path,size,hash]]`摘要；目录/权限/symlink另绑定structure_manifest。上述修正只改新套件文档，未修改官方QA或真实题包。
3. checker与报告合同的结构合法性不认证日志真伪、模型有效性或Agent真的加载文件；本次由独立审核者实际全文读取并保守填报告。核验命令不足由语义审核写H层不足，而非脚本自动从任意文本推断。此边界明确保留，不能把合同校验当独立平台验收。

未发现主/E/F/终审正文将本阶段必达模型重载、NOP、隔离或P6准备弱化为post_run的规则，也没有看到以Reference顶替best_method的许可。此验证只覆盖最小阶段/调用边界，真实成品dry-run与官方接口兼容结果见套件其他验证记录。

临时树与失败重试树在证据归档后定向删除；驱动后续执行也自动清理自己的临时树。合成驱动、报告与必要回执保留在套件，未清理或变更任何真实材料。
