# 官方 QA 对接：只复用，不替代

相对路径基于仓库根。先实际读取下列 SKILL：

- `materials/autoresearch-qa-skills/autoresearch-task-qa/SKILL.md`：21项、G01–G03、H01–H06、格式/路径/内容方法和确定性实现。
- `materials/autoresearch-qa-skills/autoresearch-baseline-quality/SKILL.md` 及 `references/baseline-review-method.md`：B01–B08；此 skill 无独立CLI。

当前总体 QA 默认 `implementation`，`precheck` 是别名，不是本套件 pre_run。官方的 `qa-spec.md`/旧 strict 不能取代当前 `implementation-policy.md` 与 `implementation-report.md`（schema 4）。来源升级需重新核对，套件不锁死平台示例字段。

只读收集命令（`PYTHON311` 为支持 tomllib 的已安装解释器路径，不安装；`OFFICIAL_QA` 为上述总体skill目录；输出在包外新目录）：

```bash
"$PYTHON311" -B "$OFFICIAL_QA/scripts/audit_task.py" "$PACKAGE" --out-dir "$REVIEW/official-collection" --policy implementation --fail-on never
```

先看真实CLI/源码；此调用只运行审查工具，不执行题包。缺支持解释器则记录证据不足，不临时安装。初始 INCOMPLETE/FAIL 是收集结果，不能直接当层结论；逐项读证据创建当前 schema 的 review 后，在另一个新输出目录运行：

```bash
"$PYTHON311" -B "$OFFICIAL_QA/scripts/audit_task.py" "$PACKAGE" --out-dir "$REVIEW/official-reviewed" --policy implementation --review "$REVIEW/official-collection/review.json" --fail-on never
```

保留原始报告、版本/hash与所有 FAIL/manual；不编辑官方代码/报告。不为符合官方final schema造轨迹。只有完整人工review才能声称完成官方QA；未运行只能称“方法复用”。正式统计可直接只读调用官方 `scripts/content_gates.py` 的 `assess_improvement`（以真实函数签名为准），输入来自全seed原始值和事前锚点；计算留审查输出，不覆盖正式summary。不复制这些算法到新检查器。

## 阶段桥接

逐项保存 `stage_bridge`，示例结构（不是官方review字段）：

```json
{
  "official_report": {"path": "official-reviewed/report.json", "sha256": "实际哈希"},
  "mode": "pre_run",
  "entries": [{
    "official_id": "QA16",
    "official_status": "fail",
    "stage_status": "合理待生成",
    "basis": "统一规范§3：未启动的真实长程时长不预填",
    "pending_ids": ["agent_effective_duration"],
    "remaining_applicable_findings": []
  }]
}
```

仅 QA16（尚未发生的真实长程时长/计数）、QA18（真实Agent最佳与其最终复验）、QA21（真实双轨）中的尚未发生部分可以阶段延期；对应字段/来源通道准备仍必达。官方整项同时含本阶段缺失时须保留该缺失，不能整项遮蔽。已有真实长跑事实不得降为待生成；QA16训练型不能套7h例外。

NOP/H06、可信评分、Hidden注入链路、模型独立重载、G01–G03/B01–B08、资源硬限、隔离与P6准备均不延期。H05有配置就查；H06当前版本NOP必须有。存在/非空不证明内容；官方脚本 EMPTY_RELOAD_LOG 只是观察，工作区允许有可追溯等价非空重载记录，须给来源后再定结论。

官方状态映射：pass→通过，fail→不通过，manual→证据不足，not_applicable→有事实理由的不适用。Baseline 总评“合理/有疑点/不合理/无法判断”分别对接 G02 pass/manual/fail/manual，保留原 B01–B08 细项。新套件不重新发明 Baseline 标准，也不凭 B/R 分差判合理。

## 已知版本差异

- `VALID_MEASUREMENT`、`inference_only` 等历史状态经真实 Schema 映射后可支持完成；未知 Schema先证据不足，不能硬判不合格。
- 缺进程元数据可 null+原因并引用等价 Trial/原始记录；不能反推补时间。
- 交付 `tests/hidden_assets` 应空；专家私有可信 bundle 并非 solver 面，不能全树同名目录强制空。
- 静态证据不认证镜像历史/物理隔离/资源硬限；普通库中的 reference 模块也不自动是泄露。
- 旧平台/旧GPU路径成功不覆盖现工作区CDI硬合同；保留历史H06真实性，另列当前准入缺口。
