# 台账、报告、空值与 COMPLETE 口径

套件Schema只管理私有整理/审查材料，不能覆盖平台Schema。目标平台拒绝额外字段时，原因、待生成状态和映射放外部台账，不在expert_annotation/run_summary/trajectory随意加字段。本文件示例是模板，使用前替换为真实内容；示例占位文本不能作为准入证据。

## 状态与阶段

逐项状态固定为`通过 / 不通过 / 证据不足 / 不适用 / 合理待生成`。前四档是判断；“合理待生成”只允许`required_stage=post_run`项，表示长程尚未发生且没有伪造成果。

| 状态 | 必须有的依据 | 对pre_run结论的影响 |
|---|---|---|
| 通过 | 真实内容符合条款、有定位和哈希、无决定性反证 | 可放行适用项 |
| 不通过 | 有直接反证或已确认本阶段必达实现/材料缺失 | 不准入；同时保留其他不足 |
| 证据不足 | 无法读取、版本/来源/语义未核或现有材料无法判断 | 不准入；不写成已证实故障 |
| 不适用 | 任务类型/角色/协议证明本项条件不成立 | 不阻塞，必须解释；不是缺材料豁免 |
| 合理待生成 | 限两轨迹、best_method及真实长程时长/轮数/选优/复验等post_run产物 | 不阻塞pre_run；pre_submit不可沿用待生成当通过 |

层/终审总判定仍用前四档。全适用pre_run项通过（条件项合理不适用），post_run均诚实登记，才可总判`通过`。存在实质不通过优先判`不通过`；无反证但必达证据不足则`证据不足`。整体不适用仅用于任务确实不属于本套件范围，不用来掩盖未完成。

以下绝不是合理待生成：G03正式B/R原日志/模型/重载、题面/环境/评分实现、当前版NOP、完整Public/Dev链路、必要隔离/资源执行或适配验收。动态步骤被禁止本轮执行时仍保留本阶段缺口，写“需授权执行”，不能自动转post_run。

## 台账结构

```json
{
  "schema_version": "package-ledger.v1",
  "mode": "pre_run",
  "package_digest": "<sha256>",
  "items": [
    {
      "id": "E05",
      "layer": "E",
      "target": "optimization_evidence/baseline_runs/seed_<real>/model/reload.log",
      "required_stage": "pre_run",
      "status": "证据不足",
      "reason": "待核对原独立重载输出；不可用空日志替代",
      "sources": [
        {"path": "<read-only-original-path>", "sha256": "<sha256-or-null>", "origin_run": "<real-run-or-null>", "version": "<real-version-or-null>"}
      ],
      "action": "读取已有真实重载记录并建立等价映射；不存在则保留缺口",
      "authorization_required": false
    },
    {
      "id": "trajectory_codex",
      "layer": "F",
      "target": "expert_evidence/trajectory_codex.json",
      "required_stage": "post_run",
      "status": "合理待生成",
      "value": null,
      "reason": "G03后、长程前，尚未启动研究Agent",
      "sources": [],
      "action": "另行授权长程后由受管工具真实收集",
      "authorization_required": true
    }
  ],
  "layers": {},
  "pending_long_run": ["trajectory_codex"]
}
```

`pre_run`项的`items.id`可在固定层检查ID后加对象后缀，便于逐seed/逐产物登记；层报告聚合仍用`A01..H07`固定ID。`post_run`项只能使用以下六个固定ID，不能加F前缀或其他后缀：`trajectory_codex`、`trajectory_seed`、`best_method`、`agent_effective_duration`、`agent_round_count`、`best_method_clean_revalidation`。未运行项均含`value:null`，`pending_long_run`引用这些已登记ID，不能填假run_id/产物hash。全套长程前骨架应登记六项；示例只展示一项结构，不是完整台账。`layers`保存每层结论、报告路径/hash、对应package_digest与受影响上游。来源没有可恢复run/version/hash时允许null，但reason必须解释；不能填字符串`"null"`。包内目标路径统一相对包根；只读外部原件来源可用明确外部路径，绝不写进Agent可见文件。

## 层报告结构

post_run固定目标映射：trajectory_codex/trajectory_seed各指向同名expert_evidence JSON；best_method指向`expert_evidence/best_method`；agent_effective_duration/agent_round_count/best_method_clean_revalidation指向`expert_evidence/run_summary.json#实际字段定位`。不能只用正确ID把评分器或重载材料伪装为长程项；平台特殊字段映射写原因/locator，基础目标对象不变。

```json
{
  "schema_version": "package-layer-review.v1",
  "mode": "pre_run",
  "layer": "A",
  "package_digest": "<sha256>",
  "skill": {"path": "layers/autoresearch-package-layer-a-structure/SKILL.md", "sha256": "<loaded-skill-sha256>"},
  "source_basis": [{"path": "references/requirements-map.md", "sha256": "<loaded-source-sha256>", "locator": "A01–A06"}],
  "commands": ["python3 -B <suite>/scripts/check_package.py --root <package> --mode pre_run (只读；exit=0；回执=@review/check.json，sha256=<sha256>)"],
  "definition": {
    "deliverables": ["本题交付清单", "现状映射", "缺口及台账"],
    "acceptance": ["原件保留且所有必交项有真实映射"],
    "risks": ["历史摘要可能缺原日志"]
  },
  "checks": [
    {
      "id": "A03",
      "status": "证据不足",
      "clause": "requirements-map.md A03；规范§2.2",
      "evidence": [{"path": "<path>", "sha256": "<sha256-or-null>", "locator": "<line-or-JSON-pointer>"}],
      "fact": "事实及文件位置",
      "reason": "不满足原因",
      "action": "具体修改动作；只读审核不执行",
      "recheck": "重验材料"
    }
  ],
  "conclusion": "证据不足",
  "risks": ["最多3条具体风险"],
  "upstream": []
}
```

