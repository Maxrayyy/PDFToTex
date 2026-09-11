# 转译链路记忆

这里记录已经反复出现、后续修改提示词或代码时必须保留的约束。

## TeX 反斜杠与 JSON

- 模型输出的 JSON 字符串中可能直接出现 `\\fieldvalue`、`\\handwritten`、`\\textbf`、`\\times` 等 TeX 命令。
- 这些反斜杠如果未经 JSON 转义，会触发 `Invalid \\escape`；其中 `\\f`、`\\t` 还可能被 JSON 误读成控制字符。
- 识别适配器在 JSON 解析前保护多字母 TeX 命令，解析后再执行字段和 TeX 校验。
- 打印值中的反斜杠是原文内容时，必须按文字处理，不能自动当成 TeX 命令或擅自改写。

## 表格结构

- 每个 `tabular` 行必须保留可见单元格，包括空单元格。
- 每行只能有一个 `\\` 行结束符，并且必须位于 `\\fieldvalue{}`、`\\handwritten{}` 外部。
- 不能用换行拆开字段值，也不能为了修复列数猜测或发明数据。
- `TABLE_ALIGNMENT_MISMATCH`、`UNCLOSED_BRACE`、`COMPILE_ERROR` 出现时，优先保留原始 TeX 并升级整页视觉识别；升级结果仍不可靠时保留原稿，不能静默发布错误结构。

## 字段与页边界

- `#VALUE_ID`、`field_meta` 和页面号必须一致，例如第 71 页只能使用 `LEX-P0071-*`。
- `Compact field belongs to a different page` 表示模型把其他页字段带入当前页，必须触发重试或回退，不能重新编号掩盖问题。
- 每页必须有且只有一个 `LEXOID_PAGE_COMPLETED: page/total` 标记，顺序不能改变。

## 识别占位页

- `LEXOID_RECOGNITION_FALLBACK` 只允许作为失败诊断产物，不能通过发布校验。
- `Missing recognition content on pages [...]` 表示该页没有可信视觉识别结果，队列应暂停并保留进度，修复后从缓存继续。
- 不能为了让队列继续而删除占位标记或放宽发布校验。

## 处理顺序

1. 先查看 `recognize.process.log` 和 `recognize.calls.jsonl`，确认是模型请求失败、JSON 解析失败还是 TeX 结构失败。
2. 修复通用解析或提示约束后，先运行视觉识别回归测试，再重建 `pdftotex:local`。
3. 重建容器时保留 `/data/workers/<stem>`、模型缓存和队列文件，避免丢失已完成页。
4. 最终必须通过双遍 XeLaTeX 编译检查，才能发布优化后的 TeX。
