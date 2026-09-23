# PDFToTex Linux 服务器迁移设计

## 1. 目标

将当前 macOS / Apple Silicon 上的 PDFToTex 完整链路迁移到 Ubuntu 24.04 x86_64 服务器，并满足以下要求：

- 服务器可以从 Git 仓库独立构建 `linux/amd64` 镜像，不依赖本机已有镜像。
- 原始 PDF、断点缓存、正式 TEX、监控状态和费用记录都持久化在宿主机。
- 队列可暂停、恢复和重建容器，删除容器不会删除数据。
- 实时监控每 120 秒运行，日报每 600 秒运行，服务器重启后自动恢复调度。
- 临时模型服务故障最多自动重启 3 次；认证、OOM、编译错误和人工暂停不自动重启。
- 每个完成批次可生成一个保持目录结构的 Overleaf 上传包；Premium Git 作为可选自动发布方式。
- 迁移期间保留本机回退能力，同一份 PDF 不在两台机器并行处理。

本设计不改变识别提示词、表格规则、模型选择、费用算法或 TEX 内容。

## 2. 当前状态

### 2.1 本机

- 代码仓库约 90 MB，`Lexoid` 是 Git submodule，`pipeline` 属于主仓库。
- `Downloads` 约 9.2 GB。
- `data` 约 25 GB，其中 `data/workers` 约 24 GB。
- 当前三个主要镜像均为 ARM64；运行时镜像约 5.73 GB。
- `pipeline/docker/Dockerfile.hybrid` 依赖本机已有的 `pdftotex-runtime:local`。
- 仓库没有可以从零构建该运行时镜像的 Dockerfile。
- 实时监控和日报的安装逻辑仅支持 macOS `launchd`。
- 当前有效 XeLaTeX 是 `TeX Live 2025/dev/Debian`，不是 TeX Live 2024。

### 2.2 目标服务器

- Ubuntu 24.04.4 LTS，x86_64，4 核，14 GiB 内存。
- Docker 29.8.1、Compose v5.5.1、Buildx v0.37.1 已安装并验证。
- Python 3.12.3、NVM 0.40.3、Node.js 24.21.0 已安装。
- Mihomo v1.19.31 已作为 systemd 服务运行，HTTP/SOCKS5 代理分别监听 `127.0.0.1:7890/7891`。
- 系统盘只有 59 GB，可用约 53 GB，没有 Swap。

当前磁盘不能容纳 34 GB 数据、运行镜像、测试镜像、构建缓存和后续产物。扩容是迁移前置条件。

## 3. 方案选择

### 3.1 推荐：Git + 服务器原生构建 + 两阶段 rsync

代码通过 Git 获取，数据通过 rsync 迁移，AMD64 镜像在服务器原生构建。先在线执行一次大体量同步，再暂停本机任务执行最终增量同步和切换。

优点：

- 服务器后续能独立升级和重建。
- 不传输无用的 Git 对象、ARM64 镜像和 Docker 构建缓存。
- 最终停机窗口只包含增量数据。
- 任一阶段失败都可以回到本机继续。

缺点是必须先补齐可复现的运行时 Dockerfile 和 Linux systemd 调度文件。

### 3.2 备选：导出并传输 Docker 镜像

当前镜像是 `linux/arm64`，目标服务器是 `linux/amd64`，直接 `docker save/load` 后不能运行。先在本机通过 QEMU 构建 AMD64 镜像再上传可行，但构建慢、镜像传输大，仍不能解决后续独立重建问题，因此不采用。

### 3.3 备选：整体 rsync 当前工作目录

直接复制约 34 GB 可以保留当前状态，但会混入本机绝对路径、macOS 调度配置和可能无用的构建文件。该方案只能作为数据迁移手段，不能代替部署设计。

## 4. 目标架构

```mermaid
flowchart TD
    G[GitHub: PDFToTex + Lexoid submodule] --> C[/srv/pdftotex/PDFToTex]
    I[/srv/pdftotex/Downloads] -->|只读 /input| W[AMD64 worker 容器]
    C -->|构建源码与脚本| W
    S[.env 密钥] -->|env_file| W
    W -->|读写 /data| D[/srv/pdftotex/data]
    W --> API[远程视觉/修复模型 API]
    D --> M[systemd timer: 实时监控 120 秒]
    D --> R[systemd timer: 日报 600 秒]
    D --> P[批次发布门槛]
    P --> Z[/srv/pdftotex/overleaf/outbox/*.zip]
    P --> O[可选 Overleaf Git 工作区]
```

### 4.1 目录

