#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sha="${1:-$(git -C "$repo_root" rev-parse --short=12 HEAD)}"
data_root="${PDFTOTEX_DATA_ROOT:-$repo_root/../data}"
output_dir="$data_root/audits/images"
runtime_image="${PDFTOTEX_RUNTIME_IMAGE:-pdftotex-runtime:local}"
worker_image="${PDFTOTEX_WORKER_IMAGE:-pdftotex:local}"
test_image="${PDFTOTEX_TEST_IMAGE:-pdftotex-test:local}"
architecture="${2:-$(docker image inspect "$runtime_image" --format '{{.Architecture}}')}"
output_file="$output_dir/$sha-$architecture.txt"

mkdir -p "$output_dir"
temporary="$(mktemp "$output_dir/.${sha}.XXXXXX.tmp")"
trap 'rm -f "$temporary"' EXIT

image_info() {
  docker image inspect "$1" --format '{{.Id}} {{.Os}}/{{.Architecture}}'
}

runtime_python="$(docker run --rm --entrypoint python "$runtime_image" --version 2>&1)"
runtime_xelatex="$(docker run --rm --entrypoint sh "$runtime_image" -lc 'xelatex --version | head -1')"
runtime_paddle="$(docker run --rm --entrypoint python "$runtime_image" -c 'import paddle; print(paddle.__version__)')"
runtime_paddleocr="$(docker run --rm --entrypoint python "$runtime_image" -c 'from importlib.metadata import version; print(version("paddleocr"))')"

{
  printf 'git_sha=%s\n' "$(git -C "$repo_root" rev-parse HEAD)"
  printf 'lexoid_sha=%s\n' "$(git -C "$repo_root/Lexoid" rev-parse HEAD)"
  printf 'runtime_image=%s\n' "$(image_info "$runtime_image")"
  printf 'worker_image=%s\n' "$(image_info "$worker_image")"
  printf 'test_image=%s\n' "$(image_info "$test_image")"
  printf 'python=%s\n' "$runtime_python"
  printf 'xelatex=%s\n' "$runtime_xelatex"
  printf 'paddle=%s\n' "$runtime_paddle"
  printf 'paddleocr=%s\n' "$runtime_paddleocr"
  printf 'pip_freeze_begin\n'
  docker run --rm --entrypoint python "$runtime_image" -m pip freeze
  printf 'pip_freeze_end\n'
} >"$temporary"

chmod 0640 "$temporary"
mv -f "$temporary" "$output_file"
trap - EXIT
printf '%s\n' "$output_file"
