# PDFToTex Pipeline Directory Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将流水线目录收敛为职责清晰的 `pipeline/` 与 `docker/` 结构，同时保持现有 PDF 转 TeX、JSON、监控和容器命令兼容。

**Architecture:** Python 流水线源码使用 `texopt` 导入名，分发名为 `pdftotex-pipeline`，源码目录提升为项目级 `pipeline/`；识别、优化、监控模块按职责分组。Docker 文件集中到 `docker/`，根 Compose 作为唯一生产入口。

**Tech Stack:** Python 3.10、setuptools、Docker Compose v2、PaddleOCR、XeLaTeX、pytest。

**Spec:** `docs/ARCHITECTURE.md`

## Global Constraints

- 保留命令：`texopt`、`texopt-pipeline`。
- 保留运行数据挂载：与 `PDFToTex` 平级的 `data/workers`、`data/optimized`、`data/monitoring`；原始 PDF 位于同级 `Downloads/`，这些目录均不纳入 Git。
- 基础镜像固定为 `pdftotex-runtime:local`，运行镜像为 `pdftotex:local`。
- 不改变纯视觉识别和 JSON 导出行为。
- 每个任务完成后运行 `git diff --check` 和受影响测试，并创建中文提交。
- 子仓库迁移完成后由父仓库统一提交；未经明确要求不 push 原子仓库或总仓库。

### Task 0: 解除流水线嵌套 Git 归属

**Files:**
- Modify: 父仓库索引 `lexiod-pipeline`
- Remove: `lexiod-pipeline/.git`

- [ ] 记录 `lexiod-pipeline` 当前 HEAD 和远程地址，确保历史可追溯。
- [ ] 在父仓库执行 `git rm --cached` 移除 `lexiod-pipeline` submodule 索引项，保留工作树文件。
- [ ] 删除 `lexiod-pipeline/.git` 元数据，不删除源码和数据文件；保留 `Lexoid/.git` 不变。
- [ ] 将 `lexiod-pipeline` 作为普通目录加入父仓库，并检查 `.gitignore` 不会纳入密钥、缓存和生成产物。
- [ ] 运行 `git ls-files --stage`，确认不再出现模式 `160000`。
- [ ] 提交：`refactor: 纳入流水线源码到总仓库`。

### Task 1: 完成 texopt 包目录迁移

**Files:**
- Rename: `lexiod-pipeline/files/` -> `pipeline/`
- Modify: `Dockerfile.hybrid`, `Dockerfile.streamlit`, `app.py`, `tests/*.py`
- Test: `pipeline/test_*.py`, `tests/test_*.py`

- [ ] 更新所有 `files.*` 导入和路径为 `texopt.*`。
- [ ] 更新 setuptools 的 `package-dir` 和 Docker COPY 路径。
- [ ] 运行 `python -m compileall pipeline`，确认包可编译。
- [ ] 运行流水线单元测试。
- [ ] 提交：`refactor: 收敛流水线包目录`。

### Task 2: 集中 Docker 入口

**Files:**
- Move: `Dockerfile.hybrid`, `Dockerfile.streamlit`, `docker-compose.yml` -> `docker/`
- Modify: 根 `docker-compose.yml`、README、AGENT.md、构建脚本

- [ ] 更新 Compose build context、Dockerfile 路径和 COPY 路径。
- [ ] 保持 `worker` 服务、`pdftotex:local` 标签和 `/data` 挂载不变。
- [ ] 运行 `docker compose config`，确认只有根 Compose 作为生产入口。
- [ ] 运行 `docker compose build worker`。
- [ ] 提交：`refactor: 集中 Docker 构建入口`。

### Task 3: 按职责归类流水线模块

**Files:**
- Move: 识别、优化、监控模块到 `pipeline/recognition`、`pipeline/optimization`、`pipeline/monitoring`
- Modify: 包内相对导入、CLI 注册、测试路径、监控命令文档

- [ ] 先为每个目标包增加 `__init__.py`，再逐组移动模块。
- [ ] 保持现有公开命令和 Python 导入兼容。
- [ ] 运行识别、优化、监控三组测试。
- [ ] 提交：`refactor: 按职责整理流水线模块`。

### Task 4: 删除重复入口并更新文档

**Files:**
- Remove: 仅被旧入口引用的子目录 Compose、重复 README 和旧 Dockerfile
- Modify: `README.md`, `AGENT.md`, `docs/ARCHITECTURE.md`

- [ ] 删除前用 `rg` 确认没有生产命令引用。
- [ ] 文档只保留根 Compose、队列启动、监控和 JSON 导出命令。
- [ ] 运行文档中的 `docker compose config`、`texopt-pipeline --help` 和监控 smoke check。
- [ ] 提交：`docs: 更新目录和运行指南`。

### Task 5: 全链路验证

- [ ] 构建 `pdftotex:local`。
- [ ] 运行流水线测试和 Lexoid 兼容测试。
- [ ] 使用一个小型 PDF 执行 `--prepare-only`，确认队列、JSON 和工作区路径不变。
- [ ] 检查 `git status`、`git diff --check`，确认没有密钥和生成产物进入提交。
- [ ] 提交：`test: 验证目录重构后的转译链路`。
