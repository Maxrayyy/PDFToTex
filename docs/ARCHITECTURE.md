# PDFToTex 目录架构

## 目标

PDFToTex 只保留当前生产链路：视觉识别、方向检测、TeX 生成、TeX 优化、XeLaTeX 编译、JSON 导出和监控，并提供隔离的 Web 工作台。目录名称应表达真实职责，避免使用没有语义的 `files` 层级。

## 目标结构

```text
PDFToTex/
├── Lexoid/                 # 视觉识别核心 Python 包
├── pipeline/               # PDF -> TeX 流水线
│   ├── texopt/              # 分发名 pdftotex-pipeline，导入名 texopt
│   ├── tests/               # 流水线测试
│   └── docker/              # Dockerfile 和 Compose 入口
├── web/                    # 独立 Web API、执行器、前端和任务数据边界
├── deploy/                 # 平台构建与调度配置
├── scripts/                # 宿主机队列及一次性工具
├── docs/                   # 架构、已知问题和运行说明
├── AGENT.md
└── README.md
```

## 迁移原则

1. 保留 `texopt` 的安装名、`texopt` 和 `texopt-pipeline` 命令，不改变容器内 API。
2. 仅调整源码路径和导入路径；运行数据目录保持不变。
3. Docker 的依赖运行时与工作镜像分层构建，镜像标签与架构由 `deploy/platforms/` 配置（见 ADR-0010）。
4. 纯视觉识别链路保持不变，不引入结构化表格中间格式。
5. 每个迁移步骤独立提交，失败时可按提交回退。

## 运行数据边界

工作区实际结构为 `lexiod/Downloads/`、`lexiod/data/` 和 `lexiod/PDFToTex/` 三个平级目录。`Downloads/` 保存原始 PDF 输入；`data/workers`、`data/optimized`、`data/monitoring` 保存 Docker 运行数据、缓存、正式产物和监控状态。它们都不属于 `PDFToTex` 源码目录，也不纳入任何 Git 仓库。目录重构只更新挂载配置和文档路径，不移动或提交其中的数据。

## Git 归属

`Lexoid/` 保留独立 Git 仓库；`pipeline/` 已解除嵌套 Git并由总项目统一提交。这样可以独立维护 Lexoid，同时让流水线目录重构、Docker 配置和监控代码随总项目统一回退。

## 当前状态

目录重构已经完成：流水线包位于 `pipeline/texopt/`，Docker 入口位于
`pipeline/docker/`，测试位于 `pipeline/tests/`，根目录 `docker-compose.yml`
是生产队列的唯一 Compose 入口。生产模块保留在同一 `texopt` 包中，以维持
`texopt` 和 `texopt-pipeline` 命令入口稳定。

`pipeline/docker/docker-compose.yml` 与 `pipeline/app.py` 是历史可选网页上传入口，
不参与根目录队列 worker 的默认启动。网页入口仍复用同一 `texopt` 包和 JSON
导出能力，不能与生产队列目录混用配置。


## 当前职责边界

- `pipeline/texopt/optimization/repair_transaction.py` 统一候选接受、定向重试与回退；`syntax_repair.py` 负责生成及候选缓存。成功模型改值沿用审计政策。
- `pipeline/texopt/optimization/compilation.py` 统一最终编译、源页映射、几何验收和有限调整，返回结果给 CLI。
- `web/backend/worker.py` 管理独立任务和容器；`observations.py` 后台增量汇总运行证据并持久化快照；API 详情只读快照。Web 不接管旧队列。

领域定义见 [CONTEXT](../CONTEXT.md) / [术语表](../GLOSSARY.md)，决策及完整 Git 检索范围见 [ADR 索引](adr/README.md)。本轮提交与回归结果见 [架构改进记录](issues/architecture-deepening.md)。
