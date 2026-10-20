# Lexiod Web 工作台

独立于现有 CLI、根 Compose、批次调度及监控的可选入口。Web 不接管既有容器；原有转换命令、`.env`、输入输出目录与 Overleaf 定时发布均保持原用法。

## 启动

在 `PDFToTex` 根目录执行。需要 Python 3.10+、Node 20+、Docker，以及已有 `pdftotex:local` 镜像。

```bash
python3 -m venv .venv-web
.venv-web/bin/python -m pip install -r web/requirements.txt
npm --prefix web/frontend ci
npm --prefix web/frontend run build
.venv-web/bin/python scripts/web.py serve
```

另开终端启动独立执行器：

```bash
.venv-web/bin/python scripts/web.py worker
```

访问 <http://127.0.0.1:8765>。关闭浏览器不影响任务；关闭 API 或执行器也不会主动杀死转换容器。重新启动执行器后，会先检查已有容器再恢复观察，避免重复执行。页面左下角显示执行器心跳，离线时文件仍可上传、排队。

部署时使用 `--config /绝对路径/web.json`，API 和执行器必须使用同一配置文件；示例见 [`deploy/web.example.json`](../deploy/web.example.json)。可用 `serve --host 0.0.0.0 --port 8765` 配置监听地址。无账号、登录或角色权限，所有访问者共享工作台。目录值建议使用绝对路径；相对目录统一以仓库根目录为基准解析。修改部署文件中的运行参数后，需要重启 API 和执行器；运行容器会被重新接管。

不要在正在提供页面的目录中同时重新构建前端；先构建，再重启 API。前端开发可另运行 `npm --prefix web/frontend run dev`，Vite 将 API 请求转发至 8765。

## 操作

- **上传与队列**：选单个/多个 PDF 或文件夹。文件夹递归列出所有 PDF，保留相对目录，按自然顺序排列，可勾选或上移。确认后分片上传并校验 SHA256。刷新后重新选择同一文件，可从已保存的偏移继续上传。
- **单文件运行**：所有浏览器共用一个持久队列，数据库强制最多一个活动文件。识别页并发默认 1，可在上传页面设置；修改默认值不影响已提交任务，未启动任务可在详情配置中修改自己的页并发。
- **模型与配置**：选择 OpenAI、DeepSeek 或自定义兼容服务，输入 Base URL 和 Key；识别阶段必须选择支持图像的模型。可以查询服务模型列表或手动填写模型 ID。校对、优化可选择各自供应商和模型。模板保存不含 Key。当前复用流水线的自动语法修复使用校对模型，若需要自动修复，应选择该流水线支持的 GPT/o 系列修复模型；DeepSeek 等可用于优化命名。模型列表查询只验证连通性，不冒充真实图像推理测试。
- **任务详情**：显示源页完成数、失败页及原因、阶段、请求模型、实际用量、重试记录、TeX 源码、结构检查及编译日志。供应商未返回 Token 时显示“未返回”，费用仅为配置价格可覆盖的估算。现有调用日志不保证返回服务端实际模型版本，因此界面明确标注“请求模型”。
- **控制**：暂停队列让当前文件继续完成；排队文件可单独暂停、取消或提前。取消运行任务会停止对应 Web 容器并保留输入/缓存。失败后停止领取后续文件；“重试并恢复队列”复用缓存继续处理。运行中文件的逐页暂停和选页重跑属于后续增强，不提供假按钮。
- **TeX 编译**：可上传单个 `.tex` 或带图片、`.sty`、`.cls`、子文件的项目文件夹，选择主文件，运行两遍 XeLaTeX。此入口不按 PDF 筛选项目。编译不调用模型，不需要 Key。缺失字体/宏包或需要 BibTeX/Biber、shell escape 的项目可能需要后续扩展工具链。
- **PDF 预览**：点击编译完成的文件或“查看 PDF”，进入稳定产物链接。支持页码跳转、缩放、下载和刷新直达；PDF 转换结果提供原件对照。旧成功版本与最近失败状态明确区分。
- **产物与追溯**：下载项目 ZIP 包含输入、各次成功编译产物和来源哈希/配置/事件清单。检查项区分通过、告警、失败、未检查；编译成功不表示内容无误。批次别名可通过 `batch_aliases` 配置，原批次名仍记录在任务中。

## 运行目录与凭据

默认工作目录为仓库外的 `../data/web/`：

