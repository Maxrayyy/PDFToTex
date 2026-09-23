#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sha="${1:-$(git -C "$repo_root" rev-parse --short=12 HEAD)}"
data_root="${PDFTOTEX_DATA_ROOT:-$repo_root/../data}"
output_dir="$data_root/audits/images"
output_file="$output_dir/$sha.txt"

mkdir -p "$output_dir"
temporary="$(mktemp "$output_dir/.${sha}.XXXXXX.tmp")"
trap 'rm -f "$temporary"' EXIT

image_info() {
  docker image inspect "$1" --format '{{.Id}} {{.Os}}/{{.Architecture}}'
}

runtime_python="$(docker run --rm --entrypoint python pdftotex-runtime:local --version 2>&1)"
runtime_xelatex="$(docker run --rm --entrypoint sh pdftotex-runtime:local -lc 'xelatex --version | head -1')"
runtime_paddle="$(docker run --rm --entrypoint python pdftotex-runtime:local -c 'import paddle; print(paddle.__version__)')"
runtime_paddleocr="$(docker run --rm --entrypoint python pdftotex-runtime:local -c 'from importlib.metadata import version; print(version("paddleocr"))')"

{
  printf 'git_sha=%s\n' "$(git -C "$repo_root" rev-parse HEAD)"
  printf 'lexoid_sha=%s\n' "$(git -C "$repo_root/Lexoid" rev-parse HEAD)"
  printf 'runtime_image=%s\n' "$(image_info pdftotex-runtime:local)"
  printf 'worker_image=%s\n' "$(image_info pdftotex:local)"
  printf 'test_image=%s\n' "$(image_info pdftotex-test:local)"
  printf 'python=%s\n' "$runtime_python"
  printf 'xelatex=%s\n' "$runtime_xelatex"
  printf 'paddle=%s\n' "$runtime_paddle"
  printf 'paddleocr=%s\n' "$runtime_paddleocr"
  printf 'pip_freeze_begin\n'
  docker run --rm --entrypoint python pdftotex-runtime:local -m pip freeze
  printf 'pip_freeze_end\n'
} >"$temporary"

chmod 0640 "$temporary"
mv -f "$temporary" "$output_file"
trap - EXIT
printf '%s\n' "$output_file"
