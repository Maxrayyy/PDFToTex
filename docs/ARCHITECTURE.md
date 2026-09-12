# PDFToTex 目录架构

## 目标

PDFToTex 只保留当前生产链路：视觉识别、方向检测、TeX 生成、TeX 优化、XeLaTeX 编译、JSON 导出和监控。目录名称应表达真实职责，避免使用没有语义的 `files` 层级。

## 目标结构

```text
PDFToTex/
├── Lexoid/                 # 视觉识别核心 Python 包
├── pipeline/               # PDF -> TeX 流水线
│   ├── texopt/              # 分发名 pdftotex-pipeline，导入名 texopt
│   ├── tests/               # 流水线测试
│   └── docker/              # Dockerfile 和 Compose 入口
├── scripts/                # 宿主机队列及一次性工具
├── docs/                   # 架构、已知问题和运行说明
├── AGENT.md
└── README.md
```

## 迁移原则

1. 保留 `texopt` 的安装名、`texopt` 和 `texopt-pipeline` 命令，不改变容器内 API。
2. 仅调整源码路径和导入路径；运行数据目录保持不变。
3. Docker 构建只使用 `pdftotex-runtime:local` 作为基础镜像，并产出 `pdftotex:local`。
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

`pipeline/docker/docker-compose.yml` 与 `pipeline/app.py` 是可选的网页上传入口，
不参与根目录队列 worker 的默认启动。网页入口仍复用同一 `texopt` 包和 JSON
导出能力，不能与生产队列目录混用配置。
