# 以实际调用统计费用并限制重复整页复核

状态：accepted；历史回填于 2026-10-20。

调用日志以调用标识关联阶段、源页、请求模型与用量，缓存复用不虚构调用。供应商未返回的用量和未知价格保持未知；重试产生的真实消耗计入统计。按页调差是单独计费规则，不替代实际调用事实。

同页的整页复核合并处理，持久预算约束重复请求；普通裁剪复核和整页复核分别管理。这样避免多个异常字段为同一页反复付费，但必须保留明确的页级预算与失败状态。

证据：主仓库 `a4c19e1`、`11ec6ac`；`docs/issues/reconcile-full-page-cost.md`、`core/model_telemetry.py`、`optimization/reconcile_budget.py`、`monitoring/daily_costs.py`。`ff1689d` 的四容器小时统计已被 `a7e02b4` 撤回，不能据此恢复独立统计入口。
