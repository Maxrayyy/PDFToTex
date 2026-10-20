# 架构决策

这些 ADR 于 2026-10-20 根据可达 Git 历史、当前代码及本次用户确认整理。
历史回填记录现行取舍，不声称每项当时都经过正式 ADR 讨论；没有证据的动机不作补写。
完整检索范围、提交清单和已撤回事项见 [历史索引](history-index.md)。

| ADR | 决策 |
| --- | --- |
| 0001 | [以视觉识别和 TeX 为主链路](0001-visual-tex-pipeline.md) |
| 0002 | [代码与运行资料分别归属](0002-code-and-runtime-ownership.md) |
| 0003 | [源页证据与输出页验收分开](0003-page-evidence-and-layout.md) |
| 0004 | [允许模型改值并保留审计信息](0004-model-values-and-audit.md) |
| 0005 | [按证据恢复并限定自动重启](0005-recovery-and-retry-ownership.md) |
| 0006 | [以实际调用统计费用并限制重复整页复核](0006-model-cost-and-page-review.md) |
| 0007 | [日报以服务器完成账本为统计来源](0007-authoritative-completion-ledger.md) |
| 0008 | [Overleaf 拉取最新后仅新增](0008-add-only-overleaf.md) |
| 0009 | [Web 工作台隔离执行并复用流水线](0009-isolated-web-workbench.md) |
| 0010 | [平台化构建与监控配置](0010-platform-build-and-monitoring.md) |
| 0011 | [修复接受与版面验收分别内聚](0011-repair-and-compilation-modules.md) |
| 0012 | [后台增量汇总与可重建进度快照](0012-incremental-observations.md) |

涉及模型值变化先看 0004；涉及版面交付先看 0003 和 0011；涉及恢复、发布和费用分别看 0005、0008 和 0006–0007；涉及 Web 执行与进度看 0009 和 0012。

已替代的历史规则：流水线子模块方式由 0002 的归属替代；按批次覆盖 Overleaf 的方案由 0008 替代；本机重复扫描服务器数据的方式由 0007 替代；0011/0012 是本轮实施后的职责归属。
