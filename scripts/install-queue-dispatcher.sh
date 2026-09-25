#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'run this installer as root\n' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
deployment_root="${PDFTOTEX_ROOT:-$(cd "$repo_root/.." && pwd)}"
unit_dir=/etc/systemd/system
config="$deployment_root/data/operations/queue-dispatch.json"

if [[ "$deployment_root" != /* ]]; then
  printf 'PDFTOTEX_ROOT must be an absolute path: %s\n' "$deployment_root" >&2
  exit 1
fi
if [[ ! -f "$config" ]]; then
  printf 'queue dispatch config does not exist: %s\n' "$config" >&2
  exit 1
fi

escaped_root="${deployment_root//\\/\\\\}"
escaped_root="${escaped_root//&/\\&}"
escaped_root="${escaped_root//|/\\|}"
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT

sed "s|@PDFTOTEX_ROOT@|$escaped_root|g" \
  "$repo_root/deploy/systemd/pdftotex-queue-dispatch.service" > "$rendered"
install -m 0644 "$rendered" "$unit_dir/pdftotex-queue-dispatch.service"
install -m 0644 "$repo_root/deploy/systemd/pdftotex-queue-dispatch.timer" "$unit_dir/"
systemd-analyze verify \
  "$unit_dir/pdftotex-queue-dispatch.service" \
  "$unit_dir/pdftotex-queue-dispatch.timer"
systemctl daemon-reload
systemctl enable --now pdftotex-queue-dispatch.timer