```text
/srv/pdftotex/
├── PDFToTex/                 Git 工作树
├── Downloads/               原始 PDF，只读挂载
├── data/
│   ├── workers/             缓存、状态库、模型调用日志
│   ├── optimized/           正式 TEX
│   ├── monitoring/          实时监控、日报和批次状态
│   ├── fix/                 修复验证产物
│   ├── audits/              审计记录
│   └── .cache/              共享缓存
└── overleaf/
    ├── outbox/              手工上传 ZIP
    └── projects/            可选的 Overleaf Git 工作区
```

`Downloads`、`data` 和 `PDFToTex` 保持同级，因此现有 Compose 挂载关系无需改变。目录归属专用用户 `pdftotex`；只有系统初始化、磁盘挂载和 systemd 安装使用 root。

### 4.2 命名

- 镜像：`pdftotex-runtime:local`、`pdftotex:local`、`pdftotex-test:local`。
- 额外保留不可变标签：`pdftotex-runtime:<git-sha>-amd64`、`pdftotex:<git-sha>-amd64`。
- 容器：`pdftotex-<批次号>-<序号>`。
- U1/U2/U3 只保留在输入和输出路径中，不进入容器名或镜像名。

## 5. 存储与容量

### 5.1 容量门槛

- 推荐挂载 200 GB 数据盘到 `/srv/pdftotex`。
- 开始迁移前 `/srv/pdftotex` 可用空间不得少于 120 GB。
- 迁移及构建后必须至少保留 30 GB 空闲空间。
- 扩容后配置 8 GB Swap，降低首次构建和并行编译被 OOM 杀死的概率。

60 GB 系统盘不作为正式部署目标。未扩容时只能做代码拉取和小型验证，不能复制完整数据或构建全部镜像。

### 5.2 数据分级

必须迁移：

- `Downloads`
- `data/workers`，包括隐藏的 `.state`、`.cache` 和 `.pipeline`
- `data/optimized`
- `data/monitoring`
- `data/.cache`
- `data/audits`
- `Lexoid/.env`、`pipeline/texopt/.env`，通过独立安全传输

可以延后：

- `data/fix`、`data/analysis`、`data/benchmarks`、`data/previews`
- 已确认不再使用的历史实验产物

默认迁移完整 `Downloads` 和 `data`，不迁移本机 Docker 镜像、容器、BuildKit 缓存或 macOS plist。

## 6. 镜像设计

### 6.1 可复现 AMD64 运行时

新增 `pipeline/docker/Dockerfile.runtime`，负责：

- 固定 Python 3.10 slim 基础镜像。
- 创建 `/opt/venv`。
- 从 `Lexoid/poetry.lock` 安装 CPU 版 PyTorch、PaddlePaddle 3.2.2、PaddleOCR 和其余锁定依赖。
- 安装 Chromium shell 及运行库。
- 安装 XeLaTeX、中文字体包、LaTeX 扩展和 Poppler。
- 不复制业务源码，不写入 API 密钥，不下载批次 PDF。

`Dockerfile.hybrid` 继续只负责安装当前 `Lexoid` 和 `pipeline/texopt` 源码。这样依赖层和代码层可以分别缓存，代码更新不重装大型模型依赖。

构建顺序固定为：

1. `pdftotex-runtime:local`
2. `pdftotex:local`
3. `pdftotex-test:local`
4. 容器测试
5. 真实 PDF canary

ARM64 和 AMD64 从同一 Dockerfile 构建，但生产验收只接受 `linux/amd64`。

### 6.2 版本与版面稳定性

当前有效镜像报告 `TeX Live 2025/dev/Debian`。服务器迁移先复现这一已验证环境；切换到 TeX Live 2024 属于独立的版面变更，必须另做代表性 PDF 回归，不能混入迁移。

每次构建保存以下清单：

- Git SHA 和 Lexoid submodule SHA
- 基础镜像 ID 与架构
- `pip freeze`
- `xelatex --version`
- Paddle/PaddleOCR/PaddleX 版本
- 镜像 ID 和构建时间

## 7. 配置与密钥

- Git 只保存 `.env.example`，不保存真实密钥。
- `Lexoid/.env`、`pipeline/texopt/.env` 在服务器上设为 `0600`。
- 模型配置保持现有值，迁移不更换模型或并发策略。
- 队列 JSON 的 `host_data` 改为 `/srv/pdftotex/data`。
- 实时监控的 Docker 路径改为 `/usr/bin/docker`。
- 所有 macOS 根路径改为 `/srv/pdftotex/...`。
- 监控间隔为 120 秒，日报间隔为 600 秒。