每项必须带条款出处与真实定位；无返修时`action/recheck`可为null。`skill`必须记录路由内实际读取的skill相对路径及当前SHA-256；`source_basis`记录加载过的条款/官方方法文件版本与定位；`commands`按审核合同记录实际执行的只读命令字符串、退出码及包外输出回执（可在括号中写回执hash/路径），不能把计划核验命令放成已执行事实。需授权动态命令另外登记。报告可附包外结构化command_receipts索引，但不改变此字段合同。`upstream`列依赖层的报告路径/hash/结论，不接受仅口述“已过”。报告可写入指定外部审计目录并由控制者回写台账；所谓只读指不改待检题包/源码/证据，不禁止生成自身审查报告。独立触发缺上游报告时，可只读检查本层，整体标证据不足并明确依赖，不凭空造上游通过。

## 既有正式证据JSON

`result.json`最小顶层字段来自规范2.3：

```text
schema_version, status, role, seed, task_type,
method, protocol, training, execution, metrics, quality_gate, artifacts
```

- `status`: `COMPLETE | INVALID | NOT_RUN`；已过G03的正式有效轮应有真实完成记录，不能新造NOT_RUN来掩盖缺失。
- `role`: `baseline | reference`；`seed`必须真实；`task_type`: `model_training | non_training`。
- `method`: 方法名、源码路径/SHA-256、固定依赖；`protocol`: 数据边界、训评口径、指标方向、gate、资源。
- `execution`: 实际命令、起止、退出码、耗时、资源；历史确实不可恢复者null+原因，不根据文件mtime或结果反推。
- `metrics/quality_gate`: 原始主/诊断指标、有效性与违规项；`artifacts`: 相对包根路径与哈希。
- `artifact.json`: 模型路径、格式、大小、SHA-256、role/seed、method hash、重载结论。只读审查不得加载pickle/权重或导入候选代码。
- 非训练型不建空model，模型项`NOT_APPLICABLE`；真实其他核心产物照常留路径/hash。

比较摘要的具体JSON键可由本题/平台Schema映射；语义最小项不得省略：指标名/方向、完整正式seed、逐seed B/R与配对改善、两组均值与样本std、平均绝对/相对改善、显著性规则、聚合gate、模型完整性、源result路径。冻结原字段不兼容时在新副本做可追溯映射，不覆盖源件。

`COMPLETE`仅当全部正式成对运行完成且有效、必要模型独立重载复评分齐备。缺/无效/失败原运行保留，不能静默从seed列表剔除。只读复算采用：

```text
sigma_B = sqrt(sum((B_i - mean(B))^2)/(n-1))
maximize: d_i = R_i - B_i; score(R) = (R-B)/(U-B)
minimize: d_i = B_i - R_i; score(R) = (B-R)/(B-U)
随机评估: mean(d) >= 3*sigma_B；强证据 >= 5*sigma_B
Reference归一化: 0.15 <= score(R) <= 0.8；U != B
```

计算结果只写审查报告。禁止重算并替换冻结证据、修改锚点、挑seed或追加训练；确定性评估/零标准差按官方G03及本题事前协议判断，不臆造sigma倍数或统一5%门槛。

## 长程前空值映射

1. `trajectory_*.json`若平台允许骨架，只有`{"rounds":[]}`；状态和原因放台账，不给rounds造“占位轮”。若固定Schema不接受骨架，先不创建最终文件，清单登记post_run待生成。
2. `best_method/`不存在、空目录或仅明确待生成说明均可；实际代码/入口/hash/选优分数全部尚不存在。不得复制Reference、Starter或专家实验当成已选最佳。
3. `run_summary`的真实`duration_hours/effective_rounds/typical_round_minutes`及`best_method_rerun_passed`未运行时用平台允许null；不得填10h、成功true或把计划轮数当实际。若目标Schema不接受null，暂存为非最终骨架并登记兼容缺口/映射，不私改Schema也不编数值。空数组本身不能证明长程已完成。
4. 计划时长/计划run-id/冻结配置与实际运行结果分开存。`NOT_RUN/待生成/null+原因`全局一致；0只用于真实测得的0，不能用0冒充“尚未发生”。
5. `pre_submit`只作扩展接口：须由官方QA核对QA16/18/21真实轨迹、时长/轮数、唯一best_method及干净复验；本套件pre_run通过不能转写成最终提交通过。

## 哈希与报告版本

`package_digest`与`check_package.py`的`facts.snapshot.sha256`一致：将全部普通文件按相对POSIX路径排序为`[[relative_path,size,file_sha256], ...]`，以UTF-8编码的`json.dumps(rows, ensure_ascii=False, separators=(',', ':'))`字节计算SHA-256。目录、权限、UID/GID和符号链接不混入此普通文件摘要；由独立`structure_manifest`记录并在冻结时与其自身哈希一并绑定。外部审计报告/台账不纳入包摘要以免循环；原始manifest文件按包成员照实纳入。大文件只读流式hash，不反序列化；外链不跟随到包外偷取证据。不能用目录mtime或随意挑文件替代。

每次修复新副本后产生新digest，旧报告只证明旧digest；只重审受影响项并记录未变文件/hash依据。规范源也保存路径/hash/读取时间与章节定位，source drift需重审受影响条款。冻结时间使用Asia/Shanghai或显式时区ISO8601；无法恢复真实时间填null+原因，不把本次整理时间当历史实验时间。
