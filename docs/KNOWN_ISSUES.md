# 转译问题索引与处理指引

本文件是问题入口和操作指引。每个顽固问题的现象、尝试记录、失败原因、当前方案和验证证据，均维护在对应的独立文档中。

## 问题索引

- [科学计数法与数学模式边界](issues/scientific-notation.md)
- [重复或奇数次反斜杠](issues/repeated-backslashes.md)
- [大括号与 `\\parbox` 参数错位](issues/brace-parameter-drift.md)
- [表格列数与边界错误](issues/table-boundaries.md)
- [缺失图片资源](issues/missing-assets.md)
- [模型服务不可用导致队列暂停](issues/model-service-paused.md)
- [缓存导致的重复失败](issues/cache-restart-loop.md)
- [JSON 与 TeX 反斜杠转义](issues/json-tex-escaping.md)
- [识别升级页过多](issues/recognition-escalation.md)
- [日期字段内容过长造成视觉重叠](issues/duplicated-date-field.md)

## 必须遵守的规则

1. 先查看目标任务的 `recognize.process.log`、`recognize.calls.jsonl`、`optimise.process.log` 和 XeLaTeX 日志，确认首个根因。
2. 语法修复模型只能返回局部、连续、整行替换；每次替换后必须重新做结构校验。
3. 结构校验或编译失败时回退到最近一次安全文本，不得带着错误继续优化或发布。
4. 表格必须保留空单元格和列位置；不得根据猜测补列、删列或重排整表。
5. 手写内容按纯文本安全处理；印刷体位置、表格结构和可编译性优先于手写文字准确率。
6. 重试问题 PDF 时只清除该 PDF 的优化、语法修复和编译产物，保留识别缓存、模型缓存和队列进度。
7. 发布前必须通过双遍 XeLaTeX 编译，并确认队列日志出现 `QUEUE FINISH ... status: done`。

## 复盘维护

每次新增尝试或发现新的失败证据，追加到对应的 `docs/issues/*.md`，不要把详细过程复制回本文件。若问题属于新类别，再新增独立文档并在上面的索引登记。
