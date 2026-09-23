#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

architecture="$(docker info --format '{{.Architecture}}')"
if [[ "$architecture" != "x86_64" ]]; then
  printf 'server build requires x86_64 Docker, got %s\n' "$architecture" >&2
  exit 1
fi

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
