# Lexoid LaTeX 可定位优化规格

当前 PDFToTex 部署的方案、Docker 启动、数据位置、队列续跑及监控操作见
[项目运行指南](https://github.com/Maxrayyy/PDFToTex#readme)。本文件侧重优化器规格与命令，末尾的 cron 目录模式属于独立部署方式。

## 1. 目标

建立一条可验证的 `lexoid → texopt → XeLaTeX → PDF` 链路，使每个可编辑字段同时具备：

1. 稳定、可重复生成的字段 ID；
2. 源码行到 PDF 的 SyncTeX 正向定位；
3. PDF 坐标到源码行的 SyncTeX 反向定位；
4. 优化前后的文本、页数和表格几何布局回归验证。

本工具不使用 OCR 重新识别原 PDF，不修改字段值，不尝试“美化”版式。

生产流水线还执行三项硬性门禁：清除分页/续跑产生的中途
`\end{document}`（完整文档只重建一个最终结束符）、把所有 `tabularx`
环境转换为 `tabular` 并移除不再使用的包声明，以及对最终导出文件执行两遍
XeLaTeX 编译。任一门禁失败都不会写入完成标记。

对于无法测得的弹性列，探针会先确保 `\multicolumn` 覆盖的列也被实际测量。
若仍因 LaTeX 结构错误阻塞，生产默认参数 `--llm-repair-on-failure` 会让模型做
一次最小语法修复，再重新进行结构检查、XeLaTeX 测宽和转换；模型不负责猜测
列宽。最终编译门禁可单独通过 `--compile-check` 启用。

字段协调 `texopt reconcile` 默认只对日期、批号、数量等格式异常调用模型。
印刷体或手写文字不确定、识别结果存在分歧和勾选不清楚均保留首次识别值，交给人工核对；
JSON 保留 `needs_review`、`review_status: deferred` 和 `review_reasons`，日志报告
自动复核数 `selected` 与人工核对数 `deferred`。需要全面内容复核时可显式传入
`--review-content`。此策略不关闭表格结构检查、语法修复或最终 XeLaTeX 编译。

流水线镜像定义保存在本仓库的 `pipeline/docker/Dockerfile.hybrid`，构建上下文为同时包含
`Lexoid/` 和 `pipeline/` 的父目录。基础镜像为现有的 `pdftotex-runtime:local`；
删除线所需的 CTAN `ulem.sty` 通过固定 SHA-256 校验值安装。运行镜像构建命令：

```sh
docker build -f pipeline/docker/Dockerfile.hybrid --target runtime -t pdftotex:local .
```

### 可选语义命名与 Kimi K3

默认 `TEXOPT_SEMANTIC_NAMING=deferred`：生成 TEX/PDF 时不调用命名模型，仍保留
稳定字段 ID、原标签和值、源页码、源码位置及可用的复核历史。基础 JSON 中
`name_status=pending` 表示尚未补充语义名称；单项提取损坏时保留原始片段和
`extraction_status=invalid`，不丢弃其他字段，也不阻断 PDF。
此设置独立于 `--llm-repair-on-failure`，必要的 GPT 语法修复仍然启用。

优化器在输出 TEX 旁写入 `<输出名>.naming.json` 命名计划。后续只补充 JSON：

```sh
texopt name-fields document.optimized.tex \
  --registry document.fields.json --output document.enriched.json \
  --model deepseek-v4-flash --name-cache /data/.cache/semantic-names.sqlite3
```

PDF→TeX 流水线的工作 TEX、命名计划和字段 JSON 位于 worker 的 `.pipeline/<文件名>/`；
正式发布的 TEX 是工作 TEX 的原样副本，可通过 `--plan` 显式指定工作目录中的计划。
命名命令验证 TEX 哈希与计划、JSON 一致，按稳定 ID 更新别名，不改值、不改 TEX，
也不重新编译。命名失败保留原有数据和待补充状态，成功与缓存命中记为 `complete`。
`complete` 仅表示名称已生成，不代表业务数据已人工确认。

命名使用独立环境变量，凭据只放本地忽略的 `.env`：

```dotenv
TEXOPT_MODEL=deepseek-v4-flash
TEXOPT_NAMING_PROVIDER=openai
TEXOPT_NAMING_BASE_URL=https://your-provider/compatible-mode/v1
TEXOPT_NAMING_API_KEY=your-naming-key
TEXOPT_NAME_CACHE=/data/.cache/semantic-names.sqlite3
```

不改变视觉识别和语法修复的 `OPENAI_*` 配置。显式指定独立命名地址但没有命名密钥时，
不会向该地址发送视觉服务的密钥。仍可用 `--semantic-naming inline` 在优化时命名。
所有 worker 共享同一 SQLite 文件以复用表单结构映射，并合并同时发生的相同请求；
网络请求期间不占用数据库写锁。部署要求本机持久磁盘，不能将 WAL 数据库放到不支持
SQLite 锁的网络共享目录。旧 JSON 缓存保留，后续使用同名 `.sqlite3`。

缓存排除页码、表序号和被字段宏包裹的填写值，保留标签、单位、结构和上下文，
无标签字段保留必要的值以消歧。模型、服务地址及提示词版本不同不能共用结果。
仅延后命名会缩短 PDF 等待；按需调用和结构缓存命中才会降低生命周期总 token。
请求仍通过 `LEXOID_MODEL_CALL_LOG` 记录服务端 usage、耗时和重试，缓存命中不调用模型。
Kimi 价格未配置时费用保持未知，不能按 GPT 价格计费；独立补充的调用日志需单独归档。

### 本地容器监测

以下命令均从 `PDFToTex/` 根目录执行。

`worker_watch.py` 仅使用 Python 标准库和本机 Docker CLI，通过 macOS `launchd`
定时读取容器状态、流水线状态库和增量日志。默认不调用模型、不重启识别任务；
显式启用 `auto_restart.enabled` 后，可恢复符合条件的网络暂停容器，恢复后转换会继续调用模型。
对已知发布目录配置错误，可显式给目标设置 `recover_publish_from`。
仅在容器正常退出、manifest 标记完成，且指定文件与优化工作文件、完成记录的
SHA-256 三者一致时，将该文件原样归位到 `output_tex`，不覆盖已有目标。
配置包含 `launchd_label`、`interval_seconds`、`docker`、`output_dir`，以及
`containers` 数组；每个目标指定 `name`、`work_root`、`stem`、`pages` 和 `output_tex`。
路径均使用绝对路径，默认间隔为 600 秒，当前部署为 360 秒。安装后立即探测一次，
所有目标均终止且没有待执行自动重启时自动卸载定时任务；新批次启动后需要重新安装。

```sh
python3 pipeline/texopt/monitoring/worker_watch.py install --config /绝对路径/config.json
python3 pipeline/texopt/monitoring/worker_watch.py once --config /绝对路径/config.json
python3 pipeline/texopt/monitoring/worker_watch.py stop --config /绝对路径/config.json
```

监测目录中的 `latest.md` 是中文状态摘要，`latest.json` 保存结构化详情，
`history.jsonl` 保留历次探测。请求失败、重试、流程错误分开计数，普通校验警告不当作请求失败。
配置 `queue_dir` 后，PDF 转译列表读取该目录的 `*.status.json`，仅将 `done` 且退出码为 0
的 PDF 计为完成。整批完成且正常退出的容器，或完成后手动删除的容器，仅保留 PDF 清单，
隐藏检测行、后续进度和历史提示；异常退出、未完成任务及发布校验异常继续展示。
完整容器快照仍保留在 `latest.json` 和历史记录中。

当前 `auto_restart` 配置为冷却 360 秒、每份 PDF 最多 3 次，仅恢复退出码 1、manifest
标记暂停、且本次运行的最后异常明确是临时模型服务故障的容器。认证/权限错误、OOM、
普通编译失败和已删除容器不会自动重启。计划维护前关闭自动重启或先停止监控调度。

报告还展示协调选中字段数、模型已返回字段数、跳过自动复核数和完成汇总，
以及优化子步骤、最近活动源页码、表编号、命名已返回表数、各阶段耗时和日志链接。
返回数量按字段或表去重，不把重试重复计入；缓存命中以阶段结束汇总为准。
表编号仅表示位置，不作为完成百分比。阶段进度从当前尝试的日志回读，
升级监测脚本即可补齐进度，原有增量错误计数不受影响，无需重启容器。
异常退出、内存不足终止、超过 20 分钟无日志更新、成功退出但没有发布 TEX 均会提示。
系统休眠或 Docker 暂停期间无法保证准点执行；Docker 连接失败会记录为监测异常并在下次重试。

### 每日转换统计

`daily_stats.py` 独立于容器监测，使用标准库只读扫描状态库、编译报告和模型调用日志。
每 600 秒刷新，当天统计持续更新，跨天后按北京时间在总表后增加新日期，零完成日也保留。
定时任务安装到 `~/Library/LaunchAgents/`，后续登录继续运行，不随当前 worker 退出而停止。

```sh
python3 pipeline/texopt/monitoring/daily_stats.py once --config /绝对路径/daily/config.json
python3 pipeline/texopt/monitoring/daily_stats.py install --config /绝对路径/daily/config.json
python3 pipeline/texopt/monitoring/daily_stats.py stop --config /绝对路径/daily/config.json
```

配置指定 `scan_roots`（worker 数据目录）、`source_root`（原始 PDF 根目录）、
`publish_root`（正式 TEX 根目录）、`output_dir`、`path_map`（容器到本机路径映射）、
`start_date`、`launchd_label` 和可选 `interval_seconds`。
`stop` 卸载当前登录会话的调度；永久停用时还需删除对应 LaunchAgent plist。

- 完成条件：优化任务为 completed、编译成功、PDF 存在、正式 TEX 和任务产物哈希一致。
  JSON 校验、PDF 溢出页数不影响计数。源页数和生成页数来自编译报告，分别列出。
- 以任务完成时间归入北京时间日期，整份任务已记录的 token 和跨度归入完成日，
  并非接口调用日账单；明细包含上一级目录（批次号）、PDF 名称和源文件完整路径。
- 调用以 call_id 去重，包含识别、协调、优化以及重试。未知 usage、缺失日志、
  中断无结果的请求明确标记。缺少 total_tokens 但有输入输出 usage 时相加得到总量。
- 任务跨度为最早保留的任务/调用起点至完成，包含中断等待；历史日志丢失可能低估。
  模型请求耗时独立累计，并行调用时间之和不能当作墙钟耗时。
- 同一来源和完成时间仅记一次。后续新完成任务单独计数，但以前计入的调用不重复收费。
  首次回填只能核实当前保留的完成版本，已覆盖旧版本无法完整恢复。
- `completions.jsonl` 持久保存完成明细，清理容器或 worker 文件不丢失已经统计的记录。
  `daily.md` 是总表和逐日文件明细；`daily.json` 保存结构化统计及覆盖缺口；
  `daily.jsonl` 每天一行，供智能体读取。日报为账本的原子更新视图，不重复追加当天记录。
  只有 TEX 而没有完整完成凭据的历史文件列为未纳入，未知消耗不会伪装成零消耗。

费用使用 `model_prices.json` 中用户提供的 USD/百万 token 价格；配置 `pricing_file`
可以指定其他价格文件。`long_context_above_tokens` 为单次输入超过该值时采用长档的阈值，
未配置时同时按短、长档计算费用范围。阈值比较使用包含缓存的输入总量。
普通输入 = 输入总量 - 缓存输入 - 缓存写入，四类 token 按各自单价计算，避免重复收费。
OpenAI usage 的输入总量包含缓存；Anthropic 的独立缓存字段先合并为输入总量。
缺少缓存明细时暂按零缓存估算，并保留标记。无 usage、未知模型、无结果及缓存计数
不合法的调用标为未计价；日报费用是估算而不是完整账单。

首次升级从原始调用日志按账本 call_id 补回逐模型用量，保存在账本的 `billing_calls` 中。
以后删除 worker 日志不会丢失已保存的计费依据；重新修改价格或阈值只重算费用，不重新
累计页数或模型调用。日报包含每日、每份 PDF 以及累计各模型的费用，JSON 保留精确十进制
金额、价格快照、缓存用量和计价缺口，Markdown 金额显示四位小数。

## 2. 已确认的 Lexoid 接口

### 2.1 分页标记

Lexoid 当前的分页标记是：

```latex
% LEXOID_PAGE_COMPLETED: <page>/<total>
```

例如：

```latex
% LEXOID_PAGE_COMPLETED: 76/134
```

`texopt` 必须以此标记作为物理 PDF 页的首选来源。`--start-page` 只用于没有分页标记的历史文件，不得覆盖文件中已有的合法标记。

### 2.2 字段标记

Lexoid 当前使用：

```latex
% #VALUE_ID: LEX-P0076-V0001
% #FIELD_VALUE: 字段名
\fieldvalue{值}
```

手写字段使用同一个 `VALUE_ID`：

```latex
% #VALUE_ID: LEX-P0076-V0002
% #FIELD_VALUE: 复核人
% #HANDWRITTEN: 张三
\fieldvalue{\handwritten{张三}}
```

可勾选项必须作为独立布尔字段，而不是用裸 `\square`、`\Box`、
`\boxtimes` 或 `\checkmark` 模拟：

```latex
% #VALUE_ID: LEX-P0076-C0001
% #FIELD_VALUE: 正常检验
\checkboxfield{LEX-P0076-C0001}{checked}{正常检验}
% #VALUE_ID: LEX-P0076-C0002
% #FIELD_VALUE: 复检
\checkboxfield{LEX-P0076-C0002}{unchecked}{复检}
```

每个选项（包括未选项）必须拥有独立且稳定的 `VALUE_ID`。选项文字放在第三个
参数，状态只能是 `checked` 或 `unchecked`。PDF 中宏负责绘制方框；JSON 中输出
`field_type: "checkbox"` 和布尔值 `checked`，后期修改只切换状态。

存疑手写值使用：

```latex
% #TODO #HANDWRITTEN: 张?; handwritten name is unclear
```

约束：

- 注释标记中必须写 `#VALUE_ID` / `#FIELD_VALUE`，不得写成 `#VALUE\_ID` / `#FIELD\_VALUE`。
- `VALUE_ID` 是下游主键，`texopt` 必须原样保留，不得用 LLM 生成的名称替换。
- 如需语义名，只能生成 `semantic_alias`，不得改变主键。
- 字段可见内容行是 `\fieldvalue{...}` 所在行，注释行本身没有 PDF box。SyncTeX 验证应使用前者。

实现同时兼容历史输出中的转义标记 `#VALUE\_ID` / `#FIELD\_VALUE`。例如：

```latex
% #VALUE\_ID: LEX-P0022-V0002
% #FIELD\_VALUE: \\#Events, All Events
\fieldvalue{231,314}
```

registry 保留 `field_id: "LEX-P0022-V0002"`，并根据字段标签生成：

```json
"semantic_alias": "LEX-P0022-V0002-events_all_events"
```

这样可读名称可以重新生成，而下游回填主键保持稳定。

## 3. 根因与术语

### 3.1 根因

`tabularx` 会先把整个环境体读取为宏参数，并为求解 `X` 列宽而多次排版。因此：

- `\the\inputlineno` 往往只能看到环境结束附近的处理行；
- SyncTeX 对 cell 内部原始行的映射可能折叠到环境重放点；
- 含计数、写文件、锚点或标签的宏可在试排阶段产生副作用。

`\inputlineno` 索引和 SyncTeX 是两套机制，报告中必须分开记录，不得将二者的成功或失败互相替代。

### 3.2 opaque 表格

对本工具而言，“opaque 表格”指环境体被整体捕获、重放，导致 cell 源码行不能稳定映射的表格。

| 环境 | 默认分类 | 策略 |
|---|---:|---|
| `tabular`, `tabular*`, `array` | 非 opaque | 仅拆分 cell 源码行 |
| `longtable`, `supertabular` | 候选非 opaque | 必须经实测通过后才加入 allowlist |
| `tabularx`, `tabulary` | opaque | 先转换再拆 cell |
| `tabu`, `longtabu`, `xltabular` | opaque | 先转换；不能精确转换则失败 |
| 用户自定义包装环境 | 未知 | 实测或显式配置 |

`\verb` 只可作为辅助线索，不能作为 opaque 的唯一判据。新版 `tabularx` 对 `\verb` 有限度特殊支持，但仍会捕获并重排环境体。

## 4. 优先从 Lexoid 源头解决

Lexoid 生成新 LaTeX 时应默认输出 SyncTeX-safe 表格：

1. 优先使用 `tabular` + `p{...}`；
2. 跨页表格优先使用已通过实测的 `longtable` / `supertabular`；
3. 不得在新生成的模板中优先推荐 `tabularx`；
4. 必须使用 `tabularx` 时，应视为待 `texopt` 规范化的中间产物，不得直接作为最终可定位 TeX。

`texopt` 的转换功能主要用于历史文件、外部 TeX 和模型未遵守生成约束的情况。

## 5. 语法处理硬约束

### 5.1 不得用正则拆表格

必须使用 TeX-aware 词法扫描器，至少跟踪：

- `{...}` 嵌套深度；
- `\begin` / `\end` 环境嵌套；
- 注释与转义字符；
- 数学模式；
- 顶层 `&` 和顶层行结束 `\\`；
- `\multicolumn`, `\multirow`, `\makecell` 以及嵌套表格。

只能在当前表格的顶层将 `&` 视为 cell 分隔符，将 `\\` 视为 row 结束符。

### 5.2 cell 拆行

每个包含 `\fieldvalue` 或 `\handwritten` 的逻辑值必须占有独立源码行。相应的 `VALUE_ID` / `FIELD_VALUE` / `HANDWRITTEN` 注释紧邻其上，且注释行不得吞掉 `&` 或 `\\`。

格式化不得改变：

- cell 的可见内容；
- row / column 数量；
- `\cline`, `\hline`, `\multicolumn` 和嵌套表格边界；
- 原有注释的语义与归属。

## 6. opaque 表格转换

### 6.1 不变量

`tabularx → tabular` 及其他转换必须保证：

1. `X` 列转换为等价 `p{<measured width>}`；
2. 探针宽度保留 TeX 输出的原始字面量，不先转为浮点数；
3. `>{...}`, `<{...}`, `|`, `\|`, `@{...}`, `!{...}` 原样保留；
4. 只剔除已识别且已被实测宽度替代的 `\hsize` 权重赋值，其他列前/列后声明必须保留；
5. 不改变列对齐语义；
6. 不改变表格的目标总宽；
7. 不得为“看起来更好”而改变字号、行高、列间距或文本。

### 6.2 列宽来源

列宽按以下顺序求解：

#### A. 静态闭式解

仅当下列条件全部成立时使用：

- 所有可变列均为已支持的等权 `X`；
- 无 `@{...}` / `!{...}` / `\extracolsep` 等改变列间开销的构造；
- 规则线数量和 `\tabcolsep` 开销可静态确定。

全 `X` 表格的基本形式为：

```latex
\dimexpr(目标宽度 - 列间开销 - 规则线开销) / X列数\relax
```

固定宽度 `p/m/b` 列与等权 `X` 混排时也使用静态闭式解：先从目标宽度中
逐项扣除固定内容宽度，再扣除全部列的 `2n\tabcolsep` 和实际竖规则宽度，
最后将剩余宽度分配给 X 列。`l/c/r` 的自然宽度依赖内容，仍必须探针。

不得在 Python 中用浮点数代替 TeX 尺寸运算。如整除产生 sp 余数，只在能证明原环境会填满目标宽时将余数补到最后一个 flex 列。

#### B. 编译探针

下列情况必须使用探针：

- `X` 与自然宽度 `l/c/r` 混排；
- 加权 `\hsize` X 列；
- `tabulary` 的 `L/C/R/J`；
- `tabu` 权重列；
- 列修饰导致静态开销不可证明。

探针必须：

- 使用原文档引擎、类、宏包、字体和相同上下文；
- 记录每个最终列的 `\hsize` 或等价宽度；
- 隔离临时产物，不覆盖原文档辅助文件；
- 有超时限制并在报告中保留编译诊断。

#### C. 不可转换

静态解和探针都不能证明等价时，抛出 `UnconvertibleTable`。strict 模式下不得猜测列宽。

### 6.3 副作用审计

转换前扫描环境体中的：

- `\footnote`；
- `\label`；
- `\refstepcounter` / `\stepcounter` / `\addtocounter`；
- `\caption`；
- `\write` / `\immediate`；
- `\hypertarget` 及其他可配置副作用宏。

命中时不一定禁止转换，但必须写入 `report.format_risks`，并由 `verify` 证明最终输出可接受。

### 6.4 审计注释

每个被转换的表格上方增加一行不参与排版的注释：

```latex
% texopt: table=<stable-table-id> from=tabularx method=static spec_sha256=<hash>
```

完整原始 `\begin{...}` 、列规格和转换结果写入 `report.json`。不将可能跨行或含 `%` 的原始内容直接塞入单行 TeX 注释。

## 7. 字段 ID 与语义命名

### 7.1 主键

已有 `% #VALUE_ID` 时，直接使用 `LEX-P####-V####`。这是 registry、验证结果和下游回填的唯一主键。

仅历史文件缺少 `VALUE_ID` 时，`texopt` 才按物理页和视觉顺序生成兼容 ID：

```text
LEX-P0076-V0001
```

生成后必须回写到 TeX，以保证下次运行字节级稳定。

### 7.2 semantic alias

可选别名格式：

```text
p076-dimension_inspection_record-outer_diameter_measured
```

别名不参与 SyncTeX 定位，不能作为回填主键。

表名来源按优先级：

1. 紧邻表格上方、独占一行的 `\textbf{...}` / `\textsc{...}`；
2. 同一空隙内的其他粗体串；
3. `\caption{...}`；
4. 最近的 `\section` / `\subsection` / `\paragraph`；
5. 稳定位置名 `table_p076_03`。

向上扫描在前一张表的 `\end{...}` 或任意 sectioning 命令处停止。

字段语义优先使用 `% #FIELD_VALUE` 的标签。只在标签缺失或过于模糊时才可按表批量调用 LLM。LLM 输出必须经过：

- JSON schema 校验；
- slug 重新生成；
- 表内去重与稳定后缀；
- 缺失 key 的位置名回退；
- 按请求指纹缓存到 `.texopt-names.json`。

`--no-llm` 必须完全离线可用，并保证主键与定位功能不受影响。

## 8. CLI 合同

### 8.1 audit

```bash
python -m texopt.optimization.cli audit input.tex --report audit.json
```

输出：

- 表格环境类型与数量；
- opaque / 未知环境；
- 可用的列宽求解方法；
- 缺失或重复的 `VALUE_ID`；
- 分页标记完整性；
- 格式副作用风险。

`audit` 只读，不修改输入。

### 8.2 optimise

```bash
python -m texopt.optimization.cli optimise input.tex -o input.opt.tex \
  --registry fields.json \
  --report report.json \
  --diff opt.diff
```

也可以分别指定导出目录和文件名：

```bash
python -m texopt.optimization.cli optimise input.tex \
  --output-dir ./exports \
  --output-name customer-form.optimized.tex \
  --registry ./exports/fields.json
```

如果完全不指定输出参数，默认写到输入文件旁的 `<输入名>.opt.tex`。`-o` 与
`--output-dir` / `--output-name` 不能同时使用。

桌面环境也可以打开系统“另存为”窗口，同时选择目录和文件名：

```bash
python -m texopt.optimization.cli optimise input.tex --choose-output
```

### 实时日志

`optimise` 默认在输出 TeX 旁生成 `<输出名>.texopt.log`，也可以自定义：

```bash
python -m texopt.optimization.cli optimise input.tex \
  --output-dir ./exports \
  --output-name result.tex \
  --log-file ./exports/result.optimizer.log
```

日志实时刷新，可以在另一个终端观察：

```bash
tail -f ./exports/result.optimizer.log
```

关键事件代码包括：

- `TABLE_FOUND`：发现 `tabularx` 等 opaque 表格及其源码行；
- `WIDTH_PLAN`：静态求解或探针决策；
- `PROBE_RESULT` / `PROBE_WIDTH`：探针耗时、TeX 错误/警告和逐列宽度；
- `TABLE_CONVERTED` / `OPAQUE_REMAINING`：转换结果或未解决表格；
- `FIELD_READY`：字段 ID、语义别名、物理页和最终 TeX 行；
- `OPT_FINISH`：退出码、耗时、转换数和 opaque 残留数。

`verify` 默认生成 `<TeX名>.verify.log`，逐字段记录 `SYNCTEX_FIELD`，也支持
`--log-file` 指定位置。

### 编码与语法门禁

输入按 BOM、UTF-8、GB18030、Big5 的顺序严格识别，不使用替换字符静默吞掉
坏字节。优化输出、registry 和提取 JSON 统一原子写为无 BOM UTF-8，适配
XeLaTeX；检测结果记录在 `INPUT_READ`、`report.input_encoding` 和
`report.output_encoding`。

转换前后都会检查：

- 花括号是否配对；
- `\begin` / `\end` 环境是否匹配；
- 是否存在非法 `\begin{tabular}{\textwidth}{...}`；
- 是否存在控制字符；
- strict 输出是否仍残留 `tabularx` 等 opaque 环境。

问题分别记录为 `SYNTAX_INPUT`、`SYNTAX_OUTPUT` 和 `SYNTAX_BLOCKED`。语法
error 返回退出码 4，并采用临时文件加原子替换，因此不会覆盖已有正确输出。

行为：

1. 读取分页标记和现有 `VALUE_ID`；
2. 审计所有表格；
3. 按需自动运行列宽探针；
4. 转换 opaque 表格；
5. 拆分 field cell 的源码行；
6. 以临时文件写出，全部 strict 检查通过后再原子替换目标文件。

选项：

- `--no-probe`：禁用编译探针，只允许静态可证明的转换；
- `--allow-opaque`：唯一降级逃生阀，允许保留未转换表格；
- `--start-page N`：仅在没有 Lexoid 分页标记时生效；
- `--semantic-aliases`：生成可选语义别名；
- `--no-llm`：语义别名只使用标签和位置回退。

strict 默认行为：

- 任何 opaque/未知表格未转换：退出 3；
- 不产生或覆盖 `.opt.tex`；
- 仍产生 `report.json` 和诊断信息，便于修复。

`--allow-opaque` 模式下：

- 允许产生 `.opt.tex`；
- stderr 必须显示 WARNING；
- `report.opaque_remaining` 必须逐表列出原因；
- 成功退出码为 2，表示有明确降级，不得返回 0。

### 8.3 verify

```bash
xelatex -synctex=1 -interaction=nonstopmode input.opt.tex

python -m texopt.optimization.cli verify input.opt.tex input.opt.pdf \
  --registry fields.json \
  --baseline-pdf input.orig.pdf \
  --check-geometry
```

`verify` 必须通过 `synctex` CLI 查询定位，不直接依赖 `.synctex.gz` 内部文本格式。

## 9. 验证项

### 9.1 compile

- XeLaTeX 退出码为 0；
- 存在 PDF 和 `.synctex.gz`；
- 中文文档使用 XeLaTeX + `ctex` / `fontspec`；
- 未定义引用、重复 destination 等会破坏定位的警告视为失败或显式风险。

### 9.2 synctex_roundtrip

对每个 registry field：

1. 使用 `\fieldvalue{...}` 的 `value_source_line` 做正向查询；
2. 获取 PDF 页和 box；
3. 使用 SyncTeX 正向结果中的源码锚点 `x/y` 做反向查询；表格返回的 `W/H`
   可能属于整行或整张表，不得误用其几何中心；
4. 反向结果必须落在该值的 `source_span`，而不是要求落在无 box 的注释行。

默认通过率必须为 100%。任何豁免都必须是配置中的显式 field ID，且写入报告。

### 9.3 content_regression

使用 `pdftotext -layout`比较：

- 页数；
- 逐页文本；
- 可配置忽略纯空白差异，但不得忽略可见字符差异。

不使用像素级截图回归。

### 9.4 geometry_regression

只要发生过 opaque 表格转换，必须自动启用，不依赖用户遗忘传入 `--check-geometry`。

使用 `pdftotext -bbox`对齐词框，报告：

- 最大位移；
- p95 位移；
- 超容差词框数；
- 无法对齐的文本及所在页。

默认容差统一为 `0.05pt`，可用 `--geometry-tolerance` 显式放宽。规格和 CLI 帮助中不得同时出现 `0.05pt` 与 `0.5pt` 两个默认值。

## 10. report.json 最低字段

```json
{
  "schema_version": "1.0",
  "input": {},
  "page_markers": {},
  "tables": [],
  "converted_tables": [],
  "opaque_remaining": [],
  "format_risks": [],
  "fields": [],
  "naming": {
    "existing_label": 0,
    "llm": 0,
    "cache": 0,
    "heuristic": 0
  },
  "naming_fallbacks": [],
  "verification": {
    "compile": {},
    "synctex_roundtrip": {},
    "content_regression": {},
    "geometry_regression": {}
  }
}
```

每张表至少记录：

- 稳定 table ID；
- 原环境和新环境；
- 源码起止行；
- 原列规格和新列规格；
- 静态/探针/未转换的决策与原因；
- 实测列宽原始字面量；
- 格式风险；
- 几何回归结果。

## 11. 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功，无降级 |
| 1 | 一般输入、解析、编译或验证失败 |
| 2 | `--allow-opaque` 下成功产出，但仍有 opaque 表格 |
| 3 | strict 模式下存在不可转换表格，未产出优化 TeX |
| 4 | 编码、LaTeX 结构、字段 ID 或分页标记不一致，未覆盖已有输出 |

## 12. 验收标准

一个文档只有在下列条件全部成立时才算优化成功：

- strict 模式下 `opaque_remaining` 为空；
- 所有 `VALUE_ID` 唯一且稳定；
- 所有 field 都有独立的 `value_source_line` / `source_span`；
- XeLaTeX 编译成功并产生 SyncTeX；
- SyncTeX 双向回路 100% 通过；
- 页数和可见文本回归通过；
- 发生表格转换时，几何回归通过；
- 报告中没有未解释的静默回退。

## 13. 实现顺序

1. 先修改 Lexoid LaTeX prompt，使新文档默认使用 `tabular` + `p{}`；
2. 实现分页标记、`VALUE_ID` 和 cell 词法解析；
3. 实现 `audit` 与非 opaque 表格拆行；
4. 实现静态闭式 `tabularx → tabular`；
5. 实现自动探针与复杂列规格；
6. 实现 SyncTeX / content / geometry 验证；
7. 最后增加可选 semantic alias 与 LLM 命名。

LLM 命名不得阻塞基础定位与格式等价转换的交付。

## 14. 参考

- `tabularx` 官方文档：环境体是宏参数，且为求解列宽会多次排版：<https://mirrors.ctan.org/macros/latex/required/tools/tabularx.pdf>
- `tabulary` 官方文档：`L/C/R/J` 列依据内容自然宽度按比例分配：<https://mirrors.ctan.org/macros/latex/contrib/tabulary/tabulary.pdf>
- SyncTeX CLI 是正向/反向查询的对外接口，实现不应自行解析内部文件格式：<https://tug.org/texlive/doc/synctex/synctex.html>

## 15. LaTeX SyncTeX 可定位优化提示词（Lexoid 实例适配版）

下面的提示词用于将 Lexoid 生成的 LaTeX 优化为字段可被 SyncTeX 精确定位的 LaTeX。默认每次处理一个物理页面，不要一次处理整份 132 页文档。

### 可直接使用的提示词

```text
你是一名精通 XeLaTeX、ctex、array、tabularx 和 SyncTeX 的 LaTeX 源码优化器。

你的任务不是重新设计文档，而是在不改变可见内容和版式的前提下，修复字段值在表格中无法通过 SyncTeX 精确定位的问题。

<INPUT_CONTEXT>
文档引擎：XeLaTeX
文档类：ctexart
当前处理范围：{{PAGE_RANGE}}
当前是否包含完整导言区：{{HAS_PREAMBLE}}
当前是否为文档最后一页：{{IS_LAST_PAGE}}
严格模式：是
</INPUT_CONTEXT>

<LATEX_SOURCE>
{{LATEX_SOURCE}}
</LATEX_SOURCE>

一、核心目标

1. 让每个 \fieldvalue{...} 和 \handwritten{...} 所在的可见值都拥有独立、稳定的 LaTeX 源码行。
2. 消除会整体吸收并重放表格 body 的 opaque 表格环境，使 SyncTeX 能看到各个 cell 的原始行。
3. 优化后的文档必须仍使用 XeLaTeX 编译，中文、表格边框、对齐、列宽、行高、换页和字段值必须保持不变。

二、绝对不能改变的内容

1. 不得修改、纠错、翻译、摘要或补全任何可见文字、数字、单位、公式和标点。
2. 不得改变任何字段值，包括看起来可能错误的值。
3. 不得改变任何已有字段 ID，例如：
   % #VALUE_ID: LEX-P0001-V0001
4. 不得重新编号、合并、拆分或删除 VALUE_ID。
5. 不得改变下列注释的内容和归属：
   % #VALUE_ID: ...
   % #FIELD_VALUE: ...
   % #HANDWRITTEN: ...
   % #TODO #HANDWRITTEN: ...
6. 不得将 #FIELD_VALUE 写成 #FIELD\_VALUE，也不得将 #VALUE_ID 写成 #VALUE\_ID。
7. 不得改变 Lexoid 物理分页标记：
   % LEXOID_PAGE_COMPLETED: <page>/<total>
8. "第1页共3页"等可见页码是原始文档的内容；"% LEXOID_PAGE_COMPLETED: 1/132"是整个输入 PDF 的物理页标记。两者语义不同，都必须原样保留。
9. 不得改变 \newpage、页面顺序、页眉、页脚、印章 TODO 或其他页面边界。
10. 不得删除 \fieldvalue 或 \handwritten 包装。

三、表格环境处理

1. 下列环境视为 opaque，优化结果中不得残留：
   tabularx、tabulary、tabu、longtabu、xltabular。
2. 普通 tabular、tabular*、array 不得为了"统一风格"而无故改写。
3. 将 tabularx 转换为 tabular 时：
   - 删除 \begin{tabularx} 的目标宽度参数；
   - 把每个 X 列替换为等价 p{<width>}；
   - 保留 >{...}、<{...}、|、\|、@{...}、!{...} 等列修饰；
   - 保留 \centering、\raggedright、\RaggedRight、\arraybackslash 等对齐声明；
   - 保留 \multicolumn、\multirow、\cline、\hline 和嵌套表格结构。
4. 普通 tabular 的正确语法是：
   \begin{tabular}{<column spec>}
   严禁生成：
   \begin{tabular}{\textwidth}{...}
   严禁在 tabular 的列规格中残留 X。

四、列宽精确转换

1. 不得凭视觉或经验猜测 X 列宽。
2. 不得使用 Python/JavaScript 浮点数计算 TeX 尺寸。
3. 如输入提供了探针实测宽度，必须原样使用宽度字面量，例如 213.39569pt，不得截断或重新四舍五入。
4. 仅当列宽可由静态闭式精确求解时，才可直接写 \dimexpr。
5. 对于没有 @{...}、!{...}、\extracolsep 的标准列规格：
   - n 个物理列的 tabcolsep 开销为 2n\tabcolsep；
   - 竖线开销为列规格中实际竖规则的数量 × \arrayrulewidth；
   - 固定 p/m/b 列的内容宽度必须从目标总宽中扣除；
   - 剩余宽度按 X 列语义分配。
6. 对本实例中的：
   \begin{tabularx}{\textwidth}{|>{\centering\arraybackslash}p{3.0cm}|X|>{\centering\arraybackslash}p{2.2cm}|X|}
   必须转换为等价形式：
   \begin{tabular}{|>{\centering\arraybackslash}p{3.0cm}|p{\dimexpr(\textwidth-5.2cm-8\tabcolsep-5\arrayrulewidth)/2\relax}|>{\centering\arraybackslash}p{2.2cm}|p{\dimexpr(\textwidth-5.2cm-8\tabcolsep-5\arrayrulewidth)/2\relax}|}
7. 对本实例中的：
   \begin{tabularx}{\textwidth}{|>{\centering\arraybackslash}p{1.7cm}|>{\centering\arraybackslash}p{1.8cm}|>{\centering\arraybackslash}p{2.7cm}|X|X|}
   两个 X 列的宽度必须为：
   p{\dimexpr(\textwidth-6.2cm-10\tabcolsep-6\arrayrulewidth)/2\relax}
8. 如果存在加权 \hsize、@{...}、!{...}、tabulary 内容比例列或其他无法静态证明的列规格，必须使用外部提供的探针宽度。
9. 如果既无法静态精确求解，也没有探针宽度，不得猜测，应进入下文定义的 BLOCKED 输出。

五、字段与 cell 拆行

1. 每个逻辑字段必须保持如下相邻结构：
   % #VALUE_ID: LEX-Pxxxx-Vxxxx
   % #FIELD_VALUE: 字段标签
   % #HANDWRITTEN: 手写值（如适用）
   \fieldvalue{\handwritten{值}}
2. 每个 \fieldvalue{...} 必须从新的独立源码行开始。
3. 同一 cell 内有多个字段时，每个字段都必须保留自己的标记和独立值行。
4. &、\\、\hline、\cline 必须放在不会被 % 注释吞掉的位置。
5. 不得将多个字段包进一个 \fieldvalue。
6. 不得为固定标题、表头、说明文字、页码或样板文字新增 \fieldvalue。
7. 每个可勾选项（选中和未选中）都必须有独立 `VALUE_ID`，并使用：
   `\checkboxfield{<VALUE_ID>}{checked|unchecked}{选项文字}`。
8. 不得用裸 `\square`、`\Box`、`\boxtimes` 或 `\checkmark` 表示可编辑选项；
   不得把选项文字塞进状态值，状态与标签必须分离。

六、宏与副作用

1. 保留导言区中已有的 \fieldvalue 和 \handwritten 定义，除非输入明确要求增加可点击索引宏。
2. 如输入分块不包含导言区，不得自行重复输出 \documentclass、\usepackage 或宏定义。
3. 检查 opaque 表格中的 \footnote、\label、\refstepcounter、\stepcounter、\caption、\write、\hypertarget。
4. 如发现上述副作用，不得删除，但必须在该表格上方增加一行风险注释：
   % texopt-risk: replay-sensitive command=<command>
5. 将 opaque 表格转换为单次排版的 tabular 后，不得为了模拟原先的重放次数而重复执行副作用宏。

七、审计注释

1. 在每个被转换的 opaque 表格上方添加一行：
   % texopt: table=<page-local-index> from=tabularx to=tabular method=<static|probe>
2. 审计注释不得参与排版，不得改变表格的垂直间距。
3. 不得把多行原始列规格完整塞进单行注释；审计注释只记录稳定索引和方法。

八、输出前必须完成的自检

1. 搜索优化结果，确认不存在：
   \begin{tabularx}
   \begin{tabulary}
   \begin{tabu}
   \begin{longtabu}
   \begin{xltabular}
2. 确认没有任何 tabular 列规格残留 X。
3. 确认不存在 \begin{tabular}{\textwidth}{...} 这类非法语法。
4. 确认 VALUE_ID 集合、顺序和每个 ID 的值与输入完全一致。
5. 确认每个 #FIELD_VALUE 都与紧随其后的 \fieldvalue 属于同一逻辑字段。
6. 确认页面标记、\newpage 和页面顺序不变。
7. 确认所有 \begin / \end、花括号、数学模式和表格行结束平衡。
8. 如输入为完整文档，保证只有一个 \begin{document} 和一个 \end{document}。
9. 不得声称已经完成 XeLaTeX、SyncTeX 或 PDF 几何验证，除非系统确实向你提供了对应工具结果。

九、严格失败规则

如果任意 opaque 表格无法精确求得列宽，或无法确定转换后与原格式等价，不得：
- 猜测宽度；
- 删除该表格；
- 保留 opaque 表格并假装优化成功；
- 输出一份部分改写的 LaTeX。

此时只输出以下 JSON，不要输出 Markdown 或 LaTeX：
{
  "status": "BLOCKED",
  "page_range": "{{PAGE_RANGE}}",
  "table_index": <index>,
  "environment": "<environment>",
  "reason": "<why exact conversion cannot be proven>",
  "required_probe": "<what width or context is required>"
}

十、成功输出格式

成功时，只输出优化后的 LaTeX 源码原文：
- 不要使用 Markdown 代码块；
- 不要添加解释、总结、标题或对话文字；
- 不要用省略号代替任何未修改内容；
- 必须完整输出当前输入分块；
- 如 {{HAS_PREAMBLE}}=否，不要补导言区；
- 如 {{IS_LAST_PAGE}}=否，不要补 \end{document}。
```

### 针对该实例的调用方式

建议使用 `% LEXOID_PAGE_COMPLETED: n/132` 将源文件分为物理页块。

第一页：

```text
{{PAGE_RANGE}} = 1/132
{{HAS_PREAMBLE}} = 是
{{IS_LAST_PAGE}} = 否
{{LATEX_SOURCE}} = 从文件开头到 % LEXOID_PAGE_COMPLETED: 1/132
```

中间页：

```text
{{PAGE_RANGE}} = n/132
{{HAS_PREAMBLE}} = 否
{{IS_LAST_PAGE}} = 否
{{LATEX_SOURCE}} = 上一个分页标记之后到 % LEXOID_PAGE_COMPLETED: n/132
```

最后一页：

```text
{{PAGE_RANGE}} = 132/132
{{HAS_PREAMBLE}} = 否
{{IS_LAST_PAGE}} = 是
{{LATEX_SOURCE}} = 第 131 页标记之后到 \end{document}
```

### 编译后验证命令

提示词只负责源码转换，不能代替真实编译与回归验证。合并所有页后执行：

```bash
xelatex -synctex=1 -interaction=nonstopmode optimized.tex
xelatex -synctex=1 -interaction=nonstopmode optimized.tex
```

然后检查：

1. XeLaTeX 无错误；
2. 生成 `optimized.synctex.gz`；
3. 每个 `\fieldvalue` 正向查询有 PDF box；
4. 使用 SyncTeX 返回的源码锚点 `x/y` 反向查询回到该 `\fieldvalue` 的源码行；
5. 与原 PDF 对比页数、`pdftotext -layout` 文本和 `pdftotext -bbox` 几何坐标。
