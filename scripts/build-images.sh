#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

target="${1:-}"
if [[ -z "$target" ]]; then
  case "$(uname -s)" in
    Darwin) target=macos ;;
    Linux) target=linux ;;
    *) printf 'unsupported host OS\n' >&2; exit 1 ;;
  esac
fi
case "$target" in
  macos|linux) ;;
  *) printf 'usage: %s [macos|linux]\n' "$0" >&2; exit 2 ;;
esac
source "${PDFTOTEX_PLATFORM_CONFIG:-$repo_root/deploy/platforms/$target.conf}"
export DOCKER_DEFAULT_PLATFORM="${PDFTOTEX_PLATFORM:-$DEFAULT_PLATFORM}"
case "$DOCKER_DEFAULT_PLATFORM" in
  linux/amd64|linux/arm64) ;;
  *) printf 'unsupported platform: %s\n' "$DOCKER_DEFAULT_PLATFORM" >&2; exit 1 ;;
esac
architecture="$(docker info --format '{{.Architecture}}')"
case "$architecture" in
  x86_64|amd64) architecture=amd64 ;;
  aarch64|arm64) architecture=arm64 ;;
esac
if [[ "$DOCKER_DEFAULT_PLATFORM" != "linux/$architecture" ]]; then
  printf 'native build requires %s Docker; current engine is linux/%s\n' "$DOCKER_DEFAULT_PLATFORM" "$architecture" >&2
  exit 1
fi

reuse_runtime="${2:-}"
if [[ -n "$reuse_runtime" && "$reuse_runtime" != --reuse-runtime ]]; then
  printf 'usage: %s [macos|linux] [--reuse-runtime]\n' "$0" >&2
  exit 2
fi
runtime_image="${PDFTOTEX_RUNTIME_IMAGE:-pdftotex-runtime:local}"
worker_image="${PDFTOTEX_WORKER_IMAGE:-pdftotex:local}"
test_image="${PDFTOTEX_TEST_IMAGE:-pdftotex-test:local}"
sha="$(git rev-parse --short=12 HEAD)"
if ! git diff --quiet HEAD; then sha="$sha-dirty"; fi

if [[ "$reuse_runtime" != --reuse-runtime ]]; then
  docker compose --profile build build runtime
fi
bash scripts/verify-runtime-image.sh "$runtime_image" "$DOCKER_DEFAULT_PLATFORM"
docker compose build worker
docker compose --profile test build tests

docker tag "$runtime_image" "pdftotex-runtime:${sha}-${architecture}"
docker tag "$worker_image" "pdftotex:${sha}-${architecture}"
docker tag "$test_image" "pdftotex-test:${sha}-${architecture}"

docker compose --profile test run --rm --no-deps tests
bash scripts/image-manifest.sh "$sha" "$architecture"