Mihomo 默认只监听宿主机回环地址。容器可以先直连模型 API；只有验证确认容器必须使用代理时，才增加绑定在 Docker bridge 地址上的受限转发端口。不得把 7890、7891 或 9090 暴露到公网。

## 8. Linux 监控

Python 监控逻辑保持不变，Linux 不调用脚本中的 `install/stop` 子命令。新增四个 systemd 文件：

- `pdftotex-realtime-monitor.service`
- `pdftotex-realtime-monitor.timer`
- `pdftotex-daily-stats.service`
- `pdftotex-daily-stats.timer`

实时服务每次运行：

```text
python3 pipeline/texopt/monitoring/worker_watch.py once --config /srv/pdftotex/data/monitoring/realtime/config.json
```

日报服务每次运行：

```text
python3 pipeline/texopt/monitoring/daily_stats.py once --config /srv/pdftotex/data/monitoring/daily/config.json
```

timer 持续存在，不因全部容器完成而自行卸载。启动批次后必须同时满足：

- 批次容器状态为 `running`。
- `pdftotex-realtime-monitor.timer` 为 `active`。
- `latest.md` 在 120 秒内刷新且包含当前容器。

容器继续使用 `restart: "no"`。自动重启仍由 `worker_watch.py` 的分类和上限控制，避免确定性错误无限重启。

## 9. 数据迁移与切换

采用两阶段 rsync：

1. **预同步**：本机仍可读取和运行，但不删除服务器文件；复制大部分 34 GB 数据。
2. **最终同步**：停止本机转换容器和两个监控调度，确认没有写入后执行增量同步。
3. 更新服务器绝对路径和权限。
4. 构建、测试并运行一份代表性 PDF。
5. 只在 canary 通过后启动新批次。

PDF、图片和压缩缓存已经高度压缩，rsync 不使用 `-z`。传输使用 `--partial --info=progress2`，首次同步不使用 `--delete`。

同一个队列不能在本机与服务器同时运行。服务器开始写入后，本机对应队列保持停止状态。

## 10. Overleaf 发布

### 10.1 默认方式：每批一个 ZIP

本地 XeLaTeX 编译成功并确认正式 TEX 已发布后，将该批次目录原样打包到：

```text
/srv/pdftotex/overleaf/outbox/<批次号>.zip
```

ZIP 保留子目录和被 TEX 引用的资源，不包含 `.pipeline`、模型日志、API 调用记录、缓存数据库或 `.env`。操作者在 Overleaf 使用 `New Project -> Upload Project`，一个 ZIP 创建一个项目。

### 10.2 可选方式：Overleaf Git

Premium Git integration 可自动更新已有项目。每个批次项目需要预先创建并记录项目 Git URL；上传器执行 `pull --rebase`、同步批次目录、提交和 push。Git token 只保存在服务器凭据存储中。

Overleaf Cloud 没有用于无人值守更新已有项目的公开上传 API，因此没有 Premium Git 时保留人工上传 ZIP 的最后一步。

## 11. 故障处理与回滚

- 镜像构建失败：保留本机构建与生产环境，不进入数据切换。
- canary 失败：停止服务器容器，保留 `/srv/pdftotex/data/fix/server-migration/` 的日志和产物，本机继续运行。
- 模型 API 失败：先区分认证、限流、连接和服务端 5xx；认证错误不重启。
- 服务器任务已经产生新进度后回滚：只把对应队列、工作区和正式产物增量同步回本机，不用旧本机目录覆盖服务器新数据。
- systemd 监控失败：转换容器可以继续，但不启动下一批，先恢复监控。

回滚不删除服务器数据、不清理缓存、不覆盖本机已完成产物。

## 12. 验收标准

迁移完成必须同时满足：

1. `/srv/pdftotex` 可用空间不少于 30 GB，Swap 为 8 GB。
2. 三个镜像均为 `linux/amd64`。
3. 运行镜像中 `pip check`、Paddle 导入、`texopt-pipeline --help` 和 XeLaTeX 检查通过。
4. 容器测试全部通过。
5. 代表性 PDF 在独立 canary 工作区完成识别、协调、优化、两遍 XeLaTeX 编译和发布。
6. 代表性页与本机输出对比无新增表格越界、方向错误、重复页面或竖线断裂。
7. 实时监控和日报由 systemd timer 定时刷新。
8. 人工停止容器不会被错误恢复；临时模型服务错误遵守冷却和最多 3 次限制。
9. 重启服务器后 Docker、Mihomo 和两个监控 timer 自动恢复。
10. 一个完成批次可以生成 ZIP，并在 Overleaf 创建可编译项目。

通过以上检查后，服务器成为主运行环境；本机保留只读副本和紧急回退能力。
