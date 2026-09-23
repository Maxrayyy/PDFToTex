# PDFToTex Linux Server Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 PDFToTex 链路迁移到 Ubuntu 24 x86_64 服务器，使服务器可独立构建、运行、监控、恢复批次并生成 Overleaf 上传包。

**Architecture:** 代码通过 Git 和 Lexoid submodule 部署，输入与状态数据通过两阶段 rsync 迁移，服务器原生构建 AMD64 运行时与 worker 镜像。转换容器继续使用宿主机持久目录，实时监控和日报改由 systemd timer 调度，Overleaf 默认使用每批一个 ZIP 的人工上传模式。

**Tech Stack:** Ubuntu 24.04, Docker Engine/Compose/Buildx, Python 3.10 container runtime, Python 3.12 host monitoring, PaddlePaddle/PaddleOCR, XeLaTeX, systemd, rsync, Git, Mihomo.

**Spec:** `docs/superpowers/specs/2026-09-23-linux-server-migration-design.md`

## Global Constraints

- 生产架构固定为 `linux/amd64`。
- 服务器根目录固定为 `/srv/pdftotex`，其中 `PDFToTex`、`Downloads`、`data` 同级。
- 容器名使用 `pdftotex-<批次号>-<序号>`；U1/U2/U3 不进入容器名或镜像名。
- `Downloads` 只读挂载到 `/input`；`data` 可写挂载到 `/data`。
- 不迁移 ARM64 镜像、容器和 BuildKit 缓存。
- 不把 `.env`、Overleaf token、订阅地址或 API 密钥提交到 Git。
- 实时监控间隔固定为 120 秒，日报间隔固定为 600 秒。
- 自动重启冷却 360 秒，每份 PDF 最多 3 次；认证、OOM、编译错误和人工暂停不自动重启。
- 识别模型、提示词、并发、DPI、费用算法和 TEX 规则在迁移中保持不变。
- 当前 XeLaTeX 基线为 `TeX Live 2025/dev/Debian`；迁移不同时切换到 TeX Live 2024。
- 开始完整数据迁移前，`/srv/pdftotex` 可用空间必须不少于 120 GB。
- 任何真实队列启动后，都必须验证容器、systemd timer 和 `latest.md` 三项状态。
- 所有 Git 提交使用 `<type>: <中文简述>`，不推送、不改写历史，除非用户明确要求。

---

### Task 1: 补齐可复现的运行时镜像

**Files:**
- Create: `pipeline/docker/Dockerfile.runtime`
- Create: `scripts/verify-runtime-image.sh`
- Modify: `docker-compose.yml`
- Modify: `.dockerignore`
- Test: `pipeline/tests/test_environment_models.py`

**Interfaces:**
- Consumes: `Lexoid/pyproject.toml`, `Lexoid/poetry.lock`, `pipeline/docker/Dockerfile.hybrid`.
- Produces: `pdftotex-runtime:local`，包含 `/opt/venv`、Paddle 3.2.2、PaddleOCR、Chromium shell、XeLaTeX 和 Poppler。

- [ ] **Step 1: 在环境测试中声明运行时必须具备的能力**

在 `pipeline/tests/test_environment_models.py` 增加只检查公开能力的测试：

```python
def test_runtime_has_required_document_toolchain():
    import importlib.util
    import shutil

    assert importlib.util.find_spec("paddle") is not None
    assert importlib.util.find_spec("paddleocr") is not None
    assert importlib.util.find_spec("pypdfium2") is not None
    assert shutil.which("xelatex")
    assert shutil.which("pdftoppm")
```

- [ ] **Step 2: 在当前测试镜像运行新增测试并确认基线**

Run:

```bash
docker compose --profile test run --rm --no-deps tests \
  python -m pytest -q /tests/pipeline/tests/test_environment_models.py
```

Expected: 当前镜像通过；该结果定义新 AMD64 运行时必须保持的能力。

- [ ] **Step 3: 创建依赖层 Dockerfile**

`pipeline/docker/Dockerfile.runtime` 使用项目根目录作为 build context，按以下层次实现：

```dockerfile
ARG BASE_IMAGE=python:3.10.21-slim-trixie
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_HOME=/root/.paddle

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl git libgl1 libglib2.0-0 poppler-utils \
      texlive-xetex texlive-latex-extra texlive-lang-chinese \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
    && pip install --upgrade pip setuptools wheel \
    && pip install --index-url ${TORCH_INDEX_URL} torch

WORKDIR /src/Lexoid
COPY Lexoid/pyproject.toml Lexoid/poetry.lock Lexoid/README.md ./
RUN pip install poetry poetry-plugin-export \
    && poetry export --only main --without-hashes -f requirements.txt -o /tmp/requirements.txt \
    && sed -i '/^\(cuda-bindings\|cuda-toolkit\|nvidia-[a-z0-9-]*\|torch\|triton\)==/d' /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && playwright install --with-deps --only-shell chromium \
    && pip uninstall -y poetry poetry-plugin-export \
    && pip check

WORKDIR /data
```

