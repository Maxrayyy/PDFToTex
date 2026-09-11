# 按页模型升级流水线

在部署环境配置：

```dotenv
LEXOID_MODEL=gpt-5.6-sol
SOL_VISION_REASONING_EFFORT=none
VISION_FALLBACK_MODEL=gpt-6-astra
VISION_CONCURRENCY=2
RENDER_DPI=240
```

`BatchConfig` 读取上述环境。配置升级模型且与主模型不同时，识别阶段使用
`python -m texopt.page_fallback`，否则保持原来的 `lexoid latex` 入口。
清空 `VISION_FALLBACK_MODEL` 可关闭检查和按页升级。

Sol 视觉识别默认显式发送 `reasoning_effort=none`，可通过
`SOL_VISION_REASONING_EFFORT` 调整，设为空字符串时恢复服务端默认值。
该设置不改变 GPT-6 升级识别及后续协调、命名模型的推理参数。
调用日志和识别证据记录实际请求的推理参数；识别草稿缓存按推理模式区分，
避免把旧模式结果当作 `none` 的输出。已创建容器需要重新创建才会使用新代码。

## 执行方式

1. 主模型识别页；识别缓存仍保存主模型的原始 TEX 和字段证据。
2. 独立检查队列按源页序检查已返回的页，与后续页的视觉识别重叠执行。
3. 先执行共享的本地 TEX 规范化及已知依赖补齐，再检查结构并执行必要的单页 XeLaTeX 编译。
4. 发现明确错误后，重新读取该页原始图片，调用升级模型一次。
5. 升级结果通过检查后替换该页 TEX 和字段证据，其余页保持原结果。写入仍按源页顺序进行。
6. 完成全部页后，继续字段协调、布局优化、最终两遍编译和发布；语义命名默认延后。

本地规则与最终优化器共用 `local_tex.py`，覆盖已知宏及包依赖、控制序列边界、
文本模式数学符号、数学空行、合并单元格换行，以及可确定的 minipage 横线和行结束符。
不猜测删除括号或重排表格。升级候选也执行相同规则；未知的明确结构错误仍保留升级机会。
每页原始、规范化及实际检查 TEX 保存在 `page-fallback/local/<hash>/`，
`local-repair.json` 记录版本、哈希及规则；原识别缓存及升级尝试身份保留。

检查队列只有一个工作线程；主模型和升级模型共用同一个并发控制器，
不会把配置为 2 的单容器模型并发叠加为 4。队列积压会对识别提交施加背压。
启用升级时主模型只调用一次，把第二次机会留给升级模型，不再先重试一次 Sol。
各容器仍独立计算并发额度，两个容器各设 2 时总模型请求最多为 4。

## 触发范围

- 明确的 TEX 结构错误，如多余单元格、括号或环境不闭合。
- 多行字段没有正确封装，连续短行把字段挤入第一列的特定模式。
- 单页实际编译失败。
- 主模型未返回可用 TEX，只剩识别降级占位内容。

普通短行警告、`visual_table_row_mismatch`、手写/印刷值不确定、仅 JSON 元数据损坏，
均不单独触发升级。PDF 页数增加、字体样式不同也不作为升级条件。
编译工具不可用或本地检查超时只记录检查警告，不作为模型识别错误花费 token。

这是结构与编译检查，不是图像逐像素比对。能编译且列结构合法的内容误识别、
划销线遗漏、签名辨识和纯视觉错位仍可能漏检，必须保留人工核对。
共享导言区会影响单页编译；后续页面依赖前页正文中定义的宏时，可能触发保守升级。

## 缓存与失败处理

本地检查以 TEX、共享导言区和检查器版本的哈希缓存，缓存命中不重复编译。
升级缓存键包括源 PDF 内容、源页码/总页数、原 TEX、模型、DPI、方向设置和提示词版本。
升级请求发出前先记录尝试；成功、失败和被中断的尝试均不会在续跑时自动再次调用模型。
如需主动重新尝试，需明确清除对应升级尝试缓存，或使用新的独立测试缓存目录。

原 TEX 与升级 TEX 分别保存在：

```text
<cache-dir>/page-fallback/attempts/<hash>/primary.tex
<cache-dir>/page-fallback/attempts/<hash>/fallback.tex
<cache-dir>/page-fallback/attempts/<hash>/attempt.json
```

升级仍失败时，保留原 TEX 并记录 `unresolved`，继续后续处理，不无限重试。
后续优化可能修复部分问题，但此策略不能保证任意损坏 TEX 最终都能编译。

最终识别证据保留 `page_models`，逐页说明实际采用哪个模型。
`<output>.page-checks.json` 记录检查页数、成功替换页和未解决页。

## 日志与监控

识别阶段原有 `*.process.log` / `*.process.calls.jsonl` 增加：

- `page_check`：本地检查耗时、页码、错误代码。
- `page_local_repair`：本地应用规则及审计路径；汇总 `local_repaired` 表示规则有改动，不等于所有问题已修复。
- `page_model_upgrade`：切换前后模型与触发原因。
- `page_upgrade_finish`：采用升级 TEX 或保留原 TEX。
- `page_checks_finish`：全文件汇总。

模型调用仍记录服务端 token usage、耗时与 attempt；升级调用记为 attempt 2。
`worker_watch.py` 显示检查页数、已替换页和未解决页；仅查看日志不会调用模型。

## 验证样本

2026-09-07 使用 S22C-726080515020 的原物理第 1–3 页 Sol 输出回放：
本地检查耗时约 1.26 秒，发现第 1 页 `COMPILE_ERROR`、第 3 页 `SPLIT_FIELD_ROW`，
第 2 页保留原 Sol 输出。第 1、3 页各调用一次 GPT-6 后通过检查，
后续协调、优化与两遍编译通过。
