# PDFToTex 目录架构

## 目标

PDFToTex 只保留当前生产链路：视觉识别、方向检测、TeX 生成、TeX 优化、XeLaTeX 编译、JSON 导出和监控。目录名称应表达真实职责，避免使用没有语义的 `files` 层级。

## 目标结构

```text
PDFToTex/
├── Lexoid/                 # 视觉识别核心 Python 包
├── pipeline/               # PDF -> TeX 流水线 Python 包（安装名 texopt）
│   ├── __init__.py
│   ├── cli.py              # texopt 命令入口
│   ├── stages.py           # 阶段编排和状态推进
│   ├── recognition/        # 识别调用、回退和证据
│   ├── optimization/       # TeX 检查、修复、测宽和编译
│   ├── monitoring/         # worker_watch、日报和队列状态
│   └── tests/              # 流水线测试
├── docker/                 # Dockerfile 和 Compose 入口
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

当前 `Lexoid/` 和 `lexiod-pipeline/` 都是父仓库中的 Git submodule（模式 `160000`），并分别保留独立远程仓库。本次只解除 `lexiod-pipeline` 的嵌套 Git；`Lexoid/` 保留 `.git` 和独立远程仓库。迁移顺序为：记录流水线子仓库最后提交号，再从父仓库索引移除 `lexiod-pipeline`，删除其目录内 `.git`，将目录内容作为普通文件重新加入父仓库。迁移完成后流水线由总项目统一提交，Lexoid 仍可独立提交。

## 当前过渡状态

流水线包已从 `lexiod-pipeline/files` 改名为 `lexiod-pipeline/texopt`，后续再将 Docker 入口、监控模块和测试按职责归位到目标目录。
