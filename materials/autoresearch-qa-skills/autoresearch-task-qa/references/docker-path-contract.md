# Docker 与运行路径契约

这是本项目 2026-09-18 起采用的静态交付检查。目标是避免把错误的构建上下文和绝对路径提交到平台；静态通过不代表镜像已构建或平台适配已部署。

## 依据和真实附件缺口

已只读核对[专家教学文档](https://bytedance.larkoffice.com/docx/P1J5dgIw1oKchcxn29GcvzlMnSd)所附 `example.zip` 中 `workspace/harbor_task/environment/Dockerfile`、`task.toml`、`README.md`、`tests/test.sh` 与 `solution/solve.sh`。附件带 `example/` 外包装，可忽略该包装层。Dockerfile 实际包含：

```dockerfile
ENV EVFI_TASK_ROOT=/workspace
COPY environment/requirements.txt /tmp/requirements.txt
WORKDIR /workspace
COPY instruction.md task.toml ./
COPY environment/starter ./environment/starter
COPY environment/public_assets ./environment/public_assets
COPY solution ./solution
COPY tests ./tests
```

这些来源要求 **任务根目录**作为 build context；Dockerfile 放在 `environment/` 不代表 context 也在 `environment/`。因此本项目默认采用下表的 `teaching-task-root-v1`。

附件仅作为布局证据，有三项已知缺口：修改前教学 Agent 提示写 `TASK_ROOT=/harbor_task`，附件却用 `/workspace`；附件 TOML 使用自定义 `[task]/[resources]/[entrypoint]`，没有证明原生 Harbor 识别这些字段；附件 `tests/test.sh` 只执行 pytest，没有直接写 Harbor reward。2026-09-18教学正文已统一 `/workspace`，并补充平台字段映射与正式reward要求，附件本身未替换。不能把附件称为已经通过完整 Harbor 接口或平台构建验证的模板。

## 两种受支持 profile

下表路径均不包含 ZIP 外包装；`T` 是 `task.toml` 所在任务根。教学完整包推荐 `T=workspace/harbor_task`。直接送审独立任务目录时，`T=.`，目录职责与完整包结构另由格式检查判定。

| 项目 | `teaching-task-root-v1`（本项目默认） | `harbor-environment-v1`（明确采用原生 provider 时） |
|---|---|---|
| Dockerfile | `T/environment/Dockerfile` | `T/environment/Dockerfile`，或已声明预构建镜像 |
| build context | `T` | `T/environment` |
| COPY 来源 | 相对 `T`，例如 `environment/starter` | 相对 `T/environment`，例如 `starter` |
| 工作目录与任务根 | 最终 `WORKDIR /workspace`；声明的 `TASK_ROOT`/`*_TASK_ROOT` 与其一致 | 由实际镜像/版本明确指定，不能假设 `/workspace` |
| Agent 提交面 | `/workspace/solution` | 依据任务接口；原生 Oracle 的 `/solution` 不能直接当 Agent 可写工作区 |
| 测试入口 | `/workspace/tests/test.sh` | Harness 注入 `/tests/test.sh`；启动 cwd 不保证是 `/tests` |
| 源包入口 | `T/tests/test.sh`、`T/solution/solve.sh`；其他配置入口须真实存在 | `T/tests/test.sh`；`solution/solve.sh` 作为 Oracle 按需存在 |

教学 profile 的等价构建命令（只用于描述，不由静态 QA 执行）：

```bash
docker build -f workspace/harbor_task/environment/Dockerfile workspace/harbor_task
```

禁止把 context 改成整个提交包后再 COPY Reference、`expert_evidence/` 或 `optimization_evidence/`。禁止依赖专家本机绝对路径或 `../` 越出 context。同一 profile 下，构建命令、实际 provider、COPY 来源、WORKDIR、环境变量和入口必须形成同一条路径链。

原生 profile 的基准来自既有 [Harbor 规范](harbor-harness.md) 所列文档与 provider 源码；复核时仍须说明目标版本。不能仅为规避教学路径错误改选原生 profile，再继续使用教学 context 的 COPY。若平台使用教学布局，H02/H05 仍应检查自定义字段和入口映射；没有动态运行材料只写“未验证运行”，不据此判构建必然失败。

## 自动检查与人工边界

`scripts/docker_paths.py` 只读处理常见 Docker shell/JSON COPY 与 ADD、默认反斜线续行、WORKDIR、TASK_ROOT 环境变量、CMD/ENTRYPOINT 的显式脚本路径、简单 dockerignore，以及 TOML 中静态入口。检查规定位置、context 内来源存在性、目录/文件复制映射、越界链接、入口文件及运行时位置。Dockerfile 相邻的 `Dockerfile.dockerignore` 优先于 context 的 `.dockerignore`。

确定缺 Dockerfile、混用 context、来源不存在、显式 WORKDIR/TASK_ROOT 错误、简单 ignore 排除必需来源、入口不存在、静态无法映射且没有生成步骤等，标 `fail`。H03 自动不通过，不能用手填 pass 或 `manual_resolution` 覆盖。

变量、远程 ADD、ADD 解包、多阶段及 COPY --from、路径相关扩展 flags、复杂 dockerignore、内嵌 shell、RUN 生成文件等不能完整解析时标 `manual`。不把阶段内来源当成本地文件，也不将“不支持解析”写成“平台一定构建失败”。须阅读调用链后补充真实证据。检查器不模拟镜像基础文件系统，不证明构建可达性、依赖安装成功、权限有效或任意 shell 命令正确；这些仍是 H03 的人工语义复核范围。

## review 与输出字段

最终 `review.harbor.path_contract` 必须显式填写 profile 和来源，旧 review 不能默认冒充新契约已通过：

```json
{
  "profile": "teaching-task-root-v1",
  "profile_basis": "2026-09-18 教学 example.zip 的 Dockerfile、task.toml 与入口；采用教学任务根构建契约"
}
```

可选 `dockerfile`、`build_context`、`runtime_task_root` 用来显式声明路径，前两种 profile 必须与计算结果一致（原生 profile 的 runtime task root 可按真实镜像声明）。`adapter_evidence` 是真实包内适配文件引用数组，不能只填口头声明。

仅对已标 manual 的动态解析项，可填写：

```json
{
  "manual_resolution": {
    "summary": "说明每个未解析项的最终路径和解析依据，最多 500 字。",
    "evidence": ["workspace/harbor_task/environment/Dockerfile:12", "workspace/harbor_task/build.sh:4"]
  }
}
```

字段是合并到同一个 `path_contract` 对象，并非另建 review。所有引用必须存在于送检包内。可采用 `custom` profile，但需填写三项路径和适配证据；它用于已有平台适配，不是任意忽略教学路径约束的豁免。无充分适配说明维持 manual。

`report.json.harbor.path_contract` 保存 `profile/profile_basis/sources/task_root/dockerfile/build_context/runtime_task_root/test_entry/observed_workdir/copy_operations/findings/status`，以及给出的适配证据和人工复核说明。每条 finding 含 `code/status/message/evidence`。原生预构建镜像无 Dockerfile 时可 `not_applicable`，镜像内部路径仍须由 H03 复核。任何路径 fail 都强制 H03 与 QA17 不通过；manual 无补充复核时 H03 不能通过。所有 profile 均不允许由静态结果声称动态构建成功。

退回建议示例：

> Dockerfile 位于 `workspace/harbor_task/environment/`，但复制来源按任务根编写；当前构建 context 使用 `environment/`，导致 `environment/requirements.txt` 与 `tests/` 无法找到。请采用教学 profile 的任务根 context，统一容器根 `/workspace`，逐项检查 COPY 和测试入口，并提交对应构建命令/日志。验收依据是规定位置存在、所有来源可解析、入口与容器路径一致；运行成功仍以实际构建和评分日志为准。