```text
config.json               # 可选部署配置；页面保存默认并发时原子写入
state.sqlite3             # 任务、配置模板、上传会话、事件和产物索引
uploads/<upload_id>/      # 分片上传和校验后的原件
jobs/<job_id>/input/       # 每份任务自己的项目，保留相对路径
jobs/<job_id>/work/        # 原流水线阶段状态与缓存，重试继续复用
jobs/<job_id>/result/<n>/  # 每次编译的独立输出
jobs/<job_id>/observations.json        # 网页读取的进度快照
jobs/<job_id>/observations-index.json  # 可重建的证据索引和日志游标
credentials/              # 0600 的 Key 文件，仅后端/对应转换容器使用
publishing/               # 可选 Web 发布的独立暂存区
heartbeat.json            # 独立执行器健康状态
latest.md                 # 独立 Web 监控摘要，列出当前容器
```

编译容器禁用网络、shell escape，限制内存/CPU/进程数/超时，不挂载 API Key 或现有业务目录。转换容器只挂载自身目录、Web runner 和自身所需的凭据，不能扫描旧队列。旧镜像不会由 Web 自动重建或覆盖；`image` 可指定固定标签或摘要。

当前 Web 数据不自动清理。`min_free_bytes` 在上传及领取任务前阻止空间不足时继续接收任务；`max_upload_bytes` 限制单文件大小。清理前应确认不再需要原件和恢复缓存。

## 进度读取与恢复

独立执行器按 `poll_seconds` 刷新运行中任务的进度，任务状态变化后也刷新一次。
HTTP 详情只读取快照，多浏览器查看不会重复扫描原始调用日志和 TeX。
新增调用按文件游标读取；TeX、编译日志和检查报告只在文件变化时重新读取。
页完成集合、未知 Token 和失败阶段继续由原始证据决定，不用最大页码推测完成数。

索引记录文件身份、长度和末段校验信息，支持追加、轮换、截断和尾部半条 JSON；
相同 `call_id` 去重。索引和快照都是可重建数据，原子写入并以任务级锁串行刷新，
不修改流水线的阶段状态库。缺失或损坏时由执行器重建；执行器重启后从保存的游标继续。
网页会显示“正在汇总进度”或“进度等待更新”，不会将尚未生成的检查结果显示为通过。

## 可选集成

下列字段全部在 Web 部署 JSON 配置中，不修改原有配置文件：

- `monitor_snapshot`、`daily_report`：现有监控/日报文件路径，只读展示。
- `legacy_artifact_roots`：按单元配置历史 TeX 根目录，如 `{"U1":"../data/optimized/U1"}`。按批次只读检索，可复制到 Web 工作目录重新编译，不写回旧文件。
- `legacy_queue_status`：旧队列 `.status.json` 路径列表，只读，不接管。
- `overleaf_config`：现有发布配置路径。未设置时 Web 发布按钮禁用。发布进程应以拥有原受保护 Git 凭据的服务账户运行，不复制 token，不把 token 放进 URL。
- `batch_aliases`：如 `{"A33Z202410007":"A33Z2202410007"}`，统一新任务批次展示，不移动历史目录。
- `email`：沿用原监控邮件配置结构，包含 `smtp_host`、`smtp_port`、`sender`、`recipient`、`username`、`password_env`。密码通过执行器环境注入，不写入前端或任务快照。

Overleaf 预检和发布均排入独立持久操作记录。实际发布复用既有发布器、项目锁和账本；先拉取最新提交，再只增加缺失文件，同名内容不同则停止。Web 仅为本次产物生成独立暂存配置，不更改生产 `source_root`，也不自动启用 U2。预检不提交或推送；人工协作者在预检后的改动仍会在正式发布时重新拉取。

临时 API 失败采用 3、6、12、24、48、96 分钟重试，最多 6 次。认证失败、OOM、编译失败不自动重启。邮件配置后，记录并发送每次重启前及启动成功/失败通知，失败包含次数；未配置和发送失败也在任务事件中明确展示。Web 统计保存于自己的任务库，不改写现有日报账本。

## 验证

```bash
PYTHONPATH=Lexoid:pipeline:. .venv-web/bin/python -m pytest web/tests -q
WEB_DOCKER_TESTS=1 PYTHONPATH=Lexoid:pipeline:. .venv-web/bin/python -m pytest web/tests -q
npm --prefix web/frontend run build
```

真实容器测试使用现有镜像及隔离的临时目录，验证项目编译、执行器接管、PDF 渲染、失败定位和队列停止。Overleaf 集成测试使用本地 Git bare 仓库验证协作者改动保留及冲突不覆盖，不访问线上项目。

浏览器测试需要先启动使用**独立测试数据目录**的 API 和执行器，再运行：

```bash
cd web/frontend
npx playwright install chromium
npm run test:e2e
```

浏览器测试会实际上传并编译测试 TeX；测试截图写到工作区 `data/audits/web-v1/`。完整 PDF 转换联调使用本地固定响应的模型服务，验证现有识别、校对、优化、编译链路和用量展示；它不是对线上模型识别准确率的验证。
