# 允许模型改值并保留审计信息

状态：accepted；2026-10-20 用户再次确认保持当前实现。

成功的模型语法修复允许修改字段值和相关标记；`repair_invariant_violations` 的结果作为 `audit_flags` 记录，不因字段内容变化本身拒绝有效候选。缓存保存候选，候选仍须经过结构及重试政策后才能交给优化流程。

这个取舍允许纠正识别内容，接受了模型可能改错业务值的风险；审计标记不能证明新值正确。本轮明确不新增“内容变化阻断”或强制人工审核流程。原设计文档中“违反任一不变量即 blocked”的概括不符合当前实现，已修正。

有一个需要保留的现行例外：当修复有失败批次而希望降级继续时，当前源文必须同时通过结构和完整性检查；否则仍停止。这个例外并不把成功模型输出的字段变化改回硬拦截。

证据：主仓库 `51ae061` 导入的 `lexiod-pipeline/texopt/syntax_repair.py` 已包含 authoritative / audit-only 政策；`d2bacbc` 另行放宽协调阶段的识别值差异；`2254403` 将接受政策收拢。当前 `syntax_repair.py`、`repair_transaction.py`，以及 `test_syntax_repair.py`、`test_repair_transaction.py` 同时覆盖成功改值和失败批次保护。不能将协调阶段的不确定值保留规则误套到所有语法修复输出。