保留 `Lexoid/poetry.lock` 的版本约束，不安装 CUDA 依赖，不复制源码或密钥。

- [ ] **Step 4: 在 Compose 增加只用于构建的 runtime 服务**

在 `docker-compose.yml` 增加：

```yaml
  runtime:
    build:
      context: .
      dockerfile: pipeline/docker/Dockerfile.runtime
      args:
        BASE_IMAGE: ${RUNTIME_BASE_IMAGE:-python:3.10.21-slim-trixie}
        PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}
        TORCH_INDEX_URL: ${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}
    image: pdftotex-runtime:local
    profiles: ["build"]
```

- [ ] **Step 5: 排除构建上下文中的数据和本机环境**

`.dockerignore` 至少包含：

```text
.git
**/.git
**/.venv*
**/__pycache__
**/.pytest_cache
data
Downloads
*.log
```

- [ ] **Step 6: 创建运行时验证脚本**

`scripts/verify-runtime-image.sh` 必须执行：

```bash
#!/usr/bin/env bash
set -euo pipefail
image="${1:-pdftotex-runtime:local}"
test "$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')" = "linux/amd64"
docker run --rm --entrypoint python "$image" -c \
  'import paddle, paddleocr, pypdfium2, cv2; print(paddle.__version__)'
docker run --rm --entrypoint sh "$image" -lc \
  'pip check && xelatex --version | head -1 && pdftoppm -v 2>&1 | head -1'
```

- [ ] **Step 7: 在 x86 服务器构建并验证运行时**

Run:

```bash
docker compose --profile build build runtime
bash scripts/verify-runtime-image.sh pdftotex-runtime:local
```

Expected: 镜像架构为 `linux/amd64`，Python 导入、`pip check`、XeLaTeX 和 Poppler 全部通过。

- [ ] **Step 8: 提交运行时构建入口**

```bash
git add .dockerignore docker-compose.yml pipeline/docker/Dockerfile.runtime \
  scripts/verify-runtime-image.sh pipeline/tests/test_environment_models.py
git commit -m "build: 增加可复现的AMD64运行时镜像"
```

### Task 2: 增加统一镜像构建与清单

