# 科学评分完成门

本组件把研究 run 的“工作进程退出”和“科学结果已可信收口”分开。`gpu_scheduler` 的 `SUCCEEDED` 只表示该评分子任务的进程正常退出；它不能直接把研究 run 标为 `COMPLETED`。完成门运行在 controller/verifier 侧，候选代码不能写入 receipt、可信 result 或 verifier reward。

## 合同

每个新配置必须声明以下八个字段：

```json
{
  "stage": "formal",
  "score_expectation": "required",
  "metric": "accuracy",
  "direction": "max",
  "required_seeds": ["0", "1", "2"],
  "deadline": "2026-10-05T00:00:00Z",
  "candidate_manifest": "candidate.manifest.json",
  "protocol_hash": "<sha256>"
}
```

`stage` 只能是 `diagnostic`、`screen`、`formal` 或 `final`。只有 `diagnostic` 能显式使用 `score_expectation: not_expected`；其余阶段必须是 `required`。必需评分阶段还要提供 `completion`：绝对的私有 `evidence_root`、owner-only `signing_key`、协议 manifest、数据和 evaluator 摘要、job ledger、结果文件、receipt、isolation attestation、`training` 和私有 roots。

`candidate_manifest` 是私有 evidence 中的候选清单，源码 descriptor 默认相对于 candidate root；模型和 checkpoint 也可显式声明 `origin: evidence`，用于可信训练输出。协议 manifest 采用 `{version: 1, protocol: [...], data: [...], evaluator: [...]}`，每个 artifact 是 `{path, size, sha256}`；`protocol_hash` 是 manifest 文件 SHA-256，data/evaluator hash 是对应数组的 canonical digest。

首次 `start` 会把实际有效硬截止写入 `completion.contract.json`。此冻结合同、候选、协议、数据、evaluator 和 controller release 都会绑定到结果；之后修改任一绑定会进入 `PROTOCOL_BINDING_MISMATCH` 或 `CANDIDATE_BINDING_MISMATCH`。

原 run 经明确授权通过官方 `amend` 延期后，可信 `adopt_evaluation` 可选用截止早于或等于当前父合同截止的已完整认证评分；科学字段和可信边界仍须一致，晚于父合同的评分截止拒绝。原评分合同、receipt、request/job/origin 与原截止不改写，选用后的隔离证据记录来源合同及 receipt 哈希。这不授权延期，也不使迟到或未完成评分有效。

`CompletionContractError(ControllerError)` 明确标识确定性的收尾合同冲突，`status` 固定为 `COMPLETION_CONTRACT_ERROR`，`code` 区分科学协议、可信边界、来源截止越界和不可覆盖的目标证据。重新调用研究模型不能解决这些冲突；控制器应保留已完成研究记账并停止自动重试。未认证来源、签名、hash、seed、reload 或评分状态失败仍使用 `EvidenceError` 和原科学失败状态，不会被改写为合同错误。

## 状态和凭证

正常流为 `RUNNING -> FINALIZING -> COMPLETED`。完成门失败会写明确终态：`INCOMPLETE_FINAL_SCORE`、`EVALUATION_PENDING`、`EVALUATION_FAILED`、`EVALUATION_UNKNOWN`、`FINAL_SCORE_INVALID`、`CANDIDATE_BINDING_MISMATCH`、`PROTOCOL_BINDING_MISMATCH`、`COMPLETION_RECEIPT_MISSING` 或 `EXPIRED`。只有 receipt、job、seed、reload、hash 和隔离检查全部通过才会进入 `COMPLETED`。

可信侧通过临时文件、flush/fsync、原子 rename 和目录 fsync 写出 result、reward 和 `completion.receipt.json`。正式 receipt 至少包含 run/task/stage、状态、metric/direction/科学分数、原始 request/job ID 及终态、seed 覆盖、候选模型/checkpoint/source hash、协议/数据/evaluator hash、结果文件的大小和 SHA-256、生成时间和原始 deadline、独立 reload 证据。receipt 只发布必要标量和不含秘密的 hash，不复制 verifier 原始日志、隐藏路径、Reference 或密钥。

训练型 formal/final 结果必须有每个 seed 的独立进程 reload。零分和负分是合法有限数值；不能用真假判断分数，也不能用 `null` 代替阶段语义。正式 result 和 receipt 使用 HMAC-SHA256，由 controller 私钥签名；私钥、evidence、scheduler root、Hidden/Reference 必须在候选 root 外且 owner-only。

## 评分等待与迟到

完成门发现评分未终态时，只查询已登记的原 `request_id`/`job_id`，刷新 controller heartbeat，不重复提交。等待遵守首次启动时的硬截止；排队不能延长 run。截止到达后写 `INCOMPLETE_FINAL_SCORE`/`EXPIRED` 观察，迟到的有效评分仅写入不可覆盖的 `completion.late/*.json` 归档，不能静默把过期 run 提升为成功。已有有效 receipt 在截止后重复收口仍返回原件，保证崩溃恢复幂等。

## Harbor 和隔离

Harbor 的 config、result、优先 reward 文件及非空 `trial.log`/`verifier/test-stdout.txt` 必须来自同一 Trial；若 `reward.txt` 和 `reward.json` 并存，合同必须声明实际 `reward_priority`。候选容器不挂载 verifier 日志目录、私有 evidence、Reference、key、scheduler socket 或 Docker socket，不得拥有特权、host namespace、危险 capability 或私钥环境变量。可信 verifier 才能发布既定 reward 合同允许的标量。

## 接入 API 和审计

可信评分适配器使用 `tools.research_handoff.experiment_batch.complete_batch()`：它只写可信 seed result，并在父 job 尚未终态时返回 `EVALUATION_PENDING`；controller 后续完成最终 receipt。诊断阶段由 controller 写出不含科学分数的诊断 receipt。

控制器侧提供只读审计入口：

```bash
python3 -B -m tools.research_completion audit --run-dir <path>
python3 -B -m tools.research_completion audit --run-dir <root> --recursive
python3 -B -m tools.research_completion validate --contract <contract.json> --receipt <completion.receipt.json>
```

审计不会补分、重提 job 或覆盖原证据。它会报告缺少最终分数、排队/未知 job、`SUCCEEDED` 无结果、候选/协议/checkpoint hash 不匹配、缺 seed/reload、正式阶段的 `scientific_score: null`、无有效 receipt 的 `COMPLETED`、迟到证据和 Harbor Trial 不一致；正式 run 发现问题时返回非零，显式无分 diagnostic 通过。

## 迁移和发布

旧历史目录先用 `audit --recursive` 做 shadow audit；缺少显式合同的历史 run 标记为无法认证，不推断其科学分数。新 bundle 包含本文件和 `research_completion.py`，但不包含题目适配器、模型、密钥或隐藏 evidence。新正式任务必须使用完整合同，不能继续依赖部分 `batch_summary.json` 作为完成证明。
