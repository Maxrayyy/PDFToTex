#!/usr/bin/env bash
# 用途：为 Lexoid 更新 Poetry 依赖锁文件，不是更新所有子项目的锁文件。
# 运行方法（在 PDFToTex 根目录执行）：bash scripts/update-lock.sh
# 也可从其他目录使用脚本绝对路径运行；项目根目录自动根据脚本位置计算。
# 前提：可用的 Bash 和 Python 3.11；首次安装 Poetry 或重新解析依赖可能需要联网。
# 可通过环境变量指定解释器和 Poetry 版本，例如：
#   PYTHON_BIN=/absolute/path/to/python3.11 POETRY_VERSION=2.4.3 bash scripts/update-lock.sh
# 路径：专用 Poetry 环境位于 PDFToTex/.venv-poetry，工作目录为 PDFToTex/Lexoid。
# 默认 PYTHON_BIN=python3.11、POETRY_VERSION=2.4.3，无需配置输入文件。
# 注意：只有专用环境中不存在可执行的 poetry 时才创建环境并安装指定版本；
# 修改 POETRY_VERSION 不会自动升级已存在的 Poetry 环境。
# 副作用：可能创建 .venv-poetry、安装工具，并更新 Lexoid/poetry.lock。
# 运行后请检查锁文件差异；本脚本不提交代码，也不推送远程仓库。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POETRY_ENV="${ROOT_DIR}/.venv-poetry"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
POETRY_VERSION="${POETRY_VERSION:-2.4.3}"

if [[ ! -x "${POETRY_ENV}/bin/poetry" ]]; then
  "${PYTHON_BIN}" -m venv "${POETRY_ENV}"
  "${POETRY_ENV}/bin/python" -m pip install "poetry==${POETRY_VERSION}"
fi

cd "${ROOT_DIR}/Lexoid"
"${POETRY_ENV}/bin/poetry" lock
