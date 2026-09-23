#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'run this installer as root\n' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
deployment_root="${PDFTOTEX_ROOT:-$(cd "$repo_root/.." && pwd)}"
unit_dir=/etc/systemd/system

if [[ "$deployment_root" != /* ]]; then
  printf 'PDFTOTEX_ROOT must be an absolute path: %s\n' "$deployment_root" >&2
  exit 1
fi

escaped_root="${deployment_root//\\/\\\\}"
escaped_root="${escaped_root//&/\\&}"
escaped_root="${escaped_root//|/\\|}"
render_dir="$(mktemp -d)"
trap 'rm -rf "$render_dir"' EXIT

for source in "$repo_root"/deploy/systemd/pdftotex-*.service; do
  target="$render_dir/$(basename "$source")"
  sed "s|@PDFTOTEX_ROOT@|$escaped_root|g" "$source" > "$target"
  install -m 0644 "$target" "$unit_dir/"
done
install -m 0644 "$repo_root"/deploy/systemd/pdftotex-*.timer "$unit_dir/"
systemd-analyze verify \
  "$unit_dir"/pdftotex-*.service \
  "$unit_dir"/pdftotex-*.timer
systemctl daemon-reload
systemctl enable --now \
  pdftotex-realtime-monitor.timer \
  pdftotex-daily-stats.timer
