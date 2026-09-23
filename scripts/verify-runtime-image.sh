#!/usr/bin/env bash
set -euo pipefail

image="${1:-pdftotex-runtime:local}"
platform="$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')"

if [[ "$platform" != "linux/amd64" ]]; then
  printf 'expected linux/amd64 image, got %s\n' "$platform" >&2
  exit 1
fi

docker run --rm --entrypoint python "$image" -c \
  'import cv2, paddle, paddleocr, pypdfium2; print(paddle.__version__)'
docker run --rm --entrypoint sh "$image" -lc \
  'pip check && xelatex --version | head -1 && pdftoppm -v 2>&1 | head -1'
