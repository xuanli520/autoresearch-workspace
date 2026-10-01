# AutoResearch 资料索引与本轮整理摘要

整理日期：2026-09-30

本次新增的三个根目录压缩包均保留在工作区根目录，未覆盖或删除原件。资料按“通用教程 / 目录格式 / PCA 教学示例”分开归位；同内容附件使用硬链接复用，文档中的相对链接仍然有效。

## 资料归位

| 资料 | 工作区位置 | 用途与状态 |
|---|---|---|
| PCA 双镜像对齐版专家教程 | [AutoResearch 专家线下标注教程｜PCA 双镜像对齐版.md](<AutoResearch 专家线下标注教程/AutoResearch 专家线下标注教程｜PCA 双镜像对齐版.md>) 及其 `图片和附件/` | 2026-09-29 的 PCA 专项补充教程；保留原教程作为通用主依据，不替换它。 |
| PCA 教学题包 | [example/pca_teaching_example/](example/pca_teaching_example/) | 从 `population-genetics-pca_teaching-format_20260929.zip` 展开的完整教学示例，包含双镜像 workspace、公开 Dev、私有 Verifier、参考实现及现有静态验证记录。 |
| 规范格式 | [规范格式/规范格式.md](规范格式/规范格式.md) 及其 `图片和附件/example.zip` | 只规定最终目录职责、可见性和 `optimization_evidence` 证据格式；附件示例与现有教程示例内容相同。 |

根目录原始输入仍为：

- `AutoResearch 专家线下标注教程｜PCA 双镜像对齐版.zip`
- `population-genetics-pca_teaching-format_20260929.zip`
- `规范格式.zip`

## 内容要点

### PCA 双镜像教程

- 任务包采用 `workspace/harbor_task/`、`workspace/reference/`、`expert_evidence/`、`optimization_evidence/` 三类交付边界。
- Agent 镜像以 `workspace/harbor_task/environment/` 为独立构建上下文；Verifier 镜像以 `workspace/harbor_task/tests/` 为独立构建上下文，禁止用 `COPY ..` 跨上下文取私有材料。
- 候选提交面统一为容器内 `/workspace/solution`，由 Harbor artifacts 交给独立 Verifier；正式入口为 `/tests/test.sh`，结果写入 `/logs/verifier/reward.txt` 或数值 `reward.json`。
- 题面仍要求真实方法空间、合理 Baseline、完整成对证据和两条独立 Agent 轨迹；固定只搜索少量权重、学习率或轮数不满足 G01。
- 附录以群体遗传学 PCA 为例，说明公开 Starter / Dev 与私有 Reference / Hidden 的边界，以及双镜像构建和候选移交流程。

### PCA 教学题包

- 研究对象是纯 Python + NumPy/SciPy 的群体遗传学 PCA：从 VCF 解析合格双等位 SNV，按 HWE 标准化，输出样本主成分子空间；优化重点是解析、数值、内存和 I/O 算法，而非单纯调参。
- `workspace/harbor_task/environment/` 提供可见 Starter、公开小型 Dev 数据和公开速度/子空间代理；`workspace/harbor_task/tests/` 保留独立私有评分、数据生成和安全执行；`workspace/reference/` 仅供专家侧使用。
- 包内明确标注为教学变体，保留源任务的 native reward 及其 `[0,1]` 分段/截断语义，不等同于通用教程要求的 B/U 归一化、Hard Gate 分类或 Reference `[0.15,0.8]` 门槛。
- 目前没有正式 Baseline/Reference 成对 seed 结果、两条长程 Agent 轨迹或真实 Harbor/Hidden 实跑；`NOT_RUN`、空轨迹和 `null` 分数是待补证状态，不是通过结论。
- 现有验证记录只覆盖静态检查和轻量 smoke；Docker 构建、完整 Harbor 往返、Hidden、Linux 隔离、长时稳定性和正式质量门仍需在目标平台另行验收。

### 规范格式

- 最终包职责固定为 `workspace/`、`expert_evidence/`、`optimization_evidence/`，并强调 Agent 可见性、Reference/Hidden 隔离和证据相对路径。
- `optimization_evidence/` 的正式 seed 数量按协议声明，不强行固定为三组；Baseline 与 Reference 必须使用相同 seed 集合、同一协议并逐 seed 配对。
- 训练型任务要保存每个 seed 的真实模型、哈希和独立重载日志；非训练型任务不创建空 `model/`。
- 本文是格式规范，不会自动证明任何题包已经完成动态构建或平台验收。

## 去重与完整性核对

- 三个 ZIP 均通过 ZIP CRC 检查；未发现绝对路径、`..` 路径、反斜杠路径或符号链接成员。
- PCA 题包在教程压缩包内附带两份同 SHA-256 的副本；工作区保留两个原文附件名，并与根目录独立题包复用同一内容。
- `规范格式.zip` 内的 `图片和附件/example.zip` 与现有 `materials/AutoResearch 专家线下标注教程/图片和附件/example.zip` SHA-256 相同；新目录通过硬链接复用，不重复复制 83 MB 示例。
- 原始压缩包 SHA-256：
  - `AutoResearch 专家线下标注教程｜PCA 双镜像对齐版.zip`: `846533ace5398a48833e08a95efb99d48100bb34c8e6ae859ad4f409fdad8b30`
  - `population-genetics-pca_teaching-format_20260929.zip`: `a4ec672dbb3bcd9d52b49c3575e1e80f559139363f7450e6e83b890342e044f7`
  - `规范格式.zip`: `037c31ea991861090d0b260e3686579516fdc45f5fccf6f3e69d19f4379da23a`

## 使用口径

当前工作区的 [AGENTS.md](../AGENTS.md) 和总流程指引优先于这些历史/教学材料。尤其要注意：PCA 教程中的 `schema_version = "1.3"` 只是该示例的配置，不是任意 Harbor 版本的通用保证；教程中关于容器稳定性、时长和评分的说明也不能替代当前题目授权、真实运行记录及平台合同。上述归位和摘要是静态资料整理，不代表已启动训练、Docker、Harbor 或 Hidden 评分。