**Files:**
- Create: `scripts/build-images.sh`
- Create: `scripts/image-manifest.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1 的 `runtime` Compose 服务和现有 `worker/tests` 服务。
- Produces: 三个本地镜像、带 Git SHA 的不可变标签和 `data/audits/images/<sha>.txt`。

- [ ] **Step 1: 创建顺序构建脚本**

`scripts/build-images.sh` 使用以下流程：

```bash
#!/usr/bin/env bash
set -euo pipefail
arch="$(docker info --format '{{.Architecture}}')"
test "$arch" = x86_64
sha="$(git rev-parse --short=12 HEAD)"
docker compose --profile build build runtime
docker compose build worker
docker compose --profile test build tests
docker tag pdftotex-runtime:local "pdftotex-runtime:${sha}-amd64"
docker tag pdftotex:local "pdftotex:${sha}-amd64"
docker tag pdftotex-test:local "pdftotex-test:${sha}-amd64"
bash scripts/verify-runtime-image.sh pdftotex-runtime:local
docker compose --profile test run --rm --no-deps tests
bash scripts/image-manifest.sh "$sha"
```

- [ ] **Step 2: 创建镜像清单脚本**

`scripts/image-manifest.sh` 写入 `../data/audits/images/<sha>.txt`，内容包含：

```text
git_sha=<主仓库 SHA>
lexoid_sha=<submodule SHA>
runtime_image=<镜像 ID 和架构>
worker_image=<镜像 ID 和架构>
test_image=<镜像 ID 和架构>
python=<版本>
xelatex=<版本首行>
paddle=<版本>
paddleocr=<版本>
```

写入采用临时文件加原子替换，且不输出 `.env`。

- [ ] **Step 3: 检查脚本语法**

```bash
bash -n scripts/build-images.sh scripts/image-manifest.sh scripts/verify-runtime-image.sh
```

Expected: 退出码 0。

- [ ] **Step 4: 更新构建文档**

README 的新机器构建步骤改为：

```bash
./scripts/build-images.sh
```

同时说明 ARM64 镜像不能传到 x86 服务器运行。

- [ ] **Step 5: 提交构建编排**

```bash
git add scripts/build-images.sh scripts/image-manifest.sh README.md
git commit -m "build: 统一运行时和工作镜像构建流程"
```

### Task 3: 增加 Linux systemd 监控

**Files:**
- Create: `deploy/systemd/pdftotex-realtime-monitor.service`
- Create: `deploy/systemd/pdftotex-realtime-monitor.timer`
- Create: `deploy/systemd/pdftotex-daily-stats.service`
- Create: `deploy/systemd/pdftotex-daily-stats.timer`
- Create: `scripts/install-systemd-monitoring.sh`
- Modify: `pipeline/docker/Dockerfile.hybrid`
- Test: `pipeline/tests/test_systemd_units.py`

**Interfaces:**
- Consumes: `/srv/pdftotex/PDFToTex` 和 `/srv/pdftotex/data/monitoring/*/config.json`。
- Produces: 两个常驻 timer；实时监控每 120 秒执行，日报每 600 秒执行。

- [ ] **Step 1: 添加失败测试，约束 timer 和命令路径**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_linux_monitoring_units_use_once_and_expected_intervals():
    realtime = (ROOT / "deploy/systemd/pdftotex-realtime-monitor.service").read_text()
    realtime_timer = (ROOT / "deploy/systemd/pdftotex-realtime-monitor.timer").read_text()
    daily = (ROOT / "deploy/systemd/pdftotex-daily-stats.service").read_text()
    daily_timer = (ROOT / "deploy/systemd/pdftotex-daily-stats.timer").read_text()
    assert "worker_watch.py once" in realtime
    assert "OnUnitActiveSec=120s" in realtime_timer
    assert "daily_stats.py once" in daily
    assert "OnUnitActiveSec=600s" in daily_timer
    assert "--scheduled" not in realtime
```

- [ ] **Step 2: 运行测试并确认失败**

```bash
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests \
  python -m pytest -q /tests/pipeline/tests/test_systemd_units.py
```

Expected: FAIL，因为 unit 文件尚不存在。

- [ ] **Step 3: 创建 realtime service 和 timer**

Service 关键内容：

```ini
[Service]
Type=oneshot
User=pdftotex
Group=pdftotex
WorkingDirectory=/srv/pdftotex/PDFToTex
ExecStart=/usr/bin/python3 /srv/pdftotex/PDFToTex/pipeline/texopt/monitoring/worker_watch.py once --config /srv/pdftotex/data/monitoring/realtime/config.json
```

Timer 关键内容：

```ini
[Timer]
OnBootSec=30s
OnUnitActiveSec=120s
AccuracySec=5s
Persistent=true
```

- [ ] **Step 4: 创建 daily service 和 timer**

Daily service 使用 `daily_stats.py once` 和日报配置；timer 使用 `OnUnitActiveSec=600s`、`Persistent=true`。

- [ ] **Step 5: 将 systemd 文件加入测试镜像**

在 `pipeline/docker/Dockerfile.hybrid` 的 test stage 增加：

```dockerfile
COPY deploy /tests/deploy
```

- [ ] **Step 6: 创建安装脚本**

`scripts/install-systemd-monitoring.sh` 必须：

```bash
#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
install -m 0644 deploy/systemd/pdftotex-*.service /etc/systemd/system/
install -m 0644 deploy/systemd/pdftotex-*.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/pdftotex-*.service \
  /etc/systemd/system/pdftotex-*.timer
systemctl daemon-reload
systemctl enable --now pdftotex-realtime-monitor.timer pdftotex-daily-stats.timer
```

- [ ] **Step 7: 运行测试和 systemd 静态检查**

```bash
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests \
  python -m pytest -q /tests/pipeline/tests/test_systemd_units.py
systemd-analyze verify deploy/systemd/*.service deploy/systemd/*.timer
```

Expected: 测试通过；systemd unit 无错误。

- [ ] **Step 8: 提交 Linux 监控**

```bash
git add deploy/systemd scripts/install-systemd-monitoring.sh \
  pipeline/docker/Dockerfile.hybrid pipeline/tests/test_systemd_units.py
git commit -m "feat: 增加Linux定时监控服务"
```

### Task 4: 增加 Overleaf 批次打包器

**Files:**
- Create: `scripts/package-overleaf-batch.py`
- Create: `pipeline/tests/test_overleaf_package.py`
- Modify: `pipeline/docker/Dockerfile.hybrid`
- Modify: `README.md`

**Interfaces:**
- Consumes: 一个完成批次的 `data/optimized/<相对批次目录>`。
- Produces: `/srv/pdftotex/overleaf/outbox/<批次号>.zip` 和同名 `.sha256`。

- [ ] **Step 1: 编写打包行为测试**

测试创建临时批次目录，包含一个 TEX 和一个引用资源，并断言：

```python
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


def test_package_preserves_relative_paths_and_excludes_runtime_state(tmp_path):
    publish = tmp_path / "optimized"
    batch = publish / "A37Z201202605030"
    (batch / "assets").mkdir(parents=True)
    (batch / ".pipeline").mkdir()
    (batch / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
    (batch / "assets" / "stamp.png").write_bytes(b"png")
    (batch / ".pipeline" / "private.log").write_text("private", encoding="utf-8")
    (batch / "secret.env").write_text("TOKEN=secret", encoding="utf-8")
    outbox = tmp_path / "outbox"

    subprocess.run([
        sys.executable,
        str(ROOT / "scripts" / "package-overleaf-batch.py"),
        "--publish-root", str(publish),
        "--batch-dir", str(batch),
        "--outbox", str(outbox),
    ], check=True)

    with ZipFile(outbox / "A37Z201202605030.zip") as archive:
        names = set(archive.namelist())
    assert names == {"main.tex", "assets/stamp.png"}
```

再增加两个失败场景：输入目录为空；目录中不存在 `.tex`。

- [ ] **Step 2: 运行测试并确认失败**

```bash
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests \
  python -m pytest -q /tests/pipeline/tests/test_overleaf_package.py
```

Expected: FAIL，因为脚本不存在。

- [ ] **Step 3: 实现安全打包**

`scripts/package-overleaf-batch.py` 使用 `pathlib` 和 `zipfile`：

- 输入必须位于配置的 `publish_root` 下。
- 拒绝符号链接和路径穿越。
- 至少包含一个 `.tex`。
- 排除 `.pipeline`、`.state`、`.cache`、`.env`、日志和 SQLite 文件。
- 临时 ZIP 写完后原子改名。
- 生成 SHA-256 文件。
- 同名 ZIP 已存在且哈希相同时幂等返回；内容变化时创建新 ZIP 后替换。

在 `pipeline/docker/Dockerfile.hybrid` 的 test stage 增加：

```dockerfile
COPY scripts /tests/scripts
```

CLI：

```text
python3 scripts/package-overleaf-batch.py \
  --publish-root /srv/pdftotex/data/optimized \
  --batch-dir /srv/pdftotex/data/optimized/<相对批次目录> \
  --outbox /srv/pdftotex/overleaf/outbox
```

- [ ] **Step 4: 运行测试**

```bash
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests \
  python -m pytest -q /tests/pipeline/tests/test_overleaf_package.py
```

Expected: 所有打包测试通过。

- [ ] **Step 5: 更新 Overleaf 文档并提交**

```bash
git add scripts/package-overleaf-batch.py pipeline/tests/test_overleaf_package.py \
  pipeline/docker/Dockerfile.hybrid README.md
git commit -m "feat: 增加Overleaf批次打包工具"
```

### Task 5: 扩容服务器并创建运行用户

**Files:**
- No repository changes.

**Interfaces:**
- Produces: 容量合格的 `/srv/pdftotex`、8 GB Swap、`pdftotex` 用户和 Docker 权限。

- [ ] **Step 1: 扩容或挂载数据盘**

将至少 200 GB 的 ext4 数据盘挂载到 `/srv/pdftotex`，写入 `/etc/fstab` 后验证：

```bash
findmnt /srv/pdftotex
test "$(df --output=avail -B1 /srv/pdftotex | tail -1)" -ge 128849018880
```

Expected: 挂载存在且可用空间至少 120 GiB。

- [ ] **Step 2: 配置 8 GB Swap**

```bash
fallocate -l 8G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show
```

- [ ] **Step 3: 创建专用用户和目录**

```bash
id pdftotex >/dev/null 2>&1 || useradd --create-home --shell /bin/bash pdftotex
usermod -aG docker pdftotex
install -d -o pdftotex -g pdftotex -m 0750 \
  /srv/pdftotex/PDFToTex \
  /srv/pdftotex/Downloads \
  /srv/pdftotex/data \
  /srv/pdftotex/overleaf/outbox \
  /srv/pdftotex/overleaf/projects
```

- [ ] **Step 4: 验证用户可访问 Docker**

```bash
runuser -u pdftotex -- docker info --format '{{.OSType}}/{{.Architecture}}'
```

Expected: `linux/x86_64`。

### Task 6: 发布代码并部署密钥

**Files:**
- No new repository files beyond Tasks 1-4.

**Interfaces:**
- Consumes: 已通过测试的本地提交和两个本地 `.env`。
- Produces: `/srv/pdftotex/PDFToTex` 工作树及权限为 `0600` 的服务器密钥文件。

- [ ] **Step 1: 在本机完成发布前检查**

```bash
git status --short
git diff --check
git -C Lexoid status --short
git submodule status
docker compose --profile test run --rm --no-deps tests
```

Expected: 两个仓库工作树干净，测试通过。

- [ ] **Step 2: 经用户明确授权后推送主仓库和必要的 Lexoid 提交**

主仓库引用的 submodule SHA 必须已经存在于 Lexoid 远端；随后推送主仓库。不要强制推送。

- [ ] **Step 3: 在服务器克隆代码**

```bash
runuser -u pdftotex -- git clone --recurse-submodules \
  https://github.com/Maxrayyy/PDFToTex.git /srv/pdftotex/PDFToTex
runuser -u pdftotex -- git -C /srv/pdftotex/PDFToTex submodule status
```

- [ ] **Step 4: 安全传输环境文件**

从本机执行，`PDFTOTEX_HOST` 必须由操作者在 shell 中设置：

```bash
cd /Users/dongdong/code/lexiod
: "${PDFTOTEX_HOST:?set PDFTOTEX_HOST to the SSH host}"
scp PDFToTex/Lexoid/.env \
  "root@${PDFTOTEX_HOST}:/srv/pdftotex/PDFToTex/Lexoid/.env"
scp PDFToTex/pipeline/texopt/.env \
  "root@${PDFTOTEX_HOST}:/srv/pdftotex/PDFToTex/pipeline/texopt/.env"
ssh "root@${PDFTOTEX_HOST}" \
  'chown pdftotex:pdftotex /srv/pdftotex/PDFToTex/Lexoid/.env /srv/pdftotex/PDFToTex/pipeline/texopt/.env && chmod 600 /srv/pdftotex/PDFToTex/Lexoid/.env /srv/pdftotex/PDFToTex/pipeline/texopt/.env'
```

- [ ] **Step 5: 验证密钥存在但不打印内容**

```bash
ssh "root@${PDFTOTEX_HOST}" \
  'stat -c "%a %U:%G %n" /srv/pdftotex/PDFToTex/Lexoid/.env /srv/pdftotex/PDFToTex/pipeline/texopt/.env'
```

Expected: 两个文件均为 `600 pdftotex:pdftotex`。

### Task 7: 执行第一阶段数据预同步

**Files:**
- No repository changes.

**Interfaces:**
- Consumes: 本机 `Downloads` 和 `data`。
- Produces: 服务器上可增量更新的完整副本。

- [ ] **Step 1: 记录源数据规模和文件数**

```bash
cd /Users/dongdong/code/lexiod
du -sh Downloads data
find Downloads -type f | wc -l
find data -type f | wc -l
```

- [ ] **Step 2: 预同步原始 PDF**

```bash
rsync -aH --partial --info=progress2 \
  Downloads/ "root@${PDFTOTEX_HOST}:/srv/pdftotex/Downloads/"
```

- [ ] **Step 3: 预同步全部数据**

```bash
rsync -aH --partial --info=progress2 \
  data/ "root@${PDFTOTEX_HOST}:/srv/pdftotex/data/"
```

首次同步不使用 `--delete`，不使用 `-z`。

- [ ] **Step 4: 修正服务器权限并比对规模**

```bash
ssh "root@${PDFTOTEX_HOST}" \
  'chown -R pdftotex:pdftotex /srv/pdftotex/Downloads /srv/pdftotex/data && du -sh /srv/pdftotex/Downloads /srv/pdftotex/data'
```

- [ ] **Step 5: 进行只读差异预览**

```bash
rsync -aHn --itemize-changes Downloads/ \
  "root@${PDFTOTEX_HOST}:/srv/pdftotex/Downloads/"
rsync -aHn --itemize-changes data/ \
  "root@${PDFTOTEX_HOST}:/srv/pdftotex/data/"
```

Expected: 除迁移期间新产生或更新的文件外无异常差异。

### Task 8: 转换服务器绝对路径并安装监控

**Files:**
- Modify on server: `/srv/pdftotex/data/monitoring/realtime/config.json`
- Modify on server: `/srv/pdftotex/data/monitoring/daily/config.json`
- Modify on server: `/srv/pdftotex/data/workers/queues/*.json`

**Interfaces:**
- Produces: 不包含 macOS 路径的 JSON 配置和已启用的 systemd timers。

- [ ] **Step 1: 备份原始 JSON 配置**

```bash
runuser -u pdftotex -- cp -a /srv/pdftotex/data/monitoring \
  /srv/pdftotex/data/monitoring.before-linux-migration
```

- [ ] **Step 2: 使用 Python 结构化更新 realtime 配置**

运行一次 Python 脚本，设置：

```python
config["docker"] = "/usr/bin/docker"
config["output_dir"] = "/srv/pdftotex/data/monitoring/realtime"
config["queue_dir"] = "/srv/pdftotex/data/workers/queues"
config["interval_seconds"] = 120
config["auto_restart"] = {
    "enabled": True,
    "cooldown_seconds": 360,
    "max_attempts": 3,
}
config["batch_status"]["source_root"] = "/srv/pdftotex/Downloads"
config["batch_status"]["publish_root"] = "/srv/pdftotex/data/optimized"
config["batch_status"]["work_root"] = "/srv/pdftotex/data/workers"
config["batch_status"]["output_file"] = "/srv/pdftotex/data/monitoring/batches.md"
```

同时将 `containers` 中的 `work_root/output_tex` 前缀转换到 `/srv/pdftotex/data`。

- [ ] **Step 3: 结构化更新 daily 配置**

设置：

```python
config["output_dir"] = "/srv/pdftotex/data/monitoring/daily"
config["scan_roots"] = ["/srv/pdftotex/data/workers"]
config["source_root"] = "/srv/pdftotex/Downloads"
config["publish_root"] = "/srv/pdftotex/data/optimized"
config["pricing_file"] = "/srv/pdftotex/PDFToTex/pipeline/texopt/model_prices.json"
config["path_map"] = {
    "/input": "/srv/pdftotex/Downloads",
    "/data": "/srv/pdftotex/data",
}
config["interval_seconds"] = 600
```

- [ ] **Step 4: 更新队列的宿主机路径**

对 `data/workers/queues/*.json` 中存在的 `host_data` 统一设置为：

```json
"host_data": "/srv/pdftotex/data"
```

容器路径 `/input` 和 `/data` 保持不变。

Steps 2-4 使用下面的结构化迁移程序一次完成；程序采用同目录临时文件和原子替换：

```bash
runuser -u pdftotex -- python3 - <<'PY'
import json
import os
from pathlib import Path

DATA = Path("/srv/pdftotex/data")
REPLACEMENTS = {
    "/Users/dongdong/code/lexiod/PDFToTex": "/srv/pdftotex/PDFToTex",
    "/Users/dongdong/code/lexiod/Downloads": "/srv/pdftotex/Downloads",
    "/Users/dongdong/code/lexiod/data": "/srv/pdftotex/data",
    "/Users/dongdong/.docker/bin/docker": "/usr/bin/docker",
}


def remap(value):
    if isinstance(value, dict):
        return {key: remap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [remap(item) for item in value]
    if isinstance(value, str):
        for source, target in REPLACEMENTS.items():
            if value.startswith(source):
                return target + value[len(source):]
    return value


def write_json(path, payload):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


realtime_path = DATA / "monitoring/realtime/config.json"
realtime = remap(json.loads(realtime_path.read_text(encoding="utf-8")))
realtime.update({
    "docker": "/usr/bin/docker",
    "output_dir": "/srv/pdftotex/data/monitoring/realtime",
    "queue_dir": "/srv/pdftotex/data/workers/queues",
    "interval_seconds": 120,
    "auto_restart": {
        "enabled": True,
        "cooldown_seconds": 360,
        "max_attempts": 3,
    },
})
realtime["batch_status"].update({
    "source_root": "/srv/pdftotex/Downloads",
    "publish_root": "/srv/pdftotex/data/optimized",
    "work_root": "/srv/pdftotex/data/workers",
    "output_file": "/srv/pdftotex/data/monitoring/batches.md",
})
write_json(realtime_path, realtime)

daily_path = DATA / "monitoring/daily/config.json"
daily = remap(json.loads(daily_path.read_text(encoding="utf-8")))
daily.update({
    "output_dir": "/srv/pdftotex/data/monitoring/daily",
    "scan_roots": ["/srv/pdftotex/data/workers"],
    "source_root": "/srv/pdftotex/Downloads",
    "publish_root": "/srv/pdftotex/data/optimized",
    "pricing_file": "/srv/pdftotex/PDFToTex/pipeline/texopt/model_prices.json",
    "path_map": {
        "/input": "/srv/pdftotex/Downloads",
        "/data": "/srv/pdftotex/data",
    },
    "interval_seconds": 600,
})
write_json(daily_path, daily)

for queue_path in sorted((DATA / "workers/queues").glob("*.json")):
    queue = remap(json.loads(queue_path.read_text(encoding="utf-8")))
    if "host_data" in queue:
        queue["host_data"] = "/srv/pdftotex/data"
    write_json(queue_path, queue)
PY
```

- [ ] **Step 5: 检查不再残留 macOS 绝对路径**

```bash
find /srv/pdftotex/data/monitoring /srv/pdftotex/data/workers/queues \
  -type f -name '*.json' -print0 | \
  xargs -0 grep -nE '/Users/dongdong|\.docker/bin/docker'
```

Expected: 无输出。

- [ ] **Step 6: 安装并验证 systemd timers**

```bash
cd /srv/pdftotex/PDFToTex
sudo ./scripts/install-systemd-monitoring.sh
systemctl is-active pdftotex-realtime-monitor.timer
systemctl is-active pdftotex-daily-stats.timer
systemctl list-timers 'pdftotex-*'
```

Expected: 两个 timer 均为 `active`。

### Task 9: 构建 AMD64 镜像并运行完整测试

**Files:**
- Writes audit output under `/srv/pdftotex/data/audits/images/`.

**Interfaces:**
- Produces: 经过测试的三个 AMD64 镜像。

- [ ] **Step 1: 检查网络和磁盘**

```bash
df -h /srv/pdftotex
curl -fsS --max-time 20 https://pypi.org/simple/ >/dev/null
docker pull hello-world:latest
```

- [ ] **Step 2: 构建全部镜像**

```bash
cd /srv/pdftotex/PDFToTex
runuser -u pdftotex -- ./scripts/build-images.sh
```

- [ ] **Step 3: 检查架构和入口**

```bash
for image in pdftotex-runtime:local pdftotex:local pdftotex-test:local; do
  docker image inspect "$image" --format '{{.RepoTags}} {{.Os}}/{{.Architecture}}'
done
docker run --rm --entrypoint texopt-pipeline pdftotex:local --help
```

Expected: 全部为 `linux/amd64`，CLI 正常输出帮助。

- [ ] **Step 4: 检查运行时版本清单**

确认审计文件中包含 Git SHA、submodule SHA、镜像 ID、Python、XeLaTeX 和 Paddle 版本，且不包含密钥。

### Task 10: 执行真实 PDF canary

**Files:**
- Create runtime data: `/srv/pdftotex/data/fix/server-migration/`
- Consume: `Downloads/U3/20260808/A31Z201202603017/MX-C4081R_20260806_143050.pdf`

**Interfaces:**
- Produces: 独立 canary 工作区、TEX、PDF、结构日志和版面对比记录。

- [ ] **Step 1: 从典型页生成两页 canary PDF**

使用运行时镜像从原 PDF 提取第 1、7 页，并用新文件名避免复用已有主干缓存：

```bash
install -d -o pdftotex -g pdftotex \
  /srv/pdftotex/Downloads/_server-canary \
  /srv/pdftotex/data/fix/server-migration
docker run --rm \
  -v /srv/pdftotex/Downloads:/input:ro \
  -v /srv/pdftotex/Downloads/_server-canary:/output \
  --entrypoint sh pdftotex-runtime:local -lc '
    pdfseparate -f 1 -l 1 \
      /input/U3/20260808/A31Z201202603017/MX-C4081R_20260806_143050.pdf \
      /output/source-page-%d.pdf
    pdfseparate -f 7 -l 7 \
      /input/U3/20260808/A31Z201202603017/MX-C4081R_20260806_143050.pdf \
      /output/source-page-%d.pdf
    pdfunite /output/source-page-1.pdf /output/source-page-7.pdf \
      /output/MX-C4081R_20260806_143050-server-canary.pdf
    rm /output/source-page-1.pdf /output/source-page-7.pdf
  '
chown -R pdftotex:pdftotex /srv/pdftotex/Downloads/_server-canary
```

- [ ] **Step 2: 创建独立 canary 清单**

清单使用容器名 `pdftotex-server-canary-001`，只处理 `/input/_server-canary/MX-C4081R_20260806_143050-server-canary.pdf`，`host_data` 为 `/srv/pdftotex/data`，发布路径指向 `/data/fix/server-migration/published`，避免覆盖正式结果。

写入 `/srv/pdftotex/data/fix/server-migration/canary.json`：

```json
{
  "container": "pdftotex-server-canary-001",
  "monitor_config": "/data/monitoring/realtime/config.json",
  "host_data": "/srv/pdftotex/data",
  "source_root": "/input",
  "sources": [
    "/input/_server-canary/MX-C4081R_20260806_143050-server-canary.pdf"
  ]
}
```

- [ ] **Step 3: 运行 prepare-only**

```bash
docker compose run --rm --no-deps \
  -e VISION_CONCURRENCY=2 \
  -e RECONCILE_CONCURRENCY=2 \
  -e RENDER_DPI=240 \
  -e PIPELINE_PUBLISH_ROOT=/data/fix/server-migration/published \
  worker python /opt/pdftotex/run-sequential-queue.py \
  /data/fix/server-migration/canary.json --prepare-only
```

Expected: 页数、模型、DPI、并发和路径校验通过，不调用模型。

- [ ] **Step 4: 启动 canary 和实时监控**

```bash
docker compose run -d --no-deps --name pdftotex-server-canary-001 \
  -e VISION_CONCURRENCY=2 \
  -e RECONCILE_CONCURRENCY=2 \
  -e RENDER_DPI=240 \
  -e PIPELINE_PUBLISH_ROOT=/data/fix/server-migration/published \
  worker python -u /opt/pdftotex/run-sequential-queue.py \
  /data/fix/server-migration/canary.json
systemctl start pdftotex-realtime-monitor.service
```

- [ ] **Step 5: 验证运行状态**

```bash
docker inspect --format '{{.State.Status}}' pdftotex-server-canary-001
systemctl is-active pdftotex-realtime-monitor.timer
stat /srv/pdftotex/data/monitoring/realtime/latest.md
```

Expected: 容器运行、timer active、`latest.md` 在 120 秒内更新并包含 canary。

- [ ] **Step 6: 完成后执行结构和版面检查**

检查：

- 队列状态为 done，容器退出码为 0。
- 两遍 XeLaTeX 编译通过。
- 正式 canary TEX 与工作产物哈希一致。
- 两页 canary 输出分别与原 PDF 第 1、7 页及本机输出对比。
- 无新增表格越界、横竖方向错误、重复页面和竖线断裂。
- 手写内容错误只记录，不作为迁移失败；印刷内容和表格位置必须可接受。

- [ ] **Step 7: 记录 canary 结果**

写入 `/srv/pdftotex/data/fix/server-migration/report.md`，包含镜像 ID、容器名、源文件、页数、阶段耗时、编译结果和人工检查页。

### Task 11: 最终增量同步与生产切换

**Files:**
- No repository changes.

**Interfaces:**
- Produces: 单写服务器生产环境；本机作为只读回退副本。

- [ ] **Step 1: 停止本机转换容器和监控调度**

停止所有仍在写入目标数据的本机容器，卸载实时监控和日报调度。确认没有 `running` 的 PDFToTex 容器。

- [ ] **Step 2: 执行最终增量同步**

```bash
rsync -aH --partial --info=progress2 Downloads/ \
  "root@${PDFTOTEX_HOST}:/srv/pdftotex/Downloads/"
rsync -aH --partial --info=progress2 data/ \
  "root@${PDFTOTEX_HOST}:/srv/pdftotex/data/"
```

仍不使用 `--delete`。同步后重新执行 Task 8 的路径转换，因为本机 JSON 可能覆盖服务器版本。

- [ ] **Step 3: 重新启动两个监控 timer**

```bash
systemctl restart pdftotex-realtime-monitor.timer pdftotex-daily-stats.timer
systemctl start pdftotex-realtime-monitor.service pdftotex-daily-stats.service
```

- [ ] **Step 4: 启动一个真实批次**

使用 `pdftotex-<批次号>-001` 命名，先 prepare-only，再后台启动。不要启动同一批次的本机容器。

- [ ] **Step 5: 执行三项启动验收**

```bash
docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME:?set CONTAINER_NAME}"
systemctl is-active pdftotex-realtime-monitor.timer
rg -n "$CONTAINER_NAME" /srv/pdftotex/data/monitoring/realtime/latest.md
```

Expected: running、active、监控文件包含容器。

- [ ] **Step 6: 重启服务器验证自动恢复**

在没有转换容器写入时安排一次重启；重连后检查：

```bash
systemctl is-active docker mihomo \
  pdftotex-realtime-monitor.timer pdftotex-daily-stats.timer
```

Expected: 全部 active。

### Task 12: 验证 Overleaf 批次交付

**Files:**
- Writes: `/srv/pdftotex/overleaf/outbox/<批次号>.zip`

**Interfaces:**
- Consumes: 一个已经完成并人工抽查的正式批次目录。
- Produces: 可由 Overleaf `Upload Project` 创建项目的 ZIP。

- [ ] **Step 1: 生成批次 ZIP**

```bash
runuser -u pdftotex -- python3 /srv/pdftotex/PDFToTex/scripts/package-overleaf-batch.py \
  --publish-root /srv/pdftotex/data/optimized \
  --batch-dir "${BATCH_DIR:?set BATCH_DIR to a completed batch directory}" \
  --outbox /srv/pdftotex/overleaf/outbox
```

- [ ] **Step 2: 检查 ZIP 内容和哈希**

```bash
unzip -l "/srv/pdftotex/overleaf/outbox/${BATCH_ID:?set BATCH_ID}.zip"
sha256sum -c "/srv/pdftotex/overleaf/outbox/${BATCH_ID}.zip.sha256"
```

Expected: TEX 和引用资源存在；无 `.env`、日志、SQLite、缓存或工作状态。

- [ ] **Step 3: 在 Overleaf 创建项目并编译**

使用 `New Project -> Upload Project` 上传 ZIP，选择主 TEX 并编译。记录项目 URL、主 TEX 和编译结果到该批次审核记录，GitHub 仓库中不保存 Overleaf token。

### Task 13: 最终验收和迁移记录

**Files:**
- Create runtime record: `/srv/pdftotex/data/audits/server-migration-2026-09-23.md`
- Modify: `README.md`

**Interfaces:**
- Produces: 可审计的迁移结果和 Linux 日常命令说明。

- [ ] **Step 1: 运行最终检查**

```bash
df -h /srv/pdftotex
swapon --show
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}'
docker compose --profile test run --rm --no-deps tests
systemctl list-timers 'pdftotex-*'
systemctl --no-pager --full status docker mihomo \
  pdftotex-realtime-monitor.timer pdftotex-daily-stats.timer
```

- [ ] **Step 2: 写迁移记录**

记录日期、服务器 OS/架构、磁盘、Swap、Git SHA、submodule SHA、镜像 ID、canary 结果、监控状态、首个生产批次和 Overleaf ZIP 哈希。不得记录 API key、订阅内容或 Overleaf token。

- [ ] **Step 3: 更新 README 的 Linux 运行命令**

增加以下内容：

- `/srv/pdftotex` 目录结构。
- `./scripts/build-images.sh`。
- systemd timer 查看与重启命令。
- Linux 队列示例中的 `host_data`。
- Overleaf ZIP 生成命令。
- 本机 `launchd` 与 Linux `systemd` 的平台差异。

- [ ] **Step 4: 验证文档和工作树**

```bash
git diff --check
git status --short
git -C Lexoid status --short
```

- [ ] **Step 5: 提交迁移文档更新**

```bash
git add README.md
git commit -m "docs: 补充Linux服务器运行与迁移说明"
```

## Rollback Procedure

canary 或首个生产批次失败时执行：

```bash
docker stop --time 60 "${CONTAINER_NAME:?set CONTAINER_NAME}"
systemctl stop pdftotex-realtime-monitor.timer pdftotex-daily-stats.timer
```

保持服务器数据不动。只将服务器产生的新队列、对应 `data/workers/<stem>`、正式批次目录和监控状态 rsync 回本机，再使用本机已验证镜像恢复。不得用本机旧 `data` 全量覆盖服务器。

回滚完成后，在迁移记录中写明失败阶段、容器退出码、OOM 状态、模型错误类型和保留日志路径。
