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

if ! command -v setfacl >/dev/null 2>&1; then
  printf 'setfacl is required; install the acl package first\n' >&2
  exit 1
fi

workers_root="$deployment_root/data/workers"
install -d -o pdftotex -g pdftotex -m 0750 "$workers_root"
setfacl -m u:pdftotex:rwx,d:u:pdftotex:rwx "$workers_root"
while IFS= read -r -d '' state_dir; do
  setfacl -m u:pdftotex:rwx,d:u:pdftotex:rwx "$state_dir"
done < <(find "$workers_root" -type d -name .state -print0)

escaped_root="${deployment_root//\\/\\\\}"
escaped_root="${escaped_root//&/\\&}"
escaped_root="${escaped_root//|/\\|}"
render_dir="$(mktemp -d)"
trap 'rm -rf "$render_dir"' EXIT

services=(
  "$repo_root/deploy/systemd/pdftotex-realtime-monitor.service"
  "$repo_root/deploy/systemd/pdftotex-daily-stats.service"
)
timers=(
  "$repo_root/deploy/systemd/pdftotex-realtime-monitor.timer"
  "$repo_root/deploy/systemd/pdftotex-daily-stats.timer"
)

for source in "${services[@]}"; do
  target="$render_dir/$(basename "$source")"
  sed "s|@PDFTOTEX_ROOT@|$escaped_root|g" "$source" > "$target"
  install -m 0644 "$target" "$unit_dir/"
done
install -m 0644 "${timers[@]}" "$unit_dir/"
systemd-analyze verify \
  "$unit_dir/pdftotex-realtime-monitor.service" \
  "$unit_dir/pdftotex-realtime-monitor.timer" \
  "$unit_dir/pdftotex-daily-stats.service" \
  "$unit_dir/pdftotex-daily-stats.timer"
systemctl daemon-reload
systemctl enable --now \
  pdftotex-realtime-monitor.timer \
  pdftotex-daily-stats.timer
