# g03-conductor pi 扩展

[tools/g03_conductor](../../tools/g03_conductor/README.md) 的 TS 薄壳。这里不实现业务逻辑：每个动作都调用 `python3 -I -B tools/g03_conductor <cmd> --root $G03_ROOT`，再解析返回的 JSON。

## 启动

```text
cd '<私有区>' && G03_ROOT='<私有区>' pi --model anthropic/claude-opus-5-5 --thinking high --no-extensions -e '<subagent绝对路径>' -e '<工具发布根>/extensions/g03-conductor' --session-dir '<私有区>/g03/pi-sessions' --append-system-prompt '<私有区>/ops/g03_handoff/main.md' '先读取ops/g03_handoff/handoff.md，调用g03_status并只读核对交接；一致则结束首轮交由扩展推进，差异则通过官方CLI暂停并报告。'
```

- 必须用交互模式才能看到小组件，建议在 tmux 中运行。没设 `G03_ROOT` 时扩展只注册一个提示用的 `/g03`，其他功能不生效。
- `G03_PYTHON` 可以覆盖解释器，默认 `python3`。
- `g03-worker` 由主会话通过官方 subagent 扩展调起；关卡 reviewer 由扩展通过 `review-run` 直接拉起独立 pi 子进程（`--no-extensions -e guard`，角色为 reviewer），主会话无法跳过，不能重复派 reviewer 代替强制关卡。
- 初始化 Agent 按 [G03 初始化与主 pi 交接规范](../../notes/g03_conductor交接规范.md) 事先执行 init/propose/status/verify，停在 S0 并填写交接与主提示模板，再交付一行实际命令。session_start 只刷新状态；命令中首条消息启动接管轮，agent_settled 后 next 才推进。因此用户不需要再输入提示，裸 pi 命令仍会等用户输入。
- worker 子进程不自动继承主会话通过 -e 加载的 G03 工具。worker 实现/核查代码后交回，所有 G03 实验由主会话 g03_run，不让 worker 直接提交 scheduler 或假定其拥有 g03_run。

## 提供的能力

| 能力 | 行为 |
|---|---|
| 工具 `g03_status` | 只读状态 |
| 工具 `g03_propose` | 协议草案、候选目录、缺陷或不可行声明 |
| 工具 `g03_run` | 唯一实验入口：diag/dev/screen。正式运行由驱动自动执行 |
| 工具 `g03_note` | ASI、负结果、诊断说明 |
| `tool_call` 门控 | 当前阶段不允许的 conductor 工具会被拦截；对 conductor 专属路径和锁定路径的 `write`/`edit` 会被拦截，候选目录可写；改写 ledger/lock/control 的 bash 会被拦截。guard 读取失败时拒绝写入（fail closed） |
| `agent_settled` 驱动 | 调用 `next` 并执行返回的动作：`continue` 通过 `sendUserMessage` 下发指令；`review` 和 `auto_run` 直接执行；`terminal` 时通知并调用 `ctx.shutdown()`；`paused`/`blocked`/`wait_expert` 时停止驱动，等专家处理。按 Esc 中止后不再自动驱动 |
| 小组件 | 显示阶段与停留时长、窗口数轴、预算 k/K 与修订 x/1、最近事件 |
| `/g03` | 只读全屏视图，包含时间线和候选表。`/g03 pause\|resume\|stop [原因]` 写 `control.json`，在下一关卡生效。`/g03 next` 在空闲时手动驱动一步 |

`guard/index.ts` 也可以单独加载：设置 `G03_ROOT` 和 `G03_ROLE=reviewer|worker` 后，对 reviewer 只允许只读操作（不能 write/edit，mutating bash 按启发式拦截，python 必须带 `-I`）。bash 拦截只是尽力而为，锁定文件的完整性最终由 Python 的哈希校验保证。

## 验证

```text
extensions/g03-conductor/test/run.sh
```

`test/harness.mjs` 用 pi 自带的 jiti 和 `VIRTUAL_MODULES` 加载扩展，注入 mock ExtensionAPI，以脚本模拟主会话，跑完 DONE（good 方法）和 STOP_UNMET（overfit 方法，K 用完）两条路径。过程中断言：没有阶段转移工具；越阶段调用 `g03_run` 被拦截；对 ledger 和锁定评分器的写入被拦截；候选目录可写；终态时调用了 `shutdown`。整个过程不调用 LLM，也不使用 GPU。

改动扩展后，再做一次真实加载检查（RPC 模式，不调用模型）：

```text
printf '%s\n' '{"id":"1","type":"get_commands"}' | G03_ROOT=<空转私有区> PI_OFFLINE=1 \
  timeout 20 pi --mode rpc --no-session --no-extensions -e extensions/g03-conductor
```

输出中应包含 `g03` 命令和 `setWidget` 请求。
