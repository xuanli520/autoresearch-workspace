# 已过 G03 → 长程 Agent 准入：题包整理 Skill 套件

本套件把已有、可追溯的 G03 研究材料整理为长程双 Agent 可用的冻结题包。只盘点、映射、整理、补齐、冻结和只读审查；不重跑科学，不运行训练/评分/Docker/NOP/正式长跑，不上传或提交。动态缺口只交准确“需授权执行”核验单。准入通过仍须后续有效启动授权。

## 加载与使用

十个入口均有真实 `SKILL.md` 与 YAML frontmatter，可独立加载；套件位于仓库根目录 `skill/`，不安装到个人目录。Codex只自动发现已注册skills根，未注册时在请求中给出下列实际文件路径并要求读取；不能以本README替代加载。入口共享套件级references/scripts，移动或安装须保留整套相对布局，不仅复制单个skill文件。

- “读取 `skill/autoresearch-package-finalize/SKILL.md`，用 autoresearch-package-finalize 整理这个已过 G03 的题目，来源为 `<题目路径>`，在新的 `<候选路径>` 整理，审查输出放 `<私有包外路径>`。”
- “读取 `skill/layers/autoresearch-package-layer-e-evidence/SKILL.md`，对 `<包路径>` 跑第 E 层只读审核，台账 `<路径>`，上游报告 `<路径>`，报告写包外。”
- “读取 `skill/autoresearch-package-final-audit/SKILL.md`，对 `<包路径>` 跑 pre_run 终审，使用 `<审查目录>` 的 A–H 报告。”

主skill先完成A初始化审核，再逐层执行“方向初始化→整理→实际加载层skill→审核→更新台账”，全通过才实际加载终审。缺层、哈希变化、证据不足或上游不通过不能跳层。硬路由在 `references/skill-routes.json`，由 `scripts/review_contract.py routes` 校验真实文件和哈希；报告合同校验不是语义通过证明。

| 入口 | 检查面 | 条款出处摘要 |
|---|---|---|
| A 结构初始化 | 清单/目标树/材料映射/缺口/台账/占位 | 规范§一；全流程§8.1；用户§4.1 |
| B 题面机器合同 | 八章全部语义/原生字段或真实适配/四方一致 | 教程195–258；全流程§8.2 |
| C 环境资源 | 冷构建/数据/Starter/反馈预算/数据盘/资源 | 教程260–278；统一规范P4 |
| D verifier | 实际调用/可信原子reward/错误分类/Hidden/保护 | 教程403–481；统一规范P3 |
| E 正式优化证据 | 全seed/原日志/result/模型与独立重载/只读复算 | 规范§2；官方G03/B方法 |
| F 专家骨架 | 真实说明/JSON/长程空值/后补清单 | 教程§4；全流程§8.1 |
| G 隔离 | 公开白名单/层/context/mount/env/cache/Git/solver探针 | 全流程§8.4；统一规范P3 |
| H 接入冻结 | 完整链路/NOP/同Trial/CDI/两配置/七类声明/P6 | 官方H01–H06；统一规范§2–4 |
| 终审 | 跨层一致、反伪造/泄露、P1–P7准入 | 统一规范§3；交付闭环§4–8 |

完整逐项条款、八章节子项和冲突采用理由在 `references/requirements-map.md`；目标清单与源→目标模板在 `delivery-checklist.md`；台账/空值/报告在 `schemas-and-status.md`。`source-manifest.json` 记录本次读过的规范与官方对象哈希，源版本变化须重审受影响口径。

## 边界与判定

`pre_run` 是本次核心，长程尚未发生时，只允许两条真实轨迹、唯一best_method、真实时长/轮数和best干净复验列“合理待生成”；它们不能伪造成功。NOP、完整评分、隔离/资源执行、模型重载、P6和配置齐备属于本阶段，缺失仍阻塞。已有真实历史长跑材料保留真值并核来源，不被强制清空。

`pre_submit` 是扩展终态接口，必须对接官方完整QA，真实轨迹/最佳/有效区间/轮数/清理与适用ZIP恢复全部到位；本套件pre_run通过不能转换成最终提交或平台验收通过。

官方 QA 原文件只读复用，未复制或修改脚本。层审只查一致性和官方结论的阶段对接；完整21项/G/H与Baseline B01–B08由原skill方法承担。原官方final语义报告可能因未运行长程失败，保留该报告，单列 `stage_bridge`；仅适用长程后部分延期，不遮蔽其他失败。详见 `official-qa-bridge.md`。

每项判定为通过/不通过/证据不足/不适用；合理待生成单列。确定性工具不判断方法合理性、实际独立重载、物理隔离或日志真实性；其退出0仅“未发现支持范围内的确定性矛盾”，`admission_decision`始终null。未知schema用真实映射或证据不足，不硬编码成品示例。干净构建/负向探针等命令只有从目标实际接口解析成准确核验单后才可交后续执行，模板本身不证明动态通过。

## 文件清单

```text
skill/
  README.md
  autoresearch-package-finalize/SKILL.md
  autoresearch-package-final-audit/SKILL.md
  layers/autoresearch-package-layer-{a..h}-*/SKILL.md
  references/      # 条款、目标清单、Schema、路由、QA桥接、待授权核验
  scripts/         # check_package.py、review_contract.py、阶段边界单测
  verification/    # 只读实包dry-run、独立行为检验、命令与误报修正
```

这些控制材料是专家私有，不属于solver公开资源，不自动进入标准交付包。真实题包副本只能按目标正式白名单生成；台账、QA报告和冻结索引通常保留包外，三目录内只放正式合同允许成员。原件/旧包/失败证据不覆盖、不删除。

套件位于仓库根目录 `skill/`，通过 `.gitignore` 白名单纳入版本管理；只允许套件文档、JSON 审查记录、Python 检查器及两份指定测试，缓存和凭据仍被排除。历史验证回执保留当时执行的路径与哈希，迁移后重放时以当前 `skill/` 根定位套件，不改写历史记录。

## 确定性验证命令

从仓库根运行（变量先设为真实绝对路径；审查输出须在包外）：

```bash
python3 -B skill/scripts/test_pre_run.py
python3 -B skill/scripts/test_review_contract.py
python3 -B skill/scripts/check_package.py --root "$PACKAGE" --mode pre_run --ledger "$REVIEW/qa-ledger.json"
python3 -B skill/scripts/review_contract.py routes
python3 -B skill/scripts/review_contract.py final --root "$PACKAGE" --reports "$REVIEW" --mode pre_run
```

Python≥3.9可运行套件脚本；官方TOML检查需已有Python≥3.11。脚本只读源文件并打印JSON，不自动创建/修改题包或台账、不读取模型对象、不执行候选。测试夹具全部在 `$TMPDIR/zcode-scratch/autoresearch-package-skills/` 下创建并自动清理。

实际验证结果、限制、真实缺口与误报修正见 [验证记录](verification/README.md)。需后续决策的只有是否注册到个人skills根，以及具体题目的动态核验/长跑授权；本次不注册、不执行这些操作。
