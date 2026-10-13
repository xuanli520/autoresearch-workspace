# 套件接口独立审查

审查日期：2026-10-12（Asia/Shanghai）。范围仅限新套件的10个SKILL、全部references与scripts，以及两个官方QA对象的只读接口；未执行题目代码、Docker、评分、训练、安装或模型反序列化。

初审结论：技能路由与分层职责完整，官方QA边界正确；发现报告合同和模板接口缺口，必须修复后回验，不能以脚本退出0代替语义准入。下表记录初次反例与要求，不涂改先前观察。

| 编号 | 可定位事实 → 不满足原因 | 具体修改动作 → 重验材料 |
|---|---|---|
| IR01 | `scripts/review_contract.py check_layer`接受`source_basis:[{}]`、`commands:[]`与所有文件证据`locator:null`，初始合成调用返回`([], "通过")`；来源版本、已执行命令和定位未按硬契约检查 | 校验来源对象的真实路径/哈希与命令记录非空；通过证据要求非空定位；补拒绝空出处/空命令/空定位的反例测试 |
| IR02 | B层引用仅含`layer/conclusion/package_digest`三字段的伪A报告，初始仍返回`([], "通过")`；只核三字段与文件哈希不能证明已加载上游审核 | 递归或分层验证上游报告完整合同、skill回执、模式与检查ID，限制循环；补最小伪报告和上游失败反例 |
| IR03 | final读取`layer-A.json`至`layer-H.json`时没有逐槽核对报告内`layer`；重复A报告可能替代B–H，违反全部层审核硬调用 | 固定槽位和实际层标识必须一致；补8个槽位都放A报告的拒绝测试 |
| IR04 | 合成空题包有19个确定性结构失败，全通过报告仍可独立通过合同；初审合同输出未连接这些反证 | 保持合同结构和语义结论分开，但最终`layers_allow_admission`必须保留确定性反证；补“报告合同合法但包结构缺失”测试，并明确`final_semantic_admission:null` |
| IR05 | `schemas-and-status.md`初始模板使用`F04.trajectory_codex`且没有`value:null`，检查器白名单要求`trajectory_codex`；初始摘要说明也把文件快照与权限结构摘要混为一类 | 统一白名单ID/空值模板、pending清单；分别准确说明snapshot与structure_manifest算法；用模板合成夹具回验 |
| IR06 | `check_package.py ledger`初始仅核post_run ID白名单，`trajectory_codex`可以指向`tests/test.sh` | 核对ID对应目标路径/字段，防评分、环境或任意对象被冒称长程产物；补错目标反例 |

反例运行方式：在`$TMPDIR/zcode-scratch/autoresearch-package-skills/`下用`TemporaryDirectory`创建纯文本最小夹具，导入本套件`Inspection/strict_json/review_contract`并调用`check_layer`；只运行审查工具，不导入待检包。夹具结束自动清理。初审反例结果通过工具输出已留在本会话，修复验收由套件回归单测保留。

已确认的接口与边界：

- 10个入口均为真实`SKILL.md`，frontmatter含name与description；A–H固定ID与`skill-routes.json`一致；内部相对层/参考链接能够解析。
- 主skill要求逐层实际读取SKILL并保存哈希回执，层内定义→整理→立即审核→返修→终审的顺序明确；独立触发缺上游不能放行。
- 新工具没有候选执行、shell/subprocess、模型加载或正式运行调用。测试中的写入/删除都限定临时夹具；正式检查工具只读取文件、解析JSON、散列并输出JSON。
- 官方总体QA仍采用implementation schema 4；precheck不是pre_run。官方终态QA16/18/21保留原结论，阶段桥接仅延期真实长程产物，NOP/H06、模型重载、评分隔离和G03均不能延期。
- Baseline角色与B01–B08继续由官方专项方法承担。新脚本不复制显著性/G03算法，不要求Starter达到SOTA，不把未知schema、同进程配对日志或库里的reference字样自动判为失败。
- 7个官方QA脚本SHA-256与本次开工只读记录逐一一致；未修改、复制或热覆盖官方脚本。新的`check_package.py`与`review_contract.py`为套件确定性合同，不能替代官方方法语义复核。

最终限制：本审查验证了文件接口和合成反例；不鉴定动态日志真实性、不证明目标镜像物理隔离、不证明CDI/资源硬限实际部署。真实包dry-run与层级语义结论应读`verification/README.md`及其记录；缺少运行证据仍列证据不足，不能由本报告覆盖。

## 修复后回验（2026-10-12）

主维护者修复 IR01–IR06 后，以下结果均通过：

- `python3 -B scripts/test_pre_run.py`：21/21 通过，0.190 秒；覆盖严格 JSON、结构/权限摘要、模型类型、公开 `tests/hidden_assets`、台账摘要、长程占位、错误层类型与只读边界。
- `python3 -B scripts/test_review_contract.py`：15/15 通过，0.080 秒；覆盖空出处/命令/定位、伪上游、层槽位替换、哈希变化、缺检查、循环/上游合同和确定性矛盾不能放行。
- `python3 -B scripts/review_contract.py routes`：A–H 与 FINAL 共 9 个路由均存在，frontmatter 和 SHA-256 回执可读；10 个 SKILL 文件均具备 YAML frontmatter。
- 官方 QA 七个脚本 SHA-256 与开工只读记录一致；未修改官方脚本。`source-manifest.json` 已存在，README 中的验证入口可解析。

修复后的接口检查仍保留两个交付前建议：终审技能文档中的示例命令应将硬编码 `--mode pre_run` 改为实际 `MODE`（否则仅影响人工执行 `pre_submit` 时的说明一致性）；`review_contract.py` 递归上游合同可额外核对上游 `mode` 与当前报告 `mode` 相同（避免跨阶段报告复用）。这两项不影响当前 21+15 合成回归测试，但在正式冻结前应补一条模式不一致反例和文档命令更新。

当前独立审查结论：**接口合同已可用，建议完成上述两项模式一致性收尾后冻结；语义审核、官方 QA 与动态核验仍不可由脚本回归替代。**
