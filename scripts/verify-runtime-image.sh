#!/usr/bin/env bash
set -euo pipefail

image="${1:-pdftotex-runtime:local}"
platform="$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')"

expected="${2:-}"
if [[ -z "$expected" ]]; then
  architecture="$(docker info --format '{{.Architecture}}')"
  case "$architecture" in
    x86_64|amd64) expected=linux/amd64 ;;
    aarch64|arm64) expected=linux/arm64 ;;
    *) printf 'unsupported Docker architecture: %s\n' "$architecture" >&2; exit 1 ;;
  esac
fi
if [[ "$platform" != "$expected" ]]; then
  printf 'expected %s image, got %s\n' "$expected" "$platform" >&2
  exit 1
fi

docker run --rm --entrypoint python "$image" -c \
  'import cv2, paddle, paddleocr, pypdfium2; print(paddle.__version__)'
docker run --rm --entrypoint sh "$image" -lc \
  'pip check && xelatex --version | head -1 && pdftoppm -v 2>&1 | head -1'
