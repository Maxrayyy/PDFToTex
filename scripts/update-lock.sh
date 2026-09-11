#!/usr/bin/env bash
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
