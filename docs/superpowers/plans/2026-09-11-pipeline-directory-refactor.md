# PDFToTex Pipeline Directory Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将流水线目录收敛为职责清晰的 `pipeline/` 与 `docker/` 结构，同时保持现有 PDF 转 TeX、JSON、监控和容器命令兼容。

**Architecture:** Python 流水线源码使用 `texopt` 导入名，分发名为 `pdftotex-pipeline`，源码目录提升为项目级 `pipeline/`；生产模块保留在一个稳定包内，测试和 Docker 入口独立归档。根 Compose 作为唯一生产入口。

**Tech Stack:** Python 3.10、setuptools、Docker Compose v2、PaddleOCR、XeLaTeX、pytest。

**Spec:** `docs/ARCHITECTURE.md`

## Global Constraints

- 保留命令：`texopt`、`texopt-pipeline`。
- 保留运行数据挂载：与 `PDFToTex` 平级的 `data/workers`、`data/optimized`、`data/monitoring`；原始 PDF 位于同级 `Downloads/`，这些目录均不纳入 Git。
- 基础镜像固定为 `pdftotex-runtime:local`，运行镜像为 `pdftotex:local`。
- 不改变纯视觉识别和 JSON 导出行为。
- 每个任务完成后运行 `git diff --check` 和受影响测试，并创建中文提交。
- 子仓库迁移完成后由父仓库统一提交；未经明确要求不 push 原子仓库或总仓库。

> 本计划对应的目录重构已于 2026-09-11 完成。下面保留迁移决策和验证记录，
> 不再把已完成步骤当作待执行任务；后续修改应以当前目录和根 Compose 为准。

### Task 0: 解除流水线嵌套 Git 归属

**Files:**
- Modify: 父仓库索引 `pipeline`
- Remove: `pipeline/.git`

- [x] 记录 `pipeline` 当前 HEAD 和远程地址，确保历史可追溯。
- [x] 将 `pipeline` 纳入父仓库，保留工作树文件和忽略规则。
- [x] 保留 `Lexoid/.git` 独立维护，不纳入父仓库提交内容。
- [x] 检查父仓库索引不再以 `160000` 模式记录 `pipeline`。
- [x] 提交：`refactor: 纳入流水线源码到总仓库`。

### Task 1: 完成 texopt 包目录迁移

**Files:**
- Rename: `pipeline/texopt/` -> `pipeline/`
- Modify: `Dockerfile.hybrid`, `Dockerfile.streamlit`, `app.py`, `tests/*.py`
- Test: `pipeline/test_*.py`, `tests/test_*.py`

- [x] 更新所有 `files.*` 导入和路径为 `texopt.*`。
- [x] 更新 setuptools 的 `package-dir` 和 Docker COPY 路径。
- [x] 运行 Python 编译检查和流水线单元测试。
- [x] 提交：`refactor: 收敛流水线包目录`。

### Task 2: 集中 Docker 入口

**Files:**
- Move: `Dockerfile.hybrid`, `Dockerfile.streamlit`, `docker-compose.yml` -> `docker/`
- Modify: 根 `docker-compose.yml`、README、AGENT.md、构建脚本

- [x] 更新 Compose build context、Dockerfile 路径和 COPY 路径。
- [x] 保持 `worker` 服务、`pdftotex:local` 标签和 `/data` 挂载不变。
- [x] 运行 `docker compose config`，确认根 Compose 是生产入口。
- [x] 运行 `docker compose build worker`。
- [x] 提交：`refactor: 集中 Docker 构建入口`。

### Task 3: 稳定生产包边界

**Files:**
- Keep: 生产模块位于 `pipeline/texopt/`
- Modify: 包说明和测试路径

- [x] 保持现有公开命令和 Python 导入兼容。
- [x] 将测试移至 `pipeline/tests/unit/`，将 Docker 入口移至 `pipeline/docker/`。
- [x] 运行 Python 编译检查和 Compose 配置检查。

### Task 4: 删除重复入口并更新文档

**Files:**
- Remove: 仅被旧入口引用的子目录 Compose、重复 README 和旧 Dockerfile
- Modify: `README.md`, `AGENT.md`, `docs/ARCHITECTURE.md`

- [x] 用 `rg` 区分生产队列入口和可选网页入口；保留仍有用途的网页入口。
- [x] 文档同步根 Compose、队列启动、监控和 JSON 导出命令。
- [x] 运行 `docker compose config`、`texopt-pipeline --help` 和监控 smoke check。
- [x] 提交：`docs: 更新目录和运行指南`。

### Task 5: 全链路验证

- [x] 构建 `pdftotex:local`。
- [x] 运行流水线测试和 Lexoid 兼容测试。
- [x] 使用队列执行 `--prepare-only`，确认队列、JSON 和工作区路径不变。
- [x] 检查 `git status`、`git diff --check`，确认没有密钥和生成产物进入提交。
- [x] 使用真实 PDF 完成识别、协调、优化和双遍 XeLaTeX 编译回归。
